/* Pixels behind the capsule for the liquid-glass material.
   In the app, the shell copies (downscaled) the screen area behind the overlay
   window while the card is invisible; nothing is stored. In a plain browser
   preview a painted stand-in desktop is used instead (?desk=default|doc|ide|photo). */
import { invoke } from '@tauri-apps/api/core';
import { inTauri } from '../lib/bridge';

const WIDTH = 300; // backdrop copy width in pixels (the window is 500 logical px wide)

export async function captureBackdrop(): Promise<HTMLCanvasElement | null> {
  try {
    if (!inTauri) return previewCapture();
    const buffer = await invoke<ArrayBuffer>('overlay_backdrop', { width: WIDTH });
    const view = new DataView(buffer);
    const w = view.getUint32(0, true), h = view.getUint32(4, true);
    if (!w || !h || buffer.byteLength < 8 + w * h * 4) return null;
    const canvas = document.createElement('canvas');
    canvas.width = w;
    canvas.height = h;
    canvas.getContext('2d', { willReadFrequently: true })!.putImageData(new ImageData(new Uint8ClampedArray(buffer, 8, w * h * 4), w, h), 0, 0);
    return canvas;
  } catch {
    return null; // the material falls back to frosted tint
  }
}

export interface Tone {
  light: number; // mean luminance 0..1
  busy: number;  // 0 calm .. 1 busy (text, edges, photos)
}

/* What the glass sits on, for adaptive tint and shadow (Liquid Glass keeps its
   dynamic range legible: more tint and a deeper shadow over busy or clashing
   content, less over calm content). `rect` is in window CSS pixels. */
export function sampleTone(image: HTMLCanvasElement, rect: { x: number; y: number; w: number; h: number }): Tone {
  const sx = image.width / innerWidth, sy = image.height / innerHeight;
  const x = Math.max(0, Math.floor(rect.x * sx)), y = Math.max(0, Math.floor(rect.y * sy));
  const w = Math.max(1, Math.min(image.width - x, Math.ceil(rect.w * sx)));
  const h = Math.max(1, Math.min(image.height - y, Math.ceil(rect.h * sy)));
  try {
    const data = image.getContext('2d', { willReadFrequently: true })!.getImageData(x, y, w, h).data;
    let sum = 0, sq = 0, edge = 0, n = 0, previous = -1;
    for (let i = 0; i < data.length; i += 4) {
      const l = (data[i] * .2126 + data[i + 1] * .7152 + data[i + 2] * .0722) / 255;
      sum += l; sq += l * l; n++;
      if (previous >= 0 && (i / 4) % w) edge += Math.abs(l - previous);
      previous = l;
    }
    const mean = sum / n;
    const spread = Math.sqrt(Math.max(0, sq / n - mean * mean));
    const busy = Math.min(1, spread * 2.2 + (edge / n) * 9);
    return { light: mean, busy };
  } catch {
    return { light: .5, busy: .3 };
  }
}

/* ── Browser preview only ─────────────────────────────────────────────── */
let desktop: HTMLCanvasElement | null = null;
type Desk = 'default' | 'doc' | 'ide' | 'photo';

export function paintPreviewDesktop(dark: boolean) {
  if (inTauri) return;
  desktop?.remove();
  const c = document.createElement('canvas');
  c.width = innerWidth;
  c.height = innerHeight;
  c.style.cssText = 'position:fixed;inset:0;z-index:-1;width:100vw;height:100vh';
  const g = c.getContext('2d')!;
  const desk = (new URLSearchParams(location.search).get('desk') ?? 'default') as Desk;
  if (desk === 'doc') paintDocument(g, c.width, c.height);
  else if (desk === 'ide') paintIde(g, c.width, c.height);
  else if (desk === 'photo') paintPhoto(g, c.width, c.height);
  else paintBrowser(g, c.width, c.height, dark);
  document.body.prepend(c);
  desktop = c;
}

function blob(g: CanvasRenderingContext2D, w: number, x: number, y: number, r: number, color: string) {
  const b = g.createRadialGradient(w * x, w * y, 0, w * x, w * y, w * r);
  b.addColorStop(0, color);
  b.addColorStop(1, color.slice(0, 7) + '00');
  g.fillStyle = b;
  g.fillRect(0, 0, g.canvas.width, g.canvas.height);
}

