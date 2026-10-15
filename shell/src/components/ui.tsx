import { AnimatePresence, motion } from 'motion/react';
import type { CSSProperties, ReactElement, ReactNode } from 'react';
import { cloneElement, useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { batteryTone } from '../lib/devices';

/* Motion vocabulary: one snappy spring for thumbs and pills, one soft spring
   for surfaces, one quick ease for fades. */
export const spring = {
  snappy: { type: 'spring', stiffness: 560, damping: 40, mass: .8 },
  soft: { type: 'spring', stiffness: 340, damping: 32 },
  gentle: { type: 'spring', stiffness: 220, damping: 28 },
} as const;
export const ease = [0.32, 0.72, 0, 1] as const;

export function Glass({ className = '', children, as = 'section', style, ...rest }: {
  className?: string; children: ReactNode; as?: 'section' | 'article' | 'div'; style?: CSSProperties; id?: string;
  'aria-label'?: string;
}) {
  const Tag = as;
  return <Tag className={`glass ${className}`} style={style} {...rest}>{children}</Tag>;
}

export function Switch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (value: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <label className="switch" title={label}>
      <input type="checkbox" role="switch" aria-label={label} checked={checked} disabled={disabled}
        onChange={(e) => onChange(e.target.checked)} />
      <span />
    </label>
  );
}

export function Segmented<T extends string>({ value, options, onChange, label, disabled, size }: {
  value: T; options: { value: T; label: string; icon?: ReactNode }[]; onChange: (value: T) => void; label: string;
  disabled?: boolean; size?: 'sm';
}) {
  const id = useId();
  return (
    <div className={`segmented ${size === 'sm' ? 'segmented-sm' : ''}`} role="group" aria-label={label}>
      {options.map((option) => (
        <button key={option.value} aria-pressed={option.value === value} disabled={disabled}
          onClick={() => option.value !== value && onChange(option.value)}>
          {option.value === value && <motion.i layoutId={id + 'thumb'} className="thumb" transition={spring.snappy} />}
          <span>{option.icon}{option.label}</span>
        </button>
      ))}
    </div>
  );
}

export function SettingRow({ title, children, description, icon, badge }: {
  title: string; description?: ReactNode; children?: ReactNode; icon?: ReactNode; badge?: ReactNode;
}) {
  return (
    <div className="setting-row">
      {icon && <span className="setting-ico">{icon}</span>}
      <div className="min-w-0 flex-1">
        <div className="title-sm flex items-center gap-2">{title}{badge}</div>
        {description && <div className="muted mt-1 text-[12.5px] leading-[1.65]">{description}</div>}
      </div>
      {children && <div className="flex shrink-0 items-center gap-2">{children}</div>}
    </div>
  );
}

export function PageHeader({ eyebrow, title, children, meta }: { eyebrow: string; title: string; children?: ReactNode; meta?: ReactNode }) {
  return (
    <header className="page-header">
      <div className="min-w-0">
        <div className="eyebrow">{eyebrow}</div>
        <h1 className="title-lg mt-1 flex items-baseline gap-3">{title}{meta && <span className="faint text-[13px] font-medium tracking-normal">{meta}</span>}</h1>
      </div>
      {children && <div className="flex shrink-0 items-center gap-2">{children}</div>}
    </header>
  );
}

/** A section heading inside a page: small icon, title, optional hint on the right. */
export function SectionTitle({ icon, title, hint, children }: { icon?: ReactNode; title: string; hint?: ReactNode; children?: ReactNode }) {
  return (
    <div className="section-title">
      <h2>{icon}{title}</h2>
      {hint && <span className="faint min-w-0 truncate text-[12px]">{hint}</span>}
      {children && <span className="ml-auto flex items-center gap-2">{children}</span>}
    </div>
  );
}

