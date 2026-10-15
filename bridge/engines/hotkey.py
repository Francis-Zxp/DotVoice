"""Hotkey bridge: the pen's record key drives another Windows dictation app.

闪电说, Typeless, 豆包输入法, 微信 and Windows 语音输入 offer no API DotVoice could
call, but each starts and stops dictation from a keyboard shortcut. This engine
types that shortcut when the record key goes down and up:

* ``toggle`` apps (a tap starts, the next tap stops): a short tap at the press and
  another at the release. 闪电说 turns a *long* press into 帮我说, so every tap is
  short (``tap_ms``, checked against ``long_press_ms``).
* ``hold`` apps (hold to talk): key-down at the press, key-up at the release.

The app records from the microphone selected in the app itself (the user picks the
pen's receiver there once; ``mic_hint`` says where) and types the text itself, so
DotVoice never sees the words: shortly after the stop, poll() reports one empty
``{"type": "result", "text": "", "external": True}`` and the controller goes idle.
DotVoice may keep its own capture open for the level meter; feed() ignores audio and
this module never opens an audio device.

Keystrokes carry the virtual-key code *and* the set-1 scan code, with
KEYEVENTF_EXTENDEDKEY on the E0 keys (Right Alt/Ctrl, Win, arrows, the editing
block), so low-level hooks, Raw Input readers and TSF input methods (豆包输入法)
all see a plausible key. Limits of synthetic input that user mode cannot get around:

* UIPI: while an elevated (administrator) window is in the foreground Windows
  refuses SendInput, and an app that runs elevated may not see DotVoice's keys.
* Injected keys carry LLKHF_INJECTED; an app that ignores injected input cannot be
  driven this way.
* Scan codes follow US/QWERTY positions (the layout Chinese input methods sit on).
* The app's state is invisible: if a toggle app stops by itself (silence or length
  limit), the next tap starts it again; one more pen press puts them back in step.

Stuck keys: a key is recorded as held *before* its key-down is sent and is released
on every path (stop, cancel, close, exceptions, interpreter exit). A key-up that
Windows refuses stays pending and is retried (poll, a background retry for two
minutes, the next connect/start/close) until it goes through.
"""
from __future__ import annotations

import atexit
import ctypes
from dataclasses import dataclass, replace
import os
import re
import sys
import threading
import time
import weakref

from ..backend import VoiceBackend

# -- keys --------------------------------------------------------------------------
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
INPUT_KEYBOARD = 1

# Canonical key names are the VK_ names without the prefix (plus NUMPADENTER, which
# shares VK_RETURN): name -> (virtual-key code, set-1 scan code, extended). Extended
# means a real keyboard sends that scan code with an E0 prefix.
KEYS = {
    'LSHIFT': (0xA0, 0x2A, False), 'RSHIFT': (0xA1, 0x36, False),
    'LCONTROL': (0xA2, 0x1D, False), 'RCONTROL': (0xA3, 0x1D, True),
    'LMENU': (0xA4, 0x38, False), 'RMENU': (0xA5, 0x38, True),
    'LWIN': (0x5B, 0x5B, True), 'RWIN': (0x5C, 0x5C, True), 'APPS': (0x5D, 0x5D, True),
    'ESCAPE': (0x1B, 0x01, False), 'TAB': (0x09, 0x0F, False), 'BACK': (0x08, 0x0E, False),
    'RETURN': (0x0D, 0x1C, False), 'SPACE': (0x20, 0x39, False), 'CAPITAL': (0x14, 0x3A, False),
    'INSERT': (0x2D, 0x52, True), 'DELETE': (0x2E, 0x53, True),
    'HOME': (0x24, 0x47, True), 'END': (0x23, 0x4F, True),
    'PRIOR': (0x21, 0x49, True), 'NEXT': (0x22, 0x51, True),
    'LEFT': (0x25, 0x4B, True), 'UP': (0x26, 0x48, True),
    'RIGHT': (0x27, 0x4D, True), 'DOWN': (0x28, 0x50, True),
    'SNAPSHOT': (0x2C, 0x37, True), 'SCROLL': (0x91, 0x46, False), 'PAUSE': (0x13, 0x45, False),
    'NUMLOCK': (0x90, 0x45, True), 'DIVIDE': (0x6F, 0x35, True), 'MULTIPLY': (0x6A, 0x37, False),
    'SUBTRACT': (0x6D, 0x4A, False), 'ADD': (0x6B, 0x4E, False), 'DECIMAL': (0x6E, 0x53, False),
    'NUMPADENTER': (0x0D, 0x1C, True),
    'OEM_1': (0xBA, 0x27, False), 'OEM_PLUS': (0xBB, 0x0D, False), 'OEM_COMMA': (0xBC, 0x33, False),
    'OEM_MINUS': (0xBD, 0x0C, False), 'OEM_PERIOD': (0xBE, 0x34, False), 'OEM_2': (0xBF, 0x35, False),
    'OEM_3': (0xC0, 0x29, False), 'OEM_4': (0xDB, 0x1A, False), 'OEM_5': (0xDC, 0x2B, False),
    'OEM_6': (0xDD, 0x1B, False), 'OEM_7': (0xDE, 0x28, False), 'OEM_102': (0xE2, 0x56, False),
}
for _row, _first in (('QWERTYUIOP', 0x10), ('ASDFGHJKL', 0x1E), ('ZXCVBNM', 0x2C)):
    for _i, _ch in enumerate(_row):
        KEYS[_ch] = (ord(_ch), _first + _i, False)
for _i, _ch in enumerate('1234567890'):
    KEYS[_ch] = (ord(_ch), 0x02 + _i, False)
