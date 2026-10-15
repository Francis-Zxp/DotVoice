/* Device illustrations — original vector drawings, never traced from or embedding
   vendor photos (reference notes and the photos used for proportions live in
   design/device-refs, which is not shipped).

   One studio for every device: key light from the top-left, objects lying on a
   surface seen almost from above, so the top face reads cleanly and the near side
   rolls away into a darker band below it. Nothing has a hard corner: bars are drawn
   as one rounded silhouette whose cross-section is painted by a single gradient
   (crest highlight on the far edge, flat top, the near edge rolling into the side
   wall, a thin bounce light at the bottom), round objects are lofted from the glass
   edge out to the rim. Soft gradient-only contact shadows (no blur filters, so idle
   repaints stay cheap), real materials: bead-blasted aluminium, matte soft-touch
   plastic, glossy black glass.

   Theme: shadows deepen and a faint back-light and rim separate dark objects in dark
   themes; the highlight colour follows --art-accent. Idle motion is limited to tiny
   regions repainted a few times a second (a stepped status-LED breath, a one-second
   watch tick) plus a single sheen pass on mount, and switches off under reduced
   motion (OS setting or the app's .reduced class).

   `highlight` lights up one key and adds a numbered callout. It accepts the manual
   numbering (①…⑪ or 1…11), a label that contains it ("长按翻译键 ⑦"), a part name
   (record, translate, annotate, laser, pageup, pagedown, airmouse, ok, volup,
   voldown, switch, usbc, led, mic, receiver, pwr, boot, screen) or a binding id
   (toggle_mode, quick_note, right_click …). */
import { useId } from 'react';

export type ArtProps = { className?: string; highlight?: string };

const FONT = '"Segoe UI Variable Display", "Segoe UI", "Microsoft YaHei UI", system-ui, sans-serif';

const CSS = `
.da{--da-k:0;overflow:visible}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]) .da{--da-k:1}}
:root[data-theme="dark"] .da,:root[data-theme="glass"] .da{--da-k:1}
.da .da-amb{opacity:calc(.78 + var(--da-k) * .22)}
.da .da-con{opacity:calc(.72 + var(--da-k) * .28)}
.da .da-back{opacity:var(--da-k)}
.da .da-hl-fill{fill:var(--art-accent,#5145e4)}
.da .da-hl-stroke{stroke:var(--art-accent,#5145e4)}
.da .da-led{animation:da-breathe 3.4s steps(14) infinite}
.da .da-sweep{animation:da-sweep 2s cubic-bezier(.45,0,.2,1) .5s 1 both}
.da .da-hl{animation:da-hl 1.5s ease-in-out infinite}
.da .da-pop{animation:da-pop .34s cubic-bezier(.2,.8,.2,1) both}
.da .da-tick{animation:da-tick 60s steps(60,end) infinite;transform-box:fill-box;transform-origin:center}
.da .da-blink{animation:da-blink 2s steps(1,end) infinite}
.da .da-draw{animation:da-draw 1.4s cubic-bezier(.3,0,.1,1) .25s both}
@keyframes da-breathe{0%,100%{opacity:1}50%{opacity:.4}}
@keyframes da-sweep{from{transform:translateX(0)}to{transform:translateX(430px)}}
@keyframes da-hl{0%,100%{opacity:.5}50%{opacity:1}}
@keyframes da-pop{from{opacity:0;transform:translateY(3px)}to{opacity:1;transform:none}}
@keyframes da-tick{to{transform:rotate(360deg)}}
@keyframes da-blink{50%{fill-opacity:.45}}
@keyframes da-draw{from{stroke-dashoffset:1}}
@media (prefers-reduced-motion:reduce){.da .da-anim{animation:none!important}}
.reduced .da .da-anim{animation:none!important}
`;

/* ── Shared helpers ─────────────────────────────────────────────────────── */

function useUid() {
  return 'da' + useId().replace(/[^a-zA-Z0-9]/g, '');
}

const ALIAS: Record<string, string> = {
  '1': 'ok', ok: 'ok', confirm: 'ok', enter: 'ok', airmouse_double: 'ok',
  '2': 'laser', laser: 'laser',
  '3': 'pageup', pageup: 'pageup', prev: 'pageup', airmouse_left: 'pageup',
  '4': 'pagedown', pagedown: 'pagedown', next: 'pagedown', airmouse_right: 'pagedown', right_click: 'pagedown',
  '5': 'airmouse', airmouse: 'airmouse', mouse: 'airmouse', airmouse_activity: 'airmouse',
  '6': 'record', record: 'record', voice: 'record', talk: 'record', arm: 'record', arm_toggle: 'record',
  '7': 'translate', translate: 'translate', toggle_mode: 'translate', quick_note: 'translate',
  '8': 'annotate', annotate: 'annotate', pen: 'annotate', highlight: 'annotate',
  '9': 'volup', volup: 'volup', 'vol+': 'volup', airmouse_wheel_up: 'volup',
  '10': 'voldown', voldown: 'voldown', 'vol-': 'voldown', airmouse_wheel_down: 'voldown',
  '11': 'switch', switch: 'switch', onoff: 'switch',
  usbc: 'usbc', typec: 'usbc', 'type-c': 'usbc', usb: 'usbc', charge: 'usbc',
  led: 'led', indicator: 'led', mic: 'mic', receiver: 'receiver', dongle: 'receiver',
  pwr: 'pwr', power: 'pwr', boot: 'boot', screen: 'screen', display: 'screen',
};

/** Normalise any accepted highlight spelling to a part id of that device. */
export function artPart(h?: string, device: 'baseus' | 'pv60' | 'tidepool' = 'baseus'): string {
  if (!h) return '';
  const circled = h.match(/[①-⑪]/);
  const key = circled ? String(circled[0].charCodeAt(0) - 0x245f) : h.trim().toLowerCase().replace(/\s+/g, '');
  const part = ALIAS[key] ?? key;
  if (device === 'tidepool' && part === 'switch') return 'pwr';
  if (device !== 'tidepool' && part === 'pwr') return 'switch';
  return part;
}

const BADGE: Record<string, string> = {
  ok: '1', laser: '2', pageup: '3', pagedown: '4', airmouse: '5', record: '6', translate: '7',
  annotate: '8', volup: '9', voldown: '10', switch: '11',
  usbc: 'Type-C', led: '指示灯', mic: '拾音孔', receiver: '接收器', pwr: 'PWR', boot: 'BOOT', screen: '屏幕',
};
const SPOKEN: Record<string, string> = {
  ok: '① 确认键', laser: '② 激光键', pageup: '③ 上翻键', pagedown: '④ 下翻键', airmouse: '⑤ 飞鼠键',
  record: '⑥ 录音键', translate: '⑦ 翻译键', annotate: '⑧ 标注键', volup: '⑨ 音量加', voldown: '⑩ 音量减',
  switch: '⑪ 开关', usbc: 'Type-C 充电口', led: '指示灯', mic: '拾音孔', receiver: '接收器',
  pwr: 'PWR 电源键', boot: 'BOOT 键', screen: '屏幕',
};
const label = (base: string, part: string, spoken: Record<string, string> = SPOKEN) =>
  (part && spoken[part] ? `${base}，高亮：${spoken[part]}` : base);

type Pt = [number, number];

const n2 = (v: number) => +v.toFixed(2);

/* Pen canvases share one box, cropped close to the drawing (callouts included)
   so the pens stay large in small cards. */
const PEN_BOX = '11 5 328 180';

/** A local drawing frame (w×h) centred at (cx, cy) and rotated by deg. */
function frame(cx: number, cy: number, w: number, h: number, deg: number) {
  const t = (deg * Math.PI) / 180, c = Math.cos(t), s = Math.sin(t);
  return {
    transform: `translate(${cx} ${cy}) rotate(${deg}) translate(${-w / 2} ${-h / 2})`,
    pt: (x: number, y: number): Pt => {
      const dx = x - w / 2, dy = y - h / 2;
      return [cx + dx * c - dy * s, cy + dx * s + dy * c];
    },
  };
}

function arc(cx: number, cy: number, rx: number, ry: number, a0: number, a1: number) {
  const p = (a: number) => [cx + rx * Math.cos((a * Math.PI) / 180), cy + ry * Math.sin((a * Math.PI) / 180)].map((v) => v.toFixed(2));
  const [x0, y0] = p(a0), [x1, y1] = p(a1);
  return `M${x0} ${y0}A${rx} ${ry} 0 ${Math.abs(a1 - a0) > 180 ? 1 : 0} 1 ${x1} ${y1}`;
}

/** Linear colour ramp over [t, #rrggbb] stops. */
function ramp(stops: [number, string][], t: number) {
  let i = 1;
  while (i < stops.length - 1 && stops[i][0] < t) i++;
  const [t0, c0] = stops[i - 1], [t1, c1] = stops[i];
  const k = Math.min(1, Math.max(0, (t - t0) / (t1 - t0 || 1)));
  const a = parseInt(c0.slice(1), 16), b = parseInt(c1.slice(1), 16);
  const ch = (s: number) => Math.round(((a >> s) & 255) * (1 - k) + ((b >> s) & 255) * k);
  return `rgb(${ch(16)},${ch(8)},${ch(0)})`;
}

/* ── Rounded bars ───────────────────────────────────────────────────────── */
/* A bar lies along x in its own frame: length L, plan height H (top face plus the
   visible near wall), rounded ends reaching e units in, long sides bowed out by
   `bow` at mid-length so the silhouette reads as soft arcs. */

/** How far a long side bulges out at x (0 at the ends, `bow` at mid-length). */
function bowAt(L: number, e: number, bow: number, x: number) {
  const t = (x - e) / (L - 2 * e);
  return t <= 0 || t >= 1 ? 0 : bow * 4 * t * (1 - t);
}

