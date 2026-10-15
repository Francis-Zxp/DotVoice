import { AnimatePresence, motion } from 'motion/react';
import { Check, Info, Keyboard, Moon, Pause, Pencil, Play, Radio, Sun, Usb, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { BaseusArt, Pv60Art, ReceiverArt, TidepoolArt } from '../components/DeviceArt';
import { useRoster } from '../components/Chrome';
import { Battery, Glass, PageHeader, SectionTitle, Segmented, spring, Tip } from '../components/ui';
import { batteryTone, cleanLabel, MODEL_NAME, TIDEPOOL_BATTERY_NOTE, type RosterItem } from '../lib/devices';
import { ago, when } from '../lib/format';
import { act, useStore } from '../lib/store';
import type { DeviceId, PenInstance, Snapshot } from '../lib/types';

const ART: Record<DeviceId, typeof BaseusArt> = { baseus: BaseusArt, pv60: Pv60Art };
const ABOUT: Record<DeviceId, string> = {
  baseus: '原生 USB 麦克风 · 长按 / 短按都能录音 · 翻译键可改作模式切换',
  pv60: '原生 Ai Pointer 麦克风 · 专用 HID 握手 · 按住说话或单击切换',
};

export function DevicesPage({ onSettings }: { onSettings: () => void }) {
  const instances = useStore((s) => s.instances);
  const pens = useRoster(true).filter((i) => i.kind === 'pen');
  const present = pens.filter((p) => p.present).length;
  return (
    <div>
      <PageHeader eyebrow="熟悉的设备，一处相遇" title="设备" meta={present ? `${present} 支笔在线` : undefined}>
        <button className="btn btn-sm" onClick={onSettings}><Keyboard size={14} />按键设置</button>
      </PageHeader>

      <SectionTitle icon={<Radio size={15} />} title="我的笔" hint="每个接收器对应一支笔，可同时插入多个" />
      {instances.length ? (
        <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))' }}>
          <AnimatePresence initial={false}>
            {instances.map((pen, i) => <PenCard key={pen.id} pen={pen} item={pens.find((p) => p.instanceId === pen.id)} index={i} />)}
          </AnimatePresence>
        </div>
      ) : (
        <Glass className="flex items-center gap-5 px-6 py-5">
          <div className="relative z-[1] w-[180px] shrink-0 opacity-80">
            <ReceiverArt className="h-[110px] w-full" />
          </div>
          <div className="relative z-[1] min-w-0 flex-1">
            <span className="empty-ico" style={{ width: 40, height: 40, borderRadius: 13 }}><Usb size={18} /></span>
            <p className="title-sm mt-3">插入接收器后，每支笔会单独出现在这里</p>
            <p className="faint mt-1 max-w-[440px] text-[12.5px] leading-relaxed">
              同时插入多个接收器时，每支笔分别显示电量、分别命名；任何一支按 ⑥ 都能说话，正在说话的那支会标出“正在录音”。
            </p>
          </div>
        </Glass>
      )}
      {instances.length > 0 && (
        <p className="faint mt-3 px-1 text-[12.5px]">任何一支笔按 ⑥ 都能说话；正在说话的那支会标出“正在录音”。名称只保存在这台电脑上，点名称即可修改。</p>
      )}

      <div className="mt-8"><SectionTitle icon={<Keyboard size={15} />} title="型号与录音方式" hint="按键协议按型号区分，两种型号分别统计续航" /></div>
      <ModelCards />

      <div className="mt-8"><SectionTitle icon={<Radio size={15} />} title="Tidepool" hint="自带接收器的圆形语音终端" /></div>
      <TidepoolCard />
    </div>
  );
}

function StateChip({ item }: { item?: RosterItem }) {
  if (!item) return null;
  return (
    <span className="chip state-chip" data-state={item.state}>
      <i className={`dot ${item.state === 'recording' ? 'live' : ''}`} data-on={item.state === 'ready' || item.state === 'connected'} />
      {item.status}
    </span>
  );
}

