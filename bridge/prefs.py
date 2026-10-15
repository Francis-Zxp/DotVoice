"""User preferences. Only whitelisted keys and value types are accepted.

API keys never live here: bridge.secrets keeps them (DPAPI). The engine section holds
only plain settings (which engine, resource / model names, the polish relay's URL)."""
import copy
import json
from pathlib import Path
import re
import threading
import time

ENGINE_KINDS = ('openless', 'volc', 'dashscope', 'hotkey')
RECOVERED = 'prefs-recovered.json'  # where settings go while prefs.json cannot be read
READ_RETRY = .25                    # another program (antivirus, backup) briefly holding it
VOLC_ENDPOINTS = ('bigmodel_async', 'bigmodel', 'bigmodel_nostream')
VOLC_RESOURCE = 'volc.seedasr.sauc.duration'
DASHSCOPE_MODEL = 'fun-asr-realtime'
OVERLAY_ANCHORS = ('top-left', 'top-center', 'top-right', 'center-left', 'center', 'center-right',
                   'bottom-left', 'bottom-center', 'bottom-right')

DEFAULTS = {
    'version': 1,
    'mode': 'input',
    'category': 'idea',  # 0.6 leftover; notes now use tags
    'sounds': {'enabled': True, 'volume': 'medium', 'keep_awake': True},
    'overlay': {'enabled': True, 'on_switch': True, 'linger_seconds': 5, 'position': None, 'monitor': None,
                'all_monitors': False, 'anchor': 'top-center', 'positions': {}},
    # ⑤ taps click, ③/④ long presses grab / right-click; no window after ⑤ any more
    # (0.10's airmouse.window_ms is retired: skipped when read).
    'airmouse': {'enabled': True, 'skip_slideshow': True},
    'hotkeys': {'toggle_mode': '', 'arm_toggle': ''},
    # live_shortcut: the shell gives desktop/Start/taskbar shortcuts a live green/red dot.
    'appearance': {'theme': 'system', 'reduced_motion': False, 'live_shortcut': True},
    'present': {'tool': 'laser', 'hide_cursor': True, 'zoom': 200},
    # 气声: auto = lift speech that stays soft, on = every recording, off = untouched.
    'voice': {'whisper': 'auto'},
    # 随拿随用: the Baseus pen and Tidepool both stay ready; OpenLess runs one session at a time.
    'devices': {'coexist': True},
    # Swallow the PowerPoint shortcuts the pen also types (⑧ Ctrl+P = print) outside slideshows.
    'pen': {'guard_shortcuts': True},
    # Opt-in: keep the last 3 recordings (raw + as sent to the engine) in runtime/diagnostics
    # for tuning the 气声 front end; local only, deleted after 24 h (bridge.diagnostics).
    'diagnostics': {'save_audio': False},
    # Who turns speech into text (bridge.engines). OpenLess stays the default so an existing
    # install keeps working as before; the setup wizard picks one on a fresh install.
    # volc.auth: 'key' = new-console API Key, 'legacy' = App ID + Access Token (old console).
    # hotkey.keys: '' = the app's documented shortcut; hotkey.style only for the custom profile.
    'engine': {
        'kind': 'openless',
        'volc': {'auth': 'key', 'app_id': '', 'resource_id': VOLC_RESOURCE, 'endpoint': 'bigmodel_async',
                 'hotwords': True},
        'dashscope': {'model': DASHSCOPE_MODEL, 'region': 'cn'},
        'polish': {'style': 'raw', 'base_url': '', 'model': '', 'prompt': ''},
        'hotkey': {'profile': 'shandianshuo', 'keys': '', 'style': 'toggle'},
    },
    # done: the first-run wizard finished or was skipped (an existing install counts as done).
    'setup': {'done': False},
    # run_elevated (兼容管理员权限窗口): DotVoice runs as administrator so its clicks and typing
    # reach elevated windows. Only the shell changes it (elevation.rs: UAC, the logon task,
    # the restart); it mirrors whether that task exists. Never switched on automatically.
    'general': {'run_elevated': False},
}

# Each leaf validator returns the cleaned value or raises ValueError.
def _choice(*options):
    def check(value):
        if value not in options:
            raise ValueError('选项无效')
        return value
    return check


def _int_range(low, high):
    def check(value):
        if type(value) is not int or not low <= value <= high:
            raise ValueError('数值超出范围')
        return value
    return check


def _bool(value):
    if type(value) is not bool:
        raise ValueError('开关值无效')
    return value


