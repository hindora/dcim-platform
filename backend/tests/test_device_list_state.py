"""The device list's operational side: what state each row carries, how it
sorts on state, and the counts the page's header is built from.

Before this the list sorted only on asset columns, carried no alarm count and
had no counts of its own - the page showed 100 rows of 716 and a line saying
"more results available". The SQL is proved by running it; what is tested
here is the shape of what is asked for, which is the part that would drift
silently.
"""

from __future__ import annotations

import pytest

from app.repositories import devices as device_repo
from app.services import devices as service


class _FakeSession:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []
        self.sql: list[str] = []
        self.params: list[dict] = []

    async def execute(self, statement, params=None):
        self.sql.append(str(statement))
        self.params.append(params or {})
        rows = self.rows

        class _Result:
            def mappings(self):
                return self

            def all(self):
                return rows

            def one(self):
                return rows[0] if rows else {}

            def scalar_one(self):
                return 0
        return _Result()


@pytest.mark.asyncio
async def test_every_row_carries_its_open_alarm_count():
    session = _FakeSession()
    await device_repo.list_devices(session)
    assert "COALESCE(ds.active_alarms, 0)              AS active_alarms" in session.sql[0]


def test_the_summary_reads_the_count_and_defaults_to_none():
    row = {"id": "d1", "name": "x", "device_type": "server", "active_alarms": 3}
    assert service._summary(row).active_alarms == 3
    assert service._summary({"id": "d1", "name": "x", "device_type": "server"}).active_alarms == 0


@pytest.mark.asyncio
async def test_status_sorts_worst_first_when_ascending():
    """An operator sorting on state wants OFFLINE at the top, not the
    alphabet's DEGRADED."""
    session = _FakeSession()
    await device_repo.list_devices(session, order_by="status")
    sql = session.sql[0]
    assert ("CASE device_comm_status(d.id,ds.status::text) WHEN 'OFFLINE' THEN 0 WHEN 'DEGRADED' "
            "THEN 1 WHEN 'UNKNOWN' THEN 2 ELSE 3 END ASC NULLS LAST, d.id::text "
            "LIMIT :limit") in sql, "the rank must survive _order_clause whole"


@pytest.mark.asyncio
async def test_alarms_and_last_seen_are_orderable():
    for key, expr in (("alarms", "COALESCE(ds.active_alarms,0) DESC NULLS LAST, d.id::text"),
                      ("last_seen", "ds.last_seen DESC"),
                      ("severity", "WHEN 'CRITICAL' THEN 0")):
        session = _FakeSession()
        await device_repo.list_devices(session, order_by=key, descending=True)
        assert expr in session.sql[0], key


@pytest.mark.asyncio
async def test_alarming_is_a_filter_with_two_answers():
    on, off = _FakeSession(), _FakeSession()
    await device_repo.list_devices(on, alarming=True)
    await device_repo.list_devices(off, alarming=False)
    assert "COALESCE(ds.active_alarms, 0) > 0" in on.sql[0]
    assert "COALESCE(ds.active_alarms, 0) = 0" in off.sql[0]
    none = _FakeSession()
    await device_repo.list_devices(none)
    assert "active_alarms, 0) >" not in none.sql[0]


@pytest.mark.asyncio
async def test_stale_means_seen_once_and_silent_since():
    """A device never seen is UNKNOWN, not stale: the two are different
    questions and the filter must not fold them together."""
    session = _FakeSession()
    await device_repo.list_devices(session, stale_s=600)
    assert ("ds.last_seen IS NOT NULL AND ds.last_seen < now() - "
            "make_interval(secs => :stale_s)") in session.sql[0]
    assert session.params[0]["stale_s"] == 600


@pytest.mark.asyncio
async def test_the_count_knows_the_new_filters_too():
    session = _FakeSession()
    await device_repo.count_matching(session, alarming=True, stale_s=300)
    assert "active_alarms, 0) > 0" in session.sql[0]
    assert ":stale_s" in session.sql[0]


@pytest.mark.asyncio
async def test_facets_are_counted_under_the_lists_own_filters():
    """716 over a list of eleven rows is the bug this exists to prevent."""
    session = _FakeSession(rows=[{
        "total": 11, "online": 10, "degraded": 0, "offline": 0, "unknown": 1,
        "not_monitored": 0, "alarming": 2, "stale": 0,
        "code": "crah", "display_name": "CRAH", "category": "cooling", "n": 7,
    }, {
        "total": 0, "online": 0, "degraded": 0, "offline": 0, "unknown": 0,
        "not_monitored": 0, "alarming": 0, "stale": 0,
        "code": "cdu", "display_name": "CDU", "category": "cooling", "n": 4,
    }])
    out = await device_repo.facets(session, room_id="r1", category=["cooling"], stale_s=900)
    assert len(session.sql) == 2
    for sql in session.sql:
        assert "rm.id = CAST(:room_id AS uuid)" in sql
        assert "dt.category = ANY(:category)" in sql
    assert "count(*) FILTER (WHERE COALESCE(ds.active_alarms, 0) > 0)" in session.sql[0]
    assert "GROUP BY d.device_type, dt.display_name, dt.category" in session.sql[1]
    assert out["total"] == 11 and out["status"]["unknown"] == 1 and out["alarming"] == 2
    assert out["stale_s"] == 900
    assert out["types"] == [
        {"code": "crah", "display_name": "CRAH", "category": "cooling", "count": 7},
        {"code": "cdu", "display_name": "CDU", "category": "cooling", "count": 4}]
    assert out["categories"] == [{"code": "cooling", "count": 11}]


