"""build_geography end to end with respx-mocked TIGERweb (layer directory, paginated GeoJSON), Census
Gazetteer files and Overpass. All geometry is SYNTHETIC (rectangles roughly where the counties are);
nothing here is real boundary or place data. Output goes to tmp_path only."""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest
import respx

from app.paths import COUNTIES_FILE, GEO_MANIFEST_FILE, NYC_COUNTIES_FILE, PLACES_FILE
from app.pipeline.geo import build as B
from app.pipeline.geo.build import GeoBuildError, build_geography, geo_files_present, read_manifest
from app.pipeline.geo.gazetteer import Gazetteer
from app.pipeline.geo.geofence import Geofence

TW = B.TIGERWEB_BASE
SC_DIR = f"{TW}State_County/MapServer"
PL_DIR = f"{TW}Places_CouSub_ConCity_SubMCD/MapServer"
OVERPASS = B.OVERPASS_MIRRORS
ALIASES = Path(__file__).parent / "fixtures" / "geo" / "no-aliases.yaml"

# SYNTHETIC rectangles (w, s, e, n)
NASSAU = (-73.75, 40.55, -73.45, 40.95)
SUFFOLK = (-73.45, 40.60, -71.85, 41.30)
QUEENS = (-73.95, 40.55, -73.75, 40.80)
KINGS = (-74.05, 40.55, -73.95, 40.74)
QUEENS_PT = (40.70, -73.85)          # (lat, lon) inside the synthetic Queens rectangle


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch):
    monkeypatch.setattr(B, "RETRY_DELAYS", (0.0, 0.0))


