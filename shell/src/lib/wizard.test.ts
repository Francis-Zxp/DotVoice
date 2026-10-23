import { describe, expect, it } from 'vitest';
import { closeStep, hotkeyChoice, renewWhileActive, trapTab, typedThisRound, type Timers, type WindowPresence } from './wizard';

/* A window that can be shown / hidden / focused by the test, and timers it can advance. */
function fakeWindow(active = true) {
  let state = active;
  const listeners = new Set<() => void>();
  const presence: WindowPresence = {
    active: () => state,
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
  return {
    presence,
    set(next: boolean) {
      state = next;
      listeners.forEach((l) => l());
    },
    listeners,
  };
}

function fakeTimers() {
  let next = 1;
  const intervals = new Map<number, { handler: () => void; ms: number; left: number }>();
  const timers: Timers = {
    setInterval(handler, ms) {
      intervals.set(next, { handler, ms, left: ms });
      return next++;
    },
    clearInterval(id) {
      intervals.delete(id);
    },
  };
  return {
    timers,
    intervals,
    advance(ms: number) {
      for (let t = 0; t < ms; t += 1000) {
        for (const entry of [...intervals.values()]) {
          entry.left -= 1000;
          if (entry.left <= 0) {
            entry.left += entry.ms;
            entry.handler();
          }
        }
      }
    },
  };
}

describe('wizard leases (test mode, microphone meter)', () => {
  it('renews only while the window is shown and focused, and gives the lease back at once', () => {
    const win = fakeWindow(true);
    const clock = fakeTimers();
    const calls: string[] = [];
    const stop = renewWhileActive(win.presence, () => calls.push('on'), () => calls.push('off'), 30000, clock.timers);
    expect(calls).toEqual(['on']);
    clock.advance(60000);
    expect(calls).toEqual(['on', 'on', 'on']);
    // Hidden to the tray, or another app in front: no more renewals, test mode off now.
    win.set(false);
    expect(calls.at(-1)).toBe('off');
    const count = calls.length;
    clock.advance(300000);
    expect(calls.length).toBe(count);
    expect(clock.intervals.size).toBe(0);
    win.set(false); // a second blur says nothing new
    expect(calls.length).toBe(count);
    // Back in front: renewed at once.
    win.set(true);
    expect(calls.at(-1)).toBe('on');
    stop();
    expect(calls.at(-1)).toBe('off');
    expect(win.listeners.size).toBe(0);
    expect(clock.intervals.size).toBe(0);
  });

  it('a wizard opened in a hidden window never takes the lease', () => {
    const win = fakeWindow(false);
    const clock = fakeTimers();
    const calls: string[] = [];
    const stop = renewWhileActive(win.presence, () => calls.push('on'), () => calls.push('off'), 8000, clock.timers);
    clock.advance(60000);
    expect(calls).toEqual([]);
    stop();
    expect(calls).toEqual(['off']); // unmount always hands it back (harmless when not held)
  });
});

describe('wizard dialog keys', () => {
  it('keeps Tab inside the dialog', () => {
    expect(trapTab(4, 5, false)).toBe(0);
    expect(trapTab(0, 5, true)).toBe(4);
    expect(trapTab(-1, 5, false)).toBe(0);
    expect(trapTab(-1, 5, true)).toBe(4);
    expect(trapTab(2, 5, false)).toBe(-1);
    expect(trapTab(2, 5, true)).toBe(-1);
    expect(trapTab(-1, 0, false)).toBe(-1);
  });

  it('asks before Esc throws away a pasted key that was not saved', () => {
    expect(closeStep({ typing: false, unsaved: false, confirming: false })).toBe('close');
    expect(closeStep({ typing: false, unsaved: true, confirming: false })).toBe('confirm');
    expect(closeStep({ typing: false, unsaved: true, confirming: true })).toBe('close');
    expect(closeStep({ typing: true, unsaved: true, confirming: false })).toBe('ignore');
  });
});

describe('wizard engine choice', () => {
  it('keeps a shortcut re-recorded in settings when the same dictation app is picked again', () => {
    // 闪电说 with Ctrl + Alt + Space while Doubao is the engine: the profile and keys stay saved.
    const current = { profile: 'shandianshuo', keys: 'ctrl+alt+space' };
    expect(hotkeyChoice(current, 'shandianshuo')).toEqual({ profile: 'shandianshuo' });
    // Another app: its own default shortcut, never the previous app's combo.
    expect(hotkeyChoice(current, 'typeless')).toEqual({ profile: 'typeless', keys: '' });
  });
});

describe('wizard 试一句 with another dictation app', () => {
  it('counts only text that appeared during this round as typed by the app', () => {
    expect(typedThisRound('', '')).toBe(false);
    expect(typedThisRound('今天下午开会', '')).toBe(true);
    // The first round's text left in the box, or words typed by hand before speaking: no proof.
    expect(typedThisRound('今天下午开会', '今天下午开会')).toBe(false);
    expect(typedThisRound('今天下午开会。明天见', '今天下午开会')).toBe(true);
    expect(typedThisRound('   ', '')).toBe(false);
  });
});
