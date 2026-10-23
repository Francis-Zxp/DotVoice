"""0.11.8 file and helper fixes (group 2): ui-errors.log under concurrent writers, prefs saved
while prefs.json was busy, notes (title, tags, end-of-text punctuation), the microphone lookup
with two receivers, update-check retries, devices.json shapes and opening links when elevated.
Fakes only: no HID, microphone, network, browser or input injection."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError

from bridge import audio, secrets
from bridge.core import HELP_URLS
from bridge.notes import NoteStore, clean_note
from bridge.prefs import Preferences
from bridge.updates import CHECK_INTERVAL, RETRY_AFTER_ERROR, UpdateWatch

try:
    from .core_fakes import make_core
    from .test_bridge import FakeBackend
except ImportError:
    from core_fakes import make_core
    from test_bridge import FakeBackend

RELEASE = json.dumps({
    'tag_name': 'v0.11.9', 'draft': False, 'prerelease': False,
    'html_url': 'https://github.com/Francis-Zxp/DotVoice/releases/tag/v0.11.9',
    'assets': [{'name': 'DotVoice-0.11.9-windows-x64.zip',
                'browser_download_url': 'https://github.com/Francis-Zxp/DotVoice/releases/download/'
                                        'v0.11.9/DotVoice-0.11.9-windows-x64.zip'}],
}).encode('utf-8')


class Case(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        original = secrets._store
        self.addCleanup(setattr, secrets, '_store', original)

    def core(self, **kwargs):
        core = make_core(self.base, FakeBackend(), **kwargs)
        self.addCleanup(core.close)
        return core


class UiErrorLogTests(Case):
    def test_a_log_cut_mid_character_heals_instead_of_raising(self):
        core = self.core()
        log = self.base / 'runtime' / 'ui-errors.log'
        log.write_bytes('2026-10-23 [x] 中'.encode('utf-8')[:-1])
        self.assertEqual(core.log_ui_error('core-usage', '出错了'), {'ok': True})
        text = log.read_text(encoding='utf-8')  # strict: valid UTF-8 again
        self.assertTrue(text.endswith('[core-usage] 出错了\n'))

    def test_overlapping_writers_keep_the_log_valid(self):
        core = self.core()
        errors = []

        def write(n):
            try:
                for i in range(60):
                    core.log_ui_error('page-%d' % n, '界面渲染错误：第 %d 次，含中文与 emoji 😀' % i)
            except Exception as error:  # noqa: BLE001 - any failure is the bug
                errors.append(error)
        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        text = (self.base / 'runtime' / 'ui-errors.log').read_bytes().decode('utf-8')
        self.assertEqual(len(text.splitlines()), 240)

    def test_a_lone_surrogate_from_a_page_is_kept_as_a_replacement(self):
        core = self.core()
        core.log_ui_error('page', 'bad \ud800 text')
        text = (self.base / 'runtime' / 'ui-errors.log').read_bytes().decode('utf-8')
        self.assertIn('bad ? text', text)

    def test_the_ticker_survives_when_logging_its_failure_fails(self):
        core = self.core()
        calls = []

        def broken(due):
            calls.append(time.monotonic())
            raise ValueError('odd reply')

        def unloggable(where='', detail=''):
            raise UnicodeDecodeError('utf-8', b'\xe4', 0, 1, 'unexpected end of data')
        core._tick_status = broken
        core.log_ui_error = unloggable
        thread = threading.Thread(target=core._ticker, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while len(calls) < 3 and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue(thread.is_alive())
        self.assertGreaterEqual(len(calls), 3, 'later ticks still run')
        core.closed.set()
        thread.join(3)
        core.closed.clear()  # the cleanup's close() runs in full


class PrefsBusyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.path = self.base / 'prefs.json'

    def busy(self, times):
        """prefs.json refuses its first `times` reads (another program holds it)."""
        real, seen = Path.read_text, []

        def read_text(path, *args, **kwargs):
            if path.name == 'prefs.json':
                seen.append(1)
                if times is None or len(seen) <= times:
                    raise PermissionError(32, 'sharing violation')
            return real(path, *args, **kwargs)
        return mock.patch.object(Path, 'read_text', read_text)

    def test_a_briefly_busy_file_is_waited_for_a_little_longer(self):
        Preferences(self.path).update({'sounds': {'volume': 'low'}})
        with self.busy(3), mock.patch('bridge.prefs.READ_RETRY', 0):
            prefs = Preferences(self.path)
        self.assertEqual(prefs.warning, '')
        self.assertEqual(prefs.get('sounds', 'volume'), 'low')

    def test_changes_saved_while_prefs_json_was_busy_survive_the_next_start(self):
        Preferences(self.path).save()  # an existing install
        before = self.path.read_bytes()
        with self.busy(None), mock.patch('bridge.prefs.READ_RETRY', 0):
            first = Preferences(self.path)
        self.assertIn('占用', first.warning)
        first.update({'sounds': {'volume': 'high'}})  # lands in prefs-recovered.json
        recovered = self.base / 'prefs-recovered.json'
        self.assertTrue(recovered.exists())
        self.assertEqual(self.path.read_bytes(), before)
        os.utime(self.path, (time.time() - 60, time.time() - 60))  # prefs.json is the older one
        second = Preferences(self.path)  # readable again
        self.assertEqual(second.warning, '')
        self.assertEqual(second.get('sounds', 'volume'), 'high')
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['sounds']['volume'], 'high')
        self.assertFalse(recovered.exists(), 'retired, never loaded again later')
        self.assertTrue((self.base / 'prefs-recovered.json.bak').exists(), 'kept aside, not deleted')

    def test_a_stale_recovered_copy_is_retired_and_never_used(self):
        recovered = self.base / 'prefs-recovered.json'
        recovered.write_text(json.dumps({'sounds': {'volume': 'low'}}), encoding='utf-8')
        os.utime(recovered, (time.time() - 3600, time.time() - 3600))
        self.path.write_text(json.dumps({'sounds': {'volume': 'high'}}), encoding='utf-8')
        prefs = Preferences(self.path)
        self.assertEqual(prefs.get('sounds', 'volume'), 'high')
        self.assertFalse(recovered.exists())
        self.path.write_text('{"sounds": {"volume": "hi', encoding='utf-8')  # really damaged later
        damaged = Preferences(self.path)
        self.assertEqual(damaged.get('sounds', 'volume'), 'medium', 'defaults, not hour-old settings')

    def test_the_self_test_settings_never_touch_the_real_recovered_copy(self):
        recovered = self.base / 'prefs-recovered.json'
        recovered.write_text(json.dumps({'sounds': {'volume': 'low'}}), encoding='utf-8')
        smoke = self.base / 'smoke-prefs.json'
        smoke.write_text(json.dumps({'sounds': {'volume': 'high'}}), encoding='utf-8')
        os.utime(smoke, (time.time() - 60, time.time() - 60))
        self.assertEqual(Preferences(smoke).get('sounds', 'volume'), 'high')
        self.assertTrue(recovered.exists())
        smoke.write_text('not json', encoding='utf-8')
        broken = Preferences(smoke)
        self.assertEqual(broken.path.name, 'smoke-prefs-recovered.json')
        self.assertEqual(broken.get('sounds', 'volume'), 'medium')


class NoteFixTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = NoteStore(Path(folder.name) / 'notes')

    def test_editing_the_first_line_updates_the_title(self):
        note = self.store.add('今天要去跟王老师讨论光催化。下午改论文。')
        self.assertEqual(note['title'], '今天要去跟王老师讨论光催化。')
        edited = self.store.update(note['id'], '今天要去跟黄老师讨论光催化。\n下午改论文。')
        self.assertEqual(edited['title'], '今天要去跟黄老师讨论光催化。')
        self.assertEqual(self.store.list()['items'][0]['title'], '今天要去跟黄老师讨论光催化。')

    def test_one_note_with_odd_tags_does_not_hide_every_note(self):
        good = self.store.add('第一条')
        odd = self.store.add('第二条', ['论文'])
        path = self.store.directory / (odd['id'] + '.json')
        data = json.loads(path.read_text(encoding='utf-8'))
        data['tags'] = [{}, 3, '论文', None]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        listing = self.store.list()
        self.assertEqual({n['id'] for n in listing['items']}, {good['id'], odd['id']})
        self.assertEqual(listing['tags'], ['论文'])
        self.assertEqual(self.store.set_tags(odd['id'], ['论文', '灵感'])['tags'], ['论文', '灵感'])

    def test_end_marks_and_closers_at_the_end_stay_on_their_line(self):
        cases = {
            '你好？！': '你好？！',
            '他说：“走吧。”': '他说：“走吧。”',
            '好的。）': '好的。）',
            '第一句。第二句！？': '第一句。\n第二句！？',
            'Really?!': 'Really?!',
            # Mid-text cases from 0.11.7 still break after the whole run.
            '真的吗？！我不信。': '真的吗？！\n我不信。',
            '他说：“真的吗？”然后走了。': '他说：“真的吗？”\n然后走了。',
            '第一句。第二句！第三句？': '第一句。\n第二句！\n第三句？',
        }
        for source, expected in cases.items():
            self.assertEqual(clean_note(source), expected, source)


class FindDeviceTests(unittest.TestCase):
    """Receiver B's HID is present; only receiver A's microphone endpoint is listed/mapped."""
    A_PARENT = 'USB\\VID_ABC8&PID_CA08\\A'
    B_PARENT = 'USB\\VID_ABC8&PID_CA08\\B'

    def lookup(self, inputs, paths, owners, parent):
        from bridge.devices import get_profile
        identity = get_profile('baseus')['identity']
        with mock.patch('bridge.rawinput.devices', return_value=[{'path': identity}]), \
                mock.patch.object(audio, 'wave_inputs', return_value=inputs), \
                mock.patch.object(audio, 'interface_path', side_effect=lambda i: paths.get(i)), \
                mock.patch('bridge.instances.usb_parent',
                           side_effect=lambda path, identity=None, tree=None: owners.get(path)):
            return audio.find_device('baseus', parent=parent)

    def test_another_receivers_microphone_is_never_taken_for_this_pen(self):
        inputs = [(0, 'Microphone (Realtek)'), (1, '麦克风 (BaseusPresent)')]
        paths = {0: 'REALTEK', 1: 'A_MIC'}
        with self.assertRaisesRegex(RuntimeError, '尚未就绪'):
            self.lookup(inputs, paths, {'A_MIC': self.A_PARENT}, self.B_PARENT)
        self.assertEqual(self.lookup(inputs, paths, {'A_MIC': self.A_PARENT}, self.A_PARENT)[0], 1)

    def test_an_unmapped_endpoint_beside_another_receivers_one_is_this_pens(self):
        inputs = [(0, '麦克风 (BaseusPresent)'), (1, '麦克风 (2- BaseusPresent)')]
        paths = {0: 'A_MIC', 1: None}  # B's endpoint path cannot be read
        self.assertEqual(self.lookup(inputs, paths, {'A_MIC': self.A_PARENT}, self.B_PARENT)[0], 1)

    def test_a_single_receiver_whose_lookup_fails_still_uses_its_name(self):
        inputs = [(0, '麦克风 (BaseusPresent)')]
        self.assertEqual(self.lookup(inputs, {0: 'X'}, {}, self.A_PARENT)[0], 0)


class UpdateRetryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'app-update.json'
        self.now = [1_000_000.0]
        self.answers = []

    def fetch(self):
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def watch(self):
        return UpdateWatch(self.path, '0.11.8', fetch=self.fetch, clock=lambda: self.now[0])

    def test_offline_at_logon_retries_soon_and_after_a_restart(self):
        watch = self.watch()
        self.answers.append(OSError('network down'))
        self.assertEqual(watch.check()['status'], 'error')
        self.now[0] += RETRY_AFTER_ERROR - 60
        self.assertFalse(watch.due())
        self.now[0] += 60
        self.assertTrue(watch.due())
        self.assertTrue(self.watch().due(), 'a restart keeps the early retry')
        self.answers.append(RELEASE)
        self.assertEqual(watch.check()['status'], 'available')
        self.now[0] += RETRY_AFTER_ERROR
        self.assertFalse(watch.due(), 'after a success: six hours again')
        self.now[0] += CHECK_INTERVAL
        self.assertTrue(watch.due())

    def test_rate_limits_and_missing_releases_keep_the_six_hour_interval(self):
        watch = self.watch()
        for code in (403, 429, 404):
            self.answers.append(HTTPError('https://api.github.com/', code, 'no', None, None))
            self.assertEqual(watch.check(force=True)['status'], 'error')
            self.now[0] += RETRY_AFTER_ERROR + 1
            self.assertFalse(watch.due(), code)
        self.answers.append(HTTPError('https://api.github.com/', 503, 'busy', None, None))
        watch.check(force=True)
        self.now[0] += RETRY_AFTER_ERROR
        self.assertTrue(watch.due(), 'a server error is passing too')


class DevicesJsonTests(Case):
    def test_a_devices_json_of_the_wrong_shape_keeps_the_defaults(self):
        runtime = self.base / 'runtime'
        runtime.mkdir()
        for content in ('null', '[]', '"pv60"', '{"device": []}', '{"device": {"x": 1}, "mode": ["hold"]}'):
            (runtime / 'devices.json').write_text(content, encoding='utf-8')
            core = make_core(self.base, FakeBackend())
            try:
                self.assertEqual((core.selected, core.mode), ('baseus', 'hold'), content)
            finally:
                core.close()
        (runtime / 'devices.json').write_text('{"device": "pv60", "mode": "toggle"}', encoding='utf-8')
        core = make_core(self.base, FakeBackend())
        self.addCleanup(core.close)
        self.assertEqual((core.selected, core.mode), ('pv60', 'toggle'))


class OpenUrlTests(Case):
    def test_an_elevated_core_opens_links_through_the_desktop_shell(self):
        core = self.core()
        core.uipi._own = {'elevated': True, 'can_elevate': True}
        with mock.patch('bridge.core.subprocess.Popen') as popen, mock.patch('bridge.core.os.startfile') as start:
            self.assertEqual(core.open_url(HELP_URLS[0]), {'ok': True})
            self.assertIn('error', core.open_url('https://example.com/'))
        start.assert_not_called()
        self.assertEqual(popen.call_count, 1)
        command = popen.call_args.args[0]
        self.assertEqual(Path(command[0]).name.lower(), 'explorer.exe')
        self.assertEqual(command[1:], [HELP_URLS[0]], 'the link is one argument, nothing else')

    def test_a_link_that_cannot_open_is_an_error_not_an_exception(self):
        core = self.core()
        with mock.patch('bridge.core.os.startfile', side_effect=OSError(1155, 'no association')):
            self.assertIn('无法打开链接', core.open_url(HELP_URLS[1])['error'])


if __name__ == '__main__':
    unittest.main()
