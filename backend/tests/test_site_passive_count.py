"""Passive equipment is counted apart from the monitored population.

A remote power panel is a panelboard: its circuits are metered by the meter
clamped onto it, so it has no endpoint and never reports a state. Counting it
among devices that are "not online" made a healthy site look short. The world
map now says "364 online of 364 monitored, 8 passive panels".
"""
from __future__ import annotations

import asyncio
from typing import Any

from app.repositories import sites as repo
from app.services import sites as service


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self):
        self.sql: list[str] = []

    async def execute(self, stmt, *_a, **_k):
        self.sql.append(str(stmt))
        return _Rows([])


def test_passive_means_no_endpoint_to_poll():
    s = _Session()
    asyncio.run(repo.site_rollups(s))
    sql = s.sql[0]
    assert "AS passive_count" in sql
    assert "NOT EXISTS" in sql and "device_endpoint" in sql
    # Offline stays an explicit state, so a panel is never also "offline".
    assert "ds.status = 'OFFLINE'" in sql


def _site(**over):
    row = {"id": "dc1", "code": "DC1", "name": "DC1", "city": None, "country": None,
           "timezone": None, "room_count": 3, "device_count": 372, "online_count": 364,
           "offline_count": 0, "passive_count": 8}
    row.update(over)
    return row


def _overview(monkeypatch, site):
    async def sites(_s):
        return [site]

    async def empty(_s, *a, **k):
        return []

    async def totals(_s):
        return {}

    async def health(_s):
        return {}

    monkeypatch.setattr(repo, "site_rollups", sites)
    monkeypatch.setattr(repo, "room_rollups", empty)
    monkeypatch.setattr(repo, "fleet_alert_totals", totals)
    monkeypatch.setattr(service, "platform_health", health)
    monkeypatch.setattr(service, "_alarms", lambda _r: {})
    return asyncio.run(service.overview(None))["sites"][0]


def test_the_overview_carries_the_passive_count(monkeypatch):
    row = _overview(monkeypatch, _site())
    assert row["passive_count"] == 8
    assert row["device_count"] - row["passive_count"] == row["online_count"]


def test_a_row_without_the_column_reads_zero(monkeypatch):
    site = _site()
    del site["passive_count"]
    assert _overview(monkeypatch, site)["passive_count"] == 0
