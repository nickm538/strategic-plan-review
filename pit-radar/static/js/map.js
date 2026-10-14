// 3D map: MapLibre GL (CSP build) + deck.gl MapboxOverlay, interleaved so hotspot columns share the depth buffer
// with real 3D buildings (docs/08 §4).
//
// Basemaps (switchable, choice remembered per browser):
//   streets  OpenFreeMap "liberty": full OpenStreetMap detail, street names, POIs, transit, 3D buildings
//   dark     OpenFreeMap "dark": street + place names; 3D buildings added here
//   aerial   New York State orthoimagery (NYS ITS Geospatial Services, 2022-2025) with street/place labels on top
// If a basemap cannot load, a built-in dark vector style (no labels) keeps counties and hotspots usable.
import { emit } from "./store.js";
import { h, nyDateTime, reducedMotion, relTime, TYPE_COLOR, TYPE_LABEL } from "./util.js";

const PITCH_3D = 50;
const BEARING = -17;
const MAX_ZOOM = 20;
const LONG_ISLAND_FALLBACK = [[-73.78, 40.54], [-71.85, 41.3]];
const BASEMAP_KEY = "pr.basemap";
export const BASEMAPS = { streets: "Streets 3D", dark: "Dark 3D", aerial: "Aerial photos (NYS)" };

let map = null;
let overlay = null;
let cfg = null;
let role = "viewer";
let hotspots = [];
let signals = [];
let placeLabels = [];
let countyLabels = [];
let density = false;
let is3D = true;
let tourTimer = null;
let tourIndex = 0;
let pulseRAF = null;
const pulseT0 = performance.now();
let tooltipEl = null;
let selectedId = null;
let baseMapFailed = false;
let noteEl = null;
let basemap = "streets";
let countiesGeo = null;

const OFM_ATTRIBUTION = '<a href="https://openfreemap.org" target="_blank" rel="noopener">OpenFreeMap</a> '
  + '© <a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> '
  + 'Data from <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';

// ---------------------------------------------------------------- styles
/** Last-resort style when no basemap can load: dark vector shapes, no labels (labels then come from deck.gl). */
function fallbackStyle(vectorUrl) {
  const inClass = (classes) => ["in", ["get", "class"], ["literal", classes]];
  return {
    version: 8, name: "PIT Radar fallback",
    sources: { omt: { type: "vector", url: vectorUrl, attribution: OFM_ATTRIBUTION } },
    layers: [
      { id: "bg", type: "background", paint: { "background-color": "#070B14" } },
      { id: "water", type: "fill", source: "omt", "source-layer": "water", paint: { "fill-color": "#06182A" } },
      { id: "roads", type: "line", source: "omt", "source-layer": "transportation", filter: inClass(["motorway", "trunk", "primary", "secondary"]),
        paint: { "line-color": "#223150", "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.5, 16, 6] } },
    ],
  };
}

async function fetchJson(url, ms = 9000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), ms);
  try {
    const res = await fetch(url, { signal: ctl.signal, credentials: "omit" });
    if (!res.ok) throw new Error(`style ${res.status}`);
    return await res.json();
  } finally { clearTimeout(t); }
}

function vectorSourceId(style) {
  return Object.keys(style.sources || {}).find((k) => style.sources[k].type === "vector");
}

function firstSymbolIndex(layers) {
  const i = layers.findIndex((l) => l.type === "symbol");
  return i < 0 ? layers.length : i;
}

/** Make sure the style has real 3D buildings (OpenMapTiles render_height / render_min_height). */
function ensureBuildings3D(style, dark) {
  const src = vectorSourceId(style);
  if (!src) return style;
  const existing = style.layers.find((l) => l.type === "fill-extrusion");
  if (existing) {
    existing.paint = { ...existing.paint, "fill-extrusion-opacity": 0.85 };
    existing.minzoom = Math.min(existing.minzoom || 14, 14);
    return style;
  }
  const layer = {
    id: "building-3d", type: "fill-extrusion", source: src, "source-layer": "building", minzoom: 14,
    paint: {
      "fill-extrusion-color": dark
        ? ["interpolate", ["linear"], ["coalesce", ["get", "render_height"], 0], 0, "#1E2433", 40, "#2C3548", 120, "#3A4762"]
        : ["interpolate", ["linear"], ["coalesce", ["get", "render_height"], 0], 0, "#DCD6CC", 40, "#CFC8BC", 120, "#BFB7A9"],
      "fill-extrusion-height": ["interpolate", ["linear"], ["zoom"], 14, 0, 14.6, ["coalesce", ["get", "render_height"], 6]],
      "fill-extrusion-base": ["coalesce", ["get", "render_min_height"], 0],
      "fill-extrusion-opacity": 0.85,
    },
  };
  style.layers.splice(firstSymbolIndex(style.layers), 0, layer);
  return style;
}

