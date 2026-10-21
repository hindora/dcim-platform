"""The anomaly steps in the ingest worker (docs/33 task 0.12).

No database: the transaction, the sweep and the fit are stubbed. What is
checked is the wiring - off means no reads or writes at all, the sweep runs
on DCIM_ANOMALY_SWEEP_S, and both steps sit inside the class, in the tick
after the snapshot, above the module's entry point.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import pytest

from app.analytics import sweep as anomaly_sweep
from app.ingest import worker as worker_mod

SRC = (Path(__file__).resolve().parents[1] / "app" / "ingest" / "worker.py").read_text(
    encoding="utf-8")


class _Redis:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def call(*a, **kw):
            self.calls.append(name)
            return None
        return call


@pytest.fixture
def worker(monkeypatch):
    w = worker_mod.IngestWorker(consumer_name="test-1")
    w.redis = _Redis()
    opened: list[str] = []

    @contextlib.asynccontextmanager
    async def uow():
        opened.append("uow")
        yield object()

    class _Conn:
        async def __aenter__(self):
            opened.append("conn")
            return object()

        async def __aexit__(self, *a):
            return False

    class _Engine:
        def connect(self):
            return _Conn()

    sweeps: list[dict] = []
    fits: list[dict] = []

    async def guarded_sweep(session, redis, **kw):
        sweeps.append(kw)
        return anomaly_sweep.SweepResult(ran=True, duration_ms=3)

    async def guarded_fit(conn, redis, **kw):
        fits.append(kw)
        return anomaly_sweep.FitResult(ran=False, skipped="already_fitted")

    monkeypatch.setattr(worker_mod, "unit_of_work", uow)
    monkeypatch.setattr(worker_mod, "get_engine", lambda: _Engine())
    monkeypatch.setattr(worker_mod.anomaly_sweep, "guarded_sweep", guarded_sweep)
    monkeypatch.setattr(worker_mod.anomaly_sweep, "guarded_fit", guarded_fit)
    w.opened, w.sweeps, w.fits = opened, sweeps, fits
    return w


def _enable(w, **kw):
    w.settings = w.settings.model_copy(update={"anomaly_enabled": True, **kw})


def test_the_flag_defaults_off():
    assert worker_mod.get_settings().anomaly_enabled is False


async def test_disabled_does_nothing_at_all(worker):
    assert worker.settings.anomaly_enabled is False
    await worker._maybe_detect_anomalies()
    await worker._maybe_fit_baselines()
    assert worker.opened == [] and worker.sweeps == [] and worker.fits == []
    assert worker.redis.calls == []


async def test_the_sweep_runs_on_its_interval(worker, monkeypatch):
    _enable(worker, anomaly_sweep_s=45)
    clock = [1000.0]
    monkeypatch.setattr(worker_mod.time, "monotonic", lambda: clock[0])

    await worker._maybe_detect_anomalies()
    assert len(worker.sweeps) == 1
    assert worker.sweeps[0]["sweep_s"] == 45
    assert worker.sweeps[0]["consumer"] == "test-1"

    clock[0] += 44.0
    await worker._maybe_detect_anomalies()
    assert len(worker.sweeps) == 1

    clock[0] += 1.0
    await worker._maybe_detect_anomalies()
    assert len(worker.sweeps) == 2


async def test_a_failed_sweep_does_not_escape(worker, monkeypatch):
    _enable(worker)

    async def boom(session, redis, **kw):
        raise RuntimeError("db gone")

    monkeypatch.setattr(worker_mod.anomaly_sweep, "guarded_sweep", boom)
    await worker._maybe_detect_anomalies()  # must not raise
    assert "lpush" in worker.redis.calls  # the error is recorded for /health


async def test_the_fit_uses_a_dedicated_connection_and_the_configured_hour(worker):
    _enable(worker, anomaly_baseline_hour_utc=4)
    await worker._maybe_fit_baselines()
    assert worker.opened == ["conn"]
    assert worker.fits[0]["hour_utc"] == 4
    await worker._maybe_fit_baselines()  # inside ANOMALY_BASELINE_CHECK_S
    assert len(worker.fits) == 1


def test_both_steps_sit_above_the_entry_point_and_run_after_the_snapshot():
    main_at = SRC.index('if __name__ == "__main__"')
    amain_at = SRC.index("async def _amain(")
    for name in ("async def _maybe_detect_anomalies(", "async def _maybe_fit_baselines("):
        assert SRC.index(name) < amain_at < main_at
    tick = SRC[SRC.index("    async def _tick(self)"):SRC.index("    async def _dispatch(")]
    snap = tick.index("await self._maybe_snapshot()")
    assert snap < tick.index("await self._maybe_detect_anomalies()") \
        < tick.index("await self._maybe_fit_baselines()")
    assert callable(worker_mod.IngestWorker._maybe_detect_anomalies)


def test_the_lock_keys_are_the_documented_ones():
    assert int.from_bytes(b"DCIMA", "big") == anomaly_sweep.ANOMALY_SWEEP_LOCK_KEY
    assert int.from_bytes(b"DCIMB", "big") == anomaly_sweep.ANOMALY_BASELINE_LOCK_KEY