for _i, _scan in enumerate((0x52, 0x4F, 0x50, 0x51, 0x4B, 0x4C, 0x4D, 0x47, 0x48, 0x49)):
    KEYS[f'NUMPAD{_i}'] = (0x60 + _i, _scan, False)
for _n in range(1, 25):  # F1-F10 3B-44, F11 57, F12 58, F13-F23 64-6E, F24 76
    _scan = 0x3A + _n if _n <= 10 else {11: 0x57, 12: 0x58, 24: 0x76}.get(_n, 0x64 + _n - 13)
    KEYS[f'F{_n}'] = (0x6F + _n, _scan, False)

MODIFIERS = frozenset(('LSHIFT', 'RSHIFT', 'LCONTROL', 'RCONTROL', 'LMENU', 'RMENU', 'LWIN', 'RWIN'))
MAX_KEYS = 4


def key_input(name, up=False):
    """(vk, scan, flags) for one key-down or key-up, the form send_keys_scan takes."""
    try:
        vk, scan, extended = KEYS[name]
    except KeyError:
        raise ValueError(f'未知按键：{name}') from None
    return vk, scan, (KEYEVENTF_EXTENDEDKEY if extended else 0) | (KEYEVENTF_KEYUP if up else 0)


# -- combos ------------------------------------------------------------------------
def _norm(token):
    token = token.strip()
    if len(token) == 1:
        return token.casefold()  # '-', '=', '`' ... are keys, not separators
    token = re.sub(r'[\s_\-]+', '', token).casefold()
    for wordy, side in (('右边', '右'), ('右侧', '右'), ('左边', '左'), ('左侧', '左')):
        token = token.replace(wordy, side)
    return token


ALIASES = {}


def _alias(name, *spellings):
    for spelling in spellings:
        ALIASES[_norm(spelling)] = name


for _name in list(KEYS):
    _alias(_name, _name, 'VK_' + _name)
# Plain modifier names mean the left key, which is what DotVoice then sends.
_alias('LCONTROL', 'Ctrl', 'Control', 'Ctl', 'VK_CONTROL', 'CommandOrControl', 'CmdOrCtrl', 'CmdOrControl',
       'CommandOrCtrl', 'LCtrl', 'LeftCtrl', 'LeftControl', 'ControlLeft', 'CtrlLeft', '左Ctrl', '左Control')
_alias('RCONTROL', 'RCtrl', 'RightCtrl', 'RightControl', 'ControlRight', 'CtrlRight', '右Ctrl', '右Control')
_alias('LMENU', 'Alt', 'VK_MENU', 'LAlt', 'LeftAlt', 'AltLeft', '左Alt')
_alias('RMENU', 'RAlt', 'RightAlt', 'AltRight', 'AltGr', 'AltGraph', '右Alt')
_alias('LSHIFT', 'Shift', 'VK_SHIFT', 'LShift', 'LeftShift', 'ShiftLeft', '左Shift')
_alias('RSHIFT', 'RShift', 'RightShift', 'ShiftRight', '右Shift')
_alias('LWIN', 'Win', 'Windows', 'WinKey', 'WindowsKey', 'Super', 'Meta', 'Cmd', 'Command', 'OS',
       'LeftWin', 'WinLeft', 'LeftWindows', 'MetaLeft', 'OSLeft', 'SuperLeft', 'LeftSuper', 'LeftMeta',
       '左Win', 'Win键', '徽标键')
_alias('RWIN', 'RightWin', 'WinRight', 'RightWindows', 'MetaRight', 'OSRight', 'SuperRight', 'RightSuper',
       'RightMeta', '右Win')
_alias('APPS', 'ContextMenu', 'Application', '菜单键')
_alias('ESCAPE', 'Esc')
_alias('BACK', 'Backspace', 'BkSp')
_alias('RETURN', 'Enter', '回车', '回车键')
_alias('NUMPADENTER', 'NumpadEnter', 'NumEnter', '小键盘Enter')
_alias('SPACE', 'Spacebar', '空格', '空格键')
_alias('CAPITAL', 'CapsLock', 'Caps')
_alias('INSERT', 'Ins')
_alias('DELETE', 'Del')
_alias('PRIOR', 'PageUp', 'PgUp')
_alias('NEXT', 'PageDown', 'PgDn', 'PgDown')
_alias('LEFT', 'ArrowLeft', 'LeftArrow', '←')
_alias('UP', 'ArrowUp', 'UpArrow', '↑')
_alias('RIGHT', 'ArrowRight', 'RightArrow', '→')
_alias('DOWN', 'ArrowDown', 'DownArrow', '↓')
_alias('SNAPSHOT', 'PrintScreen', 'PrtSc', 'PrtScn', 'PrintScrn', 'Print')
_alias('SCROLL', 'ScrollLock')
_alias('PAUSE', 'Break')
_alias('DIVIDE', 'NumpadDivide')
_alias('MULTIPLY', 'NumpadMultiply')
_alias('SUBTRACT', 'NumpadSubtract')
_alias('ADD', 'NumpadAdd')
_alias('DECIMAL', 'NumpadDecimal')
_alias('OEM_1', ';', 'Semicolon')
_alias('OEM_PLUS', '=', 'Equal', 'Equals', 'Plus')
_alias('OEM_COMMA', ',', 'Comma')
_alias('OEM_MINUS', '-', 'Minus')
_alias('OEM_PERIOD', '.', 'Period', 'Dot')
_alias('OEM_2', '/', 'Slash')
_alias('OEM_3', '`', 'Backquote', 'Grave')
_alias('OEM_4', '[', 'BracketLeft')
_alias('OEM_5', '\\', 'Backslash')
_alias('OEM_6', ']', 'BracketRight')
_alias('OEM_7', "'", 'Quote', 'Apostrophe')
_alias('OEM_102', 'IntlBackslash')
for _n in range(10):
    _alias(f'NUMPAD{_n}', f'Num{_n}', f'小键盘{_n}')
