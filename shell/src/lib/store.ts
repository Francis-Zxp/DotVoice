import { create } from 'zustand';
import { call, onCore, shell } from './bridge';
import { keptToast } from './elevation';
import { asSource, nextLive } from './liveText';
import type {
  AppUpdate, Bindings, CoreState, Device, DeviceId, EngineInfo, LearnProgress, LiveSource, Mode, ModeEvent, Note, PenInstance, Prefs,
  ProbeEvent, Reminder, Result, SecretStatus, Snapshot, Tidepool, Tool, Usage, WizardResult,
} from './types';

/* Volume levels arrive at 10 Hz; they bypass React state on purpose. */
type LevelListener = (level: number) => void;
const levelListeners = new Set<LevelListener>();
export const levels = {
  subscribe(listener: LevelListener) {
    levelListeners.add(listener);
    return () => {
      levelListeners.delete(listener);
    };
  },
};
/* Windows that don't use the store (capsule) forward level events here. */
export function pushLevel(level: number) {
  levelListeners.forEach((l) => l(level));
}

export interface Notice {
  id: number;
  text: string;
  tone: 'info' | 'error' | 'success';
  /** icon: 'undo' (default) or 'copy'. */
  action?: { label: string; run: () => void; icon?: 'undo' | 'copy' };
  ttl: number;
}

interface Store {
  ready: boolean;
  version: string;
  coreDown: string | null;
  state: CoreState;
  /** When state.phase last changed (ms): the voice page's timers survive page switches. */
  phaseAt: number;
  mode: Mode;
  pendingMode: Mode | null;
  tool: Tool;
  prefs: Prefs | null;
  bindings: Bindings | null;
  devices: Device[];
  instances: PenInstance[];
  selected: DeviceId;
  deviceMode: 'hold' | 'toggle';
  usage: Usage | null;
  tidepool: Tidepool;
  result: Result | null;
  liveText: string;
  /** Who wrote liveText; pages show it through lib/liveText. */
  liveSource: LiveSource | null;
  liveFinal: boolean;
  reminder: Reminder | null;
  appUpdate: AppUpdate | null;
  notes: Note[];
  tags: string[];
  notesWarning: string;
  /** prefs.json could not be read (settings shows it until a launch reads it again). */
  prefsWarning: string;
  learn: LearnProgress | null;
  probe: ProbeEvent[];
  airmouseActive: boolean;
  smoke: boolean;
  notices: Notice[];
  lastModeEvent: ModeEvent | null;
  /** The speech engine in use (an older core: always OpenLess). */
  engine: EngineInfo;
  /** Saved keys, masked; loaded on demand (settings, wizard). Never the values. */
  secrets: SecretStatus['secrets'] | null;
  /** The pen's record key (⑥) was pressed: the wizard's key test. */
  lastKey: { action: string; at: number } | null;
  /** The setup wizard's latest test recording. */
  wizardResult: WizardResult | null;
  /** The wizard's microphone check: could the meter open the microphone? */
  meter: { ok: boolean; message: string } | null;
  wizardOpen: boolean;
  openWizard: () => void;
  closeWizard: () => void;
  refreshSecrets: () => Promise<void>;
  notify: (text: string, tone?: Notice['tone'], action?: Notice['action']) => void;
  dismiss: (id: number) => void;
  refreshNotes: () => Promise<void>;
  renameInstance: (id: string, label: string) => Promise<boolean>;
  apply: (snapshot: Snapshot) => void;
}

export const OPENLESS_ENGINE: EngineInfo = { kind: 'openless', label: 'OpenLess', external: false };

let noticeId = 0;

/** A repeated phase (whisper boosting began mid-recording) keeps its start time. */
function phaseAt(s: Pick<Store, 'state' | 'phaseAt'>, next: CoreState) {
  return next.phase === s.state.phase ? s.phaseAt : Date.now();
}

