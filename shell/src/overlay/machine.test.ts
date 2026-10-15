import { describe, expect, it } from 'vitest';
import { initial, reduce, shownText, type Event, type Model } from './machine';

const run = (events: Event[], start: Model = initial) => events.reduce(reduce, start);
const mode = (value: Model['mode'], pending: Model['pending'] = null): Event => ({ type: 'mode', value, pending, armed: true, source: 'key' });

describe('capsule machine', () => {
  it('shows a chip for every mode in the cycle and hides it on its own timer', () => {
    let m = run([mode('notes')]);
    expect(m.view).toBe('chip');
    expect(m.chip).toMatchObject({ kind: 'mode', mode: 'notes', pending: false });
    expect(m.timer).toEqual({ ms: 3200, then: 'hide' });
    const stale = reduce(m, { type: 'timeout', token: m.token - 1 });
    expect(stale.view).toBe('chip');
    m = reduce(m, { type: 'timeout', token: m.token });
    expect(m.view).toBe('hidden');
    m = reduce(m, mode('present'));
    expect(m.chip?.mode).toBe('present');
    expect(m.timer?.ms).toBe(1800);
    m = reduce(m, mode('input'));
    expect(m.chip?.mode).toBe('input');
  });

  it('a tool change shows a tool chip, but never over a live session', () => {
    let m = run([mode('present'), { type: 'tool', tool: 'spotlight' }]);
    expect(m.chip).toMatchObject({ kind: 'tool' });
    expect(m.tool).toBe('spotlight');
    m = run([{ type: 'state', phase: 'recording', at: 1 }, { type: 'tool', tool: 'laser' }], m);
    expect(m.view).toBe('live');
    expect(m.tool).toBe('laser');
  });

  it('input sessions stay compact and show the live preview, then the final text', () => {
    let m = run([{ type: 'state', phase: 'recording', at: 1 }, { type: 'partial', text: '创建一个', source: 'remote' }]);
    expect(m.view).toBe('live');
    expect(m.text).toBe('创建一个');
    m = run([{ type: 'result', destination: 'input', text: '创建一个函数。', at: 2 }, { type: 'partial', text: '迟到的预览', source: 'remote' }], m);
    expect(m.done).toBe(true);
    expect(m.final).toBe(true);
    expect(m.text).toBe('创建一个函数。');
    expect(m.timer?.then).toBe('hide');
  });

  it('note sessions grow with the preview, are not split, and collapse after saving', () => {
    let m = run([
      mode('notes'),
      { type: 'state', phase: 'starting', at: 3 },
      { type: 'state', phase: 'recording', at: 4 },
      { type: 'partial', text: '第一句', source: 'remote' },
      { type: 'partial', text: '第一句 第二句', source: 'remote' },
    ], { ...initial, linger: 5 });
    expect(m.view).toBe('note');
    expect(m.startedAt).toBe(4);
    expect(m.text).toBe('第一句 第二句');
    expect(m.timer).toBeNull();
    m = run([{ type: 'state', phase: 'processing', at: 5 }, { type: 'result', destination: 'notes', text: '第一句，第二句。', at: 6 }], m);
    expect(m.done).toBe(true);
    expect(m.savedAt).toBe(6);
    expect(m.timer).toEqual({ ms: 5000, then: 'collapse' });
    m = reduce(m, { type: 'timeout', token: m.token });
    expect(m.view).toBe('chip');
    expect(m.chip?.kind).toBe('saved');
    m = reduce(m, { type: 'timeout', token: m.token });
    expect(m.view).toBe('hidden');
  });

  it('a new recording clears the previous text', () => {
    let m = run([mode('notes'), { type: 'state', phase: 'recording', at: 1 }, { type: 'result', destination: 'notes', text: '旧的', at: 2 }]);
    m = run([{ type: 'state', phase: 'idle', at: 3 }, { type: 'state', phase: 'recording', at: 4 }], m);
    expect(m.text).toBe('');
    expect(m.final).toBe(false);
    expect(m.startedAt).toBe(4);
  });

  it('a switch during a session becomes a badge; pinning keeps the note open', () => {
    let m = run([mode('notes'), { type: 'state', phase: 'recording', at: 1 }, mode('notes', 'present')]);
    expect(m.view).toBe('note');
    expect(m.pending).toBe('present');
    m = run([{ type: 'pin' }, { type: 'result', destination: 'notes', text: 't', at: 2 }], m);
    expect(m.timer).toBeNull();
    m = run([{ type: 'state', phase: 'idle', at: 3 }], m);
    expect(m.view).toBe('note');
    m = reduce(m, { type: 'dismiss' });
    expect(m.view).toBe('hidden');
  });

  it('a switch right after saving replaces the lingering note with the chip', () => {
    let m = run([
      mode('notes'),
      { type: 'state', phase: 'recording', at: 1 },
      { type: 'result', destination: 'notes', text: 't', at: 2 },
      { type: 'state', phase: 'idle', at: 3 },
      mode('present'),
    ]);
    expect(m.view).toBe('chip');
    expect(m.chip?.mode).toBe('present');
    m = run([{ type: 'state', phase: 'recording', at: 4 }], m);
    expect(m.view).toBe('live');
  });

  it('a ⑦ quick note looks like a note even in input mode', () => {
    let m = run([{ type: 'state', phase: 'starting', at: 1, destination: 'notes' }, { type: 'state', phase: 'recording', at: 2, destination: 'notes' }]);
    expect(m.mode).toBe('input');
    expect(m.view).toBe('note');
    expect(m.sessionMode).toBe('notes');
    m = run([mode('notes'), { type: 'state', phase: 'recording', at: 3, destination: 'input' }]);
    expect(m.view).toBe('live');
    expect(m.sessionMode).toBe('input');
  });

  it('the drag lock is announced while held and when released, but never over a session', () => {
    let m = run([{ type: 'drag', locked: true }]);
    expect(m.chip?.kind).toBe('drag');
    expect(m.timer?.ms).toBe(15000);
    m = reduce(m, { type: 'drag', locked: false });
    expect(m.chip?.kind).toBe('drop');
    m = run([{ type: 'state', phase: 'recording', at: 1 }, { type: 'drag', locked: true }]);
    expect(m.view).toBe('live');
  });

  it('silence and empty transcripts end in a "nothing heard" chip, not a saved note', () => {
    let m = run([mode('notes'), { type: 'state', phase: 'recording', at: 1 }, { type: 'empty' }, { type: 'state', phase: 'idle', at: 2 }]);
    expect(m.view).toBe('chip');
    expect(m.chip?.kind).toBe('empty');
    m = run([{ type: 'state', phase: 'recording', at: 3, destination: 'notes' }, { type: 'result', destination: 'notes', text: '收到。转写内容为空', at: 4, skipped: true }], m);
    expect(m.chip).toMatchObject({ kind: 'empty', text: 'skipped' });
    expect(m.savedAt).toBeNull();
  });

  it('an input result with no words never reads as typed', () => {
    let m = run([{ type: 'state', phase: 'recording', at: 1 }, { type: 'state', phase: 'processing', at: 2 },
      { type: 'result', destination: 'input', text: '', at: 3, empty: true }]);
    expect(m.view).toBe('chip');
    expect(m.chip).toMatchObject({ kind: 'empty', text: 'unrecognized' });
    expect(m.done).toBe(true);
    expect(m.timer).toEqual({ ms: 1900, then: 'hide' });
    // The hotkey bridge's empty text means "handed over", not "nothing heard".
    m = run([{ type: 'state', phase: 'recording', at: 4 }, { type: 'result', destination: 'input', text: '', at: 5, empty: true, external: '闪电说' }], m);
    expect(m.view).toBe('live');
    expect(m.external).toBe('闪电说');
  });

  it('a note that could not be written never reads as saved', () => {
    let m = run([mode('notes'), { type: 'state', phase: 'recording', at: 1 }, { type: 'state', phase: 'processing', at: 2 },
      { type: 'result', destination: 'notes', text: '记一下', at: 3, noteError: true }]);
    expect(m.view).toBe('chip');
    expect(m.chip).toMatchObject({ kind: 'unsaved', mode: 'notes' });
    expect(m.text).toBe('记一下');
    expect(m.savedAt).toBeNull();
    expect(m.timer).toEqual({ ms: 4200, then: 'hide' });
    m = reduce(m, { type: 'timeout', token: m.token });
    expect(m.view).toBe('hidden');   // not the "已保存" chip of a collapsing note
  });

  it('a connection error after partial note text never claims the note was saved', () => {
    let m = run([mode('notes'), { type: 'state', phase: 'recording', at: 1 },
      { type: 'partial', text: '尚未定稿的想法', source: 'cloud' },
      { type: 'state', phase: 'error', at: 2, message: '连接已断开' }]);
    expect(m.view).toBe('chip');
    expect(m.chip).toMatchObject({ kind: 'error', text: '连接已断开' });
    expect(m.savedAt).toBeNull();
    expect(m.timer?.then).toBe('hide');
    m = reduce(m, { type: 'timeout', token: m.token });
    expect(m.view).toBe('hidden');
  });

  it('a completed pinned note remains saved across a later connection error', () => {
    const m = run([mode('notes'), { type: 'state', phase: 'recording', at: 1 },
      { type: 'pin' }, { type: 'result', destination: 'notes', text: '已保存的想法', at: 2 },
      { type: 'state', phase: 'error', at: 3, message: '连接已断开' }]);
    expect(m).toMatchObject({ view: 'note', savedAt: 2, final: true, pinned: true });
    expect(m.timer).toBeNull();
  });

  it('a dismissed note stays dismissed when whisper boosting begins mid-recording', () => {
    let m = run([mode('notes'), { type: 'state', phase: 'starting', at: 1 }, { type: 'state', phase: 'recording', at: 2 },
      { type: 'partial', text: '第一句', source: 'cloud' }, { type: 'dismiss' }]);
    expect(m.view).toBe('hidden');
    m = reduce(m, { type: 'state', phase: 'recording', at: 9, whisper: true });
    expect(m).toMatchObject({ view: 'hidden', text: '第一句', startedAt: 2, whisper: true, timer: null });
    // The next session shows its card again.
    m = run([{ type: 'result', destination: 'notes', text: '第一句。', at: 10 }, { type: 'state', phase: 'idle', at: 11 },
      { type: 'state', phase: 'recording', at: 12 }], m);
    expect(m).toMatchObject({ view: 'note', text: '', startedAt: 12, whisper: false });
  });

  it('a core that exits mid-session ends the card; a pinned note stays', () => {
    // Overlay maps core_exit to a disconnected state: the restarted core never says idle.
    let m = run([mode('notes'), { type: 'state', phase: 'recording', at: 1 }, { type: 'state', phase: 'disconnected', at: 2 }]);
    expect(m.view).toBe('hidden');
    m = run([{ type: 'state', phase: 'recording', at: 3 }, { type: 'pin' }, { type: 'state', phase: 'disconnected', at: 4 }], m);
    expect(m).toMatchObject({ view: 'note', pinned: true });
  });

  it('a wizard test and a failed recording each end on their own chip, never silently', () => {
    let m = run([{ type: 'state', phase: 'recording', at: 1 }, { type: 'state', phase: 'processing', at: 2 }, { type: 'test' },
      { type: 'state', phase: 'idle', at: 3 }]);
    expect(m.view).toBe('chip');
    expect(m.chip?.kind).toBe('test');
    expect(m.timer?.then).toBe('hide');
    m = run([{ type: 'state', phase: 'recording', at: 4 }, { type: 'state', phase: 'processing', at: 5 },
      { type: 'failed', message: '与豆包语音的连接中断，本段未完成' }, { type: 'state', phase: 'idle', at: 6 }], m);
    expect(m.chip).toMatchObject({ kind: 'failed', text: '与豆包语音的连接中断，本段未完成' });
    expect(m.phase).toBe('idle');
    m = run([{ type: 'config', enabled: false, onSwitch: true, linger: 5 }, { type: 'failed', message: 'x' }, { type: 'test' }]);
    expect(m.view).toBe('hidden');
  });

  it('whisper is a session flag: kept when later events omit it, reset by the next session', () => {
    let m = run([{ type: 'state', phase: 'starting', at: 1, whisper: true }, { type: 'state', phase: 'recording', at: 2 }]);
    expect(m.view).toBe('live');
    expect(m.whisper).toBe(true);
    m = run([{ type: 'partial', text: '小声说', source: 'remote' }, { type: 'state', phase: 'processing', at: 3 }], m);
    expect(m.whisper).toBe(true);
    m = run([{ type: 'result', destination: 'input', text: '小声说。', at: 4 }, { type: 'state', phase: 'idle', at: 5 }], m);
    expect(m.whisper).toBe(true);
    m = run([{ type: 'state', phase: 'recording', at: 6 }], m);
    expect(m.whisper).toBe(false);
    m = run([{ type: 'state', phase: 'recording', at: 7, whisper: true }], m);
    expect(m.whisper).toBe(true);
    m = run([{ type: 'state', phase: 'recording', at: 8, whisper: false }], m);
    expect(m.whisper).toBe(false);
  });

  /* A pen far from its receiver loses or repeats ⑦ reports: switches arrive in bursts,
     late, or straddling the chip's own timeout. The model must always end on the latest
     switch with a complete chip (0.10.6 "empty capsule" was the view, not the model). */
  it('a burst of switches settles on the last one; older timers are stale', () => {
    let m = run([mode('notes'), mode('present'), mode('input')]);
    expect(m).toMatchObject({ view: 'chip', mode: 'input', chip: { kind: 'mode', mode: 'input', pending: false, text: '' } });
    expect(m.timer).toEqual({ ms: 1800, then: 'hide' });
    for (const stale of [m.token - 1, m.token - 2]) expect(reduce(m, { type: 'timeout', token: stale }).view).toBe('chip');
    // A repeated report only refreshes the chip and restarts its timer.
    const before = m.token;
    m = reduce(m, mode('input'));
    expect(m.chip).toMatchObject({ kind: 'mode', mode: 'input' });
    expect(m.token).toBe(before + 1);
    expect(reduce(m, { type: 'timeout', token: before }).view).toBe('chip');
    expect(reduce(m, { type: 'timeout', token: m.token }).view).toBe('hidden');
  });

  it('a switch landing just after the chip timed out shows the new chip in full', () => {
    let m = run([mode('present')]);
    const first = m.token;
    m = reduce(m, { type: 'timeout', token: first });
    expect(m).toMatchObject({ view: 'hidden', chip: null, timer: null });
    m = run([mode('input'), { type: 'timeout', token: first }], m);
    expect(m).toMatchObject({ view: 'chip', chip: { kind: 'mode', mode: 'input', pending: false }, timer: { ms: 1800, then: 'hide' } });
    // …also when the hidden card was a tool chip, and when a second late report follows.
    m = run([{ type: 'tool', tool: 'spotlight' }], m);
    m = run([{ type: 'timeout', token: m.token }, mode('present'), mode('notes')], m);
    expect(m).toMatchObject({ view: 'chip', mode: 'notes', tool: 'spotlight', chip: { kind: 'mode', mode: 'notes' }, timer: { ms: 3200 } });
  });

  it('switches interleaved with a session: badge while busy, chip for the switch applied at the end', () => {
    let m = run([
      mode('input'),
      { type: 'state', phase: 'starting', at: 1 },
      { type: 'state', phase: 'recording', at: 2 },
      mode('input', 'notes'), // ⑦ while recording: deferred
      mode('input', 'present'), // repeated report cycles the deferred target
    ]);
    expect(m).toMatchObject({ view: 'live', chip: null, mode: 'input', pending: 'present' });
    m = run([
      { type: 'state', phase: 'processing', at: 3 },
      { type: 'result', destination: 'input', text: '好。', at: 4 },
      { type: 'state', phase: 'idle', at: 5 },
      mode('present'), // the core applies the deferred switch once idle
    ], m);
    expect(m).toMatchObject({ view: 'chip', mode: 'present', pending: null, chip: { kind: 'mode', mode: 'present', pending: false } });
    // A new recording right after takes over from the chip at once.
    m = run([{ type: 'state', phase: 'recording', at: 6 }], m);
    expect(m).toMatchObject({ view: 'live', chip: null, sessionMode: 'present', text: '' });
  });

  it('random bursts of core events never leave a chip without copy, and the last switch wins', () => {
    let seed = 20261012;
    const rand = () => ((seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648);
    const pick = <T,>(xs: readonly T[]) => xs[Math.floor(rand() * xs.length)];
    const MODES = ['input', 'notes', 'present'] as const;
    const KINDS = ['mode', 'tool', 'saved', 'error', 'drag', 'drop', 'empty'];
    const next = (m: Model): Event => {
      const r = rand();
      if (r < .35) return mode(pick(MODES), rand() < .2 ? pick(MODES) : null);
      if (r < .42) return { type: 'tool', tool: pick(['mouse', 'laser', 'spotlight', 'magnifier'] as const) };
      if (r < .66) return { type: 'state', phase: pick(['starting', 'recording', 'processing', 'idle', 'idle', 'disconnected', 'error'] as const), at: 1, destination: rand() < .3 ? 'notes' : undefined };
      if (r < .74) return { type: 'result', destination: pick(['input', 'notes'] as const), text: 't', at: 2, skipped: rand() < .15 };
      if (r < .8) return { type: 'empty' };
      if (r < .85) return { type: 'drag', locked: rand() < .5 };
      if (r < .9) return { type: 'partial', text: 'p', source: 'remote' };
      if (r < .93) return { type: 'pin' };
      return { type: 'timeout', token: rand() < .7 ? m.token : m.token - 1 };
    };
    for (let i = 0; i < 400; i++) {
      let m: Model = initial;
      for (let k = 0; k < 24; k++) {
        m = reduce(m, next(m));
        if (m.view === 'hidden') expect(m.chip).toBeNull();
        if (m.view === 'chip') {
          expect(KINDS).toContain(m.chip?.kind);
          expect(MODES).toContain(m.chip?.mode);
          expect(typeof m.chip?.text).toBe('string');
        }
      }
      // Whatever came before, a switch outside a running session ends on its chip.
      const busy = (m.view === 'live' || m.view === 'note') && (!m.done || m.pinned);
      const last = pick(MODES);
      const end = reduce(m, mode(last));
      if (!busy) expect(end).toMatchObject({ view: 'chip', mode: last, chip: { kind: 'mode', mode: last } });
      else expect(end).toMatchObject({ view: m.view, mode: last });
    }
  });

  it('retired local or missing-source previews never appear, but final results do', () => {
    let m = run([{ type: 'state', phase: 'recording', at: 1 }, { type: 'partial', text: '创建一个', source: 'local' }]);
    expect(m.view).toBe('live');
    expect(m.text).toBe('');
    expect(shownText(m)).toBe('');
    m = reduce(m, { type: 'partial', text: '没有来源' });
    expect(m.source).toBeNull();
    m = reduce(m, { type: 'result', destination: 'input', text: '创建一个函数。', at: 3 });
    expect(shownText(m)).toBe('创建一个函数。');
  });

  it('OpenLess and cloud streams appear in a note and ignore late legacy previews', () => {
    for (const source of ['remote', 'cloud']) {
      const m = run([mode('notes'), { type: 'state', phase: 'recording', at: 1 }, { type: 'partial', text: '第一句', source }]);
      expect(m.view).toBe('note');
      expect(shownText(m)).toBe('第一句');
      expect(reduce(m, { type: 'partial', text: '迟到的猜测', source: 'local' })).toBe(m);
    }
  });

  it('the same explicit pin request from two screens cannot toggle the note back', () => {
    const m = run([mode('notes'), { type: 'state', phase: 'recording', at: 1 }, { type: 'pin', value: true }]);
    expect(m.pinned).toBe(true);
    expect(reduce(m, { type: 'pin', value: true })).toBe(m);
  });

  it('a partial arriving before the startup snapshot still restores the running note', () => {
    const m = run([{ type: 'partial', text: '比快照更新的实时文字', source: 'cloud' }, { type: 'snapshot', at: 100,
      snapshot: { destination: 'notes', pending_mode: null, armed: true, tool: 'laser',
        state: { phase: 'recording', message: '' }, live_text: '旧文字', live_source: 'cloud' },
      seen: { mode: false, state: false, tool: false, partial: true } }]);
    expect(m).toMatchObject({ view: 'note', phase: 'recording', mode: 'notes', armed: true, startedAt: 100,
      text: '比快照更新的实时文字', source: 'cloud' });
  });

  it('startup snapshots do not rewind newer mode, tool, phase or words', () => {
    const m = run([mode('notes'), { type: 'tool', tool: 'mouse' }, { type: 'state', phase: 'recording', at: 100 },
      { type: 'partial', text: '新文字', source: 'remote' }, { type: 'snapshot', at: 200,
        snapshot: { destination: 'input', pending_mode: null, armed: false, tool: 'laser',
          state: { phase: 'disconnected', message: '' }, live_text: '', live_source: null },
        seen: { mode: true, state: true, tool: true, partial: true } }]);
    expect(m).toMatchObject({ view: 'note', phase: 'recording', mode: 'notes', tool: 'mouse', armed: true,
      startedAt: 100, text: '新文字' });
  });

  it('a processing snapshot restores a card without pretending the note is already saved', () => {
    const m = run([{ type: 'snapshot', at: 100,
      snapshot: { destination: 'input', pending_mode: null, armed: true, tool: 'laser',
        state: { phase: 'processing', destination: 'notes', message: '' }, live_text: '正在整理', live_source: 'remote' },
      seen: { mode: false, state: false, tool: false, partial: false } }]);
    expect(m).toMatchObject({ view: 'note', phase: 'processing', done: false, savedAt: null, text: '正在整理' });
  });

  it('queued stale mirror actions cannot pin or dismiss the new recording in the same batch', () => {
    const old = run([mode('notes'), { type: 'state', phase: 'recording', at: 1 },
      { type: 'result', destination: 'notes', text: '前一段', at: 2 }, { type: 'state', phase: 'idle', at: 3 }]);
    const expected = { session: old.startedAt, token: old.token };
    for (const action of [{ type: 'dismiss', expected }, { type: 'pin', value: true, expected }] as Event[]) {
      const current = run([{ type: 'state', phase: 'starting', at: 4 }, { type: 'state', phase: 'recording', at: 5 }], old);
      expect(reduce(current, action)).toBe(current);
      expect(current).toMatchObject({ view: 'note', phase: 'recording', startedAt: 5, pinned: false });
    }
  });

  it('a final note retains a bounded copy of its waveform for newly attached screens', () => {
    const envelope = Array.from({ length: 900 }, (_, i) => i / 900);
    const m = run([mode('notes'), { type: 'state', phase: 'recording', at: 1 },
      { type: 'result', destination: 'notes', text: '记住这个想法。', at: 2, envelope }]);
    expect(m.envelope).toEqual(envelope.slice(0, 600));
    envelope[0] = 1;
    expect(m.envelope?.[0]).toBe(0);
    const next = run([{ type: 'state', phase: 'idle', at: 3 }, { type: 'state', phase: 'recording', at: 4 }], m);
    expect(next.envelope).toBeUndefined();
  });

  it('a session handed to another dictation app says so, and the next one forgets it', () => {
    let m = run([{ type: 'state', phase: 'recording', at: 1 }, { type: 'state', phase: 'processing', at: 2 },
      { type: 'result', destination: 'input', text: '', at: 3, external: '闪电说' }]);
    expect(m.done).toBe(true);
    expect(m.external).toBe('闪电说');
    expect(m.view).toBe('live');
    m = run([{ type: 'state', phase: 'idle', at: 4 }, { type: 'timeout', token: m.token }, { type: 'state', phase: 'recording', at: 5 }], m);
    expect(m.external).toBeUndefined();
    m = reduce(m, { type: 'result', destination: 'input', text: '自己输入的字。', at: 6 });
    expect(m.external).toBeUndefined();
  });

  it('an administrator window that dropped a click gets its own chip, never over a session', () => {
    let m = run([{ type: 'elevated' }]);
    expect(m.view).toBe('chip');
    expect(m.chip).toMatchObject({ kind: 'elevated' });
    expect(m.timer).toEqual({ ms: 5200, then: 'hide' });
    m = reduce(m, { type: 'timeout', token: m.token });
    expect(m.view).toBe('hidden');
    // Mid-recording: the session card stays; the notice follows once it has gone.
    m = run([{ type: 'state', phase: 'recording', at: 1 }, { type: 'elevated' }], m);
    expect(m.view).toBe('live');
    expect(m.blockedLater).toBe(true);
    m = run([{ type: 'result', destination: 'input', text: '好的。', at: 2 }, { type: 'state', phase: 'idle', at: 3 }], m);
    expect(m.view).toBe('live');
    m = reduce(m, { type: 'timeout', token: m.token });
    expect(m.chip).toMatchObject({ kind: 'elevated' });
    expect(m.blockedLater).toBe(false);
    m = reduce(m, { type: 'timeout', token: m.token });
    expect(m.view).toBe('hidden');
    // Cancelled sessions forget it.
    m = run([{ type: 'state', phase: 'recording', at: 4 }, { type: 'elevated' }, { type: 'state', phase: 'disconnected', at: 5 }], m);
    expect(m.view).toBe('hidden');
    expect(m.blockedLater).toBe(false);
    expect(run([{ type: 'config', enabled: false, onSwitch: true, linger: 5 }, { type: 'elevated' }]).view).toBe('hidden');
  });

  it('text an administrator window refused never reads as typed', () => {
    let m = run([{ type: 'state', phase: 'recording', at: 1 }, { type: 'state', phase: 'processing', at: 2 },
      { type: 'result', destination: 'input', text: '我要评论', at: 3, notTyped: true, copied: true }]);
    expect(m.view).toBe('chip');
    expect(m.chip).toMatchObject({ kind: 'kept', text: 'copied' });
    expect(m.text).toBe('我要评论');
    expect(m.timer).toEqual({ ms: 6000, then: 'hide' });
    m = run([{ type: 'result', destination: 'input', text: '我要评论', at: 4, notTyped: true, copied: false }]);
    expect(m.chip).toMatchObject({ kind: 'kept', text: '' });
    // A note is saved, not typed: never 'kept'.
    m = run([{ type: 'result', destination: 'notes', text: '记一下', at: 5, notTyped: true }]);
    expect(m.chip?.kind).toBe('saved');
  });

  it('cancelling hides at once and a disabled capsule never appears', () => {
    let m = run([{ type: 'state', phase: 'recording', at: 1 }, { type: 'state', phase: 'disconnected', at: 2 }]);
    expect(m.view).toBe('hidden');
    m = run([{ type: 'config', enabled: false, onSwitch: true, linger: 5 }, mode('notes'), { type: 'state', phase: 'recording', at: 3 }]);
    expect(m.view).toBe('hidden');
  });
});
