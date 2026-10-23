/* Settings → 语音引擎: who turns speech into text. Four cards (the running engine is marked
   正在使用); clicking one opens its settings below, and "使用…" switches to it. Switching
   happens only through that button (or the one-line Doubao hint, which does the same), so
   looking at another engine never breaks a working setup. Keys are typed into SecretField
   and only ever shown masked. */
import { AnimatePresence, motion } from 'motion/react';
import {
  AlertTriangle, ChevronDown, Cloud, Keyboard, Link2, Mic, RefreshCw, ShieldCheck, Sparkles, X, Zap,
} from 'lucide-react';
import { useEffect, useState, type CSSProperties, type ReactNode } from 'react';
import { ComboEditor, HelpLink, SecretField, setEngine, StatusDot, TestButton, TextField, useEngineData } from '../components/EngineParts';
import { Glass, Segmented, SettingRow, spring, Switch, Tip } from '../components/ui';
import {
  DASHSCOPE_MODELS, doubaoHint, ENGINE_TITLE, ENGINE_TONE, ENGINE_TONE_TEXT, engineToneVars, ENGINES, HELP, hotkeyWarnings, missing, polishMissing,
  SECRET, secretSet, STYLE_TEXT, suggestDoubao, switchText, VOLC_ENDPOINTS, VOLC_RESOURCES,
} from '../lib/engine';
import { useStore } from '../lib/store';
import type { EngineKind, EnginePrefs, HotkeyApp, HotkeyDetect, OpenLessStatus, PolishStyle, Prefs } from '../lib/types';

export const DEFAULT_ENGINE: EnginePrefs = {
  kind: 'openless',
  volc: { auth: 'key', app_id: '', resource_id: 'volc.seedasr.sauc.duration', endpoint: 'bigmodel_async' },
  dashscope: { model: 'fun-asr-realtime', region: 'cn' },
  polish: { style: 'raw', base_url: '', model: '', prompt: '' },
  hotkey: { profile: 'shandianshuo', keys: '', style: 'toggle' },
};

export const ENGINE_ICON: Record<EngineKind, typeof Zap> = { volc: Zap, dashscope: Cloud, openless: Link2, hotkey: Keyboard };

const saved = (data: unknown) => !!data;

export function EngineSection({ prefs }: { prefs: Prefs }) {
  const engine = prefs.engine ?? DEFAULT_ENGINE;
  const info = useStore((s) => s.engine);
  const secrets = useStore((s) => s.secrets);
  const phase = useStore((s) => s.state.phase);
  const current = engine.kind;
  const [view, setView] = useState<EngineKind>(current);
  const data = useEngineData();
  useEffect(() => setView(current), [current]);
  const failing = phase === 'error' || !!info.error;
  // Not while the Doubao form is open: its header already offers the same switch.
  const hint = view !== 'volc' && suggestDoubao(engine, secrets, info);
  return (
    <Glass className="engine-panel">
      <DoubaoHint show={hint} current={info.kind === current ? info.label : ENGINE_TITLE[current]} />
      <div className="engine-grid" role="group" aria-label="选择语音引擎">
        {ENGINES.map((card) => (
          <EngineCardButton key={card.kind} kind={card.kind} title={card.title} badge={card.badge} tagline={card.tagline}
            viewed={view === card.kind} current={current === card.kind} failing={current === card.kind && failing}
            status={cardStatus(card.kind, engine, secrets, data.hotkeys, data.openless)} onClick={() => setView(card.kind)} />
        ))}
      </div>
      <AnimatePresence mode="wait" initial={false}>
        <motion.div key={view} className="engine-form" initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4, transition: { duration: .1 } }} transition={spring.soft}>
          <FormHeader kind={view} engine={engine} current={current === view}
            title={view === 'hotkey' ? data.hotkeys?.apps.find((a) => a.profile === engine.hotkey.profile)?.name ?? (engine.hotkey.profile === 'custom' ? '自定义快捷键' : undefined) : undefined} />
          {view === 'volc' && <VolcForm engine={engine} />}
          {view === 'dashscope' && <DashscopeForm engine={engine} />}
          {(view === 'volc' || view === 'dashscope') && <PolishForm engine={engine} />}
          {view === 'openless' && <OpenLessPanel status={data.openless} loading={data.loading} onRefresh={data.refresh} />}
          {view === 'hotkey' && <HotkeyForm engine={engine} detect={data.hotkeys} loading={data.loading} failed={data.failed} onRetry={data.refresh} />}
        </motion.div>
      </AnimatePresence>
    </Glass>
  );
}

