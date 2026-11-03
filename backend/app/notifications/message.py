"""What an alarm email says.

Written for the person reading it on a phone at 03:00, so the order is fixed:
the subject carries severity, what happened and where, because a lock screen
shows nothing else; the body leads with the same three, then the reading,
then the link. Plain text only: it renders identically in every client and in
an email-to-SMS gateway, and an HTML alarm template is a thing to maintain.

Threading: the raise gets a stable Message-ID and its escalation and clear
reference it, so a mail client files a condition's whole life as one thread.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

VERB = {"alarm_raised": "raised", "alarm_escalated": "escalated", "alarm_cleared": "cleared"}


def _when(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return "unknown"


def location(a: dict[str, Any]) -> str:
    parts = [a.get("datacenter_code"), a.get("room_name"), a.get("rack_name")]
    where = " / ".join(p for p in parts if p)
    if a.get("u_start") and a.get("rack_name"):
        where += f", U{a['u_start']}"
    return where or "no location recorded"


def subject(kind: str, a: dict[str, Any]) -> str:
    device = a.get("device_name") or "unknown device"
    what = (a.get("message") or a.get("alarm_type") or "alarm").strip()
    if len(what) > 90:
        what = what[:89] + "…"
    if kind == "alarm_cleared":
        return f"[CLEARED] {device}: {what}"
    tag = a.get("severity") or "ALARM"
    if kind == "alarm_escalated":
        return f"[{tag}] Escalated - {device}: {what}"
    return f"[{tag}] {device}: {what}"


def body(
    kind: str,
    a: dict[str, Any],
    *,
    rule_name: str,
    rule_summary: str,
    org_name: str,
    link_base_url: str,
) -> str:
    verb = VERB.get(kind, "updated")
    sev = "Alarm" if kind == "alarm_cleared" else (a.get("severity") or "Alarm")
    at = (
        a.get("cleared_at")
        if kind == "alarm_cleared"
        else (a.get("last_seen") if kind == "alarm_escalated" else a.get("first_seen"))
    )
    # A clear with no recorded time (a manual clear racing the sweep) still says
    # what happened; "cleared at unknown" reads like a fault in the email itself.
    lines = [f"{sev} {verb} at {_when(at)}" if at else f"{sev} {verb}", ""]

    device = a.get("device_name") or "unknown device"
    ip = str(a.get("mgmt_ip") or "")
    # inet text carries the prefix length; a host address reads better without it.
    ip = ip.removesuffix("/32").removesuffix("/128")
    extra = ", ".join(x for x in (a.get("device_type"), ip) if x)
    rows = [
        ("Device", f"{device} ({extra})" if extra else device),
        ("Location", location(a)),
        ("Condition", f"{a.get('message') or a.get('alarm_type')}"),
    ]
    if a.get("trigger_value") is not None:
        reading = f"{a['trigger_value']:g}"
        if a.get("threshold") is not None:
            reading += f" (threshold {a['threshold']:g})"
        rows.append(("Reading", reading))
    rows.append(
        (
            "Category",
            " / ".join(x for x in (a.get("category"), a.get("response_class")) if x)
            or "uncategorised",
        )
    )
    if kind != "alarm_raised":
        rows.append(("First seen", _when(a.get("first_seen"))))
    if (a.get("occurrence_count") or 0) > 1:
        rows.append(("Occurrences", str(a["occurrence_count"])))
    if a.get("acknowledged_by"):
        rows.append(
            ("Acknowledged", f"by {a['acknowledged_by']} at {_when(a.get('acknowledged_at'))}")
        )
    if a.get("asset_tag"):
        rows.append(("Asset tag", a["asset_tag"]))
    width = max(len(k) for k, _ in rows) + 2
    lines += [f"{(k + ':').ljust(width)}{v}" for k, v in rows]

    if link_base_url and a.get("id"):
        lines += ["", f"Open in DCIM: {link_base_url}/alarms/{a['id']}"]
    lines += [
        "",
        "--",
        f"Sent by {org_name} DCIM because rule “{rule_name}” emails {rule_summary}.",
        "Change who receives this in Settings > Notifications.",
    ]
    return "\n".join(lines) + "\n"


def rule_summary(policy: dict[str, Any], *, notify_on_clear: bool) -> str:
    """One phrase: what a rule emails about, for the footer and the settings list."""
    sev = policy.get("min_severity") or "MAJOR"
    sevs = ["CRITICAL", "MAJOR", "MINOR", "WARNING", "INFO"]
    upto = sevs[: sevs.index(sev) + 1] if sev in sevs else [sev]
    level = (
        sev
        if len(upto) == 1
        else (f"{upto[0]} and {upto[1]}" if len(upto) == 2 else f"{sev} or worse")
    )
    classes = policy.get("response_classes") or ["alarm"]
    what = (
        "alarms"
        if classes == ["alarm"]
        else "alerts"
        if classes == ["alert"]
        else "alarms and alerts"
    )
    phrase = f"{level} {what}"
    from app.core.alert_taxonomy import CATEGORIES

    cats = policy.get("categories") or []
    if cats and set(cats) != set(CATEGORIES):
        phrase += f" in {', '.join(c.replace('_', ' ') for c in cats)}"
    delay = int(policy.get("dwell_s") or 0)
    if delay:
        phrase += f" active for {delay // 60} min" if delay >= 60 else f" active for {delay} s"
    if notify_on_clear:
        phrase += ", and when they clear"
    return phrase
