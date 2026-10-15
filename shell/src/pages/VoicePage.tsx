import { AnimatePresence, motion } from 'motion/react';
import {
  ArrowDown, ArrowUpRight, Check, Copy, Loader2, Mic, MicOff, MousePointer2, NotebookPen, Pause, RotateCcw, ShieldAlert, Sparkles, Square, X,
} from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { MODE_ICON, useRoster } from '../components/Chrome';
import { Battery, Empty, Glass, PenGlyph, spring, TidepoolGlyph, Tip, useMedia } from '../components/ui';
import { VoiceScore, type ScoreStage } from '../components/VoiceScore';
import { TIDEPOOL_BATTERY_NOTE } from '../lib/devices';
import { handedTo } from '../lib/engine';
import { ago, clock, dayLabel, duration, timer } from '../lib/format';
import { visibleLiveText } from '../lib/liveText';
import { act, levels, MODE_INFO, TOOL_INFO, useStore } from '../lib/store';
import type { Mode, Phase, Result } from '../lib/types';
import { LiveWave } from '../overlay/LiveWave';

function greeting() {
  const h = new Date().getHours();
  return h < 5 ? '夜深了' : h < 11 ? '早上好' : h < 13 ? '中午好' : h < 18 ? '下午好' : '晚上好';
}

function useElapsed(running: boolean) {
  const [start, setStart] = useState(0);
  const [, tick] = useState(0);
  useEffect(() => {
    if (!running) return;
    setStart(Date.now());
    const t = window.setInterval(() => tick((n) => n + 1), 250);
    return () => clearInterval(t);
  }, [running]);
  return running ? Date.now() - start : 0;
}