for _ch in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ':
    _alias(_ch, 'Key' + _ch)  # KeyboardEvent.code
for _ch in '0123456789':
    _alias(_ch, 'Digit' + _ch)


def parse_combo(combo):
    """'Ctrl+Alt+Space', 'RightAlt', 'Win+H', '右 Alt', ['LCONTROL', 'LWIN'] -> canonical key
    names, modifiers first in the order given. ValueError (a message for the user) if a
    key is unknown or repeated, or there is more than one non-modifier key."""
    if isinstance(combo, str):
        if not combo.strip():
            raise ValueError('快捷键为空')
        if len(combo) > 64:
            raise ValueError('快捷键太长')
        tokens = combo.split('+')
    elif isinstance(combo, (list, tuple)) and all(isinstance(token, str) for token in combo):
        tokens = list(combo)
        if not tokens:
            raise ValueError('快捷键为空')
    else:
        raise ValueError('快捷键格式无效')
    keys = []
    for token in tokens:
        if not token.strip():
            raise ValueError('快捷键格式无效：「+」两边都要有按键')
        name = ALIASES.get(_norm(token))
        if name is None:
            raise ValueError(f'无法识别的按键：{token.strip()}')
        if name in keys:
            raise ValueError(f'按键重复：{token.strip()}')
        keys.append(name)
    main = [name for name in keys if name not in MODIFIERS]
    if len(main) > 1:
        raise ValueError('快捷键里只能有一个 Ctrl / Alt / Shift / Win 以外的键')
    if len(keys) > MAX_KEYS:
        raise ValueError(f'快捷键最多 {MAX_KEYS} 个键')
    return tuple([name for name in keys if name in MODIFIERS] + main)


DISPLAY = {
    'LCONTROL': 'Ctrl', 'RCONTROL': '右 Ctrl', 'LMENU': 'Alt', 'RMENU': '右 Alt',
    'LSHIFT': 'Shift', 'RSHIFT': '右 Shift', 'LWIN': 'Win', 'RWIN': '右 Win', 'APPS': '菜单键',
    'ESCAPE': 'Esc', 'BACK': 'Backspace', 'RETURN': 'Enter', 'NUMPADENTER': '小键盘 Enter',
    'SPACE': '空格', 'CAPITAL': 'CapsLock', 'PRIOR': 'PageUp', 'NEXT': 'PageDown',
    'SNAPSHOT': 'PrintScreen', 'SCROLL': 'ScrollLock', 'NUMLOCK': 'NumLock',
    'DIVIDE': 'NumpadDivide', 'MULTIPLY': 'NumpadMultiply', 'SUBTRACT': 'NumpadSubtract',
    'ADD': 'NumpadAdd', 'DECIMAL': 'NumpadDecimal',
    'OEM_1': ';', 'OEM_PLUS': '=', 'OEM_COMMA': ',', 'OEM_MINUS': '-', 'OEM_PERIOD': '.',
    'OEM_2': '/', 'OEM_3': '`', 'OEM_4': '[', 'OEM_5': '\\', 'OEM_6': ']', 'OEM_7': "'",
    'OEM_102': 'IntlBackslash',
    **{f'NUMPAD{n}': f'小键盘 {n}' for n in range(10)},
}


def format_combo(keys):
    """Display text, e.g. ('LCONTROL', 'LWIN') -> 'Ctrl + Win'; parse_combo reads it back."""
    return ' + '.join(DISPLAY.get(name) or name.title() for name in keys)


# -- profiles ----------------------------------------------------------------------
STYLES = ('toggle', 'hold')
CUSTOM = 'custom'
DEFAULT_MIC = 'BaseusPresent'  # bridge.devices: the receiver's microphone (汉王 PV60: Ai Pointer)
_LIMITS = {'tap_ms': ('短按时长', 20, 150), 'gap_ms': ('组合键间隔', 0, 50),
           'lead_ms': ('结束前等待', 0, 1000), 'done_ms': ('结束后等待', 0, 3000)}
# Apps without their own microphone choice record from the Windows default input device.
_SYSTEM_MIC = ('打开 Windows 设置 → 系统 → 声音 → 输入，把「{mic}」（翻页笔接收器）设为默认录音设备'
               '（这会改变全系统的默认麦克风）。')


