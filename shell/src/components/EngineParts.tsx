/* Building blocks shared by the settings page's "语音引擎" section and the setup wizard.
   A typed key stays in this component's state only until it is saved, then it is
   dropped: the page only ever shows the core's masked form ("••••abcd"). */
import { AnimatePresence, motion } from 'motion/react';
import { AlertTriangle, ArrowUpRight, Check, Eye, EyeOff, Keyboard, Loader2, Lock, PencilLine, Plug, RotateCcw, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { call } from '../lib/bridge';
import { comboFromCodes, comboParts, comboText } from '../lib/engine';
import { act, useStore } from '../lib/store';
import type { EngineTest, HotkeyDetect, OpenLessStatus, Prefs, SecretName, SecretStatus } from '../lib/types';
import { spring } from './ui';

/** Save part of prefs.engine; the core validates (and may swap the running engine). */
export async function setEngine(patch: Record<string, unknown>): Promise<Prefs | null> {
  const data = await act<Prefs>('prefs_set', { patch: { engine: patch } });
  if (data) useStore.setState({ prefs: data });
  return data;
}

/** Detected dictation apps and OpenLess's status (read-only probes in the core). */
export function useEngineData(enabled = true) {
  const [hotkeys, setHotkeys] = useState<HotkeyDetect | null>(null);
  const [openless, setOpenless] = useState<OpenLessStatus | null>(null);
  const [loading, setLoading] = useState(enabled);
  // A probe that failed (core down, a timeout) is said as such and can be retried: never an
  // endless "正在检测".
  const [failed, setFailed] = useState(false);
  const coreDown = useStore((s) => !!s.coreDown);
  const refresh = useCallback(async () => {
    setLoading(true);
    const [apps, status] = await Promise.all([
      call<HotkeyDetect>('hotkey_detect').catch(() => null),
      call<OpenLessStatus>('openless_status').catch(() => null),
    ]);
    setHotkeys(apps);
    setOpenless(status);
    setFailed(!apps || !status);
    setLoading(false);
  }, []);
  // Again once a restarted core is back (meanwhile: failed, not "正在检测").
  useEffect(() => {
    if (!enabled) return;
    if (coreDown) { setLoading(false); setFailed(true); return; }
    refresh();
    useStore.getState().refreshSecrets();
  }, [enabled, coreDown, refresh]);
  return { hotkeys, openless, loading, failed, refresh };
}

/** An API key: masked once saved (更换 / 清除), a password field while typing. */
export function SecretField({ name, label, placeholder = '粘贴 API Key' }: { name: SecretName; label: string; placeholder?: string }) {
  const secret = useStore((s) => s.secrets?.[name]);
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState('');
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const saved = !!secret?.set;
  const open = editing || !saved;
  useEffect(() => { if (editing) input.current?.focus(); }, [editing]);
  const stop = () => { setEditing(false); setValue(''); setShow(false); };
  async function save() {
    const text = value.trim();
    if (!text || busy) return;
    setBusy(true);
    const data = await act<SecretStatus>('secret_set', { name, value: text });
    setBusy(false);
    setValue('');  // the typed key leaves the page whatever happened
    setShow(false);
    if (data?.secrets) {
      useStore.setState({ secrets: data.secrets });
      setEditing(false);
    }
  }
  async function clear() {
    const data = await act<SecretStatus>('secret_delete', { name });
    if (data?.secrets) useStore.setState({ secrets: data.secrets });
    stop();
  }
  // No exit animation on purpose: an exiting copy of the field would keep the typed key in
  // the page for a moment after it was saved. The new view springs in instead.
  return (
    <div className="secret" data-secret={name}>
      {open ? (
        <motion.div key="edit" className="flex items-center gap-1.5" initial={{ opacity: 0, scale: .98 }} animate={{ opacity: 1, scale: 1 }}
          transition={spring.snappy}>
          <span className="field-wrap secret-input">
            <input ref={input} className="field num" type={show ? 'text' : 'password'} autoComplete="off" spellCheck={false}
              aria-label={label} placeholder={placeholder} value={value} name={`dv-${name}`}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') save();
                if (e.key === 'Escape' && saved) stop();
              }} />
            <button type="button" className="field-eye" aria-label={show ? '隐藏输入' : '显示输入'} title={show ? '隐藏' : '显示'}
              onClick={() => setShow(!show)}>{show ? <EyeOff size={14} /> : <Eye size={14} />}</button>
          </span>
          <button className="btn btn-primary btn-sm" disabled={!value.trim() || busy} onClick={save}>
            {busy && <Loader2 size={13} className="animate-spin" />}保存
          </button>
          {saved && <button className="btn btn-ghost btn-sm" onClick={stop}>取消</button>}
        </motion.div>
      ) : (
        <motion.div key="saved" className="flex items-center gap-1.5" initial={{ opacity: 0, scale: .98 }} animate={{ opacity: 1, scale: 1 }}
          transition={spring.snappy}>
          <span className="secret-saved" title="已用 Windows 数据保护加密，只保存在这台电脑">
            <Lock size={13} className="shrink-0" />
            <span className="secret-mask num">{secret?.masked}</span>
            <span className="faint text-[12px]">已加密保存</span>
          </span>
          <button className="btn btn-sm" onClick={() => setEditing(true)}>更换</button>
          <button className="btn btn-ghost btn-sm btn-icon" aria-label={`清除${label}`} title="清除" onClick={clear}><X size={14} /></button>
        </motion.div>
      )}
    </div>
  );
}

