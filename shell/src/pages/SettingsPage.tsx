import { AnimatePresence, motion } from 'motion/react';
import {
  AlertTriangle, AppWindow, AudioLines, AudioWaveform, Bell, Bug, CircleDot, Command, Flashlight, FolderOpen, Hand, Keyboard, MousePointer2, MousePointerClick, Palette, Play,
  Presentation, Printer, Radar, RotateCcw, ShieldAlert, ShieldCheck, Usb, Wand2, Wind, X, Zap, ZoomIn,
} from 'lucide-react';
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { StatusDot } from '../components/EngineParts';
import { AppUpdateCard } from '../components/AppUpdate';
import { Glass, PageHeader, Range, Segmented, SettingRow, spring, Switch } from '../components/ui';
import { shell } from '../lib/bridge';
import { elevationView } from '../lib/elevation';
import { bindingText, slotLabel } from '../lib/format';
import { act, useStore } from '../lib/store';
import type { Bindings, ElevationStatus, OverlayAnchor, OverlayMonitor, Prefs, Tool, WhisperMode } from '../lib/types';
import { EngineSection } from './EngineSection';
import { ANCHORS } from '../overlay/layout';

type DeviceTab = 'baseus' | 'pv60' | 'tidepool';
type SetPrefs = (patch: Record<string, unknown>) => Promise<void>;

function usePrefs() {
  const prefs = useStore((s) => s.prefs);
  const set = async (patch: Partial<Record<keyof Prefs, unknown>>) => {
    const data = await act<Prefs>('prefs_set', { patch });
    if (data) useStore.setState({ prefs: data });
  };
  return [prefs, set] as const;
}

const SECTIONS = [
  { id: 'engine', label: '语音引擎', icon: AudioWaveform },
  { id: 'voice', label: '语音识别', icon: AudioLines },
  { id: 'devices', label: '设备', icon: Usb },
  { id: 'keys', label: '按键', icon: Keyboard },
  { id: 'airmouse', label: '飞鼠', icon: MousePointerClick },
  { id: 'present', label: '演示', icon: Presentation },
  { id: 'hotkeys', label: '快捷键', icon: Command },
  { id: 'feedback', label: '声音与胶囊', icon: Bell },
  { id: 'appearance', label: '外观', icon: Palette },
  { id: 'general', label: '通用', icon: ShieldCheck },
] as const;
type SectionId = typeof SECTIONS[number]['id'];

/* Sticky section bar with scroll-spy: the highlighted pill follows the reader,
   a click glides to the section. */
function SectionBar({ reduced }: { reduced: boolean }) {
  const [active, setActive] = useState<SectionId>('engine');
  const locked = useRef(0);
  const bar = useRef<HTMLDivElement>(null);
  const spy = useCallback(() => {
    if (Date.now() < locked.current) return;
    const main = document.getElementById('main');
    if (!main) return;
    const top = main.getBoundingClientRect().top + 90;
    let current: SectionId = SECTIONS[0].id;
    for (const s of SECTIONS) {
      const el = document.getElementById(`set-${s.id}`);
      if (el && el.getBoundingClientRect().top <= top) current = s.id;
    }
    if (main.scrollTop + main.clientHeight >= main.scrollHeight - 4) current = SECTIONS[SECTIONS.length - 1].id;
    setActive(current);
  }, []);
  useEffect(() => {
    const main = document.getElementById('main');
    main?.addEventListener('scroll', spy, { passive: true });
    const unlock = () => { locked.current = 0; spy(); };
    main?.addEventListener('scrollend', unlock);
    return () => { main?.removeEventListener('scroll', spy); main?.removeEventListener('scrollend', unlock); };
  }, [spy]);
  // Keep the active pill visible when the bar scrolls sideways (narrow window).
  // Only the bar moves: scrollIntoView would also nudge <main> and cancel its smooth scroll.
  useEffect(() => {
    const el = bar.current;
    const pill = el?.querySelector<HTMLElement>('[aria-current="true"]');
    if (!el || !pill) return;
    const left = pill.offsetLeft - 8, right = pill.offsetLeft + pill.offsetWidth + 8;
    if (left < el.scrollLeft) el.scrollTo({ left, behavior: reduced ? 'auto' : 'smooth' });
    else if (right > el.scrollLeft + el.clientWidth) el.scrollTo({ left: right - el.clientWidth, behavior: reduced ? 'auto' : 'smooth' });
  }, [active, reduced]);
  function go(id: SectionId) {
    setActive(id);
    locked.current = Date.now() + 900;
    document.getElementById(`set-${id}`)?.scrollIntoView({ behavior: reduced ? 'auto' : 'smooth', block: 'start' });
  }
  return (
    <div ref={bar} className="section-bar floating" role="navigation" aria-label="设置分区">
      {SECTIONS.map(({ id, label, icon: Icon }) => (
        <button key={id} aria-current={active === id} onClick={() => go(id)}>
          {active === id && <motion.span layoutId="section-thumb" className="liquid absolute inset-0 rounded-full" transition={spring.snappy} />}
          <Icon size={14} />
          <span>{label}</span>
        </button>
      ))}
    </div>
  );
}

function Section({ id, title, hint, action, children }: { id: SectionId; title: string; hint?: string; action?: ReactNode; children: ReactNode }) {
  const Icon = SECTIONS.find((s) => s.id === id)!.icon;
  return (
    <section id={`set-${id}`} className="settings-section" aria-label={title}>
      <div className="section-title">
        <h2><Icon size={15} />{title}</h2>
        {hint && <span className="faint min-w-0 truncate text-[12px]">{hint}</span>}
        {action && <span className="ml-auto flex shrink-0 items-center gap-2">{action}</span>}
      </div>
      <div className="grid gap-4">{children}</div>
    </section>
  );
}

