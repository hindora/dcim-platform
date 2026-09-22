"""The dedup key: which conditions are "the same problem".

This platform already answered that question once. `alarm_active_key` - the
unique partial index on (device_id, alarm_type, instance) WHERE state is not
CLEARED - is what makes raise/update/clear idempotent here, and it is what an
operator means when they say two rows are the same fault. A second, different
notion of sameness on the way out to Jira would let the DCIM and the service
desk disagree about it, which is worse than having no dedup at all: the
disagreement is invisible until somebody counts.

So the fingerprint is that key, hashed.

WHAT IS DELIBERATELY EXCLUDED, and why each one would be a bug:

* **severity** - a WARNING that escalates to CRITICAL is the same condition
  getting worse. Including it would open a second ticket for the escalation
  and leave the first one open beside it, which is exactly the pattern that
  makes an operator stop reading the queue.
* **the measured value** - every poll would be a new fault.
* **any timestamp** - same.
* **message** - it is a rendered template and two detectors phrase the same
  condition differently. `app/repositories/alarms.py` even rewrites it on the
  conflict path.
* **state** - an acknowledged alarm is still the same alarm.

WHAT IS DELIBERATELY INCLUDED:

* **instance** - not optional. Twenty-three Raritan conditions share one trap
  OID on this estate and are told apart only by the slot carried as the alarm
  instance. Drop it and every probe on a PDU collapses into one ticket that
  says "Airflow Alert" and means nothing.
* **a site scope** - two datacentres run the same equipment with the same
  device naming conventions. The device UUID is already unique, but the site
  is in the hash anyway so that a fingerprint read in a Jira label says which
  estate it came from when the same Jira serves two.
* **a version tag** - `dcim:v1:`. If the tuple ever has to change, v2
  fingerprints do not collide with v1 ones, so the old tickets stay findable
  instead of being silently re-opened as new ones.
"""

from __future__ import annotations

import hashlib
from typing import Any

#: Bump only when the TUPLE changes. Changing it orphans every open link, so
#: a bump is a migration, not an edit.
VERSION = "v1"

#: 16 hex characters = 64 bits. Long enough that a collision across an estate's
#: lifetime is not a thing that happens, short enough to read in a Jira label
#: and to type into a search box. The full digest buys nothing here: this is a
#: dedup key, not a security boundary, and nobody is trying to forge one.
LENGTH = 16


def compute(*, device_id: str | None, alarm_type: str, instance: str = "",
            site: str | None = None) -> str:
    """The fingerprint for one condition.

    ``device_id`` is None for platform alarms - the ones that say this
    platform's own monitoring is broken - and those are real conditions that
    deserve tickets, so None is a legitimate input rather than an error. It
    hashes as the empty string, which means every platform alarm of a given
    type and instance shares one fingerprint. That is correct: there is one
    ingest pipeline, and "ingest has stalled" is one fault however many times
    it is noticed.
    """
    parts = (
        f"dcim:{VERSION}",
        (site or "").strip().lower(),
        (device_id or "").strip().lower(),
        alarm_type.strip(),
        # NOT lowercased. An instance is an ifIndex, a BACnet object name, an
        # outlet label or an endpoint UUID, and at least one of those is
        # case-significant on the wire. Folding case here would merge two
        # genuinely different conditions.
        (instance or "").strip(),
    )
    # A separator that cannot appear in any part, so ("a", "bc") and ("ab",
    # "c") cannot hash alike. Colons appear in BACnet object names and UUIDs;
    # a NUL does not appear in any of them.
    raw = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:LENGTH]


def of_alarm(alarm: dict[str, Any]) -> str:
    """Fingerprint an alarm row as the repositories return it.

    Accepts both shapes the alarm layer produces: the thin dict from
    ``raise_alarm``/``clear_alarms`` and the wide one from the export query.
    Only the four identity fields are read, and all four are present in both.
    """
    return compute(
        device_id=alarm.get("device_id"),
        alarm_type=alarm["alarm_type"],
        instance=alarm.get("instance") or "",
        site=alarm.get("datacenter_code"),
    )


def label(fingerprint: str) -> str:
    """The Jira label that carries it.

    A label rather than a custom field, for the reason Grafana chose one:
    labels need no Jira administrator, cost nothing to create, and match in
    JQL at index speed. The prefix makes `labels = "fp-a3f9..."` unambiguous
    and `labels ~ "fp-*"` a usable sweep.
    """
    return f"fp-{fingerprint}"


def global_id(base_url: str, fingerprint: str) -> str:
    """The `globalId` of the remote link pointing back at this DCIM.

    Jira treats globalId as an upsert key: posting the same one twice updates
    the link instead of making a second. That is the third of the three
    idempotency guards in the dispatcher, and the only one that survives a
    crash between the HTTP call and our own commit.
    """
    return f"system={base_url.rstrip('/')}&alarm={fingerprint}"
