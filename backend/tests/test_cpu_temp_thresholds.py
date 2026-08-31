"""Where the CPU temperature lines sit, and why.

An operator injected a CPU-load fault and nothing else, and got a temperature
warning as well. The reading was correct - a CPU pinned at 93 % really does run
hot - but 80 C on a Xeon package is a busy server, not a failing one, so the
warning was unactionable by construction.

These pin the bands against the physics they are supposed to describe rather
than against the numbers themselves, so a future edit has to argue with the
model instead of just moving a constant.
"""

from __future__ import annotations

import os

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

DB_URL = os.getenv("DCIM_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(not DB_URL, reason="set DCIM_TEST_DATABASE_URL to run"),
    pytest.mark.asyncio,
]

# The simulator's air-cooled die model, device_state_store.py:
#     38.0 + cpu_usage * 0.45 + max(0, inlet - 22) * 0.9   (+/- 1 C jitter)
# Reproduced here so the thresholds are checked against the temperatures the
# fleet actually produces, not against remembered numbers.
JITTER = 1.0


def die_temp(cpu_pct: float, inlet_c: float = 22.0) -> float:
    return 38.0 + cpu_pct * 0.45 + max(0.0, inlet_c - 22.0) * 0.9


# The direct-to-chip model, from the same place:
#     35.0 + cpu_usage * 0.30 + 34.0 if the CDU has stopped + intake * 0.3
# A cold plate holds the die far cooler than air, which is why the CDU faults
# have to be measured against THIS curve. Checking them against the air-cooled
# one - as the first version of this file did - overstates a liquid-cooled
# server's temperature by ~15 C and invents coverage that never existed.
def liquid_die_temp(cpu_pct: float, cdu_stopped: bool = False,
                    inlet_c: float = 22.0) -> float:
    t = 35.0 + cpu_pct * 0.30 + max(0.0, inlet_c - 22.0) * 0.3
    return t + 34.0 if cdu_stopped else t


@pytest_asyncio.fixture
async def rules():
    engine = create_async_engine(DB_URL, poolclass=None)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        rows = (await s.execute(text("""
            SELECT alarm_type, threshold, clear_threshold
              FROM alarm_rule
             WHERE metric_key = 'cpu_temperature' AND enabled
        """))).mappings().all()
    await engine.dispose()
    return {r["alarm_type"]: r for r in rows}


def test_a_fully_loaded_healthy_server_does_not_warn(rules):
    """The regression, stated as physics.

    100 % CPU on a well-cooled server is ~83 C. If that warns, the alarm means
    "this server is busy", and an alarm that fires on healthy hardware is one
    that gets muted.
    """
    hottest_healthy = die_temp(100.0) + JITTER
    assert hottest_healthy < rules["cpu_temp_high"]["threshold"], (
        f"a fully loaded server reaches {hottest_healthy:.1f} C, at or above "
        f"the warning line of {rules['cpu_temp_high']['threshold']} C")


def test_the_case_that_prompted_this(rules):
    """93 % CPU with a 23.2 C inlet measured 81.01 C on the live fleet."""
    assert die_temp(93.0, 23.2) < rules["cpu_temp_high"]["threshold"]


def test_a_stopped_cdu_on_a_working_server_reaches_critical(rules):
    """The line must not be raised so far that real faults stop landing.

    A stopped CDU adds +34 C to a liquid-cooled die. On a server doing work
    that is 96 C, which is past the critical band and past the point where the
    hardware throttles itself.
    """
    assert liquid_die_temp(90.0, cdu_stopped=True) >         rules["cpu_temp_critical"]["threshold"]


def test_a_stopped_cdu_on_an_idle_server_is_the_cdu_alarm_not_this_one(rules):
    """Deliberately NOT covered here, and it never was.

    An idle liquid-cooled server with its CDU stopped reaches 75 C - below the
    old 80 C warning as well as the new 85 C one, so raising the line costs no
    coverage. 75 C is also not a dangerous die temperature, and the fault is
    not really the server's: a stopped CDU is alarmed on the CDU, by
    Unit_Running, pump state and flow. This asserts the gap on purpose so
    nobody closes it by dragging the server's threshold back down.
    """
    idle = liquid_die_temp(20.0, cdu_stopped=True)
    assert idle < rules["cpu_temp_high"]["threshold"]
    assert idle < 80, (
        "if this ever exceeds 80 the old threshold DID cover it, and raising "
        "the line lost something - re-argue the change")


def test_a_warm_room_on_a_loaded_server_still_reaches_it(rules):
    """The commonest real cause: cooling degrades, intake climbs, dies follow.

    An inlet at 35 C is a room in trouble - well outside ASHRAE A1 allowable -
    and a loaded server in it must warn.
    """
    assert die_temp(90.0, 35.0) > rules["cpu_temp_high"]["threshold"]


def test_the_bands_stay_ordered_and_hysteretic(rules):
    high, crit = rules["cpu_temp_high"], rules["cpu_temp_critical"]
    assert high["threshold"] < crit["threshold"], "the bands crossed over"
    assert high["clear_threshold"] < high["threshold"], (
        "no hysteresis: the warning would chatter on the threshold")
    assert crit["clear_threshold"] < crit["threshold"]
    # Dropping back through the critical's clear point must leave the warning
    # standing rather than clearing everything at once.
    assert crit["clear_threshold"] >= high["threshold"]


def test_critical_is_still_below_the_throttle_point(rules):
    """A CRITICAL that only fires after the hardware has already protected
    itself is an alarm about the past. The model throttles at 90 C."""
    assert rules["cpu_temp_critical"]["threshold"] <= 90
