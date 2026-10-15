export type Phase = 'disconnected' | 'idle' | 'starting' | 'recording' | 'processing' | 'error';
export type Mode = 'input' | 'notes' | 'present';
export type Tool = 'mouse' | 'laser' | 'spotlight' | 'magnifier';
export type Category = 'idea' | 'paper';
export type DeviceId = 'baseus' | 'pv60';
/** Who wrote live text: the local preview model (often wrong; shown on request),
    OpenLess's own stream, or a built-in cloud engine. See lib/liveText. */
export type LiveSource = 'local' | 'remote' | 'cloud';

export interface CoreState {
  phase: Phase;
  message: string;
  armed?: boolean;
  destination?: 'input' | 'notes';
  /** The current session applies whisper (气声) boosting. */
  whisper?: boolean;
}

/** One physical pen (one 2.4G receiver). Several of the same model can be plugged in. */
export interface PenInstance {
  id: string; // stable, e.g. "baseus:3f9a12c0"
  model: DeviceId;
  label: string; // "倍思 #1" by default, renamable
  present: boolean;
  battery: { percent: number; reported_at: number } | null;
  last_used: number | null; // epoch seconds
  active: boolean; // this pen is recording now
}

export type WhisperMode = 'auto' | 'on' | 'off';

export interface Device {
  id: DeviceId;
  name: string;
  short: string;
  present: boolean;
  validation: string;
  modes: string[];
  /** The receiver's microphone name as Windows lists it (e.g. BaseusPresent). */
  microphone?: string;
}

/* ── Speech engines (bridge.engines) ── */
export type EngineKind = 'openless' | 'volc' | 'dashscope' | 'hotkey';
export type PolishStyle = 'raw' | 'light' | 'custom';
export type HotkeyStyle = 'toggle' | 'hold';

/** prefs.engine: plain settings only; API keys live in the core's encrypted store. */
export interface EnginePrefs {
  kind: EngineKind;
  /** hotwords: the OpenLess dictionary as Doubao hotwords (absent in a core without the
      setting: counts as on). */
  volc: { auth: 'key' | 'legacy'; app_id: string; resource_id: string; endpoint: 'bigmodel_async' | 'bigmodel' | 'bigmodel_nostream'; hotwords?: boolean };
  dashscope: { model: string; region: 'cn' | 'intl' };
  polish: { style: PolishStyle; base_url: string; model: string; prompt: string };
  /** keys: '' = the app's own shortcut; style is used by the custom profile only. */
  hotkey: { profile: string; keys: string; style: HotkeyStyle };
}

/** snapshot.engine / 'engine' events: which engine runs and what to call it. */
export interface EngineInfo {
  kind: EngineKind;
  label: string;
  external: boolean;
  app?: string;
  profile?: string;
  combo?: string;
  style?: HotkeyStyle;
  /** The saved settings cannot be used yet (e.g. a custom shortcut not recorded). */
  error?: string;
  /** Doubao's key is saved but another engine runs (presence only, never the key). */
  suggest_volc?: boolean;
}

export type SecretName = 'volc.api_key' | 'volc.access_token' | 'dashscope.api_key' | 'llm.api_key';
/** secret_status(): whether each key is saved, masked ("••••abcd"). Never the value. */
export interface SecretStatus {
  secrets: Record<SecretName, { set: boolean; masked: string }>;
}

export interface HotkeyApp {
  profile: string;
  name: string;
  keys: string[];
  combo: string;
  style: HotkeyStyle;
  mic_hint: string;
  notes: string;
  custom: boolean;
  verified: boolean;
  installed: boolean;
  running: boolean;
  version: string | null;
  supported: boolean | null;
  detected: boolean;
  builtin: boolean;
  cancel: string;
}

export interface HotkeyDetect {
  apps: HotkeyApp[];
  mic: string;
  custom_mic_hint: string;
}

export interface OpenLessStatus {
  installed: boolean;
  running: boolean;
  remote_input: boolean | null;
  paired: boolean;
  port: number | null;
  active: boolean;
  connected: boolean;
}

export interface EngineTest {
  kind: string;
  ok: boolean;
  message: string;
  seconds: number;
}

/** A setup-wizard test recording: shown in the wizard only (nothing typed or saved). */
export interface WizardResult {
  text: string;
  test?: boolean;
  seconds?: number | null;
  external?: boolean;
  app?: string;
  /** Volume history of the recording (for the result's voice line). */
  envelope?: number[];
  /** Nobody spoke: the recording was cancelled. */
  empty?: boolean;
  /** The engine could not finish this test recording (network, key): why. */
  error?: string;
  at: number;
}

export interface Prefs {
  mode: Mode;
  category: Category;
  sounds: { enabled: boolean; volume: 'low' | 'medium' | 'high'; keep_awake?: boolean };
  overlay: { enabled: boolean; on_switch: boolean; linger_seconds: number; position: { x: number; y: number } | null };
  /** show_local_text: show the local preview model's words while speaking (may be wrong). */
  preview: { show_local_text: boolean };
  airmouse: { enabled: boolean; skip_slideshow: boolean };
  hotkeys: { toggle_mode: string; arm_toggle: string };
  appearance: { theme: 'system' | 'light' | 'dark' | 'glass'; reduced_motion: boolean; live_shortcut?: boolean };
  present: { tool: Tool; hide_cursor: boolean; zoom?: number };
  /** Optional so an older core without these keys still renders (defaults shown). */
  voice?: { whisper: WhisperMode; fast_raw?: boolean };
  devices?: { coexist: boolean };
  pen?: { guard_shortcuts: boolean };
  /** save_audio: keep the last 3 recordings (raw + as sent) locally for tuning 气声; 24 h. */
  diagnostics?: { save_audio: boolean };
  /** Absent in an older core (OpenLess only). */
  engine?: EnginePrefs;
  /** done: the first-run wizard finished or was skipped. */
  setup?: { done: boolean };
  /** run_elevated (兼容管理员权限窗口): mirrors the shell's logon task; changed only through
      shell.elevationSet (UAC + restart). Absent in an older core. */
  general?: { run_elevated: boolean };
}