export function VoicePage({ onNotes, onDevices }: { onNotes?: () => void; onDevices?: () => void }) {
  const state = useStore((s) => s.state);
  const mode = useStore((s) => s.mode);
  const result = useStore((s) => s.result);
  const liveText = useStore((s) => s.liveText);
  const liveSource = useStore((s) => s.liveSource);
  const prefs = useStore((s) => s.prefs);
  const bindings = useStore((s) => s.bindings);
  const selected = useStore((s) => s.selected);
  const narrow = useMedia('(max-width: 1000px)');
  const short = useMedia('(max-height: 700px)');
  const phase = state.phase;
  const reduced = !!prefs?.appearance.reduced_motion;
  const listening = phase === 'recording';
  const thinking = phase === 'processing' || phase === 'starting';
  const toggleKey = bindings?.devices?.[selected]?.toggle_mode?.label;
  // A ⑦ quick note is a note even in input / present mode.
  const look: Mode = (listening || thinking) && state.destination === 'notes' ? 'notes' : mode;
  // Until the recognizer streams words, keep the sound visualization visible.
  const live = visibleLiveText(liveText, liveSource);
  const waveOnly = (listening || thinking) && !live;

  let stage: ScoreStage = 'idle';
  let text = `${greeting()}。\n${toggleKey ?? '长按 ⑦'} 切换模式，${narrow ? '\n' : ''}按 ⑥ 开始说话。`;
  if (listening) { stage = 'listening'; text = live; }
  else if (thinking) { stage = live ? 'thinking' : 'idle'; text = live || '声音已收到，正在识别…'; }
  else if (result && phase === 'idle') {
    stage = 'answer';
    text = result.external ? `${handedTo(result.app)}。\n文字由它直接输入到光标处。`
      : result.text?.trim() ? result.text.trim() : '本次没有识别到文字。';
  }
  else if (phase === 'disconnected') text = `${greeting()}。\n启用语音后，${narrow ? '\n' : ''}按笔上的 ⑥ 开始说话。`;
  else if (phase === 'error') text = '连接断开了。\n重新连接后再说一次。';

  const transcript = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const following = useRef(true);
  const previousPhase = useRef(phase);
  const [showLatest, setShowLatest] = useState(false);
  const [waveWidth, setWaveWidth] = useState(420);
  useLayoutEffect(() => {
    const el = content.current;
    if (!el) return;
    const measure = () => setWaveWidth(Math.round(Math.max(180, Math.min(560, el.clientWidth))));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const goLatest = () => {
    following.current = true;
    setShowLatest(false);
    if (transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
  };
  useLayoutEffect(() => {
    if (phase !== previousPhase.current && (phase === 'starting' || phase === 'recording')) goLatest();
    previousPhase.current = phase;
    if (following.current) goLatest();
  }, [text, phase]);
  useLayoutEffect(() => {
    const observer = new ResizeObserver(() => {
      // Wrapping, window resize and the result card can change the viewport
      // without adding characters. Preserve the reader's explicit scroll-up.
      if (following.current && transcript.current) transcript.current.scrollTop = transcript.current.scrollHeight;
    });
    if (transcript.current) observer.observe(transcript.current);
    if (content.current) observer.observe(content.current);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="grid h-full min-h-0 gap-4" style={{ gridTemplateColumns: `minmax(0,1fr) ${narrow ? 244 : 296}px` }}>
      <section className={`stage glass mode-${look}`} data-listening={listening} aria-label="语音">
        <span className="stage-glow" aria-hidden="true" />
        <span className="stage-flood" aria-hidden="true" />
        <header className={`flex items-center justify-between gap-3 ${narrow ? 'px-6 pt-5' : 'px-8 pt-7'}`}>
          <StatusLine result={result} />
          <ModePill mode={look} onColor={listening} />
        </header>
        <div className="relative flex min-h-0 flex-1 flex-col">
          <div ref={transcript} className={`voice-transcript scroll min-h-0 flex-1 pb-4 pt-6 ${narrow ? 'px-6' : 'px-8'}`}
            role="region" aria-label="语音转写正文" tabIndex={0}
            onScroll={(event) => {
              const el = event.currentTarget;
              following.current = el.scrollHeight - el.clientHeight - el.scrollTop < 48;
              setShowLatest(!following.current);
            }}>
            <div ref={content}>
              {waveOnly ? (
                <WaveOnly phase={phase} width={waveWidth} size={narrow ? 25 : 30} reduced={reduced} tone={look} />
              ) : (
                <VoiceScore text={text} stage={stage} envelope={stage === 'answer' ? result?.envelope : null}
                  size={narrow ? 25 : 30} onColor={listening} reduced={reduced} dim={stage === 'idle' ? 1 : 0} />
              )}
            </div>
          </div>
          <AnimatePresence>
            {showLatest && (
              <motion.button key="latest" className="btn btn-sm transcript-latest" onClick={goLatest}
                initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 8 }} transition={spring.snappy}>
                <ArrowDown size={14} />回到最新
              </motion.button>
            )}
          </AnimatePresence>
        </div>
        <AnimatePresence>
          {stage === 'answer' && result && <ResultCard key="result" result={result} />}
        </AnimatePresence>
        <Controls listening={listening} narrow={narrow} />
      </section>
      <aside className="flex min-h-0 flex-col gap-4">
        <ModeList compact={short} />
        <DeviceList compact={short} onOpen={onDevices} />
        <RecentNotes onOpen={onNotes} />
      </aside>
    </div>
  );
}

function StatusLine({ result }: { result: Result | null }) {
  const phase = useStore((s) => s.state.phase);
  const whisper = useStore((s) => !!s.state.whisper);
  const engine = useStore((s) => s.engine);
  const handed = result?.external ? handedTo(result.app) : result?.not_typed ? '没能输入到管理员窗口'
    : engine.kind === 'openless' ? '已交给 OpenLess 输入' : `已输入 · ${engine.label}`;
  // Same words as the result card: no check mark for a result that saved or typed nothing.
  const outcome = result?.destination === 'notes'
    ? (result.note_skipped ? <><MicOff size={14} />未保存 · 没有识别到内容</>
      : result.note_error ? <><i className="dot" data-tone="bad" />笔记未能保存</> : <><Check size={14} />已保存为笔记</>)
    : result?.empty_text ? <><MicOff size={14} />没有识别出文字</>
    : <>{result?.not_typed ? <ShieldAlert size={14} /> : <Check size={14} />}{handed}</>;
  const listening = phase === 'recording';
  const elapsed = useElapsed(listening);
  const thinkElapsed = useElapsed(phase === 'processing');
  const key = listening ? 'listen' : phase === 'idle' && result ? 'result' : phase;
  const content = listening ? <><i className="dot live" style={{ '--dot': '#fff' } as React.CSSProperties} />正在听<span className="num opacity-80">{timer(elapsed)}</span></>
    : phase === 'processing' ? <><Loader2 size={14} className="animate-spin" />正在想<span className="num opacity-60">{(thinkElapsed / 1000).toFixed(1)} 秒</span></>
    : phase === 'starting' ? <><Loader2 size={14} className="animate-spin" />正在接入</>
    : result && phase === 'idle' ? outcome
    : phase === 'disconnected' ? <><i className="dot" />已暂停</>
    : phase === 'error' ? <><i className="dot" data-tone="bad" />连接异常</>
    : <><i className="dot" data-on="true" />等你说话</>;
  return (
    <span className="flex min-w-0 items-center gap-2 text-[13px] font-semibold">
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.span key={key} className="flex items-center gap-2" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: .2 }}>
          {content}
        </motion.span>
      </AnimatePresence>
      {whisper && (listening || phase === 'processing') && (
        <Tip text="轻声 / 气声增强已开启：提高收音增益、放宽静音判定">
          <span className="chip" style={listening ? { background: 'rgba(255,255,255,.2)', color: '#fff' } : undefined}><Sparkles size={12} />气声增强</span>
        </Tip>
      )}
    </span>
  );
}

