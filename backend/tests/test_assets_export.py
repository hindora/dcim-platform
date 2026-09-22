"""The CMDB export: the object tree, the mapping, and the import flow.

**This is the one part of the integration that has never run against a real
tenant** - Assets needs JSM Premium or Enterprise - so the tests carry more of
the weight than usual. They cover everything that does NOT depend on
Atlassian's behaviour being what the docs say: the tree's shape, the reference
keys, the attribute rules, the chunking, the idempotency keys and the order of
the HATEOAS calls.

What they cannot cover is whether Atlassian's protocol is as documented. That
is stated in the module under test rather than implied here.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.jira import assets
from app.integrations.jira import assets_schema as schema
from app.integrations.jira.client import JiraError

INVENTORY: dict[str, list[dict[str, Any]]] = {
    "datacenter": [{"id": "dc1", "code": "DC1", "name": "Chicago"}],
    "room": [{"id": "rm1", "name": "Server Hall A", "datacenter_key": "dc1"}],
    "rack": [{"id": "rk1", "name": "R03", "room_key": "rm1", "u_height": 42}],
    "vendor": [{"id": "v1", "name": "Supermicro"}],
    "model": [{"id": "m1", "name": "SYS-121H", "vendor_key": "v1",
               "u_height": 1}],
    "device": [{"id": "d1", "name": "SRV01-DC1-HA-R3-01", "device_type": "server",
                "model_key": "m1", "rack_key": "rk1", "room_key": "rm1",
                "u_start": 12, "serial_number": "S12345",
                "asset_tag": None, "lifecycle": "in_service",
                "commissioned_at": "2024-03-01T00:00:00+00:00",
                "warranty_expires": "2027-03-01", "purchase_order": "PO-99"}],
}


def rows():
    return schema.rows_for(INVENTORY)


def by_type(kind: str):
    return [r for r in rows() if r["objectTypeKey"] == kind]


# ----------------------------------------------------------- the tree

def test_parents_are_emitted_before_their_children():
    """A reference attribute can only be set once its target exists. Getting
    this wrong does not error - Assets accepts the object and leaves the
    reference empty, and the tree silently has no branches."""
    order = [r["objectTypeKey"] for r in rows()]
    assert order.index("datacenter") < order.index("room")
    assert order.index("room") < order.index("rack")
    assert order.index("rack") < order.index("device")
    assert order.index("vendor") < order.index("model")
    assert order.index("model") < order.index("device")


def test_a_device_points_at_its_rack_by_the_racks_own_key():
    """Dot-notation on the Jira side - `Rack.Room.Datacenter = "DC1"` - is
    what makes an agent able to scope a queue to one hall, and it only works
    if the reference is a real object key."""
    device = by_type("device")[0]
    assert device["attributes"]["Rack"] == schema.object_key("rack", "rk1")
    assert by_type("rack")[0]["objectKey"] == schema.object_key("rack", "rk1")


def test_the_chain_reaches_all_the_way_up():
    rack = by_type("rack")[0]
    room = by_type("room")[0]
    assert rack["attributes"]["Room"] == room["objectKey"]
    assert room["attributes"]["Datacenter"] == by_type("datacenter")[0]["objectKey"]


def test_object_keys_are_namespaced_by_type():
    """A room called DC1 and a datacenter called DC1 would otherwise collapse
    into one object, because an import matches on this value."""
    assert schema.object_key("room", "DC1") != schema.object_key("datacenter", "DC1")


def test_the_key_is_the_dcim_id_and_not_a_display_name():
    """A rack that gets renamed must stay the same object, or every device in
    it loses its parent and an agent's saved filter quietly returns nothing."""
    assert by_type("rack")[0]["objectKey"] == "rack:rk1"
    assert "R03" not in by_type("rack")[0]["objectKey"]


# ------------------------------------------------------- attribute rules

def test_an_unknown_value_is_omitted_rather_than_blanked():
    """An import that sends an empty string OVERWRITES what is there. A
    device whose asset tag this platform does not know would erase one
    somebody typed in by hand."""
    device = by_type("device")[0]
    assert "Asset tag" not in device["attributes"]
    assert device["attributes"]["Serial number"] == "S12345"


def test_a_timestamp_is_cut_down_to_a_date():
    """Assets rejects a zoned timestamp in a date field rather than
    truncating it."""
    assert by_type("device")[0]["attributes"]["Commissioned"] == "2024-03-01"


def test_an_integer_field_is_sent_as_a_number():
    assert by_type("device")[0]["attributes"]["Rack unit"] == 12
    assert by_type("rack")[0]["attributes"]["Height (U)"] == 42