function paintBrowser(g: CanvasRenderingContext2D, w: number, h: number, dark: boolean) {
  const bg = g.createLinearGradient(0, 0, w, h);
  bg.addColorStop(0, dark ? '#1b2440' : '#8fb3ff');
  bg.addColorStop(.5, dark ? '#3a1f4d' : '#f3a6c8');
  bg.addColorStop(1, dark ? '#0f3b3a' : '#ffd38a');
  g.fillStyle = bg;
  g.fillRect(0, 0, w, h);
  blob(g, w, .2, .1, .35, dark ? '#4b6cffcc' : '#5b8cffcc');
  blob(g, w, .85, .2, .3, '#ff7aa8cc');
  blob(g, w, .6, .75, .4, '#36d1b0cc');
  // A browser-like window behind the capsule: tabs, address bar and text.
  g.fillStyle = dark ? 'rgba(32,33,38,.96)' : 'rgba(255,255,255,.96)';
  g.beginPath(); g.roundRect(18, 30, w - 36, h - 50, 12); g.fill();
  g.fillStyle = dark ? '#3b3d46' : '#e8eaf0';
  g.fillRect(18, 30, w - 36, 44);
  g.fillStyle = dark ? '#5a8cff' : '#2f6bff';
  g.beginPath(); g.roundRect(34, 40, 120, 26, 8); g.fill();
  g.fillStyle = dark ? '#e8e8ee' : '#1c1c1f';
  for (let i = 0; i < 9; i++) {
    g.globalAlpha = i === 0 ? 1 : .55;
    g.fillRect(44, 104 + i * 30, (w - 120) * (0.55 + 0.4 * Math.abs(Math.sin(i * 1.7))), i === 0 ? 14 : 8);
  }
  g.globalAlpha = 1;
}

/* A bright word-processor page: white paper, black text right under the capsule. */
function paintDocument(g: CanvasRenderingContext2D, w: number, h: number) {
  g.fillStyle = '#e9ebef';
  g.fillRect(0, 0, w, h);
  g.fillStyle = '#ffffff';
  g.fillRect(24, 0, w - 48, h);
  g.fillStyle = '#1d1d1f';
  g.font = '700 22px "Segoe UI", sans-serif';
  g.fillText('季度复盘 · 第三部分', 48, 46);
  g.font = '15px "Microsoft YaHei UI", sans-serif';
  const lines = ['本季度的核心目标是把语音输入的端到端延迟', '控制在 400 毫秒以内，同时保持识别准确率。', '我们在三个场景里做了对照：会议记录、', '代码注释和演示讲稿。结果显示，气声输入', '在安静环境下的字准率提升了约 6%。', '下一步计划把实时预览与定稿模型的切换', '做得更平滑，避免用户看到文字跳动。'];
  lines.forEach((t, i) => g.fillText(t, 48, 86 + i * 27));
  g.fillStyle = '#2f6bff';
  g.fillRect(48, 290, 160, 3);
}

/* A dark code editor: near-black with thin coloured tokens. */
function paintIde(g: CanvasRenderingContext2D, w: number, h: number) {
  g.fillStyle = '#16171c';
  g.fillRect(0, 0, w, h);
  g.fillStyle = '#1f2027';
  g.fillRect(0, 0, w, 34);
  g.fillStyle = '#2b2d36';
  g.fillRect(12, 6, 110, 28);
  const palette = ['#c678dd', '#61afef', '#98c379', '#e5c07b', '#abb2bf', '#56b6c2', '#e06c75'];
  g.font = '13px Consolas, monospace';
  for (let i = 0; i < 22; i++) {
    g.fillStyle = '#4b5263';
    g.fillText(String(i + 1).padStart(2, ' '), 10, 58 + i * 20);
    let x = 44 + (i % 4) * 14;
    const tokens = 2 + ((i * 7) % 5);
    for (let k = 0; k < tokens; k++) {
      const len = 18 + ((i * 13 + k * 29) % 70);
      g.fillStyle = palette[(i + k * 3) % palette.length];
      g.globalAlpha = .9;
      g.fillRect(x, 50 + i * 20, len, 7);
      x += len + 8;
    }
  }
  g.globalAlpha = 1;
}

/* A saturated photo-like wallpaper: sunset sky, bright sun, dark ridge. */
function paintPhoto(g: CanvasRenderingContext2D, w: number, h: number) {
  const sky = g.createLinearGradient(0, 0, 0, h);
  sky.addColorStop(0, '#1e3a8a');
  sky.addColorStop(.28, '#7c3aed');
  sky.addColorStop(.5, '#f97316');
  sky.addColorStop(.62, '#fde047');
  sky.addColorStop(1, '#0f172a');
  g.fillStyle = sky;
  g.fillRect(0, 0, w, h);
  blob(g, w, .7, .12, .28, '#fef08aee');
  blob(g, w, .15, .05, .3, '#22d3eecc');
  blob(g, w, .45, .02, .18, '#ffffffcc');
  g.fillStyle = '#111827';
  g.beginPath();
  g.moveTo(0, h * .62);
  for (let x = 0; x <= w; x += 20) g.lineTo(x, h * .6 - Math.abs(Math.sin(x * .013)) * 60 - Math.sin(x * .041) * 14);
  g.lineTo(w, h); g.lineTo(0, h); g.fill();
}

function previewCapture() {
  if (!desktop) return null;
  const h = Math.round(desktop.height * WIDTH / desktop.width);
  const c = document.createElement('canvas');
  c.width = WIDTH;
  c.height = h;
  const g = c.getContext('2d', { willReadFrequently: true })!;
  g.imageSmoothingQuality = 'high';
  g.drawImage(desktop, 0, 0, WIDTH, h);
  return c;
}
