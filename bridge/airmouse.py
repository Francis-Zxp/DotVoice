"""The pen as a mouse: clicks from its own codes, and the keyboard hook that goes with it.

Input and notes modes: a ⑤ tap (vendor 71) is a left click at the cursor, sent at once;
the second tap of a double-tap clicks again, so Windows sees a double-click. A ③ long
press (6B) grabs the left button until the next ③ press, and a ④ long press (6D)
right-clicks. Present mode keeps the pen's own keys (⑤ double-tap = next tool, core).
The page keys are never remapped, in any mode: no hidden window after ⑤, no chords
(the pen's vendor report carries one key code at a time). The one exception, kept on
purpose: while ⑤ is held the volume keys scroll (or zoom the magnifier). On the
BS-OH085 they send vendor codes (78 / 79, held until 00), handled by wheel_press /
wheel_release without the hook; a keyboard VK bound by 0.11 or earlier still goes
through the hook.

A low-level keyboard hook cannot tell which keyboard sent a key, so it touches only such
keyboard wheel keys while ⑤ is held, PageUp while a grab holds the button, and the
keystrokes the pen adds to a ③ long press (judged by Raw Input, see
ShortcutGuard.typed_by_pen); none of it while a slide show is in the foreground
(skip_slideshow). A swallowed key-down always swallows its key-up, so no key or button sticks.

The same hook also runs the shortcut guard (ShortcutGuard, prefs pen.guard_shortcuts):
the pen types PowerPoint slideshow shortcuts next to its own codes, and outside a
slideshow those are swallowed when Raw Input (or the pen's vendor code) shows they came
from the receiver.
"""
import ctypes as c
from ctypes import wintypes as w
import threading
import time

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP, WM_QUIT = 0x100, 0x101, 0x104, 0x105, 0x12
LLKHF_EXTENDED, LLKHF_INJECTED = 0x01, 0x10
BUTTONS = {'left': (0x2, 0x4), 'right': (0x8, 0x10)}  # MOUSEEVENTF_*DOWN, *UP
CLICKS = {'click': ('left', 1), 'double': ('left', 2), 'right_click': ('right', 1)}  # button, count
MOUSEEVENTF_WHEEL, WHEEL_DELTA, VK_CONTROL = 0x0800, 120, 0x11
PRESENTATION_PROCESSES = ('powerpnt.exe', 'wpp.exe')
# ⑤ held + a volume key (BS-OH085: vendor 78 / 79, kept in the report until the key is let
# go). One press scrolls WHEEL_STEP notches - a useful step, where one notch (3 lines) felt
# like nothing - and a key held past WHEEL_DELAY keeps scrolling a notch every WHEEL_REPEAT.
# The magnifier, when up, takes one zoom step per press (and per ZOOM_REPEAT held).
WHEEL_STEP = 3
WHEEL_DELAY = .5        # like a keyboard's repeat delay: a normal press (~0.3 s) never repeats
WHEEL_REPEAT = .1
ZOOM_REPEAT = .4
WHEEL_HOLD_MAX = 3.0    # a lost release report (00) stops the repeat after this long
# ⑤'s hold counts this long after its press (72) at most: reading a page with ⑤ held and
# scrolling now and then is normal; a lost 73 is not. The wheel keys themselves never renew
# it - with a lost 73 they would keep scrolling instead of changing the volume for good.
WHEEL_POINTER_MAX = 90.0

user32 = c.WinDLL('user32', use_last_error=True)
kernel32 = c.WinDLL('kernel32', use_last_error=True)
LRESULT = c.c_ssize_t
HOOKPROC = c.WINFUNCTYPE(LRESULT, c.c_int, w.WPARAM, w.LPARAM)


class KBDLLHOOKSTRUCT(c.Structure):
    _fields_ = [('vkCode', w.DWORD), ('scanCode', w.DWORD), ('flags', w.DWORD),
                ('time', w.DWORD), ('dwExtraInfo', c.c_size_t)]


class MOUSEINPUT(c.Structure):
    _fields_ = [('dx', w.LONG), ('dy', w.LONG), ('mouseData', w.DWORD), ('dwFlags', w.DWORD),
                ('time', w.DWORD), ('dwExtraInfo', c.c_size_t)]


class KEYBDINPUT(c.Structure):
    _fields_ = [('wVk', w.WORD), ('wScan', w.WORD), ('dwFlags', w.DWORD),
                ('time', w.DWORD), ('dwExtraInfo', c.c_size_t)]


class HARDWAREINPUT(c.Structure):
    _fields_ = [('uMsg', w.DWORD), ('wParamL', w.WORD), ('wParamH', w.WORD)]


class _InputUnion(c.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]


class INPUT(c.Structure):
    _fields_ = [('type', w.DWORD), ('u', _InputUnion)]


class MONITORINFO(c.Structure):
    _fields_ = [('cbSize', w.DWORD), ('rcMonitor', w.RECT), ('rcWork', w.RECT), ('dwFlags', w.DWORD)]