function addSky(style, dark) {
  style.sky = dark
    ? { "sky-color": "#0A1124", "horizon-color": "#1C2744", "fog-color": "#0D1424", "sky-horizon-blend": 0.6, "horizon-fog-blend": 0.7, "fog-ground-blend": 0.85 }
    : { "sky-color": "#9CC7F0", "horizon-color": "#E4EEF8", "fog-color": "#F2F4F7", "sky-horizon-blend": 0.5, "horizon-fog-blend": 0.6, "fog-ground-blend": 0.9 };
  style.light = { anchor: "viewport", color: "#FFFFFF", intensity: dark ? 0.35 : 0.25, position: [1.3, 210, 40] };
  return style;
}

/** NYS aerial photos under the Liberty style's labels (street names, places, POIs) and translucent 3D buildings. */
function aerialStyle(liberty, aerial) {
  const s = JSON.parse(JSON.stringify(liberty));
  s.sources.nys_aerial = { type: "raster", tiles: aerial.tiles, tileSize: 512, maxzoom: 21, attribution: aerial.attribution };
  const labels = s.layers.filter((l) => l.type === "symbol");
  const b3d = s.layers.find((l) => l.type === "fill-extrusion");
  s.layers = [
    { id: "background", type: "background", paint: { "background-color": "#0B0F14" } },
    { id: "nys-aerial", type: "raster", source: "nys_aerial", paint: { "raster-fade-duration": 150 } },
    ...(b3d ? [{ ...b3d, paint: { ...b3d.paint, "fill-extrusion-opacity": 0.32 } }] : []),
    ...labels,
  ];
  return s;
}

async function buildStyle(name) {
  const m = (cfg && cfg.map) || {};
  const styles = m.styles || {};
  if (m.style_url) return m.style_url;   // admin-configured custom style wins
  if (name === "aerial") {
    if (!m.aerial || !m.aerial.tiles) throw new Error("aerial not configured");
    return addSky(aerialStyle(await fetchJson(styles.streets), m.aerial), false);
  }
  const dark = name === "dark";
  return addSky(ensureBuildings3D(await fetchJson(dark ? styles.dark : styles.streets), dark), dark);
}

async function applyBasemap(name) {
  basemap = BASEMAPS[name] ? name : "streets";
  try { localStorage.setItem(BASEMAP_KEY, basemap); } catch { /* private mode */ }
  let style;
  try {
    style = await buildStyle(basemap);
    baseMapFailed = false;
    showNote("");
  } catch {
    baseMapFailed = true;
    style = fallbackStyle(((cfg && cfg.map) || {}).vector_tiles || "https://tiles.openfreemap.org/planet");
    showNote("Base map unavailable right now. Showing counties and hotspots only.");
  }
  if (!map) return style;
  map.setStyle(style, { diff: false });
  map.once("style.load", () => { addCountyLayers(); reattachOverlay(); render(); });
  emit("basemap", basemap);
  return style;
}

export function setBasemap(name) { return applyBasemap(name); }
export const currentBasemap = () => basemap;

function libsReady() { return typeof window.maplibregl !== "undefined" && typeof window.deck !== "undefined"; }

function showNote(text) {
  if (!noteEl) return;
  noteEl.textContent = text;
  noteEl.hidden = !text;
}

