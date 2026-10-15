// Update notifications in the real React UI. Local mock only: no downloads, installs or devices.
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { chromium } from 'playwright-core';
import { createServer } from 'vite';

const root = resolve(import.meta.dirname, '..');
const server = await createServer({ root, server: { port: 5183, strictPort: true }, logLevel: 'error' });
await server.listen();
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const page = await browser.newPage({ viewport: { width: 1200, height: 800 }, reducedMotion: 'reduce' });
page.setDefaultTimeout(5000);
const errors = [];
const results = [];
page.on('pageerror', error => errors.push(error.message));

async function check(name, test) {
  try { await test(); results.push({ name, pass: true }); }
  catch (error) { results.push({ name, pass: false, error: error.message }); }
}
const update = async (version, extra = {}) => page.evaluate(({ version, extra }) => {
  const data = {
    status: 'available', current_version: window.__demo.snap.version, latest_version: version,
    download_url: `https://github.com/Francis-Zxp/DotVoice/releases/download/v${version}/DotVoice-${version}-windows-x64.zip`,
    release_url: `https://github.com/Francis-Zxp/DotVoice/releases/tag/v${version}`,
    last_checked: Date.now() / 1000, error: null, available: true, dismissed: false, dismissed_version: null, ...extra,
  };
  window.__demo.snap.app_update = data;
  window.__demo.emit('app_update', data);
}, { version, extra });

try {
  await page.route('http://localhost:5183/', async route => {
    const response = await route.fetch();
    const body = (await response.text()).replace('<script type="module" src="/src/main.tsx"></script>', `<script type="module">
      import { createMock } from '/src/lib/mock.ts';
      const mock = createMock();
      const calls = window.__updateQA = [];
      window.__DOTVOICE_MOCK__ = { ...mock, async call(method, params) {
        calls.push({ method, params });
        if (method === 'open_url') return { ok: true };
        const result = await mock.call(method, params);
        if (window.__delayUpdateReply === method) {
          await new Promise(resolve => { window.__releaseUpdateReply = resolve; });
          window.__releaseUpdateReply = null;
          window.__delayUpdateReply = null;
        }
        return result;
      }};
      await import('/src/main.tsx');
    </script>`);
    await route.fulfill({ response, body });
  });
  await page.goto('http://localhost:5183/');
  await page.getByRole('button', { name: '启用语音', exact: true }).waitFor();
  const banner = page.locator('[data-app-update]');

  await check('available updates automatically show the release and exact download links', async () => {
    await update('0.11.6');
    await banner.waitFor();
    assert.match(await banner.innerText(), /0\.11\.6 已发布/);
    await banner.getByRole('button', { name: '下载更新', exact: true }).click();
    await banner.getByRole('button', { name: '更新说明', exact: true }).click();
    assert.deepEqual(await page.evaluate(() => window.__updateQA.filter(c => c.method === 'open_url').map(c => c.params.url)), [
      'https://github.com/Francis-Zxp/DotVoice/releases/download/v0.11.6/DotVoice-0.11.6-windows-x64.zip',
      'https://github.com/Francis-Zxp/DotVoice/releases/tag/v0.11.6',
    ]);
  });
  await check('starting, recording and processing hide the banner without interrupting the session', async () => {
    await page.evaluate(() => { window.__updateQA.length = 0; });
    for (const phase of ['starting', 'recording', 'processing']) {
      await page.evaluate(phase => window.__demo.state(phase, ''), phase);
      await banner.waitFor({ state: 'detached' });
      await update('0.11.6');
      assert.equal(await banner.count(), 0);
      assert.equal(await page.evaluate(() => window.__demo.snap.state.phase), phase);
    }
    assert.equal(await page.evaluate(() => window.__updateQA.filter(c => c.method === 'action').length), 0);
    await page.evaluate(() => window.__demo.state('idle', ''));
    await banner.waitFor();
  });
  await check('later hides only the banner while Settings keeps the available download', async () => {
    await banner.getByRole('button', { name: '稍后', exact: true }).click();
    await banner.waitFor({ state: 'detached' });
    await page.locator('.nav-item', { hasText: '设置' }).click();
    const card = page.locator('#set-general [data-update-settings]');
    await card.waitFor();
    assert.match(await card.innerText(), /发现新版本 0\.11\.6/);
    await page.evaluate(() => { window.__updateQA.length = 0; });
    await card.getByRole('button', { name: '下载更新', exact: true }).click();
    assert.equal(await page.evaluate(() => window.__updateQA.find(c => c.method === 'open_url')?.params.url),
      'https://github.com/Francis-Zxp/DotVoice/releases/download/v0.11.6/DotVoice-0.11.6-windows-x64.zip');
    assert.equal(await banner.count(), 0);
  });
  await check('manual check failures report the error instead of claiming the newest version', async () => {
    const card = page.locator('[data-update-settings]');
    await page.evaluate(() => {
      window.__demo.snap.app_update = { ...window.__demo.snap.app_update, status: 'error', available: false,
        error: '无法连接 GitHub，请稍后重试。' };
      window.__updateQA.length = 0;
    });
    await card.getByRole('button', { name: '检查更新', exact: true }).click();
    await card.getByText('无法连接 GitHub，请稍后重试。', { exact: true }).waitFor();
    assert.ok(!(await card.innerText()).includes('当前已是最新正式版'));
    assert.ok(await card.getByRole('button', { name: '检查更新', exact: true }).isEnabled());
    assert.deepEqual(await page.evaluate(() => window.__updateQA.find(c => c.method === 'check_updates')?.params), { force: true });
  });
  await check('a higher release reappears even after the earlier version was dismissed', async () => {
    await update('0.11.7', { dismissed_version: '0.11.6' });
    await banner.waitFor();
    assert.match(await banner.innerText(), /0\.11\.7 已发布/);
    assert.match(await page.locator('[data-update-settings]').innerText(), /发现新版本 0\.11\.7/);
    await banner.getByRole('button', { name: '下载更新', exact: true }).click();
    assert.equal(await page.evaluate(() => window.__updateQA.filter(c => c.method === 'open_url').at(-1)?.params.url),
      'https://github.com/Francis-Zxp/DotVoice/releases/download/v0.11.7/DotVoice-0.11.7-windows-x64.zip');
  });
  await check('late check and dismiss replies never overwrite a newer release event', async () => {
    for (const [method, version] of [['check_updates', '0.11.8'], ['dismiss_update', '0.11.9']]) {
      await page.evaluate(method => { window.__delayUpdateReply = method; }, method);
      if (method === 'check_updates') {
        await page.locator('[data-update-settings]').getByRole('button', { name: '检查更新', exact: true }).click();
      } else {
        await banner.getByRole('button', { name: '稍后', exact: true }).click();
      }
      await page.waitForFunction(() => typeof window.__releaseUpdateReply === 'function');
      await update(version);
      await banner.waitFor();
      await page.evaluate(async () => {
        window.__releaseUpdateReply();
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      });
      const card = page.locator('[data-update-settings]');
      await card.getByRole('button', { name: '检查更新', exact: true }).waitFor();
      assert.ok((await banner.innerText()).includes(`${version} 已发布`));
      assert.ok((await card.innerText()).includes(`发现新版本 ${version}`));
    }
  });
  await check('no rendering exceptions', async () => assert.deepEqual(errors, []));
} finally {
  await browser.close();
  await server.close();
}
for (const result of results) console.log(`${result.pass ? 'PASS' : 'FAIL'} ${result.name}${result.error ? '\n' + result.error : ''}`);
process.exit(results.every(result => result.pass) ? 0 : 1);
