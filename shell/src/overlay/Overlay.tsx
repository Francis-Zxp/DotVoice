/* The capsule. The window stays visible (transparent, click-through); only the
   card animates, with real width/height springs so text never stretches.

   Material: liquid glass, in layers — frosted fallback → captured backdrop
   (blurred body + lensing rim, crossfaded in) → adaptive tint → mode light
   → content → specular rim that follows the pill. The backdrop is copied only
   while the card is invisible (before a reveal, and again just after a hide);
   the card never waits more than ~2 frames for it.

   Motion (Apple-style springs in duration/bounce terms): the card emerges
   from a small pill, morphs between chip / live / note (height trails width
   when growing, leads it when shrinking), content cross-blurs, the icon morphs
   and the title rolls on switches, light travels around the rim, and
   "nothing heard"/errors shake gently. Reduced motion: short fades only. */
import { getCurrentWindow } from '@tauri-apps/api/window';
import { AnimatePresence, motion, MotionConfig, useAnimate } from 'motion/react';
import {
  AppWindow, Copy, Crosshair, FlaskConical, Hand, Keyboard, Mic, MicOff, MousePointer2, NotebookPen, Pin, PinOff, Presentation,
  Search, ShieldAlert, Sun, TriangleAlert, Wind, X,
} from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { VoiceScore } from '../components/VoiceScore';
import { call, inTauri, isMock, shell } from '../lib/bridge';
import { blockedChip } from '../lib/elevation';
import { clock, timer } from '../lib/format';
import { handedTo } from '../lib/engine';
import { levels, MODE_INFO, TOOL_INFO } from '../lib/store';
import type { Mode, Snapshot, Tool } from '../lib/types';
import { captureBackdrop, paintPreviewDesktop, sampleTone, type Tone } from './backdrop';
import { LiveWave } from './LiveWave';
import { isBusy, shownText, type Model } from './machine';
import { alignment, cardPosition } from './layout';
import { useOverlayModel } from './useOverlayModel';

const MODE_ICON: Record<Mode, typeof Mic> = { input: Keyboard, notes: NotebookPen, present: Presentation };
const TOOL_ICON: Record<Tool, typeof Mic> = { mouse: MousePointer2, laser: Crosshair, spotlight: Sun, magnifier: Search };
const EASE = [.32, .72, 0, 1] as const;
const spring = (visualDuration: number, bounce: number) => ({ type: 'spring' as const, visualDuration, bounce });
const SHAPE = spring(.44, .26);       // width: lively, a little gel overshoot
const SHAPE_SOFT = spring(.5, .16);   // height / radius: calmer
const POP = spring(.36, .4);          // icon morph
const ROLL = spring(.4, .14);         // title roll
const QUICK = { duration: .18, ease: EASE };
/* Every motion element with an `exit` must also say where it returns to (`animate`).
   When the next event lands while the card is fading out, AnimatePresence brings the
   same card back; once an element's own exit has finished, motion replays its enter
   from `initial` to `animate`, and an element without `animate` keeps its exit values
   (0.10.6: an empty glass pill after a quick mode switch). SHOWN is that state for
   content that fades and blurs out; the filter is dropped once it is sharp. */
const SHOWN = { opacity: 1, filter: 'blur(0px)', transitionEnd: { filter: 'none' } };
const FAST_CAPTURE_MS = 34;  // ~2 frames: wait this long for the backdrop, then show the fallback
const LATE_CAPTURE_MS = 48;  // a capture landing later may contain the (fading-in) card: discard
const STALE_MS = 8000;       // reuse a previous backdrop on quick re-entry only while fresh
/* Live capsule widths: with words, and the narrower waveform-only pill it starts as when
   no trustworthy words are coming (it springs wider when they do). */
const LIVE_W = 364, LIVE_WHISPER_W = 404, WAVE_W = 292, WAVE_WHISPER_W = 318;
const sleep = (ms: number) => new Promise<null>((done) => setTimeout(() => done(null), ms));

type Glass = { image: HTMLCanvasElement; id: number; at: number };
let glassSeq = 0;

function Elapsed({ since }: { since: number | null }) {
  const [, tick] = useState(0);
  useEffect(() => {
    if (since == null) return;
    const t = window.setInterval(() => tick((n) => n + 1), 500);
    return () => clearInterval(t);
  }, [since]);
  return <span className="num">{since == null ? '0:00' : timer(Date.now() - since)}</span>;
}

/* Chip copy, shared by the view and the width measurement. `detail`: a second hint line
   (the administrator-window chips say what happened and where to change it). */