// ---------------------------------------------------------------- init
export async function initMap(container, config, userRole) {
  cfg = config;
  role = userRole;
  noteEl = h("div.map-note.glass", { hidden: true, role: "status" });
  container.parentElement.appendChild(noteEl);
  tooltipEl = h("div.tooltip.glass", { hidden: true, role: "tooltip" });
  container.parentElement.appendChild(tooltipEl);
  if (!libsReady()) {
    showNote("3D map libraries failed to load. The feed and lists still work.");
    return null;
  }
  try { basemap = localStorage.getItem(BASEMAP_KEY) || "streets"; } catch { basemap = "streets"; }
  const mcfg = (config && config.map) || {};
  const bounds = mcfg.bounds || LONG_ISLAND_FALLBACK;
  const pad = 0.55;
  window.maplibregl.setWorkerUrl("/static/vendor/maplibre-gl/maplibre-gl-csp-worker.js");
  const style = await applyBasemap(basemap);
  const mobile = container.clientWidth <= 768;
  map = new window.maplibregl.Map({
    container,
    style,
    bounds,
    fitBoundsOptions: { padding: mobile ? { top: 30, bottom: Math.round(container.clientHeight * 0.45), left: 10, right: 10 } : { top: 60, bottom: 110, left: 40, right: 40 } },
    pitch: PITCH_3D,
    bearing: BEARING,
    maxZoom: MAX_ZOOM,
    maxPitch: 78,
    maxBounds: [[bounds[0][0] - pad * 2, bounds[0][1] - pad], [bounds[1][0] + pad * 2, bounds[1][1] + pad]],
    attributionControl: { compact: true },
    canvasContextAttributes: { antialias: true },
  });
  const ML = window.maplibregl;
  map.addControl(new ML.NavigationControl({ visualizePitch: true, showZoom: true }), "bottom-right");
  // "Near me": the browser's location stays in the browser; it is never sent to the server (docs/08 §3).
  map.addControl(new ML.GeolocateControl({ positionOptions: { enableHighAccuracy: true }, trackUserLocation: true, showAccuracyCircle: true }), "bottom-right");
  map.addControl(new ML.ScaleControl({ unit: "imperial", maxWidth: 120 }), "bottom-left");
  map.on("error", (e) => {
    const msg = String((e && e.error && e.error.message) || "");
    const src = e && (e.sourceId || (e.source && e.source.id));
    if (!baseMapFailed && (src === "openmaptiles" || src === "omt" || src === "nys_aerial" || /Failed to fetch|NetworkError|AJAXError/i.test(msg))) {
      baseMapFailed = true;
      showNote(src === "nys_aerial" ? "Aerial photos unavailable right now. Try Streets 3D." : "Some map tiles could not load. Hotspots and counties still show.");
      render();
    }
  });
  overlay = makeOverlay();
  map.addControl(overlay);
  map.on("load", () => { addCountyLayers(); render(); emit("map:ready", true); });
  map.on("zoomend", render);
  map.on("dragstart", stopTour);
  return map;
}

function makeOverlay() {
  return new window.deck.MapboxOverlay({
    interleaved: true,
    layers: [],
    getCursor: ({ isHovering }) => (isHovering ? "pointer" : "grab"),
    onHover,
    onClick,
  });
}

/** After a basemap switch, re-attach deck.gl so its layers are re-inserted into the new style. */
function reattachOverlay() {
  if (!map || !overlay) return;
  try { map.removeControl(overlay); } catch { /* not attached */ }
  overlay = makeOverlay();
  map.addControl(overlay);
}

// ---------------------------------------------------------------- counties: mask + glow
export function setCounties(fc) {
  countiesGeo = fc && fc.features && fc.features.length ? fc : null;
  countyLabels = [];
  if (countiesGeo) {
    for (const f of countiesGeo.features) {
      const rings = outerRings(f.geometry).slice().sort((a, b) => b.length - a.length);
      const biggest = rings[0];
      if (!biggest) continue;
      let minX = 180, minY = 90, maxX = -180, maxY = -90;
      for (const [x, y] of biggest) { minX = Math.min(minX, x); minY = Math.min(minY, y); maxX = Math.max(maxX, x); maxY = Math.max(maxY, y); }
      countyLabels.push({ name: `${(f.properties && f.properties.county) || ""} County`, position: [(minX + maxX) / 2, (minY + maxY) / 2] });
    }
  }
  if (map && map.isStyleLoaded()) addCountyLayers();
  render();
}

function outerRings(geom) {
  if (!geom) return [];
  if (geom.type === "Polygon") return [geom.coordinates[0]];
  if (geom.type === "MultiPolygon") return geom.coordinates.map((p) => p[0]);
  return [];
}