/** Quadratic run parallel to a bowed long side (y at the ends, y − bow at mid-length),
    from x0 (the current point) to x1; both inside [e, L − e]. */
function sideQ(L: number, e: number, bow: number, y: number, x0: number, x1: number) {
  const t0 = (x0 - e) / (L - 2 * e), t1 = (x1 - e) / (L - 2 * e);
  const c = y - 2 * bow * ((1 - t0) * t1 + t0 * (1 - t1));
  return `Q${n2((x0 + x1) / 2)} ${n2(c)} ${n2(x1)} ${n2(y - 4 * bow * t1 * (1 - t1))}`;
}

/** A full line parallel to a long side, as its own path. */
function sideLine(L: number, e: number, bow: number, y: number, x0: number, x1: number) {
  const t0 = (x0 - e) / (L - 2 * e);
  return `M${n2(x0)} ${n2(y - 4 * bow * t0 * (1 - t0))}` + sideQ(L, e, bow, y, x0, x1);
}

/** Silhouette of a bar, optionally inset by i. `round` sets how soon the ends turn. */
function barPath(L: number, H: number, e: number, bow: number, round = 0.8, i = 0) {
  const E = Math.max(e - i * 0.4, 1), xa = i + E, xb = L - i - E, mid = L / 2;
  const top = i, bot = H - i, cyM = H / 2, m = (H / 2 - i) * round, c = E * 0.92;
  const s = (2 * bow) / ((xb - xa) / 2);
  const f = n2;
  return `M${f(xa)} ${f(top)}Q${f(mid)} ${f(top - 2 * bow)} ${f(xb)} ${f(top)}`
    + `C${f(xb + c)} ${f(top + c * s)} ${f(L - i)} ${f(cyM - m)} ${f(L - i)} ${f(cyM)}`
    + `C${f(L - i)} ${f(cyM + m)} ${f(xb + c)} ${f(bot - c * s)} ${f(xb)} ${f(bot)}`
    + `Q${f(mid)} ${f(bot + 2 * bow)} ${f(xa)} ${f(bot)}`
    + `C${f(xa - c)} ${f(bot - c * s)} ${f(i)} ${f(cyM + m)} ${f(i)} ${f(cyM)}`
    + `C${f(i)} ${f(cyM - m)} ${f(xa - c)} ${f(top + c * s)} ${f(xa)} ${f(top)}Z`;
}

/** A band of width w hugging one rounded end (left or right) of a bar. */
function endBand(L: number, H: number, e: number, round: number, w: number, side: 'left' | 'right') {
  const cyM = H / 2, m = cyM * round, c = e * 0.92, f = n2;
  if (side === 'left') {
    return `M${f(e)} 0C${f(e - c)} 0 0 ${f(cyM - m)} 0 ${f(cyM)}C0 ${f(cyM + m)} ${f(e - c)} ${f(H)} ${f(e)} ${f(H)}`
      + `H${f(e + w)}C${f(e - c + w)} ${f(H)} ${f(w)} ${f(cyM + m)} ${f(w)} ${f(cyM)}C${f(w)} ${f(cyM - m)} ${f(e - c + w)} 0 ${f(e + w)} 0Z`;
  }
  const x = L - e;
  return `M${f(x)} 0C${f(x + c)} 0 ${f(L)} ${f(cyM - m)} ${f(L)} ${f(cyM)}C${f(L)} ${f(cyM + m)} ${f(x + c)} ${f(H)} ${f(x)} ${f(H)}`
    + `H${f(x - w)}C${f(x + c - w)} ${f(H)} ${f(L - w)} ${f(cyM + m)} ${f(L - w)} ${f(cyM)}C${f(L - w)} ${f(cyM - m)} ${f(x + c - w)} 0 ${f(x - w)} 0Z`;
}

/* A part that can be lit: circle (r) or pill (w × h), plus where its callout goes. */
type Spot = { id: string; x: number; y: number; r?: number; w?: number; h?: number; side: 'up' | 'down'; badge?: string };

function Defs({ u }: { u: string }) {
  return (
    <>
      <style>{CSS}</style>
      <defs>
        <radialGradient id={u + 'hl'}>
          <stop offset="0" style={{ stopColor: 'var(--art-accent, #5145e4)' }} stopOpacity=".62" />
          <stop offset=".45" style={{ stopColor: 'var(--art-accent, #5145e4)' }} stopOpacity=".26" />
          <stop offset="1" style={{ stopColor: 'var(--art-accent, #5145e4)' }} stopOpacity="0" />
        </radialGradient>
        <radialGradient id={u + 'shA'}>
          <stop offset="0" stopColor="#0a0c12" stopOpacity=".3" />
          <stop offset=".55" stopColor="#0a0c12" stopOpacity=".12" />
          <stop offset="1" stopColor="#0a0c12" stopOpacity="0" />
        </radialGradient>
        <radialGradient id={u + 'shC'}>
          <stop offset="0" stopColor="#05060a" stopOpacity=".55" />
          <stop offset=".6" stopColor="#05060a" stopOpacity=".22" />
          <stop offset="1" stopColor="#05060a" stopOpacity="0" />
        </radialGradient>
        <radialGradient id={u + 'back'}>
          <stop offset="0" stopColor="#dfe6ff" stopOpacity=".085" />
          <stop offset=".6" stopColor="#dfe6ff" stopOpacity=".03" />
          <stop offset="1" stopColor="#dfe6ff" stopOpacity="0" />
        </radialGradient>
      </defs>
    </>
  );
}

/** Glow + ring drawn on top of a part, in the part's own frame. */
function Lit({ u, s }: { u: string; s: Spot }) {
  if (s.r != null) {
    return (
      <g pointerEvents="none">
        <circle className="da-anim da-hl" cx={s.x} cy={s.y} r={s.r * 2.5 + 4} fill={`url(#${u}hl)`} />
        <circle cx={s.x} cy={s.y} r={s.r + 2.3} fill="none" className="da-hl-stroke" strokeWidth="1.5" />
        <circle cx={s.x} cy={s.y} r={s.r + 0.9} fill="none" stroke="#fff" strokeOpacity=".55" strokeWidth=".6" />
      </g>
    );
  }
  const w = s.w ?? 10, h = s.h ?? 6;
  return (
    <g pointerEvents="none">
      <ellipse className="da-anim da-hl" cx={s.x} cy={s.y} rx={w / 2 + 11} ry={h / 2 + 10} fill={`url(#${u}hl)`} />
      <rect x={s.x - w / 2 - 2.2} y={s.y - h / 2 - 2.2} width={w + 4.4} height={h + 4.4} rx={Math.min(h / 2 + 2.2, 8)}
        fill="none" className="da-hl-stroke" strokeWidth="1.4" />
    </g>
  );
}

/** Leader line + badge, in screen coordinates. */
function Callout({ from, to, text }: { from: Pt; to: Pt; text: string }) {
  const numeric = /^\d+$/.test(text);
  const cjk = /[一-鿿]/.test(text);
  const w = numeric ? (text.length > 1 ? 21 : 17) : Math.round(text.length * (cjk ? 10 : 6.4) + 13);
  return (
    <g className="da-anim da-pop" pointerEvents="none">
      <line x1={from[0]} y1={from[1]} x2={to[0]} y2={to[1]} className="da-hl-stroke" strokeWidth="1" strokeOpacity=".8" />
      <circle cx={from[0]} cy={from[1]} r="1.7" className="da-hl-fill" />
      <rect x={to[0] - w / 2} y={to[1] - 8.5} width={w} height="17" rx="8.5" className="da-hl-fill" />
      <rect x={to[0] - w / 2 + 1} y={to[1] - 7.6} width={w - 2} height="7" rx="3.5" fill="#fff" opacity=".16" />
      <text x={to[0]} y={to[1] + 3.7} textAnchor="middle" fontFamily={FONT} fontSize={numeric ? 10.5 : 9.5}
        fontWeight="700" fill="#fff">{text}</text>
    </g>
  );
}

/** Callout for a spot inside a rotated frame. */
function FrameCallout({ s, f, top, bottom }: { s: Spot; f: ReturnType<typeof frame>; top: number; bottom: number }) {
  const ext = (s.r ?? (s.h ?? 6) / 2) + 2.5;
  const up = s.side === 'up';
  return <Callout from={f.pt(s.x, up ? s.y - ext : s.y + ext)} to={f.pt(s.x, up ? top : bottom)} text={s.badge ?? BADGE[s.id] ?? s.id} />;
}

/* ── Receivers ──────────────────────────────────────────────────────────── */

