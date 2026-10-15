import { motion } from 'motion/react';
import { BatteryCharging, Gauge, History } from 'lucide-react';
import { useId, useState } from 'react';
import { Battery, Glass, PageHeader, SectionTitle, spring } from '../components/ui';
import { batteryTone } from '../lib/devices';
import { ago, clock, duration } from '../lib/format';
import { act, useStore } from '../lib/store';
import type { Usage } from '../lib/types';

const TONE_COLOR = { none: 'var(--text-3)', low: 'var(--bad)', mid: 'var(--warn)', ok: 'var(--ok)' } as const;

function Ring({ percent, stale }: { percent: number | null; stale: boolean }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, '');
  const radius = 72, circumference = 2 * Math.PI * radius;
  const value = percent ?? 0;
  const color = TONE_COLOR[batteryTone(percent)];
  return (
    <svg viewBox="0 0 180 180" className="h-[176px] w-[176px] shrink-0" role="img" aria-label={percent == null ? '等待电量' : `设备上报 ${percent}%`}>
      <defs>
        <linearGradient id={id + 'ring'} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor={color} stopOpacity=".5" />
          <stop offset="1" stopColor={color} />
        </linearGradient>
        <filter id={id + 'glow'} x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="6" /></filter>
      </defs>
      <circle cx="90" cy="90" r={radius} fill="none" stroke="var(--chip)" strokeWidth="12" />
      {!stale && percent != null && (
        <motion.circle cx="90" cy="90" r={radius} fill="none" stroke={color} strokeWidth="12" strokeLinecap="round" opacity=".28" filter={`url(#${id}glow)`}
          transform="rotate(-90 90 90)" strokeDasharray={circumference} initial={{ strokeDashoffset: circumference }}
          animate={{ strokeDashoffset: circumference * (1 - value / 100) }} transition={{ type: 'spring', stiffness: 50, damping: 18 }} />
      )}
      <motion.circle cx="90" cy="90" r={radius} fill="none" stroke={`url(#${id}ring)`} strokeWidth="12" strokeLinecap="round"
        transform="rotate(-90 90 90)" strokeDasharray={circumference} initial={{ strokeDashoffset: circumference }}
        animate={{ strokeDashoffset: circumference * (1 - value / 100) }} transition={{ type: 'spring', stiffness: 50, damping: 18 }}
        opacity={stale ? 0.5 : 1} />
      <text x="90" y="96" textAnchor="middle" className="num" fontSize="42" fontWeight="650" letterSpacing="-1" fill="var(--text)">{percent == null ? '—' : percent}</text>
      <text x="90" y="120" textAnchor="middle" fontSize="12" fill="var(--text-3)">{percent == null ? '等待上报' : '% · 设备上报'}</text>
    </svg>
  );
}

function Spark({ samples }: { samples: Usage['battery_samples'] }) {
  const id = useId().replace(/[^a-zA-Z0-9]/g, '');
  if (samples.length < 2) return <div className="faint text-[12px]">电量变化后会出现曲线</div>;
  const w = 260, h = 56;
  const t0 = samples[0].at, t1 = samples[samples.length - 1].at || t0 + 1;
  const points = samples.map((s) => [((s.at - t0) / Math.max(1, t1 - t0)) * w, h - (s.percent / 100) * (h - 8) - 4]);
  const d = points.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join(' ');
  const [lx, ly] = points[points.length - 1];
  return (
    <svg viewBox={`0 0 ${w} ${h}`} className="h-14 w-full overflow-visible" aria-label="近期电量变化">
      <defs>
        <linearGradient id={id + 'fill'} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--ok)" stopOpacity=".22" />
          <stop offset="1" stopColor="var(--ok)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <path d={`${d} L${w} ${h} L0 ${h}Z`} fill={`url(#${id}fill)`} />
      <motion.path d={d} fill="none" stroke="var(--ok)" strokeWidth="2" strokeLinejoin="round" strokeLinecap="round"
        initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: .9, ease: [0.32, 0.72, 0, 1] }} />
      <circle cx={lx} cy={ly} r="3.5" fill="var(--ok)" />
      <circle cx={lx} cy={ly} r="7" fill="var(--ok)" opacity=".18" />
    </svg>
  );
}

