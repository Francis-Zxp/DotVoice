/* Present-mode stage: laser dot, spotlight and annotation strokes drawn on a
   transparent, click-through window. The shell streams the cursor position
   (once per composed frame) while something is shown; the page asks to be
   hidden when idle.

   Everything is drawn by one rAF loop that runs only while something moves:
   - laser: white-hot core, red body, soft bloom and a short trail that decays
     smoothly in time (it shrinks away even when the pointer stops);
   - spotlight: a gentle dim with a wide feathered hole that eases in/out and
     follows the pointer with a light critically-damped lag;
   - ink: strokes smoothed with quadratic midpoint curves, width from speed
     (slow = fuller, fast = finer, like pen pressure); finished strokes are
     cached on an offscreen layer; undo/clear fade out instead of vanishing. */
import { listen } from '@tauri-apps/api/event';
import { invoke } from '@tauri-apps/api/core';
import { call, inTauri, onCore } from './lib/bridge';
import './styles.css';

type Point = { x: number; y: number };
type Spark = Point & { t: number };
type Ink = Point & { w: number };
type Fade = { layer: HTMLCanvasElement; t0: number };

const TRAIL_MS = 190;     // laser trail lifetime
const DIM = .56;          // spotlight dim strength
const SPOT_R = 150;       // clear radius
const SPOT_FEATHER = 110; // soft edge width
const INK = '#ff3d71';
const INK_GLOW = 'rgba(255,61,113,.22)';
const FADE_MS = 240;

const canvas = document.createElement('canvas');
canvas.style.cssText = 'position:fixed;inset:0;width:100vw;height:100vh;pointer-events:none';
document.getElementById('root')!.append(canvas);
const ctx = canvas.getContext('2d')!;
const ink = document.createElement('canvas'); // finished strokes
const inkCtx = ink.getContext('2d')!;
let dpr = 1;

let tool = 'mouse';
let pointer = false;
let latched = false; // ② laser, independent of the ⑤ tool
let drawing = false;
let cursor: Point | null = null;
const trail: Spark[] = [];
const strokes: Ink[][] = [];
let live: Ink[] | null = null;
let lastInkAt = 0;
const fades: Fade[] = [];
let laserA = 0;            // 0..1 eased presence of the laser
let spotA = 0;             // 0..1 eased presence of the spotlight
let spot: Point | null = null;
let idleTimer = 0;
// The shell turns the system pointer itself into the laser core (no composition
// delay); the page then draws only the glow and trail around it.
let laserCursor = false;
// Motion prediction: the page is shown ~2 frames after the cursor was sampled, so
// what follows the pointer is drawn where it is heading, not where it was.
const LEAD_MS = 26;
const LEAD_MAX = 56;
let vel = { x: 0, y: 0 };  // px/ms, lightly smoothed
let sampledAt = 0;
let raf = 0;
let last = performance.now();

