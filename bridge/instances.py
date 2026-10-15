"""Several identical receivers at once: which pen is which, and whose microphone.

Every HID collection and the USB audio function of one receiver hang below the
same USB composite device node, e.g. (measured 2026-10-07 on the BS-OH085):

    HID\\VID_ABC8&PID_CA08&MI_03\\8&701A230&0&0000      (vendor keys, battery)
      -> USB\\VID_ABC8&PID_CA08&MI_03\\7&79B523&0&0003
      -> USB\\VID_ABC8&PID_CA08\\6&4C78E7C&0&2           <- the receiver
    USB\\VID_ABC8&PID_CA08&MI_00\\7&79B523&0&0000       (waveIn "BaseusPresent")
      -> USB\\VID_ABC8&PID_CA08\\6&4C78E7C&0&2

The receiver has no USB serial number, so Windows derives that last instance
id from the USB port: one receiver is one identity per port. When the tree
cannot be read, everything falls back to one instance per model, which is
exactly the single-receiver behaviour of earlier versions.
"""
import hashlib
import json
from pathlib import Path
import re
import threading
import time

from .devices import PROFILES

MODEL_LABELS = {'baseus': '倍思', 'pv60': '汉王 PV60'}
IDENTITIES = {model: profile['identity'] for model, profile in PROFILES.items()}
MAX_KNOWN = 16          # absent receivers kept for their names; oldest dropped first
LABEL_MAX = 24
RETRY_UNMAPPED = 5.0    # a node that is still starting is looked up again later
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')


def model_of(path):
    upper = (path or '').upper()
    for model, identity in IDENTITIES.items():
        if identity in upper:
            return model
    return None


def interface_instance(path):
    r"""'\\?\HID#VID_x&PID_y&MI_03#8&1&0&0000#{guid}' -> 'HID\VID_x&PID_y&MI_03\8&1&0&0000'.
    Also handles waveIn interface paths, which end in '#{guid}\global'."""
    if not isinstance(path, str):
        return None
    text = path.strip()
    for prefix in ('\\\\?\\', '\\\\.\\', '\\??\\'):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    parts = text.split('#')
    if len(parts) < 3 or not all(parts[:3]):
        return None
    return '\\'.join(parts[:3]).upper()


class DeviceTree:
    """Read-only cfgmgr32 walk. Opens no device; failures read as 'unknown'."""

    def __init__(self):
        self.api = None
        self.lock = threading.Lock()

    def _load(self):
        if self.api is None:
            import ctypes as c
            from ctypes import wintypes as w
            cfg = c.WinDLL('cfgmgr32')
            cfg.CM_Locate_DevNodeW.argtypes = [c.POINTER(w.DWORD), w.LPCWSTR, w.ULONG]
            cfg.CM_Get_Parent.argtypes = [c.POINTER(w.DWORD), w.DWORD, w.ULONG]
            cfg.CM_Get_Device_IDW.argtypes = [w.DWORD, w.LPWSTR, w.ULONG, w.ULONG]
            self.api = (c, w, cfg)
        return self.api

    def present(self, instance):
        """True while the device node exists (CM_LOCATE_DEVNODE_NORMAL finds present nodes only)."""
        try:
            with self.lock:
                c, w, cfg = self._load()
            node = w.DWORD()
            return cfg.CM_Locate_DevNodeW(c.byref(node), instance, 0) == 0
        except (OSError, AttributeError, ValueError):
            return True  # unknown is not proof of removal; the audio data timeout still applies

    def parent_of(self, instance):
        """Instance id of the parent device node, or None."""
        try:
            with self.lock:
                c, w, cfg = self._load()
            node, parent = w.DWORD(), w.DWORD()
            if cfg.CM_Locate_DevNodeW(c.byref(node), instance, 0):  # present nodes only
                return None
            if cfg.CM_Get_Parent(c.byref(parent), node.value, 0):
                return None
            buffer = c.create_unicode_buffer(400)
            if cfg.CM_Get_Device_IDW(parent.value, buffer, len(buffer), 0):
                return None
            return buffer.value.upper() or None
        except (OSError, AttributeError, ValueError):
            return None


_default_tree = None


def default_tree():
    global _default_tree
    if _default_tree is None:
        _default_tree = DeviceTree()
    return _default_tree


def usb_parent(path, identity=IDENTITIES['baseus'], tree=None):
    """USB composite device instance id above an interface path, or None."""
    node = interface_instance(path)
    if not node:
        return None
    tree = tree or default_tree()
    target = 'USB\\' + identity.upper() + '\\'
    for _ in range(6):
        if node.startswith(target):
            return node
        try:
            node = tree.parent_of(node)
        except Exception:
            return None
        if not node:
            return None
    return None


def node_present(instance, tree=None):
    return bool((tree or default_tree()).present(instance))


def instance_id(model, parent):
    return '%s:%s' % (model, hashlib.sha1(parent.upper().encode('utf-8')).hexdigest()[:8])


