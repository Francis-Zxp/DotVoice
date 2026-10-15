// Screen-selection UI regression with the browser's simulated monitors; no desktop or device changes.
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { chromium } from 'playwright-core';
import { createServer } from 'vite';

const root = resolve(import.meta.dirname, '..');
const server = await createServer({ root, server: { port: 5184, strictPort: true }, logLevel: 'error' });
await server.listen();
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: 1200, height: 800 }, reducedMotion: 'reduce' });
page.setDefaultTimeout(5000);
const errors = [];
page.on('pageerror', error => errors.push(error.message));
const results = [];
async function check(name, test) {
  try { await test(); results.push({ name, pass: true }); }
  catch (error) { results.push({ name, pass: false, error: error.message }); }
}
const secondary = String.raw`\\.\DISPLAY2`;
try {
  await page.goto('http://localhost:5184/');
  await page.locator('.nav-item', { hasText: '设置' }).click();
  const select = page.getByRole('combobox', { name: '胶囊显示器' });
  await select.waitFor();
  await page.waitForFunction(() => !document.querySelector('[aria-label="胶囊显示器"]')?.disabled);
  await check('default follows the primary screen and available screens have readable physical dimensions', async () => {
    assert.equal(await select.inputValue(), '');
    assert.deepEqual(await select.locator('option').allTextContents(), [
      '跟随主显示器（默认）', '显示器 1 · 主屏 · 3840×2160', '显示器 2 · 2560×1600',
    ]);
  });
  await check('selecting a monitor stores its identity and clears the prior manual position', async () => {
    await page.evaluate(() => {
      window.__demo.snap.prefs.overlay.position = { x: 500, y: 90 };
      window.__demo.emit('prefs', window.__demo.snap.prefs);
    });
    await select.selectOption(secondary);
    await page.waitForFunction(name => window.__demo.snap.prefs.overlay.monitor === name, secondary);
    assert.equal(await page.evaluate(() => window.__demo.snap.prefs.overlay.position), null);
    assert.equal(await select.inputValue(), secondary);
  });
  await check('refreshing after unplug keeps the absent selection with a primary-screen fallback explanation', async () => {
    await page.evaluate(() => { window.__removedMonitor = window.__demo.monitors.pop(); });
    await page.getByRole('button', { name: '刷新显示器', exact: true }).click();
    await page.getByText('所选屏幕未连接，暂用主屏；接回后自动恢复。也可拖动便签调整位置。', { exact: true }).waitFor();
    assert.equal(await select.inputValue(), secondary);
    assert.equal(await page.evaluate(() => window.__demo.snap.prefs.overlay.monitor), secondary);
    assert.ok((await select.locator('option:checked').innerText()).includes('未连接'));
  });
  await check('refreshing after reconnect restores the selected option without clearing its preference', async () => {
    await page.evaluate(() => window.__demo.monitors.push(window.__removedMonitor));
    await page.getByRole('button', { name: '刷新显示器', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('[aria-label="胶囊显示器"] option:checked')?.textContent?.includes('未连接'));
    assert.equal(await select.inputValue(), secondary);
    assert.equal(await page.evaluate(() => window.__demo.snap.prefs.overlay.monitor), secondary);
  });
  await check('reset removes a manual drag but retains the selected monitor', async () => {
    await page.evaluate(() => {
      window.__demo.snap.prefs.overlay.position = { x: -1600, y: 80 };
      window.__demo.emit('prefs', window.__demo.snap.prefs);
    });
    await page.getByRole('button', { name: '回到顶部居中', exact: true }).click();
    assert.deepEqual(await page.evaluate(() => ({ monitor: window.__demo.snap.prefs.overlay.monitor,
      position: window.__demo.snap.prefs.overlay.position })), { monitor: secondary, position: null });
  });
  await check('returning to follow-primary clears the selection and unnamed monitors cannot be selected', async () => {
    await select.selectOption('');
    await page.waitForFunction(() => window.__demo.snap.prefs.overlay.monitor === null);
    await page.evaluate(() => window.__demo.monitors.push({ name: null, primary: false, width: 1920, height: 1080, scale_factor: 1 }));
    await page.getByRole('button', { name: '刷新显示器', exact: true }).click();
    const unnamed = select.locator('option', { hasText: '无名称显示器' });
    await unnamed.waitFor({ state: 'attached' });
    assert.ok(await unnamed.isDisabled());
    assert.equal(await select.inputValue(), '');
  });
  await check('screen controls fit the compact settings layout without rendering exceptions', async () => {
    await page.setViewportSize({ width: 880, height: 620 });
    await select.scrollIntoViewIfNeeded();
    const bounds = await select.boundingBox();
    assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 880);
    assert.deepEqual(errors, []);
  });
} finally {
  await browser.close();
  await server.close();
}
for (const result of results) console.log(`${result.pass ? 'PASS' : 'FAIL'} ${result.name}${result.error ? '\n' + result.error : ''}`);
process.exit(results.every(result => result.pass) ? 0 : 1);
