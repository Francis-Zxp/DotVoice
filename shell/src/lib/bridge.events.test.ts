import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/* Use the installed Tauri JS API, with only its native transport simulated.
   Tauri's Any listeners receive even targeted emits: a browser-only mock bus
   that filters by owning page would hide this cross-window routing bug. */
describe('native capsule event scope', () => {
  let id: number;
  let callbacks: Map<number, (event: unknown) => void>;
  let listeners: Map<number, { event: string; target: { kind: string; label?: string }; handler: number }>;
  let native: { metadata: { currentWindow: { label: string } } };
  beforeEach(() => {
    vi.resetModules();
    id = 0;
    callbacks = new Map();
    listeners = new Map();
    native = {
      metadata: { currentWindow: { label: 'overlay' } },
      ...{
        transformCallback(callback: (event: unknown) => void) { const next = ++id; callbacks.set(next, callback); return next; },
        async invoke(command: string, args: any) {
          if (command === 'plugin:event|listen') { const next = ++id; listeners.set(next, args); return next; }
          if (command === 'plugin:event|unlisten') { listeners.delete(args.eventId); return; }
          throw new Error(`Unexpected native command: ${command}`);
        },
      },
    };
    vi.stubGlobal('window', { __TAURI_INTERNALS__: native, __TAURI_EVENT_PLUGIN_INTERNALS__: { unregisterListener() {} } });
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  const emitTo = (label: string, event: string, payload: unknown) => {
    for (const [eventId, listener] of listeners) {
      if (listener.event === event && (listener.target.kind === 'Any' || listener.target.label === label)) {
        callbacks.get(listener.handler)?.({ event, id: eventId, payload });
      }
    }
  };

  it('a mirror layout cannot demote the controller or reposition another mirror', async () => {
    const { onOverlay } = await import('./bridge');
    const controller = vi.fn(), first = vi.fn(), second = vi.fn();
    await onOverlay('layout', controller);
    native.metadata.currentWindow.label = 'overlay-mirror-1';
    await onOverlay('layout', first);
    native.metadata.currentWindow.label = 'overlay-mirror-2';
    await onOverlay('layout', second);
    emitTo('overlay-mirror-1', 'overlay://layout', { controller: false, anchor: 'bottom-right' });
    expect(controller).not.toHaveBeenCalled();
    expect(second).not.toHaveBeenCalled();
    expect(first).toHaveBeenCalledExactlyOnceWith({ controller: false, anchor: 'bottom-right' });
    emitTo('overlay', 'overlay://layout', { controller: true, anchor: 'top-center' });
    expect(controller).toHaveBeenCalledExactlyOnceWith({ controller: true, anchor: 'top-center' });
    expect(first).toHaveBeenCalledTimes(1);
  });

  it('one targeted mirror publication is received only by that mirror', async () => {
    const { onOverlay } = await import('./bridge');
    const first = vi.fn(), second = vi.fn();
    native.metadata.currentWindow.label = 'overlay-mirror-1';
    await onOverlay('model', first);
    native.metadata.currentWindow.label = 'overlay-mirror-2';
    await onOverlay('model', second);
    emitTo('overlay-mirror-2', 'overlay://model', { seq: 12, model: { pinned: true } });
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledExactlyOnceWith({ seq: 12, model: { pinned: true } });
  });

  it('pin actions go only to the controller and unlisten removes the handler', async () => {
    const { onOverlay } = await import('./bridge');
    const controller = vi.fn(), other = vi.fn();
    const stop = await onOverlay('action', controller);
    native.metadata.currentWindow.label = 'overlay-mirror-1';
    await onOverlay('action', other);
    const action = { type: 'pin', value: true, session: 123, token: 7 };
    emitTo('overlay', 'overlay://action', action);
    expect(controller).toHaveBeenCalledExactlyOnceWith(action);
    expect(other).not.toHaveBeenCalled();
    stop();
    await Promise.resolve();
    emitTo('overlay', 'overlay://action', action);
    expect(controller).toHaveBeenCalledTimes(1);
  });
});