/** The shell's elevation_status: DotVoice as administrator (兼容管理员权限窗口). */
export interface ElevationStatus {
  elevated: boolean;
  /** An administrator account that can elevate (and not the UI self-check). */
  available: boolean;
  /** The logon task "DotVoice (管理员)": absent, for this DotVoice.exe, or for another copy. */
  task: 'missing' | 'ours' | 'other';
  task_path?: string | null;
  smoke?: boolean;
}

/** 'elevated' events: an administrator window dropped a click / scroll / hotkey (first per window). */
export interface ElevatedNotice {
  kind: string;
  process?: string;
  first?: boolean;
  message: string;
}

export interface Binding {
  source: 'vendor' | 'pv60' | 'keyboard';
  fire?: string[];
  ignore?: string[];
  vk?: number[];
  gesture?: 'single' | 'double';
  label: string;
  default?: boolean;
}

export interface Learning {
  device: string;
  slot: string;
  /** tap: two short presses (then hold); hold: press + release; long: one long press only. */
  step: 'tap' | 'hold' | 'long';
  captured: string[];
}

export interface Bindings {
  devices: Record<string, Record<string, Binding>>;
  learning: Learning | null;
  warning: string;
}

export interface LearnProgress extends Learning {
  status: 'waiting' | 'step' | 'done' | 'error' | 'cancelled' | 'timeout';
  message: string;
}

export interface Note {
  id: string;
  created: number;
  tags: string[];
  title: string;
  source: string;
  text: string;
  duration?: number;
  updated?: number;
}

export interface Cycle {
  started: number;
  ended: number;
  complete: boolean;
  sessions: number;
  endpoint?: string;
}

export interface Usage {
  active: (Cycle & { observed: number; armed: number; recording: number; audio_seconds?: number; speech_seconds?: number }) | null;
  cycles: Cycle[];
  totals: { observed: number; armed: number; recording: number; sessions: number; audio_seconds?: number; speech_seconds?: number };
  battery: { percent: number; raw?: number; reported_at: number; source: string } | null;
  battery_samples: { percent: number; at: number }[];
  recent_sessions: { audio_seconds: number; speech_seconds: number; ended: number; outcome: string }[];
  reduced_motion: boolean;
  warning: string;
  now: number;
}

export interface Tidepool {
  installed?: boolean;
  receiver_running?: boolean | null;
  receiver_version?: string | null;
  control_supported?: boolean;
  device_online?: boolean | null;
  usb_connected?: boolean;
  link?: string;
  recording?: boolean | null;
  enabled?: boolean | null;
  battery_percent?: number | null;
  battery_charging?: boolean | null;
  battery_reported_at?: number | null;
  firmware_version?: string | null;
  message?: string;
}

export interface Result {
  text: string;
  seconds: number | null;
  envelope: number[];
  destination: Mode;
  category?: Category;
  note_id?: string;
  note_error?: string;
  note_skipped?: boolean;
  /** An input result with no words: nothing was typed. */
  empty_text?: boolean;
  /** The hotkey bridge: another app typed the words (text is empty). */
  external?: boolean;
  app?: string;
  /** An administrator window dropped the typed text: it is kept (and, copied, on the clipboard). */
  not_typed?: 'elevated';
  copied?: boolean;
  not_typed_message?: string;
}

export interface ModeEvent {
  value: Mode;
  pending: Mode | null;
  source: 'ui' | 'key' | 'hotkey' | 'tray' | 'pending';
  armed: boolean;
  tool: Tool;
}

export interface LivePartial {
  session: string;
  text: string;
  final: boolean;
  source?: LiveSource;
}

export interface Reminder {
  kind: 'released' | 'closed';
  version?: string;
  url: string;
}

export interface Snapshot {
  version: string;
  state: CoreState;
  result: Result | null;
  usage: Usage;
  devices: Device[];
  /** Every pen seen (present or not); absent in an older core. */
  instances?: PenInstance[];
  selected: DeviceId;
  mode: 'hold' | 'toggle';
  tidepool: Tidepool;
  destination: Mode;
  pending_mode: Mode | null;
  prefs: Prefs;
  tool: Tool;
  preview_available: boolean;
  preview_refiner?: 'sense_voice' | 'paraformer' | null;
  reminder?: Reminder | null;
  app_update?: AppUpdate | null;
  bindings: Bindings;
  live_text: string;
  live_source?: LiveSource | null;
  armed: boolean;
  airmouse_active: boolean;
  /** Absent in an older core (always OpenLess). */
  engine?: EngineInfo;
  /** Whether the core runs as administrator and could (absent in an older core). */
  elevation?: { elevated: boolean; can_elevate: boolean };
  /** prefs.json could not be read: defaults (or the saved copy) are in use. '' = fine. */
  prefs_warning?: string;
  smoke: boolean;
}

export interface AppUpdate {
  status: 'idle' | 'checking' | 'current' | 'available' | 'error';
  current_version: string;
  latest_version: string | null;
  download_url: string | null;
  release_url: string | null;
  last_checked: number | null;
  error: string | null;
  available: boolean;
  dismissed: boolean;
  dismissed_version: string | null;
}

export interface ProbeEvent {
  interface: string;
  code?: string;
  vk?: number;
  down?: boolean;
  buttons?: number;
  hex?: string;
  at: number;
}