/* The voice line remains visible until live words or the final result arrive. */
function WaveOnly({ phase, width, size, reduced, tone }: {
  phase: Phase; width: number; size: number; reduced: boolean; tone: Mode;
}) {
  const listening = phase === 'recording';
  return (
    <div className="pt-1" data-wave-only>
      <LiveWave width={width} height={Math.round(size * 2.6)} state={listening ? 'listening' : phase === 'processing' ? 'thinking' : 'idle'}
        onColor={listening} tone={`${tone}-${listening}`} reduced={reduced} />
      <div className="mt-3" style={{ fontFamily: 'var(--font-display)', fontSize: size, fontWeight: 450, lineHeight: 1.3 }}>
        {listening ? '正在听' : phase === 'processing' ? '正在识别…' : '正在接入…'}
      </div>
      <div className="mt-2 text-[13px] leading-relaxed" style={{ opacity: .74 }}>
        {listening ? '说完显示文字' : phase === 'processing' ? '声音已收到，马上显示文字' : '开头的声音已暂存'}
      </div>
    </div>
  );
}

function ModePill({ mode, onColor }: { mode: Mode; onColor: boolean }) {
  const Icon = MODE_ICON[mode];
  const tool = useStore((s) => s.tool);
  const pending = useStore((s) => s.pendingMode);
  return (
    <span className="chip shrink-0" style={onColor ? { background: 'rgba(255,255,255,.2)', color: '#fff' } : { color: 'var(--mode)', background: 'color-mix(in srgb, var(--mode) 10%, transparent)' }}>
      <Icon size={13} />{MODE_INFO[mode].long}{mode === 'present' ? ` · ${TOOL_INFO[tool]}` : ''}
      {pending && <span className="opacity-70">→ {MODE_INFO[pending].label}</span>}
    </span>
  );
}

