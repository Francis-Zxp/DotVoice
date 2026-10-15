import { describe, expect, it } from 'vitest';
import { artPart } from './DeviceArt';

describe('DeviceArt highlight names', () => {
  it('accepts the manual numbering, plain digits and labels that contain it', () => {
    expect(artPart('⑥')).toBe('record');
    expect(artPart('6')).toBe('record');
    expect(artPart('长按翻译键 ⑦')).toBe('translate');
    expect(artPart('⑩')).toBe('voldown');
    expect(artPart('⑪')).toBe('switch');
    expect(artPart('①')).toBe('ok');
  });

  it('maps binding ids and part names', () => {
    expect(artPart('toggle_mode')).toBe('translate');
    expect(artPart('airmouse_left')).toBe('pageup');
    expect(artPart('airmouse_wheel_down')).toBe('voldown');
    expect(artPart(' Type-C ')).toBe('usbc');
    expect(artPart('LED')).toBe('led');
  });

  it('keeps power names device-specific and ignores empty input', () => {
    expect(artPart('power', 'tidepool')).toBe('pwr');
    expect(artPart('⑪', 'tidepool')).toBe('pwr');
    expect(artPart('power', 'baseus')).toBe('switch');
    expect(artPart('boot', 'tidepool')).toBe('boot');
    expect(artPart(undefined)).toBe('');
    expect(artPart('')).toBe('');
  });
});
