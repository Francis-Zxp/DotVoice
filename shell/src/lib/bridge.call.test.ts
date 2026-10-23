import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/* Tauri rejects invoke() with the command's Err value as is: for core_call a plain string. */
describe('call', () => {
  let reply: () => Promise<unknown>;
  beforeEach(() => {
    vi.resetModules();
    vi.stubGlobal('window', {
      __TAURI_INTERNALS__: { invoke: () => reply(), transformCallback: () => 0 },
    });
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it('turns a string rejection into an Error carrying the core reason', async () => {
    const { call, CoreError } = await import('./bridge');
    reply = () => Promise.reject('点声核心未运行');
    const error = await call('notes').catch((e: unknown) => e);
    expect(error).toBeInstanceOf(CoreError);
    expect((error as Error).message).toBe('点声核心未运行');
  });

  it('keeps Error rejections and error results as they were', async () => {
    const { call, CoreError } = await import('./bridge');
    const original = new Error('ipc');
    reply = () => Promise.reject(original);
    await expect(call('notes')).rejects.toBe(original);
    reply = () => Promise.resolve({ error: '没有这条笔记' });
    await expect(call('delete_note')).rejects.toThrow(CoreError);
    reply = () => Promise.resolve({ ok: true });
    await expect(call('dismiss_reminder')).resolves.toEqual({ ok: true });
  });
});