function Controls({ listening, narrow }: { listening: boolean; narrow: boolean }) {
  const phase = useStore((s) => s.state.phase);
  const busy = ['starting', 'recording', 'processing'].includes(phase);
  const ring = useRef<HTMLSpanElement>(null);
  const config = {
    disconnected: { icon: Mic, label: '启用语音', action: 'arm', tone: 'brand' },
    idle: { icon: Pause, label: '暂停语音', action: 'disarm', tone: 'calm' },
    starting: { icon: Loader2, label: '正在接入', action: '', tone: 'calm' },
    recording: { icon: Square, label: '说完了', action: 'stop', tone: 'onColor' },
    processing: { icon: Loader2, label: '正在识别', action: '', tone: 'calm' },
    error: { icon: RotateCcw, label: '重新连接', action: 'arm', tone: 'brand' },
  }[phase];
  const Icon = config.icon;
  // The ring breathes with the measured volume while listening (no React renders).
  useEffect(() => {
    if (!listening) { if (ring.current) ring.current.style.transform = ''; return; }
    return levels.subscribe((level) => {
      if (ring.current) ring.current.style.transform = `scale(${1 + Math.min(.32, Math.sqrt(Math.max(0, level)) * .9)})`;
    });
  }, [listening]);
  return (
    <footer className={`grid items-end pb-6 pt-2 ${narrow ? 'px-6' : 'px-8'}`} style={{ gridTemplateColumns: '1fr auto 1fr' }}>
      <div>
        <AnimatePresence>
          {busy && (
            <motion.button key="cancel" className="btn btn-ghost btn-sm" style={listening ? { color: 'rgba(255,255,255,.88)' } : undefined}
              initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, x: -6 }} transition={spring.snappy}
              onClick={() => act('action', { action: 'cancel' })}><X size={14} />取消本段</motion.button>
          )}
        </AnimatePresence>
      </div>
      <div className="orb-wrap flex flex-col items-center gap-2" data-listening={listening} data-tone={config.tone === 'brand' ? 'invite' : undefined}>
        <div className="relative">
          <span ref={ring} className="orb-ring" aria-hidden="true" />
          <button disabled={!config.action} onClick={() => config.action && act('action', { action: config.action })}
            aria-label={config.label} className="orb-btn" data-tone={config.tone === 'brand' ? undefined : config.tone}>
            <AnimatePresence mode="popLayout" initial={false}>
              <motion.span key={phase} className="relative z-[1] grid place-items-center"
                initial={{ opacity: 0, scale: .6, rotate: -20 }} animate={{ opacity: 1, scale: 1, rotate: 0 }} exit={{ opacity: 0, scale: .6 }} transition={spring.snappy}>
                <Icon size={phase === 'recording' ? 20 : 24} className={config.icon === Loader2 ? 'animate-spin' : ''} fill={phase === 'recording' ? 'currentColor' : 'none'} />
              </motion.span>
            </AnimatePresence>
          </button>
        </div>
        <span className="text-[12px] font-semibold" style={{ opacity: listening ? .88 : .62 }}>{config.label}</span>
      </div>
      <div className="text-right text-[12px]" style={{ opacity: listening ? .82 : .55 }}>
        {listening ? '停顿也没关系，按 ⑥ 结束' : phase === 'idle' ? <>按笔上的 <b className="font-semibold">⑥</b> 说话</> : ''}
      </div>
    </footer>
  );
}

// The result is passed in (not read from the store) so the exit animation
// still has it after a new recording clears the store.
function ResultCard({ result }: { result: Result }) {
  const notify = useStore((s) => s.notify);
  async function copy() {
    if (await act('copy_text', { text: result.text })) notify('已复制', 'success');
  }
  const notes = result.destination === 'notes';
  const external = !!result.external;
  const refused = !notes && !!result.not_typed;  // an administrator window dropped the keystrokes
  const empty = !notes && !external && !!result.empty_text;  // nothing recognised, nothing typed
  const unsaved = notes && (!!result.note_skipped || !!result.note_error);
  const timing = result.seconds != null && !external && !refused && !empty && !unsaved ? `结束后 ${result.seconds.toFixed(2)} 秒完成` : '';
  const detail = notes ? (unsaved ? '' : '可在笔记页编辑、打标签') : external ? '它会把文字直接输入到光标处；点声看不到文字'
    : refused ? (result.copied ? '已复制到剪贴板，按 Ctrl+V 粘贴' : '请复制后粘贴') + ' · 设置 → 通用 → 兼容管理员权限窗口'
    : empty ? '没有输入任何内容' : '点声不会自动发送或回车';
  return (
    <motion.div initial={{ opacity: 0, y: 18, scale: .97 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 10, scale: .98 }} transition={spring.soft}
      className={`mode-${notes ? 'notes' : 'input'} floating mx-6 mb-2 flex items-center gap-3 rounded-[18px] py-3 pl-4 pr-3`}>
      <span className="mode-gradient grid h-9 w-9 shrink-0 place-items-center rounded-full text-white" style={{ boxShadow: '0 6px 14px -6px var(--mode)' }}>
        {notes ? <NotebookPen size={16} /> : refused ? <ShieldAlert size={16} /> : empty ? <MicOff size={16} /> : <Check size={17} />}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-[13.5px] font-semibold">{notes ? (result.note_skipped ? '没有识别到内容，未保存' : result.note_error || '已保存到笔记') : external ? handedTo(result.app) : refused ? '没能输入到管理员窗口' : empty ? '没有识别出文字' : '已输入到光标处'}</div>
        <div className="faint truncate text-[12px]">{[timing, detail].filter(Boolean).join(' · ')}</div>
      </div>
      {/* Nothing to copy from an empty result (copying '' would clear the clipboard). */}
      {!external && !!result.text?.trim() && <button className="btn btn-ghost btn-sm" onClick={copy}><Copy size={14} />复制</button>}
      <button className="btn btn-ghost btn-sm btn-icon" aria-label="清除显示" title="清除显示" onClick={async () => { await act('clear_result'); useStore.setState({ result: null }); }}><X size={15} /></button>
    </motion.div>
  );
}

