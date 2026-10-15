/* Only the selected recognizer's stream is shown. Old or unidentified preview events
   must not replace these words; the final result is always shown. */
import type { LiveSource } from './types';

export function isAccurate(source: unknown): source is LiveSource {
  return source === 'remote' || source === 'cloud';
}

export function asSource(source: unknown): LiveSource | null {
  return isAccurate(source) ? source : null;
}

export function liveTextShown(source: unknown): boolean {
  return isAccurate(source);
}

export function visibleLiveText(text: string, source: unknown): string {
  return text && liveTextShown(source) ? text : '';
}

export interface Live { text: string; source: LiveSource | null }

export function nextLive(current: Live, text: string, source: unknown): Live {
  const next = asSource(source);
  return next ? { text, source: next } : current;
}
