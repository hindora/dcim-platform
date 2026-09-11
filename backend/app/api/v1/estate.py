"""Estate-wide pages: thermal, power, utilisation, and the alert drill-downs.

One request renders one page. Each response carries BOTH the room rows and the
site rows folded from them, because the page is a single table with a scope
toggle - fetching again to switch from sites to rooms would show the reader two
different instants and call it the same screen.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.alert_taxonomy import (
    CATEGORIES,
    DESCRIPTIONS,
    DETECTION_DESCRIPTIONS,
    DETECTIONS,
    RESPONSE_CLASSES,
    RESPONSE_DESCRIPTIONS,
    STRIP_GROUPS,
    examples_for,
)
from app.core.security import Principal, current_principal
from app.db.session import get_session
from app.services import estate as service
from app.services import taxonomy

router = APIRouter(prefix="/estate", tags=["estate"])

#: The severities a facet can name. The column is an enum; anything else
#: would be a cast error deep in the query rather than a bad request.
SEVERITIES = ("CRITICAL", "MAJOR", "MINOR", "WARNING", "INFO")


@router.get("/thermal", summary="Intake temperature and compliance by room and site")
async def thermal(
    focus: date | None = Query(None, description="Day to report, UTC. Defaults to yesterday."),
    compare: date | None = Query(
        None, description="Day to compare against. Defaults to the day before focus."),
    mode: str = Query("daily", pattern="^(daily|live|now)$"),
    source: str = Query("auto", pattern="^(auto|probes|servers|network)$",
                        description="Pin every rack to one intake source"),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    """Rack intake air, averaged by SAMPLE across the estate.

    `now` reports the newest reading from each sensor and nothing older than
    ten minutes, counting one reading per sensor rather than every reading over
    a window; `live` reports the last hour against the hour before it; `daily`
    reports a calendar day in UTC against another. The window is echoed back in
    the response - a temperature without the window it was measured over is not
    a fact anyone can act on, and `now` and `live` answer different questions:
    what is happening, and what has been happening.
    """
    return await service.thermal(session, focus=focus, compare=compare,
                                 mode=mode, source=source)


@router.get("/power", summary="Power split IT / cooling / other, with PUE per row")
async def power(
    start: datetime | None = Query(None),
    end: datetime | None = Query(None),
    mode: str = Query("average", pattern="^(average|peak)$"),
    live: bool = Query(False, description="Instantaneous draw instead of a window"),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    if start and end and start >= end:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "start must precede end")
    if start and start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end and end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return await service.power(session, start=start, end=end, mode=mode, live=live)


@router.get("/utilization", summary="Space, power and cooling used against installed")
async def utilization(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    """Utilisation is always "now": it is a state of the estate, not a window.

    Every percentage carries the basis of its denominator, because "62% power"
    means something different against a design rating than against the summed
    nameplate of whatever PDUs happen to be installed.
    """
    return await service.utilisation(session)


@router.get("/alarm-categories", summary="The taxonomy itself: categories, "
                                        "owners, detection methods")
async def alarm_categories(
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    """What each counter means, served from the classifier that fills it.

    The UI legend is generated from this rather than written beside it, so the
    definition an operator reads and the rule the classifier applies cannot
    drift - and a category added to the taxonomy appears in the legend without
    a frontend change.
    """
    return {
        "categories": [{
            "key": c,
            "label": DESCRIPTIONS[c]["label"],
            "owner": DESCRIPTIONS[c]["owner"],
            "description": DESCRIPTIONS[c]["text"],
            # Real entries out of the classifier, not illustrations of it.
            "examples": examples_for(c),
        } for c in CATEGORIES],
        # How the eight group into the five headline counters on the home
        # strip. The table keeps one column per category, so the grouping is a
        # presentation of the same numbers and hides nothing.
        "strip_groups": [{
            "key": key, "label": label, "categories": list(members),
        } for key, label, members in STRIP_GROUPS],
        "detections": [{
            "key": d,
            "label": DETECTION_DESCRIPTIONS[d]["label"],
            "description": DETECTION_DESCRIPTIONS[d]["text"],
        } for d in DETECTIONS],
        # Alarm or alert - required response, the ISA-18.2 split. Served so
        # the legend can say what this console shows and what it leaves out:
        # every count on the home page is `alarm`, and the informational half
        # is reachable through /alarms?response_class=alert.
        "response_classes": [{
            "key": c,
            "label": RESPONSE_DESCRIPTIONS[c]["label"],
            "description": RESPONSE_DESCRIPTIONS[c]["text"],
        } for c in RESPONSE_CLASSES],
        # And the catalogue itself: every condition this platform can raise,
        # which is the difference between a legend that defines the buckets and
        # one an operator can look something up in.
        **await taxonomy.catalogue(session),
    }


@router.get("/alarms", summary="What is open in one or more categories, by room")
async def alarms(
    category: list[str] = Query(..., description=(
        "One or more of: " + ", ".join(CATEGORIES)
        + ". Repeat the parameter for a grouped counter - Cooling is cooling "
          "and environmental - so one room comes back as one row.")),
    lifecycle: str = Query("open", pattern="^(open|all)$", description=(
        "`open` (default) is what the counter counts; `all` adds the cleared "
        "conditions back, for the history behind the same rooms.")),
    severity: list[str] | None = Query(None, description=(
        "Only these severities (CRITICAL, MAJOR, MINOR, WARNING, INFO); "
        "several combine as OR. The pressed chips on the panel.")),
    detection: list[str] | None = Query(None, description=(
        "Only these detection methods: " + ", ".join(DETECTIONS)
        + ". Several combine as OR; with `severity`, AND.")),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    """The rooms behind a counter.

    The same population the counter totals, which is the point: a drill-down
    that returns more rows, or fewer, than the number that opened it teaches an
    operator to distrust both.
    """
    unknown = sorted(set(category) - set(CATEGORIES))
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"unknown category: {', '.join(unknown)}")
    severities = [s.upper() for s in (severity or [])]
    bad = sorted(set(severities) - set(SEVERITIES))
    if bad:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"unknown severity: {', '.join(bad)}")
    bad = sorted(set(detection or []) - set(DETECTIONS))
    if bad:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"unknown detection: {', '.join(bad)}")
    return await service.alarms(session, categories=list(dict.fromkeys(category)),
                                lifecycle=lifecycle,
                                severities=severities or None,
                                detections=detection or None)


@router.get("/alarm-trend", summary="Conditions raised per day in a scope")
async def alarm_trend(
    category: list[str] = Query(..., description=(
        "One or more of: " + ", ".join(CATEGORIES) + ", as for /alarms.")),
    days: int = Query(30, ge=1, le=366),
    bucket: str = Query("day", pattern="^(day|week)$", description=(
        "Bar width. `week` is Monday-anchored and widens the window back to "
        "a Monday, so the first bar is a whole week.")),
    since: date | None = Query(None, description=(
        "First day of a picked window (inclusive). With `until`, replaces "
        "`days`; at most 366 days apart.")),
    until: date | None = Query(None, description=(
        "Last day of a picked window (inclusive).")),
    room: str | None = Query(None, description="Only this room."),
    site: str | None = Query(None, description="Only this site (datacenter id)."),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    """Whether now is normal: what this scope raised per day, zeros included.

    Counted in the database. The alarm list is capped and ordered by
    severity, so a trend bucketed from it in the browser was a chart of the
    most severe 500 rather than the most recent.
    """
    unknown = sorted(set(category) - set(CATEGORIES))
    if unknown:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"unknown category: {', '.join(unknown)}")
    # The ids are cast to uuid inside the query; a malformed one would come
    # back from the database as a 500, which reads as the trend being broken
    # rather than the request.
    for name, value in (("room", room), ("site", site)):
        if value is None:
            continue
        try:
            UUID(value)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"{name} is not a uuid: {value}") from None
    try:
        return await service.alarm_trend(
            session, categories=list(dict.fromkeys(category)), days=days,
            bucket=bucket, since=since, until=until,
            room_id=room, datacenter_id=site)
    except ValueError as e:
        # A window that cannot be answered is the request's fault, not the
        # chart's: said as such rather than surfacing as a 500.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None


@router.get("/thermal-trend", summary="Intake average, p90 and max per hour or day in a scope")
async def thermal_trend(
    days: int = Query(7, ge=1, le=366),
    bucket: str = Query("hour", pattern="^(hour|day)$", description=(
        "Point width. Hourly points come from the five-minute rollup, daily "
        "points from the hourly one.")),
    since: date | None = Query(None, description=(
        "First day of a picked window (inclusive, UTC). With `until`, "
        "replaces `days`; at most 366 days apart.")),
    until: date | None = Query(None, description="Last day of a picked window (inclusive)."),
    room: str | None = Query(None, description="Only this room."),
    site: str | None = Query(None, description="Only this site (datacenter id)."),
    rack: str | None = Query(None, description="Only this rack."),
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    """The thermal table's readings over time: what a scope's intake ran at
    per bucket, against the ASHRAE band. Every bucket in the window is
    present; one with nothing measured carries nulls."""
    for name, value in (("room", room), ("site", site), ("rack", rack)):
        if value is None:
            continue
        try:
            UUID(value)
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                f"{name} is not a uuid: {value}") from None
    try:
        return await service.thermal_trend(
            session, days=days, bucket=bucket, since=since, until=until,
            room_id=room, datacenter_id=site, rack_id=rack)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None


@router.get("/rooms/{room_id}/kpi", summary="Everything the room drawer shows")
async def room_kpi(
    room_id: str,
    session: AsyncSession = Depends(get_session),
    _: Principal = Depends(current_principal),
) -> dict:
    result = await service.room_kpi(session, room_id)
    if result is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such room")
    return result
