/* Speech engines: the catalogue shown in settings and the setup wizard, plus small pure
   helpers (readiness, shortcut text, warnings). Keys never pass through here: the UI only
   ever sees `secret_status` (set + masked). */
import type { EngineInfo, EngineKind, EnginePrefs, HotkeyApp, SecretName, SecretStatus } from './types';

export const SECRET: Record<'volcKey' | 'volcToken' | 'dashscope' | 'llm', SecretName> = {
  volcKey: 'volc.api_key', volcToken: 'volc.access_token', dashscope: 'dashscope.api_key', llm: 'llm.api_key',
};

export interface EngineCard {
  kind: EngineKind;
  title: string;
  badge?: string;
  tagline: string;
}

export const ENGINES: EngineCard[] = [
  { kind: 'volc', title: '豆包语音', badge: '推荐', tagline: '在线流式，最快最准' },
  { kind: 'dashscope', title: '阿里云百炼', tagline: '在线流式 · Fun-ASR / Paraformer' },
  { kind: 'openless', title: 'OpenLess', tagline: '已安装时可用：深度集成，沿用它的润色风格' },
  { kind: 'hotkey', title: '其他语音软件', badge: '实验', tagline: '闪电说等，用热键桥接' },
];

export const ENGINE_TITLE: Record<EngineKind, string> = {
  volc: '豆包语音', dashscope: '阿里云百炼', openless: 'OpenLess', hotkey: '其他语音软件',
};

/** Each engine's accent (--tone) on the settings cards and the wizard's choices. OpenLess
    follows the theme's --engine-openless token (violet where a theme has none). */
export const ENGINE_TONE: Record<EngineKind, string> = {
  volc: '#3a6df0', dashscope: '#f2711c', openless: 'var(--engine-openless, #7c3aed)', hotkey: 'var(--present)',
};

/** The same accents for text and glyphs on a surface (the cards' icons and badges, the Doubao
    hint's button). ENGINE_TONE stays the fill, dark enough for a white glyph; on a dark theme
    text needs a lighter shade, which the theme's --engine-*-text token gives. */
export const ENGINE_TONE_TEXT: Record<EngineKind, string> = {
  volc: 'var(--engine-volc-text, #2850c8)', dashscope: 'var(--engine-dashscope-text, #9c3d00)',
  openless: 'var(--engine-openless-text, #7c3aed)', hotkey: 'var(--engine-hotkey-text, #066258)',
};

/** The custom properties an engine card or choice sets: --tone (fill) and --tone-text. */
export function engineToneVars(kind: EngineKind): Record<'--tone' | '--tone-text', string> {
  return { '--tone': ENGINE_TONE[kind], '--tone-text': ENGINE_TONE_TEXT[kind] };
}

export const VOLC_RESOURCES = [
  { id: 'volc.seedasr.sauc.duration', label: '2.0 小时版' },
  { id: 'volc.seedasr.sauc.concurrent', label: '2.0 并发版' },
] as const;

export const VOLC_ENDPOINTS = [
  { id: 'bigmodel_async', label: '优化版（推荐）', hint: '文字有变化才返回，延迟最低' },
  { id: 'bigmodel', label: '双向流式', hint: '每包都返回结果' },
  { id: 'bigmodel_nostream', label: '流式输入', hint: '说完才出字，但更准' },
] as const;

export const DASHSCOPE_MODELS = [
  { id: 'fun-asr-realtime', label: 'Fun-ASR（推荐）' },
  { id: 'paraformer-realtime-v2', label: 'Paraformer v2' },
] as const;

/* Pages that say where the keys come from (the core opens only these). */
export const HELP = {
  volcDocs: 'https://www.volcengine.com/docs/6561/1354869',
  volcConsole: 'https://console.volcengine.com/',
  bailianKey: 'https://help.aliyun.com/zh/model-studio/get-api-key',
  bailianConsole: 'https://bailian.console.aliyun.com/',
} as const;

export function secretSet(secrets: SecretStatus['secrets'] | null | undefined, name: SecretName) {
  return !!secrets?.[name]?.set;
}

/** What still stops this engine from working (null = ready to use). */
export function missing(kind: EngineKind, engine: EnginePrefs, secrets: SecretStatus['secrets'] | null): string | null {
  if (kind === 'openless') return null;
  if (kind === 'hotkey') return engine.hotkey.profile === 'custom' && !engine.hotkey.keys ? '先录入目标软件的快捷键' : null;
  if (kind === 'volc') {
    if (engine.volc.auth === 'key' && !secretSet(secrets, SECRET.volcKey)) return '先保存 API Key';
    if (engine.volc.auth === 'legacy') {
      if (!engine.volc.app_id) return '先填写 App ID';
      if (!secretSet(secrets, SECRET.volcToken)) return '先保存 Access Token';
    }
  }
  if (kind === 'dashscope' && !secretSet(secrets, SECRET.dashscope)) return '先保存 API Key';
  // Polish is optional: half filled in, the engine types the transcript as it is (and says so
  // once per recording), so it never blocks a switch - PolishForm shows what is missing.
  return null;
}

/** Doubao's key is saved but another engine runs: settings offers the switch in one line.
    The saved-key status decides once it is loaded; until then the core's flag
    (EngineInfo.suggest_volc, a presence check: the core never reads the key for it). */
