"""The object schema this platform pushes into JSM Assets, and the rows for it.

WHY A CONTAINMENT TREE AND NOT A FLAT DEVICE TABLE. Assets earns its keep
through REFERENCE attributes: a Device points at its Rack, which points at its
Room, which points at its Datacenter. That is what makes AQL dot-notation work
on the Jira side - `Rack.Room.Datacenter = "DC1"` - so an agent can scope a
queue to one hall without this platform having denormalised a location string
onto every object. A flat export would be a spreadsheet with a Jira licence.

THE ORDER MATTERS AND IS NOT ALPHABETICAL. A reference attribute can only be
set once the object it points at exists, so the types are declared parent
first and the rows are emitted in the same order. Getting this wrong does not
error: Assets accepts the object and leaves the reference empty, and the tree
silently has no branches.

WHAT IS DELIBERATELY NOT EXPORTED:

* **Telemetry.** Assets is a CMDB, not a time series, and a temperature that
  is correct for ninety seconds does not belong in a system nobody polls.
* **Alarm state.** It would be stale the moment it landed, and an agent
  reading "OK" off a CI while the console shows a critical is worse than an
  agent with no opinion at all.
* **Credentials, addresses of management interfaces.** An Assets object is
  visible to every agent on the service desk.

What IS exported is what somebody raising a ticket needs to identify a machine
and find it: what it is, where it stands, what it cost, and who to claim
against.
"""

from __future__ import annotations

from typing import Any

#: Object types, PARENT FIRST. See the module docstring - a reference cannot
#: be set before its target exists, and getting the order wrong produces a
#: tree with no branches rather than an error.
#:
#: `key` is the DCIM's own name for the type; `label` is what appears in Jira.
#: Each attribute is (jira label, dcim field, kind), where kind is "text",
#: "integer", "date", "status" or a "ref:<type key>".
SCHEMA: tuple[dict[str, Any], ...] = (
    {
        "key": "datacenter", "label": "Datacenter",
        # The object key an agent sees in a picker. Unique per type, and
        # stable: an Assets object whose label changes is one an agent's
        # saved filter stops finding.
        "label_attribute": "Code",
        "attributes": [
            ("Code", "code", "text"),
            ("Name", "name", "text"),
        ],
    },
    {
        "key": "room", "label": "Room",
        "label_attribute": "Name",
        "attributes": [
            ("Name", "name", "text"),
            ("Datacenter", "datacenter_key", "ref:datacenter"),
        ],
    },
    {
        "key": "rack", "label": "Rack",
        "label_attribute": "Name",
        "attributes": [
            ("Name", "name", "text"),
            ("Room", "room_key", "ref:room"),
            ("Height (U)", "u_height", "integer"),
        ],
    },
    {
        "key": "vendor", "label": "Vendor",
        "label_attribute": "Name",
        "attributes": [("Name", "name", "text")],
    },
    {
        "key": "model", "label": "Model",
        "label_attribute": "Name",
        "attributes": [
            ("Name", "name", "text"),
            ("Vendor", "vendor_key", "ref:vendor"),
            ("Height (U)", "u_height", "integer"),
        ],
    },
    {
        "key": "device", "label": "Device",
        # The DCIM's name, not the serial: it is what the console shows, what
        # an alarm's summary leads with, and what somebody says out loud.
        "label_attribute": "Name",
        "attributes": [
            ("Name", "name", "text"),
            ("Type", "device_type", "text"),
            ("Model", "model_key", "ref:model"),
            ("Serial number", "serial_number", "text"),
            ("Asset tag", "asset_tag", "text"),
            ("Rack", "rack_key", "ref:rack"),
            ("Room", "room_key", "ref:room"),
            ("Rack unit", "u_start", "integer"),
            ("Lifecycle", "lifecycle", "text"),
            ("Commissioned", "commissioned_at", "date"),
            ("Warranty expires", "warranty_expires", "date"),
            ("Purchase order", "purchase_order", "text"),
            # The DCIM's own id, so a later run updates rather than duplicates
            # and so a human can get back from a CI to the thing itself.
            ("DCIM id", "id", "text"),
        ],
    },
)

BY_KEY = {entry["key"]: entry for entry in SCHEMA}


def object_key(type_key: str, value: str) -> str:
    """The stable external key one object is identified by across runs.

    Prefixed by type, because an Assets import matches on this value and two
    types sharing a naming convention - a room called "DC1" and a datacenter
    called "DC1" - would otherwise collapse into one object.

    It is the DCIM's own identifier, never a display name. A rack that gets
    renamed must stay the same object, or every device in it loses its parent
    and an agent's saved filter quietly returns nothing.
    """
    return f"{type_key}:{value}"


def rows_for(inventory: dict[str, list[dict[str, Any]]]
             ) -> list[dict[str, Any]]:
    """Flatten the DCIM's inventory into the import's object rows.

    One list, in schema order, each row carrying its type and its external
    key. The Imports API takes them together; ordering them here rather than
    making the caller do it is what keeps the parent-first rule in one place.
    """
    out: list[dict[str, Any]] = []
    for entry in SCHEMA:
        for row in inventory.get(entry["key"], ()):
            # The DCIM's own id, and ONLY that. Falling back to a code or a
            # name looks harmless and is a rename hazard: an object keyed by
            # its name becomes a SECOND object the day somebody renames it,
            # and the first is orphaned in Assets with every reference still
            # pointing at it. A row with no id is dropped instead.
            key = row.get("id")
            if not key:
                continue
            out.append({
                "objectTypeKey": entry["key"],
                "objectKey": object_key(entry["key"], str(key)),
                "attributes": _attributes(entry, row),
            })
    return out


def _attributes(entry: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    attrs: dict[str, Any] = {}
    for label, field, kind in entry["attributes"]:
        value = row.get(field)
        if value is None or value == "":
            # Absent, not blank. An Assets import that sends an empty string
            # OVERWRITES whatever is there, so a device whose serial this
            # platform does not know would erase one somebody typed in by
            # hand.
            continue
        if kind.startswith("ref:"):
            attrs[label] = object_key(kind[4:], str(value))
        elif kind == "integer":
            try:
                attrs[label] = int(value)
            except (TypeError, ValueError):
                continue
        elif kind == "date":
            attrs[label] = _date(value)
        else:
            attrs[label] = str(value)
    return attrs


def _date(value: Any) -> str:
    text = str(value)
    # Assets wants a date, and a timestamp with a zone in a date field is
    # rejected by the import rather than truncated.
    return text[:10]