export function SettingsPage() {
  const [prefs, setPrefs] = usePrefs();
  const engine = useStore((s) => s.engine);
  const openWizard = useStore((s) => s.openWizard);
  if (!prefs) return null;
  const set = setPrefs as SetPrefs;
  return (
    <div className="pb-10">
      <PageHeader eyebrow="少一点打扰，多一点专注" title="设置" />
      <SectionBar reduced={prefs.appearance.reduced_motion} />
      <SettingsWarnings />
      <div className="grid gap-9">
        <Section id="engine" title="语音引擎" hint={`当前：${engine.label}`}
          action={<button className="btn btn-ghost btn-sm" onClick={openWizard}><Wand2 size={14} />重新运行配置向导</button>}>
          <EngineSection prefs={prefs} />
        </Section>
        <Section id="voice" title="语音识别" hint="收音、气声与实时文字">
          <Glass className="px-6 py-1">
            <SettingRow icon={<AudioLines size={17} />} title="实时文字" description="豆包和百炼支持边说边显示。OpenLess 是否显示实时文字取决于它的远程输入支持；没有实时文字时显示声线，说完后显示定稿。" />
            <WhisperRow prefs={prefs} setPrefs={set} />
            <DiagnosticsRow prefs={prefs} setPrefs={set} />
          </Glass>
        </Section>
        <Section id="devices" title="设备" hint="多支笔、Tidepool 与笔的原生快捷键">
          <Glass className="px-6 py-1">
            <SettingRow icon={<Hand size={17} />} title="随拿随用"
              description="倍思与 Tidepool 同时待命，拿起哪个就用哪个；关闭后只用当前选择的设备。">
              <Switch label="随拿随用" checked={prefs.devices?.coexist ?? true} onChange={(v) => set({ devices: { coexist: v } })} />
            </SettingRow>
            <SettingRow icon={<Printer size={17} />} title="拦截笔的放映快捷键"
              description={<>笔的部分按键会同时发出 PowerPoint 放映快捷键，例如 ⑧ 会发 <span className="kbd">Ctrl</span>+<span className="kbd">P</span>，在普通窗口里会弹出“打印”；长按 ④ 会发 <span className="kbd">B</span>（黑屏），会在编辑框里打出一个 b。开启后只在 PowerPoint / WPS 放映时放行；演示模式下长按 ④ 照常黑屏。</>}>
              <Switch label="拦截笔的放映快捷键" checked={prefs.pen?.guard_shortcuts ?? true} onChange={(v) => set({ pen: { guard_shortcuts: v } })} />
            </SettingRow>
          </Glass>
        </Section>
        <Section id="keys" title="按键" hint="每支笔的按键不同，分别设置">
          <BindingsCard />
        </Section>
        <Section id="airmouse" title="飞鼠" hint="单击 ⑤ 就是左键；③④ 短按始终翻页">
          <AirMouseCard prefs={prefs} setPrefs={set} />
        </Section>
        <Section id="present" title="演示" hint="任何窗口都能用的激光、聚光灯与放大镜">
          <PresentCard />
        </Section>
        <Section id="hotkeys" title="快捷键" hint="不用笔时，从键盘控制">
          <HotkeysCard prefs={prefs} setPrefs={set} />
        </Section>
        <Section id="feedback" title="声音与胶囊" hint="不看屏幕也知道当前状态">
          <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(310px, 1fr))' }}>
            <SoundsCard prefs={prefs} setPrefs={set} />
            <CapsuleCard prefs={prefs} setPrefs={set} />
          </div>
        </Section>
        <Section id="appearance" title="外观">
          <Glass className="px-6 py-1">
            <SettingRow icon={<Palette size={17} />} title="主题" description="液态玻璃轻微透出窗口后方内容；Windows 关闭透明效果时使用实色底。">
              <Segmented label="主题" value={prefs.appearance.theme} onChange={(theme) => set({ appearance: { theme } })}
                options={[{ value: 'system', label: '跟随系统' }, { value: 'light', label: '浅色' }, { value: 'dark', label: '深色' }, { value: 'glass', label: '液态玻璃' }]} />
            </SettingRow>
            <SettingRow icon={<CircleDot size={17} />} title="桌面图标实时状态点"
              description="桌面、开始菜单和任务栏上的点声图标像托盘一样显示状态：绿点就绪，红点暂停。">
              <Switch label="桌面图标实时状态点" checked={prefs.appearance.live_shortcut ?? true} onChange={(v) => set({ appearance: { live_shortcut: v } })} />
            </SettingRow>
            <SettingRow icon={<Wind size={17} />} title="减少动态效果" description="保留状态提示，停止变形和流动动画。">
              <Switch label="减少动态效果" checked={prefs.appearance.reduced_motion} onChange={(v) => set({ appearance: { reduced_motion: v } })} />
            </SettingRow>
          </Glass>
        </Section>
        <Section id="general" title="通用">
          <AppUpdateCard />
          <Glass className="px-6 py-1">
            <SettingRow icon={<Wand2 size={17} />} title="配置向导" description="重新检查接收器和麦克风、选择语音引擎、试说一句。">
              <button className="btn btn-sm" onClick={openWizard}>重新运行</button>
            </SettingRow>
            <SettingRow icon={<AppWindow size={17} />} title="关闭后留在后台" description="关闭窗口后语音输入保持当前状态；双击托盘图标返回，右键可退出。" />
            <ElevationRow prefs={prefs} />
            <SettingRow icon={<ShieldCheck size={17} />} title="隐私" description="默认不保存录音（只有在“语音识别”里打开调试录音时，才在本机保留最近 3 段，24 小时后自动删除）。普通输入不保存文字；笔记整段保存在本机。选豆包语音或阿里云百炼时，录音会实时发给对应的云服务识别；它们的密钥用 Windows 数据保护加密，只保存在这台电脑。电量、时长与次数只记录数字，每支笔分别统计。" />
          </Glass>
          <ProbeCard />
        </Section>
      </div>
    </div>
  );
}

/* 兼容管理员权限窗口 (prefs general.run_elevated, mirrored by the shell). Windows drops
   simulated clicks and typing aimed at an administrator window (UIPI); DotVoice can run as
   administrator instead. Only the user's switch changes it: one UAC prompt, a logon task
   so later logons need none, and a restart into the other integrity level. */