function ModeList({ compact }: { compact: boolean }) {
  const mode = useStore((s) => s.mode);
  const pending = useStore((s) => s.pendingMode);
  const tool = useStore((s) => s.tool);
  const bindings = useStore((s) => s.bindings);
  const selected = useStore((s) => s.selected);
  const toggleKey = bindings?.devices?.[selected]?.toggle_mode?.label ?? '长按 ⑦';
  return (
    <Glass className="shrink-0 p-2">
      {(['input', 'notes', 'present'] as Mode[]).map((value) => {
        const Icon = MODE_ICON[value];
        const active = mode === value;
        return (
          <button key={value} aria-pressed={active} onClick={() => !active && act('set_mode', { value, source: 'ui' })}
            className={`mode-${value} tile flex w-full items-center gap-3 ${compact ? 'px-2.5 py-2' : 'p-2.5'}`}>
            {active && <motion.span layoutId="mode-tile" className="absolute inset-0 rounded-[14px]"
              style={{ background: 'linear-gradient(135deg, color-mix(in srgb, var(--mode) 13%, var(--panel-solid)), color-mix(in srgb, var(--mode) 6%, var(--panel-solid)))', boxShadow: 'inset 0 0 0 1px color-mix(in srgb, var(--mode) 26%, transparent), 0 6px 18px -12px var(--mode)' }}
              transition={spring.snappy} />}
            <span className={`relative grid shrink-0 place-items-center rounded-[11px] ${compact ? 'h-8 w-8' : 'h-9 w-9'} ${active ? 'mode-gradient text-white' : ''}`}
              style={active ? { boxShadow: '0 6px 14px -6px var(--mode)' } : { background: 'var(--field)', color: 'var(--mode)' }}><Icon size={17} /></span>
            <span className="relative min-w-0">
              <span className="block text-[14px] font-semibold">{MODE_INFO[value].long}{value === 'present' && active ? ` · ${TOOL_INFO[tool]}` : ''}</span>
              {!compact && <span className="faint block truncate text-[12px]">{MODE_INFO[value].hint}</span>}
            </span>
            {pending === value && <span className="chip relative ml-auto" style={{ height: 20, fontSize: 11 }}>本段后</span>}
          </button>
        );
      })}
      {!compact && <div className="faint flex items-center gap-1.5 px-3 pb-1 pt-1.5 text-[11.5px]" title={`${toggleKey} 在三种模式间循环 · 短按 ⑦ 记闪念笔记`}><MousePointer2 size={12} className="shrink-0" /><span className="truncate">{toggleKey} 循环切换 · 短按 ⑦ 闪念笔记</span></div>}
    </Glass>
  );
}

