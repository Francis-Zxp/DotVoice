/* Global shortcuts recorded in settings (prefs.hotkeys, registered by the shell for every
   program). A bare key would be taken from every app: F5 starts a PowerPoint show, Tab
   moves focus. Only F13–F24, missing from ordinary keyboards (macro keys), may stand alone. */
const MODIFIER_CODES = ['ControlLeft', 'ControlRight', 'AltLeft', 'AltRight', 'ShiftLeft', 'ShiftRight', 'MetaLeft', 'MetaRight'];

type KeyPress = Pick<KeyboardEvent, 'code' | 'ctrlKey' | 'altKey' | 'shiftKey' | 'metaKey'>;

/** The accelerator for a key press ("Ctrl+Shift+M"), or null while it is not a valid shortcut. */
export function accelerator(e: KeyPress): string | null {
  const parts: string[] = [];
  if (e.ctrlKey) parts.push('Ctrl');
  if (e.altKey) parts.push('Alt');
  if (e.shiftKey) parts.push('Shift');
  if (e.metaKey) parts.push('Super');
  const code = e.code;
  if (MODIFIER_CODES.includes(code)) return null;
  const key = code.startsWith('Key') ? code.slice(3) : code.startsWith('Digit') ? code.slice(5) : code;
  if (key === 'Tab' && !e.ctrlKey && !e.altKey && !e.metaKey) return null;  // Shift+Tab is focus
  if (!parts.length && !/^F(1[3-9]|2[0-4])$/.test(key)) return null;
  return [...parts, key].join('+');
}
