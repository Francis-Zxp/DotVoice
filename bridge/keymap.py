"""Per-device key bindings and guided learning.

Vendor/PV60 codes have no native Windows effect, so firing an action never
needs input suppression. Keyboard VKs are learned only for the wheel slots (a
receiver whose volume keys type VK_VOLUME_UP / DOWN; the BS-OH085 sends vendor codes).
"""
import copy
import json
from pathlib import Path
import threading
import time

ACTION_SLOTS = ('toggle_mode', 'quick_note', 'arm_toggle', 'cancel')
# Vendor long-press codes that act as mouse buttons in input / notes modes (core).
# Learned with one long press; the short press of the same key keeps its own function.
LONG_SLOTS = ('right_click',)
# Keys that scroll while the air-mouse key ⑤ is held: vendor codes (the BS-OH085's
# volume keys) or, as bound by 0.11 and earlier, keyboard VKs (the keyboard hook scrolls
# with those). (0.10's page-key and Tab clicks are gone: ③④ always page, ⑤ itself
# clicks; old bindings of those slots are skipped when bindings.json is read.)
REMAP_SLOTS = ('airmouse_wheel_up', 'airmouse_wheel_down')
WHEEL_ROLES = {'airmouse_wheel_up': 'wheel_up', 'airmouse_wheel_down': 'wheel_down'}
# Vendor keys with press/release meaning (fire = press, ignore = release).
HOLD_SLOTS = ('airmouse_activity', 'airmouse_ctrl', 'annotate')
AIRMOUSE_SLOTS = REMAP_SLOTS + HOLD_SLOTS
SLOTS = ACTION_SLOTS + LONG_SLOTS + AIRMOUSE_SLOTS
DEVICES = ('baseus', 'pv60', 'tidepool')
# Record keys belong to the voice protocol and can never be rebound.
RESERVED = {'baseus': {'74', '75', '76', '77'}, 'pv60': {'17'}}
# ⑤'s quick tap (built into the core, not a slot). With ⑤'s hold codes it is never taken
# as a wheel key while one is recorded: "按住 ⑤ + 音量 +" may well be pressed just so.
AIR_TAP_CODES = {'baseus': {'71'}}
# Shift / Ctrl / Alt / Win: the pen types them in front of its letters; never a wheel key.
MODIFIER_VKS = frozenset((0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5))
DOUBLE_WINDOW = .6
LEARN_TIMEOUT = 20
# A hold key's neutral report this long after its press is its release (a key without a
# release code of its own). Sooner, it is the neutral report every press is followed by.
NEUTRAL_RELEASE = .4
# bindings.json format. 2 (0.11.1): the wheel slots default to vendor codes. A version-1
# file's cleared wheel slot cleared the old keyboard default, which never worked on the
# BS-OH085 (its volume keys type no VK at all), so the vendor default applies again.
# 3 (0.11.2): hold keys learned from an unrelated pair of codes are reset once (below).
VERSION = 3
RECOVERED = 'bindings-recovered.json'  # where changes go while bindings.json cannot be read
READ_RETRY = .25                       # another program (antivirus, backup) briefly holding it
HOLD_RESET_NOTICE ='按住类按键「{labels}」之前录错了（松开键码其实是另一个键的按下），已恢复默认；需要时请重新录制。'