/** Baseus USB-A + Type-C receiver (lives in the pen's tail). Local box 62 × 21, USB-A to the right. */
function DualReceiver({ u }: { u: string }) {
  return (
    <g>
      <defs>
        <linearGradient id={u + 'rxm'} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#f6f7f9" />
          <stop offset=".45" stopColor="#c9cdd4" />
          <stop offset="1" stopColor="#8d929b" />
        </linearGradient>
        <linearGradient id={u + 'rxb'} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#5b5e66" />
          <stop offset=".3" stopColor="#3c3e45" />
          <stop offset="1" stopColor="#25262b" />
        </linearGradient>
      </defs>
      <ellipse className="da-con" cx="32" cy="20" rx="34" ry="4.6" fill={`url(#${u}shC)`} />
      {/* near walls */}
      <rect x="41" y="6.5" width="21" height="12" rx="1.4" fill="#6f747d" />
      <rect x="2.5" y="8.5" width="16" height="7" rx="3.5" fill="#6f747d" />
      <rect x="17" y="3.5" width="26" height="17" rx="3.5" fill="#17181b" />
      <path d="M21 3.2H6.2a4.6 4.6 0 0 0-4.6 4.6v5.2a4.6 4.6 0 0 0 4.6 4.6H21" stroke="#1d1e22" strokeWidth="2.4" fill="none" />
      {/* USB-A plug */}
      <rect x="41" y="2.5" width="21" height="12" rx="1.4" fill={`url(#${u}rxm)`} />
      <rect x="53.4" y="4.6" width="3.6" height="2.6" rx=".5" fill="#4d525b" />
      <rect x="53.4" y="9.8" width="3.6" height="2.6" rx=".5" fill="#4d525b" />
      <rect x="42" y="3.2" width="19" height=".8" rx=".4" fill="#fff" opacity=".8" />
      {/* Type-C plug */}
      <rect x="2.5" y="5" width="16" height="7" rx="3.5" fill={`url(#${u}rxm)`} />
      <rect x="4.6" y="7.9" width="7" height="1.2" rx=".6" fill="#3e424a" opacity=".75" />
      {/* body */}
      <rect x="17" y="0" width="26" height="17" rx="3.5" fill={`url(#${u}rxb)`} />
      <rect x="19.5" y=".9" width="21" height=".9" rx=".45" fill="#fff" opacity=".24" />
      <rect x="19.5" y="15.5" width="21" height=".6" rx=".3" fill="#9aa1ad" opacity=".22" />
      {/* swivel bracket guarding the Type-C end, on its axle */}
      <path d="M21 1.4H6.2a4.6 4.6 0 0 0-4.6 4.6v5a4.6 4.6 0 0 0 4.6 4.6H21" stroke={`url(#${u}rxb)`} strokeWidth="2.4" fill="none" />
      <path d="M20.5 .6H6.2a5.4 5.4 0 0 0-5.1 3.6" stroke="#fff" strokeOpacity=".26" strokeWidth=".7" fill="none" strokeLinecap="round" />
      <circle cx="22.6" cy="8.5" r="2.3" fill="#303136" stroke="#55585f" strokeWidth=".6" />
    </g>
  );
}

/** Generic nano receiver (PV60). Local box 40 × 16.5, USB-A to the right. */
function NanoReceiver({ u }: { u: string }) {
  return (
    <g>
      <defs>
        <linearGradient id={u + 'nrm'} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#f3f4f7" />
          <stop offset=".5" stopColor="#c3c7cf" />
          <stop offset="1" stopColor="#8a8f98" />
        </linearGradient>
        <linearGradient id={u + 'nrb'} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#4a4f5c" />
          <stop offset=".35" stopColor="#22252e" />
          <stop offset="1" stopColor="#0f1116" />
        </linearGradient>
      </defs>
      <ellipse className="da-con" cx="20" cy="16" rx="22" ry="3.8" fill={`url(#${u}shC)`} />
      <rect x="22" y="4.5" width="18" height="11" rx="1.2" fill="#6c717a" />
      <rect x="0" y="3.5" width="25" height="13" rx="4" fill="#08090c" />
      <rect x="22" y="1" width="18" height="11" rx="1.2" fill={`url(#${u}nrm)`} />
      <rect x="32" y="3" width="3.2" height="2.4" rx=".4" fill="#4d525b" />
      <rect x="32" y="7.6" width="3.2" height="2.4" rx=".4" fill="#4d525b" />
      <rect x="23" y="1.7" width="16" height=".7" rx=".35" fill="#fff" opacity=".75" />
      <rect x="0" y="0" width="25" height="13" rx="4" fill={`url(#${u}nrb)`} />
      <rect x="2.5" y=".9" width="20" height="1.3" rx=".65" fill="#fff" opacity=".22" />
      <rect x="2.5" y="15.2" width="20" height=".7" rx=".35" fill="#8f97aa" opacity=".22" />
    </g>
  );
}

/** Standalone receiver illustration for device lists. */
export function ReceiverArt({ className, model = 'baseus' }: ArtProps & { model?: 'baseus' | 'pv60' }) {
  const u = useUid();
  const dual = model !== 'pv60';
  return (
    <svg viewBox="0 0 96 56" className={['da', className].filter(Boolean).join(' ')} role="img"
      aria-label={dual ? '倍思 USB-A / Type-C 双头接收器插画' : '汉王 PV60 接收器插画'}>
      <Defs u={u} />
      <ellipse className="da-back" cx="48" cy="28" rx="48" ry="28" fill={`url(#${u}back)`} />
      <g transform={dual ? 'translate(48 28) rotate(-12) scale(1.12) translate(-31 -10.5)' : 'translate(48 28) rotate(-12) scale(1.3) translate(-20 -8.25)'}>
        {dual ? <DualReceiver u={u} /> : <NanoReceiver u={u} />}
      </g>
    </svg>
  );
}

/* Both pens share one camera: the top face at its true width, the near side wall
   at BAR_WALL units for the Baseus. */
const BAR_WALL = 19;
/* Bead-blasted aluminium cross-section, in units from the far edge (y): the far
   edge rolls away, crest highlight, flat top, near edge rolls into the wall, bounce. */
const BAR_FACE: [number, string][] = (() => {
  const W = 45, H = 45 + BAR_WALL;
  return [
    [0, '#7d838e'], [1.4, '#b7bcc4'], [3.3, '#eceff2'], [6.2, '#c7ccd3'], [11, '#a9afb8'], [W * 0.55, '#99a0aa'],
    [W * 0.84, '#8a909b'], [W - 1.5, '#7d838e'], [W + 1.2, '#6c727d'], [W + 4, '#5d636d'], [W + BAR_WALL * 0.5, '#4e535d'],
    [H - 3.2, '#40444c'], [H - 1.5, '#555a63'], [H, '#2b2e34'],
  ];
})();

/* ── Baseus BS-OH085 ────────────────────────────────────────────────────── */
/* A slim bar, about 142 × 24 mm on the top face (6 : 1), key layout from the
   vendor's operation diagram, laid with the laser end to the upper right so the
   near side wall shows ⑧ ⑨⑩ ⑥⑦; ⑪ and Type-C are on the far wall and only peek
   over the edge. The aluminium bar has a rounded
   profile: the long sides bow out a little and roll over into the side walls, the
   ends are rounded, and the glossy dark key panel wraps over the laser end. The tail
   end face is flush, with the receiver storage bay recessed into it. */

const BL = 272, BW = 45, BD = BAR_WALL, BH = BW + BD, BE = 10, BB = 1.4, BRD = 0.8;
const bB = (x: number) => bowAt(BL, BE, BB, x);
const BC = BW / 2;        // centre line of the top face
const BS = BW + BD * 0.5; // centre line of the near-wall keys
const BKH = 6;            // near-wall key height
const BKR = 5.9;          // ③ / ① key radius
const BAY = { y: BH / 2 - 17, w: 3.1, h: 34 }; // receiver storage bay in the tail end face
const BASEUS_SPOTS: Spot[] = [
  { id: 'pageup', x: 245.9, y: BC - 11, r: BKR, side: 'up' },
  { id: 'ok', x: 245.9, y: BC + 11, r: BKR, side: 'down' },
  { id: 'pagedown', x: 217, y: BC, w: 20.7, h: 14, side: 'up' },
  { id: 'airmouse', x: 188, y: BC, w: 20.7, h: 14, side: 'up' },
  { id: 'laser', x: 141.4, y: BC, r: 9.8, side: 'up' },
  { id: 'led', x: 260.8, y: BC, r: 3.4, side: 'up' },
  { id: 'annotate', x: 231.2, y: BS + bB(231.2), w: 19, h: BKH, side: 'down' },
  { id: 'volup', x: 202.2, y: BS + bB(202.2), w: 14, h: BKH, side: 'down' },
  { id: 'voldown', x: 187.8, y: BS + bB(187.8), w: 14, h: BKH, side: 'down' },
  { id: 'record', x: 159.1, y: BS + bB(159.1), w: 19, h: BKH, side: 'down' },
  { id: 'translate', x: 137.4, y: BS + bB(137.4), w: 19, h: BKH, side: 'down' },
  { id: 'switch', x: 162.6, y: -0.9 - bB(162.6), w: 24, h: 3.6, side: 'up' },
  { id: 'usbc', x: 231.2, y: -0.9 - bB(231.2), w: 25, h: 3.6, side: 'up' },
  { id: 'mic', x: BL - 1, y: BC, r: 4.5, side: 'up' },
];

function SideKey({ u, x, w, split }: { u: string; x: number; w: number; split?: boolean }) {
  const y = BS + bB(x), h = BKH;
  return (
    <g>
      <rect x={x - w / 2 - 0.8} y={y - h / 2 - 0.8} width={w + 1.6} height={h + 1.6} rx={h / 2 + 0.8} fill="#1d1f24" opacity=".7" />
      <rect x={x - w / 2} y={y - h / 2} width={w} height={h} rx={h / 2} fill={`url(#${u}sk)`} />
      <rect x={x - w / 2 + 1.6} y={y - h / 2 + 0.5} width={w - 3.2} height=".9" rx=".45" fill="#fff" opacity=".22" />
      <rect x={x - w / 2 + 2} y={y + h / 2 + 0.7} width={w - 4} height=".6" rx=".3" fill="#fff" opacity=".12" />
      {split && <rect x={x - 0.35} y={y - h / 2} width=".7" height={h} fill="#16171b" opacity=".85" />}
    </g>
  );
}

