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
  const scope = page.getByRole('combobox', { name: '胶囊显示范围' });
  const select = page.getByRole('combobox', { name: '胶囊显示器' });
  await scope.waitFor();
  await page.waitForFunction(() => !document.querySelector('[aria-label="胶囊显示范围"]')?.disabled);
  await check('default follows the primary screen and offers one/all-screen scope', async () => {
    assert.equal(await scope.inputValue(), 'primary');
    assert.deepEqual(await scope.locator('option').allTextContents(), ['跟随主显示器（默认）', '指定一块屏幕', '所有屏幕同时显示']);
    assert.equal(await page.getByRole('button', { name: '顶部居中', exact: true }).getAttribute('aria-pressed'), 'true');
    assert.equal(await page.getByRole('switch', { name: /本机预览|极速落字/ }).count(), 0);
    await page.getByText('豆包和百炼支持边说边显示。', { exact: false }).waitFor();
  });
  await check('selecting a screen stores its identity and clears all previous manual positions', async () => {
    await page.evaluate(() => {
      window.__demo.snap.prefs.overlay.position = { x: 500, y: 90 };
      window.__demo.snap.prefs.overlay.positions = { old: { x: 100, y: 80 } };
      window.__demo.emit('prefs', window.__demo.snap.prefs);
    });
    await scope.selectOption('one');
    await select.selectOption(secondary);
    await page.waitForFunction(name => window.__demo.snap.prefs.overlay.monitor === name, secondary);
    assert.deepEqual(await select.locator('option').allTextContents(), ['显示器 1 · 主屏 · 3840×2160', '显示器 2 · 2560×1600']);
    assert.deepEqual(await page.evaluate(() => [window.__demo.snap.prefs.overlay.position, window.__demo.snap.prefs.overlay.positions]), [null, {}]);
  });
  await check('unplug and reconnect retain the selected screen and explain the fallback', async () => {
    await page.evaluate(() => { window.__removedMonitor = window.__demo.monitors.pop(); });
    await page.getByRole('button', { name: '刷新显示器', exact: true }).click();
    await page.getByText('所选屏幕未连接，暂用主屏；接回后自动恢复。', { exact: false }).waitFor();
    assert.equal(await select.inputValue(), secondary);
    assert.ok((await select.locator('option:checked').innerText()).includes('未连接'));
    await page.evaluate(() => window.__demo.monitors.push(window.__removedMonitor));
    await page.getByRole('button', { name: '刷新显示器', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('[aria-label="胶囊显示器"] option:checked')?.textContent?.includes('未连接'));
    assert.equal(await select.inputValue(), secondary);
  });
  await check('all-screen scope and all nine anchors persist and clear dragged positions', async () => {
    await scope.selectOption('all');
    await page.waitForFunction(() => window.__demo.snap.prefs.overlay.all_monitors === true);
    assert.equal(await select.count(), 0);
    const anchors = [ ['左上角','top-left'], ['顶部居中','top-center'], ['右上角','top-right'], ['左侧居中','center-left'], ['屏幕中央','center'], ['右侧居中','center-right'], ['左下角','bottom-left'], ['底部居中','bottom-center'], ['右下角','bottom-right'] ];
    for (const [label, value] of anchors) {
      await page.evaluate(() => { window.__demo.snap.prefs.overlay.positions = { old: { x: 20, y: 10 } }; });
      await page.getByRole('button', { name: label, exact: true }).click();
      await page.waitForFunction(anchor => window.__demo.snap.prefs.overlay.anchor === anchor, value);
      assert.deepEqual(await page.evaluate(() => window.__demo.snap.prefs.overlay.positions), {});
      assert.equal(await page.getByRole('button', { name: label, exact: true }).getAttribute('aria-pressed'), 'true');
    }
  });
  await check('reset removes dragging while retaining the chosen screen scope and anchor', async () => {
    await page.evaluate(() => {
      window.__demo.snap.prefs.overlay.position = { x: -1600, y: 80 };
      window.__demo.snap.prefs.overlay.positions = { old: { x: 30, y: 80 } };
      window.__demo.emit('prefs', window.__demo.snap.prefs);
    });
    await page.getByRole('button', { name: '恢复预设位置', exact: true }).click();
    assert.deepEqual(await page.evaluate(() => ({ all: window.__demo.snap.prefs.overlay.all_monitors, anchor: window.__demo.snap.prefs.overlay.anchor,
      position: window.__demo.snap.prefs.overlay.position, positions: window.__demo.snap.prefs.overlay.positions })),
      { all: true, anchor: 'bottom-right', position: null, positions: {} });
    await scope.selectOption('primary');
    await page.waitForFunction(() => window.__demo.snap.prefs.overlay.monitor === null && !window.__demo.snap.prefs.overlay.all_monitors);
  });
  await check('unnamed monitors are labelled but cannot be selected individually', async () => {
    await page.evaluate(() => window.__demo.monitors.push({ name: null, primary: false, width: 1920, height: 1080, scale_factor: 1 }));
    await page.getByRole('button', { name: '刷新显示器', exact: true }).click();
    await scope.selectOption('one');
    const unnamed = select.locator('option', { hasText: '无名称显示器' });
    assert.ok(await unnamed.isDisabled());
  });
  await check('screen controls fit the compact settings layout without rendering exceptions', async () => {
    await page.setViewportSize({ width: 880, height: 620 });
    for (const control of [scope, select, page.getByRole('group', { name: '胶囊预设位置' })]) {
      await control.scrollIntoViewIfNeeded();
      const bounds = await control.boundingBox();
      assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= 880);
    }
    assert.deepEqual(errors, []);
  });
} finally {
  await browser.close();
  await server.close();
}
for (const result of results) console.log(`${result.pass ? 'PASS' : 'FAIL'} ${result.name}${result.error ? '\n' + result.error : ''}`);
process.exit(results.every(result => result.pass) ? 0 : 1);