@dataclass(frozen=True)
class Profile:
    """How one dictation app is driven. Times are milliseconds."""
    id: str
    name: str                  # Chinese display name
    keys: tuple                # canonical key names, pressed in this order
    style: str = 'toggle'      # 'toggle': tap to start, tap again to stop; 'hold': held while talking
    tap_ms: int = 50           # how long a tap holds its keys
    lead_ms: int = 150         # wait before the stop action so the last syllable reaches the app
    done_ms: int = 300         # after the stop action, when DotVoice reports the session done
    gap_ms: int = 10           # between the keys of a chord, like fingers
    mic_hint: str = ''         # where to pick the pen's microphone in this app; {mic} = its name
    cancel_keys: tuple = ()    # tapped after the stop tap on cancel (闪电说: Esc discards)
    long_press_ms: int | None = None  # the app's long-press threshold; a tap must stay far below it
    custom: bool = False       # keys recorded by the user instead of the app's default
    verified: bool = False     # checked against the real app on a PC
    notes: str = ''
    sources: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, 'keys', parse_combo(self.keys))
        object.__setattr__(self, 'cancel_keys', parse_combo(self.cancel_keys) if self.cancel_keys else ())
        object.__setattr__(self, 'sources', tuple(self.sources))

    @property
    def combo(self):
        return format_combo(self.keys)

    @property
    def tap_hold_ms(self):
        """Longest time one key is down during a tap: the first key goes down first, up last."""
        return self.tap_ms + 2 * self.gap_ms * (len(self.keys) - 1)

    def validate(self):
        """Raise ValueError (a message for the user) unless this profile can be driven."""
        if not isinstance(self.id, str) or not re.fullmatch(r'[a-z0-9_]{1,32}', self.id):
            raise ValueError('听写软件配置编号无效')
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError('听写软件名称不能为空')
        if self.style not in STYLES:
            raise ValueError('触发方式只能是「按一下开始、再按一下结束」(toggle) 或「按住说话」(hold)')
        for field, (label, low, high) in _LIMITS.items():
            value = getattr(self, field)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f'{label}超出范围（{low}–{high} 毫秒）')
        if self.long_press_ms is not None:
            if type(self.long_press_ms) is not int or not 100 <= self.long_press_ms <= 5000:
                raise ValueError('长按阈值超出范围（100–5000 毫秒）')
            if self.tap_hold_ms * 2 > self.long_press_ms:
                raise ValueError(f'短按要按住约 {self.tap_hold_ms} 毫秒，离「{self.name}」的长按'
                                 f'（约 {self.long_press_ms} 毫秒）太近，可能被当成长按')
        return self

    def mic_text(self, mic=DEFAULT_MIC):
        return self.mic_hint.replace('{mic}', mic)

    def describe(self, mic=DEFAULT_MIC):
        return {'profile': self.id, 'name': self.name, 'keys': list(self.keys), 'combo': self.combo,
                'style': self.style, 'mic_hint': self.mic_text(mic), 'notes': self.notes,
                'custom': self.custom, 'verified': self.verified}


# Defaults from each app's documentation or press coverage (sources); none is verified
# on a PC yet. Each app lets the user change its hotkey: then pass keys= to get_profile.
PROFILES = {
    # 闪电说 documents no long-press threshold; 300 ms is an assumed lower bound (common
    # values are 300-500 ms). Its 40 ms tap holds Ctrl about 60 ms in all.
    'shandianshuo': Profile(
        id='shandianshuo', name='闪电说', keys=('LCONTROL', 'LWIN'), style='toggle',
        tap_ms=40, lead_ms=150, cancel_keys=('ESCAPE',), long_press_ms=300,
        mic_hint='打开闪电说「设置」，在「麦克风」里固定选择「{mic}」（翻页笔接收器），不要用系统默认设备。',
        notes='短按快捷键开始、再短按结束；长按会变成「帮我说」，所以点声只发短按。Esc 取消录音（0.7.5 起）。'
              '快捷键以闪电说首页顶部显示的为准（Windows 默认据报道是 Ctrl + Win），改过请在点声里录入同一组合；'
              'Ctrl + Win 也是微信语音输入的默认键，两者同时运行会冲突。',
        sources=('https://www.shandianshuo.cn/docs/beginner/settings',
                 'https://www.shandianshuo.cn/docs/voice-input',
                 'https://shandianshuo.featurebase.app/changelog',
                 'https://juejin.cn/post/7603653885602299954')),
    'typeless': Profile(
        id='typeless', name='Typeless', keys=('RMENU',), style='toggle', tap_ms=50, lead_ms=150,
        mic_hint='打开 Typeless 设置 → Audio → Microphone，选择「{mic}」（翻页笔接收器），不要用 Auto-detect。',
        notes='按一次右 Alt 开始听写，再按一次结束并输入文字。',
        sources=('https://www.typeless.com/help/quickstart/dictate',
                 'https://www.typeless.com/help/quickstart/settings')),
    'doubao_ime': Profile(
        id='doubao_ime', name='豆包输入法', keys=('RMENU',), style='hold', lead_ms=150,
        mic_hint='在豆包输入法的语音输入设置里，把麦克风选为「{mic}」（翻页笔接收器），并保持「长按模式」（按住说话、松手结束）。',
        notes='豆包输入法是输入法：只有它是当前输入法时右 Alt 才会唤起语音。唤起键也可改为「右 Alt + 空格」或'
              '「左 Ctrl + Win」，改了请在点声里同步。按住式没有取消：松开即提交。',
        sources=('https://www.jamecling.com/archives/6406', 'https://www.ithome.com/0/999/934.htm')),
    'wechat': Profile(
        id='wechat', name='微信语音输入', keys=('LCONTROL', 'LWIN'), style='hold', lead_ms=150,
        mic_hint='微信语音输入用 Windows 默认麦克风（未见单独的麦克风选项）：' + _SYSTEM_MIC,
        notes='需要微信 4.1.8 或更高版本。按住 Ctrl + Win 说话、松开结束，任意输入框可用；'
              '快捷键可在微信「设置 → 快捷键」修改。按住式没有取消：松开即提交。',
        sources=('https://m.gmw.cn/2026-03/22/content_1304386825.htm', 'https://www.ithome.com/0/929/065.htm')),
    'wechat_handsfree': Profile(
        id='wechat_handsfree', name='微信语音输入（免手持）', keys=('LCONTROL', 'LSHIFT', 'LWIN'),
        style='toggle', tap_ms=50, lead_ms=150,
        mic_hint='微信语音输入用 Windows 默认麦克风（未见单独的麦克风选项）：' + _SYSTEM_MIC,
        notes='官方只说「不想一直按住可用 Ctrl + Win + Shift」，未说明如何结束；这里假设再按一次同一组合结束，'
              '未经实测，建议优先用「按住说话」。Win 最后按下，微信一次看到三个键。',
        sources=('https://m.gmw.cn/2026-03/22/content_1304386825.htm',)),
    'windows': Profile(
        id='windows', name='Windows 语音输入', keys=('LWIN', 'H'), style='toggle', tap_ms=50, lead_ms=300,
        mic_hint='Windows 语音输入只用系统默认麦克风：按 Win + H 后点齿轮里的「默认麦克风」选择，或' + _SYSTEM_MIC
                 + '首次使用请在 设置 → 隐私和安全性 → 语音 中开启「联机语音识别」。',
        notes='Win + H 打开语音输入并开始聆听（边说边出字），再按一次 Win + H 停止。出现聆听提示后再开口。',
        sources=('https://support.microsoft.com/zh-cn/Accessibility/windows/use-voice-typing-to-talk-instead-of-type-on-your-pc',
                 'https://education.nsw.gov.au/teaching-and-learning/technology-for-learning/resources/'
                 'professional-learning-resources/inclusive-and-assistive-technology/microsoft/speech-recognition-windows11')),
}
CUSTOM_MIC_HINT = '在目标软件的设置里把麦克风选为「{mic}」（翻页笔接收器）；软件没有麦克风选项时，' + _SYSTEM_MIC


