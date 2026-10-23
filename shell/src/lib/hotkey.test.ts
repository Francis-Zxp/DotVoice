import { describe, expect, it } from 'vitest';
import { accelerator } from './hotkey';

const press = (code: string, mods: Partial<Record<'ctrlKey' | 'altKey' | 'shiftKey' | 'metaKey', boolean>> = {}) =>
  accelerator({ code, ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, ...mods });

describe('global shortcut accelerator', () => {
  it('needs a modifier for ordinary keys', () => {
    expect(press('KeyM', { ctrlKey: true, shiftKey: true })).toBe('Ctrl+Shift+M');
    expect(press('Digit3', { altKey: true })).toBe('Alt+3');
    expect(press('KeyM')).toBeNull();
    expect(press('ShiftLeft', { shiftKey: true })).toBeNull();
  });

  it('never takes a bare F5 (PowerPoint, refresh) or other F1–F12; F13–F24 may stand alone', () => {
    for (const key of ['F1', 'F5', 'F11', 'F12']) expect(press(key), key).toBeNull();
    expect(press('F5', { ctrlKey: true })).toBe('Ctrl+F5');
    expect(press('F13')).toBe('F13');
    expect(press('F24')).toBe('F24');
    expect(press('F25')).toBeNull();
  });

  it('leaves Tab and Shift+Tab to focus moves', () => {
    expect(press('Tab')).toBeNull();
    expect(press('Tab', { shiftKey: true })).toBeNull();
    expect(press('Tab', { ctrlKey: true, shiftKey: true })).toBe('Ctrl+Shift+Tab');
  });
});