function PenCard({ pen, item, index }: { pen: PenInstance; item?: RosterItem; index: number }) {
  const rename = useStore((s) => s.renameInstance);
  const notify = useStore((s) => s.notify);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(pen.label);
  const input = useRef<HTMLInputElement>(null);
  const Pen = ART[pen.model] ?? BaseusArt;
  const battery = pen.battery?.percent ?? null;
  useEffect(() => { if (!editing) setDraft(pen.label); }, [pen.label, editing]);
  useEffect(() => { if (editing) input.current?.select(); }, [editing]);

  async function save() {
    const label = cleanLabel(draft);
    setEditing(false);
    if (!label || label === pen.label) { setDraft(pen.label); return; }
    if (await rename(pen.id, label)) notify(`已改名为“${label}”`, 'success');
  }

  return (
    <motion.article layout data-instance={pen.id} className={`glass lift pen-card ${pen.present ? '' : 'is-away'}`}
      initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, scale: .96 }} transition={{ ...spring.soft, delay: index * .05 }}>
      <div className="relative z-[1] flex items-center justify-between gap-2">
        <StateChip item={item} />
        <span className="faint num text-[11px]" title={pen.id}>{pen.id.split(':')[1]?.slice(0, 6).toUpperCase()}</span>
      </div>
      <div className="pen-art"><Pen className="h-full w-full" /></div>
      <div className="relative z-[1] min-h-[34px]">
        {editing ? (
          <form className="flex items-center gap-1.5" onSubmit={(e) => { e.preventDefault(); save(); }}>
            <input ref={input} className="field h-8 flex-1 text-[15px] font-semibold" value={draft} maxLength={24} aria-label="笔的名称"
              onChange={(e) => setDraft(e.target.value)} onBlur={save}
              onKeyDown={(e) => { if (e.key === 'Escape') { setDraft(pen.label); setEditing(false); } }} />
            <button type="submit" className="btn btn-sm btn-icon btn-primary" aria-label="保存名称" onMouseDown={(e) => e.preventDefault()}><Check size={14} /></button>
            <button type="button" className="btn btn-sm btn-icon btn-ghost" aria-label="取消改名" onMouseDown={(e) => e.preventDefault()}
              onClick={() => { setDraft(pen.label); setEditing(false); }}><X size={14} /></button>
          </form>
        ) : (
          <button className="rename" onClick={() => setEditing(true)} title="点击改名" aria-label={`重命名 ${pen.label}`}>
            <span className="truncate">{pen.label}</span><Pencil size={13} />
          </button>
        )}
        <div className="faint mt-0.5 text-[12px]">{MODEL_NAME[pen.model]}</div>
      </div>
      <dl className="relative z-[1] mt-3 grid grid-cols-2 gap-2">
        <div className="glass-flat px-3 py-2.5">
          <dt className="faint text-[11.5px]">电量</dt>
          <dd className="m-0 mt-1 flex items-center justify-between gap-2">
            <span className="num text-[18px] font-semibold leading-none" style={{ color: batteryTone(battery) === 'low' ? 'var(--bad)' : undefined }}>{battery == null ? '—' : `${battery}%`}</span>
            <Battery percent={battery} showText={false} stale={!pen.present} />
          </dd>
          <div className="faint mt-1 truncate text-[11px]">{pen.battery ? `${ago(pen.battery.reported_at)}上报` : '按任意键刷新'}</div>
        </div>
        <div className="glass-flat px-3 py-2.5">
          <dt className="faint text-[11.5px]">上次使用</dt>
          <dd className="m-0 mt-1 text-[15px] font-semibold leading-[18px]">{pen.active ? '现在' : pen.last_used ? ago(pen.last_used) : '还没用过'}</dd>
          <div className="faint num mt-1 truncate text-[11px]">{pen.last_used && !pen.active ? when(pen.last_used) : pen.present ? '已连接' : '接收器未插入'}</div>
        </div>
      </dl>
    </motion.article>
  );
}

function ModelCards() {
  const devices = useStore((s) => s.devices);
  const selected = useStore((s) => s.selected);
  const mode = useStore((s) => s.deviceMode);
  const phase = useStore((s) => s.state.phase);
  const notify = useStore((s) => s.notify);
  const paused = phase === 'disconnected';

  async function choose(device: DeviceId, nextMode = mode) {
    const snapshot = await act<Snapshot>('select_device', { device, mode: nextMode });
    if (snapshot) {
      useStore.getState().apply(snapshot);
      notify('已切换设备。回到语音页手动启用。', 'success');
    }
  }

  return (
    <>
      <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))' }}>
        {devices.map((device) => {
          const Pen = ART[device.id];
          const active = device.id === selected;
          return (
            <Glass key={device.id} as="article" className="lift flex items-center gap-4 py-4 pl-3 pr-5">
              <div className="relative z-[1] h-[104px] w-[150px] shrink-0"><Pen className="h-full w-full" /></div>
              <div className="relative z-[1] min-w-0 flex-1">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="chip" style={{ height: 22 }}><i className="dot" data-on={device.present} />{device.present ? '接收器已连接' : '未检测到'}</span>
                  {active && <span className="chip" style={{ height: 22, color: 'var(--brand-text)', background: 'var(--brand-soft)' }}>当前型号</span>}
                </div>
                <h3 className="title-md mt-2 text-[16px]">{device.name}</h3>
                <p className="muted mt-0.5 text-[12.5px] leading-relaxed">{ABOUT[device.id]}</p>
                <p className="faint mt-0.5 text-[11.5px]">{device.validation}</p>
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  <button className={`btn btn-sm ${active ? '' : 'btn-primary'}`} disabled={active || !paused} onClick={() => choose(device.id)}>
                    {active ? <><Check size={14} />已选择</> : '使用这个型号'}
                  </button>
                  {active && device.id === 'pv60' && (
                    <Segmented size="sm" label="PV60 录音方式" value={mode} disabled={!paused} onChange={(m) => choose('pv60', m)}
                      options={[{ value: 'hold', label: '按住说话' }, { value: 'toggle', label: '单击切换' }]} />
                  )}
                </div>
              </div>
            </Glass>
          );
        })}
      </div>
      <p className="faint mt-3 px-1 text-[12.5px]">{paused ? '切换型号不会影响已保存的笔记和续航记录。' : '切换型号前请先在语音页暂停。'}</p>
    </>
  );
}

