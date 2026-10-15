"""User-paced, read-only key probe for the BS-OH085 receiver.

Records every Baseus report (vendor code, keyboard VK, mouse buttons/motion)
and, with --audio, the BaseusPresent microphone *level* only (RMS numbers,
never PCM). Nothing is written to the device. Stop with the stop file, Ctrl+C
or the time limit.

    .venv/Scripts/python.exe tools/probe_keys.py --audio --minutes 6
"""
import argparse
import audioop
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bridge.controller import report_code  # noqa: E402
from bridge.rawinput import RawMonitor  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--audio', action='store_true', help='log microphone level (no audio kept)')
    parser.add_argument('--minutes', type=float, default=6)
    args = parser.parse_args()
    runtime = ROOT / 'runtime'
    runtime.mkdir(exist_ok=True)
    stop = runtime / 'key-probe.stop'
    if stop.exists():
        stop.unlink()
    output = runtime / time.strftime('key-probe-%Y%m%d-%H%M%S.jsonl')
    lock = threading.Lock()
    began = time.monotonic()
    motion = {'dx': 0, 'dy': 0, 'events': 0}
    level = {'peak': 0.0}
    last_code = [None]

    with output.open('x', encoding='utf-8', buffering=1) as stream:
        def write(value):
            with lock:
                stream.write(json.dumps({'t': round(time.monotonic() - began, 3), **value},
                                        ensure_ascii=False) + '\n')

        def receive(event):
            kind = event['kind']
            if kind == 0:
                mouse = event['mouse']
                with lock:
                    motion['dx'] += mouse['dx']
                    motion['dy'] += mouse['dy']
                    motion['events'] += 1
                if mouse['buttons']:
                    write({'mouse_buttons': hex(mouse['buttons'])})
            elif kind == 1:
                key = event['keyboard']
                write({'keyboard_vk': hex(key['vk']), 'down': key['down']})
            else:
                code = report_code(event)
                vendor = 'MI_03' in event['path']
                if vendor and code is not None:
                    if code != last_code[0]:
                        last_code[0] = code
                        write({'vendor_code': format(code, '02x'), 'hex': event['hex']})
                else:
                    write({'interface': 'vendor-raw' if vendor else 'consumer', 'hex': event['hex']})

        monitor = RawMonitor(receive, lambda error: write({'error': error}))
        monitor.start()
        monitor.set_mouse(True)
        capture = None
        if args.audio:
            from bridge.audio import AudioCapture
            def pcm(chunk):
                value = audioop.rms(chunk, 2)
                with lock:
                    level['peak'] = max(level['peak'], value)
            capture = AudioCapture(pcm, lambda message: write({'audio_error': message}))
            capture.start()
        write({'state': 'READY', 'audio_level': bool(args.audio)})
        print('READY ->', output, flush=True)
        try:
            deadline = began + args.minutes * 60
            while not stop.exists() and time.monotonic() < deadline:
                time.sleep(.2)
                with lock:
                    moved = dict(motion)
                    motion.update(dx=0, dy=0, events=0)
                    peak, level['peak'] = level['peak'], 0.0
                if moved['events']:
                    write({'mouse_motion': moved})
                if args.audio:
                    write({'mic_rms': int(peak)})
        except KeyboardInterrupt:
            pass
        finally:
            if capture:
                capture.close()
            monitor.close()
            write({'state': 'CLOSED'})
            if stop.exists():
                stop.unlink()
    print('CLOSED', flush=True)


if __name__ == '__main__':
    main()
