"""The roll-up and the API on the eight categories (phases 3 and 4).

The classifier was proved in phase 1. What is unproven here is the plumbing
between it and the screen, and every check below stands for a way that plumbing
has broken before:

* A counter and its drill-down disagreeing, because one was derived and the
  other read.
* A category added to the taxonomy that the SQL never learned to count, so it
  reads zero instead of failing.
* A facet that describes a different instant of the estate than the rows it
  sits above.
* A legend maintained beside the classifier rather than generated from it.

The five-bucket vocabulary these replaced was served alongside them through
phase 3 and removed with the phase 4 UI; `test_the_old_vocabulary_is_gone`
is what stops it creeping back in as an alias.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.v1 import estate as estate_api
from app.core.alert_taxonomy import CATEGORIES, DETECTIONS
from app.repositories import sites as sites_repo
from app.services import estate as estate_service
from app.services import sites as sites_service


class _FakeSession:
    """The services never touch it; the repositories are stubbed."""


async def _no_catalogue(_session):
    """The catalogue has its own tests; these two are about the definitions."""
    return {}



def _returns(value):
    async def _fn(*_args, **_kwargs):
        return value
    return _fn


# ------------------------------------------------------------ generated SQL


def test_every_category_and_detection_is_counted():
    """The counting columns are generated from the tuples, not transcribed.

    Transcribed lists rot: a category added to the taxonomy and forgotten in
    the SQL reads as zero open alarms, which is indistinguishable from good
    news.
    """
    for c in CATEGORIES:
        assert f"AS alerts_{c}" in sites_repo._CATEGORY_COLUMNS
    for d in DETECTIONS:
        assert f"AS detected_{d}" in sites_repo._DETECTION_COLUMNS


def test_every_counted_column_is_coalesced():
    """A site with no open alarm has no `agg` row at all.

    Without the COALESCE the row comes back NULL rather than zero, and the
    healthiest site on the estate renders as blank cells.
    """
    for c in CATEGORIES:
        assert f"COALESCE(agg.alerts_{c}, 0) AS alerts_{c}" in sites_repo._AGG_COALESCE
    for d in DETECTIONS:
        assert f"COALESCE(agg.detected_{d}, 0) AS detected_{d}" in sites_repo._AGG_COALESCE


def test_the_rollup_reads_the_stamped_category_and_does_not_derive_it():
    """Phase 1 stamps the category at raise time; the roll-up must read it.

    Deriving it here would join every alarm through device to device_type on
    every count, and would rewrite history the moment a device is re-typed - a
    PDU reclassified today would move alarms raised last month.
    """
    assert "a.category       AS category" in sites_repo._ALARM_CTE
    assert "a.detection      AS detection" in sites_repo._ALARM_CTE


# ---------------------------------------------------------- the alert block


def _row(**counts) -> dict:
    row = {"alerts_total": 0, "crit": 0, "major": 0, "minor": 0}
    row.update(counts)
    return row


def test_the_alert_block_is_the_eight_categories_and_the_detections():
    """Both axes, on every row and on the strip totals.

    The table renders a column per category and the drill-down facets read the
    detections, so an absent key is a crash rather than an empty cell - hence
    the whole set, not only what happens to be non-zero.
    """
    block = sites_service._alarms(_row(
        alerts_total=7, crit=2,
        alerts_power=3, alerts_cooling=1, alerts_visibility=3,
        detected_threshold=4, detected_state=2, detected_absence=1,
    ))

    assert block["by_category"]["power"] == 3
    assert block["by_category"]["cooling"] == 1
    assert block["by_detection"]["state"] == 2
    assert block["by_category"]["capacity"] == 0
    assert set(block["by_category"]) == set(CATEGORIES)
    assert set(block["by_detection"]) == set(DETECTIONS)


def test_the_old_vocabulary_is_gone():
    """`thermal` and its four siblings are not aliases; they are removed.

    Keeping them as aliases would mean two names for populations that do not
    coincide - old `thermal` spanned environmental, cooling AND it_equipment -
    and an operator comparing the two would find the estate disagreeing with
    itself.
    """
    for old in ("thermal", "connectivity", "datapoint", "anomaly", "other"):
        assert old not in CATEGORIES


def test_severity_survives_the_move():
    block = sites_service._alarms(_row(alerts_total=9, crit=4, major=3, minor=2))
    assert (block["total"], block["critical"], block["major"], block["minor"]) \
        == (9, 4, 3, 2)


# --------------------------------------------------------------- drill-down


def _alert_row(qty: int, **counts) -> dict:
    row = {
        "room_id": "r1", "room_name": "Server Hall A", "floor": "1",
        "datacenter_id": "dc1", "site_code": "DC1", "site_name": "DC1",
        "qty": qty, "alerts": 0, "devices": qty, "critical": 0, "major": 0,
        "minor": 0, "warning": 0,
    }
    row.update(counts)
    return row


async def test_drill_down_facets_are_the_rows_they_sit_under(monkeypatch):
    """Facets are folded from the same rows, not fetched separately.

    A facet from its own query is a second instant of the estate, and the modal
    then shows a breakdown that does not add up to the rows beneath it.
    """
    rows = [
        _alert_row(5, critical=2, major=3, detected_threshold=4, detected_state=1),
        _alert_row(2, major=1, minor=1, detected_state=2),
    ]
    monkeypatch.setattr(estate_service.repo, "alarms_by_room", _returns(rows))
    monkeypatch.setattr(estate_service.repo, "unlocated_alarms_by_category",
                        _returns({"total": 0, "alarms": 0}))

    out = await estate_service.alarms(_FakeSession(), categories=["power"])

    assert out["by_severity"] == {"critical": 2, "major": 4, "minor": 1,
                                  "warning": 0}
    assert out["by_detection"]["threshold"] == 4
    assert out["by_detection"]["state"] == 3
    assert sum(out["by_severity"].values()) == sum(r["qty"] for r in rows)


async def test_the_alert_column_is_context_and_never_a_total(monkeypatch):
    """A room's informational count rides beside its alarms, and nowhere else.

    The whole console was made alarms-only because four hundred stale-telemetry
    conditions drowned six real ones. The panel may SAY a room also holds a
    hundred and twenty of them - that is the context an engineer wants before
    walking somewhere - as long as it never adds them to a count.
    """
    rows = [
        _alert_row(2, alerts=120, critical=0, major=2, detected_absence=2),
        _alert_row(1, alerts=92, major=1, detected_absence=1),
    ]
    monkeypatch.setattr(estate_service.repo, "alarms_by_room", _returns(rows))
    monkeypatch.setattr(estate_service.repo, "unlocated_alarms_by_category",
                        _returns({"total": 0, "alarms": 0}))

    out = await estate_service.alarms(_FakeSession(), categories=["visibility"])

    # `total` is everything open, matching the counter that opened the panel;
    # `alarms` is the part of it somebody has to answer. Both are reported, and
    # the columns keep them apart on every row.
    assert [r["alerts"] for r in out["rows"]] == [120, 92]
    assert out["total"] == 3 + 120 + 92
    assert out["alarms"] == 3, "the actionable count must not absorb the alerts"
    # The facets describe the ALARMS: they are what an operator triages by.
    assert sum(out["by_severity"].values()) == 3
    assert sum(out["by_detection"].values()) == 3


async def test_platform_conditions_are_named_but_not_counted(monkeypatch):
    """A platform condition has no room, so it has no row - and no place here.

    LOCATION DECIDES. The panel's total is what its rows add up to, the strip
    counter that opened it counts the same population, and the monitoring badge
    carries the pipeline's own conditions. Adding them here is what used to
    make the estate read 2 while both of its sites read 0.

    They are still REPORTED, because the panel has to be able to say what it is
    not counting and point at the thing that is. Silence would be the one
    unacceptable answer.
    """
    monkeypatch.setattr(estate_service.repo, "alarms_by_room",
                        _returns([_alert_row(3, critical=3, detected_absence=3)]))
    monkeypatch.setattr(estate_service.repo, "unlocated_alarms_by_category",
                        _returns({"total": 2, "alarms": 2}))

    out = await estate_service.alarms(_FakeSession(), categories=["visibility"])

    assert out["total"] == 3
    assert out["alarms"] == 3
    assert out["unlocated"] == 2
    assert out["unlocated_alarms"] == 2
    assert sum(out["by_severity"].values()) == 3


async def test_a_category_is_answered_from_the_stamped_column(monkeypatch):
    seen: dict = {}

    async def _capture(_session, *, categories):
        seen["categories"] = categories
        return []

    monkeypatch.setattr(estate_service.repo, "alarms_by_room", _capture)
    monkeypatch.setattr(estate_service.repo, "unlocated_alarms_by_category",
                        _returns({"total": 0, "alarms": 0}))

    await estate_api.alarms(category=["cooling"], session=_FakeSession())

    assert seen == {"categories": ["cooling"]}


async def test_a_grouped_counter_asks_once(monkeypatch):
    """Cooling is two categories and one question.

    Asking per category and stitching the answers together gave one room two
    rows, and no honest way to count its devices: a device faulting in both
    domains is one device, and only a `count(DISTINCT)` over the union can say
    so. The API takes the whole set.
    """
    seen: dict = {}

    async def _capture(_session, *, categories):
        seen["categories"] = categories
        return []

    monkeypatch.setattr(estate_service.repo, "alarms_by_room", _capture)
    monkeypatch.setattr(estate_service.repo, "unlocated_alarms_by_category",
                        _returns({"total": 0, "alarms": 0}))

    await estate_api.alarms(category=["cooling", "environmental", "cooling"],
                            session=_FakeSession())

    # Deduplicated, and in the order asked: a repeated parameter is a caller
    # mistake, not a reason to count a category twice.
    assert seen == {"categories": ["cooling", "environmental"]}


@pytest.mark.parametrize("category", ["thermalish", "thermal", "datapoint"])
async def test_an_unknown_category_is_rejected_rather_than_answered_empty(category):
    """Zero rows and a wrong filter look identical on the screen.

    The retired names are in here deliberately: a bookmarked drill-down URL
    from the old UI must fail loudly rather than open an empty modal that reads
    as "nothing wrong in this category".
    """
    with pytest.raises(HTTPException) as exc:
        await estate_api.alarms(category=[category], session=_FakeSession())
    assert exc.value.status_code == 400


# ------------------------------------------------------------------- legend


async def test_the_legend_is_generated_from_the_classifier(monkeypatch):
    """The definition an operator reads comes from the module that applies it.

    A legend written beside the classifier drifts from it, and the first
    symptom is an operator routing work by a description that stopped being
    true.
    """
    monkeypatch.setattr(estate_api.taxonomy, "catalogue", _no_catalogue)
    legend = await estate_api.alarm_categories(session=_FakeSession())

    assert [c["key"] for c in legend["categories"]] == list(CATEGORIES)
    assert all(c["owner"] and c["description"] for c in legend["categories"])

    # The strip groups the seven into five headline counters. Grouping may not
    # lose a category: the table has a column for each, and a category in no
    # group would be countable in one place and invisible in the other.
    grouped = {c for g in legend["strip_groups"] for c in g["categories"]}
    assert grouped == set(CATEGORIES)
    assert [d["key"] for d in legend["detections"]] == list(DETECTIONS)
    assert all(d["label"] and d["description"] for d in legend["detections"])

    # Examples come out of the classifier's own table, so a condition that
    # moves between categories moves in the legend with it. Every example must
    # actually classify where the legend says it does.
    from app.core.alert_taxonomy import classify
    for cat in legend["categories"]:
        for alarm_type in cat["examples"]:
            assert classify(alarm_type) == cat["key"]

    assert "legacy_categories" not in legend