user32.SetWindowsHookExW.argtypes = [c.c_int, HOOKPROC, w.HINSTANCE, w.DWORD]
user32.SetWindowsHookExW.restype = c.c_void_p
user32.CallNextHookEx.argtypes = [c.c_void_p, c.c_int, w.WPARAM, w.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [c.c_void_p]
user32.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
user32.SendInput.argtypes = [w.UINT, c.POINTER(INPUT), c.c_int]
user32.GetForegroundWindow.restype = w.HWND
user32.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, c.c_int]
user32.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
user32.GetWindowRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
user32.IsZoomed.argtypes = [w.HWND]
user32.IsZoomed.restype = w.BOOL
user32.MonitorFromWindow.argtypes = [w.HWND, w.DWORD]
user32.MonitorFromWindow.restype = w.HANDLE
user32.GetMonitorInfoW.argtypes = [w.HANDLE, c.POINTER(MONITORINFO)]
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.GetModuleHandleW.restype = w.HMODULE
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.OpenProcess.restype = w.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
kernel32.CloseHandle.argtypes = [w.HANDLE]
user32.GetDoubleClickTime.argtypes = []
user32.GetDoubleClickTime.restype = w.UINT


def _process_name(pid):
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ''
    try:
        size = w.DWORD(520)
        buffer = c.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, c.byref(size)):
            return buffer.value.rsplit('\\', 1)[-1].lower()
        return ''
    finally:
        kernel32.CloseHandle(handle)