def _accelerator(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('快捷键无效')
    return value.strip()


def _position(value):
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'x', 'y'} or
            not all(type(value[k]) is int and -20000 <= value[k] <= 20000 for k in ('x', 'y'))):
        raise ValueError('位置无效')
    return dict(value)


def _text(limit, multiline=False):
    """Plain text, trimmed; no control characters (a prompt may keep line breaks and tabs)."""
    def check(value):
        if not isinstance(value, str):
            raise ValueError('文本无效')
        value = value.strip()
        if len(value) > limit:
            raise ValueError(f'内容过长（最多 {limit} 个字符）')
        if any((ord(c) < 32 and not (multiline and c in '\n\t')) or ord(c) == 127 for c in value):
            raise ValueError('内容含有无效字符')
        return value
    return check


def _monitor(value):
    if value is None:
        return None
    value = _text(256)(value)
    if not value:
        raise ValueError('显示器名称不能为空')
    return value


def _positions(value):
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError('最多保存 32 个显示器的位置')
    positions = {}
    for name, position in value.items():
        name = _monitor(name)
        if name is None or name in positions or position is None:
            raise ValueError('显示器位置无效')
        positions[name] = _position(position)
    return positions


def _app_id(value):
    value = _text(20)(value)
    if value and not re.fullmatch(r'\d{4,20}', value):
        raise ValueError('豆包语音：App ID 应为纯数字（控制台“APP ID”一栏）')
    return value


def _resource_id(value):
    value = _text(128)(value)
    if not value:
        return VOLC_RESOURCE
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.\-]{2,127}', value):
        raise ValueError('豆包语音：Resource ID 格式不正确（例如 volc.seedasr.sauc.duration）')
    return value


def _dashscope_model(value):
    # Same rule as DashScopeASR.check: Fun-ASR / Paraformer realtime models at 16 kHz.
    value = _text(64)(value) or DASHSCOPE_MODEL
    if (not re.fullmatch(r'[a-z0-9][a-z0-9._\-]{2,63}', value)
            or not re.match(r'(fun-asr|paraformer)[a-z0-9.\-]*realtime', value) or '8k' in value):
        raise ValueError('阿里云百炼：只支持 fun-asr-realtime、paraformer-realtime-v2 这类实时模型')
    return value


def _base_url(value):
    value = _text(512)(value)
    if value:
        from .engines.llm import chat_url
        chat_url(value)  # ValueError with a Chinese message (https only, except this PC / the LAN)
    return value


def _hotkey_profile(value):
    from .engines.hotkey import CUSTOM, PROFILES
    if value != CUSTOM and value not in PROFILES:
        raise ValueError('未知的听写软件')
    return value


def _combo(value):
    """A shortcut as typed or recorded ('ControlLeft+MetaLeft', 'ctrl+win', '右 Alt'), kept as
    its display text ('Ctrl + Win'); '' means the app's own default."""
    value = _text(64)(value)
    if not value:
        return ''
    from .engines.hotkey import format_combo, parse_combo
    return format_combo(parse_combo(value))


SCHEMA = {
    'mode': _choice('input', 'notes', 'present'),
    'category': _choice('idea', 'paper'),
    'sounds': {'enabled': _bool, 'volume': _choice('low', 'medium', 'high'), 'keep_awake': _bool},
    'overlay': {'enabled': _bool, 'on_switch': _bool, 'linger_seconds': _int_range(2, 30),
                'position': _position, 'monitor': _monitor, 'all_monitors': _bool,
                'anchor': _choice(*OVERLAY_ANCHORS), 'positions': _positions},
    'airmouse': {'enabled': _bool, 'skip_slideshow': _bool},
    'hotkeys': {'toggle_mode': _accelerator, 'arm_toggle': _accelerator},
    'appearance': {'theme': _choice('system', 'light', 'dark', 'glass'), 'reduced_motion': _bool,
                   'live_shortcut': _bool},
    'present': {'tool': _choice('laser', 'spotlight', 'magnifier', 'mouse'), 'hide_cursor': _bool,
                'zoom': _int_range(125, 400)},  # magnifier, percent
    'voice': {'whisper': _choice('auto', 'on', 'off')},
    'devices': {'coexist': _bool},
    'pen': {'guard_shortcuts': _bool},
    'diagnostics': {'save_audio': _bool},
    'engine': {
        'kind': _choice(*ENGINE_KINDS),
        'volc': {'auth': _choice('key', 'legacy'), 'app_id': _app_id, 'resource_id': _resource_id,
                 'endpoint': _choice(*VOLC_ENDPOINTS), 'hotwords': _bool},
        'dashscope': {'model': _dashscope_model, 'region': _choice('cn', 'intl')},
        'polish': {'style': _choice('raw', 'light', 'custom'), 'base_url': _base_url, 'model': _text(128),
                   'prompt': _text(2000, multiline=True)},
        'hotkey': {'profile': _hotkey_profile, 'keys': _combo, 'style': _choice('toggle', 'hold')},
    },
    'setup': {'done': _bool},
    'general': {'run_elevated': _bool},
}