function ElevationRow({ prefs }: { prefs: Prefs }) {
  const notify = useStore((s) => s.notify);
  const busy = useStore((s) => ['starting', 'recording', 'processing'].includes(s.state.phase));
  const [status, setStatus] = useState<ElevationStatus | null>(null);
  const [working, setWorking] = useState(false);
  const refresh = useCallback(() => {
    shell.elevationStatus().then((value) => setStatus(value ?? null)).catch(() => setStatus(null));
  }, []);
  useEffect(refresh, [refresh]);
  const view = elevationView(status, prefs);
  async function toggle(enabled: boolean) {
    if (busy) { notify('正在录音或识别，请等本段结束后再切换', 'error'); return; }
    setWorking(true);
    try {
      const answer = await shell.elevationSet(enabled);
      if (answer?.restarting) notify(enabled ? '已开启，点声正在以管理员身份重启…' : '已关闭，点声正在以普通权限重启…', 'success');
      else if (answer?.warning) notify(answer.warning, 'error');
      else notify(enabled ? '已开启：登录 Windows 时会以管理员身份启动点声' : '已关闭：已删除管理员启动任务', 'success');
    } catch (error) {
      notify(String((error as Error)?.message ?? error) || '没有完成，设置未更改', 'error');
    } finally {
      setWorking(false);
      refresh();
    }
  }
  async function restart() {
    setWorking(true);
    try {
      await shell.elevationRestart();
      notify('点声正在以管理员身份重启…', 'success');
    } catch (error) {
      notify(String((error as Error)?.message ?? error), 'error');
      setWorking(false);
    }
  }
  return (
    <SettingRow icon={<ShieldAlert size={17} />} title="兼容管理员权限窗口" badge={<StatusDot tone={view.tone}>{view.badge}</StatusDot>}
      description={<>有些程序以管理员身份运行（例如部分安卓模拟器里的短剧 App），Windows 会拦截普通程序发给它们的模拟点击、滚动和文字输入：光标能动，却点不下去、打不进字。开启后点声以管理员身份运行：确认一次 UAC，登记一个登录 Windows 时在后台以管理员身份启动点声的计划任务（以后登录不再弹窗），然后自动重启。<span className="faint">代价：点声和它打开的程序拥有完整权限。关闭后删除该任务，恢复普通权限运行。</span>{view.note && <span className="block pt-1" data-elevation-note>{view.note}</span>}</>}>
      {view.restart && <button className="btn btn-sm" disabled={working} onClick={restart}>以管理员身份重启</button>}
      <Switch label="兼容管理员权限窗口" checked={view.on} disabled={!view.enabled || working} onChange={toggle} />
    </SettingRow>
  );
}

function WhisperRow({ prefs, setPrefs }: { prefs: Prefs; setPrefs: SetPrefs }) {
  const value: WhisperMode = prefs.voice?.whisper ?? 'auto';
  const line = { auto: '检测到轻声时自动启用。', on: '每一段都按轻声处理，适合深夜、图书馆、会议旁听。', off: '按正常音量识别。' }[value];
  return (
    <SettingRow icon={<Wind size={17} />} title="气声识别"
      description={<>轻声、气声说话也能识别：提高收音增益、放宽静音判定，不再被当成“没听到”。<span className="faint">{line}</span></>}>
      <Segmented label="气声识别" value={value} onChange={(whisper) => setPrefs({ voice: { whisper } })}
        options={[{ value: 'auto', label: '自动' }, { value: 'on', label: '始终' }, { value: 'off', label: '关闭' }]} />
    </SettingRow>
  );
}

/* Opt-in debug recordings (prefs diagnostics.save_audio, bridge.diagnostics): the raw and the
   sent audio of the last 3 recordings plus the 气声 front end's numbers, for tuning soft
   speech with real data. Local files only; the core deletes them after 24 hours. */
/* Settings files the core could not read as they were: said here, not only in a log
   (prefs.json unreadable: defaults or the saved copy are in use; a key binding reset). */
function SettingsWarnings() {
  const prefsWarning = useStore((s) => s.prefsWarning);
  const bindingsWarning = useStore((s) => s.bindings?.warning ?? '');
  const items = [prefsWarning, bindingsWarning].filter(Boolean);
  if (!items.length) return null;
  return (
    <div className="mb-6 grid gap-2" data-settings-warnings>
      {items.map((text) => (
        <div key={text} className="banner" role="status">
          <span className="banner-ico" data-tone="bad"><AlertTriangle size={15} /></span>
          <span className="flex-1">{text}</span>
        </div>
      ))}
    </div>
  );
}

function DiagnosticsRow({ prefs, setPrefs }: { prefs: Prefs; setPrefs: SetPrefs }) {
  const on = prefs.diagnostics?.save_audio ?? false;
  return (
    <SettingRow icon={<Bug size={17} />} title="保存最近 3 段录音用于调试（仅本机，24 小时后自动删除）"
      description={<>每段保存两份录音：笔的麦克风原始声音，和经过气声处理后发给语音引擎的声音，另附处理参数和识别结果，用来调整轻声识别。只在这台电脑上，不会上传。<span className="faint">默认关闭；关闭时已保存的录音会立即删除。</span></>}>
      {on && <button className="btn btn-ghost btn-sm" onClick={() => act('open_diagnostics')}><FolderOpen size={14} />打开文件夹</button>}
      <Switch label="保存最近 3 段录音用于调试" checked={on} onChange={(save_audio) => setPrefs({ diagnostics: { save_audio } })} />
    </SettingRow>
  );
}