def foreground_process():
    """Executable name of the foreground window's process ('' when unknown)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ''
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, c.byref(pid))
    return _process_name(pid.value)


def is_presentation_foreground(processes=PRESENTATION_PROCESSES):
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    name = c.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, name, 64)
    if name.value == 'screenClass':  # PowerPoint slide show window
        return True
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, c.byref(pid))
    if _process_name(pid.value) not in processes:
        return False
    # A slideshow is a borderless full-screen window, never maximized. A maximized editing
    # window overhangs its screen by the invisible DWM frame, so on a monitor without a
    # taskbar (auto-hide, a second screen) it would otherwise pass the test below.
    if user32.IsZoomed(hwnd):
        return False
    rect, info = w.RECT(), MONITORINFO(cbSize=c.sizeof(MONITORINFO))
    user32.GetWindowRect(hwnd, c.byref(rect))
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(hwnd, 2), c.byref(info)):
        return False
    screen = info.rcMonitor
    return (rect.left <= screen.left and rect.top <= screen.top and
            rect.right >= screen.right and rect.bottom >= screen.bottom)


def send_action(action, down=None, ctrl=False):
    """Inject mouse input at the cursor: a button edge ('left' / 'right' + down), a whole
    click ('click', 'double', 'right_click') or a wheel notch. Ctrl wraps a click so it
    becomes a multi-select click (⑧ held)."""
    events = []

    def mouse(flags, data=0):
        event = INPUT(type=0)
        event.u.mi = MOUSEINPUT(0, 0, data & 0xFFFFFFFF, flags, 0, 0)
        events.append(event)

    def key(up):
        event = INPUT(type=1)
        event.u.ki = KEYBDINPUT(VK_CONTROL, 0, 0x2 if up else 0, 0, 0)
        events.append(event)

    if action in BUTTONS:
        if ctrl and down:
            key(False)
        mouse(BUTTONS[action][0 if down else 1])
        if ctrl and not down:
            key(True)
    elif action in CLICKS:
        button, count = CLICKS[action]
        if ctrl:
            key(False)
        for _ in range(count):  # one batch: nothing can slip in between the clicks
            mouse(BUTTONS[button][0])
            mouse(BUTTONS[button][1])
        if ctrl:
            key(True)
    elif action in ('wheel_up', 'wheel_down'):
        mouse(MOUSEEVENTF_WHEEL, WHEEL_DELTA if action == 'wheel_up' else -WHEEL_DELTA)
    else:
        return
    array = (INPUT * len(events))(*events)
    if user32.SendInput(len(events), array, c.sizeof(INPUT)) != len(events):
        raise OSError('SendInput 被系统拒绝')


def send_keys(keys):
    """Inject keystrokes, [(vk, scan, up, extended)], as one SendInput batch."""
    array = (INPUT * len(keys))()
    for item, (vk, scan, up, extended) in zip(array, keys):
        item.type = 1
        item.u.ki = KEYBDINPUT(vk, scan, (0x2 if up else 0) | (0x1 if extended else 0), 0, 0)
    if user32.SendInput(len(keys), array, c.sizeof(INPUT)) != len(keys):
        raise OSError('SendInput 被系统拒绝')


# BS-OH085 (key log 2026-10-04): a long press on ③ sends vendor 6B until release
# plus the keystrokes Shift+F5, a Win tap and a stray Enter-up. In input / notes modes
# that long press grabs (or puts down) the left button for box select / drag, so those
# keystrokes are swallowed for a moment instead of starting a slideshow. Its short press
# types PageUp (vendor mirror 6A): while a grab holds the button, that press puts it down.
VK_F5, VK_LWIN, VK_RWIN, VK_RETURN, VK_PRIOR = 0x74, 0x5B, 0x5C, 0x0D, 0x21
DRAG_KEYS = frozenset((VK_F5, VK_LWIN, VK_RWIN, VK_RETURN))
DRAG_DELAY = .12   # let the pen's Shift go up first: Shift+click would extend selections
MUTE = 1.0

# -- Shortcut guard (prefs pen.guard_shortcuts) -----------------------------------
# Next to its vendor codes the pen types PowerPoint/WPS slideshow shortcuts: ⑧ sends
# Ctrl+P (the slideshow pen; Print everywhere else), a ③ long press Shift+F5 plus a
# Win tap and a stray Enter-up. Outside a slideshow those keystrokes are swallowed,
# but only when they came from the receiver. The hook cannot tell keyboards apart and
# Raw Input, which can, reports a key only after the hook has let it through. The pen
# always presses the modifier first (Ctrl, then P; Shift, then F5), and the modifier
# is let through, so by the time the letter reaches the hook Raw Input has already
# said which keyboard holds Ctrl/Shift (key logs: the receiver's keyboard events and
# its vendor code land within one 15 ms tick). If that report is still in flight the
# hook waits for it briefly (GUARD_WAIT). RawMonitor also reports other keyboards'
# modifiers, so a real Ctrl+P never waits and always passes. Keys without a modifier
# (plain F5, a bare letter) are judged by recent pen activity. Page keys, Tab /
# Win+Alt+Tab (①) and the volume keys are never candidates.
#
# Bare letters (no modifier held) are the pen's single-key slideshow shortcuts, each typed
# next to one vendor code (LETTER_CODES): a ④ long press types B (black screen) with 6D,
# another key E (erase the ink) with 67. Outside a slideshow and outside present mode
# (`letters`) such a letter is swallowed when its own code came a moment ago - never on
# other pen activity, so a letter typed right after a ⑤ click always passes. The B comes
# a moment BEFORE its 6D (key log 2026-10-12: the same 15 ms tick or one later), so for
# LATE_CODE_KEYS the hook waits up to GUARD_WAIT for that code - only after QUIET seconds
# without any key, so typing is never slowed: a ④ long press is a second of holding a pen key.
MODIFIERS = {0x10: 'shift', 0xA0: 'shift', 0xA1: 'shift', 0x11: 'ctrl', 0xA2: 'ctrl', 0xA3: 'ctrl',
             0x12: 'alt', 0xA4: 'alt', 0xA5: 'alt', VK_LWIN: 'win', VK_RWIN: 'win'}
MASKABLE = ('shift', 'alt', 'win')  # pressed and released alone: IME 中/英, menu bar, Start menu
VK_MASK = 0xE8        # unassigned VK (AutoHotkey's menu-mask key): "the modifier was used"
VK_A, VK_Z, VK_B, VK_E = 0x41, 0x5A, 0x42, 0x45
GUARD_WAIT = .04      # longest wait for the receiver's report of the held modifier (or for 6D)
GUARD_EPS = .002
PEN_BURST = .3        # "the pen was just active" (a receiver key or vendor code)
LETTER_CODES = {0x6D: VK_B, 0x67: VK_E}  # vendor code -> the letter the pen types with it
LATE_CODE_KEYS = frozenset((VK_B,))
QUIET = .6            # no key for this long: a bare B may be the pen's, worth a short wait
# After a swallowed pen shortcut its trailing Win tap goes too (③: Shift+F5, then Win).
# Its stray Enter-up has no press, so it changes nothing and is let through.
GUARD_TAIL = 1.0
SLIDESHOW_PROCESSES = PRESENTATION_PROCESSES + ('wps.exe',)  # WPS 2019+ runs all documents in wps.exe


GENERIC_VKS = {'shift': (0x10,), 'ctrl': (0x11,), 'alt': (0x12,), 'win': (VK_LWIN, VK_RWIN)}
user32.GetAsyncKeyState.argtypes = [c.c_int]
user32.GetAsyncKeyState.restype = c.c_short


def modifier_down(name):
    """System key state; inside the hook it covers every key processed before this one."""
    return any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in GENERIC_VKS[name])


def is_slideshow_foreground():
    return is_presentation_foreground(SLIDESHOW_PROCESSES)


def is_presentation_app_foreground():
    """PowerPoint/WPS in front, editing or showing: Shift+F5 / F5 start its slideshow."""
    return foreground_process() in SLIDESHOW_PROCESSES


class ShortcutGuard:
    """Decides, inside the keyboard hook, whether a key-down is a pen shortcut to swallow.

    hook side (hook thread): modifier(), key(), take_mask()
    raw side (Raw Input thread): raw_key() for every receiver key and other keyboards'
    modifiers, pen_activity() for vendor codes.
    """

    def __init__(self, clock=time.perf_counter, slideshow=is_slideshow_foreground,
                 presentation_app=is_presentation_app_foreground, wait=GUARD_WAIT, pressed=modifier_down):
        self.clock, self.slideshow, self.presentation_app = clock, slideshow, presentation_app
        self.wait, self.pressed = wait, pressed
        self.enabled = False
        # False in present mode: the pen's bare letters (④ long press: B = black screen)
        # stay native in every window there. Slideshows keep them in any mode.
        self.letters = True
        # Whether a ④ long press is a right click right now (AirMouse sets it): its B is then
        # swallowed even with the guard off - a right click must never type a "b" first.
        self.right_click = lambda: False
        self.cond = threading.Condition(threading.Lock())
        self.held = {}          # modifier name -> {'at', 'source', 'passed', 'masked'} (hook view)
        self.raw = {}           # modifier name -> ('pen' | 'other', at) of the latest raw press
        self.swallowed = set()  # VKs whose down was swallowed: repeats and the up follow
        self.pen_at = float('-inf')
        self.letter_at = {}     # letter VK -> when its vendor code came (LETTER_CODES)
        self.typed_at = float('-inf')  # the hook's latest key-down (any keyboard)
        self.tail_until = float('-inf')
        self.count = 0          # swallowed shortcuts (diagnosis)

    def reset(self):
        with self.cond:
            self.held.clear()
            self.swallowed.clear()
            self.tail_until = float('-inf')

    # -- raw side ------------------------------------------------------------------
    def raw_key(self, vk, down, pen):
        name = MODIFIERS.get(vk)
        now = self.clock()
        with self.cond:
            if pen:
                self.pen_at = now
            if name and down:
                self.raw[name] = ('pen' if pen else 'other', now)
            self.cond.notify_all()

    def pen_activity(self, code=None):
        """A vendor code from the receiver (`code`: which one)."""
        with self.cond:
            self.pen_at = self.clock()
            letter = LETTER_CODES.get(code)
            if letter is not None:
                self.letter_at[letter] = self.pen_at
                self.cond.notify_all()  # a bare B may be waiting for exactly this code (6D)

    # -- hook side -----------------------------------------------------------------
    def modifier(self, name, vk, down):
        """A modifier the hook let through so far. True swallows (a pen shortcut's Win tap)."""
        now = self.clock()
        with self.cond:
            if vk in self.swallowed:
                if not down:
                    self.swallowed.discard(vk)
                return True
            if not down:
                self.held.pop(name, None)
                return False
            self.typed_at = now
            if name == 'win' and self.enabled and now < self.tail_until and not self.held:
                self.swallowed.add(vk)
                return True
            if name not in self.held:
                self.held[name] = {'at': now, 'source': None, 'passed': False, 'masked': False}
            return False

    def key(self, vk, down):
        """A non-modifier key the air-mouse remap left alone. True swallows it."""
        with self.cond:
            if vk in self.swallowed:
                if not down:
                    self.swallowed.discard(vk)
                return True
            if not down:
                return False
            now = self.clock()
            try:
                if (self._decide(vk, now) if self.enabled else
                        vk in LATE_CODE_KEYS and self._paired_letter(vk, now)):
                    self.swallowed.add(vk)
                    self.count += 1
                    return True
            finally:
                self.typed_at = now
            for record in self.held.values():
                record['passed'] = True  # the modifier did its job with a real key
            return False

    def typed_by_pen(self):
        """Hook side, for an F5 key-down the air mouse may swallow (③ long press: the pen
        types Shift+F5 next to vendor 6B): True when the receiver typed it. Judged like a
        guarded shortcut - the held Shift's Raw Input source, else pen activity a moment
        ago - whether or not the guard itself is switched on. A real keyboard's F5 or
        Shift+F5 is never taken for the pen's. May wait up to `wait` for the Shift report."""
        with self.cond:
            now = self.clock()
            held = self.held
            for name in list(held):
                if not self.pressed(name):
                    del held[name]  # its release never reached this hook (another hook ate it)
            shift = held.get('shift')
            if shift is not None:
                return self._source('shift', shift, now) == 'pen'
            return now - self.pen_at <= PEN_BURST

    def take_mask(self):
        """A key-down was swallowed while Shift/Alt/Win is held: released alone they would
        switch the IME, open the menu bar or the Start menu. True = inject the mask key once."""
        with self.cond:
            masked = False
            for name, record in self.held.items():
                if name in MASKABLE and not record['passed'] and not record['masked']:
                    record['masked'] = masked = True
            return masked

    def _decide(self, vk, now):
        if not (VK_A <= vk <= VK_Z or vk == VK_F5):
            return False  # the fast path for every other key (Enter, Esc, page keys...)
        held = self.held
        for name in list(held):
            if not self.pressed(name):
                del held[name]  # its release never reached this hook (another hook ate it)
        ctrl, shift, alt, win = (held.get(name) for name in ('ctrl', 'shift', 'alt', 'win'))
        if VK_A <= vk <= VK_Z and ctrl and not alt and not win:
            family, name, record = 'ctrl', 'ctrl', ctrl     # Ctrl+P (⑧) and its kin
        elif vk == VK_F5 and not ctrl and not alt and not win:
            family, name, record = 'f5', 'shift', shift      # Shift+F5 (③ long press), F5
        elif not held:
            return self._bare(vk, now)                       # B (④ long press), E (67)
        else:
            return False
        if record is not None:
            source = self._source(name, record, now)
        else:
            source = 'pen' if now - self.pen_at <= PEN_BURST else 'other'
        if source != 'pen':
            return False
        # Inside a slideshow the shortcut does what the pen's maker meant; PowerPoint's
        # edit view keeps F5 / Shift+F5 (start the show) but not Ctrl+P (print).
        if family == 'ctrl' and self.slideshow():
            return False
        if family == 'f5' and self.presentation_app():
            return False
        self.tail_until = now + GUARD_TAIL
        return True

    def _paired_letter(self, vk, now):
        """Guard off (pen.guard_shortcuts): still the B a ④ long press types a moment before
        its 6D, when that long press is the air mouse's right click."""
        try:
            if not self.right_click():
                return False
        except Exception:
            return False
        held = self.held
        for name in list(held):
            if not self.pressed(name):
                del held[name]
        return not held and self._bare(vk, now)

    def _bare(self, vk, now):
        """A letter with no modifier held (cheap for typing: no foreground check unless its
        vendor code is involved). True: the pen typed it, outside a slideshow and outside
        present mode."""
        if not self.letters:
            return False
        if now - self.letter_at.get(vk, float('-inf')) > PEN_BURST:
            if vk not in LATE_CODE_KEYS or now - self.typed_at < QUIET or not self._await_letter(vk, now):
                return False
        # Inside a slideshow the letter does what the pen's maker meant (B: black screen).
        return not self.slideshow()

    def _await_letter(self, vk, now):
        """Wait up to `wait` for the letter's vendor code at or after the key (④: 6D after B)."""
        deadline = now + self.wait
        while self.letter_at.get(vk, float('-inf')) < now - GUARD_EPS:
            left = deadline - self.clock()
            if left <= 0:
                return False
            self.cond.wait(left)
        return True

    def _source(self, name, record, now):
        """Which keyboard holds this modifier press: 'pen' or 'other'."""
        deadline = now + self.wait
        while True:
            raw = self.raw.get(name)
            if raw is not None and raw[1] >= record['at'] - GUARD_EPS:
                record['source'] = raw[0]
                return raw[0]
            if record['source']:
                return record['source']
            left = deadline - self.clock()
            if left <= 0:
                break
            self.cond.wait(left)
        # No report for this press (Raw Input stopped, or a keyboard it filters out).
        record['source'] = 'pen' if self.clock() - self.pen_at <= PEN_BURST else 'other'
        return record['source']


def _schedule(delay, work):
    """Run `work` once after `delay` seconds on a daemon timer; returns it (cancel())."""
    timer = threading.Timer(delay, work)
    timer.daemon = True
    timer.start()
    return timer


class AirMouse:
    # `presenting` covers WPS 2019+ (wps.exe) like the shortcut guard does: with only
    # powerpnt.exe/wpp.exe, the pen clicked inside a WPS slideshow.
    def __init__(self, settings, vks, clock=time.monotonic, sender=send_action,
                 presenting=is_slideshow_foreground, guard=None, injector=send_keys,
                 double_click_ms=None):
        self.settings = settings  # callable -> {'enabled', 'skip_slideshow'}
        self.vks = vks            # callable -> {'wheel_up': set, 'wheel_down': set}: volume key VKs
        self.clock, self.sender, self.presenting = clock, sender, presenting
        self.guard = guard or ShortcutGuard()
        self.guard.right_click = self._right_clicks
        # Whether ④'s long press (6D) is bound to the right click (bridge.keymap; the core sets it).
        self.right_click_bound = lambda: True
        self.injector = injector
        self.double_click_ms = double_click_ms or user32.GetDoubleClickTime  # the system setting
        # The hook also runs for the shortcut guard alone; the pen's mouse needs this as well
        # (air mouse switched on, a Baseus receiver present and selected).
        self.remap_enabled = True
        # Input / notes modes: ⑤ taps click, ③ grabs, ④ right-clicks. Present mode keeps
        # the pen's own keys; there only the volume keys still scroll while ⑤ is held.
        self.buttons = True
        self.lock = threading.RLock()  # held / locked / muted: hook, ticker, HID and timer threads
        self.life = threading.Lock()   # start / stop
        self.down_passed = set()       # ③ keys whose press went through: so does their release
        self.spawn = lambda work: threading.Thread(target=work, name='key-mask', daemon=True).start()
        self.last_activity = float('-inf')
        self.holding = False
        self.ctrl = False
        self.ctrl_at = float('-inf')
        self.held = {}            # vk -> (role, pressed_at)
        self.locked = False       # left button held by a ③ grab
        self.grab_ctrl = False    # the grab's button went down as a Ctrl+press: so goes its release
        self.on_lock = None       # callback(bool, how) so the capsule can show the held button
        self.on_wheel = None      # callback(role) -> True when it used the scroll (magnifier zoom)
        # callback(kind) right before a click / scroll / grab is sent (bridge.elevation: an
        # administrator window drops it; the core says so once per window). Never blocks.
        self.before_input = None
        self.drag_timer = None    # pending grab after a ③ long press
        self.schedule = _schedule  # (delay, work) -> timer: the held wheel key's repeat (tests replace it)
        self.wheel = None         # the wheel key held now: {'role', 'how', 'since'}
        self.wheel_timer = None
        self.mute_until = float('-inf')
        self.muted = set()
        self.last_click = float('-inf')  # a first tap's click: the second tap makes it a double-click
        self.thread = None
        self.thread_id = None
        self.ready = threading.Event()
        self.error = None
        self.mapping = {}
        self.watch = set()
        self.skip_slideshow = True
        self.refresh()

    def refresh(self):
        settings = self.settings()
        self.skip_slideshow = settings.get('skip_slideshow', True)
        mapping = {name: set(keys) for name, keys in self.vks().items() if keys} if self.remap_enabled else {}
        self.mapping = mapping
        self.watch = set().union(*mapping.values()) if mapping else set()

    def activity(self):
        self.last_activity = self.clock()

    def press(self):
        """Air-mouse key ⑤ held (vendor code): the volume keys scroll for as long as it is down."""
        self.holding = True
        self.last_activity = self.clock()

    def release(self):
        self.holding = False
        self.last_activity = self.clock()
        self.wheel_release()

    def forget(self):
        """The receiver went away: no hold, no recent activity, no Ctrl (a real keyboard's
        volume keys must not keep scrolling after the pen was unplugged mid-hold)."""
        self.holding = False
        self.ctrl = False
        self.last_activity = float('-inf')
        self.wheel_release()

    def pointing(self, now=None, limit=30):
        """⑤ is held. A lost release report counts for `limit` seconds at most."""
        now = self.clock() if now is None else now
        return self.holding and now - self.last_activity < limit

    def _presenting(self):
        return bool(self.skip_slideshow and self.presenting())

    def _clicks(self):
        """The pen's keys act as mouse buttons: air mouse on, input / notes mode, no slideshow."""
        return bool(self.remap_enabled and self.buttons and not self._presenting())

    def _right_clicks(self):
        """A ④ long press right-clicks now (the shortcut guard asks, from the hook)."""
        return bool(self.remap_enabled and self.buttons and self.right_click_bound() and not self._presenting())

    def tapped(self):
        """⑤ was tapped (71): a tap is a press and a release, so ⑤ is not held - whatever a
        lost release report (73) left behind. The volume keys stop scrolling."""
        with self.lock:
            self.holding = False
            self._stop_wheel()

    def set_buttons(self, on):
        """Mode change. Present mode keeps the pen's own keys, so a grab is put down."""
        self.buttons = bool(on)
        if not self.buttons:
            self._release_grab()

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def _send(self, action, down=None):
        self._before(action)
        self.sender(action, down, self.ctrl)

    def _before(self, kind):
        check = self.before_input
        if check is not None:
            try:
                check(kind)
            except Exception:
                pass  # a broken check never stops a click

    def _double_click_ms(self):
        try:
            return int(self.double_click_ms()) or 500
        except (OSError, TypeError, ValueError):
            return 500

    # -- clicks from the pen's own codes (HID thread) ---------------------------------
    def tap(self, second=False):
        """⑤ tap: a left click at the cursor, sent at once (never held back to wait for a
        possible second tap). `second` = the next tap within the double-tap window: one
        more click, so Windows sees a double-click; if the first click is already older
        than the system's double-click time, two quick clicks make one. While a grab
        holds the button a tap does nothing (the next ③ press puts it down)."""
        self.tapped()
        if not self._clicks():
            return False
        with self.lock:
            if self.locked or self.drag_timer:
                return False
            now = self.clock()
            action = 'click'
            if second and (now - self.last_click) * 1000 > self._double_click_ms():
                action = 'double'
            try:
                self._send(action)
            except OSError:
                return False
            self.last_click = float('-inf') if second else now
            self.last_activity = now
            return True

    def right_click(self):
        """④ long press (its learned vendor code): a right click at the cursor."""
        if not self._clicks():
            return False
        with self.lock:
            if self.locked or self.drag_timer:
                return False
            try:
                self._send('right_click')
            except OSError:
                return False
            self.last_activity = self.clock()
            return True

    def wheel_press(self, role):
        """A wheel key went down (its vendor code; BS-OH085 volume + / −: 78 / 79). With ⑤
        held the magnifier, when up, zooms one step, else WHEEL_STEP notches scroll at the
        cursor; a key still down after WHEEL_DELAY keeps going until wheel_release (its 00,
        any other code, ⑤ let go). In every mode, but not in a slideshow (skip_slideshow) -
        the magnifier excepted. Without ⑤ nothing happens: the key keeps its own function.
        True when it scrolled or zoomed."""
        with self.lock:
            self._stop_wheel()
            how = self._wheel_step(role, WHEEL_STEP)
            if how is None:
                return False
            # Never renews ⑤'s hold (last_activity): with its release report lost, the volume
            # keys would otherwise scroll for as long as they are used.
            wheel = self.wheel = {'role': role, 'how': how, 'since': self.clock()}
            self.wheel_timer = self.schedule(WHEEL_DELAY, lambda: self._wheel_repeat(wheel))
            return True

    def wheel_release(self):
        with self.lock:
            self._stop_wheel()

    def _stop_wheel(self):
        timer, self.wheel_timer, self.wheel = self.wheel_timer, None, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass

    def _wheel_repeat(self, wheel):
        with self.lock:
            if self.wheel is not wheel:
                return  # let go, or a newer press took over
            if self.clock() - wheel['since'] > WHEEL_HOLD_MAX:
                self.wheel = self.wheel_timer = None
                return
            how = self._wheel_step(wheel['role'], 1)
            if how is None:
                self.wheel = self.wheel_timer = None
                return
            self.wheel_timer = self.schedule(ZOOM_REPEAT if how == 'zoom' else WHEEL_REPEAT,
                                             lambda: self._wheel_repeat(wheel))

    def _wheel_step(self, role, notches):
        """'zoom' (the magnifier took it), 'scroll' (notches sent) or None (nothing to do)."""
        if self._zoom(role):
            return 'zoom'
        if not (self.remap_enabled and self.pointing(limit=WHEEL_POINTER_MAX) and not self._presenting()):
            return None
        try:
            for _ in range(notches):
                self._send(role)
        except OSError:
            return None
        return 'scroll'

    def _zoom(self, role):
        hook = self.on_wheel
        try:
            return bool(hook is not None and hook(role))
        except Exception:
            return False

    def begin_drag(self):
        """③ long press (input / notes modes) toggles a grab: the left button goes down
        and stays down while ⑤ moves the pointer (in any order of keys); the next ③
        press, short or long, puts it down. No recent ⑤ use is needed. Holding two keys
        at once proved unreliable: pressing ⑤ makes the pen re-report codes mid-hold."""
        if not self.running or not self.remap_enabled:
            return False
        with self.lock:
            self.mute_until = self.clock() + MUTE  # the pen's Shift+F5 / Win / Enter-up follow
            if self.locked:
                self._release_grab()  # putting it down is always allowed
                return True
            if not self.buttons or self._presenting():
                self.mute_until = float('-inf')
                return False
            if self.drag_timer:
                self.drag_timer.cancel()  # a re-reported 6B must not queue a second grab
            self.drag_timer = threading.Timer(DRAG_DELAY, self._grab)
            self.drag_timer.daemon = True
            self.drag_timer.start()
            return True

    def _grab(self):
        with self.lock:
            self.drag_timer = None
            if self.locked or not self.remap_enabled or not self.buttons:
                return
            ctrl = self.ctrl
            self._before('grab')
            try:
                self.sender('left', True, ctrl)
            except OSError:
                return
            self.locked, self.grab_ctrl = True, ctrl
            self.last_activity = self.clock()  # the idle release counts from here
        self._notify_lock(True, 'grab')

    def _release_grab(self):
        with self.lock:
            if self.drag_timer:
                self.drag_timer.cancel()
                self.drag_timer = None
            if not self.locked:
                return
            try:
                self.sender('left', False, self.grab_ctrl)
            except OSError:
                pass
            self.locked = False
        self._notify_lock(False, 'grab')

    def end_drag(self):
        """The ③ release report: nothing to do, a grab ends on the next ③ press."""
        return False

    def _mute(self, vk, down, injected, now, pen=False):
        """Swallow the keystrokes the pen adds to a ③ long press (Shift+F5, a Win tap, a
        stray Enter-up) when that press grabs or puts down the button. The pen's F5 opens
        the mute window (`pen`: Raw Input says the receiver typed it, so a real keyboard's
        F5 / Shift+F5 keeps working); a ③ long press seen first (begin_drag) opens it too.
        A release always follows its press: a Win/Enter pressed just before the mute
        window and released inside it goes through, so it can never stick."""
        if injected:
            return False
        if vk in self.muted:
            if not down:
                self.muted.discard(vk)
            return True
        if vk in self.down_passed:
            if not down:
                self.down_passed.discard(vk)
            return False
        if not down:
            # The pen's stray Enter-up (no press at all) inside the window.
            return self.remap_enabled and now < self.mute_until
        swallow = False
        if self.remap_enabled:
            if vk == VK_F5:
                swallow = ((pen or now < self.mute_until)
                           and (self.locked or (self.buttons and not self._presenting())))
                if swallow:
                    self.mute_until = now + MUTE
            else:
                swallow = now < self.mute_until
        (self.muted if swallow else self.down_passed).add(vk)
        return swallow

    def set_ctrl(self, down):
        self.ctrl = bool(down)
        self.ctrl_at = self.clock()

    def expire_ctrl(self, limit):
        """⑧'s release report lost: Ctrl must not stay on every click for good."""
        if self.ctrl and self.clock() - self.ctrl_at > limit:
            self.ctrl = False

    # -- keyboard hook ------------------------------------------------------------------
    def handle(self, vk, message, injected, scan=0, flags=0):
        """Hook body, separated for tests. Returns True to swallow the key."""
        down = message in (WM_KEYDOWN, WM_SYSKEYDOWN)
        if not down and message not in (WM_KEYUP, WM_SYSKEYUP):
            return False
        swallow = False
        if vk in self.watch or vk in DRAG_KEYS or vk in self.held or (vk == VK_PRIOR and self.locked):
            pen = False
            if (vk == VK_F5 and down and not injected and self.remap_enabled and (self.buttons or self.locked)
                    and vk not in self.muted and vk not in self.down_passed):
                # Outside the lock: this may wait a moment for Raw Input's report of the Shift.
                pen = self.guard.typed_by_pen()
            with self.lock:
                swallow = self._remap(vk, down, injected, self.clock(), pen)
        if injected:
            return swallow
        guard = self.guard
        modifier = MODIFIERS.get(vk)
        if modifier is not None:
            return swallow or guard.modifier(modifier, vk, down)
        if not swallow:
            swallow = guard.key(vk, down)
        if swallow and down and guard.take_mask():
            self._mask()
        return swallow

    def _mask(self):
        """Tap the unassigned mask key while Shift/Alt/Win is still held, so releasing
        it alone after a swallowed key does not switch the IME or open a menu. The
        release itself is never held back: a modifier can never stick."""
        def work():
            try:
                self.injector([(VK_MASK, 0, False, False), (VK_MASK, 0, True, False)])
            except OSError:
                pass
        self.spawn(work)

    def _remap(self, vk, down, injected, now, pen=False):
        if vk in DRAG_KEYS and vk not in self.watch and vk not in self.held:
            return self._mute(vk, down, injected, now, pen)
        if vk in self.held:
            role, _ = self.held[vk]
            if down:
                if role in ('wheel_up', 'wheel_down'):
                    self._wheel(role)  # auto-repeat keeps scrolling
                return True
            del self.held[vk]  # (a key rebound while pressed still gets its release swallowed)
            return True
        if not down or injected:
            return False
        if vk == VK_PRIOR and self.locked:
            # ③'s short press types PageUp: it puts the grabbed button down instead of paging.
            self._release_grab()
            self.held[vk] = ('swallow', now)
            return True
        role = next((name for name in ('wheel_up', 'wheel_down') if vk in self.mapping.get(name, ())), None)
        if role is None:
            return False
        if self.on_wheel is not None:
            # The magnifier owns the volume keys even in a slideshow (where they otherwise
            # stay volume keys): ask it before the slideshow exemption.
            try:
                taken = self.on_wheel(role)
            except Exception:
                taken = False
            if taken:
                self.held[vk] = (role, now)
                return True
        # Only while ⑤ is held: the volume keys never scroll in a window after it.
        if not self.pointing(now) or self._presenting():
            return False
        try:
            self._wheel(role)
        except OSError:
            return False
        self.held[vk] = (role, now)
        return True

    def _wheel(self, role):
        """Scroll, unless the magnifier is up: then the same keys change its zoom."""
        hook = self.on_wheel
        try:
            if hook is not None and hook(role):
                return
        except Exception:
            pass
        self._send(role)

    def _notify_lock(self, locked, how):
        callback = self.on_lock
        if callback:
            try:
                callback(locked, how)
            except Exception:
                pass

    def expire(self, idle=20):
        """A forgotten grab is put down after `idle` seconds without air-mouse use. ⑤ held
        counts as use, but a lost release report (73) only for WHEEL_POINTER_MAX: in input /
        notes modes nothing else clears `holding`, so the button would stay down for good."""
        if self.locked and not self.pointing(limit=WHEEL_POINTER_MAX) and self.clock() - self.last_activity > idle:
            self.release_all()

    def start(self):
        with self.life:
            if self.running:
                return
            self.ready.clear()
            self.error = None
            with self.lock:
                self.muted.clear()
                self.down_passed.clear()
            self.guard.reset()  # nothing a previous hook saw is still pressed for this one
            self.thread = threading.Thread(target=self._run, name='airmouse-hook', daemon=True)
            self.thread.start()
            if not self.ready.wait(3):
                raise RuntimeError('飞鼠点击钩子启动超时')
            if self.error:
                raise RuntimeError(self.error)

    def _run(self):
        self.thread_id = kernel32.GetCurrentThreadId()

        @HOOKPROC
        def proc(code, message, data):
            try:
                # Every key passes through handle(): the guard follows the modifiers.
                if code == 0:
                    info = KBDLLHOOKSTRUCT.from_address(data)
                    if self.handle(info.vkCode, message, bool(info.flags & LLKHF_INJECTED),
                                   info.scanCode, info.flags):
                        return 1
            except Exception:
                pass  # Never let a Python error swallow the user's keystroke.
            return user32.CallNextHookEx(None, code, message, data)

        self._proc = proc
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, kernel32.GetModuleHandleW(None), 0)
        if not hook:
            self.error = '无法安装飞鼠点击钩子（Windows 错误 %d）' % c.get_last_error()
            self.ready.set()
            return
        self.ready.set()
        try:
            message = w.MSG()
            while user32.GetMessageW(c.byref(message), None, 0, 0) > 0:
                pass
        finally:
            user32.UnhookWindowsHookEx(hook)
            self.release_all()

    def release_all(self):
        """Nothing the pen holds outlives the hook, the receiver or the switch: put a grab
        down and forget held volume keys."""
        with self.lock:
            self.held.clear()
            self._stop_wheel()
            if self.drag_timer:
                self.drag_timer.cancel()
                self.drag_timer = None
            locked, self.locked = self.locked, False
            if locked:
                try:
                    self.sender('left', False, self.grab_ctrl)
                except OSError:
                    pass
        if locked:
            self._notify_lock(False, 'grab')

    def stop(self):
        with self.life:
            if self.running and self.thread_id:
                user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
                self.thread.join(2)
            self.thread = None
        self.release_all()
