// Tile PNG frames (prefix-NN.png) into one sheet for reviewing motion.
// Usage: node tools/contact-sheet.mjs DIR PREFIX [COLUMNS] [WIDTH]
import { readdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright-core';

const [dirArg, prefix, colsArg = '2', widthArg = '440'] = process.argv.slice(2);
const dir = resolve(dirArg);
const cols = Number(colsArg), width = Number(widthArg);
const files = readdirSync(dir).filter((f) => f.startsWith(prefix + '-') && f.endsWith('.png')).sort();
const cells = files.map((f) => `<div style="position:relative"><img src="${pathToFileURL(resolve(dir, f)).href}" style="display:block;width:${width}px">`
  + `<span style="position:absolute;right:4px;bottom:2px;color:#fff;font:12px sans-serif;background:#000a;padding:1px 4px">${f.slice(prefix.length + 1, -4)}</span></div>`);
const html = resolve(dir, `${prefix}.sheet.html`);
writeFileSync(html, `<body style="margin:0;background:#222;display:grid;grid-template-columns:repeat(${cols},${width}px);gap:4px">${cells.join('')}</body>`);
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: cols * (width + 4), height: 300 } });
await page.goto(pathToFileURL(html).href);
await page.waitForTimeout(300);
await page.screenshot({ path: resolve(dir, `sheet-${prefix}.png`), fullPage: true });
await browser.close();
console.log(resolve(dir, `sheet-${prefix}.png`));