/* Every device the user can pick up: each pen with its own battery, plus Tidepool. */
function DeviceList({ compact, onOpen }: { compact: boolean; onOpen?: () => void }) {
  const items = useRoster(true);
  const usage = useStore((s) => s.usage);
  const online = items.filter((i) => i.present).length;
  const t = usage?.active ?? usage?.totals;
  return (
    <Glass className="flex min-h-[124px] shrink flex-col overflow-hidden px-2 pb-2 pt-3" aria-label="设备">
      <div className="flex items-center justify-between px-2.5 pb-1">
        <span className="eyebrow">设备</span>
        <button className="btn btn-ghost btn-sm -mr-1 h-7 px-2.5" onClick={onOpen}>{online ? `${online} 台在线` : '查看'}<ArrowUpRight size={13} /></button>
      </div>
      {items.length === 0 && <p className="faint px-2.5 py-2 text-[12.5px]">插入接收器后，每支笔会单独出现在这里。</p>}
      <div className="scroll relative z-[1] -mx-1 min-h-0 flex-1 px-1">
      <AnimatePresence initial={false}>
        {items.map((item) => (
          <motion.button key={item.key} layout="position" data-roster={item.key} onClick={onOpen}
            initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} transition={spring.soft}
            className="tile block w-full overflow-hidden" style={{ opacity: item.present ? 1 : .62 }}>
            <span className={`flex items-center gap-3 px-2.5 ${compact ? 'py-1.5' : 'py-2'}`}>
              <span className="glyph" data-state={item.state} style={compact ? { width: 28, height: 28, borderRadius: 9 } : undefined}>
                {item.kind === 'tidepool' ? <TidepoolGlyph size={compact ? 15 : 17} /> : <PenGlyph size={compact ? 15 : 17} />}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13.5px] font-semibold">{item.name}</span>
                {!compact && (
                  <span className="faint flex items-center gap-1 truncate text-[11.5px]" style={item.state === 'recording' ? { color: 'var(--bad)' } : undefined}>
                    {item.status}{item.lastUsed && item.state !== 'recording' ? ` · ${ago(item.lastUsed)}使用` : ''}
                  </span>
                )}
              </span>
              {item.kind === 'tidepool' && item.battery == null
                ? <Tip text={TIDEPOOL_BATTERY_NOTE}><span className="dock-na" data-battery-na>未上报</span></Tip>
                : <Battery percent={item.battery} stale={!item.present} />}
            </span>
          </motion.button>
        ))}
      </AnimatePresence>
      </div>
      {t && !compact && (
        <div className="faint mx-2.5 mt-1 border-t pt-2 text-[11.5px]" style={{ borderColor: 'var(--hair)' }}>
          本轮 <span className="num">{t.sessions ?? 0}</span> 段 · 说话约 {duration(t.speech_seconds ?? 0)}
        </div>
      )}
    </Glass>
  );
}

function RecentNotes({ onOpen }: { onOpen?: () => void }) {
  const notes = useStore((s) => s.notes);
  const latest = [...notes].sort((a, b) => b.created - a.created).slice(0, 4);
  return (
    <Glass className="flex min-h-[104px] flex-1 basis-0 flex-col overflow-hidden px-2 py-2">
      <div className="flex items-center justify-between px-2.5 pb-1 pt-1">
        <span className="eyebrow">最近笔记</span>
        <button className="btn btn-ghost btn-sm -mr-1 h-7 px-2.5" onClick={onOpen}>全部<ArrowUpRight size={13} /></button>
      </div>
      {latest.length === 0 && <Empty className="py-5" icon={<NotebookPen size={20} />} title="还没有笔记">切到笔记模式说一段话，会出现在这里。</Empty>}
      <div className="fade-bottom relative z-[1] min-h-0 flex-1 overflow-hidden">
        {latest.map((n, i) => (
          <motion.button key={n.id} onClick={onOpen} className="tile block w-full px-2.5 py-2"
            initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ ...spring.soft, delay: i * .04 }}>
            <span className="faint flex items-center gap-2 text-[11px]"><span className="num">{dayLabel(n.created)} {clock(n.created)}</span>
              {n.tags.slice(0, 1).map((t) => <span key={t}>#{t}</span>)}</span>
            <span className="line-clamp-1 block text-[13px] font-semibold">{n.title}</span>
          </motion.button>
        ))}
      </div>
    </Glass>
  );
}