def fallback_id(model):
    """One instance per model when the device tree cannot be read."""
    return instance_id(model, 'fallback:' + IDENTITIES[model])


def _clean_label(label):
    if not isinstance(label, str):
        raise ValueError('名称必须是文字')
    text = ' '.join(_CONTROL.sub(' ', label).split())
    if len(text) > LABEL_MAX:
        raise ValueError('名称最多 %d 个字' % LABEL_MAX)
    return text


class InstanceRegistry:
    """Known receivers, their names, battery and use. Thread-safe; cheap per event."""

    def __init__(self, path, tree=None, clock=time.time, monotonic=time.monotonic):
        self.path = Path(path)
        self.tree = tree
        self.clock, self.monotonic = clock, monotonic
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.known = {}       # id -> record (persisted)
        self.present = {}     # id -> set of HID paths seen now
        self.paths = {}       # upper path -> (model, parent or None, retry_at)
        self.active = None
        self.warning = ''
        self._load()

    # -- persistence ------------------------------------------------------------
    def _load(self):
        if not self.path.exists():
            return
        try:
            saved = json.loads(self.path.read_text(encoding='utf-8'))
            items = saved.get('instances', []) if isinstance(saved, dict) else []
        except (OSError, ValueError):
            self.warning = '设备名称记录无法读取，已重新开始编号。'
            return
        for item in items:
            try:
                model = item['model']
                record = {'id': str(item['id']), 'model': model, 'seq': int(item['seq']),
                          'label': (item.get('label') or None) if isinstance(item.get('label'), str) else None,
                          'parent': item.get('parent') if isinstance(item.get('parent'), str) else None,
                          'first_seen': float(item.get('first_seen') or 0),
                          'last_seen': float(item.get('last_seen') or 0),
                          'last_used': float(item['last_used']) if item.get('last_used') is not None else None,
                          'battery': None}
                battery = item.get('battery')
                if isinstance(battery, dict) and type(battery.get('percent')) is int:
                    record['battery'] = {'percent': battery['percent'],
                                         'reported_at': float(battery.get('reported_at') or 0)}
                if model in MODEL_LABELS and record['seq'] > 0:
                    self.known[record['id']] = record
            except (KeyError, TypeError, ValueError):
                continue

    def save(self):
        """Never called with self.lock held (lock order: save_lock, then lock)."""
        with self.save_lock:  # HID, session and RPC threads may all save; the newest wins
            with self.lock:
                data = {'version': 1, 'instances': sorted(self.known.values(), key=lambda r: (r['model'], r['seq']))}
                text = json.dumps(data, ensure_ascii=False, indent=2)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix('.tmp')
                temp.write_text(text, encoding='utf-8')
                temp.replace(self.path)
            except OSError:
                self.warning = '设备名称未能保存；重启后可能恢复默认名称。'

    # -- identity -------------------------------------------------------------------
    def _map(self, path):
        """(model, parent or None) for an HID path, cached; failures retried later."""
        upper = (path or '').upper()
        cached = self.paths.get(upper)
        now = self.monotonic()
        if cached and (cached[1] is not None or now < cached[2]):
            return cached[0], cached[1]
        model = model_of(upper)
        parent = usb_parent(upper, IDENTITIES[model], self.tree) if model else None
        if len(self.paths) > 256:
            self.paths.clear()
        self.paths[upper] = (model, parent, now + RETRY_UNMAPPED)
        return model, parent

    def _create(self, rid, model, parent, present):
        """`present` maps every receiver id present now (including `rid`) to its model."""
        now = self.clock()
        same = [r for r in self.known.values() if r['model'] == model]
        others_present = [i for i, m in present.items() if i != rid and m == model]
        if len(same) == 1 and same[0]['id'] not in present and not others_present:
            # The only receiver ever seen came back on another USB port: keep its name.
            old = self.known.pop(same[0]['id'])
            record = {**old, 'id': rid, 'parent': parent, 'last_seen': now}
            if self.active == old['id']:
                self.active = rid
        else:
            seq = 1 + max((r['seq'] for r in same), default=0)
            record = {'id': rid, 'model': model, 'seq': seq, 'label': None, 'parent': parent,
                      'first_seen': now, 'last_seen': now, 'last_used': None, 'battery': None}
        self.known[rid] = record
        self._prune(set(present) | {rid})
        return record

    def _prune(self, keep):
        while len(self.known) > MAX_KNOWN:
            absent = [r for r in self.known.values() if r['id'] not in keep]
            if not absent:
                return
            del self.known[min(absent, key=lambda r: r['last_seen'])['id']]

    def update_present(self, found):
        """`found` is the RawMonitor device list. Returns True when the list changed."""
        groups, unmapped = {}, {}
        for device in found or ():
            path = str(device.get('path', ''))
            model, parent = self._map(path)
            if not model:
                continue
            if parent:
                groups.setdefault(instance_id(model, parent), (model, parent, set()))[2].add(path.upper())
            else:
                unmapped.setdefault(model, set()).add(path.upper())
        for model, paths in unmapped.items():
            if not any(m == model for m, _, _ in groups.values()):
                groups[fallback_id(model)] = (model, None, paths)
        with self.lock:
            changed = False
            ids = set(groups)
            models = {rid: group[0] for rid, group in groups.items()}
            # Receivers found in one scan are numbered by their USB location.
            for rid, (model, parent, _) in sorted(groups.items(), key=lambda item: (item[1][1] or '', item[0])):
                if rid not in self.known:
                    self._create(rid, model, parent, models)
                    changed = True
            if ids != set(self.present):
                changed = True
                now = self.clock()
                for rid in ids ^ set(self.present):
                    if rid in self.known:
                        self.known[rid]['last_seen'] = now
            self.present = {rid: groups[rid][2] for rid in ids}
            if self.active not in self.present:
                changed = changed or self.active is not None
                self.active = None
        if changed:
            self.save()
        return changed

    def observe(self, path):
        """Receiver id for an input event (None when it cannot be told apart).
        Returns (id, changed); an event proves its receiver is present."""
        model, parent = self._map(path)
        if not model:
            return None, False
        with self.lock:
            if parent:
                rid = instance_id(model, parent)
            else:
                here = [i for i in self.present if self.known.get(i, {}).get('model') == model]
                if len(here) == 1:
                    return here[0], False
                if here:
                    return None, False  # several receivers and this one is unreadable
                rid = fallback_id(model)
            if rid in self.present:
                return rid, False
            if rid not in self.known:
                models = {i: self.known[i]['model'] for i in self.present if i in self.known}
                self._create(rid, model, parent, {**models, rid: model})
            self.present[rid] = {str(path).upper()}
            self.known[rid]['last_seen'] = self.clock()
        self.save()
        return rid, True

    def resolve(self, path):
        model, parent = self._map(path)
        if not model:
            return None
        with self.lock:
            if parent:
                return instance_id(model, parent)
            here = [i for i in self.present if self.known.get(i, {}).get('model') == model]
            return here[0] if len(here) == 1 else None

    def target(self, rid):
        """What the audio side needs to open this receiver's own microphone."""
        with self.lock:
            record = self.known.get(rid)
            return {'id': rid, 'parent': record['parent']} if record else None

    def target_for(self, path):
        rid = self.resolve(path)
        return self.target(rid) if rid else None

    def default_target(self, model):
        """The receiver to keep the microphone open on: last used, else first named."""
        with self.lock:
            here = [self.known[i] for i in self.present if i in self.known and self.known[i]['model'] == model]
            if not here:
                return None
            best = min(here, key=lambda r: (-(r['last_used'] or 0), r['seq']))
            return {'id': best['id'], 'parent': best['parent']}

    def primary(self, model):
        """The present receiver whose battery feeds the usage ledger (lowest number)."""
        with self.lock:
            here = [self.known[i] for i in self.present if i in self.known and self.known[i]['model'] == model]
            return min(here, key=lambda r: r['seq'])['id'] if here else None

    def present_count(self, model):
        with self.lock:
            return sum(1 for i in self.present if self.known.get(i, {}).get('model') == model)

    def first_present(self, model):
        return self.primary(model)

    def is_present(self, rid):
        with self.lock:
            return rid in self.present

    # -- state ------------------------------------------------------------------------
    def battery(self, rid, percent):
        if rid is None or type(percent) is not int or not 0 <= percent <= 100:
            return False
        with self.lock:
            record = self.known.get(rid)
            if record is None:
                return False
            previous = record['battery']
            record['battery'] = {'percent': percent, 'reported_at': self.clock()}
            changed = previous is None or previous['percent'] != percent
        if changed:
            self.save()
        return changed

    def set_active(self, rid, used=False):
        """The pen recording now (None when nobody is). `used` stamps last_used."""
        with self.lock:
            if rid is not None and rid not in self.known:
                rid = None
            changed = rid != self.active
            self.active = rid
            if used and rid is not None:
                self.known[rid]['last_used'] = self.clock()
                changed = True
        if changed and used:
            self.save()
        return changed

    def rename(self, rid, label):
        text = _clean_label(label)
        with self.lock:
            if rid not in self.known:
                raise KeyError(rid)
            self.known[rid]['label'] = text or None  # empty restores the default name
        self.save()

    def label(self, rid):
        with self.lock:
            record = self.known.get(rid)
            return self._label(record) if record else None

    @staticmethod
    def _label(record):
        return record['label'] or '%s #%d' % (MODEL_LABELS.get(record['model'], record['model']), record['seq'])

    def snapshot(self):
        with self.lock:
            records = sorted(self.known.values(), key=lambda r: (r['model'] != 'baseus', r['model'], r['seq']))
            return [{'id': r['id'], 'model': r['model'], 'label': self._label(r),
                     'present': r['id'] in self.present,
                     'battery': dict(r['battery']) if r['battery'] else None,
                     'last_used': r['last_used'], 'active': r['id'] == self.active}
                    for r in records]
