"""Newline-delimited JSON over stdin/stdout between the Tauri shell and the core.

Requests:  {"id": 7, "method": "action", "params": {"action": "arm"}}
Responses: {"id": 7, "result": ...}  or  {"id": 7, "error": "..."}
Events:    {"event": "state", "data": {...}}
No network port is opened; the shell is the only peer.
"""
from concurrent.futures import ThreadPoolExecutor
import inspect
import json
import threading

MAX_LINE = 1 << 20


class RpcServer:
    def __init__(self, reader, writer, methods=None, workers=4):
        self.reader, self.writer = reader, writer  # binary streams
        self.methods = methods or {}
        self.write_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='rpc')
        self.stopped = threading.Event()

    def _write(self, message):
        line = json.dumps(message, ensure_ascii=False, separators=(',', ':'), default=str)
        data = line.encode('utf-8') + b'\n'
        with self.write_lock:
            if self.stopped.is_set():
                return
            try:
                self.writer.write(data)
                self.writer.flush()
            except (OSError, ValueError):
                self.stopped.set()

    def emit(self, event, data=None):
        self._write({'event': event, 'data': data})

    def _call(self, ident, method, params):
        try:
            function = self.methods[method]
        except KeyError:
            self._write({'id': ident, 'error': f'未知方法：{method}'})
            return
        try:
            if isinstance(params, dict):
                accepted = inspect.signature(function).parameters
                result = function(**{k: v for k, v in params.items() if k in accepted})
            elif isinstance(params, list):
                result = function(*params)
            else:
                result = function()
            self._write({'id': ident, 'result': result})
        except Exception as error:  # Report, never kill the reader loop.
            self._write({'id': ident, 'error': str(error) or type(error).__name__})

    def handle_line(self, raw):
        if len(raw) > MAX_LINE:
            self.emit('notice', '收到过长的控制消息，已忽略')
            return
        try:
            message = json.loads(raw.decode('utf-8'))
            if not isinstance(message, dict) or not isinstance(message.get('method'), str):
                raise ValueError('bad request')
        except (ValueError, UnicodeDecodeError):
            self.emit('notice', '收到无法解析的控制消息，已忽略')
            return
        ident = message.get('id')
        if message['method'] == 'shutdown':
            self._write({'id': ident, 'result': {'ok': True}})
            self.stopped.set()
            return
        self.pool.submit(self._call, ident, message['method'], message.get('params'))

    def serve(self):
        """Blocks until stdin closes (the shell exited) or a shutdown request."""
        while not self.stopped.is_set():
            raw = self.reader.readline(MAX_LINE + 1)
            if not raw:
                break
            raw = raw.strip()
            if raw:
                self.handle_line(raw)
        self.stopped.set()
        self.pool.shutdown(wait=False, cancel_futures=True)
