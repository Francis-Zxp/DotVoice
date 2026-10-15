"""Alibaba Cloud Bailian (阿里云百炼 / DashScope) real-time ASR: Fun-ASR and Paraformer.

Protocol (docs: https://help.aliyun.com/zh/model-studio/fun-asr-realtime-websocket-api,
.../websocket-for-paraformer-real-time-service, .../fun-asr-client-events,
.../fun-asr-server-events, .../paraformer-client-events, .../paraformer-server-events):

  handshake   Authorization: Bearer <API Key>
  client  ->  {"header": {"action": "run-task", "task_id", "streaming": "duplex"},
               "payload": {"task_group": "audio", "task": "asr", "function": "recognition",
                           "model", "parameters": {"format": "pcm", "sample_rate": 16000, ...},
                           "input": {}}}
  server  ->  task-started                       (audio may only be sent after this)
  client  ->  binary PCM (~100 ms per message, 1-16 KB)
  server  ->  result-generated: payload.output.sentence {text, sentence_end, heartbeat, ...}
              (text is the whole current sentence; sentence_end marks it final; heartbeat
              results are skipped)
  client  ->  {"header": {"action": "finish-task", "task_id", "streaming": "duplex"},
               "payload": {"input": {}}}
  server  ->  remaining results, then task-finished; or task-failed (header.error_code /
              header.error_message) at any time.

Endpoints: wss://dashscope.aliyuncs.com/api-ws/v1/inference (Beijing; still supported) or the
workspace domain wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference that the
docs now recommend; Singapore: dashscope-intl / {WorkspaceId}.ap-southeast-1. Beijing and
Singapore API keys are not interchangeable.

Fun-ASR realtime gets parameters.language_hints ["zh"] unless hints were configured. Should
the task fail on it before task-started (InvalidParameter), the same recording is sent again
at once without the hint and this process stops sending it for that model.

Manual check with a real key (read from the environment, never printed):
  set DASHSCOPE_API_KEY=...
  python -m bridge.engines.dashscope --wav sample.wav [--model paraformer-realtime-v2]
"""
import json
import os
import re
import uuid

import websocket

from .stream import EngineError, Retry, StreamSession, check_ws_url, join_text, probe, resolve

DEFAULT_MODEL = 'fun-asr-realtime'
# The documented language hint for Fun-ASR realtime (DotVoice's users speak Mandarin);
# Paraformer is left to detect the language itself.
AUTO_HINTS = ['zh']
AUTO_HINT_MODELS = re.compile(r'fun-asr-realtime(-[0-9-]+)?$')
_hints_refused = set()   # models whose task failed on the automatic hint in this process
MODELS = {
    'fun-asr-realtime': 'Fun-ASR 实时识别（推荐）',
    'fun-asr-realtime-2026-02-28': 'Fun-ASR 实时识别（2026-02-28 快照）',
    'paraformer-realtime-v2': 'Paraformer 实时识别 v2',
}
REGIONS = {
    'cn': ('wss://dashscope.aliyuncs.com/api-ws/v1/inference',
           'wss://{workspace}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference'),
    'intl': ('wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference',
             'wss://{workspace}.ap-southeast-1.maas.aliyuncs.com/api-ws/v1/inference'),
}
# "Nothing to recognise" is an empty result, not a failure.
EMPTY_CODES = {'NO_VALID_AUDIO_ERROR', 'NO_INPUT_AUDIO_ERROR', 'SUCCESS_WITH_NO_VALID_FRAGMENT',
               'ASR_RESPONSE_HAVE_NO_WORDS'}
