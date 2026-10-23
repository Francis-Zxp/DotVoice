import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/* A controllable stand-in for lib/bridge, the only path to the core. */
const bus = vi.hoisted(() => ({
  handler: null as null | ((event: string, data: any) => void),
  call: (() => {}) as (...args: any[]) => Promise<any>,
  ready: (() => Promise.resolve(null)) as () => Promise<any>,
}));
vi.mock('./bridge', () => ({
  call: (...args: any[]) => bus.call(...args),
  onCore: (handler: any) => {
    bus.handler = handler;
    return () => {};
  },
  shell: { ready: () => bus.ready() },
  inTauri: true,
  isMock: false,
  CoreError: class extends Error {},
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

const snap = (phase: string, extra: Record<string, unknown> = {}) => ({
  version: '0.11.6', state: { phase, message: '' }, result: null, usage: null, devices: [], instances: [], selected: 'baseus',
  mode: 'hold', tidepool: {}, destination: 'input', pending_mode: null, prefs: { appearance: {} }, tool: 'mouse',
  bindings: null, live_text: '', armed: true, airmouse_active: false, smoke: false, ...extra,
});
const NOTES = { items: [], tags: [], warning: '' };

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
  bus.handler = null;
  bus.ready = () => Promise.resolve(null);
  vi.stubGlobal('window', { setTimeout: (f: () => void, ms: number) => setTimeout(f, ms) });
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe('connectCore', () => {
  it('keeps events that overtook the snapshot reply', async () => {
    const { connectCore, useStore } = await import('./store');
    const snapshot = deferred<any>();
    bus.call = (method: string) => (method === 'snapshot' ? snapshot.promise : Promise.resolve(NOTES));
    const done = connectCore();
    bus.handler!('state', { phase: 'recording', message: '' });
    bus.handler!('mode', { value: 'notes', pending: null, tool: 'laser' });
    snapshot.resolve(snap('idle', { tidepool: { battery: 80 } }));
    await done;
    const s = useStore.getState();
    expect(s.ready).toBe(true);
    expect(s.state.phase).toBe('recording');
    expect(s.mode).toBe('notes');
    expect(s.tool).toBe('laser');
    // Fields no event touched still come from the snapshot.
    expect(s.tidepool).toEqual({ battery: 80 });
  });

  it('merges the same way when it falls back to the cached ready snapshot', async () => {
    const { connectCore, useStore } = await import('./store');
    const ready = deferred<any>();
    bus.call = (method: string) => (method === 'snapshot' ? Promise.reject(new Error('点声核心未运行')) : Promise.resolve(NOTES));
    bus.ready = () => ready.promise;
    const done = connectCore();
    await Promise.resolve();
    bus.handler!('state', { phase: 'processing', message: '' });
    ready.resolve(snap('idle'));
    await done;
    expect(useStore.getState().state.phase).toBe('processing');
  });

  it('later events are applied normally once the snapshot is in', async () => {
    const { connectCore, useStore } = await import('./store');
    bus.call = (method: string) => Promise.resolve(method === 'snapshot' ? snap('idle') : NOTES);
    await connectCore();
    expect(useStore.getState().state.phase).toBe('idle');
    bus.handler!('state', { phase: 'recording', message: '' });
    expect(useStore.getState().state.phase).toBe('recording');
  });
});

describe('core exit', () => {
  it('core_exit and fatal leave no stale phase in the main window', async () => {
    const { connectCore, useStore } = await import('./store');
    bus.call = (method: string) => Promise.resolve(method === 'snapshot' ? snap('idle') : NOTES);
    await connectCore();
    bus.handler!('state', { phase: 'recording', message: '' });
    bus.handler!('core_exit', { code: 1, restarting: false });
    expect(useStore.getState()).toMatchObject({ coreDown: '点声核心已退出', state: { phase: 'error', message: '点声核心已退出' } });
    bus.handler!('ready', snap('idle'));
    expect(useStore.getState()).toMatchObject({ coreDown: null, state: { phase: 'idle' } });
    bus.handler!('fatal', '另一个点声核心正在运行');
    expect(useStore.getState()).toMatchObject({ coreDown: '另一个点声核心正在运行', state: { phase: 'error' } });
  });
});

describe('dismissReminder', () => {
  it('keeps the banner when the core could not drop the reminder', async () => {
    const { dismissReminder, useStore } = await import('./store');
    const reminder = { kind: 'released' as const, version: '1.4', url: 'https://example.invalid' };
    useStore.setState({ reminder });
    bus.call = () => Promise.reject(new Error('点声核心响应超时'));
    await dismissReminder();
    expect(useStore.getState().reminder).toBe(reminder);
    expect(useStore.getState().notices.map((n) => n.text)).toEqual(['点声核心响应超时']);
    bus.call = () => Promise.resolve({ ok: true });
    await dismissReminder();
    expect(useStore.getState().reminder).toBeNull();
  });

  it('does not clear a newer reminder that arrived meanwhile', async () => {
    const { dismissReminder, useStore } = await import('./store');
    const reply = deferred<any>();
    useStore.setState({ reminder: { kind: 'closed' as const, url: 'https://example.invalid' } });
    bus.call = () => reply.promise;
    const done = dismissReminder();
    const newer = { kind: 'released' as const, version: '1.5', url: 'https://example.invalid' };
    useStore.setState({ reminder: newer });
    reply.resolve({ ok: true });
    await done;
    expect(useStore.getState().reminder).toBe(newer);
  });
});

describe('refreshNotes', () => {
  it('never shows "undefined" when the call fails without a message', async () => {
    const { useStore } = await import('./store');
    bus.call = () => Promise.reject('点声核心未运行');
    await useStore.getState().refreshNotes();
    expect(useStore.getState().notices.map((n) => n.text)).toEqual(['笔记列表未能刷新，请重试。']);
  });
});

describe('notices', () => {
  it('a failed copy from the "文字没能输入" notice says so instead of failing silently', async () => {
    const { connectCore, useStore } = await import('./store');
    bus.call = (method: string) => Promise.resolve(method === 'snapshot' ? snap('idle') : NOTES);
    await connectCore();
    bus.handler!('result', { text: '我要评论', destination: 'input', not_typed: 'refused', copied: false });
    const kept = useStore.getState().notices.at(-1)!;
    expect(kept.action?.label).toBe('复制');
    bus.call = () => Promise.reject(new Error('剪贴板被其他程序占用'));
    kept.action!.run();
    for (let i = 0; i < 5; i++) await Promise.resolve();  // the rejected call settles (no notice timers run)
    expect(useStore.getState().notices.map((n) => n.text)).toContain('剪贴板被其他程序占用');
  });

  it('a low battery names the pen when several are plugged in', async () => {
    const { connectCore, useStore } = await import('./store');
    bus.call = (method: string) => Promise.resolve(method === 'snapshot' ? snap('idle') : NOTES);
    await connectCore();
    bus.handler!('battery_alert', { percent: 18, instance: 'baseus:7b21e4d9', label: '倍思 #2' });
    bus.handler!('battery_alert', { percent: 12 });
    expect(useStore.getState().notices.map((n) => n.text)).toEqual(['倍思 #2 电量 18%，建议充电', '倍思电量 12%，建议充电']);
  });
});

describe('phase timing', () => {
  it('records when a phase began, and a repeated phase (whisper boosting began) keeps it', async () => {
    const { connectCore, useStore } = await import('./store');
    bus.call = (method: string) => Promise.resolve(method === 'snapshot' ? snap('idle') : NOTES);
    await connectCore();
    bus.handler!('state', { phase: 'recording', message: '' });
    const started = useStore.getState().phaseAt;
    vi.advanceTimersByTime(3000);
    bus.handler!('state', { phase: 'recording', message: '', whisper: true });
    expect(useStore.getState().phaseAt).toBe(started);
    bus.handler!('state', { phase: 'processing', message: '' });
    expect(useStore.getState().phaseAt).toBe(started + 3000);
    // A snapshot in the same phase (the core restarted its UI link) keeps the start too.
    vi.advanceTimersByTime(500);
    useStore.getState().apply(snap('processing') as any);
    expect(useStore.getState().phaseAt).toBe(started + 3000);
  });
});
