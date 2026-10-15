import { getCurrentWindow } from '@tauri-apps/api/window';
import { AnimatePresence, motion } from 'motion/react';
import {
  AlertTriangle, BatteryMedium, CheckCircle2, Copy, Info, Keyboard, Mic, Minus, NotebookPen, Presentation, RotateCcw,
  Settings2, Sparkles, Square, Usb, X,
} from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { inTauri, isMock, shell } from '../lib/bridge';
import { roster, TIDEPOOL_BATTERY_NOTE, type RosterItem } from '../lib/devices';
import { phaseLabel } from '../lib/format';
import { act, MODE_INFO, useStore } from '../lib/store';
import type { Mode } from '../lib/types';
import { Battery, spring, Tip } from './ui';

export type Page = 'voice' | 'notes' | 'devices' | 'battery' | 'settings';

export const PAGES: { id: Page; label: string; icon: typeof Mic }[] = [
  { id: 'voice', label: '语音', icon: Mic },
  { id: 'notes', label: '笔记', icon: NotebookPen },
  { id: 'devices', label: '设备', icon: Usb },
  { id: 'battery', label: '续航', icon: BatteryMedium },
  { id: 'settings', label: '设置', icon: Settings2 },
];

export const MODE_ICON: Record<Mode, typeof Mic> = { input: Keyboard, notes: NotebookPen, present: Presentation };

/** Everything the user can pick up right now (or, with includeAbsent, ever paired). */
export function useRoster(includeAbsent = false): RosterItem[] {
  const instances = useStore((s) => s.instances);
  const devices = useStore((s) => s.devices);
  const selected = useStore((s) => s.selected);
  const usage = useStore((s) => s.usage);
  const tidepool = useStore((s) => s.tidepool);
  const phase = useStore((s) => s.state.phase);
  return useMemo(() => roster({ instances, devices, selected, usage, tidepool, phase }, { includeAbsent }),
    [instances, devices, selected, usage, tidepool, phase, includeAbsent]);
}

export function ModeSwitch() {
  const mode = useStore((s) => s.mode);
  const pending = useStore((s) => s.pendingMode);
  return (
    <div className="mode-switch" role="group" aria-label="模式">
      {(['input', 'notes', 'present'] as Mode[]).map((value) => {
        const Icon = MODE_ICON[value];
        const active = mode === value;
        return (
          <button key={value} aria-pressed={active} className={`mode-${value}`} title={MODE_INFO[value].hint}
            onClick={() => !active && act('set_mode', { value, source: 'ui' })}>
            {active && <motion.span layoutId="mode-thumb" className="liquid absolute inset-0 rounded-full" transition={spring.snappy} />}
            <Icon size={15} className="relative" />
            <span className="relative">{MODE_INFO[value].label}</span>
            {pending === value && <span className="pending-dot relative" title="本段结束后切换" />}
          </button>
        );
      })}
    </div>
  );
}

export function StatusPill() {
  const phase = useStore((s) => s.state.phase);
  const whisper = useStore((s) => !!s.state.whisper);
  const online = ['idle', 'starting', 'recording', 'processing'].includes(phase);
  return (
    <span className="chip status-pill no-drag" role="status" aria-live="polite" data-phase={phase}>
      <i className={`dot ${phase === 'recording' ? 'live' : ''}`} data-on={online && phase !== 'recording'}
        data-tone={phase === 'error' ? 'bad' : undefined} />
      <AnimatePresence mode="popLayout" initial={false}>
        <motion.span key={phase} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: .18 }}>
          {phaseLabel[phase] ?? phase}
        </motion.span>
      </AnimatePresence>
      {whisper && phase === 'recording' && <span className="whisper-tag"><Sparkles size={11} />气声</span>}
    </span>
  );
}

