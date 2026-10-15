// 点声 DotVoice brand icons: one geometry source → SVG → PNG (Edge) → multi-size ICO.
//
// The mark (0.10.3): an indigo tile (0.10.4; rose until 0.10.3) with a white one-stroke "d" — the bowl
// sweeps up into the stem like a voice turning into a written letter — and its
// dot, the 点 of 点声. The dot is the status light: white when DotVoice is not
// running (app icon), a lit green LED when ready, red when paused/error. There
// is no separate badge anywhere, so the taskbar, tray and shortcuts all show
// exactly one light.
//
// Usage (from shell/):  node tools/make-icons.mjs [--preview-only]
// Writes: public/logo.svg, src-tauri/icons/*.ico, ../design/brand/svg/*.svg,
//         ../design/brand/png/*.png (every rendered size, for review).
// Every ICO entry is a PNG rendered at its exact pixel size; ≤32 px uses a
// heavier stroke and a flat tile so the glyph stays crisp in the tray.
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const shell = resolve(here, '..');
const brandDir = resolve(shell, '..', 'design', 'brand');

// ── Palette ─────────────────────────────────────────────────────────────
export const palette = {
  // Indigo-violet: the app's input accent and glass UI; green and red both stand out on it.
  // The UI brand colour follows this tile (src/styles.css --brand-2 / --brand / --brand-strong).
  tile: { top: '#7b74ff', mid: '#5b4ff0', bottom: '#3d2fc7' },
  glyph: '#ffffff',
  shadow: '#1b1263',
  dots: {
    ready: { base: '#2fd17a', light: '#8cf2b6', deep: '#169a4f' },
    paused: { base: '#ff4a3b', light: '#ffa093', deep: '#c42a20' },
    idle: { base: '#8a8d99', light: '#b4b7c1', deep: '#62656f' },
  },
  ring: '#17171c',
};

const f = (n) => +Number(n).toFixed(2);

/** Superellipse (n=5) tile path: continuous curvature like the capsule UI. */
export function squirclePath(x, y, w, h, n = 5, steps = 64) {
  const cx = x + w / 2, cy = y + h / 2, a = w / 2, b = h / 2, pts = [];
  const g = (v) => +v.toFixed(1);
  for (let i = 0; i < steps * 4; i++) {
    const t = (i / (steps * 4)) * Math.PI * 2, c = Math.cos(t), s = Math.sin(t);
    pts.push(`${g(cx + a * Math.sign(c) * Math.abs(c) ** (2 / n))} ${g(cy + b * Math.sign(s) * Math.abs(s) ** (2 / n))}`);
  }
  return `M${pts.join('L')}Z`;
}

/** Tile: full-bleed rounded square ≤32 px (every pixel counts), inset squircle above. */
function tilePath(size, inset = 0) {
  if (size > 32) {
    const m = (8 * size) / 256 + inset;
    return squirclePath(m, m, size - 2 * m, size - 2 * m);
  }
  const m = inset, w = size - 2 * m, r = w * 0.22, e = size - m;
  return `M${f(m + r)} ${f(m)}H${f(e - r)}A${f(r)} ${f(r)} 0 0 1 ${f(e)} ${f(m + r)}V${f(e - r)}A${f(r)} ${f(r)} 0 0 1 ${f(e - r)} ${f(e)}H${f(m + r)}A${f(r)} ${f(r)} 0 0 1 ${f(m)} ${f(e - r)}V${f(m + r)}A${f(r)} ${f(r)} 0 0 1 ${f(m + r)} ${f(m)}Z`;
}

/**
 * The glyph on a 256 canvas, optically centred (its bounding box sits on the
 * tile centre). `small` = heavier stroke and bigger dot for ≤32 px.
 */