/** Tooltip in a portal, so rounded panels with overflow clipping never cut it. */
export function Tip({ text, children, side = 'top' }: { text: ReactNode; children: ReactElement<any>; side?: 'top' | 'bottom' }) {
  const [box, setBox] = useState<DOMRect | null>(null);
  const timer = useRef(0);
  const id = useId();
  const show = (e: { currentTarget: Element }) => {
    const target = e.currentTarget;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setBox(target.getBoundingClientRect()), 120);
  };
  const hide = () => { window.clearTimeout(timer.current); setBox(null); };
  useEffect(() => () => window.clearTimeout(timer.current), []);
  const props = children.props as Record<string, any>;
  const trigger = cloneElement(children, {
    'aria-describedby': box ? id : undefined,
    onMouseEnter: (e: any) => { props.onMouseEnter?.(e); show(e); },
    onMouseLeave: (e: any) => { props.onMouseLeave?.(e); hide(); },
    onFocus: (e: any) => { props.onFocus?.(e); show(e); },
    onBlur: (e: any) => { props.onBlur?.(e); hide(); },
  });
  const x = box ? Math.min(window.innerWidth - 140, Math.max(140, box.left + box.width / 2)) : 0;
  return (
    <>
      {trigger}
      {createPortal(
        <AnimatePresence>
          {box && (
            <div className="tip-anchor" style={{ left: x, top: side === 'top' ? box.top - 8 : box.bottom + 8, transform: `translate(-50%, ${side === 'top' ? '-100%' : '0'})` }}>
              <motion.div id={id} role="tooltip" className="tip"
                initial={{ opacity: 0, y: side === 'top' ? 4 : -4, scale: .97 }} animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, transition: { duration: .1 } }} transition={spring.snappy}>
                {text}
              </motion.div>
            </div>
          )}
        </AnimatePresence>,
        document.body,
      )}
    </>
  );
}

/** Small battery glyph + percent. `null` shows a dash (honest "not reported"). */
export function Battery({ percent, showText = true, stale = false }: { percent: number | null; showText?: boolean; stale?: boolean }) {
  const tone = batteryTone(percent);
  return (
    <span className="battery-wrap" data-tone={tone} data-stale={stale || undefined}>
      <span className="battery" aria-hidden="true"><i style={{ '--level': (percent ?? 0) / 100 } as CSSProperties} /></span>
      {showText && <span className="num">{percent == null ? '—' : `${percent}%`}</span>}
    </span>
  );
}

/** Styled range: the filled part of the track follows the value. */
export function Range({ value, min, max, step = 1, onChange, label, disabled, className = '' }: {
  value: number; min: number; max: number; step?: number; onChange: (value: number) => void; label: string; disabled?: boolean; className?: string;
}) {
  const p = ((value - min) / (max - min)) * 100;
  return (
    <input type="range" className={`range ${className}`} min={min} max={max} step={step} value={value} aria-label={label}
      disabled={disabled} style={{ '--p': `${p}%` } as CSSProperties} onChange={(e) => onChange(Number(e.target.value))} />
  );
}

export function Empty({ icon, title, children, className = '' }: { icon: ReactNode; title: string; children?: ReactNode; className?: string }) {
  return (
    <div className={`empty ${className}`}>
      <span className="empty-ico">{icon}</span>
      <p className="title-sm mt-3">{title}</p>
      {children && <div className="faint mx-auto mt-1 max-w-[300px] text-[12.5px] leading-relaxed">{children}</div>}
    </div>
  );
}

/* Small model glyphs for lists (the large illustrations live in DeviceArt). */
export function PenGlyph({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true">
      <g transform="rotate(38 12 12)">
        <rect x="8.6" y="1.8" width="6.8" height="20.4" rx="3.4" />
        <circle cx="12" cy="7.4" r="1.7" />
        <circle cx="12" cy="12.3" r=".9" fill="currentColor" stroke="none" />
        <path d="M10.9 16.6h2.2" />
      </g>
    </svg>
  );
}

export function TidepoolGlyph({ size = 18 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" aria-hidden="true">
      <circle cx="12" cy="12" r="8.6" />
      <path d="M7.4 12.4c1.1-2.4 2.2-2.4 3.1 0s2 2.4 3.1 0 2-2.4 3 0" />
    </svg>
  );
}

export function useMedia(query: string) {
  const [match, setMatch] = useState(() => typeof window !== 'undefined' && window.matchMedia(query).matches);
  useEffect(() => {
    const list = window.matchMedia(query);
    const update = () => setMatch(list.matches);
    update();
    list.addEventListener('change', update);
    return () => list.removeEventListener('change', update);
  }, [query]);
  return match;
}

/** SVG lens used by `.liquid` (backdrop refraction, Chromium only; blur fallback elsewhere). */
export function LiquidDefs() {
  return (
    <svg width="0" height="0" style={{ position: 'absolute' }} aria-hidden="true">
      <filter id="liquid-lens" x="0" y="0" width="100%" height="100%" colorInterpolationFilters="sRGB">
        <feTurbulence type="fractalNoise" baseFrequency="0.012 0.02" numOctaves="2" seed="3" result="noise" />
        <feGaussianBlur in="noise" stdDeviation="2.5" result="soft" />
        <feDisplacementMap in="SourceGraphic" in2="soft" scale="22" xChannelSelector="R" yChannelSelector="G" />
      </filter>
    </svg>
  );
}