export function TitleBar() {
  const [maximized, setMaximized] = useState(false);
  useEffect(() => {
    if (!inTauri) return;
    const win = getCurrentWindow();
    win.isMaximized().then(setMaximized);
    let stop: (() => void) | undefined;
    win.onResized(() => win.isMaximized().then(setMaximized)).then((u) => (stop = u));
    return () => stop?.();
  }, []);
  const win = inTauri ? getCurrentWindow() : null;
  return (
    <header className="titlebar" data-tauri-drag-region>
      <div className="flex min-w-0 items-center gap-2.5" data-tauri-drag-region>
        <img src="logo.svg" alt="" width={22} height={22} className="brand-mark" data-tauri-drag-region />
        <span className="brand-name" data-tauri-drag-region>点声</span>
        <span className="brand-sub" data-tauri-drag-region>DotVoice</span>
        {isMock && <span className="chip no-drag preview-chip">界面预览 · 模拟数据</span>}
      </div>
      <ModeSwitch />
      <div className="flex items-center justify-end gap-2" data-tauri-drag-region>
        <StatusPill />
        <div className="no-drag ml-1 flex items-center gap-0.5">
          <button className="win-btn" aria-label="最小化" title="最小化" onClick={() => win?.minimize()}><Minus size={16} /></button>
          <button className="win-btn" aria-label={maximized ? '还原' : '最大化'} title={maximized ? '还原' : '最大化'} onClick={() => win?.toggleMaximize()}>
            {maximized ? <Copy size={13} /> : <Square size={13} />}
          </button>
          <button className="win-btn close" aria-label="关闭到托盘" title="关闭后留在托盘" onClick={() => shell.hideMain()}><X size={17} /></button>
        </div>
      </div>
    </header>
  );
}

export function Sidebar({ page, onPage }: { page: Page; onPage: (page: Page) => void }) {
  return (
    <nav className="sidebar" aria-label="主导航">
      {PAGES.map(({ id, label, icon: Icon }, index) => (
        <button key={id} className="nav-item" aria-current={page === id ? 'page' : undefined} onClick={() => onPage(id)}
          title={`${label}（Ctrl+${index + 1}）`}>
          {page === id && <motion.span layoutId="nav-thumb" className="nav-thumb" transition={spring.snappy} />}
          <Icon size={17} className="relative" />
          <span className="relative">{label}</span>
          <kbd className="nav-kbd relative">Ctrl {index + 1}</kbd>
        </button>
      ))}
      <div className="mt-auto">
        <DeviceDock onOpen={() => onPage('devices')} />
      </div>
    </nav>
  );
}

/* Every pen that is plugged in (each with its own battery) and Tidepool when it
   is online. Rows appear only for detected devices. */
function DeviceDock({ onOpen }: { onOpen: () => void }) {
  const items = useRoster();
  const phase = useStore((s) => s.state.phase);
  const mode = useStore((s) => s.mode);
  const engine = useStore((s) => s.engine);
  const online = ['idle', 'starting', 'recording', 'processing'].includes(phase);
  const openless = engine.kind === 'openless';
  const link = online ? (openless ? '已连接' : '就绪') : phase === 'error' ? '异常' : openless ? '未连接' : '已暂停';
  return (
    <div className={`dock glass mode-${mode}`}>
      <div className="dock-head">
        <span>设备</span>
        <span className="num">{items.length ? `${items.length} 台在线` : ''}</span>
      </div>
      <div className="flex flex-col">
        <AnimatePresence initial={false}>
          {items.map((item) => (
            <motion.button key={item.key} layout="position" className="dock-row" data-dock-row={item.key} onClick={onOpen}
              aria-label={`${item.name}，${item.status}，电量${item.battery == null ? (item.kind === 'tidepool' ? `未上报：${TIDEPOOL_BATTERY_NOTE}` : '未知') : item.battery + '%'}`}
              initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: 'auto' }} exit={{ opacity: 0, height: 0 }} transition={spring.soft}>
              <span className="dock-row-in">
                <i className={`dot ${item.state === 'recording' ? 'live' : ''}`} data-on={item.state === 'ready' || item.state === 'connected'} />
                <span className="min-w-0 flex-1 truncate text-left">{item.name}</span>
                {item.kind === 'tidepool' && item.battery == null
                  ? <Tip text={TIDEPOOL_BATTERY_NOTE}><span className="dock-na">未上报</span></Tip>
                  : <Battery percent={item.battery} />}
              </span>
            </motion.button>
          ))}
        </AnimatePresence>
        {items.length === 0 && <button className="dock-empty" onClick={onOpen}>插入接收器后显示</button>}
      </div>
      <div className="dock-foot">
        <span className="flex min-w-0 items-center gap-1.5" data-engine-status title={`语音引擎：${engine.label}`}>
          <i className="dot" data-on={online} data-tone={phase === 'error' ? 'bad' : undefined} />
          <span className="truncate">{engine.label} {link}</span>
        </span>
        <span className="mode-text font-semibold">{MODE_INFO[mode].label}</span>
      </div>
    </div>
  );
}

