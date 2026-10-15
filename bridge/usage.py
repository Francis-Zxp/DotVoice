"""Local telemetry and duration counters, never audio or recognized text."""
import copy
import json
import time
from pathlib import Path


class UsageLedger:
    def __init__(self, path, clock=time.time, monotonic=time.monotonic):
        self.path, self.clock, self.monotonic = Path(path), clock, monotonic
        self.data = {"version": 1, "active": None, "cycles": [], "reduced_motion": False,
                     "totals": {"observed": 0, "armed": 0, "recording": 0, "sessions": 0}}
        self.warning = ""
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                assert saved['version'] == 1 and isinstance(saved['cycles'], list)
                for key in self.data['totals']:
                    assert isinstance(saved['totals'][key], (int, float)) and saved['totals'][key] >= 0
                self.data.update(saved)
            except (ValueError, KeyError, TypeError, AssertionError):
                self.warning = "续航记录无法读取；原文件已保留，请检查 runtime/usage.json。"
                self.path = self.path.with_name('usage-recovered.json')
        self.last_tick = self.monotonic()
        for target in [self.data['totals'], self.data['active']]:
            if target is not None:
                target.setdefault('audio_seconds', 0)
                target.setdefault('speech_seconds', 0)
        self.data.setdefault('recent_sessions', [])
        self.data.setdefault('battery', None)
        self.data.setdefault('battery_samples', [])
        self.last_save = self.last_tick
        self.phase = 'disconnected'
        self.dirty = False

    def tick(self, phase=None):
        now = self.monotonic()
        delta = max(0, now - self.last_tick)
        self.last_tick = now
        # A suspension/long scheduler gap is unobserved, never counted as use.
        if delta <= 3:
            for target in [self.data['totals'], self.data['active']]:
                if target is not None:
                    target['observed'] += delta
                    if self.phase in ('idle', 'starting', 'recording', 'processing'):
                        target['armed'] += delta
                    if self.phase in ('starting', 'recording'):
                        target['recording'] += delta
            self.dirty = True
        if phase is not None:
            self.phase = phase
        # Paused counters change slowly; avoid rewriting the file every 30 s.
        interval = 30 if self.phase in ('idle', 'starting', 'recording', 'processing') else 300
        if now - self.last_save >= interval:
            self.save()

    def completed(self):
        for target in [self.data['totals'], self.data['active']]:
            if target is not None:
                target['sessions'] += 1
        self.dirty = True

    def audio(self, value):
        for target in [self.data['totals'], self.data['active']]:
            if target is not None:
                for key in ('audio_seconds', 'speech_seconds'):
                    target[key] = target.get(key, 0) + max(0, float(value.get(key, 0)))
        self.dirty = True

    def battery(self, percent):
        if type(percent) is not int or not 0 <= percent <= 100:
            return
        now = self.clock()
        previous = self.data['battery']
        changed = previous is None or previous['percent'] != percent
        self.data['battery'] = {'percent': percent, 'reported_at': now, 'source': 'baseus_hid'}
        if changed:
            self.data['battery_samples'].append({'percent': percent, 'at': now})
            self.data['battery_samples'] = self.data['battery_samples'][-500:]
        active = self.data['active']
        # A reported 100 starts observation automatically; repeated 100s do not
        # reset it. A rise is recorded, but is not a claimed charging flag.
        if percent == 100 and (active is None or active.get('source') != 'baseus_hid' or
                               (previous and previous['percent'] <= 95)):
            self.full()
            self.data['active'].update(source='baseus_hid', start_percent=100, min_percent=100)
            active = self.data['active']
        if active and active.get('source') == 'baseus_hid':
            active['min_percent'] = min(active.get('min_percent', percent), percent)
            active['last_percent'] = percent
            if percent <= 10:
                # This is a defined observation endpoint, not a firmware alarm.
                active.update(ended=now, complete=True, end_percent=percent, endpoint='10_percent')
                self.data['cycles'].append(active)
                self.data['cycles'] = self.data['cycles'][-20:]
                self.data['active'] = None
        self.dirty = True
        if changed:
            self.save()

    def session(self, value):
        self.data['recent_sessions'].append({key: value[key] for key in
            ('audio_seconds', 'speech_seconds', 'ended', 'outcome')})
        self.data['recent_sessions'] = self.data['recent_sessions'][-20:]
        self.dirty = True
        self.save()

    def full(self):
        self.tick()
        active = self.data['active']
        if active:
            active.update(ended=self.clock(), complete=False)
            self.data['cycles'].append(active)
        self.data['active'] = dict(started=self.clock(), observed=0, armed=0, recording=0, sessions=0)
        self.data['cycles'] = self.data['cycles'][-20:]
        self.dirty = True
        self.save()

    def low(self):
        self.tick()
        if not self.data['active']:
            raise ValueError('请先在充满电后标记一次，再记录低电量。')
        active = self.data['active']
        active.update(ended=self.clock(), complete=True)
        self.data['cycles'].append(active)
        self.data['cycles'] = self.data['cycles'][-20:]
        self.data['active'] = None
        self.dirty = True
        self.save()

    def motion(self, reduced):
        self.data['reduced_motion'] = bool(reduced)
        self.dirty = True
        self.save()

    def snapshot(self):
        result = copy.deepcopy(self.data)
        result['warning'] = self.warning
        result['now'] = self.clock()
        return result

    def save(self):
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(self.path)
        self.last_save = self.monotonic()
        self.dirty = False