@pytest.mark.asyncio
async def test_the_service_strips_paging_before_counting_facets(monkeypatch):
    seen = {}

    async def fake(session, **kw):
        seen.update(kw)
        return {}
    monkeypatch.setattr(device_repo, "facets", fake)
    await service.facets(None, room_id="r1", limit=50, offset=100, order_by="name",
                         descending=True, with_total=True)
    assert seen == {"room_id": "r1"}


# ---------------------------------------------------------- not monitored

@pytest.mark.asyncio
async def test_every_status_read_goes_through_device_comm_status():
    """A device with no endpoint is NOT_MONITORED, not UNKNOWN. The list, its
    filter and its counts must all ask the same function, or the band says
    sixteen unknown above a filter that finds none."""
    session = _FakeSession()
    await device_repo.list_devices(session, status=["NOT_MONITORED"])
    sql = session.sql[0]
    assert "device_comm_status(d.id, ds.status::text)  AS status" in sql
    assert "device_comm_status(d.id, ds.status::text) = ANY(:status)" in sql
    assert "COALESCE(ds.status::text, 'UNKNOWN')" not in sql


@pytest.mark.asyncio
async def test_not_monitored_is_counted_apart_from_unknown():
    session = _FakeSession(rows=[{
        "total": 20, "online": 4, "degraded": 0, "offline": 0, "unknown": 0,
        "not_monitored": 16, "alarming": 0, "stale": 0,
        "code": "rpp", "display_name": "RPP", "category": "power", "n": 16,
    }])
    out = await device_repo.facets(session)
    assert ("count(*) FILTER (WHERE device_comm_status(d.id, ds.status::text) "
            "= 'NOT_MONITORED') AS not_monitored") in session.sql[0]
    assert out["status"]["not_monitored"] == 16 and out["status"]["unknown"] == 0


@pytest.mark.asyncio
async def test_a_not_monitored_row_names_its_meter_and_the_meters_state(monkeypatch):
    """The panel has no state of its own; the list says which meter reads it
    and whether that meter is answering. The worst of several wins."""
    rows = [
        {"id": "rpp1", "name": "RPPA-DC1-HA-R1-04", "device_type": "rpp",
         "status": "NOT_MONITORED"},
        {"id": "rpp2", "name": "RPPB-DC1-HA-R1-13", "device_type": "rpp",
         "status": "NOT_MONITORED"},
        {"id": "sw1", "name": "SW1", "device_type": "switch", "status": "ONLINE"},
    ]

    async def fake_list(session, **kw):
        return rows, None

    async def fake_tags(session, ids):
        return {}

    asked = {}

    async def fake_meters(session, ids):
        asked["ids"] = list(ids)
        return {
            "rpp1": [{"meter_id": "m1", "meter_name": "EV21-DC1-HA-R1-04", "status": "ONLINE"}],
            "rpp2": [{"meter_id": "m2", "meter_name": "EV21-DC1-HA-R1-13", "status": "ONLINE"},
                     {"meter_id": "m3", "meter_name": "EV22-DC1-HA-R1-13", "status": "OFFLINE"}],
        }

    from app.repositories import panel_metering
    monkeypatch.setattr(service.repo, "list_devices", fake_list)
    monkeypatch.setattr(service.tag_repo, "tags_for_devices", fake_tags)
    monkeypatch.setattr(panel_metering, "meters_fed_from", fake_meters)
    items, _, _ = await service.list_devices(object())
    by = {d.id: d for d in items}
    assert asked["ids"] == ["rpp1", "rpp2"], "only the devices nothing polls"
    assert (by["rpp1"].metered_by, by["rpp1"].metered_status) == ("EV21-DC1-HA-R1-04", "ONLINE")
    assert by["rpp1"].metered_by_id == "m1"
    assert (by["rpp2"].metered_by, by["rpp2"].metered_status) == ("2 meters", "OFFLINE")
    assert by["sw1"].metered_by is None


def test_the_function_puts_the_endpoint_test_before_the_stored_status():
    """A device whose endpoints were removed keeps its last rollup in
    device_state; that row says nothing about now."""
    import importlib.util
    import pathlib
    path = next(pathlib.Path(__file__).resolve().parents[1].glob(
        "alembic/versions/0120_*.py"))
    spec = importlib.util.spec_from_file_location("m0120", path)
    src = path.read_text(encoding="utf-8")
    body = src[src.index("CREATE OR REPLACE FUNCTION"):]
    assert body.index("NOT EXISTS") < body.index("COALESCE(st, 'UNKNOWN')")
    assert "STABLE" in body and spec is not None