function addCountyLayers() {
  if (!map || !countiesGeo || !map.getStyle()) return;
  const holes = countiesGeo.features.flatMap((f) => outerRings(f.geometry));
  const mask = { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [[[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]], ...holes] } };
  const setData = (id, data) => {
    const src = map.getSource(id);
    if (src) src.setData(data); else map.addSource(id, { type: "geojson", data });
  };
  setData("pr-mask", mask);
  setData("pr-counties", countiesGeo);
  const layers = map.getStyle().layers || [];
  const beforeId = (layers.find((l) => l.type === "symbol") || {}).id;   // keep street names on top
  const dark = basemap !== "streets";
  if (!map.getLayer("pr-mask")) {
    map.addLayer({ id: "pr-mask", type: "fill", source: "pr-mask", paint: { "fill-color": "#070B14", "fill-opacity": dark ? 0.55 : 0.42 } }, beforeId);
    map.addLayer({ id: "pr-county-glow", type: "line", source: "pr-counties", paint: { "line-color": "#5AB0FF", "line-opacity": 0.2, "line-width": 7, "line-blur": 6 } }, beforeId);
    map.addLayer({ id: "pr-county-line", type: "line", source: "pr-counties", paint: { "line-color": "#3B8FE0", "line-opacity": 0.85, "line-width": 1.6 } }, beforeId);
  }
}

// ---------------------------------------------------------------- data in
export function setHotspots(list) { hotspots = list.slice(); render(); }
export function setSignals(list) { signals = list.slice(); render(); }
export function setPlaceLabels(list) { placeLabels = list.slice(); render(); }
export function setSelected(id) { selectedId = id; render(); }

// ---------------------------------------------------------------- layers
/** Meters per screen pixel at the map center (512 px tiles). */
function metersPerPixel() {
  const lat = map.getCenter().lat;
  return (156543.03392 * Math.cos((lat * Math.PI) / 180)) / 2 ** (map.getZoom() + 1);
}

function typeColor(t, alpha = 235) {
  const c = TYPE_COLOR[t] || TYPE_COLOR.mixed;
  return [c[0], c[1], c[2], t === "mixed" ? Math.round(alpha * 0.8) : alpha];
}

