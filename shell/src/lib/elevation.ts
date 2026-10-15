/* 兼容管理员权限窗口: the words around administrator windows (UIPI), in one place.
   Windows drops DotVoice's synthetic clicks, scrolling and typing aimed at a window that
   runs as administrator while DotVoice does not (bridge/elevation.py notices it). The
   shell can run DotVoice elevated instead (shell/src-tauri/src/elevation.rs). */
import type { ElevationStatus, Prefs } from './types';

export const ELEVATION_SETTING = '设置 → 通用 → 兼容管理员权限窗口';
export const ELEVATION_NOTICE = `该窗口以管理员权限运行，系统拦截了点声的模拟点击/输入。可在 ${ELEVATION_SETTING} 开启`;

export interface ElevationView {
  /** The switch: on while the logon task exists for this DotVoice. */
  on: boolean;
  /** The switch can be used (an administrator account, not the UI self-check). */
  enabled: boolean;
  tone: 'ok' | 'off' | 'bad' | 'wait';
  /** Short state next to the title. */
  badge: string;
  /** Anything that needs saying beyond the description ('' = nothing). */
  note: string;
  /** On, but this copy runs without administrator rights: offer a restart (no prompt). */
  restart: boolean;
}

/** What the settings row shows. `status` null: the shell has not answered (yet). */
export function elevationView(status: ElevationStatus | null, prefs: Prefs | null): ElevationView {
  if (!status) {
    const on = !!prefs?.general?.run_elevated;
    return { on, enabled: false, tone: 'wait', badge: '正在检查', note: '', restart: false };
  }
  const on = status.task === 'ours';
  if (status.smoke) return { on, enabled: false, tone: 'off', badge: '自检模式', note: '界面自检模式不更改管理员设置。', restart: false };
  if (status.elevated) {
    return {
      on, enabled: true, tone: 'ok', badge: '当前：管理员权限',
      note: on ? '' : '点声这次是以管理员身份打开的；开启后每次登录都会这样运行。',
      restart: false,
    };
  }
  if (!status.available) {
    return { on: false, enabled: false, tone: 'off', badge: '当前：普通权限', note: '当前 Windows 账户不是管理员，无法开启。', restart: false };
  }
  if (status.task === 'other') {
    return {
      on: false, enabled: true, tone: 'off', badge: '当前：普通权限',
      note: `计划任务指向另一份点声${status.task_path ? `（${status.task_path}）` : ''}；开启会改为这一份。`, restart: false,
    };
  }
  return {
    on, enabled: true, tone: on ? 'bad' : 'off', badge: '当前：普通权限',
    note: on ? '已开启，但这次没有以管理员身份运行。' : '', restart: on,
  };
}

/** The capsule chip after an administrator window dropped DotVoice's input. */
export interface BlockedChip { title: string; hint: string; detail: string }

export function blockedChip(kind: 'elevated' | 'kept', copied = false): BlockedChip {
  if (kind === 'kept') {
    return {
      title: '文字没能输入到管理员窗口',
      hint: copied ? '已复制到剪贴板，按 Ctrl+V 粘贴' : '文字保留在点声主窗口的本次结果里',
      detail: `可在 ${ELEVATION_SETTING} 开启`,
    };
  }
  return { title: '该窗口以管理员权限运行', hint: '系统拦截了点声的模拟点击/输入', detail: `可在 ${ELEVATION_SETTING} 开启` };
}

/** The main window's toast for a result an administrator window refused (the core's
    sentence when it sent one). */
export function keptToast(result: { copied?: boolean; not_typed_message?: string }): string {
  if (result.not_typed_message) return result.not_typed_message;
  return `文字没能输入：该窗口以管理员权限运行，系统拦截了点声的模拟输入。${result.copied ? '文字已复制到剪贴板，按 Ctrl+V 粘贴' : '文字保留在本次结果里'}；在 ${ELEVATION_SETTING} 开启后可直接输入。`;
}
