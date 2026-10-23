/* First-run setup: a full-surface glass sheet over the main window, four steps with dots:
   设备 (receiver, live microphone level, ⑥ key test) → 语音引擎 (Doubao recommended with
   inline key + test; Bailian, a detected OpenLess or dictation app as alternatives) →
   试一句 (a test recording shown here, never typed) → 完成 (the key cheat sheet).
   While it is open and in front, every recording is a test (core: wizard_test, renewed while
   the window is shown and focused, handed back the moment it is not; lib/wizard). */
import { AnimatePresence, motion } from 'motion/react';
import { ArrowLeft, ArrowRight, Check, Loader2, Mic, MousePointerClick, NotebookPen, Presentation, RotateCcw, Sparkles } from 'lucide-react';
import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type ReactNode } from 'react';
import { call } from '../lib/bridge';
import { ENGINE_TITLE, engineToneVars, handedTo, HELP, hotkeyWarnings, missing, SECRET, STYLE_TEXT, switchText, VOLC_RESOURCES } from '../lib/engine';
import { act, levels, useStore } from '../lib/store';
import type { EngineKind, EnginePrefs, HotkeyApp, Prefs } from '../lib/types';
import { closeStep, hasUnsavedSecret, hotkeyChoice, renewWhileActive, trapTab, typedThisRound, windowPresence } from '../lib/wizard';
import { LiveWave } from '../overlay/LiveWave';
import { DEFAULT_ENGINE, ENGINE_ICON } from '../pages/EngineSection';
import { BaseusArt, Pv60Art } from './DeviceArt';
import { HelpLink, KeyCaps, SecretField, setEngine, TestButton, useEngineData } from './EngineParts';
import { Segmented, spring, Tip } from './ui';
import { VoiceScore } from './VoiceScore';

const STEPS = ['设备', '语音引擎', '试一句', '完成'] as const;
const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';
const TITLES = ['认识一下你的笔', '选一个语音引擎', '试说一句', '都准备好了'];

interface Choice { key: string; kind: EngineKind; title: string; profile?: string }

const slide = {
  enter: (dir: number) => ({ opacity: 0, x: dir * 28, filter: 'blur(3px)' }),
  center: { opacity: 1, x: 0, filter: 'blur(0px)', transitionEnd: { filter: 'none' } },
  exit: (dir: number) => ({ opacity: 0, x: dir * -20, transition: { duration: .12 } }),
};

export function SetupWizard() {
  const open = useStore((s) => s.wizardOpen);
  return <AnimatePresence>{open && <Wizard key="wizard" />}</AnimatePresence>;
}