/** A plain setting saved on blur / Enter (Esc restores); a tick confirms the save. */
export function TextField({ value, onSave, label, placeholder, width = 280, numeric }: {
  value: string; onSave: (value: string) => Promise<boolean>; label: string; placeholder?: string; width?: number | string; numeric?: boolean;
}) {
  const [draft, setDraft] = useState(value);
  const [ok, setOk] = useState(false);
  // Esc: leave without saving (blur runs commit with this render's draft, still the typed text).
  const skip = useRef(false);
  useEffect(() => setDraft(value), [value]);
  useEffect(() => {
    if (!ok) return;
    const t = window.setTimeout(() => setOk(false), 1400);
    return () => clearTimeout(t);
  }, [ok]);
  async function commit() {
    if (skip.current) { skip.current = false; setDraft(value); return; }
    const text = draft.trim();
    if (text === value) { setDraft(value); return; }
    if (await onSave(text)) setOk(true);
    else setDraft(value);
  }
  return (
    <span className="field-wrap" style={{ width }}>
      <input className="field" aria-label={label} placeholder={placeholder} value={draft} spellCheck={false} autoComplete="off"
        inputMode={numeric ? 'numeric' : undefined}
        onChange={(e) => setDraft(e.target.value)} onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') e.currentTarget.blur();
          if (e.key === 'Escape') { skip.current = true; setDraft(value); e.currentTarget.blur(); }
        }} />
      <AnimatePresence>
        {ok && (
          <motion.span className="field-ok" aria-label="已保存" initial={{ opacity: 0, scale: .6 }} animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0 }} transition={spring.snappy}><Check size={14} /></motion.span>
        )}
      </AnimatePresence>
    </span>
  );
}

/** "测试连接": one short request with the saved settings; the answer shows inline. */
export function TestButton({ kind, disabled, children }: { kind: 'volc' | 'dashscope' | 'polish'; disabled?: boolean; children?: ReactNode }) {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<EngineTest | null>(null);
  async function run() {
    setRunning(true);
    setResult(null);
    const answer = await act<EngineTest>('engine_test', { kind });
    setRunning(false);
    setResult(answer);
  }
  return (
    <div className="test-row" data-test={kind}>
      <button className="btn btn-sm" disabled={disabled || running} onClick={run}>
        {running ? <Loader2 size={14} className="animate-spin" /> : <Plug size={14} />}{running ? '正在测试…' : '测试连接'}
      </button>
      <AnimatePresence mode="popLayout" initial={false}>
        {result && !running && (
          <motion.span key={`${result.ok}${result.message}`} className="test-result" data-tone={result.ok ? 'ok' : 'bad'} role="status"
            initial={{ opacity: 0, x: -6, scale: .97 }} animate={{ opacity: 1, x: 0, scale: 1 }} exit={{ opacity: 0, transition: { duration: .1 } }}
            transition={spring.snappy}>
            {result.ok ? <Check size={13} className="mt-[3px] shrink-0" /> : <AlertTriangle size={13} className="mt-[3px] shrink-0" />}
            <span>{result.message}{result.ok && <span className="num opacity-70"> · {result.seconds.toFixed(1)} 秒</span>}</span>
          </motion.span>
        )}
      </AnimatePresence>
      {children}
    </div>
  );
}

export function HelpLink({ url, children }: { url: string; children: ReactNode }) {
  return (
    <button className="link-btn" onClick={() => act('open_url', { url })} title={url}>
      {children}<ArrowUpRight size={12} />
    </button>
  );
}

export function KeyCaps({ combo, size }: { combo: string; size?: 'lg' }) {
  return (
    <span className="keycaps" aria-label={combo}>
      {comboParts(combo).map((part, i) => (
        <span key={i} className="flex items-center gap-1">
          {i > 0 && <span className="faint text-[11px]">+</span>}
          <kbd className={`kbd ${size === 'lg' ? 'kbd-lg' : ''}`}>{part}</kbd>
        </span>
      ))}
    </span>
  );
}