export const useStore = create<Store>((set, get) => ({
  ready: false,
  version: '',
  coreDown: null,
  state: { phase: 'disconnected', message: '' },
  phaseAt: Date.now(),
  mode: 'input',
  pendingMode: null,
  tool: 'mouse',
  prefs: null,
  bindings: null,
  devices: [],
  instances: [],
  selected: 'baseus',
  deviceMode: 'hold',
  usage: null,
  tidepool: {},
  result: null,
  liveText: '',
  liveSource: null,
  liveFinal: false,
  reminder: null,
  appUpdate: null,
  notes: [],
  tags: [],
  notesWarning: '',
  prefsWarning: '',
  learn: null,
  probe: [],
  airmouseActive: false,
  smoke: false,
  notices: [],
  lastModeEvent: null,
  engine: OPENLESS_ENGINE,
  secrets: null,
  lastKey: null,
  wizardResult: null,
  meter: null,
  wizardOpen: false,
  openWizard() {
    set({ wizardOpen: true, wizardResult: null, meter: null });
  },
  closeWizard() {
    set({ wizardOpen: false });
  },
  async refreshSecrets() {
    try {
      const data = await call<SecretStatus>('secret_status');
      if (data?.secrets) set({ secrets: data.secrets });
    } catch { /* an older core: no keys to show */ }
  },
  notify(text, tone = 'info', action) {
    const id = ++noticeId;
    // Long sentences (an administrator window refused the input) stay a little longer.
    const ttl = Math.max(action ? 6500 : 4800, Math.min(9000, 2400 + text.length * 60));
    set((s) => ({ notices: [...s.notices.slice(-3), { id, text, tone, action, ttl }] }));
    window.setTimeout(() => get().dismiss(id), ttl);
  },
  dismiss(id) {
    set((s) => ({ notices: s.notices.filter((n) => n.id !== id) }));
  },
  async refreshNotes() {
    try {
      const data = await call<{ items: Note[]; tags: string[]; warning: string }>('notes');
      set({ notes: data.items, tags: data.tags ?? [], notesWarning: data.warning });
    } catch (error) {
      get().notify((error as Error)?.message || '笔记列表未能刷新，请重试。', 'error');
    }
  },
  async renameInstance(id, label) {
    try {
      const data = await call<{ instances: PenInstance[] }>('rename_instance', { id, label });
      if (data?.instances) set({ instances: data.instances });
      return true;
    } catch (error) {
      get().notify((error as Error).message || '重命名没有成功，请重试。', 'error');
      return false;
    }
  },
  apply(s) {
    set({
      ready: true,
      coreDown: null,
      version: s.version,
      state: s.state,
      phaseAt: phaseAt(get(), s.state),
      mode: s.destination,
      pendingMode: s.pending_mode,
      tool: s.tool ?? 'mouse',
      prefs: s.prefs,
      bindings: s.bindings,
      devices: s.devices,
      instances: s.instances ?? [],
      selected: s.selected,
      deviceMode: s.mode,
      usage: s.usage,
      tidepool: s.tidepool,
      result: s.result,
      liveText: s.live_text ?? '',
      liveSource: s.live_text ? asSource(s.live_source) : null,
      reminder: s.reminder ?? null,
      appUpdate: s.app_update ?? null,
      airmouseActive: s.airmouse_active,
      engine: s.engine ?? OPENLESS_ENGINE,
      prefsWarning: s.prefs_warning ?? '',
      smoke: s.smoke,
    });
  },
}));

/* Events that arrived while connectCore's snapshot was in flight. */
let inFlight: Set<string> | null = null;

function handle(event: string, data: any) {
  inFlight?.add(event);
  const store = useStore.getState();
  switch (event) {
    case 'ready':
      store.apply(data);
      break;
    case 'state':
      useStore.setState((s) => ({
        state: data,
        phaseAt: phaseAt(s, data),
        // A new recording starts with an empty live transcript.
        liveText: data.phase === 'starting' ? '' : s.liveText,
        liveSource: data.phase === 'starting' ? null : s.liveSource,
        liveFinal: data.phase === 'starting' ? false : s.liveFinal,
        result: data.phase === 'starting' ? null : s.result,
      }));
      break;
    case 'level':
      levelListeners.forEach((l) => l(data));
      break;
    case 'partial':
      useStore.setState((s) => {
        const current = { text: s.liveText, source: s.liveSource };
        const live = nextLive(current, String(data.text ?? ''), data.source);
        return live === current ? {} : { liveText: live.text, liveSource: live.source, liveFinal: !!data.final };
      });
      break;
    case 'mode':
      useStore.setState({ mode: data.value, pendingMode: data.pending, lastModeEvent: data, tool: data.tool ?? store.tool });
      break;
    case 'tool':
      useStore.setState({ tool: data.tool });
      break;
    case 'result':
      useStore.setState({ result: data });
      if (data?.not_typed) {
        // An administrator window dropped the typed text: it is kept (clipboard, this result).
        const text = String(data.text ?? '');
        // The toast closes on click: a failed copy must say so, or the old clipboard gets pasted.
        store.notify(keptToast(data), 'error', text ? { label: '复制', icon: 'copy', run: () => {
          call('copy_text', { text }).catch((error) => useStore.getState().notify((error as Error)?.message || '没能复制，请到语音页复制本次文字', 'error'));
        } } : undefined);
      }
      break;
    case 'elevated':
      // An administrator window dropped a click / scroll / hotkey: said once per window.
      if (data?.message) store.notify(String(data.message), 'error');
      break;
    case 'note_saved':
      store.refreshNotes();
      break;
    case 'reminder':
      useStore.setState({ reminder: data });
      break;
    case 'app_update':
      useStore.setState({ appUpdate: data });
      break;
    case 'empty':
      // A wizard test that heard nothing is shown in the wizard, not as a toast.
      if (data?.test) useStore.setState({ wizardResult: { text: '', test: true, empty: true, at: Date.now() } });
      else store.notify(data?.message || '没听到说话，本段已取消，没有发给 OpenLess', 'info');
      break;
    case 'engine':
      useStore.setState({ engine: data ?? OPENLESS_ENGINE });
      break;
    case 'secrets':
      if (data?.secrets) useStore.setState({ secrets: data.secrets });
      break;
    case 'key':
      useStore.setState({ lastKey: { action: String(data), at: Date.now() } });
      break;
    case 'wizard_result':
      useStore.setState({ wizardResult: { ...data, text: String(data?.text ?? ''), at: Date.now() } });
      break;
    case 'meter':
      useStore.setState({ meter: data });
      break;
    case 'notice':
      store.notify(String(data), 'info');
      break;
    case 'usage':
      useStore.setState({ usage: data });
      break;
    case 'devices':
      useStore.setState({ devices: data.devices, selected: data.selected ?? store.selected });
      break;
    case 'instances':
      useStore.setState({ instances: Array.isArray(data) ? data : data?.instances ?? [] });
      break;
    case 'tidepool':
      useStore.setState({ tidepool: data });
      break;
    case 'prefs':
      useStore.setState({ prefs: data });
      break;
    case 'learn':
      useStore.setState({ learn: data });
      if (['done', 'error', 'timeout', 'cancelled'].includes(data.status)) {
        call<Bindings>('bindings').then((bindings) => useStore.setState({ bindings })).catch(() => {});
      }
      break;
    case 'probe':
      useStore.setState((s) => ({ probe: [{ ...data, at: Date.now() }, ...s.probe].slice(0, 40) }));
      break;
    case 'battery_alert':
      // With several receivers the core names the pen.
      store.notify(`${data.label ? `${data.label} ` : '倍思'}电量 ${data.percent}%，建议充电`, 'error');
      break;
    case 'core_exit': {
      // Like the tray and the capsule: the last phase is no longer true. The next ready restores it.
      const text = data?.restarting ? '点声核心意外退出，正在自动重启…' : '点声核心已退出';
      useStore.setState((s) => ({ coreDown: text, state: { phase: 'error', message: text }, phaseAt: phaseAt(s, { phase: 'error', message: text }), liveFinal: false }));
      break;
    }
    case 'fatal':
      useStore.setState((s) => ({ coreDown: String(data), state: { phase: 'error', message: String(data) }, phaseAt: phaseAt(s, { phase: 'error', message: '' }) }));
      break;
  }
}

