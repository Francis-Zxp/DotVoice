import { describe, expect, it } from 'vitest';
import { asSource, isAccurate, liveTextShown, nextLive, visibleLiveText } from './liveText';

describe('live text visibility', () => {
  it('shows words from the selected online recognizer', () => {
    for (const source of ['remote', 'cloud']) {
      expect(liveTextShown(source)).toBe(true);
      expect(visibleLiveText('在线的字', source)).toBe('在线的字');
    }
  });
  it('ignores retired local or unidentified preview events', () => {
    for (const source of ['local', undefined, null, '', 'openless', 7]) {
      expect(isAccurate(source)).toBe(false);
      expect(asSource(source)).toBeNull();
      expect(visibleLiveText('猜的字', source)).toBe('');
      const current = { text: '', source: null };
      expect(nextLive(current, '猜的字', source)).toBe(current);
    }
  });
  it('empty text leaves the waveform visible', () => {
    expect(visibleLiveText('', 'remote')).toBe('');
  });
  it('late legacy events cannot replace online words', () => {
    const live = nextLive({ text: '', source: null }, '在线的字', 'remote');
    expect(nextLive(live, '迟到的本机字', 'local')).toBe(live);
    expect(nextLive(live, '在线的字更多', 'remote')).toEqual({ text: '在线的字更多', source: 'remote' });
    expect(nextLive({ text: '云', source: 'cloud' }, '未知', undefined)).toEqual({ text: '云', source: 'cloud' });
  });
});
