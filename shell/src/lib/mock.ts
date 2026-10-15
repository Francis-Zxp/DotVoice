/* Browser-only preview data. Not used inside the app; the UI labels it as a preview. */
import { keyText } from './engine';
import { ELEVATION_NOTICE } from './elevation';
import type { ElevationStatus, EngineInfo, EnginePrefs, HotkeyApp, LiveSource, Mode, Note, PenInstance, SecretName, Snapshot, Tool } from './types';

type Handler = (event: string, data: any) => void;

const now = () => Date.now() / 1000;
const MODES: Mode[] = ['input', 'notes', 'present'];
const TOOLS: Tool[] = ['mouse', 'laser', 'spotlight', 'magnifier'];

function sampleNotes(): Note[] {
  const t = now();
  return [
    { id: 'a'.repeat(32), created: t - 300, tags: ['灵感'], title: '长按 ⑦ 在三种模式之间循环。',
      source: '长按 ⑦ 在三种模式之间循环。说话时便签实时出字，说完再按一次保存。',
      text: '长按 ⑦ 在三种模式之间循环。\n说话时便签实时出字，说完再按一次保存。', duration: 18.4 },
    { id: 'b'.repeat(32), created: t - 3600 * 5, tags: ['论文', '实验设计'], title: '让可见性与语音可懂度分开测量。',
      source: '让可见性与语音可懂度分开测量。可能存在交互效应，样本 n=120。',
      text: '让可见性与语音可懂度分开测量。\n可能存在交互效应，样本 n=120，不直接认定为创新。', duration: 41.2 },
    { id: 'c'.repeat(32), created: t - 86400 * 1.2, tags: [], title: '单击飞鼠键就是左键，翻页键始终翻页。',
      source: '单击飞鼠键就是左键，翻页键始终翻页。', text: '单击飞鼠键就是左键，翻页键始终翻页。', duration: 7.9 },
    { id: 'd'.repeat(32), created: t - 86400 * 3.4, tags: ['待办'], title: '周五前整理对照组数据。',
      source: '周五前整理对照组数据，记录完成时间与错误率。', text: '周五前整理对照组数据，记录完成时间与错误率。', duration: 12 },
  ];
}

const tagsOf = (notes: Note[]) => [...new Set(notes.flatMap((n) => n.tags))].sort();

/* The hotkey bridge's apps as the core lists them (bridge.engines.hotkey PROFILES), with
   闪电说 running, 微信 3.9 (too old) and Windows voice typing built in. */
const MIC = '麦克风 (BaseusPresent)';
function sampleApps(): HotkeyApp[] {
  const base = { custom: false, verified: false, version: null, supported: null, builtin: false, cancel: '' };
  const system = `打开 Windows 设置 → 系统 → 声音 → 输入，把「${MIC}」（翻页笔接收器）设为默认录音设备（这会改变全系统的默认麦克风）。`;
  return [
    { ...base, profile: 'shandianshuo', name: '闪电说', keys: ['LCONTROL', 'LWIN'], combo: 'Ctrl + Win', style: 'toggle', installed: true, running: true, detected: true, version: '0.7.8', cancel: 'Esc',
      mic_hint: `打开闪电说「设置」，在「麦克风」里固定选择「${MIC}」（翻页笔接收器），不要用系统默认设备。`,
      notes: '短按快捷键开始、再短按结束；长按会变成「帮我说」，所以点声只发短按。Esc 取消录音（0.7.5 起）。' },
    { ...base, profile: 'windows', name: 'Windows 语音输入', keys: ['LWIN', 'H'], combo: 'Win + H', style: 'toggle', installed: true, running: false, detected: true, builtin: true, supported: true,
      mic_hint: `Windows 语音输入只用系统默认麦克风：按 Win + H 后点齿轮里的「默认麦克风」选择，或${system}`,
      notes: 'Win + H 打开语音输入并开始聆听（边说边出字），再按一次 Win + H 停止。出现聆听提示后再开口。' },
    { ...base, profile: 'typeless', name: 'Typeless', keys: ['RMENU'], combo: '右 Alt', style: 'toggle', installed: false, running: false, detected: false,
      mic_hint: `打开 Typeless 设置 → Audio → Microphone，选择「${MIC}」（翻页笔接收器），不要用 Auto-detect。`, notes: '按一次右 Alt 开始听写，再按一次结束并输入文字。' },
    { ...base, profile: 'doubao_ime', name: '豆包输入法', keys: ['RMENU'], combo: '右 Alt', style: 'hold', installed: false, running: false, detected: false,
      mic_hint: `在豆包输入法的语音输入设置里，把麦克风选为「${MIC}」（翻页笔接收器），并保持「长按模式」（按住说话、松手结束）。`,
      notes: '豆包输入法是输入法：只有它是当前输入法时右 Alt 才会唤起语音。按住式没有取消：松开即提交。' },
    { ...base, profile: 'wechat', name: '微信语音输入', keys: ['LCONTROL', 'LWIN'], combo: 'Ctrl + Win', style: 'hold', installed: true, running: true, detected: false, version: '3.9.12', supported: false,
      mic_hint: '微信语音输入用 Windows 默认麦克风（未见单独的麦克风选项）：' + system, notes: '需要微信 4.1.8 或更高版本。按住 Ctrl + Win 说话、松开结束。' },
    { ...base, profile: 'wechat_handsfree', name: '微信语音输入（免手持）', keys: ['LCONTROL', 'LSHIFT', 'LWIN'], combo: 'Ctrl + Shift + Win', style: 'toggle', installed: true, running: true, detected: false, version: '3.9.12', supported: false,
      mic_hint: '微信语音输入用 Windows 默认麦克风（未见单独的麦克风选项）：' + system, notes: '官方只说「不想一直按住可用 Ctrl + Win + Shift」，未经实测。' },
  ];
}

