// UI QA against the built frontend with the browser mock core.
// No microphone, receiver, OpenLess or external pages are touched.
// Env: DOTVOICE_DIST (built folder, default shell/dist), DOTVOICE_QA_PORT (default 5175),
//      DOTVOICE_QA_SHOTS (screenshot folder, default design/qa-0.8).
import assert from 'node:assert/strict';
import { mkdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { chromium } from 'playwright-core';
import { preview } from 'vite';

const root = resolve(import.meta.dirname, '..');
const dist = resolve(process.env.DOTVOICE_DIST || resolve(root, 'dist'));
const port = Number(process.env.DOTVOICE_QA_PORT || 5175);
const shots = resolve(process.env.DOTVOICE_QA_SHOTS || resolve(root, '..', 'design', 'qa-0.8'));
mkdirSync(shots, { recursive: true });
const server = await preview({ root, build: { outDir: dist }, preview: { port, strictPort: true }, logLevel: 'error' });
const base = `http://localhost:${port}`;
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const results = [];
const check = async (name, fn) => {
  try { await fn(); results.push(['PASS', name]); } catch (error) { results.push(['FAIL', name, error.message.split('\n')[0]]); }
};
const reducedPrefs = `{mode:'input',category:'idea',sounds:{enabled:true,volume:'medium'},overlay:{enabled:true,on_switch:true,linger_seconds:30,position:null},preview:{show_local_text:false},airmouse:{enabled:true,skip_slideshow:true},hotkeys:{toggle_mode:'',arm_toggle:''},appearance:{theme:'system',reduced_motion:true}}`;
// Mid-recording in the mock's demo session (recording from 2.1 s, words every 110 ms from 2.4 s).
const MID_RECORDING_MS = 4200;
const PAGES = [['语音', 'voice'], ['笔记', 'notes'], ['设备', 'devices'], ['续航', 'battery'], ['设置', 'settings']];
const go = async (page, label) => {
  await page.locator('.nav-item', { hasText: label }).click();
  await page.waitForFunction((l) => document.querySelector('.nav-item[aria-current="page"]')?.textContent?.includes(l), label);
  await page.waitForTimeout(350);
};
// The brand colour as the page resolves it, under the OS palette (theme null) or a
// forced data-theme: the swatch's --brand and a probe .btn-primary's gradient.
const brandOf = (page, theme) => page.evaluate((theme) => {
  const root = document.documentElement, was = root.dataset.theme;
  if (theme) root.dataset.theme = theme;
  const swatch = document.createElement('i');
  swatch.style.color = 'var(--brand)';
  const button = document.createElement('button');
  button.className = 'btn btn-primary';
  document.body.append(swatch, button);
  const out = { brand: getComputedStyle(swatch).color, button: getComputedStyle(button).backgroundImage };
  swatch.remove(); button.remove();
  if (theme) { if (was === undefined) delete root.dataset.theme; else root.dataset.theme = was; }
  return out;
}, theme);
// The logo's indigo (hue ~245, strongly saturated), not the 0.7 rose (#d23a68).
const isIndigo = (rgb) => {
  const [r, g, b] = rgb.match(/[\d.]+/g).map(Number);
  const max = Math.max(r, g, b), min = Math.min(r, g, b);
  const hue = max === b && max > min ? 240 + (60 * (r - g)) / (max - min) : -1;
  return hue >= 235 && hue <= 255 && (max - min) / max > 0.6;
};
// WCAG contrast from rendered pixels: the median pixel of an element's box with its text and
// glyphs made transparent is the background; the foreground is its computed colour (alpha
// composited over that background) or the given colour.
const luminance = (rgb) => {
  const lin = (c) => { c /= 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * lin(rgb[0]) + 0.7152 * lin(rgb[1]) + 0.0722 * lin(rgb[2]);
};
const contrast = (a, b) => { const x = luminance(a), y = luminance(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
const contrastOf = async (page, locator, fg) => {
  const color = fg ?? (await locator.evaluate((e) => getComputedStyle(e).color)).match(/[\d.]+/g).map(Number);
  // Hide the text, keeping each node's own inline colour (React set some) to put back after.
  await locator.evaluate((e) => [e, ...e.querySelectorAll('*')].forEach((n) => {
    n.dataset.qaColor = n.style.getPropertyValue('color');
    n.style.setProperty('color', 'transparent', 'important');
  }));
  await page.mouse.move(0, 0);
  await page.waitForTimeout(400);
  const png = await locator.screenshot({ animations: 'disabled' });
  await locator.evaluate((e) => [e, ...e.querySelectorAll('*')].forEach((n) => {
    n.style.removeProperty('color');
    if (n.dataset.qaColor) n.style.setProperty('color', n.dataset.qaColor);
    delete n.dataset.qaColor;
  }));
  const bg = await page.evaluate(async (b64) => {
    const img = new Image();
    img.src = 'data:image/png;base64,' + b64;
    await img.decode();
    const canvas = Object.assign(document.createElement('canvas'), { width: img.width, height: img.height });
    const ctx = canvas.getContext('2d');
    ctx.drawImage(img, 0, 0);
    const d = ctx.getImageData(0, 0, img.width, img.height).data, px = [];
    for (let i = 0; i < d.length; i += 4) px.push([d[i], d[i + 1], d[i + 2]]);
    px.sort((a, b) => a[0] + a[1] + a[2] - (b[0] + b[1] + b[2]));
    return px[px.length >> 1];
  }, png.toString('base64'));
  const a = color.length > 3 ? color[3] : 1;
  return contrast(color.slice(0, 3).map((v, i) => v * a + bg[i] * (1 - a)), bg);
};

try {
  for (const scheme of ['light', 'dark']) {
    for (const [width, height] of [[1200, 800], [880, 620]]) {
      const page = await browser.newPage({ viewport: { width, height }, colorScheme: scheme, reducedMotion: 'reduce' });
      const errors = [];
      page.on('pageerror', (e) => errors.push(e.message));
      page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
      await page.goto(base + '/');
      await page.getByRole('button', { name: '启用语音' }).waitFor();
      for (const [label, file] of PAGES) {
        await go(page, label);
        await page.waitForTimeout(450);
        await page.screenshot({ path: resolve(shots, width === 1200 ? `${file}-${scheme}.png` : `${file}-${scheme}-${width}.png`) });
      }
      if (width === 1200) {
        // Every token block: light, [data-theme=dark], the OS dark palette and glass.
        await check(`${scheme}: the brand colour is the logo's indigo in every theme (no 0.7 rose)`, async () => {
          for (const theme of scheme === 'light' ? [null, 'dark'] : [null, 'glass']) {
            const { brand, button } = await brandOf(page, theme);
            assert.ok(isIndigo(brand), `${theme ?? scheme}: --brand is ${brand}`);
            assert.ok(button.includes(brand) && !button.includes('rgb(210, 58, 104)'), `${theme ?? scheme}: .btn-primary is ${button}`);
          }
        });
      }
      await check(`${scheme} ${width}: no console errors across pages`, async () => assert.deepEqual(errors, []));
      await page.close();
    }
  }

  const page = await browser.newPage({ viewport: { width: 1200, height: 800 }, reducedMotion: 'reduce' });
  await page.goto(base + '/');
  await page.getByRole('button', { name: '启用语音' }).waitFor();

  await check('title-bar switch covers all three modes', async () => {
    for (const label of ['演示', '输入', '笔记']) {
      const button = page.locator('header[data-tauri-drag-region] button', { hasText: label });
      await button.click();
      await page.waitForFunction((l) => document.querySelector('header [aria-pressed="true"]')?.textContent?.includes(l), label);
      assert.equal(await button.getAttribute('aria-pressed'), 'true');
    }
  });

  await check('sidebar shows each present pen with its own battery, plus Tidepool (never an absent pen)', async () => {
    const rows = page.locator('.dock [data-dock-row]');
    await page.waitForFunction(() => document.querySelectorAll('.dock [data-dock-row]').length === 3);
    const text = await page.locator('.dock').innerText();
    for (const want of ['倍思 #1', '76%', '倍思 #2', '18%', 'Tidepool', '未上报']) assert.ok(text.includes(want), `missing ${want}`);
    assert.ok(!text.includes('倍思 #3'), 'absent pen listed');
    assert.equal(await rows.count(), 3);
  });

  await check('Tidepool battery says 未上报 and explains why on hover', async () => {
    await page.locator('.dock .dock-na').hover();
    const tip = page.getByRole('tooltip');
    await tip.waitFor({ timeout: 3000 });
    assert.match(await tip.innerText(), /Tidepool 固件 4.0.6 起才把电量发给电脑/);
    await page.mouse.move(600, 420);
    await tip.waitFor({ state: 'detached', timeout: 3000 });
  });

  await check('home lists every device with battery and state', async () => {
    const rows = page.locator('[data-roster]');
    await page.waitForFunction(() => document.querySelectorAll('[data-roster]').length === 4);
    assert.match(await rows.nth(0).innerText(), /倍思 #1[\s\S]*76%/);
    assert.match(await page.locator('[data-roster="baseus:c0ffee01"]').innerText(), /未连接/);
    assert.match(await page.locator('[data-roster="tidepool"]').innerText(), /无线在线[\s\S]*未上报/);
  });

  await check('a newly plugged-in pen appears live from the "instances" event', async () => {
    await page.evaluate(() => {
      const list = window.__demo.snap.instances.map((p) => ({ ...p }));
      list.push({ id: 'baseus:5eed0004', model: 'baseus', label: '倍思 #4', present: true, battery: { percent: 64, reported_at: Date.now() / 1000 }, last_used: null, active: false });
      window.__demo.setInstances(list);
    });
    await page.waitForFunction(() => document.querySelectorAll('.dock [data-dock-row]').length === 4);
    assert.ok((await page.locator('.dock').innerText()).includes('倍思 #4'));
    await page.evaluate(() => window.__demo.setInstances(window.__demo.snap.instances.filter((p) => p.id !== 'baseus:5eed0004')));
    await page.waitForFunction(() => document.querySelectorAll('.dock [data-dock-row]').length === 3);
  });

  await check('enable button arms the (mock) core', async () => {
    await page.getByRole('button', { name: '启用语音' }).click();
    await page.getByRole('button', { name: '暂停语音' }).waitFor({ timeout: 3000 });
  });

  await check('local preview words stay hidden: the voice page shows the voice line, then the final text', async () => {
    await page.evaluate(() => (window).__demo.play('notes'));  // the mock's words come from the local preview
    await page.locator('main [data-wave-only]').waitFor({ timeout: 6000 });
    await page.waitForTimeout(MID_RECORDING_MS - 2100);
    const text = await page.locator('main').innerText();
    assert.ok(text.includes('正在听') && text.includes('说完显示文字'), 'waveform-only state');
    assert.ok(!text.includes('长按翻译键胶囊'), 'local guesses are not shown');
    await page.screenshot({ path: resolve(shots, 'voice-notes-wave.png') });
    await page.waitForFunction(() => document.querySelector('main')?.textContent?.includes('长按翻译键，胶囊就告诉我'), null, { timeout: 15000 });
    assert.equal(await page.locator('main [data-wave-only]').count(), 0);
  });

  await check('accurate live words (OpenLess / cloud) still stream into the voice page', async () => {
    await page.evaluate(() => (window).__demo.play('notes', { source: 'remote' }));
    await page.waitForFunction(() => document.querySelector('main')?.textContent?.includes('长按翻译键胶囊'), null, { timeout: 12000 });
    assert.equal(await page.locator('main [data-wave-only]').count(), 0);
    await page.screenshot({ path: resolve(shots, 'voice-notes-live.png') });
    await page.waitForFunction(() => document.querySelector('main')?.textContent?.includes('长按翻译键，胶囊就告诉我'), null, { timeout: 15000 });
  });

  await check('a new recording after a result never blanks the window (0.8 black screen)', async () => {
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    await page.evaluate(() => (window).__demo.play('input'));
    await page.waitForFunction(() => document.querySelector('main')?.textContent?.includes('读取配置文件，'), null, { timeout: 15000 });
    await page.evaluate(() => (window).__demo.play('notes'));
    await page.waitForTimeout(3500);
    assert.deepEqual(errors, []);
    assert.equal(await page.locator('.nav-item').count(), 5);
  });

  await check('the pen that is recording is marked 正在录音 (home and sidebar)', async () => {
    await page.waitForFunction(() => document.querySelector('[data-roster="baseus:3f9a12c0"]')?.textContent?.includes('正在录音'), null, { timeout: 8000 });
    assert.equal(await page.locator('.dock [data-dock-row="baseus:3f9a12c0"] .dot.live').count(), 1);
    await page.waitForFunction(() => !document.querySelector('[data-roster="baseus:3f9a12c0"]')?.textContent?.includes('正在录音'), null, { timeout: 20000 });
  });

  await check('Ctrl+number switches pages', async () => {
    await page.keyboard.press('Control+2');
    await page.waitForFunction(() => document.querySelector('.nav-item[aria-current="page"]')?.textContent?.includes('笔记'));
    await page.keyboard.press('Control+1');
    await page.waitForFunction(() => document.querySelector('.nav-item[aria-current="page"]')?.textContent?.includes('语音'));
  });

  await check('notes are newest first with exact times, and delete can be undone', async () => {
    // The earlier demo note session saves its note ~0.9 s after recording ends: let it land
    // first, or the count below races it (a pre-existing flake, 2026-10-14).
    await page.waitForFunction(() => ['idle', 'disconnected'].includes(window.__demo.snap.state.phase), null, { timeout: 20000 });
    await page.waitForTimeout(400);
    await go(page, '笔记');
    const items = page.locator('[data-note-id]');
    await items.first().waitFor();
    assert.match(await items.first().innerText(), /\d{2}:\d{2}:\d{2}/);
    const before = await items.count();
    const firstId = await items.first().getAttribute('data-note-id');
    await page.getByRole('button', { name: /删除/ }).click();
    await page.getByRole('button', { name: '撤销' }).waitFor();
    await page.waitForFunction((n) => document.querySelectorAll('[data-note-id]').length === n, before - 1);
    await page.getByRole('button', { name: '撤销' }).click();
    await page.waitForFunction((n) => document.querySelectorAll('[data-note-id]').length === n, before);
    assert.equal(await items.first().getAttribute('data-note-id'), firstId, 'restored note returns to the top');
  });

  await check('tags can be added from the suggestion and filtered', async () => {
    const before = await page.locator('[data-note-id]').count();
    // After the undo the detail pane swaps notes (AnimatePresence mode="wait"): the leaving
    // pane fades out first. On a slow CPU a click on 标签 landed there, and its input was
    // unmounted under the cursor (2026-10-14). Wait for the restored note's pane alone.
    await page.waitForFunction(() => {
      const panes = document.querySelectorAll('[data-note-detail]');
      return panes.length === 1 && panes[0].getAttribute('data-note-detail') === document.querySelector('[data-note-id]')?.getAttribute('data-note-id');
    });
    await page.getByRole('button', { name: '标签', exact: true }).click();
    await page.getByPlaceholder('标签名').fill('试验');
    await page.getByPlaceholder('标签名').press('Enter');
    await page.getByRole('button', { name: /^试验/ }).first().waitFor();
    await page.locator('[role="group"][aria-label="按标签筛选"] button', { hasText: '试验' }).click();
    await page.waitForFunction(() => document.querySelectorAll('[data-note-id]').length === 1);
    await page.screenshot({ path: resolve(shots, 'notes-tag-filter.png') });
    await page.locator('[role="group"][aria-label="按标签筛选"] button', { hasText: '全部' }).click();
    await page.waitForFunction((n) => document.querySelectorAll('[data-note-id]').length === n, before);
  });

  await check('search filters notes, Esc clears it', async () => {
    const before = await page.locator('[data-note-id]').count();
    const box = page.getByRole('textbox', { name: '搜索笔记' });
    await box.fill('对照组');
    await page.waitForFunction(() => document.querySelectorAll('[data-note-id]').length === 1);
    await box.press('Escape');
    await page.waitForFunction((n) => document.querySelectorAll('[data-note-id]').length === n, before);
  });

  await check('day headers stay inside the rounded list panel', async () => {
    const ok = await page.evaluate(() => {
      const list = document.querySelector('section.glass');
      const box = list.getBoundingClientRect();
      return [...list.querySelectorAll('.sticky')].every((h) => { const r = h.getBoundingClientRect(); return r.left >= box.left && r.right <= box.right; });
    });
    assert.equal(ok, true);
  });

  await check('pens can be renamed inline, and the new name shows everywhere', async () => {
    await go(page, '设备');
    await page.getByRole('button', { name: /重命名 倍思 #1/ }).click();
    const input = page.getByRole('textbox', { name: '笔的名称' });
    await input.fill('会议室');
    await input.press('Enter');
    await page.locator('[data-instance="baseus:3f9a12c0"]', { hasText: '会议室' }).waitFor();
    await page.locator('.dock', { hasText: '会议室' }).waitFor();
    // Esc cancels without saving
    await page.getByRole('button', { name: /重命名 倍思 #2/ }).click();
    await page.getByRole('textbox', { name: '笔的名称' }).fill('不要保存');
    await page.getByRole('textbox', { name: '笔的名称' }).press('Escape');
    assert.ok(!(await page.locator('main').innerText()).includes('不要保存'));
    await page.screenshot({ path: resolve(shots, 'devices-renamed.png') });
  });

  await check('present tools can be chosen in settings', async () => {
    await go(page, '设置');
    await page.getByRole('button', { name: /^聚光灯/ }).click();
    await page.waitForFunction(() => [...document.querySelectorAll('button[aria-pressed="true"]')].some((b) => b.textContent.startsWith('聚光灯')));
  });

  await check('settings section bar jumps to a section and follows scrolling', async () => {
    await page.locator('.section-bar button', { hasText: '外观' }).click();
    await page.waitForFunction(() => {
      const main = document.getElementById('main').getBoundingClientRect().top;
      const top = document.getElementById('set-appearance').getBoundingClientRect().top - main;
      return top >= -2 && top < 140;
    }, null, { timeout: 4000 });
    await page.waitForFunction(() => document.querySelector('.section-bar [aria-current="true"]')?.textContent?.includes('外观'));
    await page.evaluate(() => { document.getElementById('main').scrollTop = 0; });
    await page.waitForFunction(() => document.querySelector('.section-bar [aria-current="true"]')?.textContent?.includes('语音引擎'), null, { timeout: 3000 });
  });

  await check('语音引擎 comes first: four engine cards, the running one marked 正在使用', async () => {
    await go(page, '设置');
    assert.equal(await page.evaluate(() => document.querySelector('.settings-section')?.id), 'set-engine');
    assert.equal(await page.locator('#set-engine .engine-card').count(), 4);
    const text = await page.locator('#set-engine').innerText();
    for (const want of ['豆包语音', '推荐', '阿里云百炼', 'OpenLess', '其他语音软件', '正在使用', '检测到 闪电说', '重新运行配置向导']) assert.ok(text.includes(want), `missing ${want}`);
    assert.equal(await page.locator('.engine-card[data-current="true"]').getAttribute('data-engine'), 'openless');
    await page.locator('#set-engine', { hasText: '远程输入已开启' }).waitFor();
    assert.ok((await page.locator('#set-engine').innerText()).includes('局域网监听'), 'OpenLess LAN note');
  });

  await check('豆包语音: a saved key is shown masked only, 测试连接 answers inline, then it becomes the engine', async () => {
    const raw = 'vk-qa-RAW-0123456789abcdef';
    await page.locator('.engine-card[data-engine="volc"]').click();
    const use = page.getByRole('button', { name: '使用豆包语音' });
    await use.waitFor();
    assert.equal(await use.isDisabled(), true, 'no key yet: cannot switch');
    assert.ok((await page.locator('#set-engine').innerText()).includes('先保存 API Key'));
    await page.getByLabel('豆包 API Key').fill(raw);
    await page.locator('[data-secret="volc.api_key"]').getByRole('button', { name: '保存' }).click();
    await page.locator('[data-secret="volc.api_key"] .secret-mask', { hasText: 'cdef' }).waitFor();
    assert.ok(!(await page.content()).includes(raw), 'the typed key left the page');
    await page.locator('[data-test="volc"] .btn').click();
    await page.locator('[data-test="volc"] .test-result[data-tone="ok"]', { hasText: '豆包语音连接正常' }).waitFor({ timeout: 4000 });
    await use.click();
    await page.locator('.engine-card[data-engine="volc"][data-current="true"]').waitFor();
    await page.locator('.dock [data-engine-status]', { hasText: '豆包语音' }).waitFor();
    assert.equal(await page.evaluate(() => window.__demo.snap.prefs.engine.kind), 'volc');
    assert.ok((await page.locator('#set-engine .section-title').innerText()).includes('当前：豆包语音'), 'section hint names it');
  });

  await check('阿里云百炼 and 润色: model / region switches, custom polish shows URL, model, key and prompt', async () => {
    await page.locator('.engine-card[data-engine="dashscope"]').click();
    await page.locator('[role="group"][aria-label="百炼地域"]').getByRole('button', { name: '新加坡' }).click();
    await page.waitForFunction(() => window.__demo.snap.prefs.engine.dashscope.region === 'intl');
    await page.locator('[role="group"][aria-label="百炼模型"]').getByRole('button', { name: 'Paraformer v2' }).click();
    await page.waitForFunction(() => window.__demo.snap.prefs.engine.dashscope.model === 'paraformer-realtime-v2');
    const polish = page.locator('[role="group"][aria-label="润色方式"]');
    await polish.getByRole('button', { name: '自定义' }).click();
    for (const label of ['润色 Base URL', '润色模型', '润色 API Key', '自定义润色提示词']) await page.getByLabel(label).waitFor();
    // Half filled in, the voice still works (typed as recognised): said inline, never a blocked engine.
    const incomplete = page.locator('[data-polish-incomplete]');
    assert.match(await incomplete.innerText(), /还缺 Base URL、模型.*按识别原文输入/);
    await page.getByLabel('润色 Base URL').fill('http://api.example.com/v1');
    await page.getByLabel('润色 Base URL').press('Enter');
    await page.locator('.toast', { hasText: 'Base URL' }).waitFor({ timeout: 3000 });
    assert.equal(await page.getByLabel('润色 Base URL').inputValue(), '', 'a refused value is reverted');
    await page.getByLabel('润色 Base URL').fill('https://api.example.com/v1');
    await page.getByLabel('润色 Base URL').press('Enter');
    await page.waitForFunction(() => window.__demo.snap.prefs.engine.polish.base_url === 'https://api.example.com/v1');
    await page.waitForFunction(() => !document.querySelector('[data-polish-incomplete]')?.textContent?.includes('Base URL'));
    assert.match(await incomplete.innerText(), /还缺 模型/);
    await page.screenshot({ path: resolve(shots, 'polish-incomplete.png') });
    await polish.getByRole('button', { name: '原文' }).click();
    await page.getByLabel('润色 Base URL').waitFor({ state: 'detached' });
  });

  await check('其他语音软件: detected apps first, the pen microphone in the hint, warning chips, a typed shortcut', async () => {
    await page.locator('.engine-card[data-engine="hotkey"]').click();
    const tiles = page.locator('.app-tile');
    await tiles.first().waitFor();
    assert.equal(await tiles.first().getAttribute('data-profile'), 'shandianshuo', 'the running app is first');
    assert.match(await tiles.first().innerText(), /闪电说[\s\S]*运行中/);
    assert.match(await page.locator('[data-profile="wechat"]').innerText(), /版本过旧/);
    assert.match(await page.locator('[data-mic-hint]').innerText(), /BaseusPresent/);
    for (const id of ['admin', 'syllable', 'ctrlwin']) await page.locator(`.warn-chip[data-warn="${id}"]`).waitFor();
    await page.locator('#set-engine').getByRole('button', { name: '手动输入' }).click();
    await page.getByLabel('手动输入快捷键').fill('ctrl+alt+space');
    await page.getByLabel('手动输入快捷键').press('Enter');
    await page.waitForFunction(() => window.__demo.snap.prefs.engine.hotkey.keys === 'Ctrl + Alt + 空格');
    await page.locator('#set-engine .keycaps', { hasText: '空格' }).waitFor();
    await page.locator('#set-engine').getByRole('button', { name: '恢复默认快捷键' }).click();
    await page.waitForFunction(() => window.__demo.snap.prefs.engine.hotkey.keys === '');
    await page.getByRole('button', { name: '使用闪电说' }).click();
    await page.locator('.dock [data-engine-status]', { hasText: '闪电说' }).waitFor();
    // Back to OpenLess for the rest of the run.
    await page.locator('.engine-card[data-engine="openless"]').click();
    await page.getByRole('button', { name: '使用 OpenLess' }).click();
    await page.locator('.engine-card[data-engine="openless"][data-current="true"]').waitFor();
  });

  // The Doubao key is saved and OpenLess runs: the hint shows. Its button text is AA and the
  // current engine's white glyph keeps 3:1 on its tile in every theme (a light text shade of
  // the OpenLess violet once filled the dark tile: white on pale lavender, 2.4:1).
  await check('Doubao hint button and the current engine icon stay legible in light, dark and glass', async () => {
    const hint = page.locator('[data-engine-hint="volc"]');
    await hint.waitFor();
    const button = hint.getByRole('button', { name: '使用豆包语音' });
    const tile = page.locator('.engine-card[data-engine="openless"][data-current="true"] .engine-ico');
    const was = await page.evaluate(() => document.documentElement.dataset.theme ?? null);
    const found = [];
    try {
      for (const theme of ['light', 'dark', 'glass']) {
        await page.evaluate((t) => { document.documentElement.dataset.theme = t; }, theme);
        await page.waitForTimeout(800);  // colours and backgrounds transition (.2–.3 s)
        const text = await contrastOf(page, button);
        const glyph = await contrastOf(page, tile, [255, 255, 255]);
        found.push(`${theme} ${text.toFixed(2)}/${glyph.toFixed(2)}`);
        assert.ok(text >= 4.5, `${theme}: 使用豆包语音 is ${text.toFixed(2)}:1 (needs 4.5)`);
        assert.ok(glyph >= 3, `${theme}: the white OpenLess glyph is ${glyph.toFixed(2)}:1 (needs 3)`);
      }
    } finally {
      await page.evaluate((t) => { if (t === null) delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t; }, was);
    }
    console.log(`  contrast text/glyph: ${found.join(', ')}`);
  });

  await check('new preferences are wired: 气声识别, 随拿随用, 拦截放映快捷键, 桌面图标状态点, 显示本机预览文字, 调试录音', async () => {
    const prefs = () => page.evaluate(() => window.__demo.snap.prefs);
    const whisper = page.locator('[role="group"][aria-label="气声识别"]');
    await whisper.getByRole('button', { name: '始终' }).click();
    await page.waitForFunction(() => window.__demo.snap.prefs.voice.whisper === 'on');
    assert.equal(await whisper.getByRole('button', { name: '始终' }).getAttribute('aria-pressed'), 'true');
    assert.equal(await page.getByRole('switch', { name: '显示本机预览文字' }).isChecked(), false, 'local preview words are off by default');
    assert.equal(await page.getByRole('switch', { name: '实时预览文字' }).count(), 0, 'one live-text switch, not two');
    for (const [label, read] of [['随拿随用', (p) => p.devices.coexist], ['拦截笔的放映快捷键', (p) => p.pen.guard_shortcuts], ['桌面图标实时状态点', (p) => p.appearance.live_shortcut],
      ['显示本机预览文字', (p) => p.preview.show_local_text], ['显示本机预览文字', (p) => p.preview.show_local_text]]) {
      const before = read(await prefs());
      await page.getByRole('switch', { name: label }).click();
      await page.waitForTimeout(200);
      assert.equal(read(await prefs()), !before, label);
      assert.equal(await page.getByRole('switch', { name: label }).isChecked(), !before, label + ' (checked state)');
    }
    await whisper.getByRole('button', { name: '自动' }).click();
    // Debug recordings: off by default; on, the folder can be opened (the core never uploads them).
    const diag = page.getByRole('switch', { name: '保存最近 3 段录音用于调试' });
    assert.equal(await diag.isChecked(), false, 'debug recordings are off by default');
    assert.ok((await page.locator('#set-voice').innerText()).includes('仅本机，24 小时后自动删除'));
    assert.equal(await page.locator('#set-voice').getByRole('button', { name: '打开文件夹' }).count(), 0);
    await diag.click();
    await page.waitForFunction(() => window.__demo.snap.prefs.diagnostics.save_audio === true);
    await page.locator('#set-voice').getByRole('button', { name: '打开文件夹' }).click();
    await page.locator('#set-voice').screenshot({ path: resolve(shots, 'settings-voice-diagnostics.png') });
    await diag.click();
    await page.waitForFunction(() => window.__demo.snap.prefs.diagnostics.save_audio === false);
  });

  await check('兼容管理员权限窗口: off by default, explained, and only the switch turns it on (mock UAC + restart)', async () => {
    await go(page, '设置');
    const section = page.locator('#set-general');
    const toggle = page.getByRole('switch', { name: '兼容管理员权限窗口' });
    await section.getByText('当前：普通权限').waitFor({ timeout: 3000 });
    assert.equal(await toggle.isChecked(), false, 'never on by itself');
    assert.equal(await page.evaluate(() => window.__demo.snap.prefs.general.run_elevated), false);
    const text = await section.innerText();
    for (const want of ['兼容管理员权限窗口', '模拟点击、滚动和文字输入', 'UAC', '计划任务', '以后登录不再弹窗', '自动重启', '代价']) assert.ok(text.includes(want), `missing ${want}`);
    await section.screenshot({ path: resolve(shots, 'settings-general-elevation-off.png') });
    await toggle.click();
    await page.locator('.toast', { hasText: '正在以管理员身份重启' }).waitFor({ timeout: 3000 });
    await page.waitForFunction(() => window.__demo.snap.prefs.general.run_elevated === true);
    await section.getByText('当前：管理员权限').waitFor({ timeout: 3000 });
    assert.equal(await toggle.isChecked(), true);
    await section.screenshot({ path: resolve(shots, 'settings-general-elevation-on.png') });
    await toggle.click();
    await page.locator('.toast', { hasText: '正在以普通权限重启' }).waitFor({ timeout: 3000 });
    await page.waitForFunction(() => window.__demo.snap.prefs.general.run_elevated === false);
    assert.equal(await toggle.isChecked(), false);
    await page.evaluate(() => { window.__demo.elevation.elevated = false; });
  });

  await check('an administrator window that drops input says so once, and refused text is kept with a copy action', async () => {
    await page.evaluate(() => window.__demo.blocked());
    await page.locator('.toast', { hasText: '该窗口以管理员权限运行，系统拦截了点声的模拟点击/输入。可在 设置 → 通用 → 兼容管理员权限窗口 开启' }).waitFor({ timeout: 3000 });
    await page.evaluate(() => window.__demo.refused());
    const toast = page.locator('.toast', { hasText: '文字没能输入' });
    await toast.waitFor({ timeout: 3000 });
    assert.ok((await toast.innerText()).includes('已复制到剪贴板，按 Ctrl+V 粘贴'));
    await toast.getByRole('button', { name: '复制' }).waitFor();
    await page.screenshot({ path: resolve(shots, 'elevated-toasts.png') });
    await go(page, '语音');
    await page.locator('main', { hasText: '没能输入到管理员窗口' }).first().waitFor({ timeout: 3000 });
    assert.ok(!(await page.locator('main').innerText()).includes('已输入到光标处'), 'refused text never reads as typed');
    await page.evaluate(() => document.querySelectorAll('.toast button[aria-label="关闭提示"]').forEach((b) => b.click()));
  });

  await check('air-mouse settings: ④ long press and ⑤ + volume come with the measured codes and can be recorded again', async () => {
    await go(page, '设置');
    const section = page.locator('#set-airmouse');
    const text = await section.innerText();
    for (const want of ['单击 ⑤ = 左键', '③④ 短按始终翻页', '③ 长按 = 抓起', '④ 长按 = 右键', '6D', '按住 ⑤ + 音量 + = 上滚', '78', '79', '放映时保持原样']) assert.ok(text.includes(want), `missing ${want}`);
    for (const gone of ['松开飞鼠键后保持', '保持时间', '确认键 ①', '键码还没有实测', '键盘上的音量键也会滚动']) assert.ok(!text.includes(gone), `still shows ${gone}`);
    const row = (slot) => section.locator(`[data-mouse-row="${slot}"]`);
    // Record ⑤ + volume + (the mock answers with 78 after one press); ⑤ may be held meanwhile.
    await row('airmouse_wheel_up').getByRole('button', { name: '录制' }).click();
    const dialog = page.getByRole('dialog', { name: '录制按键' });
    await dialog.getByText('在笔上按一下这个键').waitFor();
    assert.ok((await dialog.innerText()).includes('⑤ 本身不会被录下'));
    await dialog.waitFor({ state: 'detached', timeout: 5000 });
    await row('airmouse_wheel_up').filter({ hasText: '自定义 78' }).waitFor();
    await row('airmouse_wheel_up').getByRole('button', { name: '恢复默认' }).click();
    await row('airmouse_wheel_up').filter({ hasText: '音量 + ⑨' }).waitFor();
    // ④'s long press: record it again, clear it, and bring the default back.
    await row('right_click').getByRole('button', { name: '录制' }).click();
    await dialog.getByText('长按这个键约 1 秒再松开').waitFor();
    await dialog.waitFor({ state: 'detached', timeout: 5000 });
    await row('right_click').filter({ hasText: '自定义 6D' }).waitFor();
    await section.screenshot({ path: resolve(shots, 'settings-airmouse.png') });
    await row('right_click').getByRole('button', { name: '解除绑定' }).click();
    await row('right_click').filter({ hasText: '未绑定' }).waitFor();
    await row('right_click').getByRole('button', { name: '恢复默认' }).click();
    await row('right_click').filter({ hasText: '长按下翻页 ④' }).waitFor();
  });

  await check('key learning dialog shows captured codes and cancels', async () => {
    await go(page, '设置');
    await page.getByRole('button', { name: '录制' }).first().click();
    const dialog = page.getByRole('dialog', { name: '录制按键' });
    await dialog.waitFor();
    await dialog.getByText('7B').waitFor({ timeout: 4000 });
    await dialog.getByRole('button', { name: '取消' }).click();
    await dialog.waitFor({ state: 'detached' });
  });

  await check('every visible button has hover/press feedback (pointer + transition)', async () => {
    const bad = [];
    for (const [label] of PAGES) {
      await go(page, label);
      bad.push(...await page.evaluate((l) => [...document.querySelectorAll('button')].filter((b) => {
        const r = b.getBoundingClientRect();
        if (!r.width || !r.height || b.disabled || b.closest('[aria-hidden="true"]')) return false;
        const s = getComputedStyle(b);
        const moving = s.transitionDuration.split(',').some((d) => parseFloat(d) > 0);
        return s.cursor !== 'pointer' || !moving;
      }).map((b) => `${l}: ${(b.getAttribute('aria-label') || b.textContent || b.className).trim().slice(0, 24)}`), label));
    }
    assert.deepEqual(bad, []);
  });

  await check('narrow window has no horizontal overflow', async () => {
    await page.setViewportSize({ width: 880, height: 620 });
    for (const [label] of PAGES) {
      await go(page, label);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth || document.querySelector('main').scrollWidth > document.querySelector('main').clientWidth + 1
        || document.querySelector('.titlebar').getBoundingClientRect().right > innerWidth + 1);
      assert.equal(overflow, false, label);
    }
    await page.screenshot({ path: resolve(shots, 'narrow-settings.png') });
  });
  await page.close();

  // Settings → 语音引擎, each engine's form (tall window so the whole section is in one shot).
  for (const scheme of ['light', 'dark']) {
    const tall = await browser.newPage({ viewport: { width: 1200, height: 1500 }, colorScheme: scheme, reducedMotion: 'reduce' });
    await tall.goto(base + '/');
    await tall.getByRole('button', { name: '启用语音' }).waitFor();
    await go(tall, '设置');
    await check(`${scheme}: engine section screenshots`, async () => {
      for (const kind of ['volc', 'dashscope', 'openless', 'hotkey']) {
        await tall.locator(`.engine-card[data-engine="${kind}"]`).click();
        await tall.waitForTimeout(500);
        if (kind === 'dashscope') await tall.locator('[role="group"][aria-label="润色方式"]').getByRole('button', { name: '轻度润色' }).click();
        if (kind === 'hotkey') await tall.locator('.app-tile').first().waitFor();
        await tall.evaluate(() => { const main = document.getElementById('main'); main.scrollTop = document.getElementById('set-engine').offsetTop - 60; });
        await tall.waitForTimeout(250);
        await tall.screenshot({ path: resolve(shots, `settings-engine-${kind}-${scheme}.png`) });
      }
    });
    await tall.close();
  }

  // First-run setup wizard (a fresh install: ?wizard=1 leaves prefs.setup.done false).
  for (const scheme of ['light', 'dark']) {
    const wiz = await browser.newPage({ viewport: { width: 1200, height: 800 }, colorScheme: scheme, reducedMotion: 'reduce' });
    const errors = [];
    wiz.on('pageerror', (e) => errors.push(e.message));
    wiz.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
    await wiz.goto(base + '/?wizard=1');
    const dialog = wiz.getByRole('dialog', { name: '配置向导' });
    await check(`${scheme}: a fresh install opens the wizard with four step dots`, async () => {
      await dialog.waitFor({ timeout: 5000 });
      assert.equal(await wiz.locator('.wizard .step').count(), 4);
      assert.equal(await wiz.locator('.wizard .step[data-state="current"]').innerText(), '设备');
    });
    await check(`${scheme}: step 1 设备 — receiver, a live microphone level and the ⑥ key test tick`, async () => {
      await wiz.locator('.check-row', { hasText: '接收器' }).locator('.check-ico[data-ok="true"]').waitFor();
      assert.match(await wiz.locator('.check-row', { hasText: '接收器' }).innerText(), /倍思 BS-OH085 · 2 支笔在线/);
      await wiz.waitForFunction(() => {
        const bar = document.querySelector('.meter > i');
        return bar && new DOMMatrix(getComputedStyle(bar).transform).a > 0.15;
      }, null, { timeout: 5000 });
      await wiz.locator('.check-row', { hasText: '麦克风' }).locator('.check-ico[data-ok="true"]').waitFor({ timeout: 5000 });
      assert.equal(await wiz.locator('.check-row', { hasText: '录音键' }).locator('.check-ico[data-ok="true"]').count(), 0);
      await wiz.evaluate(() => window.__demo.key('start'));
      await wiz.locator('.check-row', { hasText: '录音键' }).locator('.check-ico[data-ok="true"]').waitFor();
      await wiz.waitForTimeout(350);
      await wiz.waitForTimeout(500); // let the step's blur-in finish
      await wiz.screenshot({ path: resolve(shots, `wizard-1-device-${scheme}.png`) });
    });
    if (scheme === 'light') {
      await check(`${scheme}: hidden or in the background, the wizard hands back test mode and the microphone`, async () => {
        await wiz.waitForFunction(() => window.__demo.leases().testing && window.__demo.leases().meter);
        await wiz.evaluate(() => { document.hasFocus = () => false; window.dispatchEvent(new Event('blur')); });
        await wiz.waitForFunction(() => !window.__demo.leases().testing && !window.__demo.leases().meter, null, { timeout: 2000 });
        await wiz.evaluate(() => { delete document.hasFocus; window.dispatchEvent(new Event('focus')); });
        await wiz.waitForFunction(() => window.__demo.leases().testing && window.__demo.leases().meter, null, { timeout: 2000 });
      });
    }
    await check(`${scheme}: step 2 语音引擎 — Doubao recommended with inline key and test; alternatives listed`, async () => {
      await dialog.getByRole('button', { name: /下一步/ }).click();
      await wiz.locator('.wizard .step[data-state="current"]', { hasText: '语音引擎' }).waitFor();
      const volc = wiz.locator('.choice[data-choice="volc"]');
      assert.equal(await volc.getAttribute('data-on'), 'true', 'Doubao preselected');
      await wiz.locator('.choice[data-choice="hotkey"]').first().waitFor(); // detected apps arrive a moment later
      const text = await wiz.locator('.wizard').innerText();
      for (const want of ['推荐', '阿里云百炼', 'OpenLess', '闪电说', '跳过这一步']) assert.ok(text.includes(want), `missing ${want}`);
      const use = dialog.getByRole('button', { name: /使用豆包语音/ });
      assert.equal(await use.isDisabled(), true);
      await wiz.waitForTimeout(500); // let the step's blur-in finish
      await wiz.screenshot({ path: resolve(shots, `wizard-2-engine-${scheme}.png`) });
      await wiz.getByLabel('豆包 API Key').fill('vk-wizard-0123456789abcd');
      await volc.getByRole('button', { name: '保存' }).click();
      await volc.locator('.secret-mask', { hasText: 'abcd' }).waitFor();
      assert.ok(!(await wiz.content()).includes('vk-wizard-0123456789abcd'), 'only the masked key stays on the page');
      await volc.locator('[data-test="volc"] .btn').click();
      await volc.locator('.test-result[data-tone="ok"]').waitFor({ timeout: 4000 });
      await wiz.waitForTimeout(500); // let the step's blur-in finish
      await wiz.screenshot({ path: resolve(shots, `wizard-2-engine-ready-${scheme}.png`) });
      await use.click();
      await wiz.locator('.wizard .step[data-state="current"]', { hasText: '试一句' }).waitFor();
      assert.equal(await wiz.evaluate(() => window.__demo.snap.prefs.engine.kind), 'volc');
    });
    await check(`${scheme}: step 3 试一句 — the test result shows in the wizard only`, async () => {
      await wiz.locator('.try-stage', { hasText: '语音输入已暂停' }).waitFor();
      await wiz.waitForTimeout(500); // let the step's blur-in finish
      await wiz.screenshot({ path: resolve(shots, `wizard-3-paused-${scheme}.png`) });
      await dialog.getByRole('button', { name: '启用语音' }).click();
      await wiz.locator('.try-stage', { hasText: '按住 ⑥ 说一句话' }).waitFor();
      await wiz.evaluate(() => window.__demo.speak('今天下午三点在三楼会议室开会。'));
      await wiz.locator('.try-stage', { hasText: '正在听' }).waitFor({ timeout: 4000 });
      await wiz.waitForTimeout(600);
      await wiz.waitForTimeout(500); // let the step's blur-in finish
      await wiz.screenshot({ path: resolve(shots, `wizard-3-listening-${scheme}.png`) });
      await wiz.locator('[data-wizard-result]', { hasText: '三楼会议室' }).waitFor({ timeout: 6000 });
      assert.match(await wiz.locator('[data-wizard-result]').innerText(), /豆包语音识别成功/);
      await wiz.waitForTimeout(500); // let the step's blur-in finish
      await wiz.screenshot({ path: resolve(shots, `wizard-3-result-${scheme}.png`) });
    });
    await check(`${scheme}: step 4 完成 — the key cheat sheet, then 开始使用 closes it for good`, async () => {
      await dialog.getByRole('button', { name: /下一步/ }).click();
      await wiz.locator('[data-cheat]').waitFor();
      const text = await wiz.locator('[data-cheat]').innerText();
      for (const want of ['⑥', '⑦', '⑤', '③', '④', '演示模式', '右键', '闪念笔记']) assert.ok(text.includes(want), `missing ${want}`);
      await wiz.waitForTimeout(500);
      await wiz.waitForTimeout(500); // let the step's blur-in finish
      await wiz.screenshot({ path: resolve(shots, `wizard-4-done-${scheme}.png`) });
      await dialog.getByRole('button', { name: '开始使用' }).click();
      await dialog.waitFor({ state: 'detached' });
      assert.equal(await wiz.evaluate(() => window.__demo.snap.prefs.setup.done), true);
      assert.ok(!(await wiz.locator('main').innerText()).includes('三楼会议室'), 'the test sentence never reached the voice page');
    });
    await check(`${scheme}: the wizard can be run again from settings, and every button there has feedback`, async () => {
      await go(wiz, '设置');
      await wiz.getByRole('button', { name: '重新运行配置向导' }).click();
      await dialog.waitFor();
      const bad = await wiz.evaluate(() => [...document.querySelectorAll('.wizard button')].filter((b) => {
        const r = b.getBoundingClientRect();
        if (!r.width || !r.height || b.disabled) return false;
        const s = getComputedStyle(b);
        return s.cursor !== 'pointer' || !s.transitionDuration.split(',').some((d) => parseFloat(d) > 0);
      }).map((b) => (b.getAttribute('aria-label') || b.textContent || '').trim().slice(0, 20)));
      assert.deepEqual(bad, []);
      await dialog.getByRole('button', { name: '稍后再说' }).click();
      await dialog.waitFor({ state: 'detached' });
      assert.deepEqual(errors, []);
    });
    if (scheme === 'light') {
      await check(`${scheme}: Tab stays inside the wizard and Esc never throws away a pasted key without asking`, async () => {
        await wiz.getByRole('button', { name: '重新运行配置向导' }).click();
        await dialog.waitFor();
        for (let i = 0; i < 24; i++) await wiz.keyboard.press(i % 5 === 4 ? 'Shift+Tab' : 'Tab');
        assert.ok(await wiz.evaluate(() => document.querySelector('.wizard').contains(document.activeElement)), 'focus left the dialog');
        await dialog.getByRole('button', { name: /下一步/ }).click();
        const volc = wiz.locator('.choice[data-choice="volc"]');
        await volc.getByRole('button', { name: '更换' }).click();
        await dialog.getByLabel('豆包 API Key').fill('vk-unsaved-0123456789');
        await dialog.locator('.wizard-head h2').click();   // focus out of the field
        await wiz.keyboard.press('Escape');
        await dialog.locator('.wizard-confirm').waitFor();
        assert.equal(await dialog.getByLabel('豆包 API Key').inputValue(), 'vk-unsaved-0123456789', 'the pasted key is still there');
        await wiz.waitForTimeout(400);
        await wiz.screenshot({ path: resolve(shots, `wizard-unsaved-key-${scheme}.png`) });
        await dialog.getByRole('button', { name: '继续填写' }).click();
        await dialog.locator('.wizard-confirm').waitFor({ state: 'detached' });
        await dialog.getByRole('button', { name: '稍后再说' }).click();
        await dialog.getByRole('button', { name: '丢弃并关闭' }).click();
        await dialog.waitFor({ state: 'detached' });
        assert.deepEqual(errors, []);
      });
    }
    await wiz.close();
  }
  const narrowWizard = await browser.newPage({ viewport: { width: 880, height: 620 }, reducedMotion: 'reduce' });
  await narrowWizard.goto(base + '/?wizard=1');
  await check('narrow window: the wizard fits without horizontal overflow', async () => {
    await narrowWizard.getByRole('dialog', { name: '配置向导' }).waitFor();
    for (let i = 0; i < 4; i++) {
      const overflow = await narrowWizard.evaluate(() => {
        const box = document.querySelector('.wizard').getBoundingClientRect();
        const body = document.querySelector('.wizard-body');
        return box.right > innerWidth + 1 || box.bottom > innerHeight + 1 || body.scrollWidth > body.clientWidth + 1;
      });
      assert.equal(overflow, false, `step ${i + 1}`);
      if (i === 1) await narrowWizard.screenshot({ path: resolve(shots, 'wizard-2-engine-880.png') });
      if (i === 1) await narrowWizard.getByRole('button', { name: '跳过这一步' }).click();
      else if (i < 3) await narrowWizard.getByRole('dialog').getByRole('button', { name: /下一步/ }).click();
      await narrowWizard.waitForTimeout(400);
    }
  });
  await narrowWizard.close();

  const standard = await browser.newPage({ viewport: { width: 1200, height: 800 }, reducedMotion: 'reduce' });
  await standard.goto(base + '/?admin=0');
  await standard.getByRole('button', { name: '启用语音' }).waitFor();
  await check('a standard Windows account cannot turn 兼容管理员权限窗口 on, and is told why', async () => {
    await go(standard, '设置');
    const toggle = standard.getByRole('switch', { name: '兼容管理员权限窗口' });
    await standard.locator('#set-general [data-elevation-note]', { hasText: '当前 Windows 账户不是管理员，无法开启。' }).waitFor({ timeout: 3000 });
    assert.equal(await toggle.isDisabled(), true);
    assert.equal(await toggle.isChecked(), false);
  });
  await standard.close();

  const broken = await browser.newPage({ viewport: { width: 1200, height: 800 }, reducedMotion: 'reduce' });
  await broken.goto(base + '/?prefs=broken');
  await broken.getByRole('button', { name: '启用语音' }).waitFor();
  await check('unreadable settings are said in settings, not silently replaced by defaults', async () => {
    await go(broken, '设置');
    await broken.locator('[data-settings-warnings] .banner', { hasText: 'prefs.json 已损坏' }).waitFor({ timeout: 3000 });
    await broken.screenshot({ path: resolve(shots, 'settings-prefs-warning.png') });
  });
  await broken.close();

  const empty = await browser.newPage({ viewport: { width: 1200, height: 800 }, reducedMotion: 'reduce' });
  await empty.goto(base + '/?devices=none');
  await empty.getByRole('button', { name: '启用语音' }).waitFor();
  await check('no receiver: sidebar shows no device rows and the devices page explains how to add pens', async () => {
    assert.equal(await empty.locator('.dock [data-dock-row]').count(), 0);
    await go(empty, '设备');
    await empty.getByText('插入接收器后，每支笔会单独出现在这里').waitFor();
    await empty.screenshot({ path: resolve(shots, 'devices-empty.png') });
  });
  await empty.close();

  const overlay = await browser.newPage({ viewport: { width: 500, height: 580 }, reducedMotion: 'reduce' });
  await overlay.goto(base + '/overlay.html');
  await overlay.evaluate(`window.__demo.emit('prefs', ${reducedPrefs})`);
  await check('capsule shows the target mode on every switch', async () => {
    for (const [mode, label] of [['notes', '笔记'], ['present', '演示'], ['input', '输入']]) {
      await overlay.evaluate((m) => (window).__demo.setMode(m, 'key'), mode);
      await overlay.locator('.capsule', { hasText: label }).waitFor();
      await overlay.waitForTimeout(300);
      await overlay.screenshot({ path: resolve(shots, `capsule-chip-${mode}.png`) });
    }
    await overlay.evaluate(() => (window).__demo.setTool('magnifier'));
    await overlay.locator('.capsule', { hasText: '放大镜' }).waitFor();
  });
  await check('capsule: an administrator window that dropped a click, then text it refused (kept on the clipboard)', async () => {
    const fits = () => overlay.evaluate(() => [...document.querySelectorAll('.capsule .lg-hint, .capsule .lg-title')]
      .every((el) => el.scrollWidth <= el.clientWidth + 1));
    await overlay.evaluate(() => (window).__demo.blocked());
    const chip = overlay.locator('.capsule', { hasText: '该窗口以管理员权限运行' });
    await chip.waitFor({ timeout: 3000 });
    await overlay.waitForTimeout(600);
    const text = await chip.innerText();
    for (const want of ['系统拦截了点声的模拟点击/输入', '可在 设置 → 通用 → 兼容管理员权限窗口 开启']) assert.ok(text.includes(want), `missing ${want}`);
    assert.equal(await fits(), true, 'the whole sentence is readable (no truncation)');
    const box = await chip.boundingBox();
    assert.ok(box.width <= 440 && box.height < 90, `chip ${box.width}×${box.height}`);
    await overlay.screenshot({ path: resolve(shots, 'capsule-elevated.png'), clip: { x: 0, y: 0, width: 500, height: 110 } });
    await overlay.evaluate(() => (window).__demo.refused());
    const kept = overlay.locator('.capsule', { hasText: '文字没能输入到管理员窗口' });
    await kept.waitFor({ timeout: 3000 });
    await overlay.waitForTimeout(600);
    assert.ok((await kept.innerText()).includes('已复制到剪贴板，按 Ctrl+V 粘贴'));
    assert.ok(!(await kept.innerText()).includes('已输入到光标处'));
    assert.equal(await fits(), true);
    await overlay.screenshot({ path: resolve(shots, 'capsule-kept.png'), clip: { x: 0, y: 0, width: 500, height: 110 } });
    await overlay.locator('.capsule').waitFor({ state: 'detached', timeout: 9000 });
  });
  await check('note session: a waveform-only note (local guesses hidden), then the saved text', async () => {
    await overlay.evaluate(() => (window).__demo.play('notes'));  // the mock's words come from the local preview
    await overlay.locator('.capsule.view-note [data-wave-only]').waitFor({ timeout: 8000 });
    await overlay.waitForTimeout(MID_RECORDING_MS - 1800);
    const text = await overlay.locator('.capsule').innerText();
    assert.ok(text.includes('说完显示文字'), 'says the words come at the end');
    assert.ok(!text.includes('今天想把点声'), 'local guesses are not shown');
    await overlay.screenshot({ path: resolve(shots, 'capsule-note-wave.png') });
    await overlay.locator('.capsule', { hasText: '已保存' }).waitFor({ timeout: 10000 });
    assert.ok((await overlay.locator('.capsule').innerText()).includes('今天想把点声的切换'), 'the saved text shows');
    await overlay.screenshot({ path: resolve(shots, 'capsule-note-saved.png') });
  });
  await check('input session: a compact waveform-only pill that springs wider for the result', async () => {
    await overlay.evaluate(() => (window).__demo.play('input'));
    // The saved note yields to the "输入" chip, then the session is a compact pill with the voice line only.
    await overlay.locator('.capsule.view-live [data-wave-only]').waitFor({ timeout: 8000 });
    await overlay.waitForTimeout(MID_RECORDING_MS - 1800);
    const box = await overlay.locator('.capsule').boundingBox();
    assert.ok(box.height < 80 && box.width < 330, `waveform-only capsule ${box.width}×${box.height}`);
    const text = await overlay.locator('.capsule').innerText();
    assert.ok(text.includes('正在听') && !text.includes('读取配置'), text);
    await overlay.screenshot({ path: resolve(shots, 'capsule-input-wave.png') });
    await overlay.locator('.capsule', { hasText: '已输入到光标处' }).waitFor({ timeout: 10000 });
    await overlay.waitForTimeout(700);
    const done = await overlay.locator('.capsule').boundingBox();
    assert.ok(done.width > 340 && done.height < 80, `result capsule ${done.width}×${done.height}`);
    await overlay.screenshot({ path: resolve(shots, 'capsule-input-done.png') });
  });
  await check('accurate words show in the capsule; the switch reveals local guesses', async () => {
    await overlay.evaluate(() => (window).__demo.play('input', { source: 'remote' }));
    await overlay.waitForFunction(() => document.querySelector('.capsule .live-tail')?.textContent?.includes('读取'), null, { timeout: 12000 });
    assert.equal(await overlay.locator('.capsule [data-wave-only]').count(), 0);
    await overlay.screenshot({ path: resolve(shots, 'capsule-input-live.png') });
    await overlay.locator('.capsule', { hasText: '已输入到光标处' }).waitFor({ timeout: 10000 });
    await overlay.evaluate(`window.__demo.emit('prefs', ${reducedPrefs.replace('show_local_text:false', 'show_local_text:true')})`);
    await overlay.evaluate(() => (window).__demo.play('notes'));
    await overlay.waitForFunction(() => document.querySelector('.capsule')?.textContent?.includes('今天想把点声'), null, { timeout: 12000 });
    assert.ok((await overlay.locator('.capsule').innerText()).includes('本机预览，可能不准'));
    await overlay.screenshot({ path: resolve(shots, 'capsule-note-local.png') });
    await overlay.evaluate(`window.__demo.emit('prefs', ${reducedPrefs})`);
    await overlay.locator('.capsule', { hasText: '已保存' }).waitFor({ timeout: 12000 });
  });

  // Full motion: a hidden card fades out for ~.34 s, and a switch landing inside that brings
  // the same card back. 0.10.6 left its content at opacity 0 then: an empty glass pill.
  const lively = await browser.newPage({ viewport: { width: 500, height: 580 } });
  await lively.goto(base + '/overlay.html');
  await lively.evaluate(`window.__demo.emit('prefs', ${reducedPrefs.replace('reduced_motion:true', 'reduced_motion:false')})`);
  await check('a switch landing while the last chip fades out still shows its orb and title', async () => {
    for (const late of [140, 200, 260]) {
      // The present chip times out after 1.8 s; the next switch lands `late` ms into its fade-out.
      await lively.evaluate((ms) => { const demo = (window).__demo; demo.setMode('present', 'key'); setTimeout(() => demo.setMode('input', 'key'), 1800 + ms); }, late);
      await lively.waitForTimeout(1800 + late + 900);
      const seen = await lively.evaluate(() => {
        const shown = (el) => { let o = 1; for (let e = el; e && e !== document.documentElement; e = e.parentElement) o *= parseFloat(getComputedStyle(e).opacity); return +o.toFixed(2); };
        const best = (sel) => Math.max(0, ...[...document.querySelectorAll(sel)].map(shown));
        const title = [...document.querySelectorAll('.capsule .lg-title')].sort((a, b) => shown(b) - shown(a))[0];
        return { pill: best('.capsule'), orb: best('.capsule .orb'), title: title ? shown(title) : 0, text: title?.textContent };
      });
      if (late === 200) await lively.screenshot({ path: resolve(shots, 'capsule-chip-reshow.png'), clip: { x: 0, y: 0, width: 500, height: 110 } });
      assert.ok(seen.pill > .95 && seen.orb > .95 && seen.title > .95 && seen.text === '输入', `+${late} ms: ${JSON.stringify(seen)}`);
      await lively.waitForTimeout(1400); // let this chip time out and fade before the next round
    }
  });
  await lively.close();

  // The waveform-only listening states with full motion, light and dark (screenshots for review).
  for (const scheme of ['light', 'dark']) {
    const cap = await browser.newPage({ viewport: { width: 500, height: 580 }, colorScheme: scheme });
    await cap.goto(base + '/overlay.html');
    await cap.evaluate(`window.__demo.emit('prefs', ${reducedPrefs.replace('reduced_motion:true', 'reduced_motion:false')})`);
    await check(`${scheme}: waveform-only capsule while listening (input pill and note card)`, async () => {
      for (const [kind, words, done, height] of [['input', '读取配置', '已输入到光标处', 110], ['notes', '今天想把点声', '已保存', 270]]) {
        await cap.evaluate((k) => (window).__demo.play(k), kind);
        await cap.locator('.capsule [data-wave-only]').waitFor({ timeout: 8000 });
        await cap.waitForTimeout(MID_RECORDING_MS - 1800);
        assert.ok(!(await cap.locator('.capsule').innerText()).includes(words), `${kind}: no local guesses`);
        await cap.screenshot({ path: resolve(shots, `capsule-${kind === 'input' ? 'input' : 'note'}-wave-${scheme}.png`), clip: { x: 0, y: 0, width: 500, height } });
        await cap.locator('.capsule', { hasText: done }).waitFor({ timeout: 12000 });
      }
    });
    await cap.close();
    const win = await browser.newPage({ viewport: { width: 1200, height: 800 }, colorScheme: scheme });
    await win.goto(base + '/');
    await win.getByRole('button', { name: '启用语音' }).click();
    await win.getByRole('button', { name: '暂停语音' }).waitFor({ timeout: 3000 });
    await check(`${scheme}: waveform-only voice page while listening`, async () => {
      await win.evaluate(() => (window).__demo.play('input'));
      await win.locator('main [data-wave-only]').waitFor({ timeout: 8000 });
      await win.waitForTimeout(MID_RECORDING_MS - 1800);
      assert.ok(!(await win.locator('main').innerText()).includes('读取配置'));
      await win.screenshot({ path: resolve(shots, `voice-wave-${scheme}.png`) });
      await win.waitForFunction(() => document.querySelector('main')?.textContent?.includes('读取配置文件，'), null, { timeout: 12000 });
    });
    await win.close();
  }
} finally {
  await browser.close();
  await new Promise((done) => server.httpServer.close(done));
}

for (const [status, name, detail] of results) console.log(`${status}  ${name}${detail ? ' — ' + detail : ''}`);
const failed = results.filter((r) => r[0] === 'FAIL').length;
console.log(`\n${results.length - failed}/${results.length} checks passed; build ${dist}; screenshots in ${shots}`);
process.exit(failed ? 1 : 0);
