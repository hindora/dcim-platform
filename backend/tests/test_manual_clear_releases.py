"""Clearing a root by hand has to let go of what it was explaining.

Suppression is only ever a display decision, and it is safe because the root
is the row an operator acts on: when the root goes, the symptoms come back and
stand on their own.

Every automatic clear path did that. The one an operator actually presses did
not. Found on a live board - a temperature critical cleared by hand at 09:20
left the warning under it flagged as explained by an alarm that no longer
existed: invisible on the console, still reading 93 C, and unclearable,
because the thing that would have released it was already gone.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.requests import Request

from app.alarms import correlation
from app.api.v1 import alarms as route
from app.core.security import Principal
from app.repositories import alarms as repo

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]


@pytest_asyncio.fixture
async def session():
    """A session bound to an outer transaction that is always rolled back.

    The route under test calls session.commit(), which would end a plain
    test transaction and leave the work on a live database.
    join_transaction_mode="create_savepoint" turns that commit into a
    savepoint release inside the connection's transaction, so the endpoint
    runs exactly as it does in production and nothing survives the test.
    """
    engine = create_async_engine(DB_URL, poolclass=None)
    async with engine.connect() as conn:
        trans = await conn.begin()
        maker = async_sessionmaker(bind=conn, expire_on_commit=False,
                                   join_transaction_mode="create_savepoint")
        async with maker() as s:
            try:
                yield s
            finally:
                await trans.rollback()
    await engine.dispose()


async def _device(session, name):
    return await session.scalar(text("""
        INSERT INTO device (name, device_type, lifecycle)
        VALUES (:n, 'switch', 'in_service') RETURNING id::text
    """), {"n": name})


async def _alarm(session, device_id, alarm_type, severity):
    return await session.scalar(text("""
        INSERT INTO alarm (device_id, alarm_type, instance, severity, message,
                           source, state, first_seen, last_seen)
        VALUES (CAST(:d AS uuid), :t, 'CPU', CAST(:s AS severity_t), 'hot',
                'threshold', 'ACTIVE', now(), now())
        RETURNING id::text
    """), {"d": device_id, "t": alarm_type, "s": severity})


async def _state_of(session, alarm_id):
    return (await session.execute(text("""
        SELECT state, is_symptom, root_cause_alarm_id::text AS root
          FROM alarm WHERE id = CAST(:id AS uuid)
    """), {"id": alarm_id})).mappings().one()


def _request() -> Request:
    """The bare scope audit.client_of() reads."""
    return Request({"type": "http", "method": "POST", "path": "/",
                    "headers": [(b"user-agent", b"pytest")],
                    "client": ("127.0.0.1", 0), "query_string": b"",
                    "scheme": "http", "server": ("test", 80)})


async def test_a_hand_cleared_root_releases_its_symptoms(session):
    """The exit criterion: no symptom outlives the alarm that explained it.

    Driven through the route, not through release_symptoms - the function
    always worked, and the bug was the operator's own endpoint never calling
    it. A test that reached past the endpoint would have passed throughout.
    """
    dev = await _device(session, "OOBC-TEST")
    root = await _alarm(session, dev, "cpu_temp_critical", "CRITICAL")
    symptom = await _alarm(session, dev, "cpu_temp_high", "WARNING")
    await correlation.mark_symptom(session, alarm_id=symptom, root_alarm_id=root)

    assert (await _state_of(session, symptom))["is_symptom"] is True

    result = await route.clear(
        alarm_id=root, request=_request(), session=session,
        principal=Principal(username="admin", role="admin"))
    assert result["ok"] is True

    after = await _state_of(session, symptom)
    assert after["is_symptom"] is False, (
        "the symptom is still hidden behind an alarm that no longer exists")
    assert after["root"] is None
    assert after["state"] != "CLEARED", (
        "releasing a symptom must not clear it - the condition is still true")
    assert (await _state_of(session, root))["state"] == "CLEARED"


async def test_manual_clear_reports_the_instance_the_release_needs(session):
    """manual_clear has to hand back enough to find the link it was on.

    Without the instance there is no way to tell WHICH port recovered, so the
    connection's oper_state cannot be refreshed on a hand clear.
    """
    dev = await _device(session, "LF-TEST")
    alarm = await _alarm(session, dev, "link_down", "MAJOR")
    row = await repo.manual_clear(session, alarm, "admin")
    assert row["instance"] == "CPU"  # whatever was set; the field must be there
