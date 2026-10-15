// Slow-motion frame capture of the capsule (browser mock): the page clock runs
// at 1/10 speed so springs can be inspected frame by frame.
// Usage: node tools/motion-frames.mjs OUT_DIR
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium } from 'playwright-core';
import { preview } from 'vite';

const out = resolve(process.argv[2] ?? '../design/frames-0.9');
mkdirSync(out, { recursive: true });
const SLOW = 10;
const server = await preview({ root: process.cwd(), preview: { port: 5178, strictPort: true } });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: 440, height: 560 } });
page.on('pageerror', (e) => console.log('PAGEERROR', e.message));
await page.goto(`http://localhost:5178/overlay.html`);
await page.waitForTimeout(700);
const cdp = await page.context().newCDPSession(page);
await cdp.send('Animation.enable');
await page.evaluate((slow) => {
  const realNow = performance.now.bind(performance);
  const t0 = realNow();
  const scale = (t) => t0 + (t - t0) / slow;
  performance.now = () => scale(realNow());
  const raf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = (cb) => raf((t) => cb(scale(t)));
  const timeout = window.setTimeout.bind(window);
  window.setTimeout = (fn, ms = 0, ...a) => timeout(fn, ms * slow, ...a);
}, SLOW);
await cdp.send('Animation.setPlaybackRate', { playbackRate: 1 / SLOW });

async function burst(name, action, frames, everyMs, height) {
  await page.evaluate(action);
  const start = Date.now();
  for (let i = 0; i < frames; i++) {
    const due = start + i * everyMs * SLOW;
    const wait = due - Date.now();
    if (wait > 0) await page.waitForTimeout(wait);
    await page.screenshot({ path: resolve(out, `${name}-${String(Math.round(i * everyMs)).padStart(4, '0')}.png`), clip: { x: 0, y: 0, width: 440, height } });
  }
}
await burst('appear', () => window.__demo.setMode('notes', 'key'), 12, 40, 110);
await page.waitForTimeout(400 * SLOW / 4);
await burst('switch', () => window.__demo.setMode('present', 'key'), 12, 40, 110);
await page.waitForTimeout(2600 * SLOW);
await page.evaluate(() => window.__demo.play('notes'));
await page.waitForTimeout(1500 * SLOW); // hidden → appear note during "starting/recording"
await burst('morph', () => {}, 12, 50, 240);
await browser.close();
server.httpServer.close();