export function glyph(small, state = 'brand', id = 'g', rich = false) {
  const w = small ? 30 : 25;
  // The dot is big on purpose: it is the status light, and must read at 16 px.
  const dot = small ? { x: 76, y: 48, r: 37, ring: 7 } : { x: 78, y: 54, r: 32, ring: 5 };
  // Bounding box centred on the tile (small: x 39–185, y 11–221; master: x 46–183, y 22–219).
  const shift = small ? 'translate(16 12)' : 'translate(13.5 7.5)';
  const stroke = `<path d="M170 60V150C170 186 146 206 116 206C86 206 64 184 64 156C64 128 86 108 114 108C142 108 152 128 170 142" `
    + `fill="none" stroke="${palette.glyph}" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round"/>`;
  const c = palette.dots[state];
  if (!c) {
    return { defs: '', body: `<g transform="${shift}">${stroke}<circle cx="${dot.x}" cy="${dot.y}" r="${dot.r}" fill="${palette.glyph}"/></g>` };
  }
  // A lit LED set in the white dot: the white rim keeps red crisp on the tile.
  const led = dot.r - dot.ring;
  const defs = '';
  const body = `<g transform="${shift}">`
    + stroke
    + `<circle cx="${dot.x}" cy="${dot.y}" r="${dot.r}" fill="${palette.glyph}"/>`
    // Flat, like the rest of the mark: one solid colour, no glow or highlight.
    + `<circle cx="${dot.x}" cy="${dot.y}" r="${led}" fill="${c.base}"/>`
    + `</g>`;
  return { defs, body };
}

/**
 * The app mark at a given pixel size.
 * @param {number} size  pixel size (also the viewBox)
 * @param {'brand'|'ready'|'paused'|'idle'} state  'brand' = unlit white dot
 * @param {{id?: string, title?: string}} [opts]
 */
export function markSvg(size, state = 'brand', opts = {}) {
  const id = opts.id || `dv${size}${state}`;
  const small = size <= 32, rich = size >= 48;
  const tile = tilePath(size);
  const t = palette.tile;
  const defs = [
    `<linearGradient id="${id}t" x1="0" y1="0" x2=".35" y2="1"><stop offset="0" stop-color="${t.top}"/><stop offset=".55" stop-color="${t.mid}"/><stop offset="1" stop-color="${t.bottom}"/></linearGradient>`,
  ];
  const parts = [`<path d="${tile}" fill="url(#${id}t)"/>`];
  if (rich) {
    defs.push(
      `<linearGradient id="${id}s" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#fff" stop-opacity=".28"/><stop offset=".5" stop-color="#fff" stop-opacity="0"/></linearGradient>`,
      `<clipPath id="${id}c"><path d="${tile}"/></clipPath>`,
      `<filter id="${id}g" x="-20%" y="-20%" width="140%" height="140%"><feDropShadow dx="0" dy="${f(size / 90)}" stdDeviation="${f(size / 70)}" flood-color="${palette.shadow}" flood-opacity=".35"/></filter>`,
    );
    // Glass: a soft dome of light over the top half, and a hairline rim.
    parts.push(`<g clip-path="url(#${id}c)"><ellipse cx="${f(size / 2)}" cy="${f(size * 0.02)}" rx="${f(size * 0.72)}" ry="${f(size * 0.46)}" fill="url(#${id}s)"/></g>`);
    const rim = size / 128;
    parts.push(`<path d="${tilePath(size, rim / 2)}" fill="none" stroke="#fff" stroke-opacity=".22" stroke-width="${f(rim)}"/>`);
  }
  const mark = glyph(small, state, id, rich);
  defs.push(mark.defs);
  parts.push(`<g transform="scale(${f(size / 256)})"${rich ? ` filter="url(#${id}g)"` : ''}>${mark.body}</g>`);
  const title = opts.title ? `<title>${opts.title}</title>` : '';
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">${title}<defs>${defs.join('')}</defs>${parts.join('')}</svg>`;
}

/** The web logo (title bar, favicon): the 256 master with a scalable viewBox and a title. */
export function logoSvg() {
  const inner = markSvg(256, 'brand', { id: 'logo' })
    .replace(/^<svg[^>]*>/, '')
    .replace(/<\/svg>$/, '');
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256">\n  <title>点声 DotVoice · 一点即声</title>\n  ${inner}\n</svg>\n`;
}

