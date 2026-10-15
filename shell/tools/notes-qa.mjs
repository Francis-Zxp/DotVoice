// Exercise pending/failed note writes in the real React UI; only a browser mock is used.
import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { chromium } from 'playwright-core';
import { createServer } from 'vite';

const root = resolve(import.meta.dirname, '..');
const server = await createServer({ root, server: { port: 5187, strictPort: true }, logLevel: 'error' });
await server.listen();
const browser = await chromium.launch({ channel: 'msedge', headless: true });
const results = [];

async function open() {
  const page = await browser.newPage({ viewport: { width: 1200, height: 800 }, reducedMotion: 'reduce' });
  page.setDefaultTimeout(5000);
  await page.route('http://localhost:5187/', async (route) => {
    const response = await route.fetch();
    const body = (await response.text()).replace('<script type="module" src="/src/main.tsx"></script>', `<script type="module">
      import { createMock } from '/src/lib/mock.ts';
      const mock = createMock();
      const qa = window.__notesQA = { notes: (await mock.call('notes', {})).items, calls: [], pending: [], hold: false, fail: false };
      window.__DOTVOICE_MOCK__ = { ...mock, async call(method, params) {
        if (method === 'notes') return { items: structuredClone(qa.notes), tags: [...new Set(qa.notes.flatMap(n => n.tags))], warning: '' };
        if (['update_note', 'set_note_tags', 'copy_text'].includes(method)) {
          qa.calls.push({ method, params });
          if (qa.hold) await new Promise(resolve => qa.pending.push(resolve));
          if (qa.fail) return { error: '模拟保存失败，原稿保留' };
          const note = qa.notes.find(n => n.id === params.identifier);
          if (method === 'update_note') note.text = params.text;
          if (method === 'set_note_tags') note.tags = params.tags;
          return { note, ok: true };
        }
        return mock.call(method, params);
      }};
      await import('/src/main.tsx');
    </script>`);
    await route.fulfill({ response, body });
  });
  await page.goto('http://localhost:5187/');
  await page.locator('.nav-item', { hasText: '笔记' }).click();
  await page.locator('[data-note-detail]').waitFor();
  return page;
}
async function check(name, test) {
  const page = await open();
  try { await test(page); results.push({ name, pass: true }); }
  catch (error) { results.push({ name, pass: false, error: error.message }); }
  finally { await page.close(); }
}

try {
  await check('a new voice note does not replace the draft being edited', async (page) => {
    const id = await page.locator('[data-note-detail]').getAttribute('data-note-detail');
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    await page.getByRole('textbox', { name: '编辑整理稿' }).fill('尚未保存的修改');
    await page.evaluate(() => {
      const qa = window.__notesQA;
      qa.notes.unshift({ ...qa.notes[0], id: 'e'.repeat(32), created: Date.now() / 1000, text: '刚刚保存的语音笔记' });
      window.__demo.emit('note_saved', { id: 'e'.repeat(32) });
    });
    await page.waitForFunction(() => document.querySelectorAll('[data-note-id]').length === 5);
    await page.waitForTimeout(250);
    assert.equal(await page.locator('[data-note-detail]').getAttribute('data-note-detail'), id);
    assert.equal(await page.getByRole('textbox', { name: '编辑整理稿' }).inputValue(), '尚未保存的修改');
  });
  await check('copy while editing copies the visible draft', async (page) => {
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    await page.getByRole('textbox', { name: '编辑整理稿' }).fill('正在修改的草稿');
    await page.getByRole('button', { name: '复制', exact: true }).click();
    const copied = await page.evaluate(() => window.__notesQA.calls.find(c => c.method === 'copy_text').params.text);
    assert.equal(copied, '正在修改的草稿');
  });
  await check('pending saves cannot be duplicated or discard newly typed text', async (page) => {
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    const editor = page.getByRole('textbox', { name: '编辑整理稿' });
    await editor.fill('这次保存的修改');
    await page.evaluate(() => { window.__notesQA.hold = true; });
    const save = page.getByRole('button', { name: /保存修改|正在保存/ });
    // Two clicks before React has committed a disabled state must still issue only one write.
    await save.evaluate(b => { b.click(); b.click(); });
    assert.equal(await page.evaluate(() => window.__notesQA.calls.filter(c => c.method === 'update_note').length), 1);
    assert.ok(await editor.isDisabled());
    assert.ok(await page.getByRole('button', { name: '取消', exact: true }).isDisabled());
    await page.evaluate(() => { window.__notesQA.hold = false; window.__notesQA.pending.splice(0).forEach(resolve => resolve()); });
    await page.locator('.note-body').waitFor();
    assert.equal(await page.locator('.note-body').innerText(), '这次保存的修改');
  });
  await check('failed saves keep the draft editable and retry succeeds', async (page) => {
    await page.getByRole('button', { name: '编辑', exact: true }).click();
    const editor = page.getByRole('textbox', { name: '编辑整理稿' });
    await editor.fill('不能丢失的修改');
    await page.evaluate(() => { window.__notesQA.fail = true; });
    await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await page.getByText('模拟保存失败，原稿保留', { exact: true }).waitFor();
    assert.equal(await editor.inputValue(), '不能丢失的修改');
    assert.ok(await editor.isEnabled());
    await page.evaluate(() => { window.__notesQA.fail = false; });
    await page.getByRole('button', { name: '保存修改', exact: true }).click();
    await page.locator('.note-body').waitFor();
    assert.equal(await page.locator('.note-body').innerText(), '不能丢失的修改');
  });
  await check('failed tag writes keep the typed tag for retry', async (page) => {
    await page.getByRole('button', { name: '标签', exact: true }).click();
    const input = page.getByPlaceholder('标签名');
    await input.fill('待核实');
    await page.evaluate(() => { window.__notesQA.fail = true; });
    await input.press('Enter');
    await page.getByText('模拟保存失败，原稿保留', { exact: true }).waitFor();
    assert.equal(await input.inputValue(), '待核实');
    assert.ok(await input.isEnabled());
    await page.evaluate(() => { window.__notesQA.fail = false; });
    await input.press('Enter');
    await page.getByRole('button', { name: '移除标签 待核实', exact: true }).waitFor();
  });
  await check('tag writes are serialized until the refreshed tags arrive', async (page) => {
    await page.getByRole('button', { name: '标签', exact: true }).click();
    await page.getByPlaceholder('标签名').fill('待核实');
    await page.evaluate(() => { window.__notesQA.hold = true; });
    await page.getByPlaceholder('标签名').press('Enter');
    assert.ok(await page.getByRole('button', { name: '移除标签 灵感', exact: true }).isDisabled());
    await page.evaluate(() => { window.__notesQA.hold = false; window.__notesQA.pending.splice(0).forEach(resolve => resolve()); });
    await page.getByRole('button', { name: '移除标签 待核实', exact: true }).waitFor();
    await page.getByRole('button', { name: '移除标签 灵感', exact: true }).click();
    await page.waitForFunction(() => !document.querySelector('button[aria-label="移除标签 灵感"]'));
    assert.ok(await page.getByRole('button', { name: '移除标签 待核实', exact: true }).isVisible());
  });
} finally {
  await browser.close();
  await server.close();
}
for (const result of results) console.log(`${result.pass ? 'PASS' : 'FAIL'} ${result.name}${result.error ? '\n' + result.error : ''}`);
process.exit(results.every(result => result.pass) ? 0 : 1);