export function BaseusArt({ className, highlight }: ArtProps) {
  const u = useUid();
  const part = artPart(highlight, 'baseus');
  const f = frame(176, 92, BL, BH, -14);
  const spot = BASEUS_SPOTS.find((s) => s.id === part);
  const body = barPath(BL, BH, BE, BB, BRD);
  /* glossy key panel: from a rounded tongue at x 181 over the laser end */
  const pT = 2.4, pB = BW - 1.3, px = 181;
  const yT = pT - bB(px), yB = pB + bB(px);
  const panel = `M${px} ${n2(yB)}A16.5 ${n2((yB - yT) / 2)} 0 0 1 ${px} ${n2(yT)}`
    + sideQ(BL, BE, BB, pT, px, BL - BE) + `H${BL + 4}V${pB}H${BL - BE}` + sideQ(BL, BE, -BB, pB, BL - BE, px) + 'Z';
  const sy = (x: number) => BS + bB(x); // near-wall centre line
  /* tail end face: a crescent between the rounded end and a line ~7 units in */
  const tm = (BH / 2) * BRD, tc = BE * 0.92, tw = 7, tcy = BH / 2;
  const tailCrease = `M${BE} 0C${BE + 0.6} 1 ${tw} ${n2(tcy - tm)} ${tw} ${tcy}C${tw} ${n2(tcy + tm)} ${BE + 0.6} ${BH - 1} ${BE} ${BH}`;
  const tailFace = `M${BE} 0C${n2(BE - tc)} 0 0 ${n2(tcy - tm)} 0 ${tcy}C0 ${n2(tcy + tm)} ${n2(BE - tc)} ${BH} ${BE} ${BH}`
    + `C${BE + 0.6} ${BH - 1} ${tw} ${n2(tcy + tm)} ${tw} ${tcy}C${tw} ${n2(tcy - tm)} ${BE + 0.6} 1 ${BE} 0Z`;
  return (
    <svg viewBox={PEN_BOX} className={['da', className].filter(Boolean).join(' ')} role="img"
      aria-label={label('倍思 BS-OH085 翻页笔插画', part)}>
      <Defs u={u} />
      <defs>
        {/* cross-section: far edge rolls away, crest highlight, flat top, near edge rolls into the wall, bounce */}
        <linearGradient id={u + 'face'} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2={BH}>
          {BAR_FACE.map(([y, c]) => <stop key={y} offset={n2(y / BH)} stopColor={c} />)}
        </linearGradient>
        <radialGradient id={u + 'sheen'} gradientUnits="userSpaceOnUse" cx="78" cy="16" r="120"
          gradientTransform="translate(78 16) scale(1 .26) translate(-78 -16)">
          <stop offset="0" stopColor="#fff" stopOpacity=".42" />
          <stop offset=".5" stopColor="#fff" stopOpacity=".12" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </radialGradient>
        {/* the rounded ends turn towards / away from the key light */}
        <linearGradient id={u + 'ends'} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2={BL} y2="0">
          <stop offset="0" stopColor="#000" stopOpacity=".22" />
          <stop offset=".012" stopColor="#fff" stopOpacity=".05" />
          <stop offset=".03" stopColor="#fff" stopOpacity=".22" />
          <stop offset=".07" stopColor="#fff" stopOpacity="0" />
          <stop offset=".9" stopColor="#000" stopOpacity="0" />
          <stop offset=".965" stopColor="#000" stopOpacity=".12" />
          <stop offset="1" stopColor="#000" stopOpacity=".3" />
        </linearGradient>
        <linearGradient id={u + 'tail'} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2={BH}>
          <stop offset=".04" stopColor="#d9dde2" />
          <stop offset=".45" stopColor="#bcc1c9" />
          <stop offset=".8" stopColor="#959ba5" />
          <stop offset="1" stopColor="#6d737d" />
        </linearGradient>
        <linearGradient id={u + 'bayrim'} x1="0" y1="0" x2=".6" y2="1">
          <stop offset="0" stopColor="#f4f6f8" />
          <stop offset=".6" stopColor="#d3d7dd" />
          <stop offset="1" stopColor="#8e949e" />
        </linearGradient>
        <linearGradient id={u + 'bay'} x1="0" y1="0" x2="1" y2=".25">
          <stop offset="0" stopColor="#050608" />
          <stop offset=".6" stopColor="#15171b" />
          <stop offset="1" stopColor="#3b3f46" />
        </linearGradient>
        <linearGradient id={u + 'sk'} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#4d5058" />
          <stop offset="1" stopColor="#2b2d33" />
        </linearGradient>
        <linearGradient id={u + 'edge'} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2={BL} y2="0">
          <stop offset="0" stopColor="#fff" stopOpacity=".95" />
          <stop offset=".55" stopColor="#fff" stopOpacity=".6" />
          <stop offset="1" stopColor="#fff" stopOpacity=".18" />
        </linearGradient>
        <linearGradient id={u + 'panel'} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2={BW}>
          <stop offset="0" stopColor="#5d5f66" />
          <stop offset=".12" stopColor="#4d4f56" />
          <stop offset=".6" stopColor="#3d3f45" />
          <stop offset=".9" stopColor="#323439" />
          <stop offset="1" stopColor="#3b3d43" />
        </linearGradient>
        <linearGradient id={u + 'gloss'} gradientUnits="userSpaceOnUse" x1="0" y1="2" x2="0" y2={BW}>
          <stop offset="0" stopColor="#fff" stopOpacity=".2" />
          <stop offset=".14" stopColor="#fff" stopOpacity=".07" />
          <stop offset=".3" stopColor="#fff" stopOpacity="0" />
          <stop offset=".92" stopColor="#fff" stopOpacity="0" />
          <stop offset="1" stopColor="#fff" stopOpacity=".07" />
        </linearGradient>
        <linearGradient id={u + 'well'} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#24252a" />
          <stop offset=".5" stopColor="#313338" />
          <stop offset="1" stopColor="#47494f" />
        </linearGradient>
        <radialGradient id={u + 'key'} cx=".38" cy=".3" r=".85">
          <stop offset="0" stopColor="#64666d" />
          <stop offset=".55" stopColor="#4a4c52" />
          <stop offset="1" stopColor="#38393f" />
        </radialGradient>
        <linearGradient id={u + 'ledg'} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffc189" />
          <stop offset="1" stopColor="#ff7417" />
        </linearGradient>
        <radialGradient id={u + 'ledglow'}>
          <stop offset="0" stopColor="#ff8a2a" stopOpacity=".7" />
          <stop offset=".5" stopColor="#ff8a2a" stopOpacity=".18" />
          <stop offset="1" stopColor="#ff8a2a" stopOpacity="0" />
        </radialGradient>
        <radialGradient id={u + 'orange'} cx=".4" cy=".35" r=".75">
          <stop offset="0" stopColor="#ff9a66" />
          <stop offset=".6" stopColor="#ff5a24" />
          <stop offset="1" stopColor="#e2400f" />
        </radialGradient>
        <linearGradient id={u + 'sweep'} x1="0" y1="0" x2="1" y2=".32">
          <stop offset="0" stopColor="#fff" stopOpacity="0" />
          <stop offset=".5" stopColor="#fff" stopOpacity=".55" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
        <clipPath id={u + 'fc'}><path d={body} /></clipPath>
      </defs>

      <ellipse className="da-back" cx="175" cy="95" rx="168" ry="92" fill={`url(#${u}back)`} />

      <g transform={f.transform}>
        {/* shadows */}
        <ellipse className="da-amb" cx={BL / 2 + 8} cy={BH + 5} rx={BL / 2 + 16} ry="17" fill={`url(#${u}shA)`} />
        <ellipse className="da-con" cx={BL / 2 + 2} cy={BH} rx={BL / 2 - 2} ry="4.6" fill={`url(#${u}shC)`} />

        {/* far wall: only the ⑪ slide switch peeks over the edge (Type-C is recessed) */}
        <g transform={`translate(0 ${n2(-bB(162.6))})`}>
          <rect x="151.6" y="-1.8" width="22" height="4" rx="1.6" fill="#41444b" />
          <rect x="152.6" y="-1.4" width="4" height="1.7" rx=".85" fill="#ff6a2a" opacity=".9" />
          <rect x="157.4" y="-1.55" width="8.4" height="2.1" rx="1.05" fill="#8d939d" />
        </g>

        {/* the aluminium bar: one rounded silhouette, shaded by its cross-section */}
        <path d={body} fill={`url(#${u}face)`} />
        <path d={body} fill={`url(#${u}sheen)`} />
        <path d={body} fill={`url(#${u}ends)`} />
        <g clipPath={`url(#${u}fc)`}>
          <rect className="da-anim da-sweep" x="-86" y="-6" width="66" height={BH + 12} fill={`url(#${u}sweep)`} />
          {/* flush tail end face, turned to the key light, with the recessed receiver bay */}
          <path d={tailFace} fill={`url(#${u}tail)`} />
          <path d={tailCrease} stroke="#2f333a" strokeOpacity=".4" strokeWidth=".7" fill="none" />
          <path d={tailCrease} transform="translate(.8 0)" stroke="#fff" strokeOpacity=".35" strokeWidth=".5" fill="none" />
          <rect x="1.3" y={BAY.y - 0.6} width={BAY.w + 1.2} height={BAY.h + 1.2} rx={BAY.w / 2 + 0.6} fill={`url(#${u}bayrim)`} />
          <rect x="1.9" y={BAY.y} width={BAY.w} height={BAY.h} rx={BAY.w / 2} fill={`url(#${u}bay)`} />
          <rect x="2.3" y={BAY.y + 1.6} width=".7" height={BAY.h - 3.2} rx=".35" fill="#000" opacity=".55" />
        </g>
        {/* highlights follow the bowed sides: crest of the far edge, near edge, bounce under the wall */}
        <path d={sideLine(BL, BE, BB, 3.2, BE + 1, BL - BE - 2)} stroke="#fff" strokeOpacity=".22" strokeWidth="3.4" fill="none" strokeLinecap="round" />
        <path d={sideLine(BL, BE, BB, 2.9, BE + 2, BL - BE - 6)} stroke={`url(#${u}edge)`} strokeWidth="1.15" fill="none" strokeLinecap="round" />
        <path d={sideLine(BL, BE, -BB, BW - 0.8, BE + 4, BL - BE - 4)} stroke="#fff" strokeOpacity=".07" strokeWidth="3" fill="none" strokeLinecap="round" />
        <path d={sideLine(BL, BE, -BB, BH - 1.5, BE + 3, BL - BE - 3)} stroke="#d4d9e0" strokeOpacity=".3" strokeWidth=".8" fill="none" strokeLinecap="round" />
        <path className="da-back" d={sideLine(BL, BE, BB, 0.6, BE + 2, BL - BE - 2)} stroke="#e6ecff" strokeOpacity=".35" strokeWidth=".8" fill="none" strokeLinecap="round" />

        {/* near wall with ⑧ ⑨⑩ ⑥⑦ */}
        <SideKey u={u} x={231.2} w={19} />
        <SideKey u={u} x={195} w={28.8} split />
        <SideKey u={u} x={148.3} w={40.8} split />
        <g stroke="#d6dae1" strokeOpacity=".55" strokeWidth=".75" fill="none" strokeLinecap="round">
          {/* printed glyphs: pen beside ⑧, mic and translate after ⑦ */}
          <path d={`M245.5 ${n2(sy(245.5) + 2.3)}l3.4-3`} />
          <g transform={`translate(0 ${n2(sy(121) - 64.6)})`}>
            <path d="M121.6 62.9v2.4M120 64.6a1.6 1.6 0 0 0 3.2 0M121.6 66.3v.9" />
            <circle cx="112.6" cy="64.6" r="2" />
            <path d="M111.6 63.9h2M112.6 63.7v.2M111.9 65.5l1.4-1.4" />
          </g>
        </g>

        {/* glossy key panel, wrapping over the laser end */}
        <g clipPath={`url(#${u}fc)`}>
          <path d={panel} fill={`url(#${u}panel)`} stroke="#15161a" strokeOpacity=".5" strokeWidth=".8" />
          <path d={panel} fill={`url(#${u}gloss)`} />
          {/* dark end cap with the sound pickup port and the laser aperture */}
          <path d={endBand(BL, BH, BE, BRD, 3.2, 'right')} fill="#2e3035" />
          <path d={endBand(BL, BH, BE, BRD, 0.7, 'right')} transform="translate(-3.2 0)" fill="#fff" opacity=".1" />
          <path d={body} fill={`url(#${u}ends)`} opacity=".6" />
        </g>
        <path d={sideLine(BL, BE, BB, pT + 1, 186, BL - BE - 2)} stroke="#fff" strokeOpacity=".2" strokeWidth=".8" fill="none" strokeLinecap="round" />

        {/* status LED */}
        <ellipse className="da-anim da-led" cx="260.8" cy={BC} rx="6" ry="7.5" fill={`url(#${u}ledglow)`} />
        <rect x="259.9" y={BC - 3} width="1.9" height="6" rx=".95" fill={`url(#${u}ledg)`} />

        {/* ③ page up and ① OK */}
        {[BC - 11, BC + 11].map((y) => (
          <g key={y}>
            <circle cx="245.9" cy={y} r={BKR + 0.8} fill="#1e1f23" opacity=".85" />
            <circle cx="245.9" cy={y} r={BKR} fill={`url(#${u}key)`} />
            <path d={`M241.2 ${y - 1.2}a4.9 4.9 0 0 1 6.5-3.9`} stroke="#fff" strokeOpacity=".24" strokeWidth=".8" fill="none" strokeLinecap="round" />
          </g>
        ))}
        <path d={`M243.6 ${BC - 9.8}l2.3-2.5 2.3 2.5`} stroke="#eef0f3" strokeWidth="1.2" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        <rect x="244.3" y={BC + 8.5} width="3.2" height="5" rx="1.6" stroke="#eef0f3" strokeWidth="1.05" fill="none" />
        <path d={`M245.9 ${BC + 8.8}v4.4`} stroke="#eef0f3" strokeWidth=".85" />

        {/* recessed well with ④ page down and ⑤ air mouse */}
        <rect x="174.1" y={BC - 12.8} width="59.3" height="25.6" rx="10.5" fill={`url(#${u}well)`} />
        <rect x="174.6" y={BC - 12.3} width="58.3" height="24.6" rx="10" fill="none" stroke="#000" strokeOpacity=".3" strokeWidth="1" />
        <path d={`M180 ${BC + 12.4}H227.5`} stroke="#fff" strokeOpacity=".1" strokeWidth=".8" strokeLinecap="round" />
        {[217, 188].map((x) => (
          <g key={x}>
            <rect x={x - 10.35} y={BC - 6} width="20.7" height="14" rx="5" fill="#000" opacity=".3" />
            <rect x={x - 10.35} y={BC - 7} width="20.7" height="14" rx="5" fill={`url(#${u}key)`} />
            <rect x={x - 8.2} y={BC - 6.3} width="16.4" height=".9" rx=".45" fill="#fff" opacity=".24" />
          </g>
        ))}
        <g transform={`translate(0 ${BC - 29})`}>
          <path d="M214.6 27.8l2.4 2.5 2.4-2.5" stroke="#f2f4f7" strokeWidth="1.3" fill="none" strokeLinecap="round" strokeLinejoin="round" />
          <rect x="184" y="26.4" width="8" height="5.6" rx="2.8" stroke="#f2f4f7" strokeWidth="1.05" fill="none" />
          <path d="M188.8 26.6v5.2M188.8 29.2h3" stroke="#f2f4f7" strokeWidth=".85" />
        </g>

        {/* ② laser */}
        <circle cx="141.4" cy={BC + 0.8} r="10.8" fill="#000" opacity=".16" />
        <circle cx="141.4" cy={BC} r="10.5" fill="#3a3d44" />
        <circle cx="141.4" cy={BC} r="9.8" fill={`url(#${u}key)`} />
        <path d={`M133.4 ${BC - 2.4}a8.4 8.4 0 0 1 11-5.6`} stroke="#fff" strokeOpacity=".28" strokeWidth=".9" fill="none" strokeLinecap="round" />
        <circle cx="141.4" cy={BC} r="4.4" fill={`url(#${u}orange)`} />
        <circle cx="141.4" cy={BC} r="1.9" fill="#3c3e44" />
        <circle cx="139.8" cy={BC - 1.7} r="1" fill="#fff" opacity=".55" />

        {spot && <Lit u={u} s={spot} />}
      </g>

      {/* the receiver, pulled out and set down in front */}
      <g transform="translate(284 154) rotate(-9) scale(.9) translate(-31 -10.5)">
        <DualReceiver u={u} />
        {part === 'receiver' && <Lit u={u} s={{ id: 'receiver', x: 31, y: 8.5, w: 62, h: 17, side: 'down' }} />}
      </g>
      {part === 'receiver' && <Callout from={[262, 160]} to={[228, 172]} text={BADGE.receiver} />}
      {spot && <FrameCallout s={spot} f={f} top={-16} bottom={BH + 20} />}
    </svg>
  );
}