interface ChipCopy { icon: ReactNode; iconKey: string; title: string; hint: string; detail?: string; tone?: string; fill: string }
function chipCopy(model: Model): ChipCopy {
  const copy = chipText(model);
  return { ...copy, fill: copy.tone ?? `var(--${model.chip!.mode})` };
}
function chipText(model: Model): Omit<ChipCopy, 'fill'> {
  const chip = model.chip!;
  if (chip.kind === 'saved') return { icon: <DrawCheck />, iconKey: 'saved', title: '已保存', hint: '在笔记页查看、打标签', tone: 'var(--ok)' };
  if (chip.kind === 'drag' || chip.kind === 'drop') {
    const held = chip.kind === 'drag';
    return {
      icon: <Hand size={18} strokeWidth={2.2} />, iconKey: chip.kind, title: held ? '已按住左键' : '已松开左键',
      hint: held ? '按住 ⑤ 移动来框选、拖动 · 再按 ③ 放下' : '框选 / 拖动完成',
    };
  }
  if (chip.kind === 'empty') {
    const skipped = chip.text === 'skipped', unrecognized = chip.text === 'unrecognized';
    return {
      icon: <MicOff size={18} strokeWidth={2.2} />, iconKey: 'empty', tone: 'var(--lg-muted-orb)',
      title: skipped ? '没有识别到内容' : unrecognized ? '没有识别出文字' : '没听到说话',
      hint: skipped ? '这段没有保存为笔记' : unrecognized ? '没有输入任何内容' : '本段已取消，没有输入也没有保存',
    };
  }
  if (chip.kind === 'unsaved') return { icon: <TriangleAlert size={18} strokeWidth={2.2} />, iconKey: 'unsaved', title: '笔记未能保存', hint: '请在主窗口复制本次文字', tone: 'var(--bad)' };
  if (chip.kind === 'error') return { icon: <TriangleAlert size={18} strokeWidth={2.2} />, iconKey: 'error', title: '连接异常', hint: chip.text, tone: 'var(--bad)' };
  if (chip.kind === 'failed') return { icon: <TriangleAlert size={18} strokeWidth={2.2} />, iconKey: 'failed', title: '这段没有识别成功', hint: chip.text, tone: 'var(--bad)' };
  if (chip.kind === 'test') {
    return { icon: <FlaskConical size={18} strokeWidth={2.2} />, iconKey: 'test', title: '试说完成', hint: '配置向导的试说：文字只显示在向导里，没有输入', tone: 'var(--lg-muted-orb)' };
  }
  if (chip.kind === 'elevated' || chip.kind === 'kept') {
    const [c, reason] = chip.text.split(':');
    return { icon: <ShieldAlert size={18} strokeWidth={2.2} />, iconKey: chip.kind, tone: 'var(--warn)', ...blockedChip(chip.kind, c === 'copied', reason || 'elevated') };
  }
  if (chip.kind === 'tool') {
    const Icon = TOOL_ICON[model.tool];
    return {
      icon: <Icon size={18} strokeWidth={2.2} />, iconKey: 'tool-' + model.tool, title: TOOL_INFO[model.tool],
      hint: model.tool === 'mouse' ? '只移动光标 · 双击 ⑤ 换工具' : '按住 ⑤ 使用 · 双击 ⑤ 换工具',
    };
  }
  const Icon = MODE_ICON[chip.mode];
  return {
    icon: <Icon size={18} strokeWidth={2.2} />, iconKey: 'mode-' + chip.mode,
    title: chip.mode === 'present' ? `演示 · ${TOOL_INFO[model.tool]}` : MODE_INFO[chip.mode].label,
    hint: chip.pending ? '本段结束后切换' : !model.armed && chip.mode !== 'present' ? '语音输入已暂停' : MODE_INFO[chip.mode].hint,
  };
}

/* The chip hugs its words, like the island hugs its content. (Canvas font
   shorthand only takes standard weights, so 600/400 stand in for 650/450.) */
let measurer: { ctx: CanvasRenderingContext2D; title: string; hint: string } | null = null;
function chipWidth(copy: Pick<ChipCopy, 'title' | 'hint' | 'detail'>) {
  if (!measurer) {
    const ctx = document.createElement('canvas').getContext('2d');
    if (!ctx) return 260;
    const css = getComputedStyle(document.documentElement);
    measurer = { ctx, title: `600 15px ${css.getPropertyValue('--font-display').trim() || 'Segoe UI'}`, hint: `400 11.5px ${css.getPropertyValue('--font').trim() || 'Segoe UI'}` };
  }
  const { ctx } = measurer;
  ctx.font = measurer.title;
  const title = ctx.measureText(copy.title).width;
  ctx.font = measurer.hint;
  const hint = Math.max(ctx.measureText(copy.hint).width, copy.detail ? ctx.measureText(copy.detail).width : 0) * 1.01;
  // The two-line administrator chips may be wider: their whole sentence must be readable.
  return Math.round(Math.min(copy.detail ? 432 : 356, Math.max(212, 10 + 38 + 12 + Math.max(title, hint) + 24 + 6)));
}

