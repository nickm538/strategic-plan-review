"""Geofence (docs/06 §1, V4) against SYNTHETIC rectangles in tests/fixtures/geo/ (docs/10 §1 Geofence row).

In-scope test points come from the (synthetic) gazetteer, never typed by hand: stations, a Fishers Island
place and the Montauk area. NYC points sit inside the synthetic Queens/Kings rectangles.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.pipeline.geo.gazetteer import Gazetteer
from app.pipeline.geo.geofence import Geofence

FIX = Path(__file__).parent / "fixtures" / "geo"


@pytest.fixture(scope="module")
def gf() -> Geofence:
    return Geofence.load(FIX / "counties.geojson", FIX / "nyc_counties.geojson")


@pytest.fixture(scope="module")
def gaz() -> Gazetteer:
    return Gazetteer.load(FIX / "places.json", aliases_path=FIX / "missing-aliases.yaml")


def _centre(fc_path: Path, county: str) -> tuple[float, float]:
    fc = json.loads(fc_path.read_text())
    for f in fc["features"]:
        if f["properties"]["county"] == county:
            ring = f["geometry"]["coordinates"][0]
            lon = sum(p[0] for p in ring[:-1]) / (len(ring) - 1)
            lat = sum(p[1] for p in ring[:-1]) / (len(ring) - 1)
            return lat, lon
    raise AssertionError(county)


def test_every_gazetteer_place_is_inside(gf, gaz):
    for p in gaz.places:
        assert gf.county_at(p.lat, p.lon) == p.county, p.name


@pytest.mark.parametrize("name,kind,county", [
    ("Ronkonkoma", "lirr_station", "Suffolk"),
    ("Hicksville", "lirr_station", "Nassau"),
    ("Fishers Island", "cdp", "Suffolk"),
    ("Montauk", "cdp", "Suffolk"),
    ("Main Beach", "beach", "Suffolk"),
])
def test_in_scope_points_inside(gf, gaz, name, kind, county):
    (p,) = gaz.by_name(name, (kind,))
    assert gf.county_at(p.lat, p.lon) == county
    assert gf.contains(p.lat, p.lon)
    assert gf.nyc_county_at(p.lat, p.lon) is None
    assert gf.outside_reason(p.lat, p.lon) is None


@pytest.mark.parametrize("county", ["Queens", "Kings"])
def test_nyc_points_outside_with_explanation(gf, county):
    lat, lon = _centre(FIX / "nyc_counties.geojson", county)
    assert gf.county_at(lat, lon) is None
    assert gf.nyc_county_at(lat, lon) == county
    reason = gf.outside_reason(lat, lon)
    assert reason and county in reason and "outside Nassau/Suffolk" in reason


def test_point_in_water_off_long_beach_is_inside(gf, gaz):
    """County polygons keep their coastal water (docs/06 §2.1), so a pier/beach point just offshore of the
    Long Beach city point (synthetic) still counts as Nassau."""
    (lb,) = gaz.by_name("Long Beach", ("city",))
    assert gf.county_at(lb.lat - 0.02, lb.lon) == "Nassau"


def test_far_away_and_invalid_points(gf):
    assert gf.county_at(34.05, -118.24) is None                # Los Angeles
    assert gf.nyc_county_at(34.05, -118.24) is None
    assert "outside the Nassau/Suffolk" in gf.outside_reason(34.05, -118.24)
    for bad in [(math.nan, -73.0), (40.7, math.inf), (None, -73.0), ("x", "y"), (95.0, -73.0)]:
        assert gf.county_at(*bad) is None                      # type: ignore[arg-type]
        assert gf.nyc_county_at(*bad) is None                  # type: ignore[arg-type]


def test_bbox_and_distance(gf):
    min_lon, min_lat, max_lon, max_lat = gf.bbox()
    assert (min_lon, min_lat, max_lon, max_lat) == pytest.approx((-73.75, 40.55, -71.85, 41.30))
    assert gf.distance_m(40.70, -73.60) == 0
    assert 15_000 < gf.distance_m(40.70, -73.95) < 20_000     # ~0.2 deg west of Nassau's edge


def test_to_geojson_simplified_and_stable(gf):
    a = gf.to_geojson()
    b = gf.to_geojson()
    assert a == b and a is not b
    assert [f["properties"]["county"] for f in a["features"]] == ["Nassau", "Suffolk"]
    assert all(f["geometry"]["type"] in ("Polygon", "MultiPolygon") for f in a["features"])
    a["features"].clear()                                      # callers may mutate their copy
    assert len(gf.to_geojson()["features"]) == 2


def test_load_without_nyc_file(tmp_path):
    g = Geofence.load(FIX / "counties.geojson", tmp_path / "absent.geojson")
    lat, lon = _centre(FIX / "nyc_counties.geojson", "Queens")
    assert g.county_at(lat, lon) is None
    assert g.nyc_county_at(lat, lon) is None
    assert g.outside_reason(lat, lon) == "point outside the Nassau/Suffolk county polygons"


def test_missing_county_is_an_error(tmp_path):
    fc = json.loads((FIX / "counties.geojson").read_text())
    fc["features"] = [f for f in fc["features"] if f["properties"]["county"] == "Nassau"]
    p = tmp_path / "one.geojson"
    p.write_text(json.dumps(fc))
    with pytest.raises(ValueError, match="Suffolk"):
        Geofence.load(p)


def test_county_names_from_census_properties():
    """Raw TIGERweb-style properties (BASENAME / GEOID) are understood as well as properties.county."""
    fc = json.loads((FIX / "counties.geojson").read_text())
    fc["features"][0]["properties"] = {"BASENAME": "Nassau", "GEOID": "36059"}
    fc["features"][1]["properties"] = {"GEOID": "36103"}
    g = Geofence.from_feature_collections(fc)
    assert g.county_at(40.70, -73.60) == "Nassau"
    assert g.county_at(40.80, -73.00) == "Suffolk"


def test_self_intersecting_polygon_is_repaired():
    bowtie = {"type": "Polygon", "coordinates": [[[-73.75, 40.55], [-73.45, 40.95], [-73.45, 40.55],
                                                  [-73.75, 40.95], [-73.75, 40.55]]]}
    fc = json.loads((FIX / "counties.geojson").read_text())
    fc["features"][0]["geometry"] = bowtie
    g = Geofence.from_feature_collections(fc)
    assert g.county_at(40.75, -73.70) == "Nassau"             # inside the left triangle
    assert g.county_at(40.60, -73.60) is None                 # between the two triangles
    assert g.to_geojson()["features"][0]["geometry"]["type"] in ("Polygon", "MultiPolygon")
