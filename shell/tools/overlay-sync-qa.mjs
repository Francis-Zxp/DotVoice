// Real React windows, simulated native message bus. No microphone, input or desktop changes.
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { chromium } from 'playwright-core';
import { createServer } from 'vite';

const server = await createServer({ root: resolve(import.meta.dirname, '..'), server: { port: 5186, strictPort: true }, logLevel: 'error' });
await server.listen();
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const windows = [], results = [], errors = [];
let cached = null, seq = 0, heldSnapshot = null;
async function send(target, event, data) {
  if (!target.page.isClosed()) await target.page.evaluate(({ event, data }) => window.__demo?.shellEmit(event, data), { event, data });
}
async function broadcast(model) {
  cached = { seq: ++seq, model: structuredClone(model) };
  await Promise.all(windows.filter(window => window.mirror).map(window => send(window, 'overlay://model', cached)));
  return cached;
}
async function open({ mirror = false, hold = false, scale = 1, startup = false } = {}) {
  const page = await browser.newPage({ viewport: { width: 500, height: 580 }, deviceScaleFactor: scale, reducedMotion: 'reduce' });
  page.setDefaultTimeout(5000);
  page.on('pageerror', error => errors.push(error.message));
  const window = { page, mirror, hold };
  windows.push(window);
  await page.exposeFunction('__qaNative', async (name, args) => {
    if (name === 'overlay_publish') {
      assert.equal(mirror, false, 'a mirror must never publish a business state');
      return broadcast(args.model);
    }
    if (name === 'overlay_snapshot') {
      const snapshot = structuredClone(cached);
      if (hold) return new Promise(resolve => { heldSnapshot = () => resolve(snapshot); });
      return snapshot;
    }
    if (name === 'overlay_action') {
      const controller = windows.find(window => !window.mirror);
      await send(controller, 'overlay://action', args.action);
    }
  });
  await page.route('**/overlay.html*', async route => {
    const response = await route.fetch();
    const body = (await response.text()).replace('<script type="module" src="/src/overlay-main.tsx"></script>', `<script type="module">
      import { createMock } from '/src/lib/mock.ts';
      const mock = createMock();
      const qa = window.__qa = { calls: [], hit: null, snapshotRequested: false };
      window.__demo.snap.prefs.appearance.reduced_motion = true;
      if (${startup}) {
        window.__demo.snap.destination = 'notes';
        window.__demo.snap.state = { phase:'recording', message:'', armed:true };
        window.__demo.snap.armed = true;
        window.__demo.snap.live_text = '较旧的快照文字';
        window.__demo.snap.live_source = 'remote';
      }
      window.__DOTVOICE_MOCK__ = { ...mock,
        async shell(name, args) {
          qa.calls.push({ name, args });
          if (name === 'overlay_interactive') qa.hit = args.rect;
          if (name === 'overlay_snapshot') qa.snapshotRequested = true;
          if (['overlay_publish', 'overlay_snapshot', 'overlay_action'].includes(name)) return window.__qaNative(name, args);
          return mock.shell(name, args);
        },
        async call(method, params) {
          qa.calls.push({ method, params });
          if (${startup} && method === 'snapshot') {
            const snapshot = structuredClone(await mock.call(method, params));
            return new Promise(resolve => { qa.releaseCoreSnapshot = () => resolve(snapshot); });
          }
          return mock.call(method, params);
        }
      };
      await import('/src/overlay-main.tsx');
    </script>`);
    await route.fulfill({ response, body });
  });
  await page.goto(`http://localhost:5186/overlay.html${mirror ? '?mirror=1' : ''}`);
  await page.locator('[data-overlay-anchor]').waitFor();
  if (mirror) await page.waitForFunction(() => window.__qa.snapshotRequested);
  else await page.waitForFunction(() => window.__qa.calls.some(call => call.name === 'overlay_publish'));
  return window;
}
async function check(name, test) {
  try { await test(); results.push({ name, pass: true }); }
  catch (error) { results.push({ name, pass: false, error: error.message }); }
}
async function text(window, value) {
  await window.page.waitForFunction(value => document.querySelector('.capsule')?.textContent?.includes(value), value);
}
async function layout(window, anchor, active = true) {
  await window.page.evaluate(({ anchor, active }) => {
    Object.assign(window.__demo.layout, { anchor, active });
    window.__demo.shellEmit('overlay://layout', { ...window.__demo.layout });
  }, { anchor, active });
}
let controller, mirror;
try {
  controller = await open({ startup:true });
  await check('late startup snapshot restores an ongoing note without rewinding newer partials or preferences', async () => {
    await controller.page.waitForFunction(() => !!window.__qa.releaseCoreSnapshot);
    await controller.page.evaluate(() => {
      window.__demo.emit('partial', { text:'先到的在线文字', source:'remote' });
      const prefs = structuredClone(window.__demo.snap.prefs);
      prefs.appearance.theme = 'dark';
      prefs.overlay.on_switch = false;
      prefs.overlay.linger_seconds = 9;
      window.__demo.emit('prefs', prefs);
      window.__qa.releaseCoreSnapshot();
    });
    await text(controller, '先到的在线文字');
    assert.equal(await controller.page.locator('html').getAttribute('data-theme'), 'dark');
    assert.equal(cached.model.phase, 'recording');
    assert.equal(cached.model.sessionMode, 'notes');
    assert.equal(cached.model.onSwitch, false);
    assert.equal(cached.model.linger, 9);
  });
  await controller.page.evaluate(() => {
    window.__demo.state('idle');
    window.__demo.setMode('notes', 'key');
    window.__demo.state('recording');
    window.__demo.emit('partial', { text: '多屏共享同一段语音', source: 'remote' });
  });
  await text(controller, '多屏共享同一段语音');
  mirror = await open({ mirror: true, scale: 2 });
  await check('new monitor restores the current live text from the controller snapshot', async () => {
    await text(mirror, '多屏共享同一段语音');
    assert.ok(cached.model.startedAt);
    await mirror.page.evaluate(() => window.__demo.emit('partial', { text: '镜像不能自己改变业务状态', source: 'remote' }));
    assert.ok(!(await mirror.page.locator('.capsule').innerText()).includes('镜像不能'));
  });
  await check('pinning on one display updates every display once', async () => {
    await mirror.page.getByRole('button', { name: '固定便签', exact: true }).click();
    await controller.page.getByRole('button', { name: '取消固定', exact: true }).waitFor();
    await mirror.page.getByRole('button', { name: '取消固定', exact: true }).waitFor();
    assert.equal(cached.model.pinned, true);
  });
  await check('all nine anchors align card, copied backdrop and hit region in CSS pixels at 200% scale', async () => {
    const anchors = [ ['top-left',0,0], ['top-center',.5,0], ['top-right',1,0], ['center-left',0,.5], ['center',.5,.5], ['center-right',1,.5], ['bottom-left',0,1], ['bottom-center',.5,1], ['bottom-right',1,1] ];
    for (const [anchor, x, y] of anchors) {
      await layout(mirror, anchor);
      await mirror.page.waitForTimeout(250);
      const geometry = await mirror.page.locator('.capsule').evaluate(el => {
        const rect = el.getBoundingClientRect();
        return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, bx: Number(el.style.getPropertyValue('--bx')), by: Number(el.style.getPropertyValue('--by')), hit: window.__qa.hit };
      });
      const expected = { x: 12 + (500 - 24 - geometry.width) * x, y: 12 + (580 - 24 - geometry.height) * y };
      for (const key of ['x', 'y']) {
        assert.ok(Math.abs(geometry[key] - expected[key]) < 1.5, `${anchor} card ${key}: ${JSON.stringify(geometry)}`);
        assert.ok(Math.abs(geometry['b' + key] - expected[key]) < 1.5, `${anchor} backdrop ${key}: ${JSON.stringify(geometry)}`);
        assert.ok(Math.abs(geometry.hit[key] - geometry[key]) < 1.5, `${anchor} hit ${key}`);
      }
    }
  });
  await check('host resizing recalculates the bottom-right backdrop and clickable region', async () => {
    await layout(mirror, 'bottom-right');
    await mirror.page.setViewportSize({ width: 620, height: 700 });
    await mirror.page.waitForTimeout(100);
    const geometry = await mirror.page.locator('.capsule').evaluate(el => {
      const r = el.getBoundingClientRect();
      return { x: r.x, y: r.y, right: r.right, bottom: r.bottom, hit: window.__qa.hit, bx: Number(el.style.getPropertyValue('--bx')), by: Number(el.style.getPropertyValue('--by')) };
    });
    assert.ok(Math.abs(geometry.right - 608) < 1);
    assert.ok(Math.abs(geometry.bottom - 688) < 1);
    for (const key of ['x','y']) {
      assert.ok(Math.abs(geometry.hit[key] - geometry[key]) < 1);
      assert.ok(Math.abs(geometry['b' + key] - geometry[key]) < 1);
    }
  });
  await check('saved-note waveform is shared and copying happens only in the clicked window', async () => {
    await controller.page.evaluate(() => {
      window.__demo.emit('result', { destination: 'notes', text: '整理好的同一条笔记。', envelope: [.1,.2,.3,.2,.1] });
      window.__demo.state('idle');
    });
    await text(mirror, '整理好的同一条笔记。');
    assert.deepEqual(cached.model.envelope, [.1,.2,.3,.2,.1]);
    await mirror.page.getByRole('button', { name: '复制', exact: true }).click();
    const copies = await Promise.all([controller,mirror].map(window => window.page.evaluate(() => window.__qa.calls.filter(call => call.method === 'copy_text'))));
    assert.deepEqual(copies.map(calls => calls.length), [0,1]);
  });
  const previous = structuredClone(cached.model);
  await check('dismiss is synchronized and old-screen actions cannot dismiss the next session', async () => {
    await mirror.page.getByRole('button', { name: '收起', exact: true }).click();
    await Promise.all([controller,mirror].map(window => window.page.locator('.capsule').waitFor({ state: 'detached' })));
    await controller.page.evaluate(() => {
      window.__demo.state('recording');
      window.__demo.emit('partial', { text: '下一段保留', source: 'cloud' });
    });
    await text(mirror, '下一段保留');
    await send(controller, 'overlay://action', { type:'dismiss', session:previous.startedAt, token:previous.token });
    await text(controller, '下一段保留');
    await text(mirror, '下一段保留');
  });
  await check('a late snapshot cannot overwrite a newer mirror event', async () => {
    const late = await open({ mirror:true, hold:true });
    assert.ok(heldSnapshot);
    await controller.page.evaluate(() => window.__demo.emit('partial', { text: '最新事件胜过旧快照', source:'remote' }));
    await text(late, '最新事件胜过旧快照');
    heldSnapshot(); heldSnapshot = null;
    await late.page.waitForTimeout(100);
    await text(late, '最新事件胜过旧快照');
    await late.page.close();
  });
  await check('an inactive controller keeps processing state for the other displays', async () => {
    await layout(controller, 'top-center', false);
    await controller.page.locator('.capsule').waitFor({ state:'detached' });
    await controller.page.evaluate(() => window.__demo.emit('partial', { text: '隐藏的控制窗继续同步', source:'remote' }));
    await text(mirror, '隐藏的控制窗继续同步');
  });
  await check('mirrors never run a second timer, recording, input or note-saving path', async () => {
    const model = { ...cached.model, pinned:false, done:true, timer:{ms:30,then:'hide'}, token:cached.model.token+1 };
    await broadcast(model);
    await mirror.page.waitForTimeout(350);
    await text(mirror, '隐藏的控制窗继续同步');
    const calls = (await Promise.all(windows.filter(window => !window.page.isClosed()).map(window => window.page.evaluate(() => window.__qa.calls)))).flat();
    assert.deepEqual(calls.filter(call => ['action','update_note','save_note'].includes(call.method)), []);
    assert.deepEqual(errors, []);
  });
} finally {
  heldSnapshot?.();
  await browser.close();
  await server.close();
}
for (const result of results) console.log(`${result.pass ? 'PASS' : 'FAIL'} ${result.name}${result.error ? '\n'+result.error : ''}`);
process.exit(results.every(result => result.pass) ? 0 : 1);