const TONE_ICON = { success: CheckCircle2, error: AlertTriangle, info: Info } as const;

export function Toasts() {
  const notices = useStore((s) => s.notices);
  const dismiss = useStore((s) => s.dismiss);
  const reduced = useStore((s) => !!s.prefs?.appearance.reduced_motion);
  return (
    <div className="toasts" aria-live="polite">
      <AnimatePresence>
        {notices.map((n) => {
          const Icon = TONE_ICON[n.tone];
          return (
            <motion.div key={n.id} layout initial={{ opacity: 0, y: 18, scale: .94 }} animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 6, scale: .96, transition: { duration: .16 } }} transition={spring.soft}
              className="toast" data-tone={n.tone} role="status">
              <Icon size={16} className="toast-ico" />
              <span className="min-w-0 flex-1">{n.text}</span>
              {n.action && (
                <button className="btn btn-sm toast-action" onClick={() => { n.action!.run(); dismiss(n.id); }}>{n.action.icon === 'copy' ? <Copy size={13} /> : <RotateCcw size={13} />}{n.action.label}</button>
              )}
              <button className="btn btn-ghost btn-sm btn-icon" aria-label="关闭提示" onClick={() => dismiss(n.id)}><X size={14} /></button>
              {!reduced && <motion.i className="toast-timer" initial={{ scaleX: 1 }} animate={{ scaleX: 0 }} transition={{ duration: n.ttl / 1000, ease: 'linear' }} />}
            </motion.div>
          );
        })}
      </AnimatePresence>
    </div>
  );
}

/* OpenLess shipped (or declined) live text for remote input: say so until dismissed. */
export function ReminderBanner() {
  const reminder = useStore((s) => s.reminder);
  async function dismiss() {
    await act('dismiss_reminder');
    useStore.setState({ reminder: null });
  }
  return (
    <AnimatePresence initial={false}>
      {reminder && (
        <motion.div key="reminder" className="banner" role="status" initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} transition={spring.soft}>
          <span className="banner-ico" data-tone="ok"><Sparkles size={15} /></span>
          <span className="flex-1">{reminder.kind === 'released'
            ? `OpenLess ${reminder.version ?? ''} 已包含实时文字支持：升级 OpenLess 后，点声录音时会显示与最终结果同一引擎的实时文字。`
            : 'OpenLess 关闭了实时文字的合并请求；点声录音时继续只显示声线，需要时可在设置里打开本机预览文字。'}</span>
          <button className="btn btn-sm" onClick={() => act('open_url', { url: reminder.url })}>查看</button>
          <button className="btn btn-ghost btn-sm" onClick={dismiss}>知道了</button>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export function CoreBanner() {
  const down = useStore((s) => s.coreDown);
  return (
    <AnimatePresence initial={false}>
      {down && (
        <motion.div key="core" className="banner" role="alert" initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }} transition={spring.soft}>
          <span className="banner-ico" data-tone="bad"><AlertTriangle size={15} /></span>
          <span className="flex-1">{down}</span>
          <button className="btn btn-sm" onClick={() => shell.restartCore()}><RotateCcw size={13} />重新启动核心</button>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