const NAMES: Record<string, string> = {
  ctrl: 'Ctrl', control: 'Ctrl', controlleft: 'Ctrl', controlright: '右 Ctrl', alt: 'Alt', altleft: 'Alt', altright: '右 Alt', '右alt': '右 Alt',
  shift: 'Shift', shiftleft: 'Shift', shiftright: '右 Shift', win: 'Win', meta: 'Win', metaleft: 'Win', metaright: '右 Win', space: '空格', '空格': '空格',
};
/* A rough stand-in for the core's parse_combo + format_combo (the real one validates). */
function mockCombo(text: string): string | null {
  const parts = text.split('+').map((p) => p.trim()).filter(Boolean);
  if (!parts.length) return null;
  return parts.map((p) => NAMES[p.toLowerCase().replace(/\s+/g, '')] ?? keyText(p).toUpperCase()).join(' + ');
}
const mask = (value: string) => (value.length >= 12 ? '••••' + value.slice(-4) : value.length >= 8 ? '••••' + value.slice(-2) : '••••');

/* Two Baseus pens plugged in (one low), a third seen before but away. */
function sampleInstances(): PenInstance[] {
  const t = now();
  return [
    { id: 'baseus:3f9a12c0', model: 'baseus', label: '倍思 #1', present: true, battery: { percent: 76, reported_at: t - 240 }, last_used: t - 600, active: false },
    { id: 'baseus:7b21e4d9', model: 'baseus', label: '倍思 #2', present: true, battery: { percent: 18, reported_at: t - 3600 }, last_used: t - 86400 * 2, active: false },
    { id: 'baseus:c0ffee01', model: 'baseus', label: '倍思 #3', present: false, battery: { percent: 52, reported_at: t - 86400 * 5 }, last_used: t - 86400 * 5, active: false },
  ];
}