function BindingsCard() {
  const bindings = useStore((s) => s.bindings);
  const [tab, setTab] = useState<DeviceTab>('baseus');
  const [learning, setLearning] = useState<{ device: string; slot: string } | null>(null);
  const slots = ['toggle_mode', 'quick_note', 'arm_toggle', 'cancel'];
  const current = bindings?.devices?.[tab] ?? {};

  async function reset(slot?: string) {
    const data = await act<Bindings>('binding_reset', { device: tab, slot: slot ?? null });
    if (data) useStore.setState({ bindings: data });
  }
  async function clear(slot: string) {
    const data = await act<Bindings>('binding_set', { device: tab, slot, binding: null });
    if (data) useStore.setState({ bindings: data });
  }
  async function learn(slot: string) {
    useStore.setState({ learn: null });
    if (await act('binding_learn', { device: tab, slot })) setLearning({ device: tab, slot });
  }

  return (
    <Glass className="px-6 py-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <div className="title-sm">按键与模式切换</div>
          <p className="muted mt-0.5 text-[12.5px]">录音键属于语音协议，不能改作其它用途。</p>
        </div>
        <Segmented label="设备" value={tab} onChange={setTab}
          options={[{ value: 'baseus', label: '倍思' }, { value: 'pv60', label: '汉王' }, { value: 'tidepool', label: 'Tidepool' }]} />
      </div>
      <AnimatePresence mode="wait" initial={false}>
        <motion.div key={tab} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4, transition: { duration: .1 } }} transition={spring.soft}>
          {tab === 'tidepool' ? (
            <p className="glass-flat mt-4 px-4 py-3 text-[13px]">Tidepool 的按键由它自己的接收器处理。请在“快捷键”中设置切换键；笔记路由将在下一版接入。</p>
          ) : (
            <div className="well mt-4">
              {slots.map((slot) => {
                const binding = current[slot];
                return (
                  <div key={slot} className="binding-row grid items-center gap-4 px-4 py-2.5" style={{ gridTemplateColumns: 'minmax(96px,150px) minmax(0,1fr) auto' }}>
                    <div className="text-[13px] font-semibold">{slotLabel[slot]}</div>
                    <div className="min-w-0 truncate text-[13px]">
                      {binding ? (
                        <>
                          <span className="font-semibold">{binding.label || '自定义'}</span>
                          <span className="faint ml-2">{bindingText(binding)}{binding.ignore?.length ? ` · 松开 ${binding.ignore.join('/').toUpperCase()} 忽略` : ''}</span>
                          {binding.default && <span className="chip ml-2" style={{ height: 20, fontSize: 11 }}>默认</span>}
                        </>
                      ) : <span className="faint">未绑定</span>}
                    </div>
                    <div className="flex gap-1">
                      <button className="btn btn-sm" onClick={() => learn(slot)}>录制</button>
                      {binding && !binding.default && <button className="btn btn-ghost btn-sm btn-icon" onClick={() => reset(slot)} title="恢复默认" aria-label="恢复默认"><RotateCcw size={14} /></button>}
                      {binding && <button className="btn btn-ghost btn-sm btn-icon" onClick={() => clear(slot)} title="解除绑定" aria-label="解除绑定"><X size={14} /></button>}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          {tab === 'baseus' && <p className="faint mt-3 text-[12px] leading-relaxed">长按 ⑦ 在 输入 → 笔记 → 演示 之间循环。短按 ⑦ 记闪念笔记：第一下开始（笔的麦克风随之打开），第二下结束并保存到笔记，在任何模式下都不会输入到光标处，也不改变当前模式。已实测：⑦ 短按发 7A/7B，长按发 7C/7D。</p>}
          {tab === 'pv60' && <p className="faint mt-3 text-[12px]">汉王的额外按键要在“启用语音输入”后才能识别（需要专用 HID 握手）。</p>}
        </motion.div>
      </AnimatePresence>
      <LearnDialog open={learning} onClose={() => setLearning(null)} />
    </Glass>
  );
}

function LearnDialog({ open, onClose }: { open: { device: string; slot: string } | null; onClose: () => void }) {
  const learn = useStore((s) => s.learn);
  // A wheel key: one press of a pen key (the volume keys send their own codes); ⑤ may be held.
  const wheel = !!open && ['airmouse_wheel_up', 'airmouse_wheel_down'].includes(open.slot);
  const hold = !!open && ['airmouse_activity', 'airmouse_ctrl', 'annotate'].includes(open.slot);
  const long = !!open && open.slot === 'right_click';  // one long press; the short press stays native
  // PV60: one press is saved at once (keymap: only vendor keys take two taps); its release never fires.
  const single = open?.device === 'pv60';
  const status = learn?.status ?? 'waiting';
  useEffect(() => {
    if (status === 'done' || status === 'timeout' || status === 'cancelled') {
      const timer = window.setTimeout(onClose, status === 'done' ? 900 : 1600);
      return () => clearTimeout(timer);
    }
  }, [status, onClose]);
  async function cancel() {
    await act('binding_cancel');
    onClose();
  }
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') cancel(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  async function skip() {
    const data = await act<Bindings>('binding_skip');
    if (data) useStore.setState({ bindings: data });
  }
  const step = learn?.step ?? 'tap';
  return (
    <AnimatePresence>
      {open && (
        <motion.div className="dialog-scrim" onClick={(e) => e.target === e.currentTarget && cancel()}
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
          <motion.div role="dialog" aria-modal="true" aria-label="录制按键" className="floating w-[440px] max-w-[calc(100vw-48px)] rounded-[24px] px-7 py-6"
            initial={{ scale: .94, y: 14, opacity: 0 }} animate={{ scale: 1, y: 0, opacity: 1 }} exit={{ scale: .96, y: 6, opacity: 0 }} transition={spring.soft}>
            <div className="flex items-center gap-2">
              <span className={`listen-dot ${status === 'done' ? 'is-done' : ''}`} aria-hidden="true" />
              <div className="eyebrow">{slotLabel[open.slot]}</div>
            </div>
            <h2 className="title-lg mt-2 text-[22px]">
              {status === 'done' ? '已保存' : wheel ? '在笔上按一下这个键' : hold ? '按住这个键 1 秒再松开' : long ? '长按这个键约 1 秒再松开' : single ? '按一下这个键' : step === 'tap' ? '短按这个键两下' : '再长按 1 秒后松开'}
            </h2>
            <p className="muted mt-2 text-[13px] leading-relaxed">
              {wheel ? '可以先按住 ⑤ 再按（⑤ 本身不会被录下）。按住 ⑤ 时这个键是滚轮：按一下滚一段，按住持续滚动，演示模式用放大镜时调倍数；不按 ⑤ 时保持原功能。'
                : hold ? '记录“按下”和“松开”两种报文：按下开始，松开结束。'
                : long ? '只记录长按发出的键码，短按仍是原来的功能（翻页）。录好后，输入、笔记模式下长按就是右键。'
                : single ? '只记录这个键按下时的报文，按一下即可保存；这一下不会触发功能。'
                : step === 'tap' ? '两下是为了同时记住“开始 / 结束”两种报文，这样每按一次都会生效。'
                  : '长按的“按下”会触发，“松开”会被忽略，避免一次长按切换两次。'}
            </p>
            <div className="mt-4 flex min-h-[34px] flex-wrap items-center gap-2">
              {(learn?.captured ?? []).map((code, i) => (
                <motion.span key={i} initial={{ scale: .6, opacity: 0 }} animate={{ scale: 1, opacity: 1 }} transition={spring.snappy} className="chip num" style={{ height: 30, fontSize: 13 }}>{code}</motion.span>
              ))}
              {!(learn?.captured?.length) && <span className="faint text-[13px]">等待按键…</span>}
            </div>
            {learn?.message && <p className="mt-3 text-[13px]" style={{ color: status === 'error' ? 'var(--bad)' : 'var(--ok)' }}>{learn.message}</p>}
            <div className="mt-5 flex justify-end gap-2">
              {!wheel && !hold && !long && step === 'hold' && status !== 'done' && <button className="btn btn-sm" onClick={skip}>这个键没有长按，跳过</button>}
              <button className="btn btn-ghost btn-sm" onClick={cancel}>{status === 'done' ? '关闭' : '取消'}</button>
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function accelerator(e: React.KeyboardEvent) {
  const parts: string[] = [];
  if (e.ctrlKey) parts.push('Ctrl');
  if (e.altKey) parts.push('Alt');
  if (e.shiftKey) parts.push('Shift');
  if (e.metaKey) parts.push('Super');
  const code = e.code;
  if (['ControlLeft', 'ControlRight', 'AltLeft', 'AltRight', 'ShiftLeft', 'ShiftRight', 'MetaLeft', 'MetaRight'].includes(code)) return null;
  const key = code.startsWith('Key') ? code.slice(3) : code.startsWith('Digit') ? code.slice(5) : code;
  if (!parts.length && !/^F\d+$/.test(key)) return null;
  return [...parts, key].join('+');
}

function HotkeyField({ value, onChange, label }: { value: string; onChange: (value: string) => void; label: string }) {
  const [recording, setRecording] = useState(false);
  return (
    <div className="flex items-center gap-1.5">
      <div className={`hotkey ${recording ? 'is-recording' : ''}`}>
        <input readOnly className="field num w-[180px] text-center" aria-label={label}
          value={recording ? '请按下组合键…' : value || '未设置'}
          onFocus={() => setRecording(true)} onBlur={() => setRecording(false)}
          onKeyDown={(e) => {
            e.preventDefault();
            if (e.key === 'Escape') { (e.target as HTMLInputElement).blur(); return; }
            const accel = accelerator(e);
            if (accel) { onChange(accel); (e.target as HTMLInputElement).blur(); }
          }} />
      </div>
      <button className="btn btn-ghost btn-sm btn-icon" style={{ visibility: value ? 'visible' : 'hidden' }} onClick={() => onChange('')} aria-label="清除快捷键" title="清除快捷键"><X size={14} /></button>
    </div>
  );
}

function HotkeysCard({ prefs, setPrefs }: { prefs: Prefs; setPrefs: SetPrefs }) {
  return (
    <Glass className="px-6 py-1">
      <SettingRow icon={<Command size={17} />} title="切换模式" description="任何程序在前台时都能用；适合 Tidepool 或不想用笔上按键时。需要包含 Ctrl / Alt / Shift / Win 之一。">
        <HotkeyField label="切换模式快捷键" value={prefs.hotkeys.toggle_mode} onChange={(v) => setPrefs({ hotkeys: { toggle_mode: v } })} />
      </SettingRow>
      <SettingRow icon={<AudioLines size={17} />} title="启用 / 暂停语音" description="点击输入框后按下组合键即可录入；Esc 取消。">
        <HotkeyField label="启用暂停快捷键" value={prefs.hotkeys.arm_toggle} onChange={(v) => setPrefs({ hotkeys: { arm_toggle: v } })} />
      </SettingRow>
    </Glass>
  );
}

/* The pen as a mouse, one row per gesture ("key gesture = action"). Learnable rows show
   the bound code; ③'s long press (6B) is built in. ④'s long press (6D) and the volume keys
   (78 / 79, measured 2026-10-12) have defaults; any row can be recorded again. */
const MOUSE_ROWS: { slot: string | null; title: string; hint: string; unbound?: string }[] = [
  { slot: 'airmouse_activity', title: '⑤ 按住 = 移动光标', hint: '单击 = 左键，双击 = 双击；演示模式下双击换工具' },
  { slot: null, title: '③ 长按 = 抓起', hint: '6B · 按住左键去框选、拖动，再按一下 ③ 放下' },
  { slot: 'right_click', title: '④ 长按 = 右键', hint: '弹出右键菜单；笔同时发的 B 会被拦下，演示模式下照常黑屏', unbound: '点“录制”，长按 ④ 一次' },
  { slot: 'airmouse_wheel_up', title: '按住 ⑤ + 音量 + = 上滚', hint: '按一下滚一段，按住持续滚动；演示放大镜时放大' },
  { slot: 'airmouse_wheel_down', title: '按住 ⑤ + 音量 − = 下滚', hint: '按一下滚一段，按住持续滚动；演示放大镜时缩小' },
  { slot: 'airmouse_ctrl', title: '⑧ 按住 = Ctrl', hint: '配合单击 ⑤ 多选；演示模式下按住画标注、短按撤销' },
];

function AirMouseCard({ prefs, setPrefs }: { prefs: Prefs; setPrefs: SetPrefs }) {
  const bindings = useStore((s) => s.bindings);
  const active = useStore((s) => s.airmouseActive);
  const [learning, setLearning] = useState<{ device: string; slot: string } | null>(null);
  const baseus = bindings?.devices?.baseus ?? {};
  async function learn(slot: string) {
    useStore.setState({ learn: null });
    if (await act('binding_learn', { device: 'baseus', slot })) setLearning({ device: 'baseus', slot });
  }
  async function reset(slot: string) {
    const data = await act<Bindings>('binding_reset', { device: 'baseus', slot });
    if (data) useStore.setState({ bindings: data });
  }
  async function clear(slot: string) {
    const data = await act<Bindings>('binding_set', { device: 'baseus', slot, binding: null });
    if (data) useStore.setState({ bindings: data });
  }
  return (
    <Glass className="px-6 py-1">
      <SettingRow icon={<MousePointerClick size={17} />} title="飞鼠变成完整的鼠标" description={<>输入、笔记模式下：单击 ⑤ = 左键，双击 ⑤ = 双击，长按 ③ 抓起、再按 ③ 放下，长按 ④ = 右键；按住 ⑤ + 音量 +/− = 上下滚动（演示模式用放大镜时缩放）。③④ 短按始终翻页。{active ? '' : ' 接收器连接后生效。'}</>}>
        <Switch label="飞鼠鼠标" checked={prefs.airmouse.enabled} onChange={(v) => setPrefs({ airmouse: { enabled: v } })} />
      </SettingRow>
      <div className="well my-2" style={{ opacity: prefs.airmouse.enabled ? 1 : .55, transition: 'opacity .3s ease' }}>
        {MOUSE_ROWS.map(({ slot, title, hint, unbound }) => {
          const binding = slot ? baseus[slot] : undefined;
          const detail = binding ? `${binding.label ? `${binding.label} · ` : ''}${bindingText(binding)} · ${hint}`
            : slot ? `未绑定 · ${unbound ?? hint}` : hint;
          return (
            <div key={title} data-mouse-row={slot ?? 'grab'} className="binding-row grid items-center gap-3 px-4 py-2"
              style={{ gridTemplateColumns: 'minmax(128px,176px) minmax(0,1fr) auto' }}>
              <span className="truncate text-[13px] font-semibold">{title}</span>
              <span className={`truncate text-[12px] ${slot && !binding ? '' : 'faint'}`} title={detail}>{detail}</span>
              {slot ? (
                <div className="flex gap-1">
                  <button className="btn btn-sm" onClick={() => learn(slot)}>录制</button>
                  {/* Every row here has a default: a cleared one can go back to it too. */}
                  {!binding?.default && <button className="btn btn-ghost btn-sm btn-icon" onClick={() => reset(slot)} title="恢复默认" aria-label="恢复默认"><RotateCcw size={14} /></button>}
                  {binding && <button className="btn btn-ghost btn-sm btn-icon" onClick={() => clear(slot)} title="解除绑定" aria-label="解除绑定"><X size={14} /></button>}
                </div>
              ) : <span className="chip" style={{ height: 22, fontSize: 11 }}>内置</span>}
            </div>
          );
        })}
      </div>
      <SettingRow title="放映时保持原样" description="PowerPoint / WPS 全屏放映时不点击、不抓起、不弹右键菜单、不滚动（放大镜调倍数除外），长按 ④ 照常黑屏。">
        <Switch label="放映时保持原样" checked={prefs.airmouse.skip_slideshow} onChange={(v) => setPrefs({ airmouse: { skip_slideshow: v } })} />
      </SettingRow>
      <p className="faint pb-4 text-[12px] leading-relaxed">“按住 ⑤ + 音量键”是唯一的组合，也没有“用过飞鼠后几秒内”的隐藏规则；不按 ⑤ 时音量键保持原功能。演示模式下 ⑤ 双击换工具，③④ 保持原样。键盘钩子分不清是哪块键盘：抓起时键盘上的 PgUp 会放下。管理员权限的窗口不接受模拟点击。</p>
      <LearnDialog open={learning} onClose={() => setLearning(null)} />
    </Glass>
  );
}

function PresentCard() {
  const tool = useStore((s) => s.tool);
  const mode = useStore((s) => s.mode);
  const prefs = useStore((s) => s.prefs);
  const tools: { id: Tool; label: string; hint: string; icon: typeof Zap }[] = [
    { id: 'laser', label: '数码激光', hint: '带拖尾的红点', icon: Zap },
    { id: 'spotlight', label: '聚光灯', hint: '其余区域变暗', icon: Flashlight },
    { id: 'magnifier', label: '放大镜', hint: `光标处放大 ${((prefs?.present?.zoom ?? 200) / 100).toFixed(2).replace(/\.?0+$/, '')} 倍`, icon: ZoomIn },
    { id: 'mouse', label: '只用飞鼠', hint: '不显示任何效果', icon: MousePointer2 },
  ];
  async function setZoom(value: number) {
    const data = await act<Prefs>('prefs_set', { patch: { present: { zoom: value } } });
    if (data) useStore.setState({ prefs: data });
  }
  async function setHide(value: boolean) {
    const data = await act<Prefs>('prefs_set', { patch: { present: { hide_cursor: value } } });
    if (data) useStore.setState({ prefs: data });
  }
  return (
    <Glass className="px-6 pb-1 pt-5">
      <p className="muted text-[12.5px] leading-relaxed">在任何窗口上都能用：PPT、网页、PDF、桌面。长按 ⑦ 切到演示后，按住 ⑤ 使用当前工具（默认数码激光），双击 ⑤ 换下一个，单击 ⑤ 不点击；按一下 ② 常亮激光，再按一下关闭；⑧ 按住画标注、短按撤销。③④ 照常翻页，长按 ③ 开始放映、长按 ④ 黑屏；按住 ⑤ + 音量 +/− 上下滚动（用放大镜时调倍数）。⑥ 仍可语音输入。{mode === 'present' ? '' : ' 当前不在演示模式。'}</p>
      <div className="mode-present mt-4 grid gap-2" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(128px, 1fr))' }}>
        {tools.map((t) => {
          const on = tool === t.id;
          const Icon = t.icon;
          return (
            <div key={t.id} className="relative">
              <button aria-pressed={on} onClick={() => act('set_tool', { tool: t.id })} className={`tool-tile ${t.id !== 'mouse' ? 'has-try' : ''}`}>
                {on && <motion.span layoutId="tool-thumb" className="tool-thumb" transition={spring.snappy} />}
                <span className="tool-ico relative"><Icon size={16} /></span>
                <span className="relative mt-2 block text-[13.5px] font-semibold">{t.label}</span>
                <span className="faint relative mt-0.5 block text-[11.5px]">{t.hint}</span>
              </button>
              {t.id !== 'mouse' && (
                <button className="btn btn-sm tool-try" onClick={() => act('preview_tool', { tool: t.id })} title="在光标处显示 3 秒，不需要笔">
                  <Play size={11} />试看 3 秒
                </button>
              )}
            </div>
          );
        })}
      </div>
      {prefs && (
        <SettingRow title="使用激光 / 聚光灯时隐藏鼠标指针" description="只显示红点，和硬件激光笔一样；松开、换模式或退出时立即恢复。">
          <Switch label="隐藏鼠标指针" checked={prefs.present?.hide_cursor ?? true} onChange={setHide} />
        </SettingRow>
      )}
      {prefs && (
        <SettingRow title="放大镜倍数" description={`${((prefs.present?.zoom ?? 200) / 100).toFixed(2).replace(/\.?0+$/, '')} 倍。按住 ⑤ 使用放大镜时按笔侧面的音量 + / − 也能随时调节（按住连续调）。`}>
          <Range label="放大镜倍数" min={125} max={400} step={25} value={prefs.present?.zoom ?? 200} className="w-[180px]"
            onChange={(v) => setZoom(v)} />
        </SettingRow>
      )}
    </Glass>
  );
}

const CUES: [string, string][] = [
  ['mode_input', '切到输入'], ['mode_notes', '切到笔记'], ['start_input', '开始 · 输入'], ['start_notes', '开始 · 笔记'],
  ['stop', '结束收音'], ['done_input', '已输入'], ['done_notes', '已保存'], ['error', '出错'], ['armed', '启用'], ['disarmed', '暂停'],
];

function SoundsCard({ prefs, setPrefs }: { prefs: Prefs; setPrefs: SetPrefs }) {
  return (
    <Glass className="px-6 py-1">
      <SettingRow title="声音提示" description="两种模式音色不同：输入清脆上扬，笔记温暖下行。">
        <Switch label="声音提示" checked={prefs.sounds.enabled} onChange={(v) => setPrefs({ sounds: { enabled: v } })} />
      </SettingRow>
      <SettingRow title="保持音频设备唤醒" description="蓝牙音箱 / 耳机、显示器音频安静几秒后会休眠，吞掉第一声提示音。开启后保持一个听不见的音频流（蓝牙设备会稍多耗一点电）。">
        <Switch label="保持音频设备唤醒" checked={prefs.sounds.keep_awake ?? true} disabled={!prefs.sounds.enabled} onChange={(v) => setPrefs({ sounds: { keep_awake: v } })} />
      </SettingRow>
      <SettingRow title="音量">
        <Segmented label="提示音音量" value={prefs.sounds.volume} onChange={(volume) => setPrefs({ sounds: { volume } })}
          options={[{ value: 'low', label: '低' }, { value: 'medium', label: '中' }, { value: 'high', label: '高' }]} />
      </SettingRow>
      <div className="grid grid-cols-2 gap-1 py-3">
        {CUES.map(([name, label]) => (
          <button key={name} className="btn btn-ghost btn-sm cue justify-start" disabled={!prefs.sounds.enabled} onClick={() => act('sound_preview', { name })}>
            <span className="cue-play"><Play size={10} fill="currentColor" /></span>{label}
          </button>
        ))}
      </div>
    </Glass>
  );
}

function CapsuleCard({ prefs, setPrefs }: { prefs: Prefs; setPrefs: SetPrefs }) {
  const [monitors, setMonitors] = useState<OverlayMonitor[]>([]);
  const [loading, setLoading] = useState(true);
  const [placing, setPlacing] = useState(false);
  const [monitorError, setMonitorError] = useState('');
  const selected = prefs.overlay.monitor ?? '';
  const scope = prefs.overlay.all_monitors ? 'all' : selected ? 'one' : 'primary';
  const anchor = prefs.overlay.anchor ?? 'top-center';
  const name = (value: string) => value.replace(/^\\\\\.\\DISPLAY(\d+)$/i, '显示器 $1');
  const refreshMonitors = useCallback(async () => {
    setLoading(true);
    try {
      const displays = await shell.overlayMonitors();
      if (!displays?.length) throw new Error('暂时无法读取显示器列表，请刷新重试。');
      setMonitors(displays);
      setMonitorError('');
    } catch (error) { setMonitorError(String((error as Error).message || error)); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { refreshMonitors(); }, [refreshMonitors]);
  const missing = scope === 'one' && !!selected && !monitors.some(m => m.name === selected);
  async function place(patch: Record<string, unknown>) {
    setPlacing(true);
    try { await setPrefs({ overlay: { ...patch, position: null, positions: {} } }); }
    finally { setPlacing(false); }
  }
  async function chooseScope(value: string) {
    await place({ all_monitors: value === 'all', monitor: value === 'one' ? (selected || monitors.find(m => m.name)?.name || null) : null });
  }
  async function resetPosition() {
    setPlacing(true);
    try { await shell.overlayResetPosition(); }
    catch (error) { useStore.getState().notify(String(error), 'error'); }
    finally { setPlacing(false); }
  }
  return (
    <Glass className="px-6 py-1">
      <SettingRow title="灵动胶囊" description="桌面上的小胶囊：切换时显示当前模式，录音时显示声线（有可靠的实时文字时一并显示），笔记模式下展开成便签。置顶但不抢输入焦点。">
        <Switch label="灵动胶囊" checked={prefs.overlay.enabled} onChange={(v) => setPrefs({ overlay: { enabled: v } })} />
      </SettingRow>
      <SettingRow title="切换时显示">
        <Switch label="切换时显示" checked={prefs.overlay.on_switch} disabled={!prefs.overlay.enabled} onChange={(v) => setPrefs({ overlay: { on_switch: v } })} />
      </SettingRow>
      <SettingRow title="保存后停留" description={`${prefs.overlay.linger_seconds} 秒后收起便签；点图钉可一直保留。`}>
        <Range label="保存后停留" min={2} max={30} value={prefs.overlay.linger_seconds} className="w-[130px]" disabled={!prefs.overlay.enabled}
          onChange={(v) => setPrefs({ overlay: { linger_seconds: v } })} />
      </SettingRow>
      <SettingRow title="显示屏幕">
        <div className="flex max-w-[230px] flex-col items-stretch gap-2">
          <select className="field text-[12px]" aria-label="胶囊显示范围" value={scope} disabled={loading || placing}
            onChange={(event) => chooseScope(event.target.value)}>
            <option value="primary">跟随主显示器（默认）</option>
            <option value="one" disabled={!selected && !monitors.some(m => m.name)}>指定一块屏幕</option>
            <option value="all">所有屏幕同时显示</option>
          </select>
          {scope === 'one' && <select className="field text-[12px]" aria-label="胶囊显示器" value={selected} disabled={loading || placing}
            onChange={(event) => place({ monitor: event.target.value, all_monitors: false })}>
            {missing && <option value={selected}>{name(selected)}（未连接）</option>}
            {monitors.map((m, index) => <option key={m.name ?? `unnamed-${index}`} value={m.name ?? `unnamed-${index}`} disabled={!m.name}>
              {m.name ? name(m.name) : '无名称显示器'}{m.primary ? ' · 主屏' : ''} · {m.width}×{m.height}
            </option>)}
          </select>}
          <button className="btn btn-ghost btn-sm self-end" disabled={loading} onClick={refreshMonitors} aria-label="刷新显示器">
            <RotateCcw size={13} className={loading ? 'animate-spin' : ''} />刷新
          </button>
        </div>
      </SettingRow>
      <SettingRow title="预设位置" description={ANCHORS.find(item => item.value === anchor)?.label}>
        <div className="flex flex-col items-end gap-2">
          <div className="grid grid-cols-3 gap-1 rounded-xl p-1.5" role="group" aria-label="胶囊预设位置"
            style={{ border: '1px solid var(--hair)', background: 'var(--well)' }}>
            {ANCHORS.map(item => <button key={item.value} className="btn btn-ghost h-7 w-8 min-w-0 p-0"
              aria-label={item.label} title={item.label} aria-pressed={anchor === item.value} disabled={placing}
              onClick={() => place({ anchor: item.value as OverlayAnchor })}
              style={anchor === item.value ? { background: 'var(--brand)', color: '#fff' } : { color: 'var(--faint)' }}>
              <span className="h-1.5 w-3 rounded-full" style={{ background: 'currentColor', opacity: anchor === item.value ? 1 : .4 }} />
            </button>)}
          </div>
          <button className="btn btn-ghost btn-sm" disabled={placing} onClick={resetPosition}>恢复预设位置</button>
        </div>
      </SettingRow>
      <p className="faint pb-4 text-[12px]">{monitorError || (missing && !loading
        ? '所选屏幕未连接，暂用主屏；接回后自动恢复。'
        : scope === 'all' ? '每块屏幕同步显示；固定或收起会同步到所有胶囊。'
          : monitors.some(m => !m.name) ? '无名称的屏幕无法单独记住；仍可在所有屏幕上显示。' : '选择胶囊所在的屏幕和位置。')}
        {' '}可拖动胶囊调整，各屏分别记住；更换屏幕或预设位置后重新对齐。</p>
    </Glass>
  );
}

function ProbeCard() {
  const probe = useStore((s) => s.probe);
  const [on, setOn] = useState(false);
  useEffect(() => () => { act('probe', { enabled: false }); }, []);
  async function toggle(value: boolean) {
    if (await act('probe', { enabled: value })) setOn(value);
    if (value) useStore.setState({ probe: [] });
  }
  return (
    <Glass className="px-6 py-1">
      <SettingRow icon={<Radar size={17} />} title="按键探测" description="只读显示倍思接收器收到的报文，用来确认每个键发出的键码。">
        <Switch label="按键探测" checked={on} onChange={toggle} />
      </SettingRow>
      <AnimatePresence initial={false}>
        {on && (
          <motion.div key="probe" initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }} transition={spring.soft} className="overflow-hidden">
            <div className="scroll num glass-flat mb-4 max-h-[180px] space-y-1 px-3 py-2 text-[12.5px]">
              {probe.length === 0 && <p className="faint m-0 flex items-center gap-2 py-1"><span className="listen-dot" />按一下笔上的键…</p>}
              {probe.map((p) => (
                <motion.div key={p.at + (p.code ?? '') + (p.vk ?? '')} className="flex gap-3" initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }}>
                  <span className="faint w-[70px]">{new Date(p.at).toLocaleTimeString('zh-CN')}</span>
                  <span className="w-[80px] font-semibold">{p.interface}</span>
                  <span>{p.code ? `键码 ${p.code}` : p.vk != null ? `VK ${p.vk.toString(16).toUpperCase()} ${p.down ? '按下' : '松开'}` : p.buttons != null ? `鼠标按钮 0x${p.buttons.toString(16)}` : p.hex}</span>
                </motion.div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </Glass>
  );
}