function TidepoolCard() {
  const tp = useStore((s) => s.tidepool);
  const notify = useStore((s) => s.notify);
  async function run(action: string, message: string) {
    const answer = await act<{ status: any }>('tidepool_action', { action });
    if (answer?.status) {
      useStore.setState({ tidepool: answer.status });
      notify(message, 'success');
    }
  }
  const online = !!(tp.device_online || tp.usb_connected);
  const metrics: [string, React.ReactNode][] = [
    ['固件', tp.firmware_version ? `v${tp.firmware_version}` : online ? '未上报（需固件 4.0.8）' : '—'],
    ['接收器', tp.receiver_running ? `运行中${tp.receiver_version ? ' · v' + tp.receiver_version : ''}` : tp.receiver_running === false ? '未运行' : '尚未核实'],
    ['设备链路', tp.usb_connected ? 'USB 已连接' : tp.device_online === true ? (tp.link && tp.link !== 'unknown' ? `${tp.link}在线` : '无线在线') : tp.device_online === false ? '未连接' : '尚未核实'],
    ['语音', tp.recording ? '录音中' : tp.enabled === false ? '已暂停' : tp.enabled === true ? '等待操作' : '尚未核实'],
    ['电量', tp.battery_percent == null
      ? <Tip text={TIDEPOOL_BATTERY_NOTE}><span className="inline-flex cursor-help items-center gap-1" tabIndex={0} data-battery-na aria-label={`未上报：${TIDEPOOL_BATTERY_NOTE}`}>未上报<Info size={13} className="faint" /></span></Tip>
      : <span className="inline-flex items-center gap-2"><Battery percent={tp.battery_percent} /><span className="faint text-[12px]">{tp.battery_charging ? '充电中 · ' : ''}{tp.battery_reported_at ? ago(tp.battery_reported_at) + '更新' : ''}</span></span>],
  ];
  const disabled = !tp.control_supported;
  return (
    <Glass className="grid gap-5 px-6 py-5" as="article">
      <div className="relative z-[1] grid items-center gap-5" style={{ gridTemplateColumns: '128px minmax(0,1fr) auto' }}>
        <TidepoolArt className="h-[128px] w-[128px]" />
        <div className="min-w-0">
          <div className="eyebrow">你的另一种表达方式 · ESP32-S3</div>
          <h2 className="title-lg mt-1 text-[22px]">Tidepool OS</h2>
          <p className="muted mt-1 text-[12.5px] leading-relaxed">沿用自己的接收器把语音交给 OpenLess；模式可用全局快捷键切换。开启“随拿随用”后与倍思同时待命，拿起哪个用哪个。</p>
          <p className="faint mt-1 text-[12px]">{tp.message || '正在读取本机状态，不会启动录音。'}</p>
        </div>
        <span className="chip self-start"><i className="dot" data-on={online} />{tp.control_supported ? '统一控制已接入' : tp.installed ? '等待启用统一控制' : '未发现接收器'}</span>
      </div>
      <dl className="relative z-[1] grid gap-2" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))' }}>
        {metrics.map(([label, value]) => (
          <div key={label} className="glass-flat px-4 py-3"><dt className="faint text-[11.5px]">{label}</dt><dd className="m-0 mt-0.5 text-[13.5px] font-semibold">{value}</dd></div>
        ))}
      </dl>
      <div className="relative z-[1] flex flex-wrap items-center gap-2">
        <button className="btn btn-primary btn-sm" disabled={disabled} onClick={() => run('resume', '已交给 Tidepool，翻页笔已暂停。')}><Play size={13} />使用 Tidepool</button>
        <button className="btn btn-sm" disabled={disabled} onClick={() => run('pause', 'Tidepool 接收已暂停。')}><Pause size={13} />暂停接收</button>
        <button className="btn btn-ghost btn-sm" disabled={disabled || !tp.recording} onClick={() => run('cancel', '已取消本段。')}><X size={13} />取消本段</button>
        <span className="flex-1" />
        <button className="btn btn-ghost btn-sm" disabled={disabled || !tp.usb_connected} title={tp.usb_connected ? undefined : '需要 USB 连接'} onClick={() => run('wake', '唤醒指令已交给 USB 接收器，请核对屏幕。')}><Sun size={13} />唤醒屏幕</button>
        <button className="btn btn-ghost btn-sm" disabled={disabled || !tp.usb_connected} title={tp.usb_connected ? undefined : '需要 USB 连接'} onClick={() => run('sleep', '休眠指令已交给 USB 接收器，请核对屏幕。')}><Moon size={13} />屏幕休眠</button>
      </div>
    </Glass>
  );
}