ERRORS = {
    'InvalidApiKey': 'API Key 无效（注意北京与新加坡地域的 Key 不通用）',
    'invalid_api_key': 'API Key 无效（注意北京与新加坡地域的 Key 不通用）',
    'Arrearage': '阿里云账户已欠费，请充值后重试',
    'AccessDenied': '无权调用该模型（可能未开通、免费额度已用完且未开启付费，或模型已下线）',
    'AccessDenied.Unpurchased': '尚未开通阿里云百炼服务，请用主账号开通',
    'Model.AccessDenied': '无权调用该模型（子业务空间需要授权）',
    'AllocationQuota.FreeTierOnly': '免费额度已用完，且开启了“仅使用免费额度”',
    'ModelNotFound': '模型名称不正确或未开通',
    'model_not_found': '模型名称不正确或未开通',
    'InvalidParameter': '请求参数无效',
    'Throttling': '请求过于频繁，请稍后再试',
    'Throttling.RateQuota': '请求过于频繁，请稍后再试',
    'Throttling.AllocationQuota': '调用量超过限额，请稍后再试',
    'Throttling.BurstRate': '请求频率上升过快，请稍后再试',
    'Throttling.Concurrency': '并发路数超过限额，请稍后再试',
    'DataInspectionFailed': '内容未通过安全审核',
    'InternalError': '服务内部错误，请稍后重试',
    'RequestTimeOut': '请求超时，请稍后重试',
    'ClientDisconnect': '连接在任务结束前断开',
}


def run_task(task_id, model, parameters):
    return {'header': {'action': 'run-task', 'task_id': task_id, 'streaming': 'duplex'},
            'payload': {'task_group': 'audio', 'task': 'asr', 'function': 'recognition', 'model': model,
                        'parameters': parameters, 'input': {}}}


def finish_task(task_id):
    return {'header': {'action': 'finish-task', 'task_id': task_id, 'streaming': 'duplex'},
            'payload': {'input': {}}}


def error_message(code, message):
    code = str(code or '')
    message = str(message or '').strip()
    if 'timeout after' in message.lower() or (code == 'RequestTimeOut' and '23' in message):
        return '阿里云百炼：长时间没有收到音频，识别已结束'
    known = ERRORS.get(code)
    if known is None:
        for prefix, text in ERRORS.items():
            if code.startswith(prefix + '.'):
                known = text
                break
    if known:
        return f'阿里云百炼：{known}' + (f'。错误码 {code}' if code else '')
    detail = message[:80]
    return f'阿里云百炼识别失败（{code or "未知错误"}）' + (f'：{detail}' if detail else '')


def hint_refusal(code, message):
    """Could this failure be the language hint's fault? An invalid-parameter code, or a
    message that names the language."""
    code, message = str(code or ''), str(message or '').lower()
    return code.startswith('InvalidParameter') or code.lower().startswith('invalid_parameter') or 'language' in message


class Sentences:
    """Assembles the transcript: finished sentences plus the one still being recognised."""

    def __init__(self):
        self.done = []
        self.current = ''
        self.current_id = None

    def update(self, sentence):
        """Apply one result-generated sentence; False when it carries nothing to show."""
        if not isinstance(sentence, dict) or sentence.get('heartbeat'):
            return False
        text = sentence.get('text')
        if not isinstance(text, str):
            return False
        sentence_id = sentence.get('sentence_id')
        if (sentence_id is not None and self.current_id is not None and sentence_id != self.current_id
                and self.current):
            # A new sentence began while the old one never ended: keep what we had of it.
            self.done.append(self.current)
            self.current = ''
        if sentence.get('sentence_end'):
            self.done.append(text)
            self.current, self.current_id = '', None
        else:
            self.current, self.current_id = text, sentence_id
        return True

    def text(self):
        out = ''
        for part in self.done + [self.current]:
            out = join_text(out, part)
        return out