def _merge(target, patch, schema, path=''):
    if not isinstance(patch, dict):
        raise ValueError('设置格式无效：' + (path or '根'))
    for key, value in patch.items():
        if key not in schema:
            raise ValueError('未知设置：' + path + key)
        rule = schema[key]
        if isinstance(rule, dict):
            _merge(target[key], value, rule, path + key + '.')
        else:
            target[key] = rule(value)


def _load(target, saved, schema):
    """Apply a saved file leaf by leaf: a retired or unknown key (preview,
    voice.fast_raw, airmouse.window_ms, notes.pause_ms) or one invalid value is
    skipped on its own and never takes the rest of its section with it."""
    for key, value in saved.items():
        rule = schema.get(key)
        if rule is None:
            continue
        if isinstance(rule, dict):
            if isinstance(value, dict):
                _load(target[key], value, rule)
            continue
        try:
            target[key] = rule(value)
        except (ValueError, KeyError, TypeError):
            pass


def _read(path):
    """(settings, None) or (None, 'busy' | 'corrupt'). A file another program holds open
    (Windows sharing violation) is tried once more before giving up."""
    for attempt in (0, 1):
        try:
            saved = json.loads(path.read_text(encoding='utf-8'))
        except OSError:
            if attempt:
                return None, 'busy'
            time.sleep(READ_RETRY)
            continue
        except ValueError:
            return None, 'corrupt'
        if not isinstance(saved, dict):
            return None, 'corrupt'
        saved.pop('version', None)
        return saved, None
    return None, 'busy'


class Preferences:
    def __init__(self, path, legacy=None, existing=None):
        """`existing`: DotVoice has run here before (default: the file exists). Only a fresh
        install starts with setup.done False, so the first-run wizard never surprises an
        existing user, even one whose prefs.json was never written."""
        self.path = Path(path)
        self.lock = threading.RLock()
        self.data = copy.deepcopy(DEFAULTS)
        self.warning = ''
        found = self.path.exists()
        if existing is None:
            existing = found
        if existing:
            self.data['setup']['done'] = True
        if found:
            saved, problem = _read(self.path)
            if saved is not None:
                _load(self.data, saved, SCHEMA)
            else:
                # prefs.json is kept as it is. Changes go to prefs-recovered.json, which is read
                # back while prefs.json stays unreadable - and the user is told every time.
                recovered = self.path.with_name(RECOVERED)
                kept = _read(recovered)[0] if recovered.exists() else None
                if kept is not None:
                    _load(self.data, kept, SCHEMA)
                self.path = recovered
                cause = ('被其它程序占用，暂时无法读取' if problem == 'busy' else '已损坏，无法读取')
                using = ('正在使用之前另存的设置（prefs-recovered.json）' if kept is not None
                         else '本次使用默认设置')
                self.warning = (f'偏好设置文件 prefs.json {cause}：{using}；原文件保留，'
                                f'修改会保存到 prefs-recovered.json。')
        elif legacy:
            for patch in legacy:
                try:
                    _merge(self.data, patch, SCHEMA)
                except (ValueError, KeyError, TypeError):
                    pass

    def get(self, *keys):
        with self.lock:
            value = self.data
            for key in keys:
                value = value[key]
            return copy.deepcopy(value)

    def snapshot(self):
        return self.get()

    def validate(self, patch):
        """The settings `patch` would produce, without applying it (ValueError if invalid)."""
        with self.lock:
            candidate = copy.deepcopy(self.data)
            _merge(candidate, patch, SCHEMA)
            return candidate

    def update(self, patch, persist=True):
        """persist=False changes memory only; the caller saves after giving feedback."""
        with self.lock:
            candidate = copy.deepcopy(self.data)
            _merge(candidate, patch, SCHEMA)
            if persist:
                self._write(candidate)
            self.data = candidate
            return copy.deepcopy(self.data)

    def save(self):
        with self.lock:
            self._write(self.data)

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)