function render() {
  if (!overlay || !map) return;
  const D = window.deck;
  const zoom = map.getZoom();
  // Columns are sized in screen terms so they read well at island scale AND sit on a single place at street
  // level: about 10 px wide (12-2,000 m) and up to ~130 px tall (40 m minimum).
  const mpp = metersPerPixel();
  const radius = Math.min(2000, Math.max(12, 10 * mpp));
  const maxElev = Math.round(Math.max(40, 130 * mpp) / 5) * 5;
  const maxScore = Math.max(0.0001, ...hotspots.map((d) => d.score || 0));
  const elev = (d) => maxElev * (0.15 + 0.85 * Math.min(1, (d.score || 0) / maxScore));
  const streetLevel = zoom >= 15;
  const layers = [];

  layers.push(new D.TextLayer({
    id: "county-labels", data: zoom < 9.6 ? countyLabels : [], getPosition: (d) => d.position, getText: (d) => d.name,
    getSize: 15, getColor: basemap === "streets" ? [30, 60, 110, 210] : [156, 203, 255, 190], fontFamily: "system-ui, sans-serif",
    fontWeight: 700, characterSet: "auto", billboard: true, outlineWidth: 2, outlineColor: basemap === "streets" ? [255, 255, 255, 200] : [7, 11, 20, 200],
    fontSettings: { sdf: true }, parameters: { depthTest: false },
  }));
  if (baseMapFailed) {   // the real basemap has its own place names; only label places ourselves without it
    const placeData = placeLabels.filter((p) => ((p.kind === "town" || p.kind === "city") ? zoom >= 8.4 && zoom < 11.5 : zoom >= 11 && zoom < 14.5));
    layers.push(new D.TextLayer({
      id: "place-labels", data: placeData, getPosition: (d) => [d.lon, d.lat], getText: (d) => d.name,
      getSize: (d) => ((d.kind === "town" || d.kind === "city") ? 12 : 11), getColor: [131, 145, 173, 220],
      fontFamily: "system-ui, sans-serif", characterSet: "auto", billboard: true, parameters: { depthTest: false },
    }));
  }

  if (density) {
    layers.push(new D.HexagonLayer({
      id: "density", data: hotspots, getPosition: (d) => [d.lon, d.lat], radius: 1000, extruded: true, elevationScale: 60,
      getElevationWeight: (d) => d.score || 0, elevationAggregation: "SUM", getColorWeight: (d) => d.score || 0, colorAggregation: "SUM",
      colorRange: [[30, 60, 110], [41, 98, 160], [70, 140, 200], [140, 170, 150], [214, 160, 70], [230, 159, 0]],
      coverage: 0.88, pickable: false, upperPercentile: 100, opacity: 0.85, material: { ambient: 0.6, diffuse: 0.6, shininess: 24 },
    }));
  } else {
    layers.push(new D.ColumnLayer({
      id: "hotspot-columns", data: hotspots, diskResolution: 32, radius, extruded: true, pickable: true, autoHighlight: true,
      highlightColor: [255, 255, 255, 80], getPosition: (d) => [d.lon, d.lat], getElevation: elev,
      getFillColor: (d) => typeColor(d.primary_type, d.review_only ? 110 : (d.id === selectedId ? 255 : streetLevel ? 205 : 232)),
      material: { ambient: 0.55, diffuse: 0.65, shininess: 40, specularColor: [90, 120, 170] },
      transitions: reducedMotion() ? {} : { getElevation: { duration: 500 } },
      updateTriggers: { getFillColor: [selectedId, streetLevel], getElevation: [maxScore, maxElev] },
    }));
    layers.push(new D.ScatterplotLayer({
      id: "hotspot-rings", data: hotspots.filter((d) => d.confirmed_30d > 0 || d.needs_review_30d > 0 || d.id === selectedId),
      getPosition: (d) => [d.lon, d.lat], getRadius: radius * 1.5, stroked: true, filled: false, lineWidthUnits: "pixels",
      getLineWidth: (d) => (d.id === selectedId ? 3 : 2),
      getLineColor: (d) => (d.id === selectedId ? [255, 255, 255, 235] : d.confirmed_30d > 0 ? [0, 158, 115, 240] : [240, 228, 66, 235]),
      pickable: false, updateTriggers: { getLineColor: [selectedId], getLineWidth: [selectedId] },
    }));
    const now = Date.now();
    const fresh = hotspots.filter((d) => d.last_signal_at && now - new Date(d.last_signal_at).getTime() < 24 * 3600 * 1000);
    if (fresh.length && !reducedMotion()) {
      const t = ((performance.now() - pulseT0) % 2400) / 2400;
      layers.push(new D.ScatterplotLayer({
        id: "pulse", data: fresh, getPosition: (d) => [d.lon, d.lat], getRadius: radius, radiusScale: 1 + t * 2.4,
        stroked: true, filled: false, lineWidthUnits: "pixels", getLineWidth: 2,
        getLineColor: (d) => typeColor(d.primary_type, Math.round(255 * (1 - t))), pickable: false,
        updateTriggers: { getLineColor: [t] },
      }));
      ensurePulse();
    } else {
      stopPulse();
    }
    const top = [...hotspots].sort((a, b) => (b.score || 0) - (a.score || 0)).slice(0, zoom >= 14 ? 40 : 10);
    layers.push(new D.TextLayer({
      id: "hotspot-labels", data: zoom >= 11 ? top : [], getPosition: (d) => [d.lon, d.lat, elev(d) + maxElev * 0.08],
      getText: (d) => d.label, getSize: 13, getColor: [232, 238, 248, 255], background: true,
      getBackgroundColor: [13, 20, 36, 230], backgroundPadding: [6, 3], fontFamily: "system-ui, sans-serif",
      fontWeight: 600, characterSet: "auto", billboard: true, getPixelOffset: [0, -10], parameters: { depthTest: false },
    }));
  }
  if ((role === "reviewer" || role === "admin") && zoom >= 15) {
    const pts = signals.filter((s) => s.lat !== null && s.lon !== null && s.precision === "public_place");
    layers.push(new D.ScatterplotLayer({
      id: "signal-points", data: pts, getPosition: (d) => [d.lon, d.lat], getRadius: 6, radiusUnits: "meters",
      radiusMinPixels: 5, getFillColor: (d) => typeColor(d.signal_type, 255), stroked: true, getLineColor: [255, 255, 255, 255],
      lineWidthMinPixels: 1.5, pickable: true, parameters: { depthTest: false },
    }));
  }
  overlay.setProps({ layers });
}

function ensurePulse() {
  if (pulseRAF) return;
  let last = 0;
  const step = (ts) => {
    pulseRAF = requestAnimationFrame(step);
    if (ts - last > 50) { last = ts; render(); }   // ~20 fps is plenty for a soft pulse
  };
  pulseRAF = requestAnimationFrame(step);
}
function stopPulse() { if (pulseRAF) { cancelAnimationFrame(pulseRAF); pulseRAF = null; } }

