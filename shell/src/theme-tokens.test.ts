/* Contrast of the engine accent tokens in styles.css, theme by theme (light, the OS dark
   palette, [data-theme=dark] and glass). --engine-openless fills the current engine's icon
   tile under a white glyph; --engine-*-text colour text and icons on a surface. A text shade
   used as the fill made the dark tile pale lavender (white glyph 2.4:1, 0.11.4 review). */
import { describe, expect, it } from 'vitest';
import { ENGINE_TONE } from './lib/engine';

// Read from disk: vitest hands CSS imports (?raw too) over as empty strings. The specifier is
// built so the shell's DOM-only typings need no node types.
const fs = await import(/* @vite-ignore */ ['node', 'fs'].join(':'));
const css: string = fs.readFileSync(new URL('./styles.css', import.meta.url), 'utf8');

type RGB = [number, number, number];

/** The custom properties of the rule whose selector list is exactly `selector`. */
function block(selector: string): Record<string, string> {
  const head = new RegExp(`^[ \\t]*${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')} \\{`, 'm').exec(css);
  if (!head) throw new Error(`no block ${selector}`);
  const start = head.index + head[0].length;
  const body = css.slice(start, css.indexOf('}', start));
  return Object.fromEntries([...body.matchAll(/(--[\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
}

const light = block(':root');
const systemDark = { ...light, ...block(':root:not([data-theme="light"])') };
const dark = { ...light, ...block(':root[data-theme="dark"], :root[data-theme="glass"]') };
const glass = { ...dark, ...block(':root[data-theme="glass"]') };
const THEMES = { light, systemDark, dark, glass };

/** #rgb / #rrggbb, or rgba() as [rgb, alpha]. */
function parse(value: string): [RGB, number] {
  const hex = value.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  if (hex) {
    const h = hex[1].length === 3 ? [...hex[1]].map((c) => c + c).join('') : hex[1];
    return [[0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as RGB, 1];
  }
  const rgba = value.match(/^rgba?\(([^)]+)\)$/);
  if (!rgba) throw new Error(`not a colour: ${value}`);
  const [r, g, b, a = 1] = rgba[1].split(',').map(Number);
  return [[r, g, b], a];
}
const rgb = (value: string) => parse(value)[0];
/** `top` at `alpha` over `base` (also color-mix(in srgb, top a%, transparent) over base). */
const over = (top: RGB, alpha: number, base: RGB) => top.map((v, i) => v * alpha + base[i] * (1 - alpha)) as RGB;
const layer = (value: string, base: RGB) => { const [c, a] = parse(value); return over(c, a, base); };
const lum = (c: RGB) => {
  const lin = (v: number) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
  return 0.2126 * lin(c[0]) + 0.7152 * lin(c[1]) + 0.0722 * lin(c[2]);
};
const ratio = (a: RGB, b: RGB) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); };
const WHITE: RGB = [255, 255, 255];

/* The surfaces a token sits on: the opaque panel, and the window where it is opaque (glass's
   window is translucent over the desktop; its panel stands in). */
const surfaces = (t: Record<string, string>) => [rgb(t['--panel-solid']), ...(parse(t['--window'])[1] === 1 ? [rgb(t['--window'])] : [])];

const TOKENS = ['--engine-openless', '--engine-openless-text', '--engine-volc-text', '--engine-dashscope-text', '--engine-hotkey-text'];

describe('engine accent tokens', () => {
  it('are defined in every theme, the OS dark palette matching [data-theme=dark]', () => {
    for (const [name, t] of Object.entries(THEMES)) {
      for (const token of TOKENS) expect(t[token], `${name} ${token}`).toMatch(/^#/);
    }
    for (const token of TOKENS) expect(systemDark[token]).toBe(dark[token]);
  });

  it('give the 百炼 and hotkey cards\' badge text AA and icons 3:1, on every card background', () => {
    const cases = [['--engine-dashscope-text', ENGINE_TONE.dashscope], ['--engine-hotkey-text', '--present']] as const;
    for (const [name, t] of Object.entries(THEMES)) {
      for (const [token, fill] of cases) {
        const tone = rgb(fill.startsWith('--') ? t[fill] : fill);
        const text = rgb(t[token]);
        for (const surface of surfaces(t)) {
          // .engine-card: --field at rest; selected, .engine-thumb runs tone 15% → 5% over the panel.
          const cards = [layer(t['--field'], surface), over(tone, 0.15, rgb(t['--panel-solid'])), over(tone, 0.05, rgb(t['--panel-solid']))];
          for (const card of cards) {
            expect(ratio(text, over(tone, 0.13, card)), `${name} ${token} badge`).toBeGreaterThanOrEqual(4.5);
            expect(ratio(text, over(tone, 0.11, card)), `${name} ${token} icon`).toBeGreaterThanOrEqual(3);
          }
        }
      }
    }
  });

  it('keep the OpenLess fill dark enough for the white glyph on the current engine tile', () => {
    for (const [name, t] of Object.entries(THEMES)) {
      const fill = rgb(t['--engine-openless']);
      // .engine-card[data-current] .engine-ico: linear-gradient(mix(tone 72%, white), tone).
      expect(ratio(WHITE, fill), `${name}: white on the fill`).toBeGreaterThanOrEqual(4.5);
      expect(ratio(WHITE, over(fill, 0.72, WHITE)), `${name}: white on the tile's light end`).toBeGreaterThanOrEqual(3);
    }
  });

  it('give OpenLess text and icons AA on the panel', () => {
    for (const [name, t] of Object.entries(THEMES)) {
      for (const surface of surfaces(t)) {
        expect(ratio(rgb(t['--engine-openless-text']), surface), `${name} on ${surface}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it('give the Doubao hint button (12.5px text) AA on the hint strip, hovered or not', () => {
    for (const [name, t] of Object.entries(THEMES)) {
      const text = rgb(t['--engine-volc-text']);
      for (const surface of surfaces(t)) {
        // The strip: color-mix(in srgb, volc 7%, transparent); hover adds .btn-ghost's --field.
        const strip = over(rgb(ENGINE_TONE.volc), 0.07, surface);
        expect(ratio(text, strip), `${name} strip`).toBeGreaterThanOrEqual(4.5);
        expect(ratio(text, layer(t['--field'], strip)), `${name} hovered`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });
});

describe('text tokens', () => {
  it('give informational faint text (.faint, --text-faint) AA on the panel and the window in every theme', () => {
    expect(css).toMatch(/\.faint \{ color: var\(--text-faint\); \}/);
    for (const [name, t] of Object.entries(THEMES)) {
      for (const surface of surfaces(t)) {
        expect(ratio(layer(t['--text-faint'], surface), surface), `${name} on ${surface}`).toBeGreaterThanOrEqual(4.5);
      }
    }
  });

  it('keep white words on the recording stage readable in every mode (≥ 3.5:1 where the flood is lightest)', () => {
    const flood = /\.stage-flood \{[^}]*?background: linear-gradient\(160deg, color-mix\(in srgb, var\(--mode\) (\d+)%, black\)/.exec(css);
    expect(flood, 'the stage flood starts from the mode colour mixed with black').toBeTruthy();
    const share = Number(flood![1]) / 100;
    expect(/\.stage-flood \{[^}]*,\s*white\)/.test(css), 'no white-mixed highlight under the words').toBe(false);
    for (const mode of ['--input', '--notes', '--present']) {
      const lightest = over(rgb(light[mode]), share, [0, 0, 0]);
      expect(ratio(WHITE, lightest), mode).toBeGreaterThanOrEqual(3.5);
    }
  });
});

describe('motion cost', () => {
  it('reveals the answer per character with opacity only (no per-glyph blur; inline spans ignore transforms)', () => {
    const frames = /@keyframes score-in \{([^}]*\})\s*\}/.exec(css);
    expect(frames, 'score-in is defined').toBeTruthy();
    expect(frames![1]).not.toMatch(/filter|transform/);
  });
});
