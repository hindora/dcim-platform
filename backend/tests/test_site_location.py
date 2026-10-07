"""Where a site stands on the world map (migration 0100).

A position is asset data. The rules that matter: a known metro gives an
APPROXIMATE position and says so, an unknown city gives none rather than a
guess, and nothing an administrator set is overwritten by an import.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.v1.infrastructure import SiteLocation
from app.core.geo import METRO_CENTROIDS, city_centroid

BACKEND = Path(__file__).resolve().parents[1]
IMPORTER = (BACKEND / "app" / "importer" / "simulator.py").read_text(encoding="utf-8")


def test_a_known_metro_resolves_whatever_the_case_or_padding():
    assert city_centroid("Chicago") == (41.88, -87.63)
    assert city_centroid("  new york ") == (40.71, -74.01)


def test_an_unknown_city_gets_no_position_rather_than_a_guess():
    assert city_centroid("Atlantis") is None
    assert city_centroid(None) is None
    assert city_centroid("") is None


def test_every_centroid_is_on_the_globe():
    for city, (lat, lon) in METRO_CENTROIDS.items():
        assert -90 <= lat <= 90 and -180 <= lon <= 180, city


def test_the_migration_backfill_agrees_with_the_metro_table():
    spec = importlib.util.spec_from_file_location(
        "m0100", BACKEND / "alembic" / "versions" / "0100_sites_have_a_position_on_the_globe.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for city, pos in mod._BACKFILL.items():
        assert METRO_CENTROIDS[city] == pos, city


def test_a_position_off_the_globe_is_refused():
    with pytest.raises(ValidationError):
        SiteLocation(latitude=91, longitude=0)
    with pytest.raises(ValidationError):
        SiteLocation(latitude=0, longitude=-181)
    assert SiteLocation(latitude=41.9, longitude=-87.6).latitude == 41.9


def test_an_import_never_overwrites_a_position_somebody_set():
    start = IMPORTER.index("centroid = city_centroid(")
    body = IMPORTER[start:IMPORTER.index("# Levels, elevations and outline", start)]
    assert "AND latitude IS NULL" in body
    assert "'city centroid'" in body
