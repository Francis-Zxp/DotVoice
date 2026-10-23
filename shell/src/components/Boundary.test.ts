import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const logged: unknown[] = [];
vi.mock('../lib/bridge', () => ({
  call: (method: string, params: unknown) => {
    if (method === 'log_ui_error') logged.push(params);
    return Promise.resolve({ ok: true });
  },
}));

describe('Boundary', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal('window', { setTimeout: (f: () => void, ms: number) => setTimeout(f, ms), clearTimeout: (t: number) => clearTimeout(t) });
    logged.length = 0;
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  /* Drives the boundary the way React does for a child that throws on every render. */
  async function mount(boom: () => Error) {
    const { Boundary } = await import('./Boundary');
    const b: any = new Boundary({ name: 'notes', children: null });
    const render = () => {
      if (b.state.error) return;
      const error = boom();
      Object.assign(b.state, Boundary.getDerivedStateFromError(error));
      b.componentDidCatch(error, { componentStack: '\n    at NotesPage' });
    };
    b.setState = (update: any) => {
      Object.assign(b.state, typeof update === 'function' ? update(b.state) : update);
      render();
    };
    render();
    return b;
  }

  it('stops remounting a child that always throws and logs the error once', async () => {
    const b = await mount(() => new Error('render failed'));
    vi.advanceTimersByTime(60_000);
    expect(b.state.key).toBe(3);
    expect(b.state.error).not.toBeNull();
    expect(logged).toHaveLength(1);
  });

  it('still recovers from a one-off error', async () => {
    const b = await mount(() => new Error('once'));
    // The next render succeeds.
    b.setState = (update: any) => Object.assign(b.state, typeof update === 'function' ? update(b.state) : update);
    vi.advanceTimersByTime(600);
    expect(b.state).toMatchObject({ error: null, key: 1 });
    expect(logged).toHaveLength(1);
  });

  it('logs each different error, within the same retry limit', async () => {
    let n = 0;
    const b = await mount(() => new Error(`failure ${++n}`));
    vi.advanceTimersByTime(60_000);
    expect(b.state.key).toBe(3);
    expect(logged).toHaveLength(4);
  });
});
