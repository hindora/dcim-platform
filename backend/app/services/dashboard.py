"""Dashboard and telemetry services."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories import dashboard as repo
from app.repositories import telemetry as tel_repo
from app.schemas import (
    CollectorStatusOut,
    DashboardCounts,
    DashboardSummary,
    HistoryOut,
    Series,
)


async def summary(session: AsyncSession,
                  datacenter_id: str | None = None) -> DashboardSummary:
    counts = await repo.device_counts(session, datacenter_id)
    power = await repo.power_summary(session)
    env = await repo.environment_summary(session)
    cols = await repo.collectors(session)
    ingest = await repo.ingest_health(session)

    return DashboardSummary(
        devices=DashboardCounts(**{k: int(v or 0) for k, v in counts.items()}),
        power={k: _f(v) for k, v in power.items()},
        environment={k: _f(v) for k, v in env.items()},
        collectors=[CollectorStatusOut(**c) for c in cols],
        ingest={
            "newest_sample": ingest.get("newest_sample"),
            "lag_seconds": _f(ingest.get("lag_seconds")),
        },
        as_of=datetime.now(UTC),
    )


def _f(v: Any) -> Any:
    if v is None or isinstance(v, (str, bool, dict, list, datetime)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


async def history(session: AsyncSession, *, device_id: str, metrics: list[str],
                  start: datetime | None, end: datetime | None,
                  interval: str = "auto", agg: str = "avg",
                  instance: str | None = None) -> HistoryOut:
    end = end or datetime.now(UTC)
    start = start or (end - timedelta(hours=6))
    series, table, label, truncated = await tel_repo.history(
        session, device_id=device_id, metrics=metrics, start=start, end=end,
        interval=interval, agg=agg, instance=instance)
    return HistoryOut(
        device_id=device_id, interval=label, source=table,
        series=[Series(**s) for s in series], truncated=truncated,
    )


async def latest(session: AsyncSession, device_id: str) -> list[dict[str, Any]]:
    return await tel_repo.latest_values(session, device_id)