def test_an_unparseable_integer_is_dropped_not_sent_as_text():
    out = schema.rows_for({"rack": [{"id": "rk9", "name": "X",
                                     "u_height": "forty-two"}]})
    assert "Height (U)" not in out[0]["attributes"]


def test_an_object_with_no_identifier_is_dropped():
    """It could not be updated on the next run, so it would be duplicated on
    every one."""
    assert schema.rows_for({"vendor": [{"name": "Nameless"}]}) == []


def test_telemetry_and_alarm_state_are_not_exported():
    """Assets is a CMDB, not a time series. A temperature correct for ninety
    seconds does not belong in a system nobody polls, and an agent reading
    "OK" off a CI while the console shows a critical is worse than an agent
    with no opinion."""
    exported = {label for entry in schema.SCHEMA
                for label, _f, _k in entry["attributes"]}
    for forbidden in ("Severity", "Status", "Temperature", "Health",
                      "Management address", "Power"):
        assert forbidden not in exported


# ------------------------------------------------------------ the mapping

CACHED = {
    entry["label"]: {
        "id": str(100 + n),
        "attributes": {label: str(200 + n * 20 + i)
                       for i, (label, _f, _k) in enumerate(entry["attributes"])},
    }
    for n, entry in enumerate(schema.SCHEMA)
}


def test_the_mapping_addresses_attributes_by_numeric_id():
    """Everything in the Assets API is addressed by numeric attribute id,
    never by name. This is the single biggest source of friction in any
    Assets integration."""
    doc = assets.mapping_document(CACHED, "7")
    device = next(m for m in doc["objectTypeMappings"]
                  if m["objectTypeName"] == "Device")
    ids = {a["objectTypeAttributeId"] for a in device["attributesMapping"]}
    assert all(i.isdigit() for i in ids)


def test_the_mapping_skips_attributes_the_schema_does_not_have():
    """An import naming an attribute the schema lacks is rejected WHOLE, so a
    1,500-device run would die on the first chunk over one renamed field."""
    thin = {**CACHED, "Device": {"id": "105",
                                 "attributes": {"Name": "500"}}}
    doc = assets.mapping_document(thin, "7")
    device = next(m for m in doc["objectTypeMappings"]
                  if m["objectTypeName"] == "Device")
    assert [a["attributeName"] for a in device["attributesMapping"]] == ["Name"]


def test_missing_attributes_are_listed_before_anything_is_sent():
    thin = {**CACHED, "Device": {"id": "105",
                                 "attributes": {"Name": "500"}}}
    gaps = assets.missing_attributes(thin)
    assert "Device.Serial number" in gaps
    assert "Device.Name" not in gaps


def test_a_completely_absent_object_type_is_reported_as_such():
    without = {k: v for k, v in CACHED.items() if k != "Rack"}
    assert "Rack (object type is missing)" in assets.missing_attributes(without)


def test_a_schema_that_matches_has_no_gaps():
    assert assets.missing_attributes(CACHED) == []


# --------------------------------------------------------------- chunking

def test_objects_are_chunked():
    many = [{"objectKey": f"k{n}"} for n in range(600)]
    batches = assets.chunks(many)
    assert [len(b) for b in batches] == [250, 250, 100]
    assert sum(len(b) for b in batches) == 600


def test_an_empty_estate_produces_no_batches():
    assert assets.chunks([]) == []


# ------------------------------------------------------------ the run flow

class FakeClient:
    """Records the HATEOAS walk. The links are deliberately opaque strings:
    the code under test must FOLLOW them, never construct them."""

    def __init__(self, *, status="IDLE", info_links=None):
        self.calls: list[tuple[str, str, Any]] = []
        self.status = status
        self.info_links = info_links if info_links is not None else {
            "getStatus": "https://api.atlassian.com/opaque/status",
            "mapping": "https://api.atlassian.com/opaque/mapping",
            "start": "https://api.atlassian.com/opaque/start",
        }

    async def request(self, method, path, **kw):
        self.calls.append((method, path, kw.get("json_body")))
        if path.endswith("/imports/info"):
            return {"links": self.info_links}
        if path.endswith("/status"):
            return {"status": self.status}
        if path.endswith("/start"):
            return {"submitResults": "https://api.atlassian.com/opaque/results",
                    "submitProgress": "https://api.atlassian.com/opaque/progress",
                    "cancel": "https://api.atlassian.com/opaque/cancel"}
        return {}

    async def get(self, path, **kw):
        return await self.request("GET", path, **kw)

    async def post(self, path, **kw):
        return await self.request("POST", path, **kw)

    async def put(self, path, **kw):
        return await self.request("PUT", path, **kw)

    def paths(self, method=None):
        return [p for m, p, _ in self.calls if method is None or m == method]


