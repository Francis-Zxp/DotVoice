/* The capsule's voice line: three strands of one constant stroke width under a
   bell envelope, coloured with the mode gradient. Amplitude follows the
   measured volume (fast attack, slow release) and the strands drift faster
   when the voice is louder; while thinking the line settles flat and a soft
   light travels along it. Volume only, never pitch or words.
   One rAF loop per wave, stopped as soon as nothing moves. */
import { useEffect, useRef } from 'react';
import { levels } from '../lib/store';

export type WaveState = 'listening' | 'thinking' | 'idle';

interface Props {
  width: number;
  height: number;
  state: WaveState;
  tone?: string;        // changes when the colours should be re-read (mode, tint)
  onColor?: boolean;    // drawn on a tinted card: white strands
  reduced?: boolean;
  className?: string;
}

const STRANDS = [
  { amp: 1, freq: 1.55, speed: 2.2, alpha: 1, lift: 0 },
  { amp: .68, freq: 2.25, speed: -2.9, alpha: .5, lift: 1.7 },
  { amp: .42, freq: 3.05, speed: 3.6, alpha: .3, lift: 3.1 },
];
const STROKE = 1.75;

function readColours(el: HTMLElement, onColor: boolean): [string, string] {
  if (onColor) return ['#ffffff', 'rgba(255,255,255,.86)'];
  const css = getComputedStyle(el);
  const a = css.getPropertyValue('--mode').trim() || css.color;
  const b = css.getPropertyValue('--mode-2').trim() || a;
  return [a, b];
}

export function LiveWave({ width, height, state, tone, onColor = false, reduced, className }: Props) {
  const ref = useRef<HTMLCanvasElement>(null);
  const target = useRef(0);
  const heardAt = useRef(0);
  // Kept across state changes so the strands never jump.
  const motion = useRef({ level: 0, phase: 0 });

  useEffect(() => {
    let reference = .03;
    return levels.subscribe((level) => {
      reference = Math.max(.02, reference * .985, level);
      target.current = Math.sqrt(Math.min(1, level / reference));
      heardAt.current = performance.now();
    });
  }, []);

  useEffect(() => {
    const canvas = ref.current!;
    const ctx = canvas.getContext('2d')!;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    let colours = readColours(canvas, onColor);
    // Styles may land a frame later (mode class swap): read once more.
    const reread = requestAnimationFrame(() => { colours = readColours(canvas, onColor); });
    let raf = 0;
    let { level, phase } = motion.current;
    let last = performance.now();
    let glint = 0;
    const started = last;
    const mid = height / 2;

    const draw = (now: number) => {
      raf = 0;
      if (document.hidden) return;
      // Several monitors may show the same session. Keep canvas paints at 30 fps,
      // independent of each display's refresh rate.
      if (now - last < 1000 / 30) { raf = requestAnimationFrame(draw); return; }
      const dt = Math.min(.05, (now - last) / 1000);
      last = now;
      // Levels arrive at ~10 Hz; if they stop, the voice has stopped.
      const goal = state === 'listening' && now - heardAt.current < 350 ? target.current : 0;
      level += (goal - level) * (goal > level ? 1 - Math.exp(-dt * 24) : 1 - Math.exp(-dt * 5.5));
      phase += dt * (state === 'listening' ? .95 + level * 1.5 : .3);
      motion.current = { level, phase };
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.globalCompositeOperation = 'source-over';
      ctx.clearRect(0, 0, width, height);
      const gradient = ctx.createLinearGradient(0, 0, width, 0);
      gradient.addColorStop(0, colours[0]);
      gradient.addColorStop(1, colours[1]);
      ctx.strokeStyle = gradient;
      ctx.lineWidth = STROKE;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      // A breath of motion even in silence, so a listening line never looks dead.
      const rest = state === 'listening' ? .08 : state === 'thinking' ? .025 : .04;
      const amp = (mid - STROKE) * (rest + level * (1 - rest));
      STRANDS.forEach((s, i) => {
        ctx.globalAlpha = s.alpha;
        const wobble = .82 + .18 * Math.sin(phase * .8 + s.lift);
        ctx.beginPath();
        for (let x = 0; x <= width; x += 1.25) {
          const t = x / width;
          const envelope = Math.sin(Math.PI * t) ** 1.7;
          const y = mid + amp * s.amp * wobble * envelope * Math.sin((t * s.freq * 2 + phase * s.speed * .5 + i * .35) * Math.PI);
          if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();
      });
      ctx.globalAlpha = 1;
      if (state === 'thinking' && !reduced) {
        // A soft light running along the settled line.
        glint = ((now - started) / 1100) % 1;
        const gx = -width * .25 + glint * width * 1.5;
        const light = ctx.createRadialGradient(gx, mid, 0, gx, mid, width * .22);
        light.addColorStop(0, onColor ? 'rgba(255,255,255,.95)' : colours[1]);
        light.addColorStop(1, 'rgba(255,255,255,0)');
        ctx.globalCompositeOperation = 'source-atop';
        ctx.fillStyle = light;
        ctx.fillRect(0, 0, width, height);
      }
      // Fade both ends into the glass.
      ctx.globalCompositeOperation = 'destination-in';
      const fade = ctx.createLinearGradient(0, 0, width, 0);
      fade.addColorStop(0, 'rgba(0,0,0,0)');
      fade.addColorStop(.14, 'rgba(0,0,0,1)');
      fade.addColorStop(.86, 'rgba(0,0,0,1)');
      fade.addColorStop(1, 'rgba(0,0,0,0)');
      ctx.fillStyle = fade;
      ctx.fillRect(0, 0, width, height);
      ctx.globalCompositeOperation = 'source-over';
      const moving = !reduced && (state !== 'idle' || level > .002);
      if (moving) raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    const visibility = () => {
      if (document.hidden) { cancelAnimationFrame(raf); raf = 0; }
      else if (!raf) { last = performance.now() - 34; raf = requestAnimationFrame(draw); }
    };
    document.addEventListener('visibilitychange', visibility);
    return () => { cancelAnimationFrame(raf); cancelAnimationFrame(reread); document.removeEventListener('visibilitychange', visibility); };
  }, [width, height, state, tone, onColor, reduced]);

  return <canvas ref={ref} aria-hidden="true" className={className} style={{ width, height, display: 'block' }} />;
}
