"""What makes a poll profile valid, and what makes one dangerous.

A profile is shared. `redfish-60s` is on 310 endpoints and `snmp-bmc-120s` on
another 310, so an interval typed here is multiplied by three hundred before it
reaches a network - and the assignment ETag already digests poll settings, so
every collector picks the change up within one assignment interval. There is no
staging step between this form and the estate.

The rules below are the ones that turn a plausible number into a refusal.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.mappings_gen import MAPPING_GROUPS

#: Slug, because the importer references profiles BY NAME.
#:
#: `poll_profile="snmp-server-120s"` is written into app/importer/endpoints.py;
#: a name with a space or a capital in it is a profile the importer can never
#: select, and the failure appears at the next import as endpoints landing on
#: the wrong profile rather than as an error here.
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")

#: The fastest a profile may poll.
#:
#: Not a protocol limit - a floor against a keystroke. One second on a profile
#: with three hundred endpoints behind it is 300 requests a second at gear that
#: answers SNMP from a single-threaded agent, and the first symptom is the
#: devices going unreachable rather than anything looking like a settings
#: mistake. Ten seconds is the fastest anything in this estate polls today
#: (snmp-sensor-10s) and is a deliberate exception rather than a default.
MIN_INTERVAL_S = 5
MAX_INTERVAL_S = 86_400

MIN_TIMEOUT_MS = 250
MAX_TIMEOUT_MS = 120_000
MAX_RETRIES = 5


class PollProfileError(ValueError):
    """A rejected profile, with a message written for the operator."""


def validate(profile: dict[str, Any], *, existing_names: set[str] | None = None,
             creating: bool = False) -> dict[str, Any]:
    """Check one profile end to end and return the cleaned values."""
    out: dict[str, Any] = {}

    if creating:
        name = str(profile.get("name", "")).strip()
        if not NAME_RE.match(name):
            raise PollProfileError(
                "a profile name is a slug: lowercase letters, digits and "
                "hyphens, 3-64 characters. The importer selects profiles by "
                "name, so anything else is a profile it can never pick.")
        if existing_names and name in existing_names:
            raise PollProfileError(f"a profile called {name} already exists")
        out["name"] = name

    interval = _int(profile, "interval_s", 0, MAX_INTERVAL_S)
    push = bool(profile.get("push_enabled", False))
    timeout = _int(profile, "timeout_ms", MIN_TIMEOUT_MS, MAX_TIMEOUT_MS)
    retries = _int(profile, "retries", 0, MAX_RETRIES)

    # Interval 0 is not "as fast as possible". Paired with push it means the
    # DEVICE decides when to send and the scheduler must never poll this
    # endpoint - the gnmi-stream profile. Without push it means an endpoint
    # nothing ever collects, which reports healthy and delivers nothing.
    if interval is not None:
        if interval == 0 and not push:
            raise PollProfileError(
                "interval 0 means the device pushes on its own schedule, so it "
                "only makes sense with push enabled. A profile that polls at 0 "
                "and receives nothing is never collected at all.")
        if 0 < interval < MIN_INTERVAL_S:
            raise PollProfileError(
                f"the shortest interval allowed is {MIN_INTERVAL_S}s. A "
                f"profile is shared by hundreds of endpoints, and the interval "
                f"is multiplied by every one of them.")
        out["interval_s"] = interval

    if timeout is not None:
        out["timeout_ms"] = timeout
    if retries is not None:
        out["retries"] = retries
    out["push_enabled"] = push if "push_enabled" in profile else None
    if out["push_enabled"] is None:
        del out["push_enabled"]

    if "metric_groups" in profile:
        out["metric_groups"] = _groups(profile["metric_groups"])

    return out


def check_timing(interval_s: int, timeout_ms: int, retries: int) -> None:
    """The worst-case attempt has to fit inside the interval.

    (retries + 1) x timeout is how long one endpoint can hold a worker before
    giving up. Longer than the interval and the next cycle starts while the
    last one is still running: the queue grows, latency climbs, and the
    endpoint eventually reports unreachable for a reason that has nothing to do
    with the device.
    """
    if interval_s == 0:                      # pushed; nothing is scheduled
        return
    worst_ms = (retries + 1) * timeout_ms
    if worst_ms > interval_s * 1000:
        raise PollProfileError(
            f"{retries} retries at {timeout_ms} ms is up to "
            f"{worst_ms / 1000:.0f}s per attempt, which is longer than the "
            f"{interval_s}s interval. The next poll would start before the "
            f"last one gave up.")


def _int(profile: dict[str, Any], key: str, lo: int, hi: int) -> int | None:
    if key not in profile or profile[key] is None:
        return None
    try:
        n = int(profile[key])
    except (TypeError, ValueError):
        raise PollProfileError(f"{key} must be a whole number") from None
    if not (lo <= n <= hi):
        raise PollProfileError(f"{key} must be between {lo} and {hi}")
    return n


def _groups(raw: Any) -> list[str]:
    """Group names the SNMP adapter will actually look up.

    A name that is not in the mapping file is not a slow poll or a partial one:
    the adapter finds no profile block, reads nothing, and the endpoint reports
    healthy with no metrics behind it. That is the failure this refusal exists
    to prevent, because it looks exactly like a device that has gone quiet.
    """
    if not isinstance(raw, (list, tuple)):
        raise PollProfileError("metric groups are a list of names")
    known = set(MAPPING_GROUPS.get("snmp", ()))
    groups: list[str] = []
    for g in raw:
        name = str(g).strip()
        if not name:
            continue
        if name not in known:
            raise PollProfileError(
                f"'{name}' is not a group any mapping file defines. "
                f"Available: {', '.join(sorted(known))}.")
        if name not in groups:
            groups.append(name)
    return groups