/* ── Hanvon PV60 ────────────────────────────────────────────────────────── */
/* From the vendor's product images: a slim matte black-navy bar, 138 × 23 × 12 mm,
   rounded long edges and a flat-ish top, drawn at the Baseus' scale. Laid like the
   Baseus: the top end (laser window) to the upper right, the tail (IR signal
   window) to the lower left, so the pen's left side — ON/OFF slide switch, then the
   micro-USB port — is the far wall: the slider stands proud of the edge, the port
   shows as a slit on the rounded far edge. On the top face from the top end: the
   silicon mic pinhole, the round voice key, the page up / page down rocker and the
   round laser key. Key glyphs turn with the pen (page up points to the top end). */

const PL = 264, PW = 42, PD = 16, PH = PW + PD, PE = 7, PB = 0.6, PRD = 0.82;
const bP = (x: number) => bowAt(PL, PE, PB, x);
const PC = PW / 2; // centre line of the top face
/* along the pen, measured from the top end (mm × 1.915) */
const PK = { pin: PL - 19, voice: PL - 43.3, rocker: PL - 81.7, laser: PL - 119.3, sw: PL - 62.2, port: PL - 82.1 };
const PKR = 10.2, ROCK_W = 46, ROCK_H = 20.5;
const PV60_SPOTS: Spot[] = [
  { id: 'mic', x: PK.pin, y: PC, r: 2.2, side: 'up' },
  { id: 'record', x: PK.voice, y: PC, r: PKR, side: 'up' },
  { id: 'pageup', x: PK.rocker + ROCK_W / 4, y: PC, r: 8.6, side: 'up' },
  { id: 'pagedown', x: PK.rocker - ROCK_W / 4, y: PC, r: 8.6, side: 'up' },
  { id: 'laser', x: PK.laser, y: PC, r: PKR, side: 'up' },
  { id: 'switch', x: PK.sw, y: -0.4 - bP(PK.sw), w: 14, h: 3.6, side: 'up' },
  { id: 'usbc', x: PK.port, y: 0.6 - bP(PK.port), w: 13, h: 3, side: 'up', badge: 'Micro-USB' },
];
const PV60_SPOKEN: Record<string, string> = {
  ...SPOKEN, record: '语音键', pageup: '上翻键', pagedown: '下翻键', laser: '激光键', switch: '开关',
  usbc: 'Micro-USB 充电口', mic: '硅麦拾音孔',
};

