"""An alarm, rendered as a Jira issue.

Three principles, each of which was a decision rather than a default:

**Names are resolved at runtime, never hardcoded.** Priority names, transition
ids and custom field ids are all per-site and all editable by a Jira admin. A
mapper that ships `"priority": {"id": "2"}` works on the tenant it was written
against and silently mis-prioritises on every other one. Everything here emits
NAMES where Jira accepts them and reads ids from the connection test's cache
where it does not.

**Taxonomy goes in labels, not custom fields.** A label needs no Jira
administrator, costs nothing to create, and matches in JQL at index speed.
Custom fields are reserved for the handful of values an operator will want to
sort a queue by. Grafana made the same call for the same reason.

**The summary leads with the device.** `[CRAH-DC1-HALLA-01] Supply air 28.4C`
rather than `Supply air high on CRAH-DC1-HALLA-01`, because a service desk
queue is read as a column of left-aligned text and the first eight characters
are all most rows get.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.integrations import adf
from app.integrations import fingerprint as fp

#: Jira rejects a summary over 255 characters with a field error naming the
#: field, which reads like a mapping bug rather than a long message.
MAX_SUMMARY = 255

#: Labels cannot contain whitespace. Jira silently splits on it in some
#: entrypoints and errors in others; neither is what the caller meant.
_LABEL_SAFE = str.maketrans({" ": "-", "\t": "-", "\n": "-", "/": "-"})


def summary(alarm: dict[str, Any]) -> str:
    device = alarm.get("device_name") or alarm.get("device_id") or "platform"
    message = alarm.get("message") or alarm.get("alarm_type") or "condition"
    instance = alarm.get("instance") or ""
    # The instance only when the message does not already carry it. Most
    # rendered messages name their slot; the ones that do not are exactly the
    # shared-OID conditions where the slot IS the distinguishing fact.
    if instance and instance not in message:
        text = f"[{device}] {message} ({instance})"
    else:
        text = f"[{device}] {message}"
    return text[:MAX_SUMMARY]


def labels(alarm: dict[str, Any], cfg: dict[str, Any], print_: str) -> list[str]:
    opts = cfg["labels"]
    prefix = opts.get("prefix") or "dcim"
    out = [prefix, fp.label(print_)]
    if opts.get("category") and alarm.get("category"):
        out.append(f"{prefix}-{alarm['category']}")
    if opts.get("site") and alarm.get("datacenter_code"):
        out.append(f"site-{alarm['datacenter_code']}")
    if opts.get("room") and alarm.get("room_name"):
        out.append(f"room-{alarm['room_name']}")
    # Deduplicated while keeping order: the fingerprint label must stay
    # findable and a set would scramble which one a human reads first.
    seen, clean = set(), []
    for label in out:
        text = str(label).strip().translate(_LABEL_SAFE)
        if text and text not in seen:
            seen.add(text)
            clean.append(text)
    return clean


def priority(alarm: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """The priority NAME, or None to leave it at the project default.

    None rather than a guess when the severity is unknown: a wrong priority is
    worse than none, because a queue sorted by priority will bury or shout
    about the wrong thing and nobody will know why.
    """
    return cfg["priority_map"].get(str(alarm.get("severity") or "")) or None


def location(alarm: dict[str, Any]) -> str:
    """Where to go and stand, in one line."""
    parts = [alarm.get("datacenter_code"), alarm.get("room_name"),
             alarm.get("rack_name")]
    line = " / ".join(str(p) for p in parts if p)
    if alarm.get("u_start") and alarm.get("rack_name"):
        line += f" U{alarm['u_start']}"
    return line


def description(alarm: dict[str, Any], *, dcim_url: str | None = None
                ) -> dict[str, Any]:
    """The ADF body: a sentence, the facts as a table, then the raw record.

    The table first and the JSON last, because the reader is a facilities
    engineer deciding whether to walk to a hall, not an integrator debugging a
    payload - but the payload is there, because the one time it is wanted it
    is wanted badly and asking the DCIM team to go and look is a day's delay.
    """
    measured = _measured(alarm)
    rows: list[tuple[str, str]] = [
        ("Device", str(alarm.get("device_name") or "-")),
        ("Type", str(alarm.get("device_type") or "-")),
        ("Location", location(alarm) or "-"),
        ("Condition", str(alarm.get("alarm_type") or "-")),
        ("Instance", str(alarm.get("instance") or "")),
        ("Severity", str(alarm.get("severity") or "-")),
        ("Category", str(alarm.get("category") or "-")),
        ("Detected by", str(alarm.get("detection") or alarm.get("source") or "-")),
        ("Measured", measured),
        ("First seen", _when(alarm.get("first_seen"))),
        ("Occurrences", str(alarm.get("occurrence_count") or "")),
        ("Management address", str(alarm.get("mgmt_ip") or "")),
        ("Serial", str(alarm.get("serial_number") or "")),
        ("Asset tag", str(alarm.get("asset_tag") or "")),
    ]

    link_para = None
    if dcim_url:
        link_para = adf.paragraph(
            adf.text("Open in the DCIM", adf.link(dcim_url)))

    return adf.doc(
        adf.paragraph(str(alarm.get("message") or "")),
        link_para,
        adf.table(rows, header=("Attribute", "Value")),
        adf.rule(),
        adf.heading("Raw record", level=4),
        adf.json_block(alarm),
    )


def comment_for(kind: str, alarm: dict[str, Any], *,
                dcim_url: str | None = None) -> dict[str, Any]:
    """The body of an update comment.

    Deliberately short. An escalation or a clear is a one-line fact, and a
    comment that repeats the whole attribute table on every occurrence is how
    a ticket becomes unreadable by the time somebody opens it.
    """
    when = _when(alarm.get("cleared_at") or alarm.get("last_seen"))
    measured = _measured(alarm)
    if kind == "alarm_confirmed":
        lead = f"The condition cleared at {when}."
        detail = ("Posted after this ticket was closed: the plane confirms "
                  "the fault is actually gone, which closing a ticket by "
                  "itself does not.")
    elif kind == "alarm_cleared":
        lead = f"Cleared at {when}."
        detail = ("The condition is no longer met. This is the plane's own "
                  "measurement, not a statement that the work is finished.")
    elif kind == "alarm_escalated":
        lead = f"Escalated to {alarm.get('severity')} at {when}."
        detail = f"Now reading {measured}." if measured else ""
    else:
        lead = f"Re-asserted at {when}."
        detail = f"Occurrence {alarm.get('occurrence_count')}." \
            if alarm.get("occurrence_count") else ""

    return adf.doc(
        adf.paragraph(adf.text(lead, adf.strong()),
                      adf.text(f" {detail}") if detail else None),
        adf.paragraph(adf.text("Open in the DCIM", adf.link(dcim_url)))
        if dcim_url else None,
    )


def fields_for(alarm: dict[str, Any], cfg: dict[str, Any], print_: str, *,
               dcim_url: str | None = None) -> dict[str, Any]:
    """The `fields` object of a create, minus project and issue type.

    Those two are added by the target, because JSM adds them differently.
    """
    out: dict[str, Any] = {
        "summary": summary(alarm),
        "description": description(alarm, dcim_url=dcim_url),
        "labels": labels(alarm, cfg, print_),
    }
    name = priority(alarm, cfg)
    if name:
        out["priority"] = {"name": name}
    if cfg.get("components_enabled") and alarm.get("category"):
        out["components"] = [{"name": str(alarm["category"]).title()}]
    out.update(custom_fields(alarm, cfg))
    return out


def custom_fields(alarm: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    """Only the fields the operator actually mapped.

    An unmapped field is an empty string in `config`, and it is left OUT
    rather than sent as null: Jira rejects a create naming a field that is not
    on the project's create screen, so sending one we were not told about
    fails the whole ticket over a nice-to-have.
    """
    ids = cfg["fields"]
    out: dict[str, Any] = {}
    if ids.get("device") and alarm.get("device_name"):
        out[ids["device"]] = str(alarm["device_name"])
    if ids.get("location"):
        line = location(alarm)
        if line:
            out[ids["location"]] = line
    if ids.get("metric") and alarm.get("metric_key"):
        out[ids["metric"]] = str(alarm["metric_key"])
    if ids.get("first_seen") and alarm.get("first_seen"):
        out[ids["first_seen"]] = iso(alarm["first_seen"])
    if ids.get("occurrences") and alarm.get("occurrence_count") is not None:
        out[ids["occurrences"]] = int(alarm["occurrence_count"])
    return out


def occurrence_fields(alarm: dict[str, Any], cfg: dict[str, Any]
                      ) -> dict[str, Any]:
    """What a repeat occurrence updates IN PLACE instead of commenting.

    One write, not two, and no noise in the comment stream. It matters more
    than it looks: Jira limits writes against a single issue to 20 in two
    seconds, and a flapping condition aims the whole estate's traffic at one
    key.
    """
    ids = cfg["fields"]
    out: dict[str, Any] = {}
    if ids.get("occurrences") and alarm.get("occurrence_count") is not None:
        out[ids["occurrences"]] = int(alarm["occurrence_count"])
    return out


def remote_link(alarm: dict[str, Any], print_: str, *, base_url: str,
                dcim_url: str, resolved: bool) -> dict[str, Any]:
    """The link back to this DCIM.

    `globalId` is an upsert key on Jira's side, so posting this twice updates
    the link rather than making a second - which is the one idempotency guard
    that survives a crash between the HTTP call and our own commit.
    """
    return {
        "globalId": fp.global_id(base_url, print_),
        "application": {"type": "com.hindora.dcim", "name": "DCIM Platform"},
        "relationship": "caused by",
        "object": {
            "url": dcim_url,
            "title": f"Alarm {print_} - {alarm.get('device_name') or 'platform'}",
            "summary": (str(alarm.get("message") or ""))[:255],
            "status": {"resolved": resolved},
        },
    }


def alarm_url(dcim_base: str | None, alarm: dict[str, Any]) -> str | None:
    """Where an engineer clicks to see the condition itself.

    None when nobody configured a public base URL. A link to `localhost` on
    somebody else's service desk is worse than no link: it looks like it
    works, and the person who clicks it learns nothing except that this
    integration is careless.
    """
    if not dcim_base or not alarm.get("id"):
        return None
    return f"{dcim_base.rstrip('/')}/alarms?alarm={alarm['id']}"


# ------------------------------------------------------------------ pieces

def _measured(alarm: dict[str, Any]) -> str:
    value, threshold = alarm.get("trigger_value"), alarm.get("threshold")
    if value is None:
        return ""
    metric = alarm.get("metric_key") or "value"
    if threshold is None:
        return f"{metric} {_num(value)}"
    return f"{metric} {_num(value)} against {_num(threshold)}"


def _num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    # Two decimals, trailing zeros dropped. A temperature reported as
    # "28.399999999999999" is the kind of detail that makes a reader distrust
    # everything else on the ticket.
    return f"{number:.2f}".rstrip("0").rstrip(".")


def _when(value: Any) -> str:
    text = iso(value)
    return text or "-"


def iso(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value) if value else ""
