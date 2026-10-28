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
    assert ("CASE COALESCE(ds.status::text,'UNKNOWN') WHEN 'OFFLINE' THEN 0 WHEN 'DEGRADED' "
            "THEN 1 WHEN 'UNKNOWN' THEN 2 ELSE 3 END ASC NULLS LAST, d.id::text LIMIT :limit") in sql, "the rank must survive _order_clause whole"


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
        "alarming": 2, "stale": 0,
        "code": "crah", "display_name": "CRAH", "category": "cooling", "n": 7,
    }, {
        "total": 0, "online": 0, "degraded": 0, "offline": 0, "unknown": 0,
        "alarming": 0, "stale": 0,
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