export function createMock() {
  const handlers = new Set<Handler>();
  let notes = sampleNotes();
  const trash = new Map<string, Note>();
  const params = new URLSearchParams(location.search);
  // ?devices=none previews the empty states (no receiver, Tidepool away).
  const noDevices = params.get('devices') === 'none';
  // ?wizard=1 previews a fresh install: the setup wizard opens by itself.
  const fresh = params.get('wizard') === '1';
  // 兼容管理员权限窗口: an administrator account, not elevated, no task (?admin=0: a standard account).
  const elevation: ElevationStatus = { elevated: false, available: params.get('admin') !== '0', task: 'missing', task_path: null, smoke: false };
  const secrets: Partial<Record<SecretName, string>> = {};  // masked values only
  let testing = false;
  let meterTimer = 0;
  let meterUntil = 0;
  const snap: Snapshot = {
    version: '0.11.5-preview',
    state: { phase: 'disconnected', message: '', armed: false },
    result: null,
    usage: {
      active: { started: now() - 86400 * 2.6, ended: 0, complete: false, sessions: 23, observed: 0, armed: 0, recording: 0, audio_seconds: 1520, speech_seconds: 860 },
      cycles: [
        { started: now() - 86400 * 12, ended: now() - 86400 * 4, complete: true, sessions: 61, endpoint: '10_percent' },
        { started: now() - 86400 * 22, ended: now() - 86400 * 13, complete: true, sessions: 74, endpoint: '10_percent' },
      ],
      totals: { observed: 0, armed: 0, recording: 0, sessions: 158, audio_seconds: 5400, speech_seconds: 3100 },
      battery: { percent: 76, reported_at: now() - 240, source: 'baseus_hid' },
      battery_samples: [100, 96, 92, 88, 84, 80, 76].map((percent, i) => ({ percent, at: now() - (6 - i) * 36000 })),
      recent_sessions: [{ audio_seconds: 11.3, speech_seconds: 4.2, ended: now() - 600, outcome: 'completed' }],
      reduced_motion: false, warning: '', now: now(),
    },
    devices: [
      { id: 'baseus', name: '倍思 BS-OH085', short: 'BS-OH085', present: !noDevices, validation: '长按 / 短按已实测', modes: ['native'], microphone: 'BaseusPresent' },
      { id: 'pv60', name: '汉王 PV60', short: 'PV60', present: false, validation: '协议适配 · 等待实机验收', modes: ['hold', 'toggle'], microphone: 'Ai Pointer' },
    ],
    instances: noDevices ? [] : sampleInstances(),
    selected: 'baseus', mode: 'hold',
    tidepool: noDevices
      ? { installed: true, receiver_running: true, receiver_version: '4.0.5', control_supported: true, device_online: false, usb_connected: false, link: 'unknown', enabled: true, battery_percent: null, message: '预览数据：接收器在线，设备未连接' }
      : { installed: true, receiver_running: true, receiver_version: '4.0.5', control_supported: true, device_online: true, usb_connected: false, link: '无线', enabled: true, battery_percent: null, message: '预览数据：接收器在线，设备无线连接' },
    destination: 'input', pending_mode: null, tool: 'laser', preview_available: true, live_text: '',
    prefs: {
      mode: 'input', category: 'idea', sounds: { enabled: true, volume: 'medium', keep_awake: true },
      overlay: { enabled: true, on_switch: true, linger_seconds: 5, position: null }, preview: { show_local_text: false },
      airmouse: { enabled: true, skip_slideshow: true }, hotkeys: { toggle_mode: '', arm_toggle: '' },
      appearance: { theme: 'system', reduced_motion: false, live_shortcut: true }, present: { tool: 'laser', hide_cursor: true, zoom: 200 },
      voice: { whisper: 'auto', fast_raw: false }, devices: { coexist: true }, pen: { guard_shortcuts: true },
      diagnostics: { save_audio: false },
      general: { run_elevated: false },
      engine: {
        kind: 'openless',
        volc: { auth: 'key', app_id: '', resource_id: 'volc.seedasr.sauc.duration', endpoint: 'bigmodel_async' },
        dashscope: { model: 'fun-asr-realtime', region: 'cn' },
        polish: { style: 'raw', base_url: '', model: '', prompt: '' },
        hotkey: { profile: 'shandianshuo', keys: '', style: 'toggle' },
      },
      setup: { done: !fresh },
    },
    engine: { kind: 'openless', label: 'OpenLess', external: false },
    bindings: {
      devices: {
        baseus: {
          toggle_mode: { source: 'vendor', fire: ['7c'], ignore: ['7d'], gesture: 'single', label: '长按翻译键 ⑦', default: true },
          quick_note: { source: 'vendor', fire: ['7a', '7b'], gesture: 'single', label: '短按翻译键 ⑦', default: true },
          right_click: { source: 'vendor', fire: ['6d'], gesture: 'single', label: '长按下翻页 ④', default: true },
          airmouse_activity: { source: 'vendor', fire: ['72'], ignore: ['73'], gesture: 'single', label: '飞鼠键 ⑤', default: true },
          airmouse_wheel_up: { source: 'vendor', fire: ['78'], gesture: 'single', label: '音量 + ⑨', default: true },
          airmouse_wheel_down: { source: 'vendor', fire: ['79'], gesture: 'single', label: '音量 − ⑩', default: true },
          airmouse_ctrl: { source: 'vendor', fire: ['68'], ignore: ['69'], gesture: 'single', label: '标注键 ⑧', default: true },
        },
        pv60: {}, tidepool: {},
      },
      learning: null, warning: '',
    },
    armed: false, airmouse_active: true, smoke: false,
    elevation: { elevated: false, can_elevate: elevation.available },
    // ?prefs=broken: prefs.json could not be read (the settings page says so).
    prefs_warning: params.get('prefs') === 'broken'
      ? '偏好设置文件 prefs.json 已损坏，无法读取：本次使用默认设置；原文件保留，修改会保存到 prefs-recovered.json。' : '',
  };
  const defaultBindings = structuredClone(snap.bindings);
  const emit = (event: string, data: any) => handlers.forEach((h) => h(event, data));
  const apps = sampleApps();
  // Like the core: Doubao's key saved while another engine runs (presence only).
  const suggestVolc = (engine: EnginePrefs) => engine.kind !== 'volc'
    && (engine.volc.auth === 'key' ? !!secrets['volc.api_key'] : !!engine.volc.app_id && !!secrets['volc.access_token']);
  const engineInfo = (engine: EnginePrefs): EngineInfo => {
    const hint = suggestVolc(engine) ? { suggest_volc: true } : {};
    if (engine.kind !== 'hotkey') return { kind: engine.kind, label: { openless: 'OpenLess', volc: '豆包语音', dashscope: '阿里云百炼' }[engine.kind], external: false, ...hint };
    const app = apps.find((a) => a.profile === engine.hotkey.profile);
    const name = app?.name ?? '自定义快捷键';
    return { kind: 'hotkey', label: name, external: true, app: name, profile: engine.hotkey.profile, combo: engine.hotkey.keys || app?.combo || '', style: app?.style ?? engine.hotkey.style, ...hint };
  };
  const secretsChanged = (name: SecretName) => {
    const status = secretStatus();
    emit('secrets', status);
    if (name === 'volc.api_key' || name === 'volc.access_token') { snap.engine = engineInfo(snap.prefs.engine!); emit('engine', snap.engine); }
    return status;
  };
  const SECRET_NAMES: SecretName[] = ['volc.api_key', 'volc.access_token', 'dashscope.api_key', 'llm.api_key'];
  const secretStatus = () => ({ secrets: Object.fromEntries(SECRET_NAMES.map((name) => [name, { set: !!secrets[name], masked: secrets[name] ?? '' }])) });
  const busy = () => ['starting', 'recording', 'processing'].includes(snap.state.phase);
  const state = (phase: Snapshot['state']['phase'], message = '') => {
    // Whisper boosting is reported per session: always on "始终", never on "关闭".
    const whisper = phase === 'recording' && snap.prefs.voice?.whisper === 'on';
    snap.state = { phase, message, armed: phase !== 'disconnected', ...(whisper ? { whisper } : {}) };
    snap.armed = snap.state.armed!;
    emit('state', snap.state);
  };
  const setMode = (value: Mode, source = 'ui') => {
    snap.destination = value;
    snap.prefs.mode = value;
    const event = { value, pending: null, source, armed: snap.armed, tool: snap.tool };
    emit('mode', event);
    return event;
  };
  const setTool = (tool: Tool) => {
    snap.tool = tool;
    emit('tool', { tool });
    return { tool };
  };
  const setInstances = (list: PenInstance[]) => {
    snap.instances = list;
    emit('instances', list);
    return list;
  };
  const markActive = (active: boolean) => {
    const list = snap.instances ?? [];
    const pen = list.find((p) => p.present);
    if (!pen) return;
    setInstances(list.map((p) => (p.id === pen.id ? { ...p, active, last_used: now() } : p)));
  };
  let timers: number[] = [];
  const later = (ms: number, fn: () => void) => timers.push(window.setTimeout(fn, ms));
  /* A demo session. Its live words come from `source`: 'local' (the default, like the
     preview model; hidden unless 显示本机预览文字 is on) or 'remote' / 'cloud'. */
  function play(kind: 'input' | 'notes', { source = 'local' }: { source?: LiveSource } = {}) {
    timers.forEach(clearTimeout); timers = [];
    if (snap.destination !== kind) setMode(kind, 'key');
    if (snap.state.phase === 'disconnected') state('idle', '连接成功');
    const session = 'demo' + Math.random().toString(16).slice(2);
    later(1800, () => state('starting', '正在等待 OpenLess 确认'));
    later(2100, () => { state('recording', '正在录音'); markActive(true); });
    let n = 0;
    const levels = window.setInterval(() => emit('level', 0.02 + 0.06 * Math.abs(Math.sin(++n * 0.33)) * Math.abs(Math.cos(n * 0.11))), 100);
    timers.push(levels as unknown as number);
    const spoken = kind === 'notes'
      ? '今天想把点声的切换做得更直觉 长按翻译键胶囊就告诉我现在是输入笔记还是演示 说完再按一次就保存'
      : '创建一个 TypeScript 函数 读取配置文件 并在请求失败时显示重试按钮';
    const polished = kind === 'notes'
      ? '今天想把点声的切换做得更直觉。长按翻译键，胶囊就告诉我现在是输入、笔记还是演示。说完再按一次就保存。'
      : '创建一个 TypeScript 函数，读取配置文件，并在请求失败时显示重试按钮。';
    const chars = [...spoken];
    const end = 2400 + chars.length * 110;
    chars.forEach((_, i) => later(2400 + i * 110, () => emit('partial', { session, text: chars.slice(0, i + 1).join(''), final: false, source })));
    later(end, () => { clearInterval(levels); markActive(false); state('processing', kind === 'notes' ? '正在整理这条笔记' : 'OpenLess 正在识别并输入文字'); });
    later(end + 900, () => {
      const envelope = Array.from({ length: 90 }, (_, i) => 0.01 + 0.045 * Math.abs(Math.sin(i * 0.3)));
      if (kind === 'notes') {
        const id = 'e'.repeat(32);
        notes = [{ id, created: now(), tags: [], title: polished.split('。')[0] + '。', source: polished, text: polished, duration: (end - 2100) / 1000 }, ...notes.filter((x) => x.id !== id)];
        emit('result', { text: polished, seconds: 0.4, envelope, destination: 'notes', note_id: id });
        emit('note_saved', { id });
        state('idle', '笔记已保存；按笔继续记录下一段');
      } else {
        emit('result', { text: polished, seconds: 0.42, envelope, destination: 'input' });
        state('idle', '本段已完成');
      }
    });
  }
  /* A setup-wizard test recording ("试一句"): shown in the wizard only, never typed. */
  function speak(text = '今天下午三点在三楼会议室开会。') {
    timers.forEach(clearTimeout); timers = [];
    if (snap.state.phase === 'disconnected') state('idle', '连接成功');
    later(200, () => state('starting', '正在连接'));
    later(450, () => state('recording', '正在录音'));
    let n = 0;
    const tick = window.setInterval(() => emit('level', 0.05 + 0.22 * Math.abs(Math.sin(++n * 0.4))), 100);
    timers.push(tick as unknown as number);
    later(2300, () => { clearInterval(tick); state('processing', '正在识别（试说，不会输入）'); });
    later(2900, () => {
      const info = snap.engine!;
      const envelope = Array.from({ length: 60 }, (_, i) => 0.02 + 0.05 * Math.abs(Math.sin(i * 0.35)));
      const value = info.external ? { text: '', test: testing, external: true, app: info.app, seconds: 0.3, envelope, destination: 'input' }
        : { text, test: testing, seconds: 0.62, envelope, destination: 'input' };
      emit(testing ? 'wizard_result' : 'result', value);
      state('idle', testing ? '试说完成；文字没有输入到光标处' : '本段已完成');
    });
  }
  const key = (action = 'start') => emit('key', action);
  /* An administrator window in front: a ⑤ click it dropped (said once per window), or text
     it refused (kept: copied to the clipboard). */
  const blocked = () => emit('elevated', { kind: 'click', process: 'androws.exe', first: true, message: ELEVATION_NOTICE });
  const refused = (text = '这个剧情反转太精彩了', copied = true) => {
    const value = { text, seconds: 0.4, envelope: [], destination: 'input', not_typed: 'elevated', copied,
      not_typed_message: `文字没能输入：该窗口以管理员权限运行，系统拦截了点声的模拟输入。${copied ? '文字已复制到剪贴板，按 Ctrl+V 粘贴' : '剪贴板被占用，文字保留在点声主窗口的本次结果里'}；在 设置 → 通用 → 兼容管理员权限窗口 开启后可直接输入。` };
    snap.result = value as any;
    emit('result', value);
    state('idle', '文字没能输入到管理员权限窗口，已保留在本次结果里');
  };
  // What the setup wizard holds right now: test mode and the microphone meter (UI QA).
  const leases = () => ({ testing, meter: meterUntil > Date.now() });
  (window as any).__demo = { play, speak, key, setMode, setTool, emit, state, setInstances, snap, secrets, blocked, refused, elevation, leases };
  const query = params.get('demo');
  const live = params.get('live');  // ?live=remote: the demo session streams accurate words
  if (query === 'input' || query === 'notes') setTimeout(() => play(query, { source: live === 'remote' || live === 'cloud' ? live : 'local' }), 400);
  if (query === 'switch') setTimeout(() => setMode(MODES[(MODES.indexOf(snap.destination) + 1) % MODES.length], 'key'), 400);
  if (query === 'present') setTimeout(() => { setMode('present', 'key'); later(2200, () => setTool('spotlight')); }, 400);

  return {
    on(handler: Handler) { handlers.add(handler); return () => handlers.delete(handler); },
    async shell(name: string, args?: Record<string, unknown>) {
      if (name === 'core_ready') return snap;
      if (name === 'elevation_status') return { ...elevation };
      if (name === 'elevation_set') {
        // The real shell asks UAC, registers / deletes the logon task and restarts DotVoice.
        await new Promise((done) => setTimeout(done, 300));
        if (busy()) throw '正在录音或识别，请等本段结束后再切换';
        const enabled = !!args?.enabled;
        if (enabled && !elevation.available) throw '当前 Windows 账户不是管理员，无法以管理员身份运行点声';
        elevation.task = enabled ? 'ours' : 'missing';
        elevation.elevated = enabled;  // as after the restart
        snap.elevation = { elevated: enabled, can_elevate: elevation.available };
        snap.prefs = { ...snap.prefs, general: { run_elevated: enabled } };
        emit('prefs', snap.prefs);
        return { restarting: true };
      }
      if (name === 'elevation_restart') { elevation.elevated = true; return { restarting: true }; }
      return undefined;
    },
    async call(method: string, params: any): Promise<any> {
      switch (method) {
        case 'check_updates': {
          const update = snap.app_update ?? { status: 'current', current_version: snap.version, latest_version: snap.version,
            download_url: null, release_url: null, last_checked: Date.now() / 1000, error: null,
            available: false, dismissed: false, dismissed_version: null };
          emit('app_update', update);
          return { update };
        }
        case 'dismiss_update': {
          const current = snap.app_update;
          if (current && current.latest_version === params.version) {
            snap.app_update = { ...current, dismissed: true, dismissed_version: params.version };
            emit('app_update', snap.app_update);
          }
          return { update: snap.app_update };
        }
        case 'snapshot': return { ...snap, usage: { ...snap.usage, now: now() } };
        case 'action':
          if (params.action === 'arm') state('idle', '连接成功；把光标放进编辑框后使用笔上录音键');
          else if (params.action === 'disarm') state('disconnected', '已暂停，麦克风已释放');
          else if (params.action === 'stop') state('processing', '识别中');
          return { ok: true };
        case 'set_mode': return setMode(params.value, params.source);
        case 'toggle_mode': return setMode(MODES[(MODES.indexOf(snap.destination) + 1) % MODES.length], 'ui');
        case 'set_tool': return setTool(TOOLS.includes(params.tool) ? params.tool : 'mouse');
        case 'notes': return { items: notes, tags: tagsOf(notes), warning: '' };
        case 'update_note': {
          notes = notes.map((n) => (n.id === params.identifier ? { ...n, text: params.text, updated: now() } : n));
          return { note: notes.find((n) => n.id === params.identifier) };
        }
        case 'set_note_tags': {
          notes = notes.map((n) => (n.id === params.identifier ? { ...n, tags: [...new Set<string>(params.tags)] } : n));
          return { note: notes.find((n) => n.id === params.identifier) };
        }
        case 'delete_note': {
          const note = notes.find((n) => n.id === params.identifier);
          if (note) trash.set(note.id, note);
          notes = notes.filter((n) => n.id !== params.identifier);
          return { id: params.identifier };
        }
        case 'restore_note': {
          const note = trash.get(params.identifier);
          if (note) notes = [...notes, note].sort((a, b) => b.created - a.created);
          return { note };
        }
        case 'copy_text': return { ok: true };
        case 'prefs_set': {
          const merge = (a: any, b: any) => { for (const k in b) { if (b[k] && typeof b[k] === 'object' && !Array.isArray(b[k])) merge(a[k], b[k]); else a[k] = b[k]; } };
          // Match the real core: each response/event is a new JSON value.
          const updated = structuredClone(snap.prefs);
          const patch = structuredClone(params.patch ?? {});
          if (patch.engine?.hotkey?.keys) {
            const combo = mockCombo(patch.engine.hotkey.keys);
            if (!combo) return { error: '快捷键格式无效' };
            patch.engine.hotkey.keys = combo;
          }
          if (patch.engine?.polish?.base_url && !/^https:\/\//.test(patch.engine.polish.base_url)) {
            return { error: '润色：Base URL 无效，应以 https:// 开头（例如 https://api.example.com/v1）' };
          }
          merge(updated, patch);
          if (patch.engine) {
            const before = snap.prefs.engine!, after = updated.engine!;
            const running = (engine: EnginePrefs) => { const { suggest_volc: _hint, ...info } = engineInfo(engine); return JSON.stringify(info); };
            const changed = running(before) !== running(after)
              || JSON.stringify(before[before.kind as 'volc'] ?? null) !== JSON.stringify(after[after.kind as 'volc'] ?? null);
            if (changed && busy()) return { error: '正在录音或识别，请等本段结束后再更改语音引擎' };
            if (after.kind === 'hotkey' && after.hotkey.profile === 'custom' && !after.hotkey.keys) return { error: '请先录入目标软件的快捷键' };
          }
          snap.prefs = updated;
          if (patch.engine) { snap.engine = engineInfo(updated.engine!); emit('engine', snap.engine); }
          emit('prefs', updated); return updated;
        }
        case 'secret_status': return secretStatus();
        case 'secret_set': {
          const value = String(params.value ?? '').trim();
          if (!SECRET_NAMES.includes(params.name)) return { error: '不支持的密钥名称' };
          if (/\s|[^\x21-\x7e]/.test(value)) return { error: '密钥格式不正确（不能含空格、换行或中文）' };
          if (value) secrets[params.name as SecretName] = mask(value); else delete secrets[params.name as SecretName];
          return secretsChanged(params.name);
        }
        case 'secret_delete': {
          delete secrets[params.name as SecretName];
          return secretsChanged(params.name);
        }
        case 'engine_test': {
          await new Promise((done) => setTimeout(done, 700));
          const engine = snap.prefs.engine!;
          if (params.kind === 'volc') {
            const ok = engine.volc.auth === 'key' ? !!secrets['volc.api_key'] : !!engine.volc.app_id && !!secrets['volc.access_token'];
            return { kind: 'volc', ok, seconds: 0.82, message: ok ? '豆包语音连接正常' : '豆包语音：请填写新版控制台的 API Key（或改用 App ID + Access Token）' };
          }
          if (params.kind === 'dashscope') {
            const ok = !!secrets['dashscope.api_key'];
            return { kind: 'dashscope', ok, seconds: 0.64, message: ok ? '阿里云百炼连接正常' : '阿里云百炼：请填写 API Key（百炼控制台“API Key”页面）' };
          }
          if (params.kind === 'polish') {
            if (engine.polish.style === 'raw') return { kind: 'polish', ok: true, seconds: 0, message: '原文模式不调用大模型' };
            const ok = !!secrets['llm.api_key'] && !!engine.polish.base_url && !!engine.polish.model;
            return { kind: 'polish', ok, seconds: 1.12, message: ok ? '润色服务可用' : '润色：请填写 API Key' };
          }
          return { error: '这个引擎没有连接测试' };
        }
        case 'hotkey_detect': return { apps: structuredClone(apps), mic: MIC, custom_mic_hint: `在目标软件的设置里把麦克风选为「${MIC}」（翻页笔接收器）；软件没有麦克风选项时，打开 Windows 设置 → 系统 → 声音 → 输入，把它设为默认录音设备。` };
        case 'openless_status': return { installed: true, running: true, remote_input: true, paired: true, port: 8443, active: snap.engine?.kind === 'openless', connected: snap.engine?.kind === 'openless' && snap.armed };
        case 'wizard_test': testing = !!params.enabled; return { ok: true, enabled: testing };
        case 'mic_meter': {
          meterUntil = Date.now() + Number(params.seconds ?? 0) * 1000;
          clearInterval(meterTimer);
          if (params.seconds > 0) {
            emit('meter', { ok: !noDevices, message: noDevices ? '未连接所选设备：倍思 BS-OH085' : '' });
            if (!noDevices) {
              let n = 0;
              meterTimer = window.setInterval(() => {
                if (Date.now() > meterUntil) { clearInterval(meterTimer); return; }
                n += 1;
                emit('level', n % 40 < 22 ? 0.03 + 0.18 * Math.abs(Math.sin(n * 0.45)) : 0.004);
              }, 100);
            }
          }
          return { ok: true };
        }
        case 'bindings': return snap.bindings;
        case 'binding_set': {
          snap.bindings = structuredClone(snap.bindings);
          const slots = snap.bindings.devices[params.device] ?? {};
          if (params.binding) slots[params.slot] = { ...params.binding, default: false };
          else delete slots[params.slot];
          return snap.bindings;
        }
        case 'binding_reset': {
          snap.bindings = structuredClone(snap.bindings);
          const slots = snap.bindings.devices[params.device] ?? {};
          const original = defaultBindings.devices[params.device]?.[params.slot];
          if (original) slots[params.slot] = structuredClone(original);
          else delete slots[params.slot];
          return snap.bindings;
        }
        case 'binding_learn':
          if (params.slot === 'right_click') {  // one long press of ④ (measured: 6D)
            later(1100, () => {
              snap.bindings = structuredClone(snap.bindings);
              snap.bindings.devices.baseus.right_click = { source: 'vendor', fire: ['6d'], ignore: [], gesture: 'single', label: '自定义 6D', default: false };
              emit('learn', { device: params.device, slot: params.slot, step: 'long', captured: ['6D'], status: 'done', message: '已保存' });
            });
            return { learning: { device: params.device, slot: params.slot, step: 'long', captured: [] } };
          }
          if (params.slot === 'airmouse_wheel_up' || params.slot === 'airmouse_wheel_down') {  // one press of ⑨ / ⑩
            const code = params.slot === 'airmouse_wheel_up' ? '78' : '79';
            later(1000, () => {
              snap.bindings = structuredClone(snap.bindings);
              snap.bindings.devices.baseus[params.slot] = { source: 'vendor', fire: [code], ignore: [], gesture: 'single', label: `自定义 ${code}`, default: false };
              emit('learn', { device: params.device, slot: params.slot, step: 'tap', captured: [code], status: 'done', message: '已保存' });
            });
            return { learning: { device: params.device, slot: params.slot, step: 'tap', captured: [] } };
          }
          later(900, () => emit('learn', { device: params.device, slot: params.slot, step: 'tap', captured: ['7A'], status: 'step', message: '' }));
          later(1700, () => emit('learn', { device: params.device, slot: params.slot, step: 'hold', captured: ['7A', '7B'], status: 'step', message: '' }));
          return { learning: { device: params.device, slot: params.slot, step: 'tap', captured: [] } };
        case 'binding_cancel': return { ok: true };
        case 'open_diagnostics': return { ok: true, path: 'C:\\Users\\预览\\AppData\\Local\\DotVoice\\runtime\\diagnostics' };
        case 'select_device': snap.selected = params.device; snap.mode = params.mode ?? 'hold'; return { ...snap };
        case 'rename_instance': {
          const label = String(params.label ?? '').trim();
          if (!label) return { error: '名称不能为空' };
          return { instances: setInstances((snap.instances ?? []).map((p) => (p.id === params.id ? { ...p, label } : p))) };
        }
        case 'tidepool_action': return { status: snap.tidepool };
        case 'mark': return snap.usage;
        case 'note_record': setMode('notes'); state('idle'); return { ok: true, awaiting_pen: true };
        default: return { ok: true };
      }
    },
  };
}