export function suggestDoubao(engine: EnginePrefs, secrets: SecretStatus['secrets'] | null,
  info?: Pick<EngineInfo, 'suggest_volc'> | null): boolean {
  if (engine.kind === 'volc') return false;
  return secrets ? missing('volc', engine, secrets) === null : !!info?.suggest_volc;
}

/** The hint's sentence, naming the engine in use ("仍在使用 OpenLess", "仍在使用阿里云百炼"). */
export function doubaoHint(current: string) {
  return `已配置豆包语音密钥，但当前仍在${switchText(current)}。豆包内置引擎是实时二遍识别，轻声和气声更准。`;
}

/** What the chosen polish style still lacks (empty: complete, or 原文). Until then every
    recording is typed as recognised (core: POLISH_INCOMPLETE). */
export function polishMissing(polish: EnginePrefs['polish'], secrets: SecretStatus['secrets'] | null): string[] {
  if (polish.style === 'raw') return [];
  const lacking: string[] = [];
  if (!polish.base_url.trim()) lacking.push('Base URL');
  if (!polish.model.trim()) lacking.push('模型');
  if (!secretSet(secrets, SECRET.llm)) lacking.push('API Key');
  if (polish.style === 'custom' && !polish.prompt.trim()) lacking.push('提示词');
  return lacking;
}

/* ── Shortcuts: KeyboardEvent.code values as the core reads them (bridge.engines.hotkey) ── */
const MODIFIER_ORDER = ['ControlLeft', 'ControlRight', 'AltLeft', 'AltRight', 'ShiftLeft', 'ShiftRight', 'MetaLeft', 'MetaRight'];
const KEY_TEXT: Record<string, string> = {
  ControlLeft: 'Ctrl', ControlRight: '右 Ctrl', AltLeft: 'Alt', AltRight: '右 Alt', ShiftLeft: 'Shift',
  ShiftRight: '右 Shift', MetaLeft: 'Win', MetaRight: '右 Win', Space: '空格', Escape: 'Esc', Enter: 'Enter',
  Backspace: 'Backspace', ContextMenu: '菜单键', CapsLock: 'CapsLock',
};

export const isModifier = (code: string) => MODIFIER_ORDER.includes(code);

/** Display text for one key code, matching the core's (Ctrl, 右 Alt, 空格, A, F9 …). */
export function keyText(code: string) {
  if (KEY_TEXT[code]) return KEY_TEXT[code];
  if (/^Key[A-Z]$/.test(code)) return code.slice(3);
  if (/^Digit\d$/.test(code)) return code.slice(5);
  if (/^Numpad\d$/.test(code)) return `小键盘 ${code.slice(6)}`;
  return code;
}

/** Codes pressed together, modifiers first in their usual order: what the core stores. */
export function comboFromCodes(codes: string[]): string {
  const unique = [...new Set(codes)];
  const mods = unique.filter(isModifier).sort((a, b) => MODIFIER_ORDER.indexOf(a) - MODIFIER_ORDER.indexOf(b));
  return [...mods, ...unique.filter((c) => !isModifier(c))].join('+');
}

export function comboText(codes: string[]) {
  return comboFromCodes(codes).split('+').filter(Boolean).map(keyText).join(' + ');
}

/** 'Ctrl + Win' -> ['Ctrl', 'Win'] for key caps. */
export function comboParts(combo: string) {
  return combo.split(' + ').map((p) => p.trim()).filter(Boolean);
}

export const STYLE_TEXT: Record<'toggle' | 'hold', string> = {
  toggle: '按一下开始、再按一下结束',
  hold: '按住说话，松开结束',
};

export interface Warning { id: string; label: string; tip: string }

/** The limits of driving another app by synthetic keys, as chips. */
export function hotkeyWarnings(app: Pick<HotkeyApp, 'combo' | 'style' | 'profile'> | null, combo?: string): Warning[] {
  const list: Warning[] = [
    { id: 'admin', label: '管理员窗口无效', tip: '前台是以管理员身份运行的窗口时，Windows 会拦下模拟按键；以管理员身份运行的听写软件也可能收不到。' },
    { id: 'syllable', label: '开口稍等半拍', tip: '按下 ⑥ 后，对方软件要一点时间才开始收音，紧接着说的第一个字可能丢失。' },
  ];
  const keys = (combo || app?.combo || '').replace(/\s/g, '');
  if (keys === 'Ctrl+Win') {
    list.push({ id: 'ctrlwin', label: '只认 Ctrl + Win', tip: '闪电说、微信语音输入默认都用 Ctrl + Win，同时运行会冲突；在其中一个软件里改了快捷键，就在这里录入同一组合。' });
  }
  return list;
}

/** "使用豆包语音" / "使用 OpenLess" (a Latin name keeps a space from the Chinese). */
export function switchText(name: string) {
  return '使用' + (/^[\x00-\x7f]/.test(name) ? ' ' : '') + name;
}

/** "已交给闪电说" / "已交给 Typeless": a Latin name keeps a space from the Chinese. */
export function handedTo(name?: string | null) {
  const app = (name || '').trim() || '对方软件';
  return '已交给' + (/^[\x00-\x7f]/.test(app) ? ' ' : '') + app;
}