// ── ICO packing (PNG-compressed entries) ────────────────────────────────
export function packIco(entries) {
  const header = Buffer.alloc(6);
  header.writeUInt16LE(0, 0);
  header.writeUInt16LE(1, 2);
  header.writeUInt16LE(entries.length, 4);
  const dir = Buffer.alloc(16 * entries.length);
  let offset = 6 + dir.length;
  entries.forEach(({ size, png }, i) => {
    const e = i * 16;
    dir[e] = size >= 256 ? 0 : size;
    dir[e + 1] = size >= 256 ? 0 : size;
    dir[e + 2] = 0;
    dir[e + 3] = 0;
    dir.writeUInt16LE(1, e + 4);
    dir.writeUInt16LE(32, e + 6);
    dir.writeUInt32LE(png.length, e + 8);
    dir.writeUInt32LE(offset, e + 12);
    offset += png.length;
  });
  return Buffer.concat([header, dir, ...entries.map((x) => x.png)]);
}

// ── Icon set ────────────────────────────────────────────────────────────
const APP_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256];
const TRAY_SIZES = [16, 20, 24, 32, 40, 48, 64];
export const ICONS = [
  { file: 'icon.ico', sizes: APP_SIZES, svg: (s) => markSvg(s, 'brand') },
  { file: 'status-online.ico', sizes: TRAY_SIZES, svg: (s) => markSvg(s, 'ready') },
  { file: 'status-offline.ico', sizes: TRAY_SIZES, svg: (s) => markSvg(s, 'paused') },
  { file: 'shortcut-online.ico', sizes: APP_SIZES, svg: (s) => markSvg(s, 'ready') },
  { file: 'shortcut-offline.ico', sizes: APP_SIZES, svg: (s) => markSvg(s, 'paused') },
  // Not running: the plain mark, its dot unlit (white).
  { file: 'shortcut-idle.ico', sizes: APP_SIZES, svg: (s) => markSvg(s, 'brand') },
];

/** Render SVG strings to exact-size transparent PNGs with Edge. */
export async function renderPngs(jobs) {
  const { createRequire } = await import('node:module');
  const require = createRequire(resolve(shell, 'package.json'));
  const { chromium } = require('playwright-core');
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 512, height: 512 }, deviceScaleFactor: 1 });
  // Rasterise in-page on a canvas of the exact pixel size: one round trip for the whole set.
  const encoded = await page.evaluate(async (list) => {
    const out = [];
    for (const { svg, size } of list) {
      const img = new Image();
      img.src = 'data:image/svg+xml;base64,' + btoa(unescape(encodeURIComponent(svg)));
      await img.decode();
      const canvas = document.createElement('canvas');
      canvas.width = size;
      canvas.height = size;
      canvas.getContext('2d').drawImage(img, 0, 0, size, size);
      out.push(canvas.toDataURL('image/png').split(',')[1]);
    }
    return out;
  }, jobs.map(({ svg, size }) => ({ svg, size })));
  await browser.close();
  return encoded.map((b64) => Buffer.from(b64, 'base64'));
}

/**
 * Re-encode an RGBA8 PNG with per-row adaptive filtering and maximum deflate
 * (browser encoders favour speed). Fully transparent pixels get zero RGB.
 */