function resize() {
  dpr = window.devicePixelRatio || 1;
  for (const c of [canvas, ink]) {
    c.width = Math.round(innerWidth * dpr);
    c.height = Math.round(innerHeight * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  inkCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
  repaintInk();
  schedule();
}
addEventListener('resize', resize);

function laserOn() { return !!cursor && ((pointer && tool === 'laser') || latched); }
function spotOn() { return !!cursor && pointer && tool === 'spotlight'; }

function busy() {
  return pointer || latched || drawing || strokes.length > 0 || fades.length > 0;
}

function settle() {
  clearTimeout(idleTimer);
  if (!busy()) idleTimer = window.setTimeout(() => {
    if (!busy() && inTauri) invoke('stage_idle').catch((e) => call('log_ui_error', { where: 'stage', detail: 'stage_idle: ' + String(e) }).catch(() => {}));
  }, 450); // after the laser/spotlight have eased out
}

/* ── Ink ─────────────────────────────────────────────────────────────── */
function strokePath(g: CanvasRenderingContext2D, s: Ink[]) {
  // Glow pass: one path, constant width (no overlapping joints).
  g.lineCap = 'round';
  g.lineJoin = 'round';
  if (s.length === 1) {
    g.fillStyle = INK;
    g.beginPath(); g.arc(s[0].x, s[0].y, s[0].w / 2, 0, Math.PI * 2); g.fill();
    return;
  }
  const avg = s.reduce((a, p) => a + p.w, 0) / s.length;
  g.strokeStyle = INK_GLOW;
  g.lineWidth = avg + 7;
  g.beginPath();
  g.moveTo(s[0].x, s[0].y);
  for (let i = 1; i < s.length - 1; i++) {
    const mx = (s[i].x + s[i + 1].x) / 2, my = (s[i].y + s[i + 1].y) / 2;
    g.quadraticCurveTo(s[i].x, s[i].y, mx, my);
  }
  g.lineTo(s[s.length - 1].x, s[s.length - 1].y);
  g.stroke();
  // Body: midpoint quadratic segments, each with its own width (opaque, so joints vanish).
  g.strokeStyle = INK;
  let px = s[0].x, py = s[0].y;
  for (let i = 1; i < s.length; i++) {
    const end = i < s.length - 1 ? { x: (s[i].x + s[i + 1].x) / 2, y: (s[i].y + s[i + 1].y) / 2 } : s[i];
    g.lineWidth = (s[i - 1].w + s[i].w) / 2;
    g.beginPath();
    g.moveTo(px, py);
    g.quadraticCurveTo(s[i].x, s[i].y, end.x, end.y);
    g.stroke();
    px = end.x; py = end.y;
  }
}

function repaintInk() {
  inkCtx.clearRect(0, 0, innerWidth, innerHeight);
  for (const s of strokes) strokePath(inkCtx, s);
}

function addInk(p: Point) {
  if (!live) return;
  const now = performance.now();
  const prev = live[live.length - 1];
  if (!prev) { live.push({ ...p, w: 4.6 }); lastInkAt = now; return; }
  const d = Math.hypot(p.x - prev.x, p.y - prev.y);
  if (d < 1.2) return;
  const speed = d / Math.max(4, now - lastInkAt); // px per ms
  const goal = Math.max(2.4, Math.min(6.4, 6.4 - speed * 1.7));
  live.push({ ...p, w: prev.w + (goal - prev.w) * .32 });
  lastInkAt = now;
}

function snapshotInk(): HTMLCanvasElement {
  const c = document.createElement('canvas');
  c.width = ink.width;
  c.height = ink.height;
  c.getContext('2d')!.drawImage(ink, 0, 0);
  return c;
}

/* ── Laser ───────────────────────────────────────────────────────────── */
function drawLaser(now: number, a: number) {
  const head = ahead(now);
  if (!head) return;
  const { x, y } = head;
  const s = .55 + .45 * a;
  ctx.save();
  // Trail: a tapered, smoothed ribbon that shrinks with age. Butt caps and plain
  // compositing, so consecutive segments never overlap into beads.
  const pts = trail.filter((p) => now - p.t < TRAIL_MS);
  if (pts.length > 1) {
    ctx.lineCap = 'butt';
    for (let i = 1; i < pts.length; i++) {
      const life = 1 - (now - pts[i].t) / TRAIL_MS;
      const k = life * life;
      ctx.strokeStyle = `rgba(255,${50 + 40 * k | 0},${70 + 30 * k | 0},${.42 * k * a})`;
      ctx.lineWidth = 1 + 8 * k * s;
      const a0 = i > 1 ? { x: (pts[i - 1].x + pts[i - 2].x) / 2, y: (pts[i - 1].y + pts[i - 2].y) / 2 } : pts[0];
      const a1 = { x: (pts[i].x + pts[i - 1].x) / 2, y: (pts[i].y + pts[i - 1].y) / 2 };
      ctx.beginPath();
      ctx.moveTo(a0.x, a0.y);
      ctx.quadraticCurveTo(pts[i - 1].x, pts[i - 1].y, a1.x, a1.y);
      ctx.stroke();
    }
  }
  // Wide halo, bloom (additive), then the dot.
  ctx.globalCompositeOperation = 'lighter';
  const halo = ctx.createRadialGradient(x, y, 0, x, y, 70 * s);
  halo.addColorStop(0, `rgba(255,40,70,${.16 * a})`);
  halo.addColorStop(1, 'rgba(255,40,70,0)');
  ctx.fillStyle = halo;
  ctx.beginPath(); ctx.arc(x, y, 70 * s, 0, Math.PI * 2); ctx.fill();
  if (laserCursor) { ctx.restore(); return; }  // the pointer is the bloom and core
  const bloom = ctx.createRadialGradient(x, y, 0, x, y, 24 * s);
  bloom.addColorStop(0, `rgba(255,90,110,${.85 * a})`);
  bloom.addColorStop(.3, `rgba(255,45,75,${.5 * a})`);
  bloom.addColorStop(.65, `rgba(255,30,60,${.14 * a})`);
  bloom.addColorStop(1, 'rgba(255,30,60,0)');
  ctx.fillStyle = bloom;
  ctx.beginPath(); ctx.arc(x, y, 24 * s, 0, Math.PI * 2); ctx.fill();
  ctx.restore();
  const core = ctx.createRadialGradient(x, y, 0, x, y, 5.6 * s);
  core.addColorStop(0, `rgba(255,255,255,${a})`);
  core.addColorStop(.42, `rgba(255,236,238,${a})`);
  core.addColorStop(.7, `rgba(255,70,92,${a})`);
  core.addColorStop(1, 'rgba(255,40,70,0)');
  ctx.fillStyle = core;
  ctx.beginPath(); ctx.arc(x, y, 5.6 * s, 0, Math.PI * 2); ctx.fill();
}

/* ── Spotlight ───────────────────────────────────────────────────────── */
function drawSpotlight(a: number) {
  if (!spot) return;
  ctx.save();
  ctx.fillStyle = `rgba(4,6,14,${DIM * a})`;
  ctx.fillRect(0, 0, innerWidth, innerHeight);
  // Cut a hole whose edge follows a smoothstep falloff (no visible ring).
  ctx.globalCompositeOperation = 'destination-out';
  const r = SPOT_R + SPOT_FEATHER;
  const g = ctx.createRadialGradient(spot.x, spot.y, 0, spot.x, spot.y, r);
  const inner = SPOT_R / r;
  g.addColorStop(0, 'rgba(0,0,0,1)');
  for (let i = 0; i <= 8; i++) {
    const t = i / 8;
    const smooth = 1 - t * t * (3 - 2 * t);
    g.addColorStop(inner + (1 - inner) * t, `rgba(0,0,0,${smooth})`);
  }
  ctx.fillStyle = g;
  ctx.beginPath(); ctx.arc(spot.x, spot.y, r, 0, Math.PI * 2); ctx.fill();
  ctx.restore();
}

/** Where the pointer is about to be when this frame reaches the screen. */
function ahead(now: number): Point | null {
  if (!cursor) return null;
  const age = Math.max(0, now - sampledAt);
  if (age > 70) return cursor;  // stopped: no overshoot
  const lead = LEAD_MS * (1 - age / 70);
  let dx = vel.x * lead, dy = vel.y * lead;
  const len = Math.hypot(dx, dy);
  if (len > LEAD_MAX) { dx *= LEAD_MAX / len; dy *= LEAD_MAX / len; }
  return { x: cursor.x + dx, y: cursor.y + dy };
}

/* ── Frame ───────────────────────────────────────────────────────────── */
function frame(now: number) {
  raf = 0;
  const dt = Math.min(.05, (now - last) / 1000);
  last = now;
  let moving = false;
  const ease = (value: number, goal: number, up: number, down: number) => {
    const next = value + (goal - value) * (1 - Math.exp(-dt * (goal > value ? up : down)));
    const done = Math.abs(goal - next) < .003;
    if (!done) moving = true;
    return done ? goal : next;
  };
  laserA = ease(laserA, laserOn() ? 1 : 0, 26, 12);
  spotA = ease(spotA, spotOn() ? 1 : 0, 10, 9);
  const target = ahead(now);
  if (target) {
    if (!spot) spot = { ...target };
    // Only enough smoothing to calm hand tremor (~8 ms), not a visible lag.
    const k = 1 - Math.exp(-dt * 120);
    spot.x += (target.x - spot.x) * k;
    spot.y += (target.y - spot.y) * k;
    if (spotA > 0 && Math.hypot(target.x - spot.x, target.y - spot.y) > .3) moving = true;
    if ((spotA > 0 || laserA > 0) && now - sampledAt < 70) moving = true; // keep predicting
  }
  while (trail.length && now - trail[0].t > TRAIL_MS) trail.shift();
  if (trail.length > 1 && laserA > 0) moving = true;

  ctx.clearRect(0, 0, innerWidth, innerHeight);
  if (spotA > 0) drawSpotlight(spotA);
  if (strokes.length) ctx.drawImage(ink, 0, 0, innerWidth, innerHeight);
  for (let i = fades.length - 1; i >= 0; i--) {
    const t = (now - fades[i].t0) / FADE_MS;
    if (t >= 1) { fades.splice(i, 1); continue; }
    ctx.globalAlpha = (1 - t) * (1 - t);
    ctx.drawImage(fades[i].layer, 0, 0, innerWidth, innerHeight);
    ctx.globalAlpha = 1;
    moving = true;
  }
  if (live && live.length) strokePath(ctx, live);
  if (laserA > 0) drawLaser(now, laserA);
  if (moving) schedule();
  else settle();
}

function schedule() {
  if (!raf) raf = requestAnimationFrame(frame);
}

function onCursor(p: Point) {
  const t = performance.now();
  if (cursor && t - sampledAt < 90) {
    const dt = Math.max(4, t - sampledAt);
    vel.x += ((p.x - cursor.x) / dt - vel.x) * .55;
    vel.y += ((p.y - cursor.y) / dt - vel.y) * .55;
  } else {
    vel = { x: 0, y: 0 };
  }
  sampledAt = t;
  cursor = p;
  if (laserOn()) trail.push({ ...p, t: performance.now() });
  if (drawing) addInk(p);
  // Only what follows the pointer needs a frame; an idle stage must reach stage_idle
  // (each idle frame restarts that timer, and the shell streams samples while it shows).
  if (laserOn() || spotOn() || drawing || laserA > 0 || spotA > 0) schedule();
}

if (inTauri) {
  listen<Point>('stage://cursor', ({ payload }) => onCursor(payload));
  listen<boolean>('stage://laser-cursor', ({ payload }) => { laserCursor = !!payload; schedule(); });
}
else {
  // Browser preview: the mouse stands in for the pen, on a stand-in slide.
  addEventListener('mousemove', (e) => onCursor({ x: e.clientX, y: e.clientY }));
  document.documentElement.classList.add('preview');
  document.body.style.cssText = 'background:linear-gradient(160deg,#fbfbfd,#eef0f5) !important';
  const slide = document.createElement('div');
  slide.style.cssText = 'position:fixed;inset:0;z-index:-1;padding:72px 88px;font:600 44px "Segoe UI Variable Display","Microsoft YaHei UI",sans-serif;color:#1c1c1f';
  slide.innerHTML = '季度复盘<div style="font:400 22px \'Microsoft YaHei UI\',sans-serif;color:#555;margin-top:24px;line-height:2">· 端到端延迟降到 400 ms 以内<br>· 气声输入字准率提升约 6%<br>· 下一步：实时预览与定稿无缝衔接</div>';
  document.body.append(slide);
}

onCore((event, data) => {
  if (event === 'pointer' && data.latched) {
    latched = !!data.active;
    trail.length = 0;
  } else if (event === 'pointer') {
    pointer = !!data.active && data.tool !== 'magnifier';
    tool = data.tool;
    trail.length = 0;
  } else if (event === 'tool') {
    tool = data.tool;
  } else if (event === 'annotate') {
    if (data.action === 'draw') {
      const down = !!data.down;
      if (down && !drawing) {
        live = [];
        if (cursor) addInk(cursor);
      } else if (!down && drawing && live) {
        if (live.length) { strokes.push(live); strokePath(inkCtx, live); }
        live = null;
      }
      drawing = down;
    } else if (data.action === 'undo') {
      drawing = false;
      live = null;
      if (strokes.length) {
        const before = snapshotInk();
        strokes.pop();
        repaintInk();
        // Fade only what was removed: the old layer minus what remains.
        const g = before.getContext('2d')!;
        g.globalCompositeOperation = 'destination-out';
        g.drawImage(ink, 0, 0);
        fades.push({ layer: before, t0: performance.now() });
      }
    } else if (data.action === 'clear') {
      drawing = false;
      live = null;
      if (strokes.length) {
        fades.push({ layer: snapshotInk(), t0: performance.now() });
        strokes.length = 0;
        repaintInk();
      }
    }
  } else if (event === 'core_exit') {
    // The shell hid the stage and forgot the pointer; the restarted core starts unlatched.
    pointer = latched = drawing = false;
    live = null;
    trail.length = 0;
    strokes.length = 0;
    repaintInk();
  } else if (event === 'mode' && data.value !== 'present') {
    pointer = latched = drawing = false;
    live = null;
    strokes.length = 0;
    repaintInk();
  } else return;
  last = performance.now();
  schedule();
  settle();
});

resize();