/* Each pen reports its own battery; this strip shows all of them at once. */
function PenBatteries() {
  const instances = useStore((s) => s.instances);
  if (instances.length < 1) return null;
  return (
    <>
      <SectionTitle icon={<BatteryCharging size={15} />} title="每支笔的电量" hint="随按键报告送达，不额外耗电" />
      <div className="mb-6 grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(190px, 1fr))' }}>
        {instances.map((pen, i) => {
          const p = pen.battery?.percent ?? null;
          return (
            <motion.div key={pen.id} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ ...spring.soft, delay: i * .05 }}>
              <Glass className="px-4 py-3" style={{ opacity: pen.present ? 1 : .6 }}>
                <div className="relative z-[1] flex items-center justify-between gap-2">
                  <span className="truncate text-[13px] font-semibold">{pen.label}</span>
                  <Battery percent={p} showText={false} stale={!pen.present} />
                </div>
                <div className="relative z-[1] mt-1.5 flex items-baseline justify-between gap-2">
                  <span className="num text-[22px] font-semibold leading-none" style={{ color: batteryTone(p) === 'low' ? 'var(--bad)' : undefined }}>{p == null ? '—' : p}<span className="faint ml-0.5 text-[12px]">{p == null ? '' : '%'}</span></span>
                  <span className="faint text-[11px]">{!pen.present ? '未连接' : pen.battery ? `${ago(pen.battery.reported_at)}` : '等待上报'}</span>
                </div>
                <div className="relative z-[1] mt-2 h-1 overflow-hidden rounded-full" style={{ background: 'var(--chip)' }}>
                  <motion.i className="block h-full rounded-full" style={{ background: TONE_COLOR[batteryTone(p)] }}
                    initial={{ width: 0 }} animate={{ width: `${p ?? 0}%` }} transition={{ ...spring.gentle, delay: .1 + i * .05 }} />
                </div>
              </Glass>
            </motion.div>
          );
        })}
      </div>
    </>
  );
}