class DashScopeSession(StreamSession):
    name = '阿里云百炼'
    source = 'dashscope'

    def __init__(self, client, emit):
        super().__init__(emit)
        self.client = client
        self.task_id = uuid.uuid4().hex
        self.sentences = Sentences()
        for name in ('connect_timeout', 'ready_timeout', 'final_timeout'):
            if getattr(client, name, None):
                setattr(self, name, getattr(client, name))
        self.auto_hints = True
        self.sent_auto_hints = False
        self.started = False   # task-started arrived (no Retry after that)

    def endpoint(self):
        key = resolve(self.client.api_key)
        if not key:
            raise EngineError('阿里云百炼：未配置 API Key')
        headers = {'Authorization': 'Bearer ' + key, 'User-Agent': 'DotVoice'}
        return self.client.endpoint(), headers

    def open(self, ws):
        parameters = self.client.parameters(auto_hints=self.auto_hints)
        self.sent_auto_hints = 'language_hints' in parameters and not self.client.language_hints
        ws.send(json.dumps(run_task(self.task_id, self.client.model_name(), parameters), ensure_ascii=False))

    def retryable(self):
        return self.sent_auto_hints

    def restart(self, retry):
        # The same recording as a new task without the automatic language hint.
        self.auto_hints = False
        self.task_id = uuid.uuid4().hex
        self.sentences = Sentences()
        self.started = False

    def _failed(self, code, message):
        if self.sent_auto_hints and not self.started and hint_refusal(code, message):
            raise Retry(error_message(code, message))
        raise EngineError(error_message(code, message))

    def send_audio(self, ws, chunk):
        ws.send(chunk, opcode=websocket.ABNF.OPCODE_BINARY)

    def finish_audio(self, ws, remainder):
        if remainder:
            ws.send(remainder, opcode=websocket.ABNF.OPCODE_BINARY)
        ws.send(json.dumps(finish_task(self.task_id)))

    def on_text(self, ws, text):
        try:
            message = json.loads(text)
        except ValueError:
            raise EngineError('阿里云百炼返回的数据无法解析') from None
        header = message.get('header') if isinstance(message, dict) else None
        if not isinstance(header, dict):
            return
        task = header.get('task_id')
        if task and task != self.task_id:
            return   # not this recognition
        event = header.get('event')
        if event == 'task-started':
            if not self.started:
                self.started = True
                self.accept()
                if self.retries and not self.auto_hints:
                    self.client.refuse_hints()   # started without the hint: stop sending it
        elif event == 'result-generated':
            output = (message.get('payload') or {}).get('output') or {}
            if self.sentences.update(output.get('sentence')):
                self.partial(self.sentences.text())
        elif event == 'task-finished':
            self.final(self.sentences.text())
        elif event == 'task-failed':
            code = header.get('error_code')
            if code in EMPTY_CODES:
                self.final(self.sentences.text())
                return
            self._failed(code, header.get('error_message'))
        elif event == 'error':
            payload = message.get('payload') or {}
            self._failed(header.get('error_code'), payload.get('message') or header.get('error_message'))

    def handshake_error(self, status, body, headers):
        code = message = ''
        try:
            data = json.loads(body) if body else {}
            if isinstance(data, dict):
                code, message = data.get('code') or '', data.get('message') or ''
        except ValueError:
            pass
        if code:
            return error_message(code, message)
        if status == 401:
            return '阿里云百炼：API Key 无效（注意北京与新加坡地域的 Key 不通用）'
        if status == 403:
            return '阿里云百炼：无权使用实时语音识别（请确认已开通模型服务，子业务空间需授权）'
        if status == 429:
            return '阿里云百炼：请求过于频繁或并发超限，请稍后再试'
        if status >= 500:
            return f'阿里云百炼暂时不可用（HTTP {status}），请稍后重试'
        return f'阿里云百炼拒绝了连接（HTTP {status}）'