export function Overlay() {
  const [reduced, setReduced] = useState(false);
  const [noteHeight, setNoteHeight] = useState(184);
  const [glass, setGlass] = useState<Glass | null>(null);
  const [revealed, setRevealed] = useState(false);
  const exiting = useRef(false);
  const showing = useRef(0); // bumps on every hide: late async work from an older showing is ignored
  const glassRef = useRef<Glass | null>(null);
  glassRef.current = glass;
  const card = useRef<HTMLDivElement>(null);
  const noteBody = useRef<HTMLDivElement>(null);
  const [squash, animateSquash] = useAnimate();
  const appearance = useRef<Snapshot['prefs'] | null>(null);

  const applyAppearance = useCallback((prefs: Snapshot['prefs']) => {
    appearance.current = prefs;
    const root = document.documentElement;
    const still = prefs.appearance.reduced_motion || matchMedia('(prefers-reduced-motion: reduce)').matches;
    setReduced(still);
    root.classList.toggle('reduced', still);
    if (prefs.appearance.theme === 'system') delete root.dataset.theme;
    else root.dataset.theme = prefs.appearance.theme;
    if (isMock) {
      const dark = prefs.appearance.theme === 'glass' || prefs.appearance.theme === 'dark' || (prefs.appearance.theme === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
      paintPreviewDesktop(dark);
    }
  }, []);
  // MotionConfig gets an explicit always/never: follow the system setting while running.
  useEffect(() => {
    const query = matchMedia('(prefers-reduced-motion: reduce)');
    const follow = () => { if (appearance.current) applyAppearance(appearance.current); };
    query.addEventListener('change', follow);
    return () => query.removeEventListener('change', follow);
  }, [applyAppearance]);
  const { model, layout, action } = useOverlayModel(applyAppearance);
  const anchor = layout?.anchor ?? 'top-center';
  const align = alignment(anchor);
  const currentAnchor = useRef(anchor);
  currentAnchor.current = anchor;
  const syncGlass = useRef<() => void>(() => {});

  // The copied desktop stays aligned while the card springs, at every anchor.
  const attachCard = useCallback((el: HTMLDivElement | null) => {
    card.current = el;
    if (!el) return;
    const sync = () => {
      const w = el.offsetWidth, h = el.offsetHeight;
      const point = cardPosition(currentAnchor.current, w, h, document.documentElement.clientWidth, document.documentElement.clientHeight);
      el.style.setProperty('--bx', String(point.x));
      el.style.setProperty('--by', String(point.y));
      el.style.setProperty('--cw', String(w));
      el.style.setProperty('--ch', String(h));
    };
    syncGlass.current = sync;
    sync();
    const observer = new ResizeObserver(sync);
    observer.observe(el);
    window.addEventListener('resize', sync);
    return () => { observer.disconnect(); window.removeEventListener('resize', sync); card.current = null; syncGlass.current = () => {}; };
  }, []);
  useLayoutEffect(() => { syncGlass.current(); }, [anchor]);

  // Warm the CJK fonts once at load (canvas measure + DOM text), so the very first
  // reveal never stalls on font loading.
  useEffect(() => {
    const warm = () => chipWidth({ title: '输入笔记演示已保存', hint: '说完插入当前编辑框 · 按住 ⑤ 使用 ③ 正在听… 说完出字 · 正在识别' });
    const idle = (window as any).requestIdleCallback?.(warm, { timeout: 1500 }) ?? window.setTimeout(warm, 300);
    return () => { (window as any).cancelIdleCallback?.(idle); clearTimeout(idle); };
  }, []);

  /* Reveal at once. If the card is fully gone, copy the backdrop first and use it
     when it lands within ~2 frames; otherwise show the frosted fallback and
     crossfade the copy in if it lands before the card is visibly there. While
     the previous card is still fading out it must not be copied: re-enter with
     the last backdrop if it is fresh. */
  const hidden = model.view === 'hidden' || !layout?.active;
  useEffect(() => {
    if (hidden) {
      showing.current++;
      if (revealed) exiting.current = true;
      setRevealed(false);
      return;
    }
    if (revealed) return;
    const token = ++showing.current;
    const fresh = () => glassRef.current && performance.now() - glassRef.current.at < STALE_MS;
    if (exiting.current) {
      if (!fresh()) setGlass(null);
      // The fading card comes straight back (AnimatePresence re-enters it), so its
      // exit never completes: it is on screen again, no longer "exiting".
      exiting.current = false;
      setRevealed(true);
      return;
    }
    let shownAt = 0;
    const accept = (image: HTMLCanvasElement | null) => {
      if (!image || token !== showing.current) return;
      if (shownAt && performance.now() - shownAt > LATE_CAPTURE_MS) return;
      setGlass({ image, id: ++glassSeq, at: performance.now() });
    };
    const capture = captureBackdrop();
    Promise.race([capture, sleep(FAST_CAPTURE_MS)]).then((quick) => {
      if (token !== showing.current) return;
      if (quick) accept(quick);
      else {
        if (!fresh()) setGlass(null);
        capture.then(accept);
      }
      shownAt = performance.now();
      setRevealed(true);
    });
  }, [hidden, revealed]);

  /* Once the card has fully faded out (plus two frames), copy the backdrop again so
     a quick re-show (mode switches, the next session) has fresh glass at once even
     if its own copy is slow. Dropped if a new showing starts meanwhile. */
  const refreshWhileHidden = () => {
    exiting.current = false;
    // Fully gone: nothing on screen may take clicks.
    shell.overlayInteractive(null);
    const token = showing.current;
    window.setTimeout(() => {
      if (token !== showing.current || exiting.current) return;
      captureBackdrop().then((image) => {
        if (image && token === showing.current) setGlass({ image, id: ++glassSeq, at: performance.now() });
      });
    }, 60);
  };

  // The note grows with its text, up to a comfortable reading height.
  useLayoutEffect(() => {
    const body = noteBody.current;
    if (!body) return;
    const measure = () => setNoteHeight(Math.min(470, Math.max(150, body.scrollHeight)));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(body);
    return () => observer.disconnect();
  }, [model.view, revealed]);

  const report = useCallback(() => {
    if (!hidden && revealed && card.current) {
      const r = card.current.getBoundingClientRect();
      shell.overlayInteractive({ x: r.left, y: r.top, width: r.width, height: r.height });
    } else shell.overlayInteractive(null);
  }, [hidden, revealed]);
  // The exiting card keeps the props of its last render: its animation callback must read
  // the current state, or the end of its exit registers a click region after the hide.
  const reportRef = useRef(report);
  reportRef.current = report;
  useLayoutEffect(() => {
    report();
    window.addEventListener('resize', report);
    return () => window.removeEventListener('resize', report);
  }, [report, model.view, noteHeight, model.done, anchor, layout?.monitor]);

  useEffect(() => {
    if (!inTauri) return;
    let cancelled = false;
    const stops: (() => void)[] = [];
    const register = (stop: () => void) => { if (cancelled) stop(); else stops.push(stop); };
    // Native drag-end handles persistence. Automatic moves/DPI changes only
    // invalidate the copied desktop pixels, never become a saved manual position.
    getCurrentWindow().onMoved(() => setGlass(null)).then(register);
    getCurrentWindow().onScaleChanged(() => setGlass(null)).then(register);
    return () => { cancelled = true; stops.forEach((stop) => stop()); };
  }, []);

  const mode: Mode = model.view === 'chip' ? model.chip!.mode : model.view === 'note' ? 'notes' : model.view === 'live' ? model.sessionMode : model.mode;
  const listeningNote = model.view === 'note' && model.phase === 'recording' && !model.done;
  const copy = model.view === 'chip' ? chipCopy(model) : null;
  const waveOnly = model.view === 'live' && !model.done && !shownText(model);
  const target = model.view === 'note' ? { w: 412, h: noteHeight, r: 26 }
    : model.view === 'live' ? { w: waveOnly ? (model.whisper ? WAVE_WHISPER_W : WAVE_W) : (model.whisper ? LIVE_WHISPER_W : LIVE_W), h: 54, r: 27 }
      : { w: copy ? chipWidth(copy) : 252, h: copy?.detail ? 74 : 58, r: copy?.detail ? 26 : 29 };
  const contentKey = model.view === 'chip' ? 'chip' : model.view;
  const pulseKey = `${model.view}-${model.chip?.kind}-${model.chip?.mode}-${model.tool}-${model.done}`;

  // Shape springs: height trails width while growing and leads it while shrinking,
  // so the card never passes through a tall, thin silhouette.
  const lastShape = useRef(target);
  const growing = target.h > lastShape.current.h + 1;
  const shrinking = target.h < lastShape.current.h - 1;
  useEffect(() => { lastShape.current = target; });
  const shape = reduced
    ? { width: QUICK, height: QUICK, borderRadius: QUICK }
    : {
      width: shrinking ? { ...SHAPE, delay: .05 } : SHAPE,
      height: growing ? { ...SHAPE_SOFT, delay: .05 } : SHAPE_SOFT,
      borderRadius: growing ? { ...SHAPE_SOFT, delay: .05 } : SHAPE_SOFT,
    };

  // Adaptive material: how bright and how busy the scene under the card is.
  const tone = useMemo<Tone | null>(() => glass
    ? sampleTone(glass.image, { ...cardPosition(anchor, target.w, target.h, innerWidth, innerHeight), w: target.w, h: target.h })
    : null, [glass, target.w, target.h, anchor]);

  // Feedback on every change while visible: a soft squash on switches, a
  // gentle shake for "nothing heard"/errors, a small lift for success.
  const lastPulse = useRef('');
  useEffect(() => {
    const previous = lastPulse.current;
    lastPulse.current = revealed ? pulseKey : '';
    if (!revealed || !previous || previous === pulseKey || reduced || !squash.current) return;
    const sameView = previous.split('-')[0] === model.view;
    const kind = model.chip?.kind;
    if (model.view === 'chip' && (kind === 'empty' || kind === 'error' || kind === 'failed' || kind === 'unsaved' || kind === 'elevated' || kind === 'kept')) {
      animateSquash(squash.current, { x: [0, -7, 6, -3.5, 1.5, 0] }, { duration: .5, ease: 'easeOut' });
    } else if (kind === 'saved' || (model.view === 'live' && model.done)) {
      animateSquash(squash.current, { scale: [1, 1.035, .995, 1] }, { duration: .5, ease: 'easeOut' });
    } else if (sameView) {
      animateSquash(squash.current, { scaleX: [1, 1.03, .992, 1], scaleY: [1, .955, 1.012, 1] }, { duration: .48, ease: 'easeOut' });
    }
  }, [pulseKey, revealed, reduced, animateSquash, squash, model.view, model.chip?.kind, model.done]);

  // Light follows the pointer over the (interactive) note card.
  const light = (e: React.PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    e.currentTarget.style.setProperty('--px', `${e.clientX - r.left}px`);
    e.currentTarget.style.setProperty('--py', `${e.clientY - r.top}px`);
  };

  const materialStyle = {
    ...(tone ? { '--bg-l': tone.light.toFixed(3), '--bg-busy': tone.busy.toFixed(3) } : {}),
    ...(copy?.tone ? { '--tone': copy.tone } : model.view === 'live' && model.done ? { '--tone': 'var(--ok)' } : {}),
  } as React.CSSProperties;

  return (
    <MotionConfig reducedMotion={reduced ? 'always' : 'never'}>
      <div aria-hidden="true" className="pointer-events-none fixed -left-[999px] top-0 whitespace-nowrap opacity-0"
        style={{ fontFamily: 'var(--font-display)', fontWeight: 650, fontSize: 15 }}>
        输入笔记演示已保存 <span style={{ fontFamily: 'var(--font)', fontWeight: 450, fontSize: 11.5 }}>说完插入当前编辑框 · 按住 ⑤ 使用 ③ 正在听…0:12 说完出字 · 正在识别</span>
      </div>
      <div className="fixed inset-0 flex p-3" data-overlay-anchor={anchor}
        style={{ justifyContent: align.x === 0 ? 'flex-start' : align.x === 1 ? 'flex-end' : 'center',
          alignItems: align.y === 0 ? 'flex-start' : align.y === 1 ? 'flex-end' : 'center' }}>
        <AnimatePresence onExitComplete={refreshWhileHidden}>
          {revealed && !hidden && (
            <motion.div key="presence" style={{ transformOrigin: `${align.x * 100}% ${align.y * 100}%` }}
              initial={{ opacity: 0, y: -8, scale: .92 }}
              animate={{ opacity: 1, y: 0, scale: 1, transition: reduced ? QUICK : { ...SHAPE, opacity: { duration: .2, ease: [.4, 0, .6, 1] } } }}
              exit={{ opacity: 0, y: -6, scale: .9, transition: reduced ? QUICK : { duration: .26, ease: EASE, delay: .08 } }}>
              <div ref={squash} style={{ transformOrigin: '50% 45%' }}>
                <motion.div ref={attachCard} role="status" aria-live="polite"
                  className={`capsule view-${model.view} mode-${mode} ${listeningNote ? 'tinted' : ''} ${glass ? 'has-backdrop' : ''}`}
                  style={materialStyle}
                  onPointerDown={model.view !== 'note' && inTauri ? (event) => { if (event.button === 0) void getCurrentWindow().startDragging().catch(console.error); } : undefined}
                  onPointerMove={model.view === 'note' ? light : undefined}
                  initial={{ width: 64, height: 34, borderRadius: 17 }}
                  animate={{ width: target.w, height: target.h, borderRadius: target.r }}
                  exit={{ width: 72, height: 34, borderRadius: 17, transition: reduced ? QUICK : { duration: .32, ease: EASE } }}
                  transition={shape}
                  onAnimationComplete={() => reportRef.current()}>
                  <GlassLayers glass={glass} thick={model.view === 'note'} />
                  <div className="lg-tint" />
                  <div className="lg-tint-mode" />
                  <div className="lg-gloss" />
                  <div className="lg-glow" aria-hidden="true" />
                  {model.view === 'note' && <div className="lg-light" aria-hidden="true" />}
                  {/* Content materialises after the shape starts opening (also on first show),
                      and on hide it blurs away before the shape contracts. If the card is
                      brought back mid-exit, the stage materialises again like fresh content. */}
                  <motion.div className="lg-stage" initial={false}
                    animate={{ ...SHOWN, transition: reduced ? QUICK : { delay: .1, duration: .34, ease: EASE } }}
                    exit={{ opacity: 0, filter: 'blur(5px)', transition: { duration: .1, ease: 'linear' } }}>
                  <AnimatePresence>
                    <motion.div key={contentKey} className="lg-content" style={{ width: target.w, marginLeft: -target.w / 2 }}
                      initial={{ opacity: 0, y: model.view === 'note' ? -4 : 4, scale: .96, filter: 'blur(8px)' }}
                      animate={{ opacity: 1, y: 0, scale: 1, filter: 'blur(0px)', transition: reduced ? QUICK : { delay: .1, duration: .34, ease: EASE } }}
                      exit={{ opacity: 0, scale: .97, filter: 'blur(5px)', transition: { duration: .13, ease: 'easeIn' } }}>
                      {model.view === 'chip' && copy && <ChipView copy={copy} />}
                      {model.view === 'live' && <LiveView model={model} reduced={reduced} />}
                      {model.view === 'note' && (
                        <NoteView model={model} bodyRef={noteBody} reduced={reduced} listening={listeningNote}
                          onPin={() => action('pin')} onClose={() => action('dismiss')} />
                      )}
                    </motion.div>
                  </AnimatePresence>
                  </motion.div>
                  <div className="lg-rim" />
                  {/* Light travels around the rim on every change (remount restarts it). */}
                  <div key={reduced ? 'still' : pulseKey} className={`lg-spec ${reduced ? '' : 'sweep'}`} />
                </motion.div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </MotionConfig>
  );
}

/* Blurred body + lensing rim from the copied backdrop. The canvases are laid
   over the whole window and shifted so they line up with the screen; a newer
   copy fades in over the older one. */
function GlassLayers({ glass, thick }: { glass: Glass | null; thick: boolean }) {
  return (
    <AnimatePresence initial={false}>
      {glass && (
        <motion.div key={glass.id} className="lg-glass" aria-hidden="true"
          initial={{ opacity: 0 }} animate={{ opacity: 1, transition: { duration: .26, ease: 'easeOut' } }}
          exit={{ opacity: 0, transition: { duration: .2, delay: .26 } }}>
          <GlassImage image={glass.image} kind="lens" thick={thick} />
          <div className="lg-body"><GlassImage image={glass.image} kind="base" thick={thick} /></div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/* The material's blur/saturation is baked into the pixels once per copy (the
   CSS filter would be re-rasterised on every frame of a size spring); the
   canvas then only moves, by transform, on the compositor. Larger cards are a
   thicker material (Liquid Glass: deeper blur, more tint), so the note re-bakes
   its body when it grows out of a chip. */
function GlassImage({ image, kind, thick }: { image: HTMLCanvasElement; kind: 'lens' | 'base'; thick: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useLayoutEffect(() => {
    const c = ref.current;
    if (!c) return;
    const pad = 16;
    const { width: w, height: h } = image;
    // Clamp the edges so the blur never pulls in transparency.
    const src = document.createElement('canvas');
    src.width = w + pad * 2;
    src.height = h + pad * 2;
    const s = src.getContext('2d')!;
    s.drawImage(image, pad, pad);
    s.drawImage(image, 0, 0, w, 1, pad, 0, w, pad);
    s.drawImage(image, 0, h - 1, w, 1, pad, h + pad, w, pad);
    s.drawImage(src, pad, 0, 1, h + pad * 2, 0, 0, pad, h + pad * 2);
    s.drawImage(src, w + pad - 1, 0, 1, h + pad * 2, w + pad, 0, pad, h + pad * 2);
    const perCss = w / document.documentElement.clientWidth; // canvas px per CSS px
    const filter = getComputedStyle(c).getPropertyValue(kind === 'lens' ? '--lens-filter' : thick ? '--body-filter-thick' : '--body-filter').trim() || 'none';
    c.width = src.width;
    c.height = src.height;
    const g = c.getContext('2d')!;
    g.filter = filter.replace(/blur\(([\d.]+)px\)/g, (_, n) => `blur(${(Number(n) * perCss).toFixed(2)}px)`);
    g.drawImage(src, 0, 0);
    g.filter = 'none';
    c.style.setProperty('--pad-x', String((pad / w) * innerWidth));
    c.style.setProperty('--pad-y', String((pad / h) * innerHeight));
  }, [image, kind, thick]);
  return <canvas ref={ref} className={`lg-img lg-${kind}`} />;
}

/* Glossy orb. A colour change is an ink fill spreading from the centre over the
   previous colour (no muddy RGB cross-fade between, say, amber and teal). */
const FILL = spring(.4, .08);
let fillSeq = 0;
function OrbFill({ fill }: { fill: string }) {
  const [z] = useState(() => ++fillSeq);
  return (
    <motion.span className="orb-fill" style={{ backgroundColor: fill, zIndex: z }}
      // Grows past the rim (clipped), so the old colour never lingers as a thin ring.
      initial={{ scale: 0 }} animate={{ scale: 1.3, opacity: 1 }}
      // Stay under the incoming fill until it covers the orb, then go.
      exit={{ opacity: 0, transition: { delay: .42, duration: .05 } }} transition={FILL} />
  );
}

function Orb({ fill, children, pulse, ping }: { fill: string; children: ReactNode; pulse?: boolean; ping?: boolean }) {
  const ring = useRef<HTMLSpanElement>(null);
  // Every colour change is a new drop on top, also back to a colour that is still
  // fading out underneath (quick A → B → A switches), so the newest colour always wins.
  const [ink, setInk] = useState({ fill, drop: 0 });
  if (ink.fill !== fill) setInk({ fill, drop: ink.drop + 1 });
  useEffect(() => {
    if (!pulse) return;
    let reference = .03;
    return levels.subscribe((level) => {
      reference = Math.max(.02, reference * .985, level);
      const v = Math.sqrt(Math.min(1, level / reference));
      if (ring.current) {
        ring.current.style.transform = `scale(${1 + v * .22})`;
        ring.current.style.opacity = String(.14 + v * .4);
      }
    });
  }, [pulse]);
  return (
    <span className="orb" style={{ '--tone': fill } as React.CSSProperties}>
      <span className="orb-fills"><AnimatePresence initial={false}><OrbFill key={ink.drop} fill={fill} /></AnimatePresence></span>
      {pulse && <span ref={ring} className="orb-ring" />}
      {ping && <span className="orb-ping" />}
      <span className="orb-icon">{children}</span>
    </span>
  );
}

function DrawCheck({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.6} strokeLinecap="round" strokeLinejoin="round">
      <motion.path d="M5 12.5l4.2 4.2L19 7" initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: .42, ease: EASE, delay: .14 }} />
    </svg>
  );
}

function WhisperTag() {
  return (
    <motion.span className="lg-tag" title="本段已针对轻声 / 气声说话增强识别"
      initial={{ opacity: 0, scale: .7, filter: 'blur(4px)' }} animate={{ opacity: 1, scale: 1, filter: 'blur(0px)' }} transition={POP}>
      <Wind size={11} strokeWidth={2.4} />气声
    </motion.span>
  );
}

function ChipView({ copy }: { copy: ChipCopy }) {
  const ping = copy.iconKey === 'saved';
  return (
    <div className="flex items-center gap-3 pl-[10px] pr-6" style={{ height: copy.detail ? 74 : 58 }}>
      <Orb fill={copy.fill} ping={ping}>
        <AnimatePresence mode="popLayout" initial={false}>
          <motion.span key={copy.iconKey} className="grid place-items-center text-white"
            initial={{ scale: .35, rotate: -28, opacity: 0, filter: 'blur(3px)' }}
            animate={{ scale: 1, rotate: 0, opacity: 1, filter: 'blur(0px)' }}
            exit={{ scale: .35, rotate: 28, opacity: 0, filter: 'blur(3px)', transition: { duration: .15, ease: 'easeIn' } }} transition={POP}>
            {copy.icon}
          </motion.span>
        </AnimatePresence>
      </Orb>
      <div className="relative min-w-0 flex-1 overflow-hidden" style={{ height: copy.detail ? 60 : 42 }}>
        <AnimatePresence mode="popLayout" initial={false}>
          <motion.div key={copy.title + copy.hint + (copy.detail ?? '')} className="absolute inset-0 flex flex-col justify-center leading-tight"
            initial={{ y: 16, opacity: 0, filter: 'blur(5px)' }} animate={{ y: 0, opacity: 1, filter: 'blur(0px)' }}
            exit={{ y: -16, opacity: 0, filter: 'blur(5px)', transition: { duration: .2, ease: 'easeIn' } }} transition={ROLL}>
            <div className="lg-title truncate">{copy.title}</div>
            <motion.div className="lg-faint lg-hint mt-[2px] truncate" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: .06, duration: .3 }}>{copy.hint}</motion.div>
            {copy.detail && <motion.div className="lg-faint lg-hint mt-[1px] truncate" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: .1, duration: .3 }}>{copy.detail}</motion.div>}
          </motion.div>
        </AnimatePresence>
      </div>
    </div>
  );
}

function LiveView({ model, reduced }: { model: Model; reduced: boolean }) {
  const recording = model.phase === 'recording' && !model.done;
  const tail = Array.from(shownText(model)).slice(-18);
  const wave = recording ? 'listening' : model.phase === 'processing' ? 'thinking' : 'idle';
  return (
    <div className="flex h-[54px] items-center gap-2.5 pl-[9px] pr-[18px]">
      <Orb pulse={recording} fill={model.done ? 'var(--ok)' : `var(--${model.sessionMode})`} ping={model.done}>
        <AnimatePresence mode="popLayout" initial={false}>
          <motion.span key={model.done ? 'done' : 'mic'} className="grid place-items-center text-white"
            initial={{ scale: .35, opacity: 0, filter: 'blur(3px)' }} animate={{ scale: 1, opacity: 1, filter: 'blur(0px)' }}
            exit={{ scale: .35, opacity: 0, transition: { duration: .14 } }} transition={POP}>
            {model.done ? <DrawCheck size={16} /> : <Mic size={16} strokeWidth={2.3} />}
          </motion.span>
        </AnimatePresence>
      </Orb>
      <AnimatePresence mode="popLayout" initial={false}>
        {model.done ? (
          <motion.div key="done" className="min-w-0 flex-1 leading-tight"
            initial={{ opacity: 0, x: 8, filter: 'blur(4px)' }} animate={{ opacity: 1, x: 0, filter: 'blur(0px)' }} transition={{ duration: .32, ease: EASE }}>
            <div className="flex items-center gap-1.5 text-[13.5px] font-semibold">{model.external ? handedTo(model.external) : '已输入到光标处'}</div>
            <div className="lg-faint truncate text-[11.5px]">{model.external ? '文字由它直接输入到光标处' : model.text}</div>
          </motion.div>
        ) : tail.length ? (
          <motion.div key="live" className="flex min-w-0 flex-1 items-center gap-2.5"
            initial={false} animate={{ ...SHOWN, transition: QUICK }} exit={{ opacity: 0, filter: 'blur(4px)', transition: { duration: .14 } }}>
            <LiveWave width={86} height={32} state={wave} tone={model.sessionMode} reduced={reduced} className="shrink-0" />
            <div className={`live-tail min-w-0 flex-1 text-[13.5px] font-medium ${model.phase === 'processing' ? 'shimmer' : ''}`}>
              <span>{tail.slice(0, -2).join('')}<span className="opacity-55">{tail.slice(-2).join('')}</span></span>
            </div>
            {model.whisper && <WhisperTag />}
            <span className="lg-faint shrink-0 text-[11.5px]"><Elapsed since={model.startedAt} /></span>
          </motion.div>
        ) : (
          /* The voice line carries the moment until the recognizer streams words. */
          <motion.div key="wave" className="flex min-w-0 flex-1 items-center gap-3" data-wave-only
            initial={false} animate={{ ...SHOWN, transition: QUICK }} exit={{ opacity: 0, filter: 'blur(4px)', transition: { duration: .14 } }}>
            <div className="min-w-0 flex-1 leading-tight">
              <div className="flex items-center gap-1.5 text-[13.5px] font-semibold">
                {model.phase === 'processing' ? <span className="shimmer">正在识别</span> : model.phase === 'starting' ? '正在接入' : '正在听'}
                {model.whisper && <WhisperTag />}
              </div>
              <div className="lg-faint mt-[2px] truncate text-[11.5px]">
                <Elapsed since={model.startedAt} />{recording ? ' · 说完出字' : ''}
              </div>
            </div>
            <LiveWave width={112} height={34} state={wave} tone={model.sessionMode} reduced={reduced} className="shrink-0" />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function NoteView({ model, bodyRef, reduced, listening, onPin, onClose }: {
  model: Model; bodyRef: React.RefObject<HTMLDivElement | null>; reduced: boolean; listening: boolean;
  onPin: () => void; onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const reset = useRef(0);
  const scroller = useRef<HTMLDivElement>(null);
  const text = shownText(model);
  // The card stays mounted from one note to the next: "已复制" belongs to its own session.
  useEffect(() => { clearTimeout(reset.current); setCopied(false); }, [model.startedAt]);
  useEffect(() => () => clearTimeout(reset.current), []);
  useEffect(() => { scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: reduced ? 'auto' : 'smooth' }); }, [text, reduced]);
  // The session ended without a result (cancelled, or the core exited): never "正在整理…".
  const stopped = !model.done && !isBusy(model.phase);
  const stage = listening ? 'listening' : model.done ? 'answer' : model.phase === 'processing' && text ? 'thinking' : 'idle';
  async function copy() {
    if (!model.text) return;
    try {
      await call('copy_text', { text: model.text });
      clearTimeout(reset.current);
      setCopied(true);
      reset.current = window.setTimeout(() => setCopied(false), 1400);
    } catch { /* the note is also in the main window */ }
  }
  const stagger = (i: number) => ({ initial: { opacity: 0, y: -5, filter: 'blur(3px)' }, animate: { opacity: 1, y: 0, filter: 'blur(0px)' }, transition: { delay: .12 + i * .04, duration: .3, ease: EASE } });
  // No words to show (yet): the voice line fills the card - while listening, and as it
  // settles into "thinking" until the saved text arrives.
  const prompt = !model.done && !text;
  const caption = listening ? '说完显示文字 · 再按一下结束并保存'
    : model.phase === 'processing' ? '马上显示整理好的文字' : model.phase === 'starting' ? '开头的声音已暂存' : '';
  const footer = model.savedAt ? `已保存于 ${clock(model.savedAt)}`
    : stopped ? '本段没有完成，未保存'
    : listening ? (!text ? '声线随音量变化 · 结束后定稿' : '实时文字 · 结束后定稿')
      : model.phase === 'starting' ? '正在接入语音引擎' : '正在识别完整内容';
  return (
    <div ref={bodyRef} className="flex flex-col">
      <div className="flex h-[46px] items-center gap-2 pl-3.5 pr-2.5" data-tauri-drag-region>
        <motion.span {...stagger(0)} className="orb orb-sm" data-tauri-drag-region><span className="orb-icon"><NotebookPen size={13} className="text-white" /></span></motion.span>
        <motion.span {...stagger(1)} className="text-[13.5px] font-bold tracking-tight" data-tauri-drag-region>笔记</motion.span>
        <motion.span {...stagger(2)} className="flex items-center gap-1.5 text-[12px] font-semibold" data-tauri-drag-region>
          <AnimatePresence mode="popLayout" initial={false}>
            <motion.span key={listening ? 'listen' : model.done ? 'done' : stopped ? 'stopped' : model.phase} className="flex items-center gap-1.5"
              initial={{ opacity: 0, y: 6, filter: 'blur(3px)' }} animate={{ opacity: 1, y: 0, filter: 'blur(0px)' }} exit={{ opacity: 0, y: -6, filter: 'blur(3px)' }} transition={ROLL}>
              {listening ? <><i className="rec-dot" />正在听 <span className="lg-faint"><Elapsed since={model.startedAt} /></span></>
                : model.done ? <span style={{ color: 'var(--lg-ok)' }} className="flex items-center gap-1"><DrawCheck size={13} />已保存</span>
                  : model.phase === 'starting' ? <span className="lg-faint">正在接入…</span>
                    : stopped ? <span className="lg-faint">已中断</span>
                      : <span className="shimmer">正在整理…</span>}
            </motion.span>
          </AnimatePresence>
        </motion.span>
        {model.whisper && <WhisperTag />}
        {model.pending && <span className="lg-chip">结束后切到{MODE_INFO[model.pending].label}</span>}
        <span className="flex-1 self-stretch" data-tauri-drag-region />
        <button className={`lg-btn ${model.pinned ? 'is-on' : ''}`} onClick={onPin} aria-pressed={model.pinned} aria-label={model.pinned ? '取消固定' : '固定便签'} title={model.pinned ? '取消固定' : '固定便签'}>
          <AnimatePresence mode="popLayout" initial={false}>
            <motion.span key={model.pinned ? 'on' : 'off'} className="grid place-items-center"
              initial={{ scale: .4, rotate: -40, opacity: 0 }} animate={{ scale: 1, rotate: 0, opacity: 1 }} exit={{ scale: .4, rotate: 40, opacity: 0 }} transition={POP}>
              {model.pinned ? <PinOff size={14} /> : <Pin size={14} />}
            </motion.span>
          </AnimatePresence>
        </button>
        <button className="lg-btn" onClick={onClose} aria-label="收起" title="收起"><X size={15} /></button>
      </div>
      <motion.div ref={scroller} className="scroll max-h-[340px] px-5 pb-2"
        initial={{ opacity: 0, filter: 'blur(6px)' }} animate={{ opacity: 1, filter: 'blur(0px)' }} transition={{ delay: .18, duration: .35, ease: EASE }}>
        <AnimatePresence mode="popLayout" initial={false}>
          {prompt ? (
            <motion.div key="prompt" className="flex flex-col items-center pb-3 pt-1 text-center" data-wave-only
              initial={{ opacity: 0, filter: 'blur(6px)' }} animate={{ opacity: 1, filter: 'blur(0px)' }}
              exit={{ opacity: 0, filter: 'blur(6px)', transition: { duration: .16 } }}>
              <LiveWave width={280} height={44} state={listening ? 'listening' : model.phase === 'processing' ? 'thinking' : 'idle'}
                onColor={listening} reduced={reduced} tone={listening ? 'notes-tinted' : 'notes'} />
              <div className="lg-faint mt-2 text-[12px]">{caption}</div>
            </motion.div>
          ) : (
            <motion.div key="score" className="pt-1"
              initial={{ opacity: 0, filter: 'blur(6px)' }} animate={{ opacity: 1, filter: 'blur(0px)' }} transition={{ duration: .3, ease: EASE }}>
              <VoiceScore text={text || '…'} stage={stage} envelope={model.done ? model.envelope : null} size={17} onColor={listening} reduced={reduced} />
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
      <div className="lg-divider flex h-[42px] items-center gap-1 px-3 text-[12px]">
        <span className="lg-faint truncate pl-1">{footer}</span>
        <span className="flex-1" />
        <button className="lg-btn lg-btn-text" disabled={!model.final} onClick={copy}>
          <AnimatePresence mode="popLayout" initial={false}>
            <motion.span key={copied ? 'ok' : 'copy'} className="grid place-items-center"
              initial={{ scale: .4, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} exit={{ scale: .4, opacity: 0 }} transition={POP}>
              {copied ? <DrawCheck size={13} /> : <Copy size={13} />}
            </motion.span>
          </AnimatePresence>
          {copied ? '已复制' : '复制'}
        </button>
        <button className="lg-btn lg-btn-text" onClick={() => shell.showMain()}><AppWindow size={13} />打开</button>
      </div>
    </div>
  );
}
