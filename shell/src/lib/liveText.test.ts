import { describe, expect, it } from 'vitest';
import { asSource, isAccurate, liveTextShown, nextLive, visibleLiveText } from './liveText';

describe('live text visibility', () => {
  it('shows OpenLess and cloud words always, local preview words only on request', () => {
    for (const source of ['remote', 'cloud'] as const) {
      expect(liveTextShown(source, false)).toBe(true);
      expect(liveTextShown(source, true)).toBe(true);
    }
    expect(liveTextShown('local', false)).toBe(false);
    expect(liveTextShown('local', true)).toBe(true);
  });

  it('treats a partial without a known source as local (never trusted by default)', () => {
    for (const source of [undefined, null, '', 'openless', 7]) {
      expect(isAccurate(source)).toBe(false);
      expect(asSource(source)).toBe('local');
      expect(visibleLiveText('猜的字', source, false)).toBe('');
      expect(visibleLiveText('猜的字', source, true)).toBe('猜的字');
    }
  });

  it('empty text is the waveform-only state whatever the source', () => {
    expect(visibleLiveText('', 'remote', true)).toBe('');
    expect(visibleLiveText('在线的字', 'remote', false)).toBe('在线的字');
  });

  it('an accurate stream is never replaced by a late local guess in the same session', () => {
    let live = nextLive({ text: '', source: null }, '本机', 'local');
    expect(live).toEqual({ text: '本机', source: 'local' });
    live = nextLive(live, '在线的字', 'remote');
    expect(live).toEqual({ text: '在线的字', source: 'remote' });
    const late = nextLive(live, '迟到的本机字', 'local');
    expect(late).toBe(live);
    expect(nextLive(live, '在线的字更多', 'remote')).toEqual({ text: '在线的字更多', source: 'remote' });
    expect(nextLive({ text: '云', source: 'cloud' }, '本机', undefined)).toEqual({ text: '云', source: 'cloud' });
  });
});
