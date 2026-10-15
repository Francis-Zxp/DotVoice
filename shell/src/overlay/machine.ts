/* Capsule behaviour as a pure reducer. Timers are requested through
   `timer` + `token`; the component schedules them and echoes the token back. */
import { liveTextShown, nextLive } from '../lib/liveText';
import type { LiveSource, Mode, Phase, Tool } from '../lib/types';

export type View = 'hidden' | 'chip' | 'live' | 'note';
/* elevated: an administrator window dropped a click / scroll (said once per window);
   kept: it dropped the typed text, which is kept (chip text 'copied' = on the clipboard).
   test: a setup-wizard test recording ended - nothing was typed, the wizard shows the text.
   failed: a built-in engine could not finish this one recording (chip text = why); the pen
   stays ready, so this is no connection error.
   unsaved: the note could not be written to disk; the text is only in the main window.
   empty: chip text 'skipped' = an empty note, 'unrecognized' = an input result with no words. */
export type ChipKind = 'mode' | 'tool' | 'saved' | 'error' | 'drag' | 'drop' | 'empty' | 'elevated' | 'kept' | 'test' | 'failed' | 'unsaved';

export interface Model {
  view: View;
  mode: Mode;
  pending: Mode | null;
  tool: Tool;
  armed: boolean;
  phase: Phase;
  chip: { kind: ChipKind; mode: Mode; pending: boolean; text: string } | null;
  sessionMode: Mode;
  startedAt: number | null;
  text: string;          // live text, then the final text (what is shown: see shownText)
  source: LiveSource | null; // who wrote the live text
  final: boolean;        // text is the final result
  savedAt: number | null;
  fast?: boolean;            // the text was typed by 极速落字 (local recognition)
  external?: string;         // the hotkey bridge handed the session to this app (it typed the text)
  done: boolean;
  pinned: boolean;
  whisper: boolean;      // the session applies whisper (气声) boosting
  enabled: boolean;
  onSwitch: boolean;
  linger: number;
  showLocal: boolean;    // prefs preview.show_local_text: the local preview's words may show
  blockedLater?: boolean;    // an 'elevated' notice came mid-session: shown once the session card goes
  timer: { ms: number; then: 'hide' | 'collapse' } | null;
  token: number;
}

export type Event =
  | { type: 'config'; enabled: boolean; onSwitch: boolean; linger: number; showLocal?: boolean }
  | { type: 'init'; mode: Mode; pending: Mode | null; armed: boolean; phase: Phase; tool: Tool }
  | { type: 'mode'; value: Mode; pending: Mode | null; armed: boolean; source: string; tool?: Tool }
  | { type: 'tool'; tool: Tool }
  | { type: 'state'; phase: Phase; armed?: boolean; message?: string; at: number; destination?: 'input' | 'notes'; whisper?: boolean }
  | { type: 'drag'; locked: boolean; how?: 'grab' | 'lock' }
  | { type: 'empty' }
  | { type: 'test' }
  | { type: 'failed'; message: string }
  | { type: 'elevated' }
  | { type: 'partial'; text: string; source?: string }
  | { type: 'result'; destination: Mode; text: string; at: number; skipped?: boolean; empty?: boolean; noteError?: boolean; local?: boolean; external?: string; notTyped?: boolean; copied?: boolean }
  | { type: 'timeout'; token: number }
  | { type: 'pin' }
  | { type: 'dismiss' };

export const initial: Model = {
  view: 'hidden', mode: 'input', pending: null, tool: 'mouse', armed: false, phase: 'disconnected', chip: null,
  sessionMode: 'input', startedAt: null, text: '', source: null, final: false, savedAt: null, done: false, pinned: false,
  whisper: false, enabled: true, onSwitch: true, linger: 5, showLocal: false, timer: null, token: 0,
};

/* The words the capsule may show now: the final result, or live words from a source
   the user trusts (lib/liveText). '' while live = the waveform-only listening state. */
export function shownText(model: Model): string {
  if (model.final) return model.text;
  return model.text && liveTextShown(model.source, model.showLocal) ? model.text : '';
}

const BUSY: Phase[] = ['starting', 'recording', 'processing'];

