/* Every var(--x) a component reads must be defined somewhere: an undefined one silently
   resolves to nothing (0.11.6's position grid read --well and --faint, which no theme sets). */
import { describe, expect, it } from 'vitest';

// Read from disk, built so the shell's DOM-only typings need no node types (see theme-tokens.test.ts).
const fs = await import(/* @vite-ignore */ ['node', 'fs'].join(':'));
const dir = new URL('./', import.meta.url);
const read = (path: string): string => fs.readFileSync(new URL(path, dir), 'utf8');
const sources: string[] = (fs.readdirSync(dir, { recursive: true }) as string[])
  .map((path) => path.replace(/\\/g, '/'))
  .filter((path) => /\.tsx?$/.test(path) && !/\.test\.tsx?$/.test(path));

describe('CSS custom properties', () => {
  it('read by components are all defined', () => {
    const code = sources.map(read).join('\n');
    const defined = new Set([
      // Declarations in styles.css and in CSS strings inside components.
      ...[...(read('styles.css') + code).matchAll(/(--[\w-]+)\s*:/g)].map((m) => m[1]),
      // Inline style keys ('--tone': …) and setProperty('--x', …).
      ...[...code.matchAll(/['"](--[\w-]+)['"]/g)].map((m) => m[1]),
    ]);
    const missing = new Set<string>();
    for (const source of sources) {
      // A var() with a fallback is safe on its own.
      for (const m of read(source).matchAll(/var\((--[\w-]+)\s*\)/g)) if (!defined.has(m[1])) missing.add(`${m[1]} in ${source}`);
    }
    expect([...missing]).toEqual([]);
  });
});
