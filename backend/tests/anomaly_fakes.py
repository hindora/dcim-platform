"""Shared fixtures for the Phase 1 anomaly tests (not a test module).

* `seed_rows()` / `seed_params(id)`: the 0112 migration's rows, read from the
  migration file itself, so a test can never drift from what ships.
* `FakeDB`: an in-memory stand-in for the repository calls the sweep makes,
  so the whole engine (reader, L0, L2, dwell, raise/clear, frames) runs with
  no database. Telemetry is a list of rows; findings and alarms are dicts.
* `FakeRedis`: the hash / string / list subset the sweep and fanout touch.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.analytics import catalogue as cat
from app.analytics import sweep as anomaly_sweep
from app.analytics import window as win_store
from app.analytics.detectors.rules import BoolSeries, DeviceView, Sample

_MIG = (Path(__file__).resolve().parents[1] / "alembic" / "versions"
        / "0112_the_rules_that_need_no_history.py")


def _migration():
    spec = importlib.util.spec_from_file_location("mig0112", _MIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed_rows() -> list[dict[str, Any]]:
    return [dict(r) for r in _migration().ROWS]


def seed_params(detector_id: str) -> dict[str, Any]:
    return dict(next(r for r in seed_rows() if r["id"] == detector_id)["params"])


def seed_catalogue(overrides: list[cat.Override] = (), **changes) -> cat.Catalogue:
    dets = []
    for r in seed_rows():
        row = {**r, "version": 1, "cadence_s": 60}
        if r["id"] in changes:
            row.update(changes[r["id"]])
        dets.append(cat.Detector.from_row(row))
    return cat.Catalogue(dets, overrides)


# --- views for pure rule tests ------------------------------------------------

def series(values: list[float], t_end: float, step: float = 30.0, q: str = "good"
           ) -> list[Sample]:
    n = len(values)
    return [Sample(t_end - (n - 1 - i) * step, float(v), q) for i, v in enumerate(values)]


def flat(value: float, t_end: float, n: int = 40, step: float = 30.0) -> list[Sample]:
    return series([value] * n, t_end, step)


def running(t: float, since: float, value: bool = True, known: bool = True) -> BoolSeries:
    return BoolSeries(value=value, last_ts=t - 10, since=since, since_known=known)


def view(dtype: str = "crah", **kw) -> DeviceView:
    v = DeviceView(device_id=kw.pop("device_id", "dev-1"), name=kw.pop("name", "UNIT-1"),
                   device_type=dtype, room_id=kw.pop("room_id", "room-1"),
                   datacenter_id="dc-1", **kw)
    return v


# --- in-memory Redis ------------------------------------------------------------

class FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.lists: dict[str, list[str]] = {}
        self.published: list[tuple[str, dict]] = []

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ex=None):
        self.kv[key] = value

    async def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key, 0)) + 1)
        return int(self.kv[key])

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, mapping=None, **kw):
        self.hashes.setdefault(key, {}).update(mapping or {})

    async def hdel(self, key, *fields):
        for f in fields:
            self.hashes.get(key, {}).pop(f, None)

    async def expire(self, key, ttl):
        return True

    async def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)

    async def ltrim(self, key, a, b):
        self.lists[key] = self.lists.get(key, [])[a:b + 1]

    async def lrange(self, key, a, b):
        return self.lists.get(key, [])[a:b + 1]

    async def publish(self, channel, body):
        self.published.append((channel, json.loads(body)))

    def pipeline(self):
        return _Pipe(self)

    async def aclose(self):
        return None


class _Pipe:
    def __init__(self, r: FakeRedis) -> None:
        self.r, self.ops = r, []

    def publish(self, channel, body):
        self.ops.append((channel, body))

    async def execute(self):
        for c, b in self.ops:
            await self.r.publish(c, b)
        self.ops = []


# --- in-memory repository --------------------------------------------------------

METRIC_KEYS = ["supply_air_temp", "return_air_temp", "air_setpoint_temp", "fan_speed_pct",
               "valve_position_pct", "equipment_state", "water_supply_temp",
               "water_return_temp", "water_setpoint_temp", "water_flow",
               "compressor_load_pct", "water_pressure", "inlet_temperature",
               "ambient_temperature", "relative_humidity", "dew_point", "pump_speed_pct",
               "fuel_level_pct", "transfer_count", "cpu_utilization", "power_draw"]


class FakeDB:
    """What the sweep reads and writes, without Postgres."""

    def __init__(self, catalogue: cat.Catalogue | None = None) -> None:
        self.catalogue = catalogue or seed_catalogue()
        self.mids = {k: i + 1 for i, k in enumerate(METRIC_KEYS)}
        self.devices: list[dict[str, Any]] = []
        self.num: list[tuple[str, str, str, float, float, str]] = []
        self.bools: list[tuple[str, str, str, float, bool, str]] = []
        self.findings: dict[str, dict[str, Any]] = {}
        self.alarms: dict[str, dict[str, Any]] = {}
        self.raises: list[dict[str, Any]] = []
        self.refreshed: list[list[str]] = []
        self.history: list[dict[str, Any]] = []

    # setup
    def add_device(self, dtype: str, name: str | None = None, **kw) -> str:
        did = kw.pop("device_id", None) or str(uuid.uuid4())
        self.devices.append({"device_id": did, "name": name or f"{dtype}-{did[:4]}",
                             "device_type": dtype, "lifecycle": "in_service",
                             "room_id": kw.pop("room_id", "room-1"),
                             "datacenter_id": "dc-1", "role": "cooling", "online": True,
                             "poll_interval_s": 30, "maintenance": False,
                             "fire_predischarge": False, **kw})
        return did

    def sample(self, dev: str, key: str, ts: float, value: float, inst: str = "",
               q: str = "good") -> None:
        self.num.append((dev, key, inst, ts, float(value), q))

    def state(self, dev: str, key: str, ts: float, value: bool, inst: str) -> None:
        self.bools.append((dev, key, inst, ts, bool(value), "good"))

    def patch(self, monkeypatch) -> None:
        r, a = anomaly_sweep.repo, anomaly_sweep.alarm_repo
        for name in ("load_catalogue", "sweep_devices", "open_findings", "metric_ids",
                     "read_numeric", "read_bools", "open_finding", "touch_findings",
                     "resolve_finding", "link_alarm", "sync_alarm_acks", "findings_by_ids"):
            monkeypatch.setattr(r, name, getattr(self, name))
        for name in ("raise_alarm", "clear_alarms", "record_history",
                     "refresh_device_alarm_state"):
            monkeypatch.setattr(a, name, getattr(self, name))
        win_store.reset_memory()

    # repo
    async def load_catalogue(self, session, now=None):
        return self.catalogue

    async def sweep_devices(self, session, types):
        return [dict(d) for d in self.devices if d["device_type"] in types]

    async def metric_ids(self, session):
        return dict(self.mids)

    async def open_findings(self, session):
        return [{"id": f["id"], "detector_id": f["detector_id"], "device_id": f["device_id"],
                 "metric_id": f["metric_id"], "instance": f["instance"], "state": f["state"],
                 "lane": f["lane"], "kind": f["kind"], "alarm_id": f["alarm_id"],
                 "reopen_count": f["reopen_count"], "first_seen": f["first_seen"],
                 "verdict": f["verdict"], "occurrences": f["occurrences"]}
                for f in self.findings.values() if f["state"] != "resolved"]

    def _read(self, rows, since_by_device, mids, floor, now):
        keys = {v: k for k, v in self.mids.items()}
        out = []
        for dev, key, inst, ts, v, q in rows:
            if dev not in since_by_device or self.mids[key] not in mids:
                continue
            if ts > since_by_device[dev].timestamp() and floor.timestamp() < ts <= \
                    now.timestamp():
                out.append((dev, self.mids[key], inst, ts, v, q))
        _ = keys
        return out

    async def read_numeric(self, session, since_by_device, mids, floor, now):
        return self._read(self.num, since_by_device, mids, floor, now)

    async def read_bools(self, session, since_by_device, mids, floor, now):
        return self._read(self.bools, since_by_device, mids, floor, now)

    async def open_finding(self, session, **kw):
        for f in self.findings.values():
            if (f["detector_id"], f["device_id"], f["metric_id"], f["instance"]) == \
                    (kw["detector_id"], kw["device_id"], kw["metric_id"], kw["instance"]) \
                    and f["state"] != "resolved":
                f["occurrences"] += 1
                return {"id": f["id"], "alarm_id": f["alarm_id"],
                        "reopen_count": f["reopen_count"], "outcome": "existing"}
        for f in sorted(self.findings.values(), key=lambda x: x["resolved_at"] or kw["now"],
                        reverse=True):
            if (f["detector_id"], f["device_id"], f["metric_id"], f["instance"]) == \
                    (kw["detector_id"], kw["device_id"], kw["metric_id"], kw["instance"]) \
                    and f["state"] == "resolved" and \
                    (kw["now"] - f["resolved_at"]).total_seconds() < kw["reopen_window_s"]:
                f.update(state="open", resolved_at=None, resolution=None,
                         reopen_count=f["reopen_count"] + 1)
                return {"id": f["id"], "alarm_id": f["alarm_id"],
                        "reopen_count": f["reopen_count"], "outcome": "reopened"}
        fid = str(uuid.uuid4())
        self.findings[fid] = {
            "id": fid, "state": "open", "verdict": None, "alarm_id": None,
            "reopen_count": 0, "occurrences": 1, "resolved_at": None, "resolution": None,
            "last_seen": kw["now"], **{k: kw[k] for k in (
                "detector_id", "detector_version", "layer", "lane", "kind", "device_id",
                "metric_id", "instance", "first_seen", "summary", "evidence", "observed",
                "expected", "score")}}
        return {"id": fid, "alarm_id": None, "reopen_count": 0, "outcome": "opened"}

    async def touch_findings(self, session, touches, now):
        for t in touches:
            f = self.findings[t["id"]]
            f["last_seen"], f["occurrences"] = now, f["occurrences"] + 1
        return len(touches)

    async def resolve_finding(self, session, finding_id, *, resolution, at, by="system"):
        f = self.findings.get(finding_id)
        if not f or f["state"] == "resolved":
            return None
        f.update(state="resolved", resolution=resolution, resolved_at=at)
        return {"id": f["id"], "alarm_id": f["alarm_id"], "device_id": f["device_id"],
                "detector_id": f["detector_id"], "instance": f["instance"],
                "verdict": f["verdict"]}

    async def link_alarm(self, session, finding_id, alarm_id):
        self.findings[finding_id]["alarm_id"] = alarm_id

    async def sync_alarm_acks(self, session, now):
        out = []
        for f in self.findings.values():
            a = self.alarms.get(f["alarm_id"] or "")
            if a and a["state"] == "ACKNOWLEDGED" and f["state"] == "open" \
                    and f["lane"] == "alert":
                f["state"] = "acknowledged"
                out.append(f["id"])
        return out

    async def findings_by_ids(self, session, ids):
        return [{"id": i, "device_id": self.findings[i]["device_id"],
                 "detector_id": self.findings[i]["detector_id"],
                 "state": self.findings[i]["state"]} for i in ids if i in self.findings]

    # alarm repo
    async def raise_alarm(self, session, **kw):
        self.raises.append(kw)
        key = (kw["device_id"], kw["alarm_type"], kw["instance"])
        for a in self.alarms.values():
            if (a["device_id"], a["alarm_type"], a["instance"]) == key and \
                    a["state"] != "CLEARED":
                return {**a, "change": "touched"}
        aid = str(uuid.uuid4())
        self.alarms[aid] = {"id": aid, "device_id": kw["device_id"], "state": "ACTIVE",
                            "alarm_type": kw["alarm_type"], "instance": kw["instance"],
                            "severity": kw["severity"], "source": kw["source"],
                            "attributes": kw.get("attributes")}
        return {**self.alarms[aid], "change": "created"}

    async def clear_alarms(self, session, *, device_id, alarm_types, instance, at, by="system"):
        out = []
        for a in self.alarms.values():
            if a["device_id"] == device_id and a["alarm_type"] in alarm_types and \
                    a["instance"] == instance and a["state"] != "CLEARED":
                a["state"] = "CLEARED"
                out.append(dict(a))
        return out

    async def record_history(self, session, **kw):
        self.history.append(kw)

    async def refresh_device_alarm_state(self, session, device_ids):
        self.refreshed.append(list(device_ids))


async def sweep_once(db: FakeDB, t: float, redis: Any = None) -> anomaly_sweep.SweepResult:
    return await anomaly_sweep.run_sweep(object(), datetime.fromtimestamp(t, UTC),
                                         redis=redis)