function cardStatus(kind: EngineKind, engine: EnginePrefs, secrets: ReturnType<typeof useStore.getState>['secrets'],
  hotkeys: HotkeyDetect | null, openless: OpenLessStatus | null): string {
  if (kind === 'volc') {
    const key = engine.volc.auth === 'key' ? secretSet(secrets, SECRET.volcKey) : !!engine.volc.app_id && secretSet(secrets, SECRET.volcToken);
    return secrets ? (key ? '密钥已保存' : '需要 API Key') : '';
  }
  if (kind === 'dashscope') return secrets ? (secretSet(secrets, SECRET.dashscope) ? '密钥已保存' : '需要 API Key') : '';
  if (kind === 'openless') {
    if (!openless) return '';
    if (!openless.installed) return '未检测到 OpenLess';
    if (!openless.running) return '已安装 · 未运行';
    return openless.remote_input && openless.paired ? '运行中 · 远程输入已开' : '运行中 · 远程输入未开';
  }
  const found = (hotkeys?.apps ?? []).filter((a) => a.detected && !a.builtin);
  return found.length ? `检测到 ${found.slice(0, 2).map((a) => a.name).join('、')}` : hotkeys ? '可桥接 Windows 语音输入等' : '';
}

/* Dismissed for the rest of this app session (prefs has no key for it). */
let hintDismissed = false;

/** One calm line: Doubao's key is saved, yet another engine runs. The button does what the
    Doubao form's "使用豆包语音" does; the hint goes once switched or dismissed. */