export async function optimizePng(buf) {
  const zlib = await import('node:zlib');
  let pos = 8, ihdr = null;
  const idat = [];
  while (pos + 8 <= buf.length) {
    const len = buf.readUInt32BE(pos), type = buf.toString('latin1', pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === 'IHDR') ihdr = data; else if (type === 'IDAT') idat.push(data);
    pos += 12 + len;
  }
  if (!ihdr || ihdr[8] !== 8 || ihdr[9] !== 6 || ihdr[12] !== 0) return buf;
  const w = ihdr.readUInt32BE(0), h = ihdr.readUInt32BE(4), stride = w * 4;
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const paeth = (a, b, c) => { const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c); return pa <= pb && pa <= pc ? a : pb <= pc ? b : c; };
  const px = Buffer.alloc(h * stride);
  for (let y = 0; y < h; y++) {
    const type = raw[y * (stride + 1)], src = y * (stride + 1) + 1, dst = y * stride;
    for (let x = 0; x < stride; x++) {
      const a = x >= 4 ? px[dst + x - 4] : 0, b = y ? px[dst - stride + x] : 0, c = x >= 4 && y ? px[dst - stride + x - 4] : 0;
      const v = raw[src + x];
      px[dst + x] = (type === 1 ? v + a : type === 2 ? v + b : type === 3 ? v + ((a + b) >> 1) : type === 4 ? v + paeth(a, b, c) : v) & 255;
    }
  }
  for (let i = 0; i < px.length; i += 4) if (px[i + 3] === 0) px[i] = px[i + 1] = px[i + 2] = 0;
  const filtered = Buffer.alloc(h * (stride + 1));
  const row = Buffer.alloc(stride);
  for (let y = 0; y < h; y++) {
    let best = -1, bestScore = Infinity;
    for (let type = 0; type < 5; type++) {
      let score = 0;
      for (let x = 0; x < stride; x++) {
        const i = y * stride + x;
        const a = x >= 4 ? px[i - 4] : 0, b = y ? px[i - stride] : 0, c = x >= 4 && y ? px[i - stride - 4] : 0;
        const v = (px[i] - (type === 1 ? a : type === 2 ? b : type === 3 ? (a + b) >> 1 : type === 4 ? paeth(a, b, c) : 0)) & 255;
        row[x] = v;
        score += v < 128 ? v : 256 - v;
      }
      if (score < bestScore) { bestScore = score; best = type; row.copy(filtered, y * (stride + 1) + 1); }
    }
    filtered[y * (stride + 1)] = best;
  }
  const deflated = [zlib.constants.Z_DEFAULT_STRATEGY, zlib.constants.Z_FILTERED]
    .map((strategy) => zlib.deflateSync(filtered, { level: 9, memLevel: 9, strategy }))
    .reduce((x, y) => (y.length < x.length ? y : x));
  const chunk = (type, data) => {
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const body = Buffer.concat([Buffer.from(type, 'latin1'), data]);
    const crc = Buffer.alloc(4); crc.writeUInt32BE(zlib.crc32(body) >>> 0);
    return Buffer.concat([len, body, crc]);
  };
  const out = Buffer.concat([buf.subarray(0, 8), chunk('IHDR', ihdr), chunk('IDAT', deflated), chunk('IEND', Buffer.alloc(0))]);
  return out.length < buf.length ? out : buf;
}

async function main() {
  const previewOnly = process.argv.includes('--preview-only');
  const iconsDir = resolve(shell, 'src-tauri', 'icons');
  const pngDir = resolve(brandDir, 'png');
  const svgDir = resolve(brandDir, 'svg');
  for (const d of [pngDir, svgDir]) mkdirSync(d, { recursive: true });

  const jobs = ICONS.flatMap((icon) => icon.sizes.map((size) => ({ icon, size, svg: icon.svg(size) })));
  const pngs = await renderPngs(jobs);
  for (const [i, job] of jobs.entries()) {
    job.png = await optimizePng(pngs[i]);
    writeFileSync(resolve(pngDir, `${job.icon.file.replace('.ico', '')}-${job.size}.png`), job.png);
  }
  for (const icon of ICONS) {
    const entries = jobs.filter((j) => j.icon === icon).map(({ size, png }) => ({ size, png }));
    if (!previewOnly) writeFileSync(resolve(iconsDir, icon.file), packIco(entries));
    console.log(`${icon.file.padEnd(22)} ${entries.map((e) => e.size).join(',')}`);
  }
  // Reference SVGs (master size) for design review and other tools.
  for (const dot of ['brand', ...Object.keys(palette.dots)]) writeFileSync(resolve(svgDir, `mark-${dot}.svg`), markSvg(256, dot));
  writeFileSync(resolve(svgDir, 'mark-brand-16.svg'), markSvg(16, 'brand'));
  if (!previewOnly) writeFileSync(resolve(shell, 'public', 'logo.svg'), logoSvg());
  console.log(previewOnly ? 'preview PNGs written' : 'icons, logo and previews written');
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  await main();
}
