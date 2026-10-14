// Copy the pinned map libraries from node_modules into static/vendor (run after `npm ci`).
// MapLibre: CSP build + separate worker (no blob: workers). deck.gl: standalone UMD bundle (global `deck`).
import { cpSync, mkdirSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const nm = join(root, "node_modules");
const out = join(root, "static", "vendor");
const files = [
  ["maplibre-gl/dist/maplibre-gl-csp.js", "maplibre-gl/maplibre-gl-csp.js"],
  ["maplibre-gl/dist/maplibre-gl-csp-worker.js", "maplibre-gl/maplibre-gl-csp-worker.js"],
  ["maplibre-gl/dist/maplibre-gl.css", "maplibre-gl/maplibre-gl.css"],
  ["maplibre-gl/LICENSE.txt", "maplibre-gl/LICENSE.txt"],
  ["deck.gl/dist.min.js", "deck.gl/dist.min.js"],
  ["deck.gl/LICENSE", "deck.gl/LICENSE"],
];
for (const [src, dst] of files) {
  const from = join(nm, src);
  if (!existsSync(from)) { console.error(`missing ${from}; run npm ci first`); process.exit(1); }
  mkdirSync(dirname(join(out, dst)), { recursive: true });
  cpSync(from, join(out, dst));
  console.log(`vendored ${dst}`);
}