/** A slightly recessed key: dark pocket, domed cap, rim light. w = h for a round key. */
function Pv60Key({ u, x, w, h }: { u: string; x: number; w: number; h: number }) {
  const r = h / 2;
  return (
    <g>
      <rect x={x - w / 2 - 1.6} y={PC - r - 1.6} width={w + 3.2} height={h + 3.2} rx={r + 1.6} fill={`url(#${u}pocket)`} />
      <rect x={x - w / 2 + 0.3} y={PC - r + 0.8} width={w} height={h} rx={r} fill="#000" opacity=".5" />
      <rect x={x - w / 2} y={PC - r} width={w} height={h} rx={r} fill={`url(#${u}cap)`} />
      <rect x={x - w / 2 + 0.5} y={PC - r + 0.5} width={w - 1} height={h - 1} rx={r - 0.5} fill="none" stroke={`url(#${u}rim)`} strokeWidth=".8" />
    </g>
  );
}

export function Pv60Art({ className, highlight }: ArtProps) {
  const u = useUid();
  const part = artPart(highlight, 'pv60');
  const f = frame(176, 92, PL, PH, -14);
  const spot = PV60_SPOTS.find((s) => s.id === part);
  const body = barPath(PL, PH, PE, PB, PRD);
  const ink = '#d3d6dc';
  const swY = -bP(PK.sw), poY = -bP(PK.port); // far edge at the switch / port
  return (
    <svg viewBox={PEN_BOX} className={['da', className].filter(Boolean).join(' ')} role="img"
      aria-label={label('汉王 PV60 翻页笔插画', part, PV60_SPOKEN)}>
      <Defs u={u} />
      <defs>
        {/* matte soft-touch cross-section, black with a navy cast */}
        <linearGradient id={u + 'face'} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2={PH}>
          <stop offset="0" stopColor="#161920" />
          <stop offset=".028" stopColor="#3a404d" />
          <stop offset=".062" stopColor="#5a6272" />
          <stop offset=".11" stopColor="#3d4350" />
          <stop offset=".21" stopColor="#2e333e" />
          <stop offset=".45" stopColor="#272b35" />
          <stop offset=".65" stopColor="#21252e" />
          <stop offset=".72" stopColor="#1c1f27" />
          <stop offset=".78" stopColor="#16181f" />
          <stop offset=".88" stopColor="#111319" />
          <stop offset=".95" stopColor="#0d0e13" />
          <stop offset=".975" stopColor="#1f232c" />
          <stop offset="1" stopColor="#08090c" />
        </linearGradient>
        <radialGradient id={u + 'sheen'} gradientUnits="userSpaceOnUse" cx="70" cy="16" r="120"
          gradientTransform="translate(70 16) scale(1 .28) translate(-70 -16)">
          <stop offset="0" stopColor="#c9d4ff" stopOpacity=".16" />
          <stop offset=".5" stopColor="#c9d4ff" stopOpacity=".05" />
          <stop offset="1" stopColor="#c9d4ff" stopOpacity="0" />
        </radialGradient>
        <linearGradient id={u + 'ends'} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2={PL} y2="0">
          <stop offset="0" stopColor="#fff" stopOpacity=".04" />
          <stop offset=".022" stopColor="#fff" stopOpacity=".12" />
          <stop offset=".06" stopColor="#fff" stopOpacity="0" />
          <stop offset=".92" stopColor="#000" stopOpacity="0" />
          <stop offset="1" stopColor="#000" stopOpacity=".35" />
        </linearGradient>
        {/* dark glass windows in the ends: IR receiver in the tail (left), laser at the top end */}
        <linearGradient id={u + 'win'} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="0" y2={PH}>
          <stop offset=".1" stopColor="#030304" stopOpacity="0" />
          <stop offset=".2" stopColor="#030304" stopOpacity=".92" />
          <stop offset=".62" stopColor="#030304" stopOpacity=".92" />
          <stop offset=".74" stopColor="#030304" stopOpacity="0" />
        </linearGradient>
        <linearGradient id={u + 'edge'} gradientUnits="userSpaceOnUse" x1="0" y1="0" x2={PL} y2="0">
          <stop offset="0" stopColor="#dfe6ff" stopOpacity=".7" />
          <stop offset=".5" stopColor="#dfe6ff" stopOpacity=".42" />
          <stop offset="1" stopColor="#dfe6ff" stopOpacity=".12" />
        </linearGradient>
        <linearGradient id={u + 'pocket'} x1="0" y1="0" x2=".3" y2="1">
          <stop offset="0" stopColor="#050609" />
          <stop offset=".55" stopColor="#0d0f14" />
          <stop offset="1" stopColor="#363b46" />
        </linearGradient>
        <radialGradient id={u + 'cap'} cx=".36" cy=".28" r=".9">
          <stop offset="0" stopColor="#555a66" />
          <stop offset=".55" stopColor="#3a3e48" />
          <stop offset="1" stopColor="#2b2e36" />
        </radialGradient>
        <linearGradient id={u + 'rim'} x1="0" y1="0" x2=".7" y2="1">
          <stop offset="0" stopColor="#fff" stopOpacity=".42" />
          <stop offset=".5" stopColor="#fff" stopOpacity=".06" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
        <linearGradient id={u + 'knob'} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#6d7380" />
          <stop offset="1" stopColor="#2f333b" />
        </linearGradient>
        <linearGradient id={u + 'sweep'} x1="0" y1="0" x2="1" y2=".32">
          <stop offset="0" stopColor="#fff" stopOpacity="0" />
          <stop offset=".5" stopColor="#fff" stopOpacity=".16" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
        <clipPath id={u + 'fc'}><path d={body} /></clipPath>
      </defs>

      <ellipse className="da-back" cx="175" cy="95" rx="168" ry="92" fill={`url(#${u}back)`} />

      <g transform={f.transform}>
        <ellipse className="da-amb" cx={PL / 2 + 8} cy={PH + 5} rx={PL / 2 + 16} ry="16" fill={`url(#${u}shA)`} />
        <ellipse className="da-con" cx={PL / 2 + 2} cy={PH} rx={PL / 2 - 3} ry="4.4" fill={`url(#${u}shC)`} />

        {/* body */}
        <path d={body} fill={`url(#${u}face)`} />
        <path d={body} fill={`url(#${u}sheen)`} />
        <path d={body} fill={`url(#${u}ends)`} />
        <g clipPath={`url(#${u}fc)`}>
          <rect className="da-anim da-sweep" x="-80" y="-6" width="56" height={PH + 12} fill={`url(#${u}sweep)`} />
          <path d={endBand(PL, PH, PE, PRD, 2.6, 'left')} fill={`url(#${u}win)`} />
          <path d={endBand(PL, PH, PE, PRD, 3, 'right')} fill={`url(#${u}win)`} />
        </g>
        <circle cx="2.2" cy="14" r=".7" fill="#fff" opacity=".45" />
        <circle cx={PL - 1.6} cy="14.6" r=".55" fill="#fff" opacity=".25" />
        {/* soft crest along the far rounded edge, bounce under the wall */}
        <path d={sideLine(PL, PE, PB, 3.1, PE + 1, PL - PE - 1)} stroke="#c9d4ff" strokeOpacity=".1" strokeWidth="3.6" fill="none" strokeLinecap="round" />
        <path d={sideLine(PL, PE, PB, 2.8, PE + 2, PL - PE - 6)} stroke={`url(#${u}edge)`} strokeWidth="1" fill="none" strokeLinecap="round" />
        <path d={sideLine(PL, PE, -PB, PH - 1.4, PE + 3, PL - PE - 3)} stroke="#9aa4bb" strokeOpacity=".28" strokeWidth=".8" fill="none" strokeLinecap="round" />
        {/* dark themes: a cool rim so the black pen separates from the panel */}
        <path className="da-back" d={body} fill="none" stroke="#d6e0ff" strokeOpacity=".16" strokeWidth=".9" />
        <path className="da-back" d={sideLine(PL, PE, PB, 0.7, PE + 2, PL - PE - 2)} stroke="#e6ecff" strokeOpacity=".3" strokeWidth=".8" fill="none" strokeLinecap="round" />

        {/* far wall (the pen's left side): the micro-USB port is a slit on the rounded
            edge, the ON/OFF slider (ON towards the top end) stands proud of it */}
        <rect x={PK.port - 6.2} y={n2(poY + 0.35)} width="12.4" height="1.6" rx=".8" fill="#030304" />
        <rect x={PK.port - 3.8} y={n2(poY + 0.85)} width="7.6" height=".6" rx=".3" fill="#4a505c" />
        <path d={`M${PK.port - 5.4} ${n2(poY + 2.6)}h10.8`} stroke="#9aa4bb" strokeOpacity=".22" strokeWidth=".5" strokeLinecap="round" />
        <rect x={PK.sw - 7} y={n2(swY + 0.3)} width="14" height="1.7" rx=".85" fill="#040507" />
        <path d={`M${PK.sw - 6} ${n2(swY + 2.6)}h12`} stroke="#9aa4bb" strokeOpacity=".22" strokeWidth=".5" strokeLinecap="round" />
        <rect x={PK.sw + 0.4} y={n2(swY - 2.3)} width="5.8" height="3.7" rx="1.2" fill={`url(#${u}knob)`} stroke="#0b0c10" strokeWidth=".4" />
        <path d={`M${PK.sw + 2} ${n2(swY - 1.7)}v2.2M${PK.sw + 3.3} ${n2(swY - 1.7)}v2.2M${PK.sw + 4.6} ${n2(swY - 1.7)}v2.2`} stroke="#14161b" strokeWidth=".45" />
        <rect x={PK.sw + 1} y={n2(swY - 2)} width="4.6" height=".6" rx=".3" fill="#fff" opacity=".28" />

        {/* silicon mic pinhole */}
        <circle cx={PK.pin} cy={PC} r="1.15" fill="#020203" />
        <path d={`M${PK.pin - 0.9} ${PC + 0.8}a1.3 1.3 0 0 0 1.9 .1`} stroke="#fff" strokeOpacity=".22" strokeWidth=".45" fill="none" />

        {/* keys: voice, page up / page down rocker, laser */}
        <Pv60Key u={u} x={PK.voice} w={PKR * 2} h={PKR * 2} />
        <Pv60Key u={u} x={PK.rocker} w={ROCK_W} h={ROCK_H} />
        <Pv60Key u={u} x={PK.laser} w={PKR * 2} h={PKR * 2} />
        <path d={`M${PK.rocker} ${PC - 5.5}v11`} stroke="#000" strokeOpacity=".14" strokeWidth=".7" strokeLinecap="round" />
        <g stroke={ink} fill="none" strokeLinecap="round" strokeLinejoin="round">
          <g transform={`translate(${PK.voice} ${PC}) rotate(90)`} strokeWidth="1.25">
            <rect x="-1.75" y="-5" width="3.5" height="6.1" rx="1.75" />
            <path d="M-3.7 -.9a3.7 3.7 0 0 0 7.4 0M0 2.8v2.1M-1.7 4.9h3.4" />
          </g>
          <g transform={`translate(${PK.rocker} ${PC}) rotate(90)`} strokeWidth="1.45">
            <path d="M-3.1 -10.2L0 -13.2 3.1 -10.2" />
            <path d="M-3.1 10.2L0 13.2 3.1 10.2" />
          </g>
          <g transform={`translate(${PK.laser} ${PC})`} strokeWidth="1">
            <circle r="1.75" />
            {Array.from({ length: 8 }, (_, i) => {
              const a = (i * Math.PI) / 4, c = Math.cos(a), s = Math.sin(a);
              return <path key={i} d={`M${n2(c * 3)} ${n2(s * 3)}L${n2(c * 4.6)} ${n2(s * 4.6)}`} />;
            })}
          </g>
        </g>

        {spot && <Lit u={u} s={spot} />}
      </g>

      {/* the receiver, set down in front where the Baseus keeps its own */}
      <g transform="translate(286 154) rotate(-9) scale(.95) translate(-20 -8.25)">
        <NanoReceiver u={u} />
        {part === 'receiver' && <Lit u={u} s={{ id: 'receiver', x: 20, y: 6.5, w: 40, h: 13, side: 'down' }} />}
      </g>
      {part === 'receiver' && <Callout from={[266, 159]} to={[232, 172]} text={BADGE.receiver} />}
      {spot && <FrameCallout s={spot} f={f} top={-16} bottom={PH + 20} />}
    </svg>
  );
}

