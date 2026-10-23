const WEEK = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

const pad = (n: number) => String(n).padStart(2, '0');

export function clock(seconds: number) {
  const d = new Date(seconds * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function shortClock(seconds: number) {
  const d = new Date(seconds * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function startOfDay(d: Date) {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
}

/** Group label: 今天 · 10月4日 / 昨天 · 10月3日 / 10月2日 周五 / 2025年3月1日 */
export function dayLabel(seconds: number, now = Date.now()) {
  const d = new Date(seconds * 1000);
  const days = Math.round((startOfDay(new Date(now)) - startOfDay(d)) / 86400000);
  const md = `${d.getMonth() + 1}月${d.getDate()}日`;
  if (days === 0) return `今天 · ${md}`;
  if (days === 1) return `昨天 · ${md}`;
  if (d.getFullYear() !== new Date(now).getFullYear()) return `${d.getFullYear()}年${md}`;
  return `${md} ${WEEK[d.getDay()]}`;
}

/** Short moment: 今天 23:12 / 昨天 23:12 / 10月5日 23:12 / 2025年3月1日 */
export function when(seconds: number, now = Date.now()) {
  const d = new Date(seconds * 1000);
  const days = Math.round((startOfDay(new Date(now)) - startOfDay(d)) / 86400000);
  const time = shortClock(seconds);
  if (days === 0) return `今天 ${time}`;
  if (days === 1) return `昨天 ${time}`;
  if (d.getFullYear() !== new Date(now).getFullYear()) return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日`;
  return `${d.getMonth() + 1}月${d.getDate()}日 ${time}`;
}

export function dayKey(seconds: number) {
  const d = new Date(seconds * 1000);
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

export function fullDate(seconds: number) {
  const d = new Date(seconds * 1000);
  return `${d.getFullYear()}年${d.getMonth() + 1}月${d.getDate()}日 ${WEEK[d.getDay()]} ${clock(seconds)}`;
}

export function duration(seconds: number | null | undefined) {
  if (seconds == null || !Number.isFinite(seconds)) return '—';
  const s = Math.max(0, seconds);
  if (s < 59.5) return `${s.toFixed(s < 10 ? 1 : 0)} 秒`;
  // Round the total first, so 119.6 s reads 2 分 0 秒 rather than 1 分 60 秒.
  const t = Math.round(s);
  const m = Math.floor(t / 60);
  if (m < 60) return `${m} 分 ${t % 60} 秒`;
  return `${Math.floor(m / 60)} 小时 ${m % 60} 分`;
}

export function ago(seconds: number, now = Date.now() / 1000) {
  const s = Math.max(0, now - seconds);
  if (s < 45) return '刚刚';
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.round(s / 3600)} 小时前`;
  return `${Math.round(s / 86400)} 天前`;
}

export function timer(ms: number) {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${pad(s % 60)}`;
}

export const phaseLabel: Record<string, string> = {
  disconnected: '已暂停', idle: '已就绪', starting: '正在接入', recording: '正在聆听',
  processing: '识别中', error: '连接异常',
};

export const slotLabel: Record<string, string> = {
  toggle_mode: '切换模式', quick_note: '闪念笔记', arm_toggle: '启用 / 暂停语音', cancel: '取消本段',
  right_click: '④ 长按 = 右键',
  airmouse_wheel_up: '按住 ⑤ + 音量 + = 上滚', airmouse_wheel_down: '按住 ⑤ + 音量 − = 下滚', airmouse_activity: '飞鼠键 ⑤',
  airmouse_ctrl: '按住 = Ctrl / 标注', annotate: '演示标注',
};

export function bindingText(binding?: { source: string; fire?: string[]; vk?: number[]; label: string }) {
  if (!binding) return '未绑定';
  const codes = binding.source === 'keyboard'
    ? (binding.vk ?? []).map((v) => 'VK ' + v.toString(16).toUpperCase().padStart(2, '0'))
    : (binding.fire ?? []).map((c) => c.toUpperCase());
  return codes.join(' · ');
}