/* The session's look follows where its text goes: a ⑦ quick note is a note even in
   input or present mode; an input session in notes mode cannot happen. */
function sessionLook(destination: 'input' | 'notes' | undefined, mode: Mode): Mode {
  if (destination === 'notes') return 'notes';
  if (destination === 'input') return mode === 'notes' ? 'input' : mode;
  return mode;
}

function arm(model: Model, ms: number, then: 'hide' | 'collapse'): Model {
  return { ...model, timer: { ms, then }, token: model.token + 1 };
}

function still(model: Model): Model {
  return { ...model, timer: null, token: model.token + 1 };
}

function hide(model: Model): Model {
  return { ...still(model), view: 'hidden', chip: null, blockedLater: false };
}

function chip(model: Model, kind: ChipKind, mode: Mode, pending = false, text = '', ms = 1700): Model {
  return arm({ ...model, view: 'chip', chip: { kind, mode, pending, text } }, ms, 'hide');
}

export function reduce(model: Model, event: Event): Model {
  switch (event.type) {
    case 'config': {
      const next = { ...model, enabled: event.enabled, onSwitch: event.onSwitch, linger: event.linger, showLocal: event.showLocal ?? model.showLocal };
      return event.enabled ? next : hide(next);
    }
    case 'init':
      return { ...model, mode: event.mode, pending: event.pending, armed: event.armed, phase: event.phase, tool: event.tool };
    case 'mode': {
      const next = { ...model, mode: event.value, pending: event.pending, armed: event.armed, tool: event.tool ?? model.tool };
      if (!model.enabled || !model.onSwitch) return next;
      // During a session the switch is a badge; a finished, unpinned card yields to the chip.
      if ((model.view === 'note' || model.view === 'live') && (!model.done || model.pinned)) return next;
      const target = event.pending ?? event.value;
      return chip(next, 'mode', target, !!event.pending, '', target === 'notes' && !event.pending ? 3200 : 1800);
    }
    case 'tool': {
      const next = { ...model, tool: event.tool };
      if (!model.enabled || model.view === 'note' || model.view === 'live') return next;
      return chip(next, 'tool', 'present', false, '', 1500);
    }
    case 'state': {
      const phase = event.phase;
      let next: Model = { ...model, phase, armed: event.armed ?? model.armed };
      if (!model.enabled) return next;
      if (phase === 'starting' || phase === 'recording') {
        // A new session follows a quiet phase (or a finished one); a repeat mid-session (whisper
        // boosting began) is the same session, also when its card was dismissed.
        const fresh = !isBusy(model.phase) || model.done;
        const sessionMode = fresh ? sessionLook(event.destination, model.mode) : model.sessionMode;
        if (fresh) next = { ...next, text: '', source: null, final: false, done: false, savedAt: null, sessionMode, startedAt: null, whisper: false, fast: false, external: undefined };
        // Whisper is a property of the session: a later event may confirm it; an omitted flag keeps it.
        next = { ...next, whisper: event.whisper ?? next.whisper };
        if (!fresh && model.view === 'hidden') return still(next);  // dismissed: stays out of the way
        if (phase === 'recording' && (fresh || next.startedAt == null)) next = { ...next, startedAt: event.at };
        return still({ ...next, view: sessionMode === 'notes' ? 'note' : 'live', chip: null });
      }
      if (phase === 'processing') return still({ ...next, whisper: event.whisper ?? model.whisper });
      if (phase === 'error') {
        // Partial words are not a saved note. Only a confirmed result may retain the saved card.
        if (model.view === 'note' && model.final && model.savedAt != null) {
          return model.pinned ? still(next) : arm(next, model.linger * 1000, 'collapse');
        }
        return chip({ ...next, done: true, pinned: false }, 'error', model.mode, false, event.message || '连接异常', 2800);
      }
      // idle / disconnected
      if (model.pinned && model.view === 'note') return next;
      if ((model.view === 'note' || model.view === 'live') && !model.done) {
        return phase === 'disconnected' ? hide(next) : arm(next, 1200, 'hide');
      }
      if (phase === 'disconnected' && model.view === 'chip' && model.chip?.kind === 'error') return hide(next);
      return next;
    }
    case 'drag': {
      // The double-tap drag lock holds the left button: say so until it is released.
      if (!model.enabled || model.view === 'note' || model.view === 'live') return model;
      return event.locked ? chip(model, 'drag', model.mode, false, event.how ?? 'grab', 15000) : chip(model, 'drop', model.mode, false, '', 1100);
    }
    case 'empty': {
      // Nobody spoke: the recording was cancelled, nothing typed or saved.
      if (!model.enabled) return model;
      return chip({ ...model, done: true, pinned: false }, 'empty', model.mode, false, '', 1900);
    }
    case 'test': {
      // Never a silent end like a dictation that went missing: it was a test, nothing typed.
      if (!model.enabled) return model;
      return chip({ ...model, done: true, pinned: false }, 'test', model.mode, false, '', 2600);
    }
    case 'failed': {
      if (!model.enabled) return model;
      return chip({ ...model, done: true, pinned: false }, 'failed', model.mode, false, event.message || '识别没有完成', 3400);
    }
    case 'elevated': {
      // The window in front runs as administrator and dropped a click / scroll. The core says
      // it once per window; a session card on screen is never interrupted for it (shown after).
      if (!model.enabled) return model;
      if ((model.view === 'note' || model.view === 'live') && !model.done) return { ...model, blockedLater: true };
      if (model.view === 'note' && model.pinned) return { ...model, blockedLater: true };
      return chip({ ...model, blockedLater: false }, 'elevated', model.mode, false, '', 5200);
    }
    case 'partial': {
      // Everything is kept (with its source); shownText decides what may be seen, so the
      // switch can change mid-session. An accurate stream is never overwritten by a late local guess.
      if (model.final) return model;
      const current = { text: model.text, source: model.source };
      const live = nextLive(current, event.text, event.source);
      return live === current ? model : { ...model, ...live };
    }
    case 'result': {
      if (!model.enabled) return model;
      if (event.skipped) return chip({ ...model, done: true, pinned: false }, 'empty', 'notes', false, 'skipped', 1900);
      // Nothing recognised, so nothing typed: never "已输入到光标处". (An external app's empty text means handed over.)
      if (event.empty && !event.external && event.destination !== 'notes') {
        return chip({ ...model, done: true, pinned: false }, 'empty', model.mode, false, 'unrecognized', 1900);
      }
      // The note could not be written: never "已保存". The text is kept in the main window.
      if (event.noteError && event.destination === 'notes') {
        return chip({ ...model, done: true, pinned: false, text: event.text, final: true, savedAt: null }, 'unsaved', 'notes', false, '', 4200);
      }
      const next = { ...model, done: true, text: event.text, final: true, savedAt: event.at, fast: !!event.local, external: event.external };
      if (event.notTyped && event.destination !== 'notes') {
        // Never "已输入到光标处": the text did not arrive. It is kept (clipboard, main window).
        return chip({ ...next, blockedLater: false }, 'kept', model.mode, false, event.copied ? 'copied' : '', 6000);
      }
      if (event.destination === 'notes') {
        if (model.view !== 'note') return chip(next, 'saved', 'notes', false, '', 1600);
        return model.pinned ? still(next) : arm(next, model.linger * 1000, 'collapse');
      }
      return model.view === 'hidden' ? next : arm({ ...next, view: 'live' }, 1600, 'hide');
    }
    case 'timeout': {
      if (event.token !== model.token || !model.timer) return model;
      if (model.pinned && model.view === 'note') return still(model);
      if (model.timer.then === 'collapse') return chip(model, 'saved', 'notes', false, '', 1300);
      if (model.blockedLater) return chip({ ...model, blockedLater: false }, 'elevated', model.mode, false, '', 5200);
      return hide(model);
    }
    case 'pin': {
      const pinned = !model.pinned;
      const next = { ...model, pinned };
      return pinned ? still(next) : model.done ? arm(next, model.linger * 1000, 'collapse') : next;
    }
    case 'dismiss':
      return hide({ ...model, pinned: false });
  }
}

export function isBusy(phase: Phase) {
  return BUSY.includes(phase);
}
