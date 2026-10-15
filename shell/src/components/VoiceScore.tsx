/* "应声"-style score: a thin voice line above every line of text.
   - idle:      a calm prompt, still lines (decorative, no volume implied)
   - listening: live words; the newest line's wave follows the measured volume,
                finished lines keep the shape they had; a dot rides the line end
   - thinking:  waves settle; times, numbers and key words get a bracket + label
                (brackets only here, so they never crowd the answer's waves)
   - answer:    the final text is written line by line, waves drawn from the
                recording's real volume history
   Independent implementation; volume only, never pitch or word timing. */
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { levels } from '../lib/store';
import { keywords, type Mark } from '../lib/transcript';

export type ScoreStage = 'idle' | 'listening' | 'thinking' | 'answer';

interface Props {
  text: string;
  stage: ScoreStage;
  envelope?: number[] | null;
  size?: number;
  onColor?: boolean;
  reduced?: boolean;
  dim?: number; // idle: lines after this index are shown faint
  className?: string;
}

interface Line { left: number; right: number; top: number; first: number; last: number }

const FRAME = 1000 / 30;

function smooth(values: number[], nodes: number) {
  const reference = Math.max(.02, [...values].sort((a, b) => a - b)[Math.floor(values.length * .9)] || .02);
  return Array.from({ length: nodes + 1 }, (_, i) => {
    if (!values.length) return 0;
    const center = (i / nodes) * (values.length - 1);
    const radius = Math.max(1, values.length / nodes / 2);
    let sum = 0, weights = 0;
    for (let j = Math.max(0, Math.floor(center - radius)); j <= Math.min(values.length - 1, Math.ceil(center + radius)); j++) {
      const w = Math.max(0, 1 - Math.abs(j - center) / (radius + 1));
      sum += Math.sqrt(Math.min(1, Math.max(0, values[j] - .0015) / reference)) * w;
      weights += w;
    }
    return weights ? sum / weights : 0;
  });
}

/* One clean stroke of constant width: the voice shows as height, not weight. */
function ribbon(ctx: CanvasRenderingContext2D, x0: number, x1: number, y: number, values: number[],
  amp: number, thick: number, progress: number, seed: number, still: number, dot: boolean, flow = 0) {
  const width = Math.max(4, x1 - x0);
  const total = Math.max(24, Math.ceil(width / 2));
  const nodes = Math.max(3, Math.round(width / 46));
  const lv = values.length ? smooth(values, nodes) : Array(nodes + 1).fill(still);
  const waves = Math.max(1.5, width / 84);
  const end = Math.max(1, Math.floor(total * progress));
  let lastX = x0, lastY = y;
  ctx.lineWidth = 1.8 * thick;
  ctx.lineCap = 'round';
  ctx.lineJoin = 'round';
  ctx.beginPath();
  for (let i = 0; i <= end; i++) {
    const t = i / total;
    const at = t * nodes, k = Math.min(nodes - 1, Math.floor(at)), u = at - k, e = u * u * (3 - 2 * u);
    const level = lv[k] + (lv[k + 1] - lv[k]) * e;
    const taper = Math.sin(Math.PI * Math.min(1, t)) ** .55;
    const phase = t * Math.PI * waves + seed + flow;
    lastX = x0 + width * t;
    lastY = y + level * amp * taper * (Math.sin(phase) * .82 + Math.sin(phase * .46 + seed * 1.7) * .18);
    if (i) ctx.lineTo(lastX, lastY); else ctx.moveTo(lastX, lastY);
  }
  ctx.stroke();
  if (dot) {
    ctx.beginPath(); ctx.arc(lastX, lastY, 3.1 * thick, 0, Math.PI * 2); ctx.fill();
    ctx.globalAlpha *= .22;
    ctx.beginPath(); ctx.arc(lastX, lastY, 7.5 * thick, 0, Math.PI * 2); ctx.fill();
  }
}