let started = false;
export async function connectCore() {
  if (started) return;
  started = true;
  onCore(handle);
  const seen = (inFlight = new Set<string>());
  let snapshot: Snapshot | null = null;
  try {
    snapshot = await call<Snapshot>('snapshot');
  } catch {
    snapshot = (await shell.ready()) ?? null;
  } finally {
    inFlight = null;
  }
  if (snapshot) {
    const now = useStore.getState();
    now.apply(snapshot);
    // Events can overtake the snapshot reply: keep what they wrote (as the capsule does).
    useStore.setState({
      ...(seen.has('state') ? { state: now.state, phaseAt: now.phaseAt, result: now.result, liveText: now.liveText, liveSource: now.liveSource } : {}),
      ...(seen.has('result') ? { result: now.result } : {}),
      ...(seen.has('partial') ? { liveText: now.liveText, liveSource: now.liveSource } : {}),
      ...(seen.has('mode') ? { mode: now.mode, pendingMode: now.pendingMode, tool: now.tool } : {}),
      ...(seen.has('tool') ? { tool: now.tool } : {}),
      ...(seen.has('prefs') ? { prefs: now.prefs } : {}),
      ...(seen.has('devices') ? { devices: now.devices, selected: now.selected } : {}),
      ...(seen.has('instances') ? { instances: now.instances } : {}),
      ...(seen.has('tidepool') ? { tidepool: now.tidepool } : {}),
      ...(seen.has('usage') ? { usage: now.usage } : {}),
      ...(seen.has('reminder') ? { reminder: now.reminder } : {}),
      ...(seen.has('app_update') ? { appUpdate: now.appUpdate } : {}),
      ...(seen.has('engine') ? { engine: now.engine } : {}),
      ...(seen.has('core_exit') || seen.has('fatal') ? { coreDown: now.coreDown, state: now.state, phaseAt: now.phaseAt } : {}),
    });
  }
  useStore.getState().refreshNotes();
}

export async function act<T = any>(method: string, params?: Record<string, unknown>): Promise<T | null> {
  try {
    return await call<T>(method, params);
  } catch (error) {
    useStore.getState().notify((error as Error).message || '操作未完成，请重试。', 'error');
    return null;
  }
}

/** Hides the reminder only once the core has dropped it, and only the one that was shown. */
export async function dismissReminder() {
  const shown = useStore.getState().reminder;
  if (await act('dismiss_reminder')) useStore.setState((s) => (s.reminder === shown ? { reminder: null } : {}));
}

export const MODE_INFO: Record<Mode, { label: string; long: string; hint: string }> = {
  input: { label: '输入', long: '输入到光标', hint: '说完插入当前编辑框' },
  notes: { label: '笔记', long: '留作笔记', hint: '说完按键，整段保存为笔记' },
  present: { label: '演示', long: '演示模式', hint: '任何窗口可用 · 双击 ⑤ 换工具' },
};

export const TOOL_INFO: Record<Tool, string> = {
  mouse: '飞鼠', laser: '数码激光', spotlight: '聚光灯', magnifier: '放大镜',
};