# BS-OH085, measured 2026-10-04 (tools/probe_keys.py): key 7 sends 7a/7b for a
# short press and 7c/7d for a long press/release. A short press also latches
# the pen microphone on until the next short press. So the long press switches
# modes (the firmware closes the microphone on release), and the short press is
# a quick note: 7a (microphone on) starts a note recording, 7b (off) ends it.
# Key 5 (air mouse) sends 72 while held and 73 on release, 71 for a quick tap (core:
# a left click in input / notes modes). Page keys send the PageUp/PageDown VKs
# (vendor mirror 6a/6c) and always stay page keys; ③'s long press sends 6b (core: a
# grab). ④'s long press sends 6d plus a keyboard B (PowerPoint: black screen; key log
# 2026-10-12): right_click (core; the shortcut guard swallows the B outside slideshows).
# The volume keys ⑨⑩ send 78 / 79 for as long as they are held, then 00 - with or
# without ⑤ held, and no keyboard VK at all (key log 2026-10-12): with ⑤ held they scroll.
# Key 8 (annotate) sends 68 on press and 69 on release (measured 2026-10-04, and in
# every key log since). It is the "hold = Ctrl / 标注" key: Ctrl for multi-select
# clicks, the annotation pen in present mode (core._hold_key). It
# must be a default: 0.9 shipped it only in the measuring user's own bindings.json,
# so on any other PC ⑧ did nothing until it was recorded by hand.
DEFAULTS = {
    'baseus': {
        'toggle_mode': {'source': 'vendor', 'fire': ['7c'], 'ignore': ['7d'],
                        'gesture': 'single', 'label': '长按翻译键 ⑦'},
        'quick_note': {'source': 'vendor', 'fire': ['7a', '7b'], 'gesture': 'single',
                       'label': '短按翻译键 ⑦'},
        'right_click': {'source': 'vendor', 'fire': ['6d'], 'gesture': 'single', 'label': '长按下翻页 ④'},
        'airmouse_wheel_up': {'source': 'vendor', 'fire': ['78'], 'gesture': 'single', 'label': '音量 + ⑨'},
        'airmouse_wheel_down': {'source': 'vendor', 'fire': ['79'], 'gesture': 'single', 'label': '音量 − ⑩'},
        'airmouse_activity': {'source': 'vendor', 'fire': ['72'], 'ignore': ['73'],
                              'gesture': 'single', 'label': '飞鼠键 ⑤'},
        'airmouse_ctrl': {'source': 'vendor', 'fire': ['68'], 'ignore': ['69'],
                          'gesture': 'single', 'label': '标注键 ⑧'},
    },
    'pv60': {},
    'tidepool': {},
}


def slot_sources(device, slot):
    """Key sources a slot accepts. A Baseus wheel slot takes a vendor code or a keyboard VK."""
    if slot in REMAP_SLOTS:
        return ('vendor', 'keyboard') if device == 'baseus' else ('keyboard',)
    return ('pv60',) if device == 'pv60' else ('vendor',)


def _code(value):
    return format(value, '02x') if isinstance(value, int) else str(value).lower()


def _clean(device, slot, binding):
    if binding is None:
        return None
    if not isinstance(binding, dict):
        raise ValueError('绑定格式无效')
    source = binding.get('source')
    if source not in slot_sources(device, slot):
        raise ValueError('该位置不能使用这种按键来源')
    label = str(binding.get('label') or '')[:24]
    if source == 'keyboard':
        vks = binding.get('vk')
        if (not isinstance(vks, list) or not vks or
                not all(type(v) is int and 1 <= v <= 254 for v in vks)):
            raise ValueError('键盘按键无效')
        return {'source': source, 'vk': sorted(set(vks)), 'label': label}
    fire, ignore = binding.get('fire', []), binding.get('ignore', [])
    if not isinstance(fire, list) or not isinstance(ignore, list):
        raise ValueError('按键码无效')  # e.g. "fire": null in a hand-edited bindings.json
    fire = [_code(c) for c in fire]
    ignore = [_code(c) for c in ignore]
    if not fire or not all(len(c) == 2 and all(ch in '0123456789abcdef' for ch in c)
                           for c in fire + ignore):
        raise ValueError('按键码无效')
    if '00' in fire + ignore:
        raise ValueError('中性报告不能绑定')
    taken = set(fire + ignore) & RESERVED.get(device, set())
    if taken:
        raise ValueError('录音键属于语音协议，不能改作其它用途')
    gesture = binding.get('gesture', 'single')
    if gesture not in ('single', 'double'):
        raise ValueError('触发方式无效')
    return {'source': source, 'fire': sorted(set(fire)), 'ignore': sorted(set(ignore) - set(fire)),
            'gesture': gesture, 'label': label}


def _retired(slot, binding):
    """A version-1 entry that no longer means what it did: a cleared wheel slot cleared the
    old keyboard default (VK_VOLUME_UP / DOWN), not the volume keys' vendor codes."""
    return binding is None and slot in REMAP_SLOTS


def is_release(press, release):
    """The BS-OH085's press / release pairs are an even code and the next one up: 72/73,
    68/69, 7a/7b, 7c/7d, 74/75, 76/77."""
    try:
        press, release = int(press, 16), int(release, 16)
    except (TypeError, ValueError):
        return False
    return press % 2 == 0 and release == press + 1


def known_fire_codes(device):
    """Codes the shipped defaults (and the core's own ⑤ tap) fire on."""
    codes = set(AIR_TAP_CODES.get(device, ()))
    for binding in DEFAULTS.get(device, {}).values():
        codes.update(binding.get('fire', []))
    return codes


