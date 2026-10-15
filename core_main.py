"""DotVoice core process. Started by the Tauri shell with piped stdio."""
import argparse
import ctypes as c
import json
import os
from pathlib import Path
import sys
import traceback


def _log_dir():
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'DotVoice'


def _check_preview(program, writer):
    import time
    from bridge.preview import LivePreview, find_model, find_refiner, openless_models_dir
    model = find_model(program)
    refiner = find_refiner(program, openless_models_dir())
    report = {'model': str(model) if model else None, 'refiner': refiner[1] if refiner else None, 'ok': False}
    if model:
        events = []
        preview = LivePreview(model, lambda kind, value: events.append((kind, value)), refiner=refiner)
        started = time.monotonic()
        preview.start('check')
        for _ in range(12):
            preview.feed(bytes(3200))  # 1.2 s of silence: enough for one refinement
        preview.finish()
        while time.monotonic() - started < 60 and not any(k == 'partial' and v.get('final') for k, v in events):
            time.sleep(.05)
        preview.close()
        report.update(ok=bool(events) and events[-1][0] == 'partial' and events[-1][1]['final']
                      and not any(kind == 'preview_error' for kind, _ in events),
                      events=[kind for kind, _ in events], error=preview.error,
                      seconds=round(time.monotonic() - started, 2))
    writer.write(json.dumps(report, ensure_ascii=False).encode('utf-8') + b'\n')
    writer.flush()
    return 0 if report['ok'] else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', help='folder that contains (or will contain) runtime/')
    parser.add_argument('--smoke', action='store_true', help='no microphone, HID or device changes')
    parser.add_argument('--check-preview', action='store_true', help='load the bundled preview model, decode silence, print JSON')
    args = parser.parse_args()
    reader, writer = sys.stdin.buffer, sys.stdout.buffer
    # Anything printed by a library must not corrupt the protocol stream.
    sys.stdout = sys.stderr

    base = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).parent
    if args.data:
        data = Path(args.data)
    elif getattr(sys, 'frozen', False) and (base.parent / 'installed.json').exists():
        data = _log_dir()
    else:
        data = base.parent if getattr(sys, 'frozen', False) else base
    if args.check_preview:
        return _check_preview(base.parent if getattr(sys, 'frozen', False) else base, writer)
    data.mkdir(parents=True, exist_ok=True)

    from bridge.backend import OpenLessBackend
    from bridge.core import Core
    from bridge.rpc import RpcServer

    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.restype = c.c_void_p
    mutex = None
    if not args.smoke:
        mutex = kernel.CreateMutexW(None, False, 'Local\\DotVoice-core')
        error = c.get_last_error()
        # 183: it already exists; 5 (no handle): an elevated core holds it and we may not open it.
        if error == 183 or (not mutex and error == 5):
            writer.write(json.dumps({'event': 'fatal', 'data': '另一个点声核心正在运行'},
                                    ensure_ascii=False).encode('utf-8') + b'\n')
            writer.flush()
            return 2

    config_path = (base.parent if getattr(sys, 'frozen', False) else base) / 'config.json'
    config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    if config.get('backend', 'openless') != 'openless':
        raise RuntimeError('此版本只实现了 OpenLess 后端')

    rpc = RpcServer(reader, writer)
    # Models ship next to the program (dev: repo root; frozen: DotVoice/ beside core/).
    program = base.parent if getattr(sys, 'frozen', False) else base
    core = Core(data, OpenLessBackend(config.get('openless_url', 'wss://127.0.0.1:8443/ws')),
                rpc.emit, smoke=args.smoke, program_dir=program)
    rpc.methods = core.methods()
    try:
        core.start()
        rpc.emit('ready', core.snapshot())
        rpc.serve()
    finally:
        core.close()
        if mutex:
            kernel.CloseHandle(c.c_void_p(mutex))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        folder = _log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'core-error.log').write_text(traceback.format_exc(), encoding='utf-8')
        try:
            sys.__stdout__.buffer.write(json.dumps(
                {'event': 'fatal', 'data': '点声核心未能启动，诊断记录：' + str(folder / 'core-error.log')},
                ensure_ascii=False).encode('utf-8') + b'\n')
            sys.__stdout__.buffer.flush()
        except Exception:
            pass
        sys.exit(1)
