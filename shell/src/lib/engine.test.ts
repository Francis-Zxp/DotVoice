import { describe, expect, it } from 'vitest';
import {
  comboFromCodes, comboParts, comboText, doubaoHint, ENGINE_TONE, ENGINE_TONE_TEXT, engineToneVars, ENGINES, handedTo, hotkeyWarnings, keyText, missing, polishMissing, SECRET, suggestDoubao,
} from './engine';
import type { EnginePrefs, SecretStatus } from './types';

const engine = (patch: Partial<EnginePrefs> = {}): EnginePrefs => ({
  kind: 'openless',
  volc: { auth: 'key', app_id: '', resource_id: 'volc.seedasr.sauc.duration', endpoint: 'bigmodel_async' },
  dashscope: { model: 'fun-asr-realtime', region: 'cn' },
  polish: { style: 'raw', base_url: '', model: '', prompt: '' },
  hotkey: { profile: 'shandianshuo', keys: '', style: 'toggle' },
  ...patch,
});

const secrets = (...set: (keyof typeof SECRET)[]): SecretStatus['secrets'] => Object.fromEntries(
  Object.entries(SECRET).map(([alias, name]) => [name, { set: set.includes(alias as keyof typeof SECRET), masked: set.includes(alias as keyof typeof SECRET) ? '••••abcd' : '' }]),
) as SecretStatus['secrets'];

describe('engine catalogue', () => {
  it('lists the four engines with Doubao recommended first', () => {
    expect(ENGINES.map((e) => e.kind)).toEqual(['volc', 'dashscope', 'openless', 'hotkey']);
    expect(ENGINES[0].badge).toBe('推荐');
  });

  it('gives every engine one accent, OpenLess from the theme token', () => {
    expect(Object.keys(ENGINE_TONE).sort()).toEqual(['dashscope', 'hotkey', 'openless', 'volc']);
    expect(ENGINE_TONE.openless).toBe('var(--engine-openless, #7c3aed)');
    expect(Object.values(ENGINE_TONE)).not.toContain('var(--brand)');
  });

  it('pairs each fill with a text shade: Doubao and OpenLess follow their theme text tokens', () => {
    expect(Object.keys(ENGINE_TONE_TEXT).sort()).toEqual(['dashscope', 'hotkey', 'openless', 'volc']);
    expect(ENGINE_TONE_TEXT.volc).toBe('var(--engine-volc-text, #2850c8)');
    expect(ENGINE_TONE_TEXT.openless).toBe('var(--engine-openless-text, #7c3aed)');
    // The fill never doubles as the text token (dark: white glyph on pale lavender).
    expect(ENGINE_TONE.openless).not.toContain('-text');
    expect(engineToneVars('openless')).toEqual({ '--tone': ENGINE_TONE.openless, '--tone-text': ENGINE_TONE_TEXT.openless });
    expect(engineToneVars('dashscope')).toEqual({ '--tone': '#f2711c', '--tone-text': '#f2711c' });
  });
});

describe('the Doubao hint', () => {
  it('shows while Doubao is ready but another engine runs', () => {
    expect(suggestDoubao(engine(), secrets('volcKey'))).toBe(true);
    expect(suggestDoubao(engine({ kind: 'hotkey' }), secrets('volcKey'))).toBe(true);
    expect(suggestDoubao(engine({ kind: 'volc' }), secrets('volcKey'))).toBe(false);
    expect(suggestDoubao(engine(), secrets('dashscope'))).toBe(false);
    const legacy = engine({ volc: { auth: 'legacy', app_id: '', resource_id: 'volc.seedasr.sauc.duration', endpoint: 'bigmodel_async' } });
    expect(suggestDoubao(legacy, secrets('volcToken'))).toBe(false);
    expect(suggestDoubao({ ...legacy, volc: { ...legacy.volc, app_id: '123456' } }, secrets('volcToken'))).toBe(true);
  });

  it('uses the core flag until the saved-key status is loaded, which then decides', () => {
    expect(suggestDoubao(engine(), null, { suggest_volc: true })).toBe(true);
    expect(suggestDoubao(engine(), null, {})).toBe(false);
    expect(suggestDoubao(engine(), null)).toBe(false);
    expect(suggestDoubao(engine(), secrets(), { suggest_volc: true })).toBe(false);
    expect(suggestDoubao(engine({ kind: 'volc' }), null, { suggest_volc: true })).toBe(false);
  });

  it('names the engine in use', () => {
    expect(doubaoHint('OpenLess')).toBe('已配置豆包语音密钥，但当前仍在使用 OpenLess。豆包内置引擎是实时二遍识别，轻声和气声更准。');
    expect(doubaoHint('阿里云百炼')).toContain('仍在使用阿里云百炼。');
  });
});