function Wizard() {
  const close = useStore((s) => s.closeWizard);
  const prefs = useStore((s) => s.prefs);
  const engine = prefs?.engine ?? DEFAULT_ENGINE;
  // Run again from settings: closing it never brings it back at the next launch.
  const setupDone = prefs?.setup?.done !== false;
  const secrets = useStore((s) => s.secrets);
  const [step, setStep] = useState(0);
  const [dir, setDir] = useState(1);
  const info = useStore((s) => s.engine);
  const [choice, setChoice] = useState<Choice>(() => (engine.kind === 'openless'
    ? { key: 'volc', kind: 'volc', title: ENGINE_TITLE.volc } : engine.kind === 'hotkey'
      ? { key: 'hotkey:' + engine.hotkey.profile, kind: 'hotkey', title: info.app ?? info.label, profile: engine.hotkey.profile }
      : { key: engine.kind, kind: engine.kind, title: ENGINE_TITLE[engine.kind] }));
  const [applying, setApplying] = useState(false);
  // An API key typed or pasted but not saved: closing, another step or another engine card
  // would throw it away, so ask first ('close' / 'leave'; `pending` runs on 丢弃).
  const [confirming, setConfirming] = useState<'close' | 'leave' | null>(null);
  const pending = useRef<() => void>(close);
  const dialog = useRef<HTMLDivElement>(null);
  function guard(action: () => void) {
    if (hasUnsavedSecret(dialog.current)) { pending.current = action; setConfirming('leave'); return; }
    action();
  }
  const go = (next: number) => guard(() => { setDir(next > step ? 1 : -1); setStep(next); setConfirming(null); });

  // While the wizard is in front a press of ⑥ is a test: nothing is typed or saved. Hidden to
  // the tray or behind another app, dictation works as usual at once.
  useEffect(() => renewWhileActive(windowPresence(),
    () => { call('wizard_test', { enabled: true }).catch(() => {}); },
    () => { call('wizard_test', { enabled: false }).catch(() => {}); }, 30000), []);
  function requestClose(typing = false) {
    const next = closeStep({ typing, unsaved: hasUnsavedSecret(dialog.current), confirming: confirming === 'close' });
    if (next === 'confirm') { pending.current = close; setConfirming('close'); }
    else if (next === 'close') close();
  }
  const latestClose = useRef(requestClose);
  latestClose.current = requestClose;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      latestClose.current(!!(e.target as HTMLElement)?.closest?.('input, textarea'));
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  // Focus starts inside the dialog and Tab never leaves it (the page behind stays out of reach).
  useEffect(() => {
    const box = dialog.current;
    if (box && !box.contains(document.activeElement)) box.focus({ preventScroll: true });
  }, []);
  function trapFocus(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key !== 'Tab' || !dialog.current) return;
    const items = [...dialog.current.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.getClientRects().length > 0);
    const index = trapTab(items.indexOf(document.activeElement as HTMLElement), items.length, e.shiftKey);
    if (index >= 0) {
      e.preventDefault();
      items[index].focus();
    }
  }

  async function finish() {
    const data = await act<Prefs>('prefs_set', { patch: { setup: { done: true } } });
    if (data) useStore.setState({ prefs: data });
    close();
  }
  const chosenCurrent = choice.kind === engine.kind && (choice.kind !== 'hotkey' || choice.profile === engine.hotkey.profile);
  const hotkey = choice.kind === 'hotkey' ? hotkeyChoice(engine.hotkey, choice.profile ?? '') : null;
  const blocked = chosenCurrent ? null : missing(choice.kind, hotkey ? { ...engine, hotkey: { ...engine.hotkey, ...hotkey } } : engine, secrets);
  async function applyChoice() {
    if (chosenCurrent) { go(2); return; }
    setApplying(true);
    const patch: Record<string, unknown> = { kind: choice.kind };
    if (hotkey) patch.hotkey = hotkey;
    const data = await setEngine(patch);
    setApplying(false);
    if (data) go(2);
  }

  return (
    <motion.div className="wizard-scrim" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0, transition: { duration: .18 } }}>
      <motion.div ref={dialog} role="dialog" aria-modal="true" aria-label="配置向导" className="wizard floating" tabIndex={-1}
        onKeyDown={trapFocus} initial={{ opacity: 0, scale: .965, y: 18 }} animate={{ opacity: 1, scale: 1, y: 0 }} exit={{ opacity: 0, scale: .975, y: 8 }}
        transition={spring.soft}>
        <header className="wizard-head">
          <div className="min-w-0">
            <div className="eyebrow">初次设置 · {step + 1}/{STEPS.length}</div>
            <AnimatePresence mode="popLayout" initial={false}>
              <motion.h2 key={step} className="title-lg mt-1 text-[22px]" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6, transition: { duration: .1 } }} transition={spring.snappy}>{TITLES[step]}</motion.h2>
            </AnimatePresence>
          </div>
          <StepDots step={step} onGo={(i) => go(i)} />
          <button className="btn btn-ghost btn-sm" onClick={() => requestClose()} title={setupDone ? '关闭向导' : '关闭向导；下次启动点声时还会出现'}>稍后再说</button>
        </header>
        <div className="wizard-body scroll">
          <AnimatePresence mode="wait" initial={false} custom={dir}>
            <motion.div key={step} className={step === 1 ? undefined : 'wizard-step'} custom={dir} variants={slide} initial="enter" animate="center" exit="exit" transition={spring.soft}>
              {step === 0 && <DeviceStep />}
              {step === 1 && <EngineStep choice={choice} onChoose={(c) => { if (c.key !== choice.key) guard(() => setChoice(c)); }} engine={engine} />}
              {step === 2 && <TryStep onBack={() => go(1)} />}
              {step === 3 && <DoneStep />}
            </motion.div>
          </AnimatePresence>
        </div>
        <AnimatePresence initial={false}>
          {confirming && (
            <motion.div key="unsaved" className="wizard-confirm" role="alert" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 6, transition: { duration: .12 } }} transition={spring.snappy}>
              <span className="flex-1">{confirming === 'close' ? '粘贴的 API Key 还没有保存，关闭向导会丢弃它。' : '粘贴的 API Key 还没有保存，继续会丢弃它。'}</span>
              <button className="btn btn-sm" onClick={() => setConfirming(null)}>继续填写</button>
              <button className="btn btn-ghost btn-sm" onClick={() => { setConfirming(null); pending.current(); }}>{confirming === 'close' ? '丢弃并关闭' : '丢弃并继续'}</button>
            </motion.div>
          )}
        </AnimatePresence>
        <footer className="wizard-foot">
          {step === 0
            ? <button className="btn btn-ghost btn-sm" onClick={finish} title="不再自动打开向导">跳过向导</button>
            : <button className="btn btn-ghost btn-sm" onClick={() => go(step - 1)}><ArrowLeft size={14} />上一步</button>}
          <span className="flex items-center gap-2">
            {step === 1 && (
              <>
                {blocked && <span className="faint hidden text-[12px] sm:inline">{blocked}</span>}
                {!chosenCurrent && <button className="btn btn-ghost btn-sm" onClick={() => go(2)} title={`保持现在的 ${ENGINE_TITLE[engine.kind]}`}>跳过这一步</button>}
                <button className="btn btn-primary" disabled={!!blocked || applying} onClick={applyChoice}>
                  {applying && <Loader2 size={14} className="animate-spin" />}
                  {chosenCurrent ? '下一步' : switchText(choice.title)}<ArrowRight size={15} />
                </button>
              </>
            )}
            {step !== 1 && step < 3 && <button className="btn btn-primary" onClick={() => go(step + 1)}>下一步<ArrowRight size={15} /></button>}
            {step === 3 && <button className="btn btn-primary" onClick={finish}><Check size={15} />开始使用</button>}
          </span>
        </footer>
      </motion.div>
    </motion.div>
  );
}