def custom_profile(keys, style='toggle', name='自定义快捷键', **changes):
    """A profile for any app from a user-recorded combo ('Ctrl+Alt+Space'); validated."""
    changes.setdefault('mic_hint', CUSTOM_MIC_HINT)
    return Profile(id=CUSTOM, name=name, keys=keys, style=style, custom=True, **changes).validate()


def get_profile(profile_id, keys=None, **changes):
    """A built-in profile, optionally with the user's own keys (the hotkey was changed in
    the app) or timings; 'custom' builds one from `keys` alone. ValueError if unusable."""
    if profile_id == CUSTOM:
        if keys is None:
            raise ValueError('请先录入目标软件的快捷键')
        return custom_profile(keys, **changes)
    base = PROFILES.get(profile_id)
    if base is None:
        raise ValueError(f'未知的听写软件：{profile_id}')
    if keys is not None:
        changes.update(keys=keys, custom=True)
    return replace(base, **changes).validate() if changes else base


# -- SendInput ---------------------------------------------------------------------
class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', ctypes.c_uint16), ('wScan', ctypes.c_uint16), ('dwFlags', ctypes.c_uint32),
                ('time', ctypes.c_uint32), ('dwExtraInfo', ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', ctypes.c_int32), ('dy', ctypes.c_int32), ('mouseData', ctypes.c_uint32),
                ('dwFlags', ctypes.c_uint32), ('time', ctypes.c_uint32), ('dwExtraInfo', ctypes.c_size_t)]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', ctypes.c_uint32), ('wParamL', ctypes.c_uint16), ('wParamH', ctypes.c_uint16)]