// ---------------------------------------------------------------- interaction
function onHover(info) {
  if (!tooltipEl) return;
  const d = info && info.object;
  if (!d || !(info.layer && (info.layer.id === "hotspot-columns" || info.layer.id === "signal-points"))) {
    tooltipEl.hidden = true;
    return;
  }
  tooltipEl.replaceChildren();
  if (info.layer.id === "hotspot-columns") {
    tooltipEl.append(
      h("div.t-title", d.label),
      h("div.t-row", h(`span.glyph.${d.primary_type}`), TYPE_LABEL[d.primary_type] || d.primary_type, ` · ${d.town ? d.town + ", " : ""}${d.county}`),
      h("div.t-row", `Last signal ${relTime(d.last_signal_at)}`),
      h("div.t-row", `${d.signals_7d} in 7 days · ${d.signals_30d} in 30 days`),
      h("div.t-row", `${d.distinct_sources_30d} independent source${d.distinct_sources_30d === 1 ? "" : "s"}`),
      h("div.t-row", d.review_only ? "Awaiting staff review" : d.confirmed_30d > 0 ? `${d.confirmed_30d} staff-confirmed` : "AI-verified, not yet confirmed"),
    );
  } else {
    tooltipEl.append(h("div.t-title", d.place_label || "Signal"), h("div.t-row", nyDateTime(d.published_at)));
  }
  tooltipEl.style.left = `${info.x}px`;
  tooltipEl.style.top = `${info.y}px`;
  tooltipEl.hidden = false;
}

function onClick(info) {
  const d = info && info.object;
  if (!d) return;
  stopTour();
  if (info.layer.id === "hotspot-columns") emit("select", { kind: "hotspot", id: d.id });
  else if (info.layer.id === "signal-points") emit("select", { kind: "signal", id: d.id });
}

/** Keep the target visible beside the detail drawer (desktop) or above the bottom sheet (phones). */
function viewPadding() {
  const el = map.getContainer();
  const w = el.clientWidth, hgt = el.clientHeight;
  if (w <= 768) return { top: 40, bottom: Math.round(hgt * 0.45), left: 20, right: 20 };
  const drawerOpen = document.querySelector(".drawer.open");
  return { top: 60, bottom: 120, left: 40, right: drawerOpen ? Math.min(440, w * 0.45) : 40 };
}

export function flyTo(lon, lat, { zoom = 16.5, orbit = true } = {}) {
  if (!map) return;
  const rm = reducedMotion();
  map.flyTo({ center: [lon, lat], zoom: Math.max(map.getZoom(), zoom), pitch: is3D ? 60 : 0, bearing: map.getBearing(),
    padding: viewPadding(), duration: rm ? 0 : 1400, essential: true });
  if (orbit && !rm && is3D) {
    map.once("moveend", () => map.easeTo({ bearing: map.getBearing() + 12, duration: 2800, easing: (t) => t * (2 - t) }));
  }
}

export function flyToHotspot(hs) { if (hs) { setSelected(hs.id); flyTo(hs.lon, hs.lat); } }

export function toggle3D() {
  if (!map) return is3D;
  is3D = !is3D;
  map.easeTo({ pitch: is3D ? PITCH_3D : 0, bearing: is3D ? map.getBearing() : 0, duration: reducedMotion() ? 0 : 700 });
  return is3D;
}
export function set3D(on) { if (is3D !== on) toggle3D(); }

export function resetView() {
  if (!map) return;
  const b = (cfg && cfg.map && cfg.map.bounds) || LONG_ISLAND_FALLBACK;
  map.fitBounds(b, { padding: viewPadding(), pitch: is3D ? PITCH_3D : 0, bearing: BEARING, duration: reducedMotion() ? 0 : 900 });
}

export function toggleDensity() { density = !density; render(); return density; }

function ranked() { return [...hotspots].sort((a, b) => (b.score || 0) - (a.score || 0)); }

export function step(dir) {
  const list = ranked();
  if (!list.length) return null;
  const cur = list.findIndex((d) => d.id === selectedId);
  const next = list[(cur + dir + list.length) % list.length];
  emit("select", { kind: "hotspot", id: next.id });
  return next;
}

export function toggleTour() {
  if (tourTimer) { stopTour(); return false; }
  const list = ranked().slice(0, 12);
  if (!list.length) return false;
  tourIndex = 0;
  const go = () => { const d = list[tourIndex % list.length]; tourIndex += 1; emit("select", { kind: "hotspot", id: d.id, fromTour: true }); };
  go();
  tourTimer = setInterval(go, 6000);
  emit("tour", true);
  return true;
}
export function stopTour() { if (tourTimer) { clearInterval(tourTimer); tourTimer = null; emit("tour", false); } }

export function resize() { if (map) map.resize(); }
export const getMap = () => map;
