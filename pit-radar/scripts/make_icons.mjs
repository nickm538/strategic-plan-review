// Render the PIT Radar icon (SVG below) to PNGs with headless Chromium. Run:
//   NODE_PATH=/opt/node-tools/node_modules node scripts/make_icons.mjs
import { createRequire } from "node:module";
const { chromium } = createRequire(import.meta.url)("playwright");   // resolved via NODE_PATH
import { writeFileSync } from "node:fs";

const svg = (pad) => `
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <radialGradient id="bg" cx="50%" cy="45%" r="70%"><stop offset="0" stop-color="#16244A"/><stop offset="1" stop-color="#070B14"/></radialGradient>
    <linearGradient id="sweep" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#5AB0FF" stop-opacity="0.75"/><stop offset="1" stop-color="#5AB0FF" stop-opacity="0"/></linearGradient>
  </defs>
  <rect width="512" height="512" rx="${pad ? 0 : 112}" fill="url(#bg)"/>
  <g transform="translate(256 266) scale(${pad ? 0.72 : 0.9})">
    <circle r="200" fill="none" stroke="#2A3A63" stroke-width="10"/>
    <circle r="135" fill="none" stroke="#2A3A63" stroke-width="10"/>
    <circle r="70" fill="none" stroke="#2A3A63" stroke-width="10"/>
    <path d="M0 0 L0 -200 A200 200 0 0 1 173 -100 Z" fill="url(#sweep)"/>
    <rect x="-118" y="-30" width="34" height="110" rx="8" fill="#E69F00"/>
    <rect x="-40" y="-110" width="34" height="190" rx="8" fill="#56B4E9"/>
    <rect x="38" y="-62" width="34" height="142" rx="8" fill="#CC79A7"/>
    <rect x="-150" y="80" width="300" height="12" rx="6" fill="#5AB0FF"/>
  </g>
</svg>`;

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined });
const page = await browser.newPage();
for (const [name, size, pad] of [["icon-192.png", 192, false], ["icon-512.png", 512, false], ["maskable-512.png", 512, true], ["favicon.png", 64, false]]) {
  await page.setViewportSize({ width: size, height: size });
  await page.setContent(`<html><body style="margin:0;background:transparent">${svg(pad).replace("<svg ", `<svg width="${size}" height="${size}" `)}</body></html>`);
  writeFileSync(`static/icons/${name}`, await page.screenshot({ omitBackground: true }));
  console.log("wrote", name);
}
writeFileSync("static/icons/icon.svg", svg(false).trim() + "\n");
await browser.close();