class _INPUTUNION(ctypes.Union):
    # MOUSEINPUT is the largest member: without it INPUT is too small and SendInput fails.
    _fields_ = [('mi', _MOUSEINPUT), ('ki', _KEYBDINPUT), ('hi', _HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [('type', ctypes.c_uint32), ('u', _INPUTUNION)]


_send_input = None


def _api():
    global _send_input
    if _send_input is None:
        user32 = ctypes.WinDLL('user32', use_last_error=True)  # own instance: argtypes stay local
        user32.SendInput.argtypes = (ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int)
        user32.SendInput.restype = ctypes.c_uint
        _send_input = user32.SendInput
    return _send_input


def send_keys_scan(events, send_input=None):
    """Inject [(vk, scan, flags)] as one SendInput call, each INPUT carrying both the
    virtual key and the scan code (flags: KEYEVENTF_EXTENDEDKEY / KEYEVENTF_KEYUP). OSError
    unless Windows took every event (it refuses while an elevated window is in front)."""
    events = [tuple(event) for event in events]
    if not events:
        return 0
    array = (INPUT * len(events))()
    allowed = KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP | KEYEVENTF_SCANCODE
    for item, (vk, scan, flags) in zip(array, events):
        if not (0 < vk < 0xFF and 0 <= scan <= 0xFF and not flags & ~allowed):
            raise ValueError(f'无效的按键事件：{(vk, scan, flags)}')
        item.type = INPUT_KEYBOARD
        item.u.ki.wVk, item.u.ki.wScan, item.u.ki.dwFlags = vk, scan, flags
    sent = (send_input or _api())(len(events), array, ctypes.sizeof(INPUT))
    if sent != len(events):
        code = ctypes.get_last_error() if send_input is None else 0
        raise OSError(f'SendInput 只送出 {sent}/{len(events)} 个按键（Windows 错误 {code}；'
                      '前台可能是以管理员身份运行的窗口）')
    return sent


# -- backend -----------------------------------------------------------------------
class HotkeyError(RuntimeError):
    """A shortcut could not be typed; the message is meant for the user."""


class KeysStuck(HotkeyError):
    """Windows refused a key-up; the key stays pending and is retried."""


class ExternalAppOnly(HotkeyError):
    """The app types into the focused field itself: DotVoice cannot keep the text as a note."""


RETRY_EVERY = 0.5  # seconds between attempts to release a key Windows refused to release
RETRY_FOR = 120.0  # how long the background retry keeps trying (the next connect/start/close retry too)
_LIVE = weakref.WeakSet()  # backends that may hold keys, for the exit handler


def _spawn_daemon(target):
    threading.Thread(target=target, name='hotkey-release', daemon=True).start()


class HotkeyBackend(VoiceBackend):
    """Drives a dictation app by its hotkey; see the module docstring. Thread-safe."""

    external = True  # the app inserts the text itself: results carry no words

    def __init__(self, profile, sender=send_keys_scan, sleep=time.sleep, clock=time.monotonic,
                 spawn=_spawn_daemon):
        self.profile = get_profile(profile) if isinstance(profile, str) else profile
        self.sender, self.sleep, self.clock, self.spawn = sender, sleep, clock, spawn
        self.insert_text = True
        self.lock = threading.RLock()
        # 'active': the app is recording; 'done': the result is not polled yet; 'unknown': a
        # toggle app's stop shortcut was refused while cancelling, so it most likely still records.
        self.phase = 'idle'
        self.held = []       # keys we pressed whose key-up has not gone through, in press order
        self.done_at = None
        self.next_retry = 0.0
        self.retrying = False

    @property
    def name(self):
        return self.profile.name

    def describe(self, mic=DEFAULT_MIC):
        return self.profile.describe(mic)

    # -- VoiceBackend ----------------------------------------------------------------
    def connect(self):
        with self.lock:
            self.profile.validate()
            if self.sender is send_keys_scan and sys.platform != 'win32':
                raise HotkeyError('热键桥只能在 Windows 上使用')
            self._flush()

    def start(self):
        with self.lock:
            self._flush()
            if not self.insert_text:
                raise ExternalAppOnly(f'「{self.name}」会把文字直接输入到光标处，不能保存为点声笔记；请切换到输入模式')
            if self.phase == 'active':
                raise HotkeyError(f'「{self.name}」的上一段尚未结束')
            if self.phase == 'unknown':
                # The last cancel could not stop the app: stop (and discard) that recording
                # first, or this press would toggle it off instead of starting a new one.
                try:
                    self.cancel()
                except HotkeyError as error:
                    if self.phase == 'unknown':
                        raise HotkeyError(f'「{self.name}」可能还在录上一段：它的结束快捷键被 Windows 拒绝了'
                                          '（前台可能是以管理员身份运行的窗口）；请切到普通窗口再按，'
                                          '或在该软件里手动结束') from error
                    raise
            self.phase, self.done_at = 'idle', None
            if self.profile.style == 'toggle':
                try:
                    self._tap(self.profile.keys, '开始')
                except KeysStuck:
                    self.phase = 'active'  # the press went through: the app counts as started
                    raise
            else:
                self._press_or_undo(self.profile.keys, '开始')
            self.phase = 'active'

    def feed(self, pcm):
        """The app records from its own microphone; DotVoice's audio is not needed here."""

    def stop(self):
        with self.lock:
            if self.phase == 'active':
                interrupted = None
                try:
                    self._pause(self.profile.lead_ms)  # the last syllable is still on its way
                except BaseException as error:
                    interrupted = error
                try:
                    if self.profile.style == 'toggle':
                        self._tap(self.profile.keys, '结束')
                    else:
                        self._release(list(self.held))
                except HotkeyError as error:
                    # A refused press leaves a toggle app recording: phase stays 'active' so
                    # the controller's cancel() tries to stop it again. Otherwise the stop
                    # went out (a refused key-up is retried) and no result will follow.
                    if isinstance(error, KeysStuck) or self.profile.style == 'hold':
                        self.phase, self.done_at = 'idle', None
                    raise
                except BaseException:
                    self.phase, self.done_at = 'idle', None
                    raise
                if interrupted is not None:
                    self.phase, self.done_at = 'idle', None
                    raise interrupted
            # Even without a session the controller gets its one result and goes idle.
            self.phase, self.done_at = 'done', self.clock() + self.profile.done_ms / 1000

    def poll(self):
        with self.lock:
            if self._stuck() and self.clock() >= self.next_retry:
                self._release(list(self.held), quiet=True)
            if self.done_at is not None and self.clock() >= self.done_at:
                self.phase, self.done_at = 'idle', None
                return {'type': 'result', 'text': '', 'external': True, 'source': 'hotkey',
                        'app': self.profile.name, 'profile': self.profile.id}
            return None

    def cancel(self):
        with self.lock:
            active = self.phase in ('active', 'unknown')
            self.done_at = None
            if not active:
                self.phase = 'idle'
                self._release(list(self.held), quiet=True)  # only refused key-ups can be left
                return
            if self.profile.style == 'hold':
                self.phase = 'idle'
                self._release(list(self.held))  # hold-to-talk apps finish on release
            else:
                self._release(list(self.held), quiet=True)
                try:
                    self._tap(self.profile.keys, '结束')  # stop the recording ...
                except KeysStuck:
                    self.phase = 'idle'  # the press went in: stopped (its key-up is retried)
                    raise
                except BaseException:
                    # Windows refused the press: the app still records. Never "idle" - the next
                    # start would toggle it off. start() / close() / the next cancel retry.
                    self.phase = 'unknown'
                    raise
                self.phase = 'idle'
            if self.profile.cancel_keys:
                self._tap(self.profile.cancel_keys, '取消')  # ... then discard it (闪电说: Esc)

    def close(self):
        with self.lock:
            try:
                if self.phase in ('active', 'unknown'):
                    self.cancel()
            finally:
                if self.phase != 'unknown':
                    self.phase = 'idle'
                self.done_at = None
                self._release(list(self.held), quiet=True)

    # -- keys --------------------------------------------------------------------------
    def _stuck(self):
        """Keys are down that should not be: anything held outside an active hold session."""
        return bool(self.held) and not (self.phase == 'active' and self.profile.style == 'hold')

    def _flush(self):
        """Release keys a refused key-up left down; KeysStuck while Windows still refuses."""
        if self._stuck():
            self._release(list(self.held))

    def _pause(self, ms):
        if ms > 0:
            self.sleep(ms / 1000)

    def _press(self, names):
        for index, name in enumerate(names):
            if index:
                self._pause(self.profile.gap_ms)
            added = name not in self.held
            if added:
                self.held.append(name)  # recorded before the event goes out: no path forgets it
            _LIVE.add(self)
            try:
                self.sender([key_input(name)])
            except Exception:
                # The sender refused this one-event batch, so the key never went down. Anything
                # else (KeyboardInterrupt...) might come after the event went in: keep the key.
                if added:
                    self.held.remove(name)
                raise

    def _undo(self, names, error, what):
        self._release([name for name in self.held if name in names], quiet=True)
        if isinstance(error, OSError):
            raise HotkeyError(f'无法向「{self.name}」发送{what}快捷键 {format_combo(names)}：'
                              'Windows 拒绝了模拟按键（前台可能是以管理员身份运行的窗口）') from error
        raise error

    def _press_or_undo(self, names, what):
        try:
            self._press(names)
        except BaseException as error:
            self._undo(names, error, what)

    def _tap(self, names, what):
        """A short press of `names`. HotkeyError if Windows refused the press (nothing stays
        down); KeysStuck if it refused the release (the keys stay pending and are retried)."""
        try:
            self._press(names)
            self._pause(self.profile.tap_ms)
        except BaseException as error:
            self._undo(names, error, what)
        self._release([name for name in names if name in self.held])

    def _release(self, names, quiet=False):
        """Key-ups in reverse press order; every key gets its attempt. A refused key-up keeps
        its key in `held` and schedules a retry; KeysStuck unless `quiet`."""
        refused = interrupted = None
        for index, name in enumerate(reversed(names)):
            if index:
                try:
                    self._pause(self.profile.gap_ms)
                except BaseException as error:  # a failing sleep must not skip a key-up
                    interrupted = interrupted or error
            try:
                self.sender([key_input(name, up=True)])
            except Exception as error:
                refused = refused or error
                continue
            if name in self.held:
                self.held.remove(name)
        if not self.held:
            _LIVE.discard(self)
        if refused is not None:
            self.next_retry = self.clock() + RETRY_EVERY
            self._schedule_retry()
            if not quiet:
                raise KeysStuck(f'按键 {format_combo(self.held)} 没能松开：Windows 拒绝了模拟按键'
                                '（前台可能是以管理员身份运行的窗口）；切回普通窗口后点声会自动重试') from refused
        if interrupted is not None:
            raise interrupted

    def _schedule_retry(self):
        """Keep releasing in the background: the controller stops polling once it disconnects."""
        if self.retrying:
            return
        self.retrying = True
        deadline = time.monotonic() + RETRY_FOR

        def retry():
            try:
                while time.monotonic() < deadline:
                    time.sleep(RETRY_EVERY)
                    with self.lock:
                        if self._stuck():
                            self._release(list(self.held), quiet=True)
                        if not self._stuck():
                            return
            finally:
                self.retrying = False

        try:
            self.spawn(retry)
        except Exception:  # e.g. no new threads at interpreter shutdown
            self.retrying = False


def _release_at_exit():
    """Injected key-downs outlive the process: let nothing stay pressed after DotVoice exits."""
    for backend in list(_LIVE):
        locked = backend.lock.acquire(timeout=1)
        try:
            backend._release(list(backend.held), quiet=True)
        except Exception:
            pass
        finally:
            if locked:
                backend.lock.release()


atexit.register(_release_at_exit)


# -- detection (read-only, for a setup wizard) ---------------------------------------
# Seen on a real PC (2026-10-12): 闪电说 0.7.8 runs as shandianshuo.exe, uninstall key and
# DisplayName "闪电说", user-chosen folder; 微信 4.1.13 runs as Weixin.exe, key "Weixin",
# DisplayName "WeChat", next to an old 3.9 entry (key "WeChat", name 微信, no voice input).
# Inferred (marked "?"): Typeless is an electron-builder per-user NSIS (winget
# SimplyCA.Typeless, product code below); 豆包输入法's process name is unknown, so its
# uninstall entry and a name heuristic are used.
_WECHAT = {'processes': ('weixin.exe',), 'exact_names': ('微信', 'wechat', 'weixin'),
           'keys': ('weixin', 'wechat'), 'min_version': (4, 1, 7),
           'paths': (r'%ProgramFiles%\Tencent\Weixin\Weixin.exe', r'%ProgramFiles(x86)%\Tencent\Weixin\Weixin.exe')}
DETECT = {
    'shandianshuo': {'processes': ('shandianshuo.exe',), 'names': ('闪电说', 'shandianshuo'),  # exe name?
                     'paths': (r'%LOCALAPPDATA%\shandianshuo\shandianshuo.exe',
                               r'%LOCALAPPDATA%\Programs\shandianshuo\shandianshuo.exe',
                               r'%ProgramFiles%\shandianshuo\shandianshuo.exe')},
    'typeless': {'processes': ('typeless.exe',), 'names': ('typeless',),
                 'keys': ('801bda48-0940-5127-a918-a53339b92128',),
                 'paths': (r'%LOCALAPPDATA%\Programs\Typeless\Typeless.exe',)},  # path?
    'doubao_ime': {'names': ('豆包输入法',), 'process_words': ('doubao', ('ime', 'input', 'shurufa'))},
    'wechat': _WECHAT,
    'wechat_handsfree': _WECHAT,
    'windows': {'builtin': True},
}


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [('dwSize', ctypes.c_uint32), ('cntUsage', ctypes.c_uint32), ('th32ProcessID', ctypes.c_uint32),
                ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', ctypes.c_uint32),
                ('cntThreads', ctypes.c_uint32), ('th32ParentProcessID', ctypes.c_uint32),
                ('pcPriClassBase', ctypes.c_int32), ('dwFlags', ctypes.c_uint32),
                ('szExeFile', ctypes.c_wchar * 260)]


def running_processes():
    """Lower-case executable names of running processes (Toolhelp snapshot; empty on failure)."""
    if sys.platform != 'win32':
        return set()
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (ctypes.c_uint32, ctypes.c_uint32)
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    for name in ('Process32FirstW', 'Process32NextW'):
        getattr(kernel32, name).argtypes = (ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W))
        getattr(kernel32, name).restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return set()
    names = set()
    try:
        entry = _PROCESSENTRY32W(dwSize=ctypes.sizeof(_PROCESSENTRY32W))
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            names.add(entry.szExeFile.casefold())
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return names


def uninstall_entries():
    """[{'key', 'DisplayName', 'DisplayVersion', 'InstallLocation', 'DisplayIcon'}] from the
    per-user and machine (64- and 32-bit) Uninstall keys, read-only."""
    try:
        import winreg
    except ImportError:
        return []
    path = r'Software\Microsoft\Windows\CurrentVersion\Uninstall'
    views = ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY))
    entries = []
    for root, view in views:
        try:
            parent = winreg.OpenKey(root, path, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        with parent:
            index = 0
            while True:
                try:
                    sub = winreg.EnumKey(parent, index)
                except OSError:
                    break
                index += 1
                entry = {'key': sub}
                try:
                    with winreg.OpenKey(parent, sub, 0, winreg.KEY_READ | view) as item:
                        for field in ('DisplayName', 'DisplayVersion', 'InstallLocation', 'DisplayIcon'):
                            try:
                                value = winreg.QueryValueEx(item, field)[0]
                            except OSError:
                                continue
                            if isinstance(value, str):
                                entry[field] = value
                except OSError:
                    continue
                entries.append(entry)
    return entries


def _version(text):
    numbers = re.findall(r'\d+', text or '')
    return tuple(int(n) for n in numbers[:4])


def _expand(template, environ):
    missing = []
    expanded = re.sub(r'%([^%]+)%', lambda m: environ.get(m.group(1)) or missing.append(m.group(1)) or '',
                      template)
    return None if missing else expanded


def _matches(entry, spec):
    name = (entry.get('DisplayName') or '').strip().casefold()
    if entry.get('key', '').casefold() in spec.get('keys', ()):
        return True
    if name and name in spec.get('exact_names', ()):
        return True
    return bool(name) and any(part in name for part in spec.get('names', ()))


def _entry_path(entry):
    icon = (entry.get('DisplayIcon') or '').strip()  # '"C:\...\app.exe",0' or 'C:\...\app.exe'
    icon = icon[1:].split('"', 1)[0] if icon.startswith('"') else icon.split(',', 1)[0].strip()
    if icon.lower().endswith('.exe'):
        return icon
    return (entry.get('InstallLocation') or '').strip().strip('"') or None


def scan(processes=None, entries=None, exists=os.path.isfile, environ=None, windows_build=None):
    """Read-only look for each profile's app: {id: {'installed', 'running', 'version',
    'path', 'supported'}}; supported is False when the version is known to be too old."""
    processes = running_processes() if processes is None else {p.casefold() for p in processes}
    entries = uninstall_entries() if entries is None else entries
    environ = os.environ if environ is None else environ
    if windows_build is None:
        windows_build = sys.getwindowsversion().build if sys.platform == 'win32' else 0
    found = {}
    for profile_id, spec in DETECT.items():
        if spec.get('builtin'):  # voice typing (Win + H) ships with Windows 10 1709+ / 11
            present = windows_build >= 16299
            found[profile_id] = {'installed': present, 'running': False, 'version': None, 'path': None,
                                 'supported': present}
            continue
        running = any(name in processes for name in spec.get('processes', ()))
        words = spec.get('process_words')
        if words:
            running = running or any(words[0] in name and any(w in name for w in words[1]) for name in processes)
        # Several entries can match (微信 3.x "WeChat" next to 4.x "Weixin"): the newest counts.
        matches = [entry for entry in entries if _matches(entry, spec)]
        best = max(matches, key=lambda entry: _version(entry.get('DisplayVersion')), default=None)
        installed = best is not None
        version = best.get('DisplayVersion') if best else None
        path = _entry_path(best) if best else None
        for template in spec.get('paths', ()):
            candidate = _expand(template, environ)
            if candidate and exists(candidate):
                installed, path = True, path or candidate
        supported = None
        if spec.get('min_version') and version:
            supported = _version(version) >= spec['min_version']
        found[profile_id] = {'installed': installed, 'running': running, 'version': version, 'path': path,
                             'supported': supported}
    return found


def detect(**kwargs):
    """Profile ids whose app is installed or running (and not known to be too old)."""
    found = scan(**kwargs)
    return [profile_id for profile_id, info in ((pid, found.get(pid)) for pid in PROFILES)
            if info and (info['installed'] or info['running']) and info['supported'] is not False]