def _mislearned_hold(device, slot, binding):
    """0.11.1 and earlier took any two codes as a hold key's press and release: a stray code
    first, then the real press, saved e.g. ⑧ as fire 67 / ignore 68 - so 68 (⑧'s press)
    only ever released it. Such a release code is another key's press."""
    return (slot in HOLD_SLOTS and isinstance(binding, dict) and binding.get('source') == 'vendor'
            and bool(set(binding.get('ignore', [])) & known_fire_codes(device)))


def _read(path):
    """A bindings file's JSON (a UTF-8 BOM is fine). A file another program holds open
    (Windows sharing violation) is tried once more before giving up."""
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except OSError:
        time.sleep(READ_RETRY)
        return json.loads(path.read_text(encoding='utf-8-sig'))


def _saved_at(path, form='%Y-%m-%d %H:%M'):
    return time.strftime(form, time.localtime(path.stat().st_mtime))


def _load(saved):
    """A bindings file's contents -> (user bindings, labels of hold keys reset once).
    Raises AttributeError for a file of the wrong shape."""
    user, reset = {device: {} for device in DEVICES}, []
    version = saved.get('version', 1)
    version = version if isinstance(version, int) else 1
    for device, slots in saved.get('devices', {}).items():
        for slot, binding in slots.items():
            if device in DEVICES and slot in SLOTS:
                # Version 2 already had the vendor wheel defaults: its cleared wheel slot is the user's.
                if version < 2 and _retired(slot, binding):
                    continue
                try:
                    cleaned = _clean(device, slot, binding)
                except ValueError:
                    continue
                if version < 3 and _mislearned_hold(device, slot, cleaned):
                    reset.append(DEFAULTS.get(device, {}).get(slot, {}).get('label') or slot)
                    continue  # once, when the file is upgraded: back to the default
                user[device][slot] = cleaned
    return user, reset