export function VoiceScore({ text, stage, envelope, size = 30, onColor = false, reduced = false, dim = 0, className }: Props) {
  const wrap = useRef<HTMLDivElement>(null);
  const para = useRef<HTMLParagraphElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const lines = useRef<Line[]>([]);
  const lineEnv = useRef<number[][]>([]);
  const history = useRef<number[]>([]);
  const lastLevel = useRef(0);
  const stageAt = useRef(performance.now());
  const [layout, setLayout] = useState(0);
  const [boxes, setBoxes] = useState<{ mark: Mark; x: number; y: number; w: number; label: boolean }[]>([]);
  const chars = useMemo(() => Array.from(text), [text]);
  const marks = useMemo(() => (stage === 'thinking' ? keywords(text, 3) : [])
    // Patterns return UTF-16 offsets; the rendered glyph array uses code points.
    .map((m) => ({ ...m, start: Array.from(text.slice(0, m.start)).length, end: Array.from(text.slice(0, m.end)).length })), [text, stage]);
  const lh = size * 2.8;
  // Label + bracket must fit the leading above a line (≈ (lh − 1.32em) / 2) so they
  // never reach the previous line, e.g. in the capsule note at 17 px.
  const gap = (lh - size * 1.32) / 2;
  const bracketHeight = Math.max(3, Math.min(size * .15, gap * .25));
  const labelSize = Math.max(8, Math.min(size * .34, gap - bracketHeight - 3));

  useEffect(() => { stageAt.current = performance.now(); if (stage === 'listening') { history.current = []; lineEnv.current = []; } }, [stage]);

  useEffect(() => levels.subscribe((v) => {
    lastLevel.current = v;
    history.current.push(v);
    if (history.current.length > 600) history.current.shift();
  }), []);

  // Measure lines (and bracket boxes) from the rendered characters.
  useLayoutEffect(() => {
    const measure = () => {
      const p = para.current;
      if (!p) return;
      const spans = Array.from(p.children) as HTMLElement[];
      const found: Line[] = [];
      spans.forEach((s, i) => {
        if (!s.textContent?.trim()) return; // spaces and line breaks get no voice line
        const top = s.offsetTop, left = s.offsetLeft, right = left + s.offsetWidth;
        const line = found.find((l) => Math.abs(l.top - top) < 4);
        if (line) { line.left = Math.min(line.left, left); line.right = Math.max(line.right, right); line.last = i; }
        else found.push({ top, left, right, first: i, last: i });
      });
      // Freeze the finished line's wave when the transcript wraps.
      if (stage === 'listening' && found.length > lines.current.length && lines.current.length) {
        const width = lines.current[lines.current.length - 1].right - lines.current[lines.current.length - 1].left;
        lineEnv.current[lines.current.length - 1] = history.current.slice(-Math.max(8, Math.round(width / 12)));
      }
      lines.current = found;
      const next: typeof boxes = [];
      if (stage === 'thinking') marks.forEach((mark) => {
        let first = true;
        found.forEach((line) => {
          const start = Math.max(line.first, mark.start), end = Math.min(line.last + 1, mark.end);
          if (start >= end) return;
          const a = spans[start], b = spans[end - 1];
          const crowded = next.some((box) => box.label && Math.abs(box.y - a.offsetTop) < 4 &&
            a.offsetLeft < box.x + Math.max(box.w, size * 2.3) + 12);
          next.push({ mark, x: a.offsetLeft, y: a.offsetTop, w: b.offsetLeft + b.offsetWidth - a.offsetLeft, label: first && !crowded });
          first = false;
        });
      });
      setBoxes(next);
      setLayout((n) => n + 1);
    };
    measure();
    const observer = new ResizeObserver(measure);
    if (wrap.current) observer.observe(wrap.current);
    return () => observer.disconnect();
  }, [chars, marks, stage, size]);

  // Draw waves; animate only while listening/settling/revealing.
  useEffect(() => {
    const c = canvas.current!;
    const ctx = c.getContext('2d')!;
    let raf = 0, timer = 0, lastPaint = 0;
    const draw = (now: number) => {
      const box = c.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
      const w = Math.round(box.width * dpr), h = Math.round(box.height * dpr);
      if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, box.width, box.height);
      const color = getComputedStyle(c).color;
      ctx.fillStyle = color;
      ctx.strokeStyle = color;
      const age = now - stageAt.current;
      const ls = lines.current;
      const env = envelope?.length ? envelope : null;
      ls.forEach((line, i) => {
        // line.top is the inline glyph box top (offsetTop), not the line box top;
        // the ribbon floats in the leading above it.
        const y = line.top - Math.max(6, size * .4);
        let values: number[] = [];
        let amp = Math.min(size * .34, 5 + size * .2), progress = 1, alpha = onColor ? .92 : .8, still = .12;
        if (stage === 'idle') {
          amp = size * .07; still = .55; alpha = i >= dim && dim ? .32 : .55;
        } else if (stage === 'listening') {
          const width = line.right - line.left;
          values = i === ls.length - 1 ? history.current.slice(-Math.max(8, Math.round(width / 12))) : (lineEnv.current[i] ?? []);
        } else if (stage === 'thinking') {
          values = lineEnv.current[i] ?? [];
          amp *= Math.max(0, 1 - age / 500);
          alpha = .35;
        } else if (stage === 'answer') {
          if (env) {
            const a = Math.floor((i / ls.length) * env.length), b = Math.floor(((i + 1) / ls.length) * env.length);
            values = env.slice(a, Math.max(a + 2, b));
          } else values = [];
          const reveal = reduced ? 1 : Math.min(1, age / Math.min(1200, 380 + ls.length * 260));
          progress = Math.max(0, Math.min(1, reveal * ls.length - i));
        }
        if (progress <= 0) return;
        ctx.globalAlpha = alpha;
        const newest = stage === 'listening' && i === ls.length - 1;
        ribbon(ctx, line.left, line.right, y, values, amp, onColor ? 1.15 : 1, progress, i * 1.3, still,
          newest, newest && !reduced ? now / 520 : 0);
      });
      ctx.globalAlpha = 1;
    };
    const animated = () => !reduced && (stage === 'listening' || (stage === 'thinking' && performance.now() - stageAt.current < 600)
      || (stage === 'answer' && performance.now() - stageAt.current < 1600));
    const loop = () => {
      timer = window.setTimeout(() => {
        raf = requestAnimationFrame((now) => {
          draw(now); lastPaint = now;
          if (animated()) loop();
        });
      }, Math.max(1, FRAME - (performance.now() - lastPaint)));
    };
    draw(performance.now());
    if (animated()) loop();
    return () => { clearTimeout(timer); cancelAnimationFrame(raf); };
  }, [stage, chars, envelope, size, onColor, reduced, dim, layout]);

  const marked = new Set<number>();
  marks.forEach((m) => { for (let i = m.start; i < m.end; i++) marked.add(i); });
  const revealStep = Math.max(1, chars.length);

  return (
    <div ref={wrap} className={`relative ${className ?? ''}`} style={{ fontSize: size, lineHeight: `${lh}px` }}>
      <canvas ref={canvas} aria-hidden="true" className="pointer-events-none absolute inset-0 h-full w-full"
        style={{ color: onColor ? '#fff' : stage === 'listening' ? 'var(--mode)' : 'var(--art-ink)' }} />
      <p ref={para} className="relative m-0" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', fontFamily: 'var(--font-display)', fontWeight: 450, userSelect: 'text' }}>
        {chars.map((ch, i) => {
          let color = onColor ? '#fff' : 'var(--text)';
          let opacity = 1;
          if (stage === 'listening' && i >= chars.length - 2) opacity = .5;
          if (stage === 'thinking' && !marked.has(i)) color = 'var(--text-3)';
          if (stage === 'idle' && dim) {
            const line = lines.current.findIndex((l) => i >= l.first && i <= l.last);
            if (line >= dim) color = 'var(--text-3)';
          }
          const style: React.CSSProperties = { color, opacity, transition: 'color .45s ease, opacity .3s ease' };
          if (stage === 'answer' && !reduced) {
            style.animation = `score-in .5s ease both`;
            style.animationDelay = `${Math.min(900, (i / revealStep) * 900)}ms`;
          }
          return <span key={i} style={style}>{ch}</span>;
        })}
      </p>
      {boxes.map(({ mark, x, y, w, label }) => (
        // Anchored by its bottom edge to the top of the glyphs (glyph box top + ~0.12em).
        <div key={`${mark.start}-${y}`} className="score-bracket pointer-events-none absolute" style={{ left: x, top: y + size * .08, width: w, transform: 'translateY(-100%)' }}>
          {label && <div style={{ fontSize: labelSize, lineHeight: 1, color: 'var(--mode, var(--brand))', fontWeight: 600, whiteSpace: 'nowrap', marginBottom: 1 }}>{mark.label}</div>}
          <div style={{ height: bracketHeight, borderTop: '1.5px solid var(--mode, var(--brand))', borderLeft: '1.5px solid var(--mode, var(--brand))', borderRight: '1.5px solid var(--mode, var(--brand))', borderRadius: '2px 2px 0 0', opacity: .9 }} />
        </div>
      ))}
    </div>
  );
}