async def test_the_run_follows_the_links_rather_than_building_them():
    """Atlassian's guide is explicit that the URLs are bound to the token that
    produced them. A stored or hand-edited link is the failure that looks like
    an authentication problem a week later."""
    client = FakeClient()
    run = assets.ImportRun(client)
    await run.begin()
    await run.start()
    assert "https://api.atlassian.com/opaque/status" in client.paths("GET")
    assert "https://api.atlassian.com/opaque/start" in client.paths("POST")


async def test_an_import_already_running_is_refused():
    """Two runs against one source interleave their objects, and the second
    one's completion ends the first. Better to skip a nightly sync than to
    corrupt one."""
    run = assets.ImportRun(FakeClient(status="RUNNING"))
    with pytest.raises(JiraError, match="already running"):
        await run.begin()


async def test_a_disabled_import_source_is_refused():
    run = assets.ImportRun(FakeClient(status="DISABLED"))
    with pytest.raises(JiraError, match="disabled"):
        await run.begin()


async def test_a_configuration_with_no_start_link_says_so():
    run = assets.ImportRun(FakeClient(info_links={}))
    with pytest.raises(JiraError, match="no start link"):
        await run.begin()


async def test_each_chunk_carries_its_own_idempotency_key():
    """A chunk re-sent after a timeout must not be applied twice, and this is
    the only thing that prevents it."""
    client = FakeClient()
    run = assets.ImportRun(client)
    await run.begin()
    await run.start()
    await run.submit([{"objectKey": "a"}], chunk_id="one")
    await run.submit([{"objectKey": "b"}], chunk_id="two")
    bodies = [b for m, p, b in client.calls
              if p.endswith("/results") and b and "clientGeneratedId" in b]
    assert [b["clientGeneratedId"] for b in bodies] == ["one", "two"]


async def test_completion_is_its_own_call():
    client = FakeClient()
    run = assets.ImportRun(client)
    await run.begin()
    await run.start()
    await run.finish()
    assert {"completed": True} in [b for _m, _p, b in client.calls]


async def test_a_failed_progress_report_does_not_fail_the_run():
    """Cosmetic. Failing the run over a progress bar would be throwing away
    the work to avoid a missing spinner."""
    class Breaks(FakeClient):
        async def put(self, path, **kw):
            raise JiraError("nope", status=500)

    client = Breaks()
    run = assets.ImportRun(client)
    await run.begin()
    await run.start()
    await run.progress(step=1, steps=2, description="x", processed=0, total=10)


# ------------------------------------------------------ licence detection

async def test_a_site_without_assets_is_a_licence_fact_not_a_failure():
    """A site on Standard is correctly configured and simply does not have
    Assets. Reporting that as "the connection failed" sends somebody looking
    for a credential problem that is not there."""
    class NoAssets:
        async def get(self, path, **kw):
            raise JiraError("not found", status=404)

    with pytest.raises(assets.AssetsUnavailableError, match="Premium"):
        await assets.discover_workspace(NoAssets())


async def test_an_empty_workspace_list_is_also_a_licence_fact():
    class Empty:
        async def get(self, path, **kw):
            return {"values": []}

    with pytest.raises(assets.AssetsUnavailableError):
        await assets.discover_workspace(Empty())


async def test_a_real_failure_is_not_disguised_as_a_licence_problem():
    """A 500 from Atlassian is an outage, and telling an operator to buy
    Premium would be a lie."""
    class Broken:
        async def get(self, path, **kw):
            raise JiraError("boom", status=500)

    with pytest.raises(JiraError) as caught:
        await assets.discover_workspace(Broken())
    assert not isinstance(caught.value, assets.AssetsUnavailableError)


async def test_the_workspace_id_is_returned_when_assets_exists():
    class Premium:
        async def get(self, path, **kw):
            return {"values": [{"workspaceId": "ws-123"}]}

    assert await assets.discover_workspace(Premium()) == "ws-123"


# --------------------------------------------------- what "nothing to do" is

def test_parents_are_always_present_so_rows_is_never_empty():
    """Which is why an idle run is decided on DEVICES rather than on rows.

    The parents - datacentres, rooms, racks, vendors, models - are sent whole
    every time, because a device whose rack was created since the last run
    would otherwise point at an object that does not exist, and Assets accepts
    that silently, leaving the tree with no branches.
    """
    quiet = {k: v for k, v in INVENTORY.items() if k != "device"}
    assert schema.rows_for(quiet) != []
    assert not [r for r in schema.rows_for(quiet)
                if r["objectTypeKey"] == "device"]
