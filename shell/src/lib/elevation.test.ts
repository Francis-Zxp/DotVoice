import { describe, expect, it } from 'vitest';
import { blockedChip, ELEVATION_NOTICE, ELEVATION_SETTING, elevationView, keptToast } from './elevation';
import type { ElevationStatus } from './types';

const status = (patch: Partial<ElevationStatus>): ElevationStatus => ({ elevated: false, available: true, task: 'missing', ...patch });

describe('兼容管理员权限窗口', () => {
  it('the notice is the agreed sentence and names the setting', () => {
    expect(ELEVATION_NOTICE).toBe('该窗口以管理员权限运行，系统拦截了点声的模拟点击/输入。可在 设置 → 通用 → 兼容管理员权限窗口 开启');
    expect(ELEVATION_SETTING).toBe('设置 → 通用 → 兼容管理员权限窗口');
  });

  it('the switch follows the logon task, and says how DotVoice runs now', () => {
    expect(elevationView(status({}), null)).toMatchObject({ on: false, enabled: true, badge: '当前：普通权限', restart: false });
    expect(elevationView(status({ elevated: true, task: 'ours' }), null)).toMatchObject({ on: true, enabled: true, tone: 'ok', badge: '当前：管理员权限', note: '' });
    // Run as administrator by hand: on is still off until the user turns it on.
    expect(elevationView(status({ elevated: true }), null)).toMatchObject({ on: false, tone: 'ok' });
    // On, but this copy is not elevated (the hand-over failed): offer a restart, no prompt.
    expect(elevationView(status({ task: 'ours' }), null)).toMatchObject({ on: true, tone: 'bad', restart: true });
    // A task for another copy is not this one's.
    const other = elevationView(status({ task: 'other', task_path: String.raw`D:\Portable\DotVoice.exe` }), null);
    expect(other).toMatchObject({ on: false, enabled: true });
    expect(other.note).toContain(String.raw`D:\Portable\DotVoice.exe`);
  });

  it('a standard account, the self-check and an unanswered shell cannot switch', () => {
    expect(elevationView(status({ available: false }), null)).toMatchObject({ on: false, enabled: false, note: '当前 Windows 账户不是管理员，无法开启。' });
    expect(elevationView(status({ available: false, smoke: true }), null)).toMatchObject({ enabled: false, badge: '自检模式' });
    const prefs = { general: { run_elevated: true } } as any;
    expect(elevationView(null, prefs)).toMatchObject({ on: true, enabled: false, tone: 'wait' });
  });

  it('capsule and toast copy for dropped input', () => {
    expect(blockedChip('elevated')).toEqual({ title: '该窗口以管理员权限运行', hint: '系统拦截了点声的模拟点击/输入', detail: `可在 ${ELEVATION_SETTING} 开启` });
    expect(blockedChip('kept', true).hint).toBe('已复制到剪贴板，按 Ctrl+V 粘贴');
    expect(blockedChip('kept', false).hint).toContain('主窗口');
    expect(blockedChip('kept', true, 'elevated')).toEqual(blockedChip('kept', true));
    // A lock screen / UAC prompt refused the keystrokes: never blame an administrator window.
    for (const reason of ['refused', 'partial']) {
      const refused = blockedChip('kept', true, reason);
      expect(refused.title).toBe('文字没能输入');
      expect(refused.hint).toBe('已复制到剪贴板，按 Ctrl+V 粘贴');
      expect(refused.detail).not.toContain(ELEVATION_SETTING);
    }
    expect(keptToast({ not_typed_message: '核心的原话' })).toBe('核心的原话');
    expect(keptToast({ copied: true })).toContain('已复制到剪贴板');
    expect(keptToast({ copied: false })).toContain(ELEVATION_SETTING);
  });
});