def rect(w: float, s: float, e: float, n: float) -> dict[str, Any]:
    return {"type": "Polygon", "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


def feat(props: dict[str, Any], geom: Optional[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "Feature", "properties": props, "geometry": geom}


def fc(features: list[dict[str, Any]], exceeded: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "FeatureCollection", "features": features}
    if exceeded:
        out["exceededTransferLimit"] = True
        out["properties"] = {"exceededTransferLimit": True}
    return out


def ip(lat: float, lon: float) -> dict[str, str]:
    return {"INTPTLAT": f"{lat:+.7f}", "INTPTLON": f"{lon:+011.7f}"}


def lyr(i: int, name: str, parent: int = -1, subs: Optional[list[int]] = None) -> dict[str, Any]:
    return {"id": i, "name": name, "parentLayerId": parent, "subLayerIds": subs,
            "type": "Group Layer" if subs else "Feature Layer"}


SC_LAYERS = {"layers": [lyr(0, "States"), lyr(7, "Counties"), lyr(30, "Census 2020", subs=[31, 32]),
                        lyr(31, "States", 30), lyr(32, "Counties", 30)]}
PL_LAYERS = {"layers": [lyr(20, "Estates"), lyr(21, "County Subdivisions"), lyr(23, "Consolidated Cities"),
                        lyr(24, "Incorporated Places"), lyr(25, "Census Designated Places"),
                        lyr(40, "ACS 2025", subs=[41, 44, 45]), lyr(41, "County Subdivisions", 40),
                        lyr(44, "Incorporated Places", 40), lyr(45, "Census Designated Places", 40)]}

COUNTIES = fc([
    feat({"GEOID": "36059", "BASENAME": "Nassau", "NAME": "Nassau County", "STATE": "36", "COUNTY": "059"}, rect(*NASSAU)),
    feat({"GEOID": "36103", "BASENAME": "Suffolk", "NAME": "Suffolk County", "STATE": "36", "COUNTY": "103"}, rect(*SUFFOLK)),
])
NYC = fc([
    feat({"GEOID": "36081", "BASENAME": "Queens", "NAME": "Queens County", "STATE": "36", "COUNTY": "081"}, rect(*QUEENS)),
    feat({"GEOID": "36047", "BASENAME": "Kings", "NAME": "Kings County", "STATE": "36", "COUNTY": "047"}, rect(*KINGS)),
])


def cousub(geoid: str, name: str, lsadc: str, county: str, box: tuple[float, float, float, float]) -> dict[str, Any]:
    w, s, e, n = box
    suffix = {"43": "town", "25": "city", "86": "Reservation"}[lsadc]
    return feat({"GEOID": geoid, "BASENAME": name, "NAME": f"{name} {suffix}", "LSADC": lsadc, "COUNTY": county,
                 "COUSUB": geoid[-5:], **ip((s + n) / 2, (w + e) / 2)}, rect(*box))


COUSUBS = fc([
    cousub("3605900001", "Hempstead", "43", "059", (-73.75, 40.55, -73.55, 40.80)),
    cousub("3605900002", "North Hempstead", "43", "059", (-73.75, 40.80, -73.55, 40.95)),
    cousub("3605900003", "Oyster Bay", "43", "059", (-73.55, 40.55, -73.45, 40.95)),
    cousub("3605900004", "Long Beach", "25", "059", (-73.70, 40.56, -73.64, 40.60)),
    cousub("3610300001", "Islip", "43", "103", (-73.45, 40.60, -73.10, 40.80)),
    cousub("3610300002", "Brookhaven", "43", "103", (-73.10, 40.60, -72.70, 41.00)),
    cousub("3610300003", "Huntington", "43", "103", (-73.45, 40.80, -73.30, 41.00)),
    cousub("3610300004", "Southold", "43", "103", (-72.70, 41.00, -71.85, 41.30)),
    cousub("3610300005", "East Hampton", "43", "103", (-72.30, 40.85, -71.85, 41.00)),
    cousub("3610300006", "Synthetic", "86", "103", (-72.85, 40.78, -72.82, 40.80)),
])


def place(geoid: str, name: str, lsadc: str, box: tuple[float, float, float, float],
          pt: Optional[tuple[float, float]] = None) -> dict[str, Any]:
    w, s, e, n = box
    lat, lon = pt or ((s + n) / 2, (w + e) / 2)
    suffix = {"47": "village", "25": "city", "57": "CDP"}[lsadc]
    return feat({"GEOID": geoid, "BASENAME": name, "NAME": f"{name} {suffix}", "LSADC": lsadc, **ip(lat, lon)},
                rect(*box))


INCORPORATED_P1 = fc([
    place("3600001", "Hempstead", "47", (-73.64, 40.69, -73.60, 40.72)),
    place("3600002", "Long Beach", "25", (-73.70, 40.56, -73.64, 40.60)),
    place("3600003", "Synthetic Outside Village", "47", (-73.84, 40.98, -73.80, 41.00)),
], exceeded=True)
INCORPORATED_P2 = fc([place("3600004", "Patchogue", "47", (-73.03, 40.75, -73.00, 40.78))])
CDPS = fc([
    place("3600011", "Bay Shore", "57", (-73.27, 40.71, -73.22, 40.74)),
    place("3600012", "Hicksville", "57", (-73.54, 40.75, -73.50, 40.79)),
    place("3600013", "Medford", "57", (-73.00, 40.80, -72.97, 40.84)),
    place("3600014", "Synthetic Queens Place", "57", (-73.87, 40.69, -73.83, 40.71)),
])


def node(i: int, lat: float, lon: float, **tags: str) -> dict[str, Any]:
    return {"type": "node", "id": i, "lat": lat, "lon": lon, "tags": tags}


def way(i: int, lat: Optional[float], lon: Optional[float], kind: str = "way", **tags: str) -> dict[str, Any]:
    el: dict[str, Any] = {"type": kind, "id": i, "tags": tags}
    if lat is not None:
        el["center"] = {"lat": lat, "lon": lon}
    return el


STATIONS = {"version": 0.6, "osm3s": {"timestamp_osm_base": "2026-10-14T00:00:00Z"}, "elements": [
    node(1, 40.7240, -73.2440, name="Bay Shore", railway="station", network="LIRR", operator="Long Island Rail Road"),
    way(2, 40.7242, -73.2442, name="Bay Shore Station", railway="station", operator="Long Island Rail Road"),
    node(3, QUEENS_PT[0], QUEENS_PT[1], name="Jamaica", railway="station", network="LIRR"),
    node(4, 40.7670, -73.5280, name="Hicksville", railway="station", operator="MTA Long Island Rail Road",
         phone="+1 555 0100"),
    node(5, 40.9000, -73.6000, name="Synthetic Other Railroad Stop", railway="station", network="Metro-North"),
    way(6, None, None, kind="relation", name="Patchogue", railway="station", network="LIRR"),
]}
LANDMARKS = {"version": 0.6, "elements": [
    way(10, 40.7660, -73.0130, name="Patchogue-Medford Library", amenity="library", website="https://example.org"),
    way(11, 40.7300, -73.2400, name="Town Park", leisure="park"),
    node(12, 40.7305, -73.2405, name="Town Park", leisure="park"),
    way(13, 40.7700, -73.0200, name="Town Park", leisure="park"),
    node(14, 40.7050, -73.6250, name="Hempstead Transit Center", amenity="bus_station", alt_name="Hempstead Bus Terminal"),
    way(15, 40.7280, -73.2500, name="Synthetic Private Garden", leisure="park", access="private"),
    node(16, QUEENS_PT[0], QUEENS_PT[1], name="Synthetic Queens Hospital", amenity="hospital"),
    way(17, 40.8800, -72.8000, kind="relation", name="Synthetic Pine Preserve", leisure="nature_reserve"),
    node(18, 40.7700, -73.0100, amenity="library"),
    way(19, 40.8210, -72.9850, name="Synthetic Bus Hub", public_transport="station", bus="yes"),
]}


class GeoMock:
    """respx routes for TIGERweb, Census Gazetteer and Overpass, with switchable failures."""

    def __init__(self, router: respx.MockRouter, *, fail: set[str] = frozenset()):  # type: ignore[assignment]
        self.fail = set(fail)
        self.requests: list[httpx.Request] = []
        router.route(method="GET", host="tigerweb.geo.census.gov").mock(side_effect=self.tigerweb)
        router.route(method="GET", host="www2.census.gov").mock(side_effect=self.gazetteer)
        for url in OVERPASS:
            router.post(url).mock(side_effect=self.overpass)
        self.gazetteer_files: dict[str, bytes] = {}

    def tigerweb(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, p = request.url.path, request.url.params
        if path.endswith("State_County/MapServer"):
            if "sc_dir" in self.fail:
                return httpx.Response(503)
            return httpx.Response(200, json=SC_LAYERS)
        if path.endswith("Places_CouSub_ConCity_SubMCD/MapServer"):
            return httpx.Response(200, json=PL_LAYERS)
        assert p["f"] == "geojson" and p["outSR"] == "4326" and p["returnGeometry"] == "true"
        if path.endswith("State_County/MapServer/7/query"):
            if "counties" in self.fail:
                return httpx.Response(200, json={"error": {"code": 500, "message": "synthetic failure"}})
            if "mercator" in self.fail:
                bad = json.loads(json.dumps(COUNTIES))
                for f in bad["features"]:
                    f["geometry"] = rect(-8_200_000, 4_950_000, -8_100_000, 5_000_000)
                return httpx.Response(200, json=bad)
            assert p["maxAllowableOffset"] == "0.0002" and p["geometryPrecision"] == "6"
            return httpx.Response(200, json=COUNTIES if "'059'" in p["where"] else NYC)
        if path.endswith("/MapServer/21/query"):
            assert "COUNTY IN ('059','103')" in p["where"]
            return httpx.Response(200, json=COUSUBS)
        if path.endswith("/MapServer/24/query"):
            if "places" in self.fail:
                return httpx.Response(200, json={"error": {"code": 400, "message": "synthetic failure"}})
            assert p["geometryType"] == "esriGeometryEnvelope" and p["spatialRel"] == "esriSpatialRelIntersects"
            assert p["inSR"] == "4326" and p["where"] == "STATE='36'"
            return httpx.Response(200, json=INCORPORATED_P1 if p["resultOffset"] == "0" else INCORPORATED_P2)
        if path.endswith("/MapServer/25/query"):
            if "places" in self.fail:
                return httpx.Response(502)
            return httpx.Response(200, json=CDPS)
        return httpx.Response(404)

    def gazetteer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        name = request.url.path.rsplit("/", 1)[-1]
        body = self.gazetteer_files.get(name)
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    def overpass(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "overpass" in self.fail:
            return httpx.Response(504)
        form = dict(x.split("=", 1) for x in request.content.decode().split("&"))
        q = httpx.QueryParams(request.content.decode())["data"]
        assert "out center tags" in q and form
        return httpx.Response(200, json=STATIONS if '"railway"="station"' in q else LANDMARKS)

    def calls(self, needle: str) -> list[httpx.Request]:
        return [r for r in self.requests if needle in str(r.url)]


async def _build(tmp_path: Path, **kw: Any) -> tuple[dict[str, Any], GeoMock]:
    fail = kw.pop("fail", set())
    files = kw.pop("gazetteer_files", {})
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            mock = GeoMock(router, fail=fail)
            mock.gazetteer_files.update(files)
            manifest = await build_geography(http, tmp_path / "geo", aliases_path=ALIASES,
                                             contact_email=kw.pop("contact_email", "ops@example.org"))
    return manifest, mock


def load_places(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "geo" / PLACES_FILE).read_text())


def by(places: list[dict[str, Any]], name: str, kind: str) -> dict[str, Any]:
    got = [p for p in places if p["name"] == name and p["kind"] == kind]
    assert len(got) == 1, (name, kind, got)
    return got[0]


# ---------------------------------------------------------------------------- happy path
async def test_build_end_to_end(tmp_path):
    manifest, mock = await _build(tmp_path)
    out = tmp_path / "geo"
    assert geo_files_present(out)
    assert read_manifest(out) == manifest
    for name in (COUNTIES_FILE, NYC_COUNTIES_FILE, PLACES_FILE, GEO_MANIFEST_FILE):
        assert (out / name).is_file()
    assert not list(out.glob(".*tmp*"))                              # atomic writes leave no temp files

    assert manifest["status"] == "ok" and manifest["degraded_sources"] == []
    assert manifest["counts"] == {
        "town": {"Nassau": 3, "Suffolk": 5}, "city": {"Nassau": 1},
        "village": {"Nassau": 1, "Suffolk": 1}, "cdp": {"Nassau": 1, "Suffolk": 2},
        "lirr_station": {"Nassau": 1, "Suffolk": 1},
        "library": {"Suffolk": 1}, "park": {"Suffolk": 3}, "transit": {"Nassau": 1, "Suffolk": 1},
    }
    assert manifest["dropped_outside"] == 4
    assert manifest["dropped_outside_by_source"] == {"census_designated_places": 1, "incorporated_places": 1,
                                                     "landmarks": 1, "lirr_stations": 1}
    assert any("expected 13 towns + 2 cities" in w for w in manifest["warnings"])
    assert manifest["sources"]["counties"]["layer_id"] == 7
    assert manifest["sources"]["county_subdivisions"]["layer_id"] == 21
    assert manifest["sources"]["incorporated_places"]["layer_id"] == 24
    assert manifest["sources"]["census_designated_places"]["layer_id"] == 25
    assert manifest["sources"]["lirr_stations"]["osm_timestamp"] == "2026-10-14T00:00:00Z"
    assert any("OpenStreetMap" in a for a in manifest["attribution"])

    # Pagination honored exceededTransferLimit, with the envelope filter on every page.
    pages = mock.calls("/MapServer/24/query")
    assert [r.url.params["resultOffset"] for r in pages] == ["0", "3"]
    w, s, e, n = (float(v) for v in pages[0].url.params["geometry"].split(","))
    assert w < NASSAU[0] and s < NASSAU[1] and e > SUFFOLK[2] and n > SUFFOLK[3]
    # Identification on every request.
    assert all("PIT-Radar/1.0" in r.headers["user-agent"] and "ops@example.org" in r.headers["user-agent"]
               for r in mock.requests)

    data = load_places(tmp_path)
    places = data["places"]
    assert data["format"] == "pit-radar-places/1" and data["count"] == len(places)
    for p in places:
        assert set(p) == {"name", "kind", "county", "lat", "lon", "source", "source_id", "aliases",
                          "parent_name", "town"}
    hv = by(places, "Hempstead", "village")
    assert (hv["town"], hv["source"], hv["source_id"]) == ("Hempstead", "census_tigerweb", "place:3600001")
    assert "Village of Hempstead" in hv["aliases"]
    assert by(places, "Hempstead", "town")["source_id"] == "cousub:3605900001"
    assert by(places, "Long Beach", "city")["town"] == "Long Beach"
    assert not any(p["name"] == "Synthetic" for p in places)            # reservation is not a town
    bs = by(places, "Bay Shore", "lirr_station")
    assert (bs["source_id"], bs["parent_name"], bs["town"]) == ("node/1", "Bay Shore", "Islip")
    assert "Bay Shore LIRR station" in bs["aliases"]
    assert by(places, "Hicksville", "lirr_station")["parent_name"] == "Hicksville"
    lib = by(places, "Patchogue-Medford Library", "library")
    assert (lib["parent_name"], lib["town"], lib["source_id"]) == ("Patchogue", "Brookhaven", "way/10")
    parks = sorted((p["parent_name"] or "", p["source_id"]) for p in places if p["name"] == "Town Park")
    assert parks == [("Bay Shore", "way/11"), ("Patchogue", "way/13")]       # node/12 deduped
    pres = by(places, "Synthetic Pine Preserve", "park")
    assert (pres["parent_name"], pres["town"]) == (None, "Brookhaven")
    assert by(places, "Hempstead Transit Center", "transit")["aliases"] == ["Hempstead Bus Terminal"]
    assert by(places, "Synthetic Bus Hub", "transit")["parent_name"] == "Medford"
    names = {p["name"] for p in places}
    assert not names & {"Jamaica", "Synthetic Queens Hospital", "Synthetic Private Garden", "Synthetic Queens Place",
                        "Synthetic Outside Village", "Synthetic Other Railroad Stop", "Patchogue Station"}
    raw = (out / PLACES_FILE).read_text()
    assert "555" not in raw and "example.org" not in raw                   # tag allow-list

    # The outputs load with the runtime loaders, and every place is inside the polygons.
    gf = Geofence.load(out / COUNTIES_FILE, out / NYC_COUNTIES_FILE)
    gaz = Gazetteer.load(out / PLACES_FILE, aliases_path=ALIASES)
    assert all(gf.county_at(p.lat, p.lon) == p.county for p in gaz.places)
    assert gf.nyc_county_at(*QUEENS_PT) == "Queens"
    assert gaz.match_location("the train station in Bay Shore").source_id == "node/1"


async def test_build_without_contact_email_still_identifies(tmp_path):
    _, mock = await _build(tmp_path, contact_email="")
    assert all(r.headers["user-agent"].startswith("PIT-Radar/1.0 (") for r in mock.requests)


# ---------------------------------------------------------------------------- failures
async def test_overpass_failure_is_degraded_not_fatal(tmp_path):
    manifest, mock = await _build(tmp_path, fail={"overpass"})
    assert manifest["status"] == "degraded"
    assert manifest["degraded_sources"] == ["landmarks", "lirr_stations"]
    assert manifest["sources"]["lirr_stations"]["status"] == "failed"
    assert manifest["sources"]["landmarks"]["status"] == "failed"
    assert "lirr_station" not in manifest["counts"] and "park" not in manifest["counts"]
    assert len(mock.calls("/api/interpreter")) == 2 * 2 * len(OVERPASS)    # 2 queries x 2 attempts x mirrors
    assert geo_files_present(tmp_path / "geo")


async def test_county_failure_raises_and_writes_nothing(tmp_path):
    with pytest.raises(GeoBuildError, match="county polygons"):
        await _build(tmp_path, fail={"counties"})
    assert not geo_files_present(tmp_path / "geo")
    assert not (tmp_path / "geo" / PLACES_FILE).exists()


async def test_counties_in_wrong_projection_are_rejected(tmp_path):
    with pytest.raises(GeoBuildError, match="WGS84"):
        await _build(tmp_path, fail={"mercator"})


async def test_directory_failure_uses_known_layer_ids(tmp_path, monkeypatch):
    monkeypatch.setitem(B.FALLBACK_LAYER_IDS, (B.STATE_COUNTY_SERVICE, "counties"), 7)
    manifest, _ = await _build(tmp_path, fail={"sc_dir"})
    assert any("using known layer id 7" in w for w in manifest["warnings"])
    assert manifest["counts"]["town"] == {"Nassau": 3, "Suffolk": 5}


def gazetteer_text() -> bytes:
    """A SYNTHETIC file in the Census Gazetteer layout (tab-separated, padded last header)."""
    rows = [
        ["USPS", "GEOID", "GEOIDFQ", "ANSICODE", "NAME", "LSAD", "FUNCSTAT", "ALAND", "AWATER", "ALAND_SQMI",
         "AWATER_SQMI", "INTPTLAT", "INTPTLONG" + " " * 20],
        ["NY", "3600001", "x", "1", "Hempstead village", "47", "A", "1", "0", "1", "0", "40.705", "-73.620"],
        ["NY", "3600004", "x", "1", "Patchogue village", "47", "A", "1", "0", "1", "0", "40.765", "-73.015"],
        ["NY", "3600002", "x", "1", "Long Beach city", "25", "A", "1", "0", "1", "0", "40.580", "-73.670"],
        ["NY", "3600011", "x", "1", "Bay Shore CDP", "57", "S", "1", "0", "1", "0", "40.725", "-73.245"],
        ["NY", "3600012", "x", "1", "Hicksville CDP", "57", "S", "1", "0", "1", "0", "40.770", "-73.520"],
        ["NY", "3600099", "x", "1", "Synthetic Upstate village", "47", "A", "1", "0", "1", "0", "42.650", "-73.750"],
    ]
    return ("\n".join("\t".join(r) for r in rows) + "\n").encode()


async def test_places_layer_failure_falls_back_to_gazetteer_file(tmp_path):
    manifest, mock = await _build(tmp_path, fail={"places"},
                                  gazetteer_files={"2024_gaz_place_36.txt": gazetteer_text()})
    assert manifest["status"] == "degraded"
    for key in ("incorporated_places", "census_designated_places"):
        assert manifest["sources"][key]["source"] == "census_gazetteer"
        assert manifest["sources"][key]["url"].endswith("2024_gaz_place_36.txt")
    assert manifest["counts"]["village"] == {"Nassau": 1, "Suffolk": 1}
    assert manifest["counts"]["cdp"] == {"Nassau": 1, "Suffolk": 1}
    assert manifest["counts"]["city"] == {"Nassau": 1}                       # still the county subdivision
    places = load_places(tmp_path)["places"]
    assert by(places, "Bay Shore", "cdp")["source"] == "census_gazetteer"
    assert by(places, "Bay Shore", "cdp")["town"] == "Islip"
    # Without village/CDP polygons, parents come from the nearest centroid (within 2 km).
    assert by(places, "Patchogue-Medford Library", "library")["parent_name"] == "Patchogue"
    assert len(mock.calls("www2.census.gov")) == 1                           # fetched once, reused


async def test_zipped_gazetteer_variant(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("2024_Gaz_place_national.txt", gazetteer_text())
    manifest, mock = await _build(tmp_path, fail={"places"},
                                  gazetteer_files={"2024_Gaz_place_national.zip": buf.getvalue()})
    assert manifest["sources"]["census_designated_places"]["url"].endswith("2024_Gaz_place_national.zip")
    assert manifest["counts"]["cdp"] == {"Nassau": 1, "Suffolk": 1}
    tried = [r.url.path.rsplit("/", 1)[-1] for r in mock.calls("www2.census.gov")]
    assert tried[-1] == "2024_Gaz_place_national.zip" and tried[0] == "2024_gaz_place_36.txt"


async def test_places_and_gazetteer_failure_is_fatal(tmp_path):
    with pytest.raises(GeoBuildError, match="towns/localities"):
        await _build(tmp_path, fail={"places"})
    assert not geo_files_present(tmp_path / "geo")


# ---------------------------------------------------------------------------- helpers
def test_pick_layer_prefers_current_top_level():
    assert B.pick_layer(SC_LAYERS["layers"], ("Counties",)) == 7
    assert B.pick_layer(PL_LAYERS["layers"], ("Census Designated Places",)) == 25
    assert B.pick_layer([lyr(3, "Counties (2024)")], ("Counties",)) == 3
    assert B.pick_layer(PL_LAYERS["layers"], ("Tracts",)) is None


def test_clean_station_name_and_landmark_kind():
    assert B.clean_station_name("Bay Shore Station") == "Bay Shore"
    assert B.clean_station_name("Hicksville (LIRR)") == "Hicksville"
    assert B.clean_station_name("  Ronkonkoma  LIRR station ") == "Ronkonkoma"
    assert B.landmark_kind({"amenity": "bus_station"}) == "transit"
    assert B.landmark_kind({"public_transport": "station", "bus": "yes"}) == "transit"
    assert B.landmark_kind({"public_transport": "station"}) is None
    assert B.landmark_kind({"leisure": "nature_reserve"}) == "park"
    assert B.landmark_kind({"natural": "beach"}) == "beach"


def test_overpass_queries_use_the_bbox():
    bbox = (-73.77, 40.53, -71.83, 41.32)
    q = B.overpass_station_query(bbox)
    assert "(40.53000,-73.77000,41.32000,-71.83000)" in q
    assert '"network"~"Long Island Rail ?Road|LIRR",i' in q and '"operator"~' in q
    lq = B.overpass_landmark_query(bbox)
    for sel in ('"leisure"="park"', '"leisure"="nature_reserve"', '"natural"="beach"', '"shop"="mall"',
                '"amenity"="bus_station"', '"bus"="yes"', '"amenity"="library"', '"amenity"="hospital"'):
        assert sel in lq


def test_geo_files_present_requires_manifest(tmp_path):
    d = tmp_path / "geo"
    d.mkdir()
    assert not geo_files_present(d)
    (d / COUNTIES_FILE).write_text("{}")
    (d / PLACES_FILE).write_text("{}")
    assert not geo_files_present(d)
    (d / GEO_MANIFEST_FILE).write_text("not json")
    assert not geo_files_present(d)
    (d / GEO_MANIFEST_FILE).write_text(json.dumps({"built_at": "2026-10-14T00:00:00+00:00"}))
    assert geo_files_present(d)


# ---------------------------------------------------------------------------- CLI
def test_cli_prints_manifest_and_exit_codes(tmp_path, monkeypatch, capsys):
    import importlib.util

    spec = importlib.util.spec_from_file_location("build_geo_cli", Path(__file__).parents[1] / "scripts" / "build_geo.py")
    cli = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(cli)

    async def ok(http, out_dir, *, aliases_path=None):
        assert out_dir == tmp_path / "out"
        return {"built_at": "2026-10-14T00:00:00+00:00", "counts": {}, "dropped_outside": 0}

    async def boom(http, out_dir, *, aliases_path=None):
        raise GeoBuildError("county polygons unavailable: synthetic")

    monkeypatch.setattr(cli, "build_geography", ok)
    assert cli.main(["--out", str(tmp_path / "out"), "--quiet"]) == 0
    assert json.loads(capsys.readouterr().out)["built_at"].startswith("2026-10-14")
    monkeypatch.setattr(cli, "build_geography", boom)
    assert cli.main(["--out", str(tmp_path / "out"), "--quiet"]) == 1
    assert "county polygons unavailable" in capsys.readouterr().err
