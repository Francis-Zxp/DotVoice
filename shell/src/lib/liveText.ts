/* Which live words may be shown while someone speaks.

   Live text has three sources: the local preview model ('local': fast, often wrong),
   OpenLess's own stream ('remote') and the built-in cloud engines ('cloud'). Remote and
   cloud words come from the recognizer that also writes the final text, so they are
   shown; local words only when the user asked for them (prefs preview.show_local_text).
   A partial without a known source counts as local. Final results are always shown. */
import type { LiveSource } from './types';

export function isAccurate(source: unknown): source is 'remote' | 'cloud' {
  return source === 'remote' || source === 'cloud';
}

export function asSource(source: unknown): LiveSource {
  return isAccurate(source) ? source : 'local';
}

/** May words from this source be shown now? */
export function liveTextShown(source: unknown, showLocal: boolean): boolean {
  return isAccurate(source) || showLocal;
}

/** The live words to show: '' means the waveform-only listening state. */
export function visibleLiveText(text: string, source: unknown, showLocal: boolean): string {
  return text && liveTextShown(source, showLocal) ? text : '';
}

export interface Live { text: string; source: LiveSource | null }

/** A partial arrives. Once an accurate stream has spoken in this session, a late local
    guess (queued in the preview before it was stopped) never replaces it: `current` is
    returned unchanged. */
export function nextLive(current: Live, text: string, source: unknown): Live {
  const next = asSource(source);
  if (next === 'local' && current.source && current.source !== 'local') return current;
  return { text, source: next };
}