class DashScopeASR:
    """Configuration for Bailian real-time ASR; session() starts one recognition.

    api_key may be a string or a zero-argument callable (read when a recording starts).
    region: 'cn' (华北2 北京) or 'intl' (新加坡); workspace: optional Workspace ID for the
    workspace domain the docs recommend; url overrides both."""
    name = '阿里云百炼'
    source = 'dashscope'

    def __init__(self, api_key=None, model=DEFAULT_MODEL, region='cn', workspace='', url=None,
                 language_hints=(), vocabulary_id='', semantic_punctuation=False, max_sentence_silence=None,
                 disfluency_removal=False, heartbeat=True, connect_timeout=None, ready_timeout=None,
                 final_timeout=None):
        if region not in REGIONS:
            raise ValueError('阿里云百炼的地域无效')
        self.api_key, self.model = api_key, model
        self.region, self.workspace, self.url = region, workspace, url
        self.language_hints = [str(h) for h in (language_hints or ()) if h]
        self.vocabulary_id = vocabulary_id
        self.semantic_punctuation, self.max_sentence_silence = semantic_punctuation, max_sentence_silence
        self.disfluency_removal = disfluency_removal
        # heartbeat keeps the task alive through long silent stretches of a long note.
        self.heartbeat = heartbeat
        self.connect_timeout, self.ready_timeout, self.final_timeout = connect_timeout, ready_timeout, final_timeout

    def __repr__(self):   # never shows the key
        return f'DashScopeASR(model={self.model_name()!r}, region={self.region!r})'

    def model_name(self):
        return resolve(self.model) or DEFAULT_MODEL

    def endpoint(self):
        if self.url:
            return self.url
        workspace = resolve(self.workspace) or ''
        legacy, dedicated = REGIONS[self.region]
        return dedicated.format(workspace=workspace) if workspace else legacy

    def check(self):
        """Cheap configuration check (no network). ValueError with a Chinese message."""
        workspace = resolve(self.workspace) or ''
        if workspace and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{1,62}', workspace):
            raise ValueError('阿里云百炼：Workspace ID 格式不正确')
        check_ws_url(self.endpoint(), '阿里云百炼')
        key = resolve(self.api_key)
        if not key:
            raise ValueError('阿里云百炼：请填写 API Key（百炼控制台“API Key”页面）')
        if not (key.isascii() and len(key) <= 512 and not any(c.isspace() for c in key)):
            raise ValueError('阿里云百炼：API Key 格式不正确（不能含空格或中文）')
        model = self.model_name()
        if not re.fullmatch(r'[a-z0-9][a-z0-9._\-]{2,63}', model):
            raise ValueError('阿里云百炼：模型名称格式不正确（例如 fun-asr-realtime）')
        # This protocol (run-task on /api-ws/v1/inference) serves Fun-ASR and Paraformer realtime;
        # qwen3-asr-flash-realtime speaks a different one, and 8k models reject 16 kHz audio.
        if not re.match(r'(fun-asr|paraformer)[a-z0-9.\-]*realtime', model) or '8k' in model:
            raise ValueError(f'阿里云百炼：不支持模型 {model}（请用 fun-asr-realtime 或 paraformer-realtime-v2）')

    def hints(self, auto=True):
        """language_hints to send: the configured ones, else ["zh"] for Fun-ASR realtime (unless
        that model refused it in this process), else none."""
        if self.language_hints:
            return list(self.language_hints)
        model = self.model_name()
        if auto and AUTO_HINT_MODELS.fullmatch(model) and model not in _hints_refused:
            return list(AUTO_HINTS)
        return []

    def refuse_hints(self):
        _hints_refused.add(self.model_name())

    def parameters(self, auto_hints=True):
        parameters = {'format': 'pcm', 'sample_rate': 16000}
        if self.heartbeat:
            parameters['heartbeat'] = True
        hints = self.hints(auto_hints)
        if hints:
            parameters['language_hints'] = hints
        vocabulary = resolve(self.vocabulary_id)
        if vocabulary:
            parameters['vocabulary_id'] = vocabulary
        if self.semantic_punctuation:
            parameters['semantic_punctuation_enabled'] = True
        if self.max_sentence_silence:
            parameters['max_sentence_silence'] = min(6000, max(200, int(self.max_sentence_silence)))
        if self.disfluency_removal and self.model_name().startswith('paraformer'):
            parameters['disfluency_removal_enabled'] = True   # Paraformer only
        return parameters

    def session(self, emit):
        return DashScopeSession(self, emit)

    def probe(self, timeout=12):
        """User-triggered connection test (sends 0.5 s of silence): (ok, Chinese message)."""
        return probe(self, timeout)


def main(argv=None):
    from .stream import cli_wav_parser, configure_console, run_wav
    configure_console()
    parser = cli_wav_parser('用真实密钥测试阿里云百炼实时语音识别（密钥从环境变量读取，不会打印）')
    parser.add_argument('--model', default=os.environ.get('DASHSCOPE_MODEL') or DEFAULT_MODEL)
    parser.add_argument('--region', choices=sorted(REGIONS), default=os.environ.get('DASHSCOPE_REGION') or 'cn')
    parser.add_argument('--workspace', default=os.environ.get('DASHSCOPE_WORKSPACE') or '')
    args = parser.parse_args(argv)
    client = DashScopeASR(api_key=os.environ.get('DASHSCOPE_API_KEY') or None, model=args.model,
                          region=args.region, workspace=args.workspace)
    try:
        client.check()
    except ValueError as exc:
        print(exc, '\n请设置环境变量 DASHSCOPE_API_KEY')
        return 2
    print(f'阿里云百炼：{client.endpoint()}，模型 {client.model_name()}')
    return run_wav(client, args.wav, realtime=not args.fast)


if __name__ == '__main__':
    raise SystemExit(main())
