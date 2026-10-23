"""Local telemetry and duration counters, never audio or recognized text."""
import copy
import json
import time
from pathlib import Path

READ_RETRY = .25  # another program (antivirus, backup) briefly holding it
UNREADABLE = (OSError, ValueError, KeyError, TypeError, AssertionError)


def _read(path):
    """A usage file's checked contents. A file another program holds open (Windows
    sharing violation) is tried once more before giving up."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        time.sleep(READ_RETRY)
        text = path.read_text(encoding="utf-8")
    saved = json.loads(text)
    assert saved['version'] == 1 and isinstance(saved['cycles'], list)
    for key in ("observed", "armed", "recording", "sessions"):
        assert isinstance(saved['totals'][key], (int, float)) and saved['totals'][key] >= 0
    # Shapes tick(), battery() and the core's usage view rely on (a hand-edited file).
    assert all(isinstance(cycle, dict) for cycle in saved['cycles'])
    active = saved.get('active')
    assert active is None or (isinstance(active, dict) and all(
        isinstance(active.get(key), (int, float)) for key in ('started', 'observed', 'armed', 'recording', 'sessions')))
    battery = saved.get('battery')
    assert battery is None or (isinstance(battery, dict) and type(battery.get('percent')) is int
                               and isinstance(battery.get('reported_at'), (int, float)))
    samples = saved.get('battery_samples', [])
    assert isinstance(samples, list) and all(
        isinstance(s, dict) and type(s.get('percent')) is int and isinstance(s.get('at'), (int, float)) for s in samples)
    sessions = saved.get('recent_sessions', [])
    assert isinstance(sessions, list) and all(isinstance(s, dict) for s in sessions)
    return saved


def _saved_at(path, form='%Y-%m-%d %H:%M'):
    return time.strftime(form, time.localtime(path.stat().st_mtime))


def _number(value):
    return value if isinstance(value, (int, float)) else 0


class UsageLedger:
    def __init__(self, path, clock=time.time, monotonic=time.monotonic):
        self.path, self.clock, self.monotonic = Path(path), clock, monotonic
        self.data = {"version": 1, "active": None, "cycles": [], "reduced_motion": False,
                     "totals": {"observed": 0, "armed": 0, "recording": 0, "sessions": 0}}
        self.warning = ""
        self.failed = False  # the last save failed: the core said so in self.warning
        if self.path.exists():
            recovered = self.path.with_name(self.path.stem + '-recovered.json')
            try:
                self.data.update(_read(self.path))
            except UNREADABLE as error:
                # The original is kept as it is. New records go to <name>-recovered.json, which
                # is read back while the original stays unreadable (usage-pv60.json has its own).
                problem = '被其它程序占用，暂时无法读取' if isinstance(error, OSError) else '已损坏，无法读取'
                saved = None
                if recovered.exists():
                    try:
                        saved = _saved_at(recovered)
                        self.data.update(_read(recovered))
                    except UNREADABLE:
                        saved = None
                using = f'正在使用之前另存的记录（{saved} 保存）' if saved else '本次从头记录'
                self.warning = (f'续航记录 {self.path.name} {problem}：{using}；原文件保留，'
                                f'新记录保存到 {recovered.name}。')
                self.path = recovered
            else:
                if recovered.exists():
                    self._take_recovered(recovered)
        self.notice = self.warning  # about the file itself: stays for the session
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

    def _take_recovered(self, recovered):
        """The original reads again, but earlier sessions kept their records in the recovered file
        (the original was unreadable then). Those were counted from zero, never from the
        original, so they are added to it. The copy is renamed first, never deleted: it is never
        added twice, nor does it come back later as "the saved records"."""
        try:
            mtime = recovered.stat().st_mtime
            try:
                saved = _read(recovered)
            except (ValueError, KeyError, TypeError, AssertionError):
                saved = None
            kept = recovered.with_name(f'{recovered.stem}-{time.strftime("%Y%m%d-%H%M%S", time.localtime(mtime))}.json')
            recovered.rename(kept)  # Windows: never over another kept copy
        except OSError:
            return  # still there: looked at again at the next start
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))
        if saved is None:
            self.warning = f'{when} 另存的续航记录 {recovered.name} 无法读取，未并入；该副本已改名为 {kept.name} 保留。'
            return
        self._merge(saved)
        self.dirty = True
        try:
            self.save()
        except OSError:
            pass  # in memory; written with a later save
        self.warning = f'已并入 {when} 另存的续航记录（当时 {self.path.name} 无法读取）；该副本已改名为 {kept.name} 保留。'

    def _merge(self, saved):
        data = self.data
        counts = {key: value for key, value in saved['totals'].items() if isinstance(value, (int, float))}
        for key, value in counts.items():
            data['totals'][key] = data['totals'].get(key, 0) + value
        mine, theirs = data.get('active'), saved.get('active')
        if theirs is None:
            if mine is not None:  # that observation went on meanwhile
                for key, value in counts.items():
                    mine[key] = mine.get(key, 0) + value
        else:
            if mine is not None and mine['started'] > theirs['started']:
                mine, theirs = theirs, mine
            if mine is not None:  # a newer observation replaced it, as full() does
                data['cycles'].append({**mine, 'ended': theirs['started'], 'complete': False})
            data['active'] = theirs
        for key, limit, order in (('cycles', 20, 'started'), ('recent_sessions', 20, 'ended'),
                                  ('battery_samples', 500, 'at')):
            items = data.get(key, []) + saved.get(key, [])
            data[key] = sorted(items, key=lambda item: _number(item.get(order)))[-limit:]
        battery = saved.get('battery')
        if battery and (data.get('battery') is None or battery['reported_at'] >= data['battery']['reported_at']):
            data['battery'] = battery

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
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(self.path)
        except OSError:
            self.failed = True
            raise
        self.last_save = self.monotonic()
        self.dirty = False
        if self.failed:
            # Writing works again: the core's "could not be saved" warning is over.
            self.failed, self.warning = False, self.notice