export function BatteryPage() {
  const usage = useStore((s) => s.usage);
  const selected = useStore((s) => s.selected);
  const devices = useStore((s) => s.devices);
  const [confirm, setConfirm] = useState<'full' | 'low' | null>(null);
  if (!usage) return null;
  const device = devices.find((d) => d.id === selected);
  const battery = selected === 'baseus' ? usage.battery : null;
  const age = battery ? usage.now - battery.reported_at : null;
  const active = usage.active;
  const t = active ?? usage.totals;
  const recent = usage.cycles.slice(-5).reverse();
  const longest = Math.max(1, ...recent.map((c) => c.ended - c.started));

  async function mark(kind: 'full' | 'low') {
    const data = await act<Usage>('mark', { kind });
    if (data) useStore.setState({ usage: data });
    setConfirm(null);
  }

  return (
    <div>
      <PageHeader eyebrow={device?.name ?? ''} title="续航" />
      <PenBatteries />
      <SectionTitle icon={<Gauge size={15} />} title="本轮续航" hint="最近一次上报与满电以来的使用" />
      <div className="grid gap-4" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(400px, 1fr))' }}>
        <Glass className="flex items-center gap-6 px-6 py-5">
          <div className="relative z-[1]"><Ring percent={battery?.percent ?? null} stale={age == null || age > 1800 || !device?.present} /></div>
          <div className="relative z-[1] min-w-0">
            <div className="title-md">{battery ? `${ago(battery.reported_at, usage.now)}上报` : selected === 'pv60' ? 'PV60 电量协议待验证' : '尚未收到电量'}</div>
            <p className="muted mt-1 text-[12.5px] leading-relaxed">
              {battery ? `最后一次 ${clock(battery.reported_at)}。` : ''}电量随笔的每个按键报告一起送达，点声只读取、从不向笔发送查询，所以<strong>不额外耗电</strong>。长时间不按键时显示上次的数值，按任意键即可刷新。
            </p>
            <p className="faint mt-2 text-[11.5px] leading-relaxed">
              精度取决于笔的固件（原厂软件只分高 / 中 / 低三档）。静置后的小幅回弹会被平滑，显示 15 分钟内的较低值
              {battery?.raw != null && battery.raw !== battery.percent ? `（最近一次原始上报 ${battery.raw}%）` : ''}。
            </p>
            <div className="mt-3"><Spark samples={usage.battery_samples} /></div>
          </div>
        </Glass>
        <Glass className="px-6 py-5">
          <div className="relative z-[1] flex items-center justify-between gap-3">
            <div className="title-md">本轮使用记录</div>
            <span className="faint truncate text-[12px]">{active ? `始于 ${new Date(active.started * 1000).toLocaleString('zh-CN')}` : '收到满电报告后自动开始'}</span>
          </div>
          <div className="relative z-[1] mt-4 grid grid-cols-3 gap-2">
            {[
              ['满电后经过', active ? duration(usage.now - active.started) : '—', '从首次观察到 100%'],
              ['说话时长', duration(t.speech_seconds ?? 0), '本地语音检测 · 估计'],
              ['累计录音', duration(t.audio_seconds ?? 0), `${t.sessions ?? 0} 次完成转写`],
            ].map(([label, value, hint]) => (
              <div key={label} className="glass-flat min-w-0 px-3.5 py-3">
                <div className="faint text-[11.5px]">{label}</div>
                <div className="num mt-0.5 truncate text-[17px] font-semibold">{value}</div>
                <div className="faint truncate text-[11px]">{hint}</div>
              </div>
            ))}
          </div>
          <div className="relative z-[1] mt-5 flex items-center justify-between">
            <div className="title-md flex items-center gap-2"><History size={15} className="faint" />续航的真实轮廓</div>
            <span className="faint text-[12px]">最近 5 个周期</span>
          </div>
          <div className="relative z-[1] mt-3 space-y-2.5">
            {recent.length === 0 && <p className="faint text-[13px]">{active ? '第一轮正在积累，降到 10% 时完成观察。' : '等待一次满电上报，开始记录这一轮。'}</p>}
            {recent.map((c, i) => (
              <div key={c.started} className="grid items-center gap-3 text-[12.5px]" style={{ gridTemplateColumns: 'minmax(96px, 120px) 1fr auto' }}>
                <span className="faint truncate">{new Date(c.started * 1000).toLocaleDateString('zh-CN')} {c.complete ? (c.endpoint === '10_percent' ? '至 10%' : '手动') : '未完成'}</span>
                <div className="h-2 overflow-hidden rounded-full" style={{ background: 'var(--chip)' }}>
                  <motion.i className="block h-full rounded-full" style={{ background: 'linear-gradient(90deg, var(--ok), color-mix(in srgb, var(--ok) 45%, white))', opacity: c.complete ? 1 : 0.5 }}
                    initial={{ width: 0 }} animate={{ width: `${Math.max(2, ((c.ended - c.started) / longest) * 100)}%` }} transition={{ ...spring.gentle, delay: .1 + i * .06 }} />
                </div>
                <span className="num text-right">{((c.ended - c.started) / 86400).toFixed(1)} 天 · {c.sessions} 次</span>
              </div>
            ))}
          </div>
          {selected !== 'baseus' && (
            <div className="relative z-[1] mt-5 flex flex-wrap gap-2">
              <button className="btn btn-primary btn-sm" onClick={() => setConfirm('full')}>我已充满电</button>
              <button className="btn btn-sm" disabled={!active} onClick={() => setConfirm('low')}>记录低电量</button>
              {confirm && (
                <span className="flex items-center gap-2 text-[12.5px]">
                  <span className="muted">{confirm === 'full' ? '确认这支笔已经充满？' : '确认本轮到此结束？'}</span>
                  <button className="btn btn-sm" onClick={() => mark(confirm)}>确认</button>
                  <button className="btn btn-ghost btn-sm" onClick={() => setConfirm(null)}>取消</button>
                </span>
              )}
            </div>
          )}
          <p className="faint relative z-[1] mt-4 text-[11.5px] leading-relaxed">飞鼠、激光与软件关闭时的耗电无法分别计量，历史跨度不等于剩余时间。电量降到 20% 和 10% 时会各提醒一次。</p>
        </Glass>
      </div>
    </div>
  );
}
