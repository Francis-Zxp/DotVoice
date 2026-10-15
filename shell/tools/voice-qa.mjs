// Focused regression checks; mock events only, never mic/HID or real input.
import assert from 'node:assert/strict';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium } from 'playwright-core';
import { preview } from 'vite';

const root = resolve(import.meta.dirname, '..');
const out = resolve(root, '../design/qa-0.9.6');
mkdirSync(out, { recursive: true });
const server = await preview({ root, preview: { port: 5176, strictPort: true } });
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const results = [];
const check = async (name, fn) => {
  console.log('CHECK ' + name);
  try { await fn(); results.push({ name, pass: true }); }
  catch (error) { results.push({ name, pass: false, error: error.message }); }
};
try {
  const page = await browser.newPage({ viewport: { width: 1120, height: 760 }, colorScheme: 'dark', reducedMotion: 'reduce' });
  page.setDefaultTimeout(5000);
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto('http://localhost:5176/');
  await page.getByRole('button', { name: '启用语音', exact: true }).waitFor();
  const scroller = page.getByRole('region', { name: '语音转写正文' });
  const gap = () => scroller.evaluate((e) => e.scrollHeight - e.clientHeight - e.scrollTop);
  const partial = async (text) => {
    // An accurate stream (OpenLess / cloud): local preview words would stay hidden by default.
    await page.evaluate((text) => window.__demo.emit('partial', { session: 'qa', text, final: false, source: 'remote' }), text);
    await page.waitForTimeout(100);
  };
  await check('glass selection works under both OS palettes', async () => {
    await page.locator('.nav-item', { hasText: '设置' }).click();
    await page.getByRole('button', { name: '液态玻璃', exact: true }).click();
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'glass');
    for (const colorScheme of ['light', 'dark']) {
      await page.emulateMedia({ colorScheme });
      assert.equal(await page.locator('html').getAttribute('data-theme'), 'glass');
      assert.equal(await page.locator('html').evaluate((e) => getComputedStyle(e).colorScheme), 'dark');
    }
    await page.screenshot({ path: resolve(out, 'settings-glass.png') });
    await page.locator('.nav-item', { hasText: '语音' }).click();
    await page.waitForTimeout(400);
    await page.screenshot({ path: resolve(out, 'voice-glass-preview.png') });
  });
  await page.locator('.nav-item', { hasText: '语音' }).click();
  await scroller.waitFor();
  const long = ('首先我们讨论整体需求，保留已有功能，让长文本跟随最新内容。这个方案需要经过真实使用验证。\n').repeat(75);
  await check('3000+ streaming characters remain at the bottom', async () => {
    assert.ok(long.length > 3000);
    await page.evaluate(() => { window.__demo.state('starting', ''); window.__demo.state('recording', ''); });
    for (let n = 200; n < long.length; n += 200) {
      await partial(long.slice(0, n));
      assert.ok(await gap() < 3, `tail gap ${await gap()} at ${n}`);
    }
    await partial(long + '这是最新的一句话。');
    assert.ok(await gap() < 3);
    await page.screenshot({ path: resolve(out, 'long-listening-tail.png') });
  });
  await check('manual review holds position and return-to-latest resumes', async () => {
    await scroller.hover(); await page.mouse.wheel(0, -900); await page.waitForTimeout(200);
    const top = await scroller.evaluate((e) => e.scrollTop);
    assert.ok(await gap() > 300);
    await partial(long + '这是最新的一句话。继续补充下一句话。');
    assert.ok(Math.abs(await scroller.evaluate((e) => e.scrollTop) - top) < 3);
    await page.getByRole('button', { name: '回到最新' }).click();
    assert.ok(await gap() < 3);
    await partial(long + '继续输入，恢复自动跟随。');
    assert.ok(await gap() < 3);
  });
  await check('resize and final result card leave latest text visible', async () => {
    await page.setViewportSize({ width: 940, height: 680 }); await page.waitForTimeout(250);
    assert.ok(await gap() < 3);
    await page.evaluate((text) => {
      window.__demo.state('processing', '');
      window.__demo.emit('result', { text, destination: 'input', envelope: [], seconds: .5 });
      window.__demo.state('idle', '');
    }, long + '最终句尾，不能丢失。');
    await page.waitForTimeout(1300);
    assert.ok(await gap() < 3);
    assert.ok(await scroller.locator('p').innerText().then((t) => t.endsWith('最终句尾，不能丢失。')));
  });
  await check('new session resets manual-review mode', async () => {
    await scroller.hover(); await page.mouse.wheel(0, -700); await page.waitForTimeout(200);
    await page.evaluate(() => window.__demo.state('starting', ''));
    await page.waitForTimeout(100);
    await page.evaluate(() => window.__demo.state('recording', ''));
    await partial(long);
    assert.ok(await gap() < 3);
  });
  await check('finished text retains the original unadorned answer style', async () => {
    await page.setViewportSize({ width: 1120, height: 760 });
    await page.evaluate(() => {
      window.__demo.emit('result', { text: '💡 修复自动滚动。\n不要自动发送，必须保留原始文字。\n使用 TypeScript，目标延迟 150 ms。', destination: 'input', envelope: [.04,.09,.03], seconds: .5 });
      window.__demo.state('idle', '');
    });
    await page.waitForTimeout(1300);
    assert.equal(await scroller.locator('p').innerText(), '💡 修复自动滚动。\n不要自动发送，必须保留原始文字。\n使用 TypeScript，目标延迟 150 ms。');
    assert.equal(await scroller.locator('.score-emphasis').count(), 0);
    assert.ok(await scroller.locator('p > span').evaluateAll((spans) => spans.every((s) => getComputedStyle(s).textDecorationLine === 'none')));
    assert.equal(await scroller.locator('.score-bracket').count(), 0);
    await page.screenshot({ path: resolve(out, 'finished-original-style.png') });
  });
  await check('processing brackets keep original styling and intact negative phrases', async () => {
    await partial('💡 不要自动发送，必须保留原始文字。使用 TypeScript 完成界面，目标延迟 150 ms。');
    await page.evaluate(() => window.__demo.state('processing', ''));
    await page.waitForTimeout(700);
    const panel = await scroller.boundingBox();
    const phrase = await scroller.locator('p > span').evaluateAll((spans) => spans.filter((s) => s.style.color === 'var(--text)').map((s) => s.textContent).join(''));
    assert.ok(phrase.includes('不要自动发送'));
    assert.ok(!phrase.includes('💡'));
    assert.ok(await scroller.locator('.score-bracket').count() > 0);
    for (const bracket of await scroller.locator('.score-bracket').all()) {
      const b = await bracket.boundingBox();
      assert.ok(b.x >= panel.x && b.x + b.width <= panel.x + panel.width);
    }
    const label = scroller.locator('.score-bracket > div').first();
    assert.equal(await label.evaluate((e) => e.style.color), 'var(--mode, var(--brand))');
    assert.equal(await label.evaluate((e) => getComputedStyle(e).fontSize), '10.2px');
    await page.screenshot({ path: resolve(out, 'thinking-phrases.png') });
  });
  await check('ordinary dark theme has an opaque stable canvas', async () => {
    await page.locator('.nav-item', { hasText: '设置' }).click();
    await page.getByRole('button', { name: '深色', exact: true }).click();
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark');
    // --window in the dark tokens (styles.css): #0e0f14, fully opaque.
    assert.equal(await page.locator('.window').evaluate((e) => getComputedStyle(e).backgroundColor), 'rgb(14, 15, 20)');
  });
  await check('no rendering exceptions', async () => assert.deepEqual(errors, []));
} finally {
  await browser.close(); await new Promise((r) => server.httpServer.close(r));
}
writeFileSync(resolve(out, 'checks.json'), JSON.stringify(results, null, 2));
for (const r of results) console.log(`${r.pass ? 'PASS' : 'FAIL'} ${r.name}${r.error ? ': ' + r.error : ''}`);
process.exit(results.every((r) => r.pass) ? 0 : 1);
