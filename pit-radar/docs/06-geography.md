# 06 — Geography: geofence, gazetteer, geocoding, snapping

## 1. Ground truth

| Fact | Value |
|---|---|
| In scope | **Nassau County (FIPS 36059)** and **Suffolk County (FIPS 36103)**, New York |
| Out of scope though on the island | **Queens (36081)** and **Kings / Brooklyn (36047)**, including Long Island City, Jamaica, Flushing, the Rockaways |
| Nassau municipalities | Towns of Hempstead, North Hempstead, Oyster Bay; Cities of Glen Cove and Long Beach |
| Suffolk towns | Babylon, Brookhaven, East Hampton, Huntington, Islip, Riverhead, Shelter Island, Smithtown, Southampton, Southold (Southold includes Fishers Island, far to the northeast) |
| Rough bounding box (for API viewbox hints only, never for scope decisions) | lon −73.78 to −71.85, lat 40.54 to 41.30 [verify against the built polygons] |

Scope is decided **only** by point-in-polygon against the county polygons below, never by bounding box or by the words "Long Island".

## 2. Data build (`scripts/build_geo.py`, run once with network access; output committed to `data/`)

### 2.1 County polygons → `data/li_counties.geojson`

- **Source (preferred):** U.S. Census Bureau TIGERweb ArcGIS REST, State_County service, counties layer. Query `STATE='36' AND COUNTY IN ('059','103')`, `f=geojson`, `outSR=4326`. Fetch Queens and Kings the same way into `data/nyc_li_counties.geojson` for negative tests. **[verify]** the layer id and field names from the service directory (`…/TIGERweb/State_County/MapServer?f=json`).
- **Fallback:** Census cartographic boundary file (`cb_<year>_us_county_500k`), filtered to the two GEOIDs.
- **Processing:** keep full resolution for the Nassau–Queens line, or simplify with ≤ 0.0002° tolerance (~20 m). Keep coastal water areas inside the polygon, so piers and beaches are not lost.
- **Validation (tests):** known in-scope points (Hempstead Transit Center, Ronkonkoma station, Montauk Point, Fishers Island) are inside. Known out-of-scope points (Jamaica station, Long Island City, Far Rockaway station, Floral Park's Queens side) are outside. Pull the test coordinates from the gazetteer and OSM; never type them by hand.

### 2.2 Gazetteer → `data/li_places.json` (and the `places` table)

| Layer | Source | Kinds |
|---|---|---|
| County subdivisions (13 towns + 2 cities) | TIGERweb `Places_CouSub_ConCity_SubMCD`, County Subdivisions layer | `town`, `city` |
| Incorporated places and CDPs (villages, hamlets) | same service, Incorporated Places + Census Designated Places layers, using `INTPTLAT`/`INTPTLON` as the centroid | `village`, `cdp` |
| LIRR stations | OpenStreetMap via Overpass: `railway=station` with `network`/`operator` = Long Island Rail Road, inside the county polygons (or MTA LIRR GTFS `stops.txt`) | `lirr_station` |
| Public landmarks | OSM Overpass: named `leisure=park` and `leisure=nature_reserve`, `natural=beach`, `shop=mall`, `amenity=bus_station`, `public_transport=station` (bus), `amenity=library`, `amenity=hospital` | `park`, `beach`, `mall`, `transit`, `library`, `hospital` |

Each entry: `{name, aliases[], kind, county, lat, lon, parent_place (village/CDP containing it), source, source_id}`.

- **Aliases:** generate spelling variants ("Bayshore"/"Bay Shore", "Pt Jeff"/"Port Jefferson", "Hicksville station"/"Hicksville LIRR", "Hempstead Village", "Ronkonkoma station"/"Ronk"), plus common abbreviations (St/Saint, Mt/Mount). Keep a hand-curated `data/aliases.yaml` and review it in code review.
- **Validation:** every entry must pass point-in-polygon against `li_counties.geojson`; drop and report any that fail. Record counts by kind and county, plus the retrieval date, in `data/README.md`.
- **Never fabricate coordinates.** Every entry comes from a fetched dataset with its `source_id`.

## 3. Geocoding pipeline (`app/pipeline/geo/`)

Input: the verified `location_text` (verbatim from the post), plus context: `platform_place_tag`, the query's place, the subreddit, and towns mentioned elsewhere in the text.

1. **Normalize:** lowercase; strip "the", "LIRR", "train", "station", "stop" into a station flag; expand abbreviations.
2. **Exact or alias match** in the gazetteer. When the text names both a landmark and a hamlet ("the library in Patchogue"), prefer the landmark whose `parent_place` matches the hamlet. Multiple landmarks with the same name (several "Town Park"s): choose the one whose parent matches a hamlet or town in the text; if still ambiguous → hamlet level.
3. **Fuzzy match** (`rapidfuzz`, token-set ratio ≥ 92) against names and aliases, restricted to candidates in the hamlet/town mentioned if any.
4. **Nominatim fallback** (only for intersections, street-level business descriptions, or unmatched names). Use `https://nominatim.openstreetmap.org/search` with `q=<text + hamlet + ", NY">`, `countrycodes=us`, `viewbox=<LI bbox>`, `bounded=1`, `addressdetails=1`, `limit=3`. Send a real `User-Agent` naming LICH and a contact email. Throttle to ≤ 1 request/second, and cache every result in `geocode_cache` (usage policy [verify current terms at operations.osmfoundation.org/policies/nominatim]). Accept only results whose `address.county` is Nassau or Suffolk **and** which pass the polygon test.
5. **Hamlet fallback:** if only a hamlet, village or town is identifiable, use its centroid with `precision = "hamlet"` (or `"town"`).
6. **Geofence:** shapely point-in-polygon on the final point. Fail → not mapped (V4).

## 4. Snapping (privacy rule R5)

The stored coordinate is **never** a raw geocode of a street address.

| Location kind | Stored point | `precision` |
|---|---|---|
| Named public place (station, park, beach, library, mall, transit center) | Gazetteer coordinate of that place | `public_place` |
| Business or intersection | Nearest gazetteer public place within 250 m if one exists; otherwise the Nominatim point **rounded to 3 decimals** (~100 m) | `public_place` |
| Street only, or private residence (`is_private_residence`) | Hamlet/village centroid | `hamlet` |
| Town only | Town centroid | `town` (feed and county stats only; not mapped as a pin) |
| Self-report | Town/hamlet name only, no coordinate stored | n/a (R7) |

## 5. Disambiguation reminders

These cases need tests: Long Island City; Jamaica; "Long Beach" (CA vs. NY); "Huntington" (WV/CA); "Levittown" (PA); "Freeport" (ME/Bahamas); "Brentwood" (CA/TN/UK); "Medford" (MA/OR); "Southampton" (UK); and "Westbury" (UK). The classifier rejects out-of-area items (V4). The geocoder independently refuses any point outside the polygons, so both layers must agree before a pin appears.