class Keymap:
    def __init__(self, path, on_learn=lambda value: None, clock=time.monotonic):
        self.path = Path(path)
        self.on_learn = on_learn
        self.clock = clock
        self.lock = threading.RLock()
        self.user = {device: {} for device in DEVICES}
        self.warning = ''
        self.learning = None
        self.last_fire = {}
        if self.path.exists():
            reset, problem = [], None
            recovered = self.path.with_name(RECOVERED)
            try:
                self.user, reset = _load(_read(self.path))
            except OSError:
                problem = '被其它程序占用，暂时无法读取'
            except (ValueError, AttributeError):
                problem = '已损坏，无法读取'
            if problem:
                # bindings.json is kept as it is. Changes go to bindings-recovered.json, which is
                # read back while bindings.json stays unreadable - and the user is told every time.
                saved = None
                if recovered.exists():
                    try:
                        saved = _saved_at(recovered)
                        self.user = _load(_read(recovered))[0]
                    except (OSError, ValueError, AttributeError):
                        saved = None
                self.path = recovered
                using = (f'正在使用之前另存的绑定（bindings-recovered.json，{saved} 保存）' if saved
                         else '本次使用默认值')
                self.warning = (f'按键绑定文件 bindings.json {problem}：{using}；原文件保留，'
                                f'修改会保存到 bindings-recovered.json。')
            else:
                notices = []
                if recovered.exists():
                    self._take_recovered(recovered, notices)  # before the save below changes the file time
                if reset:
                    notices.append(HOLD_RESET_NOTICE.format(labels='、'.join(reset)))
                    try:
                        self._save(self.user)  # every other binding exactly as it was
                    except OSError:
                        pass
                self.warning = ' '.join(notices)

    def _take_recovered(self, recovered, notices):
        """bindings.json reads again, but an earlier session saved its changes to
        bindings-recovered.json (bindings.json was unreadable then: that session started from the
        defaults, so the copy holds just the slots changed then). Newer than bindings.json, those
        slots go on top of it, one key still one action; older, bindings.json was edited since and
        stays. The copy is then renamed, never deleted: it cannot come back later as "the saved
        bindings"."""
        reason = '早于 bindings.json 的修改'
        try:
            mtime = recovered.stat().st_mtime
            changes = None
            if mtime > self.path.stat().st_mtime:
                try:
                    changes = _load(_read(recovered))[0]
                except (ValueError, AttributeError):
                    reason = '无法读取'
            if changes is not None:
                self.user = user = copy.deepcopy(self.user)  # bindings() reads the merge so far
                for device, slots in changes.items():
                    for slot, binding in slots.items():
                        self._release_others(user, device, slot, binding)
                        user[device][slot] = binding
                self._save(user)  # idempotent: if the rename below fails, the next start merges again
            kept = recovered.with_name(f'{recovered.stem}-{time.strftime("%Y%m%d-%H%M%S", time.localtime(mtime))}.json')
            recovered.rename(kept)  # Windows: never over another kept copy
        except OSError:
            return  # still there: looked at again at the next start
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))
        notices.append((f'已并入 {when} 另存的按键修改（当时 bindings.json 无法读取）' if changes is not None else
                        f'{when} 另存的按键绑定 bindings-recovered.json {reason}，未采用')
                       + f'；该副本已改名为 {kept.name} 保留。')

    def bindings(self, device):
        with self.lock:
            merged = copy.deepcopy(DEFAULTS.get(device, {}))
            for slot, binding in self.user.get(device, {}).items():
                if binding is None:
                    merged.pop(slot, None)
                else:
                    merged[slot] = copy.deepcopy(binding)
            for slot, binding in merged.items():
                binding['default'] = slot not in self.user.get(device, {})
            return merged

    def snapshot(self):
        with self.lock:
            learning = None
            if self.learning:
                learning = {k: self.learning[k] for k in ('device', 'slot', 'step', 'captured')}
            return {'devices': {device: self.bindings(device) for device in DEVICES},
                    # Slots with a built-in binding: an unbound one can still be restored.
                    'defaults': {device: sorted(DEFAULTS.get(device, {})) for device in DEVICES},
                    'learning': learning, 'warning': self.warning}

    def _save(self, user):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps({'version': VERSION, 'devices': user}, ensure_ascii=False, indent=2),
                        encoding='utf-8')
        temp.replace(self.path)

    def set(self, device, slot, binding):
        if device not in DEVICES or slot not in SLOTS:
            raise ValueError('未知的设备或按键位置')
        cleaned = _clean(device, slot, binding)
        with self.lock:
            user = copy.deepcopy(self.user)
            moved = self._release_others(user, device, slot, cleaned)
            user[device][slot] = cleaned
            self._save(user)
            self.user = user
            return {'binding': cleaned, 'released': moved}

    def _release_others(self, user, device, slot, binding):
        """One physical key performs one action: release `binding`'s codes from the other
        slots (in `user`). Returns the slots it was taken from."""
        moved = []
        if not binding or binding['source'] == 'keyboard':
            return moved
        codes = set(binding['fire'])
        for other, existing in self.bindings(device).items():
            if other == slot or existing.get('source') != binding['source']:
                continue
            if codes & set(existing.get('fire', [])):
                user[device][other] = None
                moved.append(other)
            elif codes & set(existing.get('ignore', [])):
                # Its "release" is this key's press now: it must never swallow it.
                kept = {k: v for k, v in existing.items() if k != 'default'}
                kept['ignore'] = sorted(set(existing['ignore']) - codes)
                user[device][other] = kept
        return moved

    def reset(self, device, slot=None):
        with self.lock:
            user = copy.deepcopy(self.user)
            if slot is None:
                user[device] = {}
            else:
                user[device].pop(slot, None)
                # The restored default's key may have moved to another slot meanwhile.
                self._release_others(user, device, slot, DEFAULTS.get(device, {}).get(slot))
            self._save(user)
            self.user = user

    # -- matching -----------------------------------------------------------
    def _match(self, device, source, code):
        now = self.clock()
        bindings = {slot: b for slot, b in self.bindings(device).items() if b.get('source') == source}
        # A code bound to fire somewhere wins over another slot's ignore list.
        for slot, binding in bindings.items():
            if code in binding.get('fire', []):
                if binding.get('gesture') == 'double':
                    previous = self.last_fire.get((device, slot))
                    if previous is None or now - previous > DOUBLE_WINDOW:
                        self.last_fire[(device, slot)] = now
                        return 'ignored'
                    self.last_fire.pop((device, slot), None)
                return slot
        if any(code in binding.get('ignore', []) for binding in bindings.values()):
            return 'ignored'
        return None

    def vendor(self, device, code):
        """A new non-neutral vendor edge. Returns a slot, 'ignored', 'learning' or None."""
        code = _code(code)
        with self.lock:
            if self._takes(device, code):
                self._learn(code, 'vendor')
                return 'learning'
            return self._match(device, 'vendor', code)

    def learns(self, device, code):
        """True when a guided recording would take this vendor code: the core then skips its
        own handling of it (a ⑤ tap, a ③ grab, a hold key)."""
        with self.lock:
            return self._takes(device, _code(code))

    def _takes(self, device, code):
        learning = self.learning
        if not learning or learning['device'] != device or 'vendor' not in learning['sources']:
            return False
        if learning['slot'] in REMAP_SLOTS and code in self._pointer_codes(device):
            return False  # ⑤ (or ⑧) held while the wheel key is pressed: they keep working
        return True

    def _pointer_codes(self, device):
        codes = set(AIR_TAP_CODES.get(device, ()))
        for slot, binding in self.bindings(device).items():
            if slot in HOLD_SLOTS:
                codes.update(binding.get('fire', []) + binding.get('ignore', []))
        return codes

    def pv60(self, code, down):
        code = _code(code)
        with self.lock:
            if self.learning and 'pv60' in self.learning['sources']:
                if down:
                    self._learn(code, 'pv60')
                return 'learning'
            if not down:
                binding_codes = {c for b in self.bindings('pv60').values() for c in b.get('fire', [])}
                return 'ignored' if code in binding_codes else None
            return self._match('pv60', 'pv60', code)

    def keyboard(self, device, vk, down):
        """The receiver's keyboard collection: read only while a wheel key is being recorded."""
        with self.lock:
            learning = self.learning
            if not learning or learning['device'] != device or 'keyboard' not in learning['sources']:
                return None
            vk = int(vk)
            if vk in MODIFIER_VKS:
                return 'learning'
            if learning['sources'] == ('keyboard',):
                if down:
                    self._learn(vk, 'keyboard')
                return 'learning'
            # A vendor code wins: the pen types a key next to some of its codes, sometimes a
            # moment before (④ long press: B, then 6D). So a key is taken on its release,
            # unless a vendor code arrived in between (which ends the recording).
            if down:
                learning.setdefault('pending', vk)
            elif learning.get('pending') == vk:
                self._learn(vk, 'keyboard')
            return 'learning'

    def airmouse_vks(self, device):
        bindings = self.bindings(device)
        return {slot[len('airmouse_'):]: set(bindings.get(slot, {}).get('vk', [])) for slot in REMAP_SLOTS}

    def activity_codes(self, device):
        return set(self.bindings(device).get('airmouse_activity', {}).get('fire', []))

    def hold_slot(self, device, code):
        """For HOLD_SLOTS: ('slot', True) on press, ('slot', False) on release, else None.
        Every slot's press first: one slot's release list never shadows another's press."""
        code = _code(code)
        bindings = self.bindings(device)
        for slot in HOLD_SLOTS:
            if code in (bindings.get(slot) or {}).get('fire', []):
                return slot, True
        for slot in HOLD_SLOTS:
            if code in (bindings.get(slot) or {}).get('ignore', []):
                return slot, False
        return None

    def neutral_release(self, device, code):
        """The neutral report after `code`: ('slot', False) when `code` presses a hold key
        that has no release code of its own (its release is the neutral report), else None."""
        code = _code(code)
        bindings = self.bindings(device)
        for slot in HOLD_SLOTS:
            binding = bindings.get(slot) or {}
            if binding.get('source') == 'vendor' and code in binding.get('fire', []) and not binding.get('ignore'):
                return slot, False
        return None

    def neutral(self, device):
        """A neutral vendor report (00). While a hold key is recorded and only its press has
        arrived, a neutral report well after the press is that key's release. True if taken."""
        with self.lock:
            learning = self.learning
            if (not learning or learning['device'] != device or learning['step'] != 'hold'
                    or learning['slot'] not in HOLD_SLOTS or len(learning['hold']) != 1
                    or learning['source'] != 'vendor'):
                return False
            if self.clock() - learning['hold'][0][1] < NEUTRAL_RELEASE:
                return False  # every press is followed by a neutral report at once
            try:
                self._commit()
            except ValueError:
                pass
            return True

    # -- learning -----------------------------------------------------------
    def learn_start(self, device, slot):
        if device not in DEVICES or slot not in SLOTS:
            raise ValueError('未知的设备或按键位置')
        if device == 'tidepool':
            raise ValueError('Tidepool 的按键由其接收器处理；请使用全局快捷键')
        sources = slot_sources(device, slot)
        source = sources[0]
        if source == 'vendor' and slot in HOLD_SLOTS:
            steps = ['hold']  # press = fire, release = ignore
        elif source == 'vendor' and slot in ACTION_SLOTS:
            steps = ['tap', 'hold']
        elif slot in LONG_SLOTS:
            steps = ['long']  # one long press: its code fires; the short press stays native
        else:
            steps = ['tap']   # a wheel key: one press (a vendor code, or a keyboard key)
        with self.lock:
            self.learning = {'device': device, 'slot': slot, 'source': source, 'sources': sources,
                             'steps': steps, 'step': steps[0], 'captured': [], 'taps': [], 'hold': [],
                             'started': self.clock()}
            self._progress('waiting')
            return self.snapshot()['learning']

    def learn_cancel(self):
        with self.lock:
            if self.learning:
                self._progress('cancelled')
            self.learning = None

    def learn_skip(self):
        """Finish without the long-press step (keys whose firmware has no hold code)."""
        with self.lock:
            if not self.learning:
                raise ValueError('当前没有正在录制的按键')
            return self._commit()

    def tick(self):
        with self.lock:
            if self.learning and self.clock() - self.learning['started'] > LEARN_TIMEOUT:
                self._progress('timeout', '录制超时，未保存修改')
                self.learning = None

    def _progress(self, status, message=''):
        learning = self.learning
        self.on_learn({'device': learning['device'], 'slot': learning['slot'],
                       'step': learning['step'], 'captured': list(learning['captured']),
                       'status': status, 'message': message})

    def _learn(self, code, source):
        learning = self.learning
        learning.pop('pending', None)
        display = 'VK ' + format(code, '02X') if source == 'keyboard' else code.upper()
        learning['captured'].append(display)
        if source != 'keyboard' and code in RESERVED.get(learning['device'], set()):
            self._progress('error', '这是录音键，不能改作其它用途；请换一个键')
            learning['captured'].pop()
            return
        learning['source'] = source
        learning['started'] = self.clock()
        if learning['step'] == 'tap':
            learning['taps'].append((code, self.clock()))
            # A vendor action key: two taps learn both latch codes. A wheel key: one press.
            needed = 2 if source == 'vendor' and learning['slot'] not in REMAP_SLOTS else 1
            if len(learning['taps']) < needed:
                self._progress('step')
            elif learning['steps'][-1] == 'hold':
                learning['step'] = 'hold'
                self._progress('step')
            else:
                self._commit()
        elif learning['step'] == 'long':
            learning['hold'].append((code, self.clock()))
            self._commit()
        else:
            hold = learning['hold']
            if hold and source != 'keyboard' and not is_release(hold[0][0], code):
                # Not the release of the code before it (a stray code came first, or another
                # key): never save an unrelated pair. This code may be the real press.
                press = hold[0][0]
                hold[:] = [(code, self.clock())]
                learning['captured'] = learning['captured'][:-2] + [display]
                self._progress('error', f'{press.upper()} 和 {code.upper()} 不是同一个键的按下与松开'
                                        f'（松开的键码应比按下大 1，例如 68 → 69）；已从 {code.upper()} 重新开始，'
                                        '请只按住要录的键 1 秒再松开')
                return
            hold.append((code, self.clock()))
            if len(hold) >= 2:
                self._commit()
            else:
                self._progress('step')

    def _commit(self):
        learning = self.learning
        device, slot = learning['device'], learning['slot']
        if learning['source'] == 'keyboard':
            binding = {'source': 'keyboard', 'vk': sorted({c for c, _ in learning['taps']}),
                       'label': '键盘 VK ' + '/'.join(format(c, '02X') for c, _ in learning['taps'])}
        else:
            fire = {c for c, _ in learning['taps']}
            ignore = set()
            if learning['hold']:
                fire.add(learning['hold'][0][0])
                if len(learning['hold']) > 1:
                    ignore.add(learning['hold'][1][0])
            fire -= ignore  # the hold step proved it a release, even if a tap sent it too (⑧: 68 -> 69)
            binding = {'source': learning['source'], 'fire': sorted(fire),
                       'ignore': sorted(ignore), 'gesture': 'single',
                       'label': '自定义 ' + '/'.join(sorted(c.upper() for c in fire))}
        if not learning['taps'] and not learning['hold']:
            self._progress('error', '没有收到按键，未保存修改')
            self.learning = None
            raise ValueError('没有收到按键，未保存修改')
        try:
            result = self.set(device, slot, binding)
        except (ValueError, OSError) as error:
            self._progress('error', str(error))
            self.learning = None
            raise ValueError(str(error)) from None
        self._progress('done', '已保存' + ('，并从其它功能上解除了这个键' if result['released'] else ''))
        self.learning = None
        return result
