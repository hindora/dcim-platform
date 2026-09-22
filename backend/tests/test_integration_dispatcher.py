"""Draining the outbox: what happens to a row that fails, and the storm brake.

Only the decisions are exercised here. The claim itself - `FOR UPDATE SKIP
LOCKED`, which is what makes two ingest workers safe - is SQL and is covered
by the migrations gate; what is testable in isolation is everything built on
top of it, and that is where the judgement lives.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta

import pytest

from app.integrations import dispatcher as disp
from app.integrations.jira.client import JiraError

NOW = datetime.now(UTC)

INTEGRATION = {"id": "i1", "name": "Acme Jira", "config": {}}


def row(**over):
    base = {"id": 1, "integration_id": "i1", "kind": "alarm_raised",
            "fingerprint": "abcd1234", "alarm_id": "a1", "attempts": 1,
            "created_at": NOW, "payload": {"id": "a1", "device_id": "d1"}}
    base.update(over)
    return base


@pytest.fixture
def fake(monkeypatch):
    """Stand in for the repository and for `unit_of_work`.

    `unit_of_work` is replaced rather than mocked at the session level because
    the dispatcher opens a SHORT transaction per decision on purpose - the
    HTTP call must never be inside one - and the test wants that structure
    to stay visible rather than being papered over.
    """

    class Fake:
        def __init__(self):
            self.done: list[int] = []
            self.dead: list[tuple[int, str]] = []
            self.retried: list[tuple[int, datetime, str]] = []
            self.decremented: list[int] = []
            self.open_alarms: set[str] = {"a1"}

        async def mark_done(self, session, row_id):
            self.done.append(row_id)

        async def mark_dead(self, session, row_id, *, error):
            self.dead.append((row_id, error))

        async def mark_retry(self, session, row_id, *, not_before, error):
            self.retried.append((row_id, not_before, error))

        async def open_alarm_ids(self, session, ids):
            return {i for i in ids if i in self.open_alarms}

    f = Fake()
    monkeypatch.setattr(disp, "repo", f)

    @contextlib.asynccontextmanager
    async def uow():
        yield _Session(f)

    monkeypatch.setattr(disp, "unit_of_work", uow)
    return f


class _Session:
    def __init__(self, fake):
        self.fake = fake

    async def execute(self, statement, params=None):
        # The only raw statement the dispatcher runs is the held-row
        # attempt decrement.
        self.fake.decremented.append((params or {}).get("id"))
        return None


def dispatcher():
    return disp.Dispatcher("test-worker")


# ---------------------------------------------------------------- retries

async def test_a_429_is_rescheduled_for_when_atlassian_said(fake):
    d = dispatcher()
    exc = JiraError("rate limited", status=429, retry_after=90.0)
    await d._schedule_retry(INTEGRATION, row(attempts=1), exc, {"max_attempts": 8})
    row_id, when, _ = fake.retried[0]
    assert row_id == 1
    assert 85 <= (when - datetime.now(UTC)).total_seconds() <= 95
    assert not fake.dead


async def test_a_400_goes_dead_on_the_first_attempt(fake):
    """Eight identical attempts spend eight requests of a tenant's hourly
    quota to learn the same thing, then bury the one useful error under seven
    copies of itself."""
    d = dispatcher()
    exc = JiraError("bad request", status=400,
                    body="customfield_10101: not on the appropriate screen")
    await d._schedule_retry(INTEGRATION, row(attempts=1), exc, {"max_attempts": 8})
    assert not fake.retried
    assert fake.dead[0][0] == 1
    # The field error has to survive: it is the whole repair instruction.
    assert "customfield_10101" in fake.dead[0][1]


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_other_statements_about_the_request_also_go_dead(fake, status):
    d = dispatcher()
    await d._schedule_retry(INTEGRATION, row(), JiraError("no", status=status),
                            {"max_attempts": 8})
    assert fake.dead and not fake.retried


async def test_a_row_that_has_used_its_attempts_gives_up(fake):
    d = dispatcher()
    exc = JiraError("still down", status=503)
    await d._schedule_retry(INTEGRATION, row(attempts=8), exc,
                            {"max_attempts": 8})
    assert not fake.retried
    assert "gave up after 8 attempts" in fake.dead[0][1]


async def test_a_transport_failure_with_no_status_is_retried(fake):
    d = dispatcher()
    await d._schedule_retry(INTEGRATION, row(attempts=2),
                            JiraError("connect timeout", status=None),
                            {"max_attempts": 8})
    assert fake.retried and not fake.dead


async def test_a_held_row_is_not_scheduled_twice(fake):
    """`_hold` already set its own `not_before`; the generic path must not
    overwrite it with a backoff delay."""
    d = dispatcher()
    await d._schedule_retry(INTEGRATION, row(), disp._HeldError("abcd1234"),
                            {"max_attempts": 8})
    assert not fake.retried and not fake.dead


# ------------------------------------------------------------ storm brake

def cfg(**over):
    base = {"storm_threshold": 3, "storm_window_s": 300}
    base.update(over)
    return base


def test_the_brake_trips_only_after_the_threshold():
    d = dispatcher()
    for _ in range(2):
        assert not d._storm_tripped(INTEGRATION, cfg())
        d._note_create("i1")
    assert not d._storm_tripped(INTEGRATION, cfg())
    d._note_create("i1")
    assert d._storm_tripped(INTEGRATION, cfg())


def test_the_window_slides_rather_than_resetting(monkeypatch):
    """A counter that resets on a boundary lets twice the threshold through
    across it."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(disp.time, "monotonic", lambda: clock["t"])
    d = dispatcher()
    for _ in range(3):
        d._note_create("i1")
    assert d._storm_tripped(INTEGRATION, cfg())
    clock["t"] += 301
    assert not d._storm_tripped(INTEGRATION, cfg())