describe('what an engine still needs', () => {
  it('OpenLess and a built-in hotkey profile are always usable', () => {
    expect(missing('openless', engine(), null)).toBeNull();
    expect(missing('hotkey', engine(), null)).toBeNull();
    expect(missing('hotkey', engine({ hotkey: { profile: 'custom', keys: '', style: 'toggle' } }), null)).toBe('先录入目标软件的快捷键');
    expect(missing('hotkey', engine({ hotkey: { profile: 'custom', keys: 'F9', style: 'hold' } }), null)).toBeNull();
  });

  it('cloud engines need their key; the old Doubao console also an App ID', () => {
    expect(missing('volc', engine(), secrets())).toBe('先保存 API Key');
    expect(missing('volc', engine(), secrets('volcKey'))).toBeNull();
    const legacy = engine({ volc: { auth: 'legacy', app_id: '', resource_id: 'volc.seedasr.sauc.duration', endpoint: 'bigmodel_async' } });
    expect(missing('volc', legacy, secrets('volcKey'))).toBe('先填写 App ID');
    expect(missing('volc', { ...legacy, volc: { ...legacy.volc, app_id: '123456' } }, secrets())).toBe('先保存 Access Token');
    expect(missing('volc', { ...legacy, volc: { ...legacy.volc, app_id: '123456' } }, secrets('volcToken'))).toBeNull();
    expect(missing('dashscope', engine(), secrets('volcKey'))).toBe('先保存 API Key');
    expect(missing('dashscope', engine(), secrets('dashscope'))).toBeNull();
  });

  it('a half-configured polish never blocks the switch: the engine types the transcript as it is', () => {
    const light = engine({ polish: { style: 'light', base_url: 'https://api.example.com/v1', model: 'm', prompt: '' } });
    expect(missing('volc', light, secrets('volcKey'))).toBeNull();
    const custom = engine({ polish: { style: 'custom', base_url: '', model: '', prompt: '  ' } });
    expect(missing('dashscope', custom, secrets('dashscope'))).toBeNull();
  });

  it('says inline what the chosen polish still lacks', () => {
    expect(polishMissing(engine().polish, null)).toEqual([]);
    const light = engine({ polish: { style: 'light', base_url: 'https://api.example.com/v1', model: 'm', prompt: '' } }).polish;
    expect(polishMissing(light, secrets('volcKey'))).toEqual(['API Key']);
    expect(polishMissing(light, secrets('llm'))).toEqual([]);
    const custom = engine({ polish: { style: 'custom', base_url: ' ', model: '', prompt: '' } }).polish;
    expect(polishMissing(custom, null)).toEqual(['Base URL', '模型', 'API Key', '提示词']);
  });
});

describe('shortcut text', () => {
  it('orders modifiers first and reads like the core', () => {
    expect(comboFromCodes(['KeyH', 'MetaLeft'])).toBe('MetaLeft+KeyH');
    expect(comboFromCodes(['MetaLeft', 'ControlLeft', 'ControlLeft'])).toBe('ControlLeft+MetaLeft');
    expect(comboText(['AltRight'])).toBe('右 Alt');
    expect(comboText(['ControlLeft', 'AltLeft', 'Space'])).toBe('Ctrl + Alt + 空格');
    expect(keyText('Digit7')).toBe('7');
    expect(keyText('F9')).toBe('F9');
    expect(comboParts('Ctrl + Win')).toEqual(['Ctrl', 'Win']);
  });

  it('warns about admin windows and the first syllable, and Ctrl + Win clashes', () => {
    const base = hotkeyWarnings({ combo: '右 Alt', style: 'toggle', profile: 'typeless' }).map((w) => w.id);
    expect(base).toEqual(['admin', 'syllable']);
    expect(hotkeyWarnings({ combo: 'Ctrl + Win', style: 'toggle', profile: 'shandianshuo' }).map((w) => w.id)).toContain('ctrlwin');
    expect(hotkeyWarnings(null, 'Ctrl + Win').map((w) => w.id)).toContain('ctrlwin');
  });

  it('names the app a session was handed to', () => {
    expect(handedTo('闪电说')).toBe('已交给闪电说');
    expect(handedTo('Typeless')).toBe('已交给 Typeless');
    expect(handedTo('')).toBe('已交给对方软件');
  });
});