/* ── Tidepool (Waveshare ESP32-S3-Touch-AMOLED-1.75C) ──────────────────── */
/* Φ55 mm CNC aluminium case, Φ48.96 mm glass, Φ43.76 mm active area, 15 mm deep;
   PWR at about two o'clock and BOOT at four on the right edge, Type-C at six, two
   mic holes on the left of the bezel. Lying face-up, seen a little from the front.
   The case is a rounded puck (a torus-like rim), so it is lofted: bands from the
   glass edge out to the silhouette, each a shade of the rim's profile — dark gasket,
   polished inner lip, bright crest, the front rolling away, a bounce under it. The
   dial is the Tidepool OS watch face. */

const RIM: [number, string][] = [
  [0, '#1b1d21'], [0.035, '#35383e'], [0.07, '#91969d'], [0.12, '#f1f3f5'], [0.2, '#dcdfe3'], [0.36, '#c6cacf'],
  [0.55, '#abb0b7'], [0.72, '#92979f'], [0.86, '#767b83'], [0.935, '#8c9198'], [0.968, '#a6abb2'], [1, '#5b6068'],
];

export function TidepoolArt({ className, highlight }: ArtProps) {
  const u = useUid();
  const part = artPart(highlight, 'tidepool');
  const cx = 110, cy = 95, R = 80, K = 0.95, Ry = R * K, D = 13;
  const Rg = R * 0.89, Rgy = Rg * K, Ra = R * 0.796;
  const sy = cy + D / 2, Sy = Ry + D / 2; // silhouette: the widest girth of the rounded case
  /* band g of the rim: 0 = the glass edge, 1 = the silhouette */
  const band = (g: number) => ({ y: cy + (sy - cy) * g, rx: Rg + (R - Rg) * g, ry: Rgy + (Sy - Rgy) * g });
  const on = (g: number, a: number, out = 0): Pt => {
    const b = band(g), t = (a * Math.PI) / 180;
    return [cx + (b.rx + out) * Math.cos(t), b.y + (b.ry + out) * Math.sin(t)];
  };
  const bandArc = (g: number, a0: number, a1: number, dr = 0) => { const b = band(g); return arc(cx, b.y, b.rx + dr, b.ry + dr, a0, a1); };
  const PWR = -44, BOOT = 40;
  const pwr = on(1, PWR, 1.6), boot = on(1, BOOT, 1.6);
  const tangent = (a: number) => { const t = (a * Math.PI) / 180; return (Math.atan2(Sy * Math.cos(t), -R * Math.sin(t)) * 180) / Math.PI; };
  const micA = on(0.42, -135), micB = on(0.42, 135);
  const usb = on(0.6, 90);
  const N = 30;
  const loft = Array.from({ length: N + 1 }, (_, i) => 1 - i / N);
  const ticks = Array.from({ length: 12 }, (_, i) => i * 30);
  const progress = 0.72;
  const endA = -90 + progress * 360;
  const end: Pt = [53 * Math.cos((endA * Math.PI) / 180), 53 * Math.sin((endA * Math.PI) / 180)];
  const crest = band(0.13), lip = band(0.05);
  return (
    <svg viewBox="14 10 192 192" className={['da', className].filter(Boolean).join(' ')} role="img"
      aria-label={label('Tidepool OS 圆形屏幕设备插画', part)}>
      <Defs u={u} />
      <defs>
        {/* key light from the top-left over the whole case */}
        <linearGradient id={u + 'light'} x1=".12" y1=".04" x2=".88" y2=".96">
          <stop offset="0" stopColor="#fff" stopOpacity=".32" />
          <stop offset=".38" stopColor="#fff" stopOpacity="0" />
          <stop offset=".56" stopColor="#000" stopOpacity="0" />
          <stop offset="1" stopColor="#000" stopOpacity=".3" />
        </linearGradient>
        <linearGradient id={u + 'crest'} gradientUnits="userSpaceOnUse" x1={cx - R} y1={cy - R} x2={cx + R * 0.6} y2={cy + R * 0.7}>
          <stop offset="0" stopColor="#fff" stopOpacity="1" />
          <stop offset=".55" stopColor="#fff" stopOpacity=".35" />
          <stop offset="1" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
        <linearGradient id={u + 'lip'} gradientUnits="userSpaceOnUse" x1={cx - Rg} y1={cy - Rgy} x2={cx + Rg * 0.7} y2={cy + Rgy}>
          <stop offset="0" stopColor="#4c5057" />
          <stop offset=".5" stopColor="#9da2a9" />
          <stop offset="1" stopColor="#ffffff" />
        </linearGradient>
        <linearGradient id={u + 'btn'} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#f6f7f9" />
          <stop offset=".55" stopColor="#c3c7cd" />
          <stop offset="1" stopColor="#80858d" />
        </linearGradient>
        <radialGradient id={u + 'dial'} cx=".5" cy=".8" r=".75">
          <stop offset="0" stopColor="#0c3d42" />
          <stop offset=".45" stopColor="#051a1e" />
          <stop offset="1" stopColor="#000" />
        </radialGradient>
        <radialGradient id={u + 'vig'} r=".5">
          <stop offset=".78" stopColor="#000" stopOpacity="0" />
          <stop offset="1" stopColor="#000" stopOpacity=".55" />
        </radialGradient>
        <linearGradient id={u + 'ring'} gradientUnits="userSpaceOnUse" x1="-53" y1="-53" x2="53" y2="53">
          <stop offset="0" stopColor="#5eead4" />
          <stop offset=".5" stopColor="#38bdf8" />
          <stop offset="1" stopColor="#a78bfa" />
        </linearGradient>
        <radialGradient id={u + 'cap'}>
          <stop offset="0" stopColor="#c4b5fd" stopOpacity=".9" />
          <stop offset="1" stopColor="#a78bfa" stopOpacity="0" />
        </radialGradient>
        <linearGradient id={u + 'tide'} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#2dd4bf" stopOpacity=".55" />
          <stop offset="1" stopColor="#0e7490" stopOpacity=".08" />
        </linearGradient>
        <linearGradient id={u + 'tide2'} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#818cf8" stopOpacity=".35" />
          <stop offset="1" stopColor="#4338ca" stopOpacity=".04" />
        </linearGradient>
        <linearGradient id={u + 'glare'} x1="0" y1="0" x2=".75" y2=".9">
          <stop offset="0" stopColor="#fff" stopOpacity=".24" />
          <stop offset=".42" stopColor="#fff" stopOpacity=".06" />
          <stop offset=".6" stopColor="#fff" stopOpacity="0" />
        </linearGradient>
        <radialGradient id={u + 'gedge'} r=".5">
          <stop offset=".86" stopColor="#000" stopOpacity="0" />
          <stop offset="1" stopColor="#000" stopOpacity=".6" />
        </radialGradient>
        <clipPath id={u + 'glass'}><ellipse cx={cx} cy={cy} rx={Rg} ry={Rgy} /></clipPath>
        <clipPath id={u + 'act'}><circle cx="0" cy="0" r={Ra} /></clipPath>
      </defs>

      <ellipse className="da-back" cx={cx} cy={sy} rx="110" ry="104" fill={`url(#${u}back)`} />
      <ellipse className="da-amb" cx={cx + 8} cy={sy + Sy - 6} rx={R + 14} ry="17" fill={`url(#${u}shA)`} />
      <ellipse className="da-con" cx={cx + 3} cy={sy + 4} rx={R + 2} ry={Sy + 2} fill={`url(#${u}shC)`} opacity=".9" />

      {/* PWR and BOOT stand proud of the right edge */}
      {([[PWR, pwr], [BOOT, boot]] as [number, Pt][]).map(([a, p]) => (
        <g key={a} transform={`translate(${n2(p[0])} ${n2(p[1])}) rotate(${n2(tangent(a))})`}>
          <rect x="-7.8" y="-2.9" width="15.6" height="5.8" rx="2.6" fill={`url(#${u}btn)`} stroke="#5d626a" strokeOpacity=".55" strokeWidth=".6" />
          <rect x="-6.2" y="-2.2" width="12.4" height=".9" rx=".45" fill="#fff" opacity=".75" />
        </g>
      ))}

      {/* the rounded aluminium case, lofted from the silhouette in to the glass */}
      {loft.map((g) => { const b = band(g); return <ellipse key={g} cx={cx} cy={n2(b.y)} rx={n2(b.rx)} ry={n2(b.ry)} fill={ramp(RIM, g)} />; })}
      <ellipse cx={cx} cy={sy} rx={R} ry={Sy} fill={`url(#${u}light)`} />
      {/* crest glint and the soft reflection rolling round the front */}
      <path d={arc(cx, crest.y, crest.rx, crest.ry, 150, 330)} stroke={`url(#${u}crest)`} strokeWidth="1.5" strokeLinecap="round" fill="none" />
      <path d={bandArc(0.55, 100, 165)} stroke="#fff" strokeOpacity=".3" strokeWidth="3.2" strokeLinecap="round" fill="none" />
      <path d={bandArc(0.955, 35, 145)} stroke="#fff" strokeOpacity=".32" strokeWidth=".9" strokeLinecap="round" fill="none" />
      <path d={bandArc(0.5, -80, -10)} stroke="#000" strokeOpacity=".07" strokeWidth="2.4" strokeLinecap="round" fill="none" />
      <path className="da-back" d={bandArc(1, 190, 320, -0.6)} stroke="#fff" strokeOpacity=".5" strokeWidth=".8" strokeLinecap="round" fill="none" />
      {/* polished inner lip: lit on the near side, in shade on the far side */}
      <ellipse cx={cx} cy={lip.y} rx={lip.rx} ry={lip.ry} fill="none" stroke={`url(#${u}lip)`} strokeWidth="1.1" opacity=".85" />
      {/* mic holes and the Type-C port */}
      {[micA, micB].map(([x, y]) => (
        <g key={x + y}>
          <ellipse cx={x} cy={y} rx="1.35" ry="1.2" fill="#3c4047" />
          <path d={`M${n2(x - 1)} ${n2(y + 0.85)}a1.35 1.2 0 0 0 2 -.1`} stroke="#fff" strokeOpacity=".85" strokeWidth=".5" fill="none" />
        </g>
      ))}
      <rect x={usb[0] - 8.5} y={usb[1] - 2.2} width="17" height="4.4" rx="2.2" fill="#121316" />
      <rect x={usb[0] - 5.4} y={usb[1] - 0.45} width="10.8" height="1.1" rx=".55" fill="#585c64" />
      <path d={`M${n2(usb[0] - 7)} ${n2(usb[1] + 2.9)}h14`} stroke="#fff" strokeOpacity=".45" strokeWidth=".6" strokeLinecap="round" />

      {/* glass and the Tidepool OS dial */}
      <ellipse cx={cx} cy={cy} rx={Rg} ry={Rgy} fill="#040506" />
      <g transform={`translate(${cx} ${cy}) scale(1 ${K})`}>
        <g clipPath={`url(#${u}act)`}>
          <circle r={Ra} fill={`url(#${u}dial)`} />
          <path d="M-70 36C-54 30-38 30-22 35S8 42 26 36 56 29 70 33V70H-70Z" fill={`url(#${u}tide2)`} />
          <path d="M-70 41C-56 37-42 37-28 41S0 47 16 42 46 36 70 40V70H-70Z" fill={`url(#${u}tide)`} />
          <path d="M-70 41C-56 37-42 37-28 41S0 47 16 42 46 36 70 40" stroke="#99f6e4" strokeOpacity=".75" strokeWidth="1" fill="none" />
          <circle r={Ra} fill={`url(#${u}vig)`} />
        </g>
        {ticks.map((a) => {
          const s = Math.sin((a * Math.PI) / 180), c = -Math.cos((a * Math.PI) / 180);
          const major = a % 90 === 0;
          return <line key={a} x1={s * 58.6} y1={c * 58.6} x2={s * 61.2} y2={c * 61.2} stroke="#fff"
            strokeOpacity={major ? 0.55 : 0.24} strokeWidth={major ? 1.4 : 1} strokeLinecap="round" />;
        })}
        <circle r="53" fill="none" stroke="#fff" strokeOpacity=".08" strokeWidth="3.2" />
        <path className="da-anim da-draw" d="M0 -53A53 53 0 1 1 -0.01 -53" pathLength={1} strokeDasharray="1"
          strokeDashoffset={1 - progress} stroke={`url(#${u}ring)`} strokeWidth="3.2" strokeLinecap="round" fill="none" />
        <circle cx={end[0]} cy={end[1]} r="6" fill={`url(#${u}cap)`} className="da-anim da-led" />
        <circle cx={end[0]} cy={end[1]} r="1.7" fill="#fff" />
        <g className="da-anim da-tick">
          <circle r="47" fill="none" />
          <circle cx="0" cy="-47" r="1.35" fill="#e6fffb" opacity=".85" />
        </g>
        <text y="-23" textAnchor="middle" fontFamily={FONT} fontSize="6.2" fontWeight="650" letterSpacing="2.4" fill="#5eead4">TIDEPOOL</text>
        <text y="7.5" textAnchor="middle" fontFamily={FONT} fontSize="26" fontWeight="300" fill="#f4f7f9" letterSpacing=".4"
          style={{ fontVariantNumeric: 'tabular-nums' }}>
          09<tspan className="da-anim da-blink">:</tspan>41
        </text>
        <rect x="-13" y="14" width="26" height="10.5" rx="5.25" fill="#5eead4" fillOpacity=".12" stroke="#5eead4" strokeOpacity=".35" strokeWidth=".6" />
        <g stroke="#99f6e4" strokeWidth=".85" fill="none" strokeLinecap="round">
          <rect x="-1.4" y="16.1" width="2.8" height="4.2" rx="1.4" />
          <path d="M-3 18.9a3 3 0 0 0 6 0M0 21.9v.9" />
        </g>
      </g>
      {/* glass: black border band under the cover glass, a soft sky reflection, rim lights */}
      <ellipse cx={cx} cy={cy} rx={Rg} ry={Rgy} fill={`url(#${u}gedge)`} pointerEvents="none" />
      <g clipPath={`url(#${u}glass)`} pointerEvents="none">
        <path d={`M${cx - Rg - 4} ${cy - 4}C${cx - 52} ${cy - 30} ${cx - 10} ${cy - 50} ${cx + 46} ${cy - 70}L${cx - Rg} ${cy - Rgy - 6}Z`} fill={`url(#${u}glare)`} />
      </g>
      <path d={arc(cx, cy, Rg - 1, Rgy - 1, 196, 286)} stroke="#fff" strokeOpacity=".5" strokeWidth="1" strokeLinecap="round" fill="none" />
      <path d={arc(cx, cy, Rg - 1, Rgy - 1, 18, 72)} stroke="#fff" strokeOpacity=".16" strokeWidth=".9" strokeLinecap="round" fill="none" />

      {part === 'pwr' && <><Lit u={u} s={{ id: 'pwr', x: pwr[0], y: pwr[1], r: 7, side: 'up' }} /><Callout from={on(1, PWR, 10)} to={[190, 20]} text={BADGE.pwr} /></>}
      {part === 'boot' && <><Lit u={u} s={{ id: 'boot', x: boot[0], y: boot[1], r: 7, side: 'down' }} /><Callout from={on(1, BOOT, 10)} to={[188, 194]} text={BADGE.boot} /></>}
      {part === 'usbc' && <><Lit u={u} s={{ id: 'usbc', x: usb[0], y: usb[1], w: 17, h: 4.4, side: 'down' }} /><Callout from={[usb[0], usb[1] + 6]} to={[usb[0], 197]} text={BADGE.usbc} /></>}
      {part === 'mic' && <><Lit u={u} s={{ id: 'mic', x: micA[0], y: micA[1], r: 3, side: 'up' }} /><Callout from={[micA[0] - 3, micA[1] - 3]} to={[36, 19]} text={BADGE.mic} /></>}
      {part === 'screen' && <>
        <ellipse className="da-anim da-hl da-hl-stroke" cx={cx} cy={cy} rx={Rg + 2.5} ry={Rgy + 2.5} fill="none" strokeOpacity=".35" strokeWidth="6" />
        <ellipse cx={cx} cy={cy} rx={Rg + 0.6} ry={Rgy + 0.6} fill="none" className="da-hl-stroke" strokeWidth="1.6" />
        <Callout from={[cx, cy - Rgy - 1]} to={[cx, 12]} text={BADGE.screen} />
      </>}
    </svg>
  );
}