function DoubaoHint({ show, current }: { show: boolean; current: string }) {
  const [dismissed, setDismissed] = useState(hintDismissed);
  const [busy, setBusy] = useState(false);
  // The volc fill tints the strip; its text shade (per theme) colours the icon and the button.
  const tone = ENGINE_TONE.volc;
  const ink = ENGINE_TONE_TEXT.volc;
  async function use() {
    setBusy(true);
    await setEngine({ kind: 'volc' });
    setBusy(false);
  }
  function dismiss() {
    hintDismissed = true;
    setDismissed(true);
  }
  return (
    <AnimatePresence initial={false}>
      {show && !dismissed && (
        <motion.div key="doubao-hint" className="overflow-hidden" initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
          exit={{ height: 0, opacity: 0, transition: { duration: .16 } }} transition={spring.soft}>
          <div className="mb-3 flex flex-wrap items-center gap-x-2.5 gap-y-1.5 rounded-xl px-3.5 py-2" role="note" data-engine-hint="volc"
            style={{ background: `color-mix(in srgb, ${tone} 7%, transparent)`, border: `1px solid color-mix(in srgb, ${tone} 16%, transparent)` }}>
            <Zap size={14} className="shrink-0" style={{ color: ink }} />
            <span className="muted min-w-0 flex-1 text-[12.5px] leading-relaxed">{doubaoHint(current)}</span>
            <span className="flex shrink-0 items-center gap-1">
              <button className="btn btn-ghost btn-sm" style={{ color: ink }} disabled={busy} onClick={use}>
                {busy && <RefreshCw size={13} className="animate-spin" />}{switchText(ENGINE_TITLE.volc)}
              </button>
              <button className="btn btn-ghost btn-sm btn-icon" aria-label="不再提示" title="不再提示" onClick={dismiss}><X size={14} /></button>
            </span>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export function EngineCardButton({ kind, title, badge, tagline, viewed, current, failing, status, onClick, compact }: {
  kind: EngineKind; title: string; badge?: string; tagline: string; viewed: boolean; current: boolean; failing?: boolean; status: string;
  onClick: () => void; compact?: boolean;
}) {
  const Icon = ENGINE_ICON[kind];
  return (
    <button className="engine-card" data-engine={kind} aria-pressed={viewed} data-current={current} data-compact={compact || undefined}
      style={engineToneVars(kind) as CSSProperties} onClick={onClick}>
      {viewed && <motion.span layoutId={compact ? 'engine-thumb-c' : 'engine-thumb'} className="engine-thumb" transition={spring.snappy} />}
      <span className="engine-ico"><Icon size={16} /></span>
      <span className="engine-name">{title}{badge && <span className="engine-badge">{badge}</span>}</span>
      <span className="engine-tag">{tagline}</span>
      <span className="engine-status" data-tone={current ? (failing ? 'bad' : 'ok') : undefined}>
        {current ? <><i className="dot" data-on={!failing} data-tone={failing ? 'bad' : undefined} />{failing ? '正在使用 · 有问题' : '正在使用'}</> : status}
      </span>
    </button>
  );
}

const ABOUT: Record<EngineKind, ReactNode> = {
  volc: '火山引擎的大模型流式语音识别：边说边出字，松开后约半秒定稿，文字由点声直接输入到光标处。',
  dashscope: '阿里云百炼的实时语音识别（Fun-ASR / Paraformer）：边说边出字，文字由点声直接输入到光标处。',
  openless: '经 OpenLess 的「远程输入」识别：识别服务、润色风格和词典都沿用 OpenLess 自己的设置。',
  hotkey: '笔上的 ⑥ 替你按下其他听写软件的快捷键：它们自己收音、自己输入文字。只能输入到光标处，不能记笔记。',
};

function FormHeader({ kind, engine, current, title }: { kind: EngineKind; engine: EnginePrefs; current: boolean; title?: string }) {
  const secrets = useStore((s) => s.secrets);
  const info = useStore((s) => s.engine);
  const state = useStore((s) => s.state);
  const [busy, setBusy] = useState(false);
  const why = secrets || kind === 'openless' || kind === 'hotkey' ? missing(kind, engine, secrets) : '正在读取密钥状态…';
  const problem = current ? (info.error || (state.phase === 'error' ? state.message : '')) : '';
  async function use() {
    setBusy(true);
    await setEngine({ kind });
    setBusy(false);
  }
  return (
    <div className="engine-head">
      <div className="min-w-0 flex-1">
        <div className="title-md">{ENGINE_TITLE[kind]}</div>
        <p className="muted mt-1 text-[12.5px] leading-[1.65]">{ABOUT[kind]}</p>
        {problem && <p className="mt-1.5 flex items-start gap-1.5 text-[12.5px] leading-relaxed" style={{ color: 'var(--bad)' }}><AlertTriangle size={13} className="mt-[3px] shrink-0" />{problem}</p>}
      </div>
      <div className="flex shrink-0 flex-col items-end gap-1">
        {current ? (
          <span className="chip engine-using"><i className="dot" data-on="true" />正在使用</span>
        ) : (
          <button className="btn btn-primary btn-sm" disabled={!!why || busy} onClick={use}>{busy && <RefreshCw size={13} className="animate-spin" />}{switchText(title ?? ENGINE_TITLE[kind])}</button>
        )}
        {!current && why && <span className="faint text-[11.5px]">{why}</span>}
      </div>
    </div>
  );
}

function VolcForm({ engine }: { engine: EnginePrefs }) {
  const volc = engine.volc;
  const preset = VOLC_RESOURCES.some((r) => r.id === volc.resource_id) ? volc.resource_id : 'other';
  const [other, setOther] = useState(preset === 'other');
  const [advanced, setAdvanced] = useState(volc.endpoint !== 'bigmodel_async');
  return (
    <div className="engine-rows">
      <SettingRow title="鉴权方式" description="新版控制台用 API Key；旧版控制台用 App ID + Access Token。">
        <Segmented label="豆包鉴权方式" size="sm" value={volc.auth} onChange={(auth) => setEngine({ volc: { auth } })}
          options={[{ value: 'key', label: 'API Key' }, { value: 'legacy', label: 'App ID + Token' }]} />
      </SettingRow>
      {volc.auth === 'key' ? (
        <SettingRow title="API Key" description={<>在火山引擎「豆包语音」新版控制台创建，并开通<b>流式语音识别模型 2.0</b>。</>}>
          <SecretField name={SECRET.volcKey} label="豆包 API Key" />
        </SettingRow>
      ) : (
        <>
          <SettingRow title="App ID" description="旧版控制台应用管理里的 APP ID（纯数字）。">
            <TextField label="豆包 App ID" value={volc.app_id} placeholder="例如 1234567890" numeric
              onSave={async (app_id) => saved(await setEngine({ volc: { app_id } }))} />
          </SettingRow>
          <SettingRow title="Access Token" description="同一应用的 Access Token。">
            <SecretField name={SECRET.volcToken} label="豆包 Access Token" placeholder="粘贴 Access Token" />
          </SettingRow>
        </>
      )}
      <SettingRow title="资源" description={<>要和控制台开通的服务一致：小时版按时长计费，并发版按路数。<span className="mono-id">{volc.resource_id}</span>
        {(other || preset === 'other') && (
          <span className="mt-2 block">
            <TextField label="Resource ID" value={volc.resource_id} placeholder="volc.seedasr.sauc.duration" width="100%"
              onSave={async (resource_id) => saved(await setEngine({ volc: { resource_id } }))} />
          </span>
        )}</>}>
        <Segmented label="豆包资源" size="sm" value={other ? 'other' : preset}
          onChange={(value) => {
            if (value === 'other') { setOther(true); return; }
            setOther(false);
            setEngine({ volc: { resource_id: value } });
          }}
          options={[...VOLC_RESOURCES.map((r) => ({ value: r.id as string, label: r.label })), { value: 'other', label: '其他' }]} />
      </SettingRow>
      {/* Only a core with the engine.volc.hotwords setting shows the switch (an older one: always on). */}
      {typeof volc.hotwords === 'boolean' && (
        <SettingRow title="词典热词" description="把 OpenLess 词典里启用的词作为豆包热词（常用的在前，约 100 字以内），人名和术语更准。">
          <Switch label="词典热词" checked={volc.hotwords} onChange={(hotwords) => setEngine({ volc: { hotwords } })} />
        </SettingRow>
      )}
      <button className="engine-more" aria-expanded={advanced} onClick={() => setAdvanced(!advanced)}>
        <ChevronDown size={14} className="engine-more-ico" />高级
      </button>
      <AnimatePresence initial={false}>
        {advanced && (
          <motion.div key="adv" className="overflow-hidden" initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }} transition={spring.soft}>
            <SettingRow title="接口" description={VOLC_ENDPOINTS.find((e) => e.id === volc.endpoint)?.hint}>
              <Segmented label="豆包接口" size="sm" value={volc.endpoint} onChange={(endpoint) => setEngine({ volc: { endpoint } })}
                options={VOLC_ENDPOINTS.map((e) => ({ value: e.id, label: e.label }))} />
            </SettingRow>
          </motion.div>
        )}
      </AnimatePresence>
      <TestButton kind="volc">
        <span className="engine-help">
          <HelpLink url={HELP.volcConsole}>火山引擎控制台</HelpLink>
          <HelpLink url={HELP.volcDocs}>接口文档</HelpLink>
        </span>
      </TestButton>
    </div>
  );
}

function DashscopeForm({ engine }: { engine: EnginePrefs }) {
  const dash = engine.dashscope;
  const models = DASHSCOPE_MODELS.some((m) => m.id === dash.model)
    ? DASHSCOPE_MODELS.map((m) => ({ value: m.id as string, label: m.label }))
    : [...DASHSCOPE_MODELS.map((m) => ({ value: m.id as string, label: m.label })), { value: dash.model, label: dash.model }];
  return (
    <div className="engine-rows">
      <SettingRow title="API Key" description="在阿里云百炼控制台的「API Key」页面创建（需用主账号开通百炼服务）。">
        <SecretField name={SECRET.dashscope} label="百炼 API Key" />
      </SettingRow>
      <SettingRow title="模型" description="Fun-ASR 中文、方言和中英混说更准；Paraformer 更省。">
        <Segmented label="百炼模型" size="sm" value={dash.model} onChange={(model) => setEngine({ dashscope: { model } })} options={models} />
      </SettingRow>
      <SettingRow title="地域" description="北京与新加坡的 API Key 不通用：国际站账号选新加坡。">
        <Segmented label="百炼地域" size="sm" value={dash.region} onChange={(region) => setEngine({ dashscope: { region } })}
          options={[{ value: 'cn', label: '北京' }, { value: 'intl', label: '新加坡' }]} />
      </SettingRow>
      <TestButton kind="dashscope">
        <span className="engine-help">
          <HelpLink url={HELP.bailianKey}>如何获取 API Key</HelpLink>
          <HelpLink url={HELP.bailianConsole}>百炼控制台</HelpLink>
        </span>
      </TestButton>
    </div>
  );
}

const POLISH_TEXT: Record<PolishStyle, string> = {
  raw: '直接输入识别原文，不调用大模型，最快。',
  light: '去掉口头禅和重复，补全标点，不改原意。',
  custom: '按你的提示词处理，例如改成正式语气或翻译。',
};

function PolishForm({ engine }: { engine: EnginePrefs }) {
  const polish = engine.polish;
  const secrets = useStore((s) => s.secrets);
  const lacking = polishMissing(polish, secrets);
  const [prompt, setPrompt] = useState(polish.prompt);
  useEffect(() => setPrompt(polish.prompt), [polish.prompt]);
  return (
    <div className="engine-rows engine-polish">
      <div className="engine-sub"><Sparkles size={14} />润色<span className="faint">（可选，豆包和百炼共用）</span></div>
      <SettingRow title="润色方式" description={POLISH_TEXT[polish.style]}>
        <Segmented label="润色方式" size="sm" value={polish.style} onChange={(style) => setEngine({ polish: { style } })}
          options={[{ value: 'raw', label: '原文' }, { value: 'light', label: '轻度润色' }, { value: 'custom', label: '自定义' }]} />
      </SettingRow>
      <AnimatePresence initial={false}>
        {polish.style !== 'raw' && (
          <motion.div key="relay" className="overflow-hidden" initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }} transition={spring.soft}>
            {lacking.length > 0 && (
              <div className="setting-row block" data-polish-incomplete>
                <span className="test-result" data-tone="bad" role="status">
                  <AlertTriangle size={13} className="mt-[3px] shrink-0" />
                  还缺 {lacking.join('、')}：填完之前，语音照常可用，按识别原文输入。
                </span>
              </div>
            )}
            <SettingRow title="Base URL" description="任何 OpenAI 兼容接口：服务商或你自己的中转，通常以 /v1 结尾。">
              <TextField label="润色 Base URL" value={polish.base_url} placeholder="https://api.example.com/v1"
                onSave={async (base_url) => saved(await setEngine({ polish: { base_url } }))} />
            </SettingRow>
            <SettingRow title="模型" description="接口里的模型名称。">
              <TextField label="润色模型" value={polish.model} placeholder="例如 deepseek-chat"
                onSave={async (model) => saved(await setEngine({ polish: { model } }))} />
            </SettingRow>
            <SettingRow title="API Key" description="只用于这个接口。">
              <SecretField name={SECRET.llm} label="润色 API Key" />
            </SettingRow>
            {polish.style === 'custom' && (
              <div className="setting-row block">
                <div className="title-sm">提示词</div>
                <p className="muted mt-1 text-[12.5px]">识别原文作为用户消息发送；点声会再提醒模型只处理文字、不回答问题。</p>
                <textarea className="field mt-2.5 min-h-[96px] w-full" aria-label="自定义润色提示词" value={prompt} maxLength={2000}
                  placeholder="例如：把口语整理成书面语，保留原意，不要添加内容。"
                  onChange={(e) => setPrompt(e.target.value)}
                  onBlur={() => prompt.trim() !== polish.prompt && setEngine({ polish: { prompt } })} />
              </div>
            )}
            <TestButton kind="polish" />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function OpenLessPanel({ status, loading, onRefresh }: { status: OpenLessStatus | null; loading: boolean; onRefresh: () => void }) {
  const remote = status?.remote_input && status.paired;
  return (
    <div className="engine-rows">
      <div className="setting-row flex-wrap gap-2.5">
        {status ? (
          <>
            <StatusDot tone={status.installed ? 'ok' : 'off'}>{status.installed ? '已安装' : '未检测到 OpenLess'}</StatusDot>
            <StatusDot tone={status.running ? 'ok' : 'off'}>{status.running ? '正在运行' : '未运行'}</StatusDot>
            <StatusDot tone={remote ? 'ok' : status.remote_input === false ? 'bad' : 'off'}>
              {remote ? `远程输入已开启${status.port ? ` · 端口 ${status.port}` : ''}` : status.remote_input === false ? '远程输入已关闭' : '远程输入未配对'}
            </StatusDot>
          </>
        ) : <span className="faint text-[12.5px]">{loading ? '正在检查 OpenLess…' : '暂时无法检查 OpenLess，请点“刷新”重试'}</span>}
        <button className="btn btn-ghost btn-sm ml-auto" onClick={onRefresh} disabled={loading}>
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />刷新
        </button>
      </div>
      <div className="setting-row block">
        <div className="title-sm">怎样接入</div>
        <ol className="engine-steps">
          <li>打开 OpenLess → 设置，开启<b>远程输入</b>；识别服务和润色风格保持不变。</li>
          <li>保持 OpenLess 运行。点声自动读取本机的配对码，不会修改 OpenLess 的任何文件。</li>
          <li>是否显示实时文字取决于 OpenLess 的远程输入支持；没有实时文字时显示声线，说完后显示定稿。</li>
        </ol>
        <p className="faint mt-2.5 flex items-start gap-1.5 text-[12px] leading-relaxed">
          <ShieldCheck size={13} className="mt-[3px] shrink-0" />
          OpenLess 的远程输入会在局域网监听（有 6 位配对码保护）；点声只连接这台电脑的 127.0.0.1。不用时可以在 OpenLess 里关掉。
        </p>
      </div>
    </div>
  );
}

function HotkeyForm({ engine, detect, loading, failed, onRetry }: {
  engine: EnginePrefs; detect: HotkeyDetect | null; loading: boolean; failed: boolean; onRetry: () => void;
}) {
  const conf = engine.hotkey;
  const apps = detect?.apps ?? [];
  const [pendingCustom, setPendingCustom] = useState(false);
  const selected = pendingCustom ? 'custom' : conf.profile;
  const app = apps.find((a) => a.profile === selected) ?? null;
  // keys belong to the saved profile: a custom shortcut is only shown once custom is saved.
  const combo = selected === 'custom' ? (conf.profile === 'custom' ? conf.keys : '') : conf.keys || app?.combo || '';
  const style = selected === 'custom' ? conf.style : app?.style ?? 'toggle';
  async function pick(profile: string) {
    if (profile === 'custom' && !(conf.profile === 'custom' && conf.keys)) { setPendingCustom(true); return; }
    setPendingCustom(false);
    if (profile !== conf.profile) await setEngine({ hotkey: { profile, keys: profile === 'custom' ? conf.keys : '' } });
  }
  async function saveCombo(keys: string) {
    const patch = selected === 'custom' ? { profile: 'custom', keys, style: conf.style } : { keys };
    const ok = saved(await setEngine({ hotkey: patch }));
    if (ok) setPendingCustom(false);
    return ok;
  }
  return (
    <div className="engine-rows">
      <div className="setting-row block">
        <div className="title-sm">听写软件</div>
        <p className="muted mt-1 text-[12.5px]">检测到的排在前面。按 ⑥ 时点声替你按下它的快捷键，松开时再按一次（或松开）。</p>
        {!detect && loading ? (
          <p className="faint mt-3 flex items-center gap-2 text-[12.5px]"><RefreshCw size={13} className="animate-spin" />正在检测这台电脑上的听写软件…</p>
        ) : (
          <>
          {/* A failed detection still offers 自定义快捷键, and a retry. */}
          {failed && !detect && (
            <p className="faint mt-3 flex items-center gap-2 text-[12.5px]" data-detect-failed>检测失败，暂时列不出这台电脑上的听写软件。
              <button className="btn btn-ghost btn-sm" onClick={onRetry}><RefreshCw size={13} />重试</button></p>
          )}
          <div className="app-grid mt-3">
            {[...apps, null].map((item) => {
              const id = item?.profile ?? 'custom';
              const on = selected === id;
              return (
                <button key={id} className="app-tile" aria-pressed={on} data-profile={id} onClick={() => pick(id)}>
                  {on && <motion.span layoutId="app-thumb" className="app-thumb" transition={spring.snappy} />}
                  <span className="app-name">{item?.name ?? '自定义快捷键'}</span>
                  <span className="app-meta">
                    {item ? <AppState app={item} /> : <span className="faint">任何支持快捷键的软件</span>}
                  </span>
                </button>
              );
            })}
          </div>
          </>
        )}
      </div>
      {(app || selected === 'custom') && (
        <div className="well mx-0 my-3">
          <div className="hk-row">
            <span className="hk-label">快捷键</span>
            {/* Keyed by app: picking 自定义快捷键 remounts it straight into recording. */}
            <ComboEditor key={selected} combo={combo} custom={selected === 'custom' || !!conf.keys} startRecording={pendingCustom}
              onSave={saveCombo} onReset={selected !== 'custom' && conf.keys ? () => setEngine({ hotkey: { keys: '' } }) : undefined} />
          </div>
          <div className="hk-row">
            <span className="hk-label">方式</span>
            {selected === 'custom' ? (
              <Segmented label="触发方式" size="sm" value={conf.style} onChange={(value) => setEngine({ hotkey: { style: value } })}
                options={[{ value: 'toggle', label: '按一下开始 / 结束' }, { value: 'hold', label: '按住说话' }]} />
            ) : (
              <span className="text-[13px]">{STYLE_TEXT[style]}{app?.cancel ? <span className="faint"> · 取消时补按 {app.cancel}</span> : null}</span>
            )}
          </div>
          <div className="hk-row items-start" data-mic-hint>
            <span className="hk-label"><Mic size={13} />麦克风</span>
            {/* The core put the pen's real microphone name into the hint. */}
            <span className="text-[12.5px] leading-relaxed">{app?.mic_hint ?? detect?.custom_mic_hint ?? ''}</span>
          </div>
          <div className="hk-row flex-wrap">
            <span className="hk-label">注意</span>
            <span className="flex flex-wrap gap-1.5">
              {hotkeyWarnings(app, combo).map((w) => (
                <Tip key={w.id} text={w.tip}><span className="warn-chip" tabIndex={0} data-warn={w.id}><AlertTriangle size={11} />{w.label}</span></Tip>
              ))}
              {app && !app.verified && <Tip text="快捷键来自该软件的说明或报道，尚未在电脑上实测；不灵时请录入它当前显示的快捷键。"><span className="warn-chip" data-tone="info" tabIndex={0}>未经实测</span></Tip>}
            </span>
          </div>
          {app?.notes && <p className="faint px-4 pb-3 pt-1 text-[12px] leading-relaxed">{app.notes}</p>}
        </div>
      )}
    </div>
  );
}

function AppState({ app }: { app: HotkeyApp }) {
  if (app.supported === false) return <span style={{ color: 'var(--warn)' }}>版本过旧{app.version ? ` · ${app.version}` : ''}</span>;
  if (app.running) return <StatusDot tone="ok">运行中</StatusDot>;
  if (app.installed) return <StatusDot tone="off">{app.builtin ? '系统自带' : '已安装'}</StatusDot>;
  return <span className="faint">未检测到</span>;
}
