/* Setup wizard plumbing kept free of React, so it can be tested on its own.

   While the wizard is open every ⑥ press is a test and step 1 keeps the pen's microphone
   open for its level meter. Both are leases the core lets lapse (90 s / 20 s) unless the
   page renews them. A wizard left open behind other windows, or in a main window hidden to
   the tray (the page stays mounted), must not keep renewing them: every dictation would
   become a test and the microphone would stay open. So a lease is renewed only while the
   window is shown and focused, and given back the moment that stops. */

export interface WindowPresence {
  /** The window is shown and has the focus. */
  active(): boolean;
  /** Call `listener` whenever that may have changed; returns the unsubscribe. */
  subscribe(listener: () => void): () => void;
}

export interface Timers {
  setInterval(handler: () => void, ms: number): number;
  clearInterval(id: number): void;
}

/** The real window: visible (not minimised / hidden to the tray) and focused. */
export function windowPresence(doc: Document = document, win: Window = window): WindowPresence {
  return {
    active: () => !doc.hidden && doc.hasFocus(),
    subscribe(listener) {
      doc.addEventListener('visibilitychange', listener);
      win.addEventListener('focus', listener);
      win.addEventListener('blur', listener);
      return () => {
        doc.removeEventListener('visibilitychange', listener);
        win.removeEventListener('focus', listener);
        win.removeEventListener('blur', listener);
      };
    },
  };
}

/** Calls `on` at once and every `every` ms while the window is active, and `off` as soon as
    it is hidden or loses focus - and when the returned stop function runs (unmount). */
export function renewWhileActive(presence: WindowPresence, on: () => void, off: () => void, every: number,
  timers: Timers = window): () => void {
  let timer: number | null = null;
  let leased = false;
  const sync = () => {
    if (presence.active()) {
      if (timer === null) {
        leased = true;
        on();
        timer = timers.setInterval(on, every);
      }
    } else if (timer !== null || leased) {
      if (timer !== null) timers.clearInterval(timer);
      timer = null;
      leased = false;
      off();
    }
  };
  const unsubscribe = presence.subscribe(sync);
  sync();
  return () => {
    unsubscribe();
    if (timer !== null) timers.clearInterval(timer);
    timer = null;
    leased = false;
    off();
  };
}

/** Tab inside the dialog: the index to move the focus to (wrapping at both ends, pulling it
    back in from outside), or -1 to leave the browser's own move alone. */
export function trapTab(current: number, count: number, back: boolean): number {
  if (count === 0) return -1;
  if (current < 0) return back ? count - 1 : 0;
  if (back && current === 0) return count - 1;
  if (!back && current === count - 1) return 0;
  return -1;
}

export type CloseStep = 'close' | 'confirm' | 'ignore';

/** What Esc (or 稍后再说) does. `typing`: Esc pressed inside a text field (the field handles it);
    `unsaved`: an API key was typed or pasted but not saved - closing would throw it away, so
    the first attempt only asks; `confirming`: that question is on screen already. */
export function closeStep({ typing, unsaved, confirming }: { typing: boolean; unsaved: boolean; confirming: boolean }): CloseStep {
  if (typing) return 'ignore';
  if (unsaved && !confirming) return 'confirm';
  return 'close';
}

/** The hotkey patch for picking dictation app `profile`. A shortcut re-recorded in settings
    belongs to its app: kept when the same app is picked again, cleared (the app's default) on
    a change - the same rule as the app picker in settings. */
export function hotkeyChoice(current: { profile: string }, profile: string): { profile: string; keys?: string } {
  return profile === current.profile ? { profile } : { profile, keys: '' };
}

/** Any secret field in `root` holding text that was never saved. */
export function hasUnsavedSecret(root: ParentNode | null | undefined): boolean {
  if (!root) return false;
  return [...root.querySelectorAll<HTMLInputElement>('.secret input')].some((input) => input.value.trim() !== '');
}
