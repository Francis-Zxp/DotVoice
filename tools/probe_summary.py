"""Summarize a key-probe log into bursts (one burst per user step).

    .venv/Scripts/python.exe tools/probe_summary.py [runtime/key-probe-*.jsonl]

Events closer than GAP seconds belong to one burst. For each burst it lists
vendor codes, keyboard VKs, mouse buttons/motion and consumer reports, plus the
microphone RMS peak from the burst start until 3 s after it ends, compared with
the quiet baseline. Numbers only; the probe never stores audio.
"""
import json
from pathlib import Path
import statistics
import sys

GAP = 1.5
AFTER = 3.0  # steps are separated by 4 s pauses
VK_NAMES = {0x21: 'PageUp', 0x22: 'PageDown', 0x25: 'Left', 0x26: 'Up', 0x27: 'Right', 0x28: 'Down',
            0x0D: 'Enter', 0x1B: 'Esc', 0x74: 'F5', 0xBE: '.', 0x42: 'B', 0xAF: 'VolUp', 0xAE: 'VolDown'}
BUTTONS = {0x1: '左键按下', 0x2: '左键松开', 0x4: '右键按下', 0x8: '右键松开', 0x10: '中键按下', 0x20: '中键松开'}


def load(path):
    rows = []
    for line in Path(path).read_text(encoding='utf-8').splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def describe_buttons(value):
    value = int(value, 16)
    return '+'.join(name for bit, name in BUTTONS.items() if value & bit) or hex(value)


def main():
    files = sys.argv[1:] or sorted(str(p) for p in (Path(__file__).resolve().parents[1] / 'runtime').glob('key-probe-*.jsonl'))[-1:]
    if not files:
        raise SystemExit('没有找到 key-probe 记录')
    rows = load(files[0])
    mic = [(r['t'], r['mic_rms']) for r in rows if 'mic_rms' in r]
    keys = [r for r in rows if any(k in r for k in ('vendor_code', 'keyboard_vk', 'mouse_buttons', 'mouse_motion', 'interface'))]
    keys = [r for r in keys if not (r.get('vendor_code') == '00')]
    bursts, current = [], []
    for row in keys:
        if current and row['t'] - current[-1]['t'] > GAP:
            bursts.append(current)
            current = []
        current.append(row)
    if current:
        bursts.append(current)
    quiet = [v for t, v in mic if not any(b[0]['t'] - 1 <= t <= b[-1]['t'] + AFTER for b in bursts)]
    baseline = statistics.median(quiet) if quiet else None
    print(f'记录：{files[0]}')
    print(f'麦克风静默基线 RMS：{baseline if baseline is not None else "无音量数据"}\n')
    for index, burst in enumerate(bursts, 1):
        start, end = burst[0]['t'], burst[-1]['t']
        vendor = [r['vendor_code'].upper() for r in burst if 'vendor_code' in r]
        keyboard = [f"{VK_NAMES.get(int(r['keyboard_vk'], 16), r['keyboard_vk'])}{'↓' if r['down'] else '↑'}"
                    for r in burst if 'keyboard_vk' in r]
        buttons = [describe_buttons(r['mouse_buttons']) for r in burst if 'mouse_buttons' in r]
        motion = [r['mouse_motion'] for r in burst if 'mouse_motion' in r]
        other = sorted({f"{r['interface']}:{r['hex'][:16]}" for r in burst if 'interface' in r})
        peak = max([v for t, v in mic if start - .2 <= t <= end + AFTER], default=None)
        parts = [f'#{index}  {start:7.1f}s–{end:7.1f}s']
        if vendor:
            parts.append('厂商键码 ' + ' → '.join(vendor))
        if keyboard:
            parts.append('键盘 ' + ' '.join(keyboard))
        if buttons:
            parts.append('鼠标 ' + ' '.join(buttons))
        if motion:
            parts.append(f"移动 {sum(m['events'] for m in motion)} 次 (Δx {sum(m['dx'] for m in motion)}, Δy {sum(m['dy'] for m in motion)})")
        if other:
            parts.append('其它 ' + ', '.join(other))
        if peak is not None:
            ratio = f'（约基线 {peak / baseline:.0f} 倍）' if baseline else ''
            parts.append(f'麦克风峰值 {peak}{ratio}')
        print('  |  '.join(parts))


if __name__ == '__main__':
    main()