/** The dictation app's shortcut: shown as key caps, recorded by pressing it or typed. */
export function ComboEditor({ combo, custom, onSave, onReset, startRecording }: {
  combo: string; custom: boolean; onSave: (value: string) => Promise<boolean>; onReset?: () => void; startRecording?: boolean;
}) {
  const [mode, setMode] = useState<'view' | 'record' | 'type'>(startRecording ? 'record' : 'view');
  const [codes, setCodes] = useState<string[]>([]);
  const [text, setText] = useState('');
  const down = useRef(new Set<string>());
  const order = useRef<string[]>([]);
  const capture = useRef<HTMLInputElement>(null);
  const typed = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (mode === 'record') capture.current?.focus();
    if (mode === 'type') typed.current?.focus();
    down.current.clear();
    order.current = [];
    setCodes([]);
  }, [mode]);
  async function finish(value: string) {
    if (await onSave(value)) setMode('view');
    else { order.current = []; setCodes([]); }
  }
  async function saveTyped() {
    const value = text.trim();
    if (!value) return;
    if (await onSave(value)) { setText(''); setMode('view'); }
  }
  return (
    <div className="combo" data-combo-mode={mode}>
      <AnimatePresence mode="popLayout" initial={false}>
        {mode === 'view' && (
          <motion.div key="view" className="flex flex-wrap items-center gap-2" initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, transition: { duration: .1 } }} transition={spring.snappy}>
            {combo ? <KeyCaps combo={combo} size="lg" /> : <span className="faint text-[13px]">还没有快捷键</span>}
            {custom && combo && <span className="chip" style={{ height: 20, fontSize: 11 }}>已改</span>}
            <span className="flex items-center gap-1">
              <button className="btn btn-sm" onClick={() => setMode('record')}><Keyboard size={14} />录制</button>
              <button className="btn btn-ghost btn-sm" onClick={() => setMode('type')}><PencilLine size={14} />手动输入</button>
              {custom && onReset && <button className="btn btn-ghost btn-sm btn-icon" onClick={onReset} title="恢复这个软件的默认快捷键" aria-label="恢复默认快捷键"><RotateCcw size={14} /></button>}
            </span>
          </motion.div>
        )}
        {mode === 'record' && (
          <motion.div key="record" className="grid gap-1.5" initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, transition: { duration: .1 } }} transition={spring.snappy}>
            <div className="flex items-center gap-1.5">
              <span className="hotkey is-recording">
                <input ref={capture} readOnly className="field num w-[220px] text-center" aria-label="按下目标软件的快捷键"
                  value={codes.length ? comboText(codes) : '请按下快捷键…'}
                  onBlur={() => setMode('view')}
                  onKeyDown={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    if (e.repeat) return;
                    if (e.code === 'Escape' && !order.current.length) { setMode('view'); return; }
                    down.current.add(e.code);
                    if (!order.current.includes(e.code)) order.current.push(e.code);
                    setCodes([...order.current]);
                  }}
                  onKeyUp={(e) => {
                    e.preventDefault();
                    down.current.delete(e.code);
                    if (!down.current.size && order.current.length) finish(comboFromCodes(order.current));
                  }} />
              </span>
              <button className="btn btn-ghost btn-sm" onMouseDown={(e) => e.preventDefault()} onClick={() => setMode('view')}>取消</button>
            </div>
            <span className="faint text-[11.5px]">全部松开即保存；含 Win 的组合会被系统拦截，请用「手动输入」（例如 Ctrl+Win）。</span>
          </motion.div>
        )}
        {mode === 'type' && (
          <motion.div key="type" className="flex items-center gap-1.5" initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, transition: { duration: .1 } }} transition={spring.snappy}>
            <input ref={typed} className="field w-[240px]" aria-label="手动输入快捷键" placeholder="例如 Ctrl+Win、右 Alt" value={text}
              spellCheck={false} autoComplete="off"
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') saveTyped();
                if (e.key === 'Escape') setMode('view');
              }} />
            <button className="btn btn-primary btn-sm" disabled={!text.trim()} onClick={saveTyped}>保存</button>
            <button className="btn btn-ghost btn-sm" onClick={() => setMode('view')}>取消</button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/** A small status light with text: ok (green), off (grey) or bad (red). */
export function StatusDot({ tone, children }: { tone: 'ok' | 'off' | 'bad' | 'wait'; children: ReactNode }) {
  return (
    <span className="status-dot" data-tone={tone}>
      <i className="dot" data-on={tone === 'ok'} data-tone={tone === 'bad' ? 'bad' : undefined} />
      {children}
    </span>
  );
}