def test_the_brake_can_be_switched_off():
    d = dispatcher()
    for _ in range(50):
        d._note_create("i1")
    assert not d._storm_tripped(INTEGRATION, cfg(storm_threshold=0))


def test_each_integration_brakes_independently():
    d = dispatcher()
    for _ in range(3):
        d._note_create("i1")
    assert d._storm_tripped(INTEGRATION, cfg())
    assert not d._storm_tripped({**INTEGRATION, "id": "i2"}, cfg())


async def test_holding_a_row_does_not_count_it_as_an_attempt(fake):
    """Nothing failed. Without the decrement a long storm walks the very
    tickets it was protecting the desk from into the dead state."""
    d = dispatcher()
    await d._hold(INTEGRATION, row(attempts=1), cfg())
    assert fake.decremented == [1]
    assert fake.retried and "held" in fake.retried[0][2]


# ----------------------------------------------------- the delayed create

async def test_a_fresh_row_is_acted_on_as_recorded(fake):
    """A condition that raises and clears within one pass is a flap, and the
    alarm engine's own dwell is where that belongs - not here."""
    d = dispatcher()
    fake.open_alarms = set()                      # the alarm has since cleared
    assert await d._should_still_create(row(created_at=NOW)) is True


async def test_a_held_row_whose_alarm_cleared_is_dropped(fake):
    """This is what makes the brake a brake rather than a delay line."""
    d = dispatcher()
    fake.open_alarms = set()
    stale = row(created_at=NOW - timedelta(seconds=disp.RECHECK_AFTER_S + 60))
    assert await d._should_still_create(stale) is False


async def test_a_held_row_whose_alarm_is_still_open_still_creates(fake):
    d = dispatcher()
    fake.open_alarms = {"a1"}
    stale = row(created_at=NOW - timedelta(seconds=disp.RECHECK_AFTER_S + 60))
    assert await d._should_still_create(stale) is True


async def test_a_row_with_no_alarm_id_is_never_dropped(fake):
    """A platform alarm, or a row whose alarm was purged. Nothing to check,
    and dropping it would lose the only record that something happened."""
    d = dispatcher()
    stale = row(alarm_id=None,
                created_at=NOW - timedelta(seconds=disp.RECHECK_AFTER_S + 60))
    assert await d._should_still_create(stale) is True


# ------------------------------------------------------- operations alerts

class _AlertTarget:
    """Shaped like `OpsAlertTarget`, recording what it was asked to do."""

    def __init__(self):
        self.calls: list[str] = []

    async def handle(self, *, kind, alarm, print_, link=None, attempts=1,
                     now=None):
        self.calls.append(kind)
        from app.integrations.ops.target import AlertUpdate
        return AlertUpdate(print_, "created" if kind != "alarm_cleared"
                           else "closed")


OPS = {"id": "i1", "name": "Pager", "kind": "jsm_ops", "config": {}}


async def test_an_alert_is_delivered_without_touching_the_link_table(fake):
    """The alias IS the key and the API owns it. A link row would be a second
    record of something this platform does not need to remember."""
    d = dispatcher()
    target = _AlertTarget()
    await d._deliver_alert(target, OPS, row(), "abcd1234")
    assert target.calls == ["alarm_raised"]
    assert fake.done == [1]


async def test_a_cleared_alert_is_never_held_by_the_storm_brake(fake):
    """The brake exists to stop people being woken up. Refusing to close an
    alert would leave one ringing about a fault that is over."""
    d = dispatcher()
    for _ in range(3):
        d._note_create("i1")
    integration = {**OPS, "config": {"storm_threshold": 3,
                                     "storm_window_s": 300}}
    target = _AlertTarget()
    await d._deliver_alert(target, integration,
                           row(kind="alarm_cleared"), "abcd1234")
    assert target.calls == ["alarm_cleared"]


async def test_a_new_alert_is_held_when_the_brake_is_engaged(fake):
    """It matters more here than on the issue path: that one fills a queue,
    this one wakes people up."""
    d = dispatcher()
    for _ in range(3):
        d._note_create("i1")
    integration = {**OPS, "config": {"storm_threshold": 3,
                                     "storm_window_s": 300}}
    with pytest.raises(disp._HeldError):
        await d._deliver_alert(_AlertTarget(), integration, row(), "abcd1234")


async def test_a_change_request_is_skipped_by_a_paging_integration(fake):
    """An Operations alert cannot hold a change request, and queueing one
    against a paging tier would wake somebody up about paperwork."""
    d = dispatcher()
    await d._deliver_window(_AlertTarget(), OPS,
                            row(kind="window_requested",
                                payload={"window": {"id": "w1"}}))
    assert fake.done == [1]
