"""The CRITICAL trap that never reached the console.

Live, 2026-10-27: four ingest ticks failed with UntranslatableCharacterError.
The trap was cpu_temp_critical, an IPMI Platform Event Trap (PEN 3183) whose
one varbind is the 47-byte binary PET event record - what iDRAC, iLO, XCC and
Supermicro BMCs really send. Decoded as a string it carried NULs, and JSONB
refuses \\u0000, so the whole insert failed. Worse, the batch's dedup keys had
been claimed in Redis before the transaction, so the redelivery read as "already
seen" and every trap in it was dropped.
"""

from __future__ import annotations

import json

import pytest

from app.alarms.service import event_row

PET_RECORD = ("\x00" * 16 + "\x08 \x01\x00\x00\x01\x01\x01" + "\x00" * 23)


def _ev(**over) -> dict:
    ev = {"observed_at": None, "event_type": "cpu_temp_critical",
          "message": "CPU & Temperature Critical", "varbinds": {}}
    ev.update(over)
    return ev


def test_a_binary_varbind_is_stored_as_hex():
    row = event_row(_ev(varbinds={"1.3.6.1.4.1.3183.1.1.1": PET_RECORD}))
    stored = json.loads(row["raw"])["1.3.6.1.4.1.3183.1.1.1"]
    assert stored == "0x" + PET_RECORD.encode().hex()
    assert "\\u0000" not in row["raw"], "JSONB refuses \\u0000"


def test_printable_varbinds_are_kept_as_they_came():
    """sysName, a model string, a reading: hex-encoding those would hide them."""
    vb = {"1.3.6.1.2.1.1.5.0": "SRV01-DC1-HA-R2-01", "x": "Line 1\tA\r\n",
          "y": "Température"}
    assert json.loads(event_row(_ev(varbinds=vb))["raw"]) == vb


def test_a_nul_never_reaches_a_text_column():
    row = event_row(_ev(message="CPU\x00 Critical"))
    assert row["message"] == "CPU Critical"


class _FakeRedis:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def set(self, key, _v, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True

    async def delete(self, *keys):
        for k in keys:
            self.keys.discard(k)
        return len(keys)


@pytest.mark.asyncio
async def test_a_failed_insert_releases_its_claims_so_the_redelivery_lands(monkeypatch):
    from app.ingest.worker import IngestWorker

    worker = IngestWorker.__new__(IngestWorker)
    worker.redis = _FakeRedis()
    payload = {"collector_id": "c", "events": [
        {"event_type": "cpu_temp_critical", "dedup_key": "k1", "severity": 5},
        {"event_type": "psu_failure", "dedup_key": "k2", "severity": 4}]}

    async def boom(fresh):
        raise RuntimeError("insert failed")

    monkeypatch.setattr(worker, "_store_events", boom)
    with pytest.raises(RuntimeError):
        await worker._handle_events([payload])
    assert worker.redis.keys == set(), "claims left behind drop the redelivery"

    stored: list = []

    async def ok(fresh):
        stored.extend(fresh)
        return [], []

    monkeypatch.setattr(worker, "_store_events", ok)
    await worker._handle_events([payload])
    assert [e.dedup_key for e in stored] == ["k1", "k2"]