function StepDots({ step, onGo }: { step: number; onGo: (step: number) => void }) {
  return (
    <ol className="steps" aria-label="步骤">
      {STEPS.map((label, i) => {
        const state = i < step ? 'done' : i === step ? 'current' : 'todo';
        return (
          <li key={label} className="step" data-state={state}>
            {i > 0 && <span className="step-line" data-done={i <= step} aria-hidden="true" />}
            <button className="step-btn" disabled={i >= step} onClick={() => onGo(i)} aria-current={i === step ? 'step' : undefined}
              title={i < step ? `回到「${label}」` : undefined}>
              <span className="step-dot">{state === 'done' && <Check size={9} strokeWidth={3.4} />}</span>
              <span className="step-label">{label}</span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

/* ── 1 设备 ─────────────────────────────────────────────────────────────── */
function DeviceStep() {
  const devices = useStore((s) => s.devices);
  const selected = useStore((s) => s.selected);
  const instances = useStore((s) => s.instances);
  const lastKey = useStore((s) => s.lastKey);
  const meter = useStore((s) => s.meter);
  const [since] = useState(() => Date.now());
  const [heard, setHeard] = useState(false);
  const bar = useRef<HTMLElement>(null);
  const device = devices.find((d) => d.id === selected);
  const present = !!device?.present;
  const pens = instances.filter((p) => p.present && p.model === selected).length;
  const key = lastKey && lastKey.at >= since ? lastKey : null;
  const keyName = selected === 'pv60' ? '语音键' : '⑥';

  // Microphone levels while this step shows and the window is in front (the core opens the
  // pen's microphone for it; hidden or behind another app, it is let go at once).
  useEffect(() => renewWhileActive(windowPresence(),
    () => { call('mic_meter', { seconds: 20 }).catch(() => {}); },
    () => { call('mic_meter', { seconds: 0 }).catch(() => {}); }, 8000), []);
  useEffect(() => levels.subscribe((level) => {
    const value = Math.min(1, Math.sqrt(Math.max(0, level)) * 1.5);
    if (bar.current) bar.current.style.transform = `scaleX(${value.toFixed(3)})`;
    if (level > 0.05) setHeard(true);
  }), []);

  const Art = selected === 'pv60' ? Pv60Art : BaseusArt;
  return (
    <div className="wizard-split">
      <div className="wizard-art">
        <Art className="h-full w-full" highlight={key ? undefined : '6'} />
      </div>
      <div className="grid content-start gap-3">
        <p className="muted text-[13px] leading-relaxed">插上接收器、打开笔的开关，然后对着笔说几句、按一下 {keyName}。三项都打勾最好；没有也可以先继续。</p>
        <div className="well">
          <CheckRow ok={present} icon={<span className="text-[12px] font-bold">1</span>} title="接收器"
            detail={present ? `${device?.name ?? '翻页笔'}${pens > 1 ? ` · ${pens} 支笔在线` : ''}` : '没有检测到：插上接收器，打开笔的开关'} />
          <CheckRow ok={heard} icon={<Mic size={14} />} title="麦克风"
            detail={meter && !meter.ok ? meter.message : heard ? `听到了 · ${device?.microphone ?? '翻页笔麦克风'}` : `对着笔说几句话 · ${device?.microphone ?? '翻页笔麦克风'}`}
            bad={!!meter && !meter.ok}>
            <span className="meter" aria-hidden="true"><i ref={bar} /></span>
          </CheckRow>
          <CheckRow ok={!!key} icon={<span className="text-[12px] font-bold">{keyName}</span>} title="录音键"
            detail={key ? `收到了（${key.action === 'stop' ? '结束' : '开始'}信号）` : `按一下笔上的 ${keyName} 试试`} />
        </div>
      </div>
    </div>
  );
}

function CheckRow({ ok, icon, title, detail, bad, children }: { ok: boolean; icon: ReactNode; title: string; detail: string; bad?: boolean; children?: ReactNode }) {
  return (
    <div className="check-row" data-ok={ok}>
      <span className="check-ico" data-ok={ok} data-bad={bad || undefined}>
        <AnimatePresence mode="popLayout" initial={false}>
          <motion.span key={ok ? 'ok' : 'wait'} className="grid place-items-center" initial={{ scale: .4, opacity: 0 }} animate={{ scale: 1, opacity: 1 }}
            exit={{ scale: .4, opacity: 0, transition: { duration: .1 } }} transition={spring.snappy}>
            {ok ? <Check size={15} strokeWidth={2.8} /> : icon}
          </motion.span>
        </AnimatePresence>
      </span>
      <span className="min-w-0 flex-1">
        <span className="block text-[13.5px] font-semibold">{title}</span>
        <span className="block truncate text-[12px]" style={{ color: bad ? 'var(--bad)' : 'var(--text-3)' }}>{detail}</span>
      </span>
      {children}
    </div>
  );
}

/* ── 2 语音引擎 ──────────────────────────────────────────────────────────── */
function EngineStep({ choice, onChoose, engine }: { choice: Choice; onChoose: (choice: Choice) => void; engine: EnginePrefs }) {
  const data = useEngineData();
  const info = useStore((s) => s.engine);
  const apps = (data.hotkeys?.apps ?? []).filter((a) => a.detected && !a.builtin).slice(0, 3);
  const openless = data.openless;
  const showOpenLess = !!openless?.installed || engine.kind === 'openless';
  const current = (c: Choice) => c.kind === info.kind && (c.kind !== 'hotkey' || c.profile === engine.hotkey.profile);
  const options: (Choice & { badge?: string; desc: string })[] = [
    { key: 'volc', kind: 'volc', title: '豆包语音', badge: '推荐', desc: '在线流式，最快最准；需要火山引擎的 API Key' },
    { key: 'dashscope', kind: 'dashscope', title: '阿里云百炼', desc: 'Fun-ASR 实时识别；需要百炼的 API Key' },
    ...(showOpenLess ? [{ key: 'openless', kind: 'openless' as const, title: 'OpenLess',
      desc: !openless ? (data.loading ? '正在检查…' : '暂时无法检查，可在设置里刷新') : openless.running ? (openless.remote_input && openless.paired ? '已安装 · 运行中 · 远程输入已开' : '已安装 · 运行中 · 需开启远程输入') : '已安装 · 未运行' }] : []),
    ...apps.map((app) => ({ key: 'hotkey:' + app.profile, kind: 'hotkey' as const, profile: app.profile, title: app.name, badge: '实验',
      desc: `检测到 · 热键桥接，${app.running ? '正在运行' : '已安装'}` })),
  ];
  return (
    <div className="grid gap-2" role="radiogroup" aria-label="语音引擎">
      <p className="muted mb-1 text-[13px] leading-relaxed">点声把笔的声音交给语音引擎识别。推荐豆包语音：边说边出字、松开约半秒定稿。以后可以在「设置 → 语音引擎」里更换，润色等选项也在那里。</p>
      {options.map((option) => (
        <ChoiceCard key={option.key} on={choice.key === option.key} kind={option.kind} title={option.title} badge={option.badge}
          desc={option.desc} current={current(option)} onPick={() => onChoose({ key: option.key, kind: option.kind, title: option.title, profile: option.profile })}>
          {option.kind === 'volc' && <VolcQuick engine={engine} />}
          {option.kind === 'dashscope' && <DashQuick engine={engine} />}
          {option.kind === 'openless' && (
            <p className="text-[12.5px] leading-relaxed">在 OpenLess 设置里开启<b>远程输入</b>即可，识别服务和润色风格沿用 OpenLess 的。远程输入会在局域网监听（有配对码保护）；点声只连接这台电脑。</p>
          )}
          {option.kind === 'hotkey' && <HotkeyQuick app={data.hotkeys?.apps.find((a) => a.profile === option.profile) ?? null}
            keys={option.profile === engine.hotkey.profile ? engine.hotkey.keys : ''} />}
        </ChoiceCard>
      ))}
    </div>
  );
}

function ChoiceCard({ on, kind, title, badge, desc, current, onPick, children }: {
  on: boolean; kind: EngineKind; title: string; badge?: string; desc: string; current: boolean; onPick: () => void; children?: ReactNode;
}) {
  const Icon = ENGINE_ICON[kind];
  return (
    <div className="choice" data-on={on} data-choice={kind} style={engineToneVars(kind) as CSSProperties}>
      {on && <motion.span layoutId="choice-thumb" className="choice-thumb" transition={spring.snappy} />}
      <button className="choice-head" role="radio" aria-checked={on} onClick={onPick}>
        <span className="radio" aria-hidden="true" />
        <span className="engine-ico engine-ico-sm"><Icon size={14} /></span>
        <span className="min-w-0 flex-1">
          <span className="flex items-center gap-2 text-[14px] font-semibold">{title}{badge && <span className="engine-badge">{badge}</span>}
            {current && <span className="chip" style={{ height: 20, fontSize: 11 }}><i className="dot" data-on="true" />正在使用</span>}</span>
          <span className="faint block truncate text-[12px]">{desc}</span>
        </span>
      </button>
      <AnimatePresence initial={false}>
        {on && children && (
          <motion.div key="body" className="choice-body" initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0, transition: { duration: .16 } }} transition={spring.soft}>
            <div className="choice-inner">{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function VolcQuick({ engine }: { engine: EnginePrefs }) {
  return (
    <div className="grid gap-3">
      <div className="quick-row"><span className="quick-label">API Key</span><SecretField name={engine.volc.auth === 'key' ? SECRET.volcKey : SECRET.volcToken}
        label={engine.volc.auth === 'key' ? '豆包 API Key' : '豆包 Access Token'} placeholder={engine.volc.auth === 'key' ? '粘贴新版控制台的 API Key' : '粘贴 Access Token'} /></div>
      <div className="quick-row"><span className="quick-label">资源</span>
        <Segmented label="豆包资源" size="sm" value={VOLC_RESOURCES.some((r) => r.id === engine.volc.resource_id) ? engine.volc.resource_id : VOLC_RESOURCES[0].id}
          onChange={(resource_id) => setEngine({ volc: { resource_id } })} options={VOLC_RESOURCES.map((r) => ({ value: r.id as string, label: r.label }))} />
      </div>
      <p className="faint text-[12px] leading-relaxed">在火山引擎「豆包语音」新版控制台创建 API Key，并开通<b>流式语音识别模型 2.0</b>（与上面的小时版 / 并发版一致）。旧版控制台的 App ID + Token 在设置里填写。</p>
      <TestButton kind="volc"><span className="engine-help"><HelpLink url={HELP.volcConsole}>火山引擎控制台</HelpLink><HelpLink url={HELP.volcDocs}>文档</HelpLink></span></TestButton>
    </div>
  );
}

function DashQuick({ engine }: { engine: EnginePrefs }) {
  return (
    <div className="grid gap-3">
      <div className="quick-row"><span className="quick-label">API Key</span><SecretField name={SECRET.dashscope} label="百炼 API Key" /></div>
      <div className="quick-row"><span className="quick-label">地域</span>
        <Segmented label="百炼地域" size="sm" value={engine.dashscope.region} onChange={(region) => setEngine({ dashscope: { region } })}
          options={[{ value: 'cn', label: '北京' }, { value: 'intl', label: '新加坡' }]} />
      </div>
      <p className="faint text-[12px] leading-relaxed">在阿里云百炼控制台的「API Key」页面创建；北京与新加坡的 Key 不通用。</p>
      <TestButton kind="dashscope"><span className="engine-help"><HelpLink url={HELP.bailianKey}>如何获取 API Key</HelpLink></span></TestButton>
    </div>
  );
}

/* `keys`: a shortcut re-recorded in settings for this app, kept when it is picked here. */
function HotkeyQuick({ app, keys }: { app: HotkeyApp | null; keys: string }) {
  if (!app) return null;
  return (
    <div className="grid gap-2.5">
      <div className="quick-row"><span className="quick-label">快捷键</span><KeyCaps combo={keys || app.combo} /><span className="faint text-[12px]">{STYLE_TEXT[app.style]}</span></div>
      <p className="text-[12.5px] leading-relaxed">{app.mic_hint}</p>
      <span className="flex flex-wrap gap-1.5">
        {hotkeyWarnings(app, keys).map((w) => <Tip key={w.id} text={w.tip}><span className="warn-chip" tabIndex={0}>{w.label}</span></Tip>)}
      </span>
      <p className="faint text-[12px]">它会把文字直接输入到光标处，不能记成点声笔记。快捷键改过的话，到设置里录入同一组合。</p>
    </div>
  );
}

/* ── 3 试一句 ────────────────────────────────────────────────────────────── */
function TryStep({ onBack }: { onBack: () => void }) {
  const state = useStore((s) => s.state);
  const engine = useStore((s) => s.engine);
  const result = useStore((s) => s.wizardResult);
  const reduced = useStore((s) => !!s.prefs?.appearance.reduced_motion);
  // The Hanvon PV60 records with its voice key, not ⑥ (as in step 1).
  const pv60 = useStore((s) => s.selected) === 'pv60';
  const keyName = pv60 ? '语音键' : '⑥';
  const [since] = useState(() => Date.now());
  const [arming, setArming] = useState(false);
  const [typed, setTyped] = useState('');
  const baseline = useRef('');
  const box = useRef<HTMLTextAreaElement>(null);
  const fresh = result && result.at >= since ? result : null;
  const phase = state.phase;
  // The text box as this round began: only new text counts as the other app typing.
  useEffect(() => { if (phase === 'starting' || phase === 'recording') baseline.current = typed; }, [phase]);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    useStore.setState({ wizardResult: null });
    if (engine.external) box.current?.focus();
  }, [engine.external]);
  async function arm() {
    setArming(true);
    await act('action', { action: 'arm' });
    setArming(false);
  }
  const external = engine.external;
  const typedOk = external && typedThisRound(typed, baseline.current);
  let body: ReactNode;
  if (phase === 'disconnected') {
    body = (
      <Stage tone="calm" title="语音输入已暂停" text={`启用后按住 ${keyName} 说一句话。`}>
        <button className="btn btn-primary" disabled={arming} onClick={arm}>{arming ? <Loader2 size={14} className="animate-spin" /> : <Mic size={15} />}启用语音</button>
      </Stage>
    );
  } else if (phase === 'error') {
    body = (
      <Stage tone="bad" title="语音引擎没有接通" text={state.message || '连接异常'}>
        <span className="flex gap-2"><button className="btn btn-sm" onClick={arm}><RotateCcw size={13} />重试</button>
          <button className="btn btn-ghost btn-sm" onClick={onBack}>回到上一步检查</button></span>
      </Stage>
    );
  } else if (phase === 'recording') {
    body = (
      <Stage tone="live" title="正在听…" text={`说完松开 ${keyName}（短按开始的话，再按一下）。`}>
        <span className="mode-input block"><LiveWave width={360} height={60} state="listening" tone="input-wizard" reduced={reduced} /></span>
      </Stage>
    );
  } else if (phase === 'processing' || phase === 'starting') {
    body = <Stage tone="calm" title={phase === 'starting' ? `正在接入${engine.label}…` : '正在识别…'} text={external ? `${handedTo(engine.app)}，等它输入文字` : '马上显示在这里'} spinner />;
  } else if (fresh && fresh.error) {
    body = (
      <Stage tone="bad" title="这一句没有识别成功" text={fresh.error}>
        <span className="flex gap-2"><button className="btn btn-ghost btn-sm" onClick={onBack}>回到上一步检查</button></span>
      </Stage>
    );
  } else if (fresh && fresh.empty) {
    body = <Stage tone="calm" title="没听到说话" text="靠近笔再说一次；轻声也可以，气声识别会自动帮忙。" />;
  } else if (fresh && fresh.external) {
    body = (
      <Stage tone={typedOk ? 'ok' : 'calm'} title={typedOk ? '成功了' : handedTo(fresh.app)} text={typedOk ? `${fresh.app ?? engine.label} 把文字输入到了下面的框里。` : '看看下面的框里有没有出现文字；没有的话，检查它的快捷键和麦克风设置。'} />
    );
  } else if (fresh) {
    body = (
      <div className="try-result" data-wizard-result>
        <VoiceScore text={fresh.text.trim() || '没有识别到文字'} stage="answer" envelope={fresh.envelope ?? null} size={24} reduced={reduced} />
        <p className="try-ok"><Check size={14} strokeWidth={2.8} />{fresh.text.trim() ? `${engine.label}识别成功` : '没有识别到文字'}
          {fresh.seconds != null && <span className="faint num"> · 松开后 {fresh.seconds.toFixed(1)} 秒</span>}</p>
        <p className="faint text-[12px]">文字只显示在这里，没有输入到别处。想再试，就再按一次 {keyName}。</p>
      </div>
    );
  } else {
    body = <Stage tone="calm" title={`按住 ${keyName} 说一句话`} text={external ? `松开后，${engine.label} 会把文字输入到下面的框里。` : '松开后结果显示在这里，不会输入到别的窗口。'} />;
  }
  const Art = pv60 ? Pv60Art : BaseusArt;
  return (
    <div className="wizard-split">
      <div className="wizard-art"><Art className="h-full w-full" highlight="6" /></div>
      <div className="grid content-start gap-3">
        <div className="try-stage glass-flat">
          <AnimatePresence mode="popLayout" initial={false}>
            <motion.div key={`${phase}-${fresh?.at ?? 0}`} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -6, transition: { duration: .12 } }} transition={spring.soft}>{body}</motion.div>
          </AnimatePresence>
        </div>
        {external && (
          <label className="grid gap-1.5">
            <span className="faint text-[12px]">点一下这个框，再按 {keyName} 说话：</span>
            <textarea ref={box} className="field min-h-[72px]" aria-label="试说输入框" value={typed} onChange={(e) => setTyped(e.target.value)}
              placeholder={`${engine.label} 输入的文字会出现在这里`} />
          </label>
        )}
        <p className="faint flex items-center gap-1.5 text-[12px]"><Sparkles size={12} />语音引擎：{engine.label}{engine.external ? '（热键桥接）' : ''}</p>
      </div>
    </div>
  );
}

function Stage({ tone, title, text, spinner, children }: { tone: 'calm' | 'live' | 'ok' | 'bad'; title: string; text: string; spinner?: boolean; children?: ReactNode }) {
  return (
    <div className="try-copy" data-tone={tone}>
      <div className="try-title">{spinner && <Loader2 size={18} className="animate-spin" />}{tone === 'ok' && <Check size={18} strokeWidth={2.8} />}{title}</div>
      <p className="try-text">{text}</p>
      {children && <div className="mt-3">{children}</div>}
    </div>
  );
}

/* ── 4 完成 ──────────────────────────────────────────────────────────────── */
const CHEATS: { key: string; title: string; text: string; icon?: typeof Mic }[] = [
  { key: '⑥', title: '说话', text: '按住说话、松开结束；或短按开始、再短按结束' },
  { key: '⑦', title: '模式 · 闪念笔记', text: '长按：在 输入 / 笔记 / 演示 之间切换；短按：记一条闪念笔记', icon: NotebookPen },
  { key: '⑤', title: '鼠标', text: '单击 = 左键 · 双击 = 双击 · 按住 = 移动光标，再按音量 +/− 上下滚动', icon: MousePointerClick },
  { key: '③', title: '抓起', text: '长按：抓起去框选、拖动；再按一下 ③ 放下' },
  { key: '④', title: '右键', text: '长按：右键（演示模式和放映时照常黑屏）' },
  { key: '演示', title: '演示模式', text: '按住 ⑤ 用激光 / 聚光灯 / 放大镜，双击 ⑤ 换工具；② 常亮激光，⑧ 按住画标注', icon: Presentation },
];

/* The Hanvon PV60: its voice key records; its other keys work once bound in settings. */
const PV60_CHEATS: typeof CHEATS = [
  { key: '语音键', title: '说话', text: '按住说话、松开结束；选了“单击切换”时按一下开始、再按一下结束' },
  { key: '其他键', title: '自定义', text: '其他按键在「设置 → 按键 → 汉王」绑定后生效（需先启用语音）' },
];

function DoneStep() {
  const engine = useStore((s) => s.engine);
  const items = useStore((s) => s.selected) === 'pv60' ? PV60_CHEATS : CHEATS;
  return (
    <div className="grid gap-4">
      <div className="flex items-center gap-3">
        <span className="done-badge"><Check size={20} strokeWidth={2.8} /></span>
        <p className="muted text-[13px] leading-relaxed">语音引擎：<b>{engine.label}</b>{engine.external ? '（热键桥接）' : ''}。下面是笔上的按键速查；以后在「设置」里随时调整，也可以重新运行这个向导。</p>
      </div>
      <div className="cheat" data-cheat>
        {items.map((item, i) => (
          <motion.div key={item.key} className="cheat-item" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ ...spring.soft, delay: .04 * i }}>
            <span className="key-badge">{item.key}</span>
            <span className="min-w-0">
              <span className="block text-[13.5px] font-semibold">{item.title}</span>
              <span className="muted block text-[12.5px] leading-relaxed">{i === 0 && engine.external ? `${item.text}（交给 ${engine.label}）` : item.text}</span>
            </span>
          </motion.div>
        ))}
      </div>
    </div>
  );
}
