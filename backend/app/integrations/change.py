"""A maintenance window, rendered as a change request.

THIS IS THE HALF NO SURVEYED PRODUCT SHIPS. Nlyte does deep change management
against ServiceNow; Device42, Sunbird, NetBox and Hyperview do none against
Jira. What every one of them leaves out is the number a change advisory board
actually wants, which the DCIM is the only system that can compute:

    "this window would shelve 43 alarms across 11 racks, darken 12 machines
     and leave 3 more on a single feed"

That comes from `POST /maintenance/windows/preview`, which already exists and
already traverses the power and impact graphs. A CAB reviewing a window
without it is approving a title and a time range.

APPROVAL IS READ FROM STATUS NAMES, and that is a deliberate retreat from
cleverness. JSM approvals can arrive as a first-class approval object, as a
workflow transition, or as a custom field, and which one a customer uses is a
property of their workflow rather than of Jira. Every other branch in this
package reads the status CATEGORY precisely because names vary - but a change
workflow's categories are useless here: "Awaiting approval", "Approved" and
"Implementing" are all `indeterminate`. So the names are configuration, with
defaults that match Jira's own change-management template, and an unrecognised
status leaves the window exactly where it was rather than guessing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.integrations import adf

#: Statuses that mean a human said yes. Jira's own change-management workflow
#: uses the first three; the rest are what teams rename them to.
DEFAULT_APPROVED = ("approved", "scheduled", "implementing", "in progress",
                    "authorised", "authorized")

#: Statuses that mean a human said no. A declined change CANCELS its window:
#: the work is not happening, and a window left scheduled would shelve alarms
#: at 02:00 for maintenance nobody is performing.
DEFAULT_DECLINED = ("declined", "rejected", "cancelled", "canceled",
                    "not approved")

PENDING = "pending"
APPROVED = "approved"
DECLINED = "declined"


def decide(status: str | None, *, approved: tuple[str, ...] = DEFAULT_APPROVED,
           declined: tuple[str, ...] = DEFAULT_DECLINED) -> str | None:
    """What a status name says about approval, or None for "it says nothing".

    None is the important return. A change moving from "Draft" to "Awaiting
    CAB" is a real transition that means approval has NOT been granted, and
    treating every unrecognised status as a decline would cancel windows for
    paperwork.
    """
    if not status:
        return None
    name = status.strip().casefold()
    if name in {s.casefold() for s in declined}:
        return DECLINED
    if name in {s.casefold() for s in approved}:
        return APPROVED
    return None


def summary(window: dict[str, Any]) -> str:
    kind = (window.get("kind") or "planned").upper()
    return f"[{kind} CHANGE] {window.get('title') or 'Maintenance window'}"[:255]


def description(window: dict[str, Any], preview: dict[str, Any],
                targets: list[dict[str, Any]], *,
                dcim_url: str | None = None) -> dict[str, Any]:
    """The body a change advisory board reads.

    Ordered for the reader rather than for the writer: what is being done, what
    it costs if it goes wrong, then the equipment list. A board that has to
    scroll past forty device names to find the impact is a board that stops
    reading the impact.
    """
    rows: list[tuple[str, str]] = [
        ("Window", f"{_when(window.get('starts_at'))} to "
                   f"{_when(window.get('ends_at'))} ({_duration(window)})"),
        ("Kind", str(window.get("kind") or "planned")),
        ("Requested by", str(window.get("created_by") or "-")),
        ("Equipment", str(len(targets))),
        ("Alarms it will silence", str(preview.get("alarms_currently_active") or 0)),
        ("Machines it darkens", str(preview.get("cut_off") or 0)),
        ("Machines it leaves degraded",
         str(max(0, int(preview.get("downstream_devices") or 0)
                 - int(preview.get("cut_off") or 0)))),
        ("Alarm suppression",
         "on - alarms on this equipment will not page" if window.get("suppress")
         else "OFF - alarms will page as usual"),
    ]

    warnings = preview.get("redundancy_warnings") or []
    blocks: list[dict[str, Any] | None] = [
        adf.paragraph(str(window.get("description")
                          or "No description was given.")),
        adf.paragraph(adf.text("Open in the DCIM", adf.link(dcim_url)))
        if dcim_url else None,
        adf.table(rows, header=("", "")),
    ]

    if warnings:
        # The single most important thing on the page, so it is not at the
        # bottom under the device list. A window that costs a redundant side
        # is the one a board exists to catch.
        blocks.append(adf.heading("Redundancy this window removes", level=4))
        for warning in warnings[:20]:
            blocks.append(adf.paragraph(
                adf.text(str(warning.get("device") or "?"), adf.strong()),
                adf.text(f" - {warning.get('detail') or warning.get('reason') or ''}")))

    blocks.append(adf.rule())
    blocks.append(adf.heading("Equipment in this window", level=4))
    blocks.append(adf.table(
        [(str(t.get("name") or t.get("id")), _location(t)) for t in targets[:60]],
        header=("Device", "Location")))
    if len(targets) > 60:
        blocks.append(adf.paragraph(
            f"... and {len(targets) - 60} more, listed in the DCIM."))

    return adf.doc(*blocks)


def completion_comment(window: dict[str, Any], report: dict[str, Any], *,
                       dcim_url: str | None = None) -> dict[str, Any]:
    """What the window actually cost, posted when it closes.

    The useful line is the last one. "Did anything ELSE break while we were in
    there" is the question asked after every window, and the shelved set is the
    only place it can be answered - an alarm that was raised DURING the work,
    on equipment in the window, is invisible on the console by design and would
    otherwise be found days later.
    """
    still_open = report.get("still_open") or []
    cleared = int(report.get("cleared") or 0)
    shelved = int(report.get("shelved") or 0)

    lead = (f"Window closed {_when(window.get('ends_at'))} "
            f"({report.get('outcome') or 'completed'}).")
    counts = (f"{shelved} alarm{'' if shelved == 1 else 's'} were shelved; "
              f"{cleared} cleared during the work.")

    blocks: list[dict[str, Any] | None] = [
        adf.paragraph(adf.text(lead, adf.strong()), adf.text(f" {counts}")),
    ]

    if still_open:
        blocks.append(adf.paragraph(adf.text(
            f"{len(still_open)} condition"
            f"{'' if len(still_open) == 1 else 's'} on this equipment "
            f"{'is' if len(still_open) == 1 else 'are'} still open:",
            adf.strong())))
        blocks.append(adf.table(
            [(str(a.get("device_name") or "-"),
              f"{a.get('severity')} - {a.get('message') or a.get('alarm_type')}")
             for a in still_open[:25]],
            header=("Device", "Condition")))
    else:
        blocks.append(adf.paragraph(
            "Nothing on this equipment is alarming now."))

    if dcim_url:
        blocks.append(adf.paragraph(
            adf.text("Open the window in the DCIM", adf.link(dcim_url))))
    return adf.doc(*blocks)


def window_url(dcim_base: str | None, window: dict[str, Any]) -> str | None:
    if not dcim_base or not window.get("id"):
        return None
    return f"{dcim_base.rstrip('/')}/assets/maintenance/{window['id']}"


# ------------------------------------------------------------------ pieces

def _location(target: dict[str, Any]) -> str:
    parts = [target.get("datacenter_code"), target.get("room_name"),
             target.get("rack_name")]
    return " / ".join(str(p) for p in parts if p) or "-"


def _when(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes")
    return str(value) if value else "-"


def _duration(window: dict[str, Any]) -> str:
    starts, ends = window.get("starts_at"), window.get("ends_at")
    if not isinstance(starts, datetime) or not isinstance(ends, datetime):
        return "-"
    minutes = int((ends - starts).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, rest = divmod(minutes, 60)
    return f"{hours}h" if not rest else f"{hours}h {rest}m"
