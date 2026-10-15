"""Speech-engine selection, keys over RPC, connection tests and the core side of the
setup wizard (bridge.prefs / bridge.core / bridge.controller).

No network, microphone or key press: other engines are fakes (or real ones built
without keys, which never connect), secrets use real DPAPI in a temporary folder and
probes either use fakes or stop at the configuration check before any connection."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
import uuid

from bridge import secrets
from bridge.controller import (CLOUD_LIMIT_NOTICE, EXTERNAL_NOTES_NOTICE, TESTED_MIC_OFF_NOTICE, Controller, backend_label,
                               handed_to)
from bridge.core import (ENGINE_BUSY, HELP_URLS, SECRET_NAMES, UnavailableBackend, cloud_settings,
                         engine_signature)
from bridge.engines import cloud, hotkey
from bridge.engines.cloud import CloudBackend
from bridge.engines.hotkey import ExternalAppOnly, HotkeyBackend, get_profile
from bridge.prefs import DEFAULTS, Preferences

try:
    from .test_bridge import FakeBackend, FakeCapture, event, wait_for
    from .core_fakes import make_core
except ImportError:
    from test_bridge import FakeBackend, FakeCapture, event, wait_for
    from core_fakes import make_core

SPEECH = (3000).to_bytes(2, 'little', signed=True) * 1600   # 0.1 s, loud enough to be a level


class LabelledBackend(FakeBackend):
    """A built-in engine stand-in (豆包语音 / 阿里云百炼): its own label, cloud live text."""
    partial_source = 'cloud'

    def __init__(self, label='豆包语音'):
        super().__init__()
        self.label = label
        self.insert_seen = []

    def start(self):
        self.insert_seen.append(self.insert_text)
        super().start()


class ExternalBackend(FakeBackend):
    """A hotkey-bridge stand-in: the other app types; the result carries no words."""
    external = True

    def __init__(self, name='闪电说'):
        super().__init__()
        self.name = name
        self.insert_seen = []

    def start(self):
        self.insert_seen.append(self.insert_text)
        if not self.insert_text:
            raise ExternalAppOnly('「闪电说」会把文字直接输入到光标处，不能保存为点声笔记；请切换到输入模式')
        super().start()

    def stop(self):
        super().stop()
        self.incoming.put({'type': 'result', 'text': '', 'external': True, 'source': 'hotkey',
                           'app': self.name, 'profile': 'shandianshuo'})


class RecordingCapture(FakeCapture):
    """Remembers every capture the controller opens (session and meter-only ones)."""
    opened = []

    def __init__(self, callback, error, target=None):
        super().__init__(callback, error)
        self.closed = False
        RecordingCapture.opened.append(self)

    def close(self):
        self.closed = True


class TempCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.events, self.sounds = [], []
        original = secrets._store
        self.addCleanup(setattr, secrets, '_store', original)   # each core points it at its runtime/

    def write_prefs(self, data):
        runtime = self.base / 'runtime'
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / 'prefs.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def core(self, backend=None, **kwargs):
        core = make_core(self.base, backend or FakeBackend(), events=self.events, sounds=self.sounds, **kwargs)
        self.addCleanup(core.close)
        return core

    def emitted(self, kind):
        return [value for k, value in self.events if k == kind]

    def arm(self, control):
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')

    def record(self, control, audio=()):
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        for pcm in audio:
            control._audio(pcm)
        control.command('stop')
        wait_for(lambda: control.phase in ('processing', 'idle'))


# -- preferences ------------------------------------------------------------------------
class EnginePrefsTests(unittest.TestCase):
    def prefs(self, folder, **kwargs):
        return Preferences(Path(folder) / 'prefs.json', **kwargs)

    def test_defaults_keep_openless_and_hold_no_keys(self):
        self.assertEqual(DEFAULTS['engine']['kind'], 'openless', 'an existing setup is never switched')
        text = json.dumps(DEFAULTS['engine'])
        for word in ('api_key', 'token', 'secret', 'access'):
            self.assertNotIn(word, text)
        with tempfile.TemporaryDirectory() as folder:
            prefs = self.prefs(folder)
            for patch in ({'engine': {'volc': {'api_key': 'sk-1'}}}, {'engine': {'polish': {'api_key': 'x'}}},
                          {'engine': {'dashscope': {'key': 'x'}}}):
                with self.assertRaisesRegex(ValueError, '未知设置'):
                    prefs.update(patch)
            self.assertFalse((Path(folder) / 'prefs.json').exists())

    def test_engine_values_are_validated_and_normalised(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs = self.prefs(folder)
            data = prefs.update({'engine': {'kind': 'volc', 'volc': {'auth': 'legacy', 'app_id': ' 1234567 ', 'resource_id': '',
                                                                     'endpoint': 'bigmodel_nostream'},
                                            'dashscope': {'model': 'paraformer-realtime-v2', 'region': 'intl'},
                                            'polish': {'style': 'custom', 'base_url': 'https://api.example.com/v1',
                                                       'model': ' gpt-x ', 'prompt': '第一行\n第二行'},
                                            'hotkey': {'profile': 'typeless', 'keys': 'ControlLeft+MetaLeft', 'style': 'hold'}}})
            engine = data['engine']
            self.assertEqual(engine['volc'], {'auth': 'legacy', 'app_id': '1234567', 'resource_id': 'volc.seedasr.sauc.duration',
                                              'endpoint': 'bigmodel_nostream', 'hotwords': True})
            self.assertEqual(engine['polish']['model'], 'gpt-x')
            self.assertEqual(engine['polish']['prompt'], '第一行\n第二行', 'a prompt keeps its line breaks')
            self.assertEqual(engine['hotkey']['keys'], 'Ctrl + Win', 'recorded key codes are stored as display text')
            self.assertEqual(prefs.update({'engine': {'hotkey': {'keys': '右 Alt'}}})['engine']['hotkey']['keys'], '右 Alt')
            self.assertEqual(prefs.update({'engine': {'hotkey': {'keys': 'ctrl+alt+space'}}})['engine']['hotkey']['keys'],
                             'Ctrl + Alt + 空格')
            self.assertEqual(prefs.update({'engine': {'polish': {'base_url': 'http://192.168.1.5:3000/v1'}}})['engine']['polish']
                             ['base_url'], 'http://192.168.1.5:3000/v1', 'plain http only on the LAN')
            bad = [
                {'kind': 'whisper'}, {'volc': {'auth': 'token'}}, {'volc': {'app_id': 'abc'}},
                {'volc': {'resource_id': 'bad id!'}}, {'volc': {'endpoint': 'bigmodel_fast'}},
                {'dashscope': {'model': 'qwen3-asr-flash-realtime'}}, {'dashscope': {'model': 'paraformer-realtime-8k-v2'}},
                {'dashscope': {'region': 'us'}}, {'polish': {'style': 'heavy'}},
                {'polish': {'base_url': 'http://api.example.com/v1'}}, {'polish': {'base_url': 'ftp://example.com'}},
                {'polish': {'model': 'x' * 200}}, {'polish': {'prompt': 'a\x00b'}}, {'polish': {'prompt': 'x' * 2001}},
                {'hotkey': {'profile': 'zoom'}}, {'hotkey': {'keys': 'Ctrl+Ctrl'}}, {'hotkey': {'keys': 'A+B'}},
                {'hotkey': {'keys': 'Hyper+Q'}}, {'hotkey': {'style': 'double'}}, {'volc': 'key'},
            ]
            for patch in bad:
                with self.assertRaises(ValueError, msg=patch):
                    prefs.update({'engine': patch})
            self.assertEqual(prefs.get('engine', 'kind'), 'volc', 'a refused patch changes nothing')

    def test_setup_is_only_pending_on_a_fresh_install(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prefs.json'
            self.assertFalse(Preferences(path).get('setup', 'done'), 'fresh install: the wizard opens')
            self.assertTrue(Preferences(path, existing=True).get('setup', 'done'), 'other runtime files: never asked')
            path.write_text(json.dumps({'mode': 'notes'}), encoding='utf-8')
            self.assertTrue(Preferences(path).get('setup', 'done'), 'prefs from an earlier version: never asked')
            path.write_text(json.dumps({'setup': {'done': False}}), encoding='utf-8')
            self.assertFalse(Preferences(path).get('setup', 'done'), 'a wizard left unfinished opens again')
            path.write_text('{broken', encoding='utf-8')
            self.assertTrue(Preferences(path).get('setup', 'done'), 'an unreadable file still means an existing user')

    def test_the_core_only_offers_the_wizard_on_a_fresh_install(self):
        original = secrets._store
        self.addCleanup(setattr, secrets, '_store', original)
        with tempfile.TemporaryDirectory() as folder:
            core = make_core(Path(folder))
            self.assertFalse(core.snapshot()['prefs']['setup']['done'], 'nothing in runtime/: a fresh install')
            core.close()
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder) / 'runtime'
            runtime.mkdir()
            (runtime / 'usage.json').write_text('{}', encoding='utf-8')   # used before, prefs never written
            core = make_core(Path(folder))
            self.assertTrue(core.snapshot()['prefs']['setup']['done'])
            self.assertEqual(core.snapshot()['engine']['kind'], 'openless', 'and its engine stays OpenLess')
            core.close()

    def test_saved_engine_settings_load_leaf_by_leaf(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prefs.json'
            path.write_text(json.dumps({'engine': {'kind': 'volc', 'volc': {'app_id': 'abc', 'resource_id': 'volc.bigasr.sauc.duration'},
                                                   'hotkey': {'keys': 'Nope+Q', 'profile': 'wechat'}, 'retired': 1}}),
                            encoding='utf-8')
            engine = Preferences(path).get('engine')
            self.assertEqual(engine['kind'], 'volc')
            self.assertEqual(engine['volc']['app_id'], '', 'the invalid leaf is skipped')
            self.assertEqual(engine['volc']['resource_id'], 'volc.bigasr.sauc.duration')
            self.assertEqual((engine['hotkey']['profile'], engine['hotkey']['keys']), ('wechat', ''))


# -- building and swapping engines ----------------------------------------------------------
class EngineConstructionTests(TempCase):
    def test_prefs_map_onto_the_engine_settings(self):
        engine = json.loads(json.dumps(DEFAULTS['engine']))
        engine.update(kind='volc')
        engine['volc'].update(auth='legacy', app_id='123456', endpoint='bigmodel')
        settings = cloud_settings(engine)
        self.assertEqual(settings['engine'], 'volc')
        self.assertEqual(settings['volc'], {'auth': 'app', 'app_id': '123456', 'resource_id': 'volc.seedasr.sauc.duration',
                                            'mode': 'stream'})
        engine['volc']['auth'] = 'key'
        self.assertEqual(cloud_settings(engine)['volc']['auth'], 'api_key')
        self.assertEqual(cloud_settings(engine)['polish'], DEFAULTS['engine']['polish'])
        before = engine_signature(engine)
        engine['dashscope']['region'] = 'intl'
        self.assertEqual(engine_signature(engine), before, 'an inactive engine does not need a rebuild')
        engine['polish']['style'] = 'light'
        self.assertNotEqual(engine_signature(engine), before)

    def test_the_core_builds_the_engine_saved_in_prefs(self):
        self.write_prefs({'engine': {'kind': 'volc', 'volc': {'endpoint': 'bigmodel_async'}}})
        core = self.core()
        backend = core.controller.backend
        self.assertIsInstance(backend, CloudBackend)
        self.assertEqual((backend.label, backend.asr.mode, backend.asr.auth), ('豆包语音', 'async', 'api_key'))
        self.assertEqual(core.snapshot()['engine'], {'kind': 'volc', 'label': '豆包语音', 'external': False})
        self.assertIsNone(backend.polish, 'raw style: no language model involved')
        # Keys come from the core's own store, read when a recording starts.
        raw = 'vk-' + uuid.uuid4().hex
        self.assertNotIn('error', core.secret_set(cloud.SECRET_VOLC_API_KEY, raw))
        self.assertEqual(backend.asr.headers('request')['X-Api-Key'], raw)
        self.assertTrue((self.base / 'runtime' / 'secrets.json').exists())

    def test_bailian_and_the_hotkey_bridge(self):
        self.write_prefs({'engine': {'kind': 'dashscope', 'dashscope': {'model': 'paraformer-realtime-v2', 'region': 'intl'}}})
        core = self.core()
        self.assertEqual(core.controller.backend.asr.model_name(), 'paraformer-realtime-v2')
        self.assertEqual(core.controller.backend.asr.region, 'intl')
        self.assertEqual(core.engine_view()['label'], '阿里云百炼')
        core.close()

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.write_prefs({'engine': {'kind': 'hotkey', 'hotkey': {'profile': 'typeless', 'keys': 'Ctrl+Alt+Space'}}})
        core = self.core()
        backend = core.controller.backend
        self.assertIsInstance(backend, HotkeyBackend)
        self.assertEqual(backend.profile.keys, ('LCONTROL', 'LMENU', 'SPACE'))
        self.assertTrue(backend.profile.custom)
        view = core.engine_view()
        self.assertEqual((view['label'], view['external'], view['app'], view['combo'], view['style']),
                         ('Typeless', True, 'Typeless', 'Ctrl + Alt + 空格', 'toggle'))
        self.assertEqual(backend_label(backend), 'Typeless')

    def test_an_unusable_engine_explains_itself_instead_of_falling_back(self):
        self.write_prefs({'engine': {'kind': 'hotkey', 'hotkey': {'profile': 'custom', 'keys': ''}}})
        core = self.core()
        backend = core.controller.backend
        self.assertIsInstance(backend, UnavailableBackend)
        self.assertEqual(core.engine_view()['error'], '请先录入目标软件的快捷键')
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'error')
        self.assertEqual(control.message, '请先录入目标软件的快捷键')
        self.assertNotIn('connect', core.openless_backend.calls, 'OpenLess is not used behind the user\'s back')


class EngineSwapTests(TempCase):
    def setUp(self):
        super().setUp()
        self.built = []

    def factory(self, kind, engine):
        backend = ExternalBackend() if kind == 'hotkey' else LabelledBackend('豆包语音' if kind == 'volc' else '阿里云百炼')
        self.built.append((kind, backend))
        return backend

    def test_switching_while_idle_disarms_swaps_and_rearms(self):
        core = self.core(engine_factory=self.factory)
        control, openless = core.controller, core.openless_backend
        self.arm(control)
        data = core.prefs_set({'engine': {'kind': 'volc'}})
        self.assertEqual(data['engine']['kind'], 'volc')
        new = self.built[-1][1]
        self.assertIs(control.backend, new)
        self.assertIn('close', openless.calls)
        wait_for(lambda: control.phase == 'idle' and control.want_armed and 'connect' in new.calls)
        self.assertEqual(self.emitted('engine')[-1]['label'], '豆包语音')
        self.assertEqual(core.snapshot()['engine']['kind'], 'volc')
        saved = Preferences(self.base / 'runtime' / 'prefs.json').get('engine', 'kind')
        self.assertEqual(saved, 'volc')
        phases = [value['phase'] for value in self.emitted('state')]
        self.assertIn('disconnected', phases[phases.index('idle') + 1:], 'paused while the engine changed')
        # And back: OpenLess is the very backend the core was given.
        core.prefs_set({'engine': {'kind': 'openless'}})
        self.assertIs(control.backend, openless)
        wait_for(lambda: control.phase == 'idle' and 'close' in new.calls)

    def test_a_switch_that_cannot_pause_the_voice_turns_it_back_on(self):
        # Review 2026-10-14: the pause was asked for, then timed out - the engine stayed the
        # same and the voice stayed paused until the user noticed.
        core = self.core(engine_factory=self.factory)
        control, openless = core.controller, core.openless_backend
        self.arm(control)

        def stuck(timeout=4):
            control.command('disarm')  # what the real one asks for first
            wait_for(lambda: control.phase == 'disconnected')
            raise RuntimeError('翻页笔尚未完全释放，请稍后再试')
        control.disarm_and_wait = stuck
        answer = core.prefs_set({'engine': {'kind': 'volc'}})
        self.assertIn('翻页笔尚未完全释放', answer['error'])
        self.assertIn('已恢复', answer['error'])
        self.assertIs(control.backend, openless, 'the engine did not change')
        self.assertEqual(core.prefs.get('engine', 'kind'), 'openless')
        self.assertIn('close', self.built[-1][1].calls, 'the unused engine is let go')
        wait_for(lambda: control.want_armed and control.phase == 'idle')

    def test_switching_while_paused_does_not_enable_the_voice(self):
        core = self.core(engine_factory=self.factory)
        control = core.controller
        core.prefs_set({'engine': {'kind': 'hotkey'}})
        backend = self.built[-1][1]
        self.assertIs(control.backend, backend)
        time.sleep(.2)
        self.assertFalse(control.want_armed)
        self.assertEqual(control.phase, 'disconnected')
        self.assertNotIn('connect', backend.calls)
        self.assertTrue(core.engine_view()['external'])

    def test_switching_is_refused_while_recording_and_changes_nothing(self):
        core = self.core(engine_factory=self.factory)
        control, openless = core.controller, core.openless_backend
        self.arm(control)
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        before = core.prefs.get('engine')
        self.assertEqual(core.prefs_set({'engine': {'kind': 'volc'}}), {'error': ENGINE_BUSY})
        self.assertEqual(core.prefs.get('engine'), before)
        self.assertIs(control.backend, openless)
        self.assertEqual(control.phase, 'recording')
        self.assertEqual(self.built, [], 'nothing was even built')
        saved = json.loads((self.base / 'runtime' / 'prefs.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['engine']['kind'], 'openless', 'the saved engine is unchanged')

    def test_only_the_active_engines_settings_rebuild_it(self):
        core = self.core(engine_factory=self.factory)
        control = core.controller
        core.prefs_set({'engine': {'kind': 'volc'}})
        self.assertEqual(len(self.built), 1)
        core.prefs_set({'engine': {'dashscope': {'region': 'intl'}}})
        core.prefs_set({'engine': {'hotkey': {'profile': 'wechat'}}})
        self.assertEqual(len(self.built), 1, 'other engines are only settings')
        core.prefs_set({'engine': {'volc': {'resource_id': 'volc.seedasr.sauc.concurrent'}}})
        self.assertEqual(len(self.built), 2, 'the active engine is rebuilt')
        self.assertIs(control.backend, self.built[-1][1])
        self.arm(control)
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        self.assertEqual(core.prefs_set({'engine': {'polish': {'style': 'light', 'base_url': 'https://api.example.com/v1',
                                                               'model': 'm'}}}), {'error': ENGINE_BUSY})
        self.assertNotIn('error', core.prefs_set({'engine': {'dashscope': {'model': 'paraformer-realtime-v2'}}}))
        self.assertEqual(len(self.built), 2)

    def test_an_unusable_hotkey_profile_is_refused_before_switching(self):
        core = self.core()   # the real builders
        answer = core.prefs_set({'engine': {'kind': 'hotkey', 'hotkey': {'profile': 'custom', 'keys': ''}}})
        self.assertEqual(answer, {'error': '请先录入目标软件的快捷键'})
        self.assertEqual(core.prefs.get('engine', 'kind'), 'openless')
        data = core.prefs_set({'engine': {'kind': 'hotkey', 'hotkey': {'profile': 'custom', 'keys': 'F9', 'style': 'hold'}}})
        self.assertEqual(data['engine']['hotkey']['keys'], 'F9')
        self.assertEqual(core.controller.backend.profile.style, 'hold')
        self.assertEqual(core.engine_view()['label'], '自定义快捷键')



class PrefsRecoveryTests(TempCase):
    def test_a_briefly_locked_settings_file_is_read_on_the_second_try(self):
        path = self.base / 'prefs.json'
        path.write_text(json.dumps({'sounds': {'enabled': False}}), encoding='utf-8')
        real, calls = Path.read_text, []

        def locked_once(self_path, *args, **kwargs):
            calls.append(self_path.name)
            if len(calls) == 1:
                raise PermissionError(13, 'The process cannot access the file')  # sharing violation
            return real(self_path, *args, **kwargs)
        with mock.patch('bridge.prefs.READ_RETRY', 0), mock.patch.object(Path, 'read_text', locked_once):
            prefs = Preferences(path)
        self.assertEqual(prefs.warning, '')
        self.assertFalse(prefs.get('sounds', 'enabled'), 'the saved settings, not defaults')
        self.assertEqual(prefs.path.name, 'prefs.json')

    def test_an_unreadable_file_is_said_and_later_changes_are_not_lost(self):
        path = self.base / 'prefs.json'
        path.write_text('{"sounds": {"enabled": fal', encoding='utf-8')   # cut off mid-write
        prefs = Preferences(path)
        self.assertIn('已损坏', prefs.warning)
        self.assertIn('默认设置', prefs.warning)
        prefs.update({'sounds': {'volume': 'high'}})
        self.assertEqual(path.read_text(encoding='utf-8'), '{"sounds": {"enabled": fal', 'the original is kept')
        again = Preferences(path)   # next launch: the changes made since are read back, and said
        self.assertEqual(again.get('sounds', 'volume'), 'high')
        self.assertIn('之前另存的设置', again.warning)
        with mock.patch('bridge.prefs.READ_RETRY', 0), \
                mock.patch.object(Path, 'read_text', side_effect=PermissionError(13, 'locked')):
            busy = Preferences(path)
        self.assertIn('占用', busy.warning)

    def test_the_warning_reaches_the_snapshot_and_status_json(self):
        self.write_prefs({})
        (self.base / 'runtime' / 'prefs.json').write_text('not json', encoding='utf-8')
        core = self.core()
        self.assertIn('prefs.json', core.snapshot()['prefs_warning'])
        core._write_status()
        status = json.loads((self.base / 'runtime' / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(status['prefs_warning'], core.prefs.warning)
        self.assertEqual(status['bindings_warning'], '')


# -- keys over RPC ----------------------------------------------------------------------------
class SecretRpcTests(TempCase):
    def test_raw_keys_never_leave_the_core(self):
        core = self.core()
        raw = 'sk-RAWSECRET' + uuid.uuid4().hex
        answer = core.secret_set(cloud.SECRET_DASHSCOPE_API_KEY, '  ' + raw + '\n')
        self.assertEqual(answer['secrets'][cloud.SECRET_DASHSCOPE_API_KEY], {'set': True, 'masked': '••••' + raw[-4:]})
        self.assertEqual(set(answer['secrets']), set(SECRET_NAMES))
        self.assertFalse(answer['secrets'][cloud.SECRET_VOLC_API_KEY]['set'])
        self.assertEqual(core.secrets.load(cloud.SECRET_DASHSCOPE_API_KEY), raw, 'stored (trimmed) for the engine')
        core.prefs_set({'engine': {'dashscope': {'region': 'intl'}}, 'sounds': {'enabled': False}})
        core._write_status()
        runtime = self.base / 'runtime'
        places = {
            'rpc answer': json.dumps(answer, ensure_ascii=False),
            'status': json.dumps(core.secret_status(), ensure_ascii=False),
            'snapshot': json.dumps(core.snapshot(), ensure_ascii=False, default=str),
            'events': json.dumps(self.events, ensure_ascii=False, default=str),
            'prefs.json': (runtime / 'prefs.json').read_text(encoding='utf-8'),
            'status.json': (runtime / 'status.json').read_text(encoding='utf-8'),
            'secrets.json': (runtime / 'secrets.json').read_text(encoding='utf-8'),
        }
        for where, text in places.items():
            self.assertNotIn(raw, text, where)
            self.assertNotIn(raw[3:], text, where)
        self.assertTrue(any(kind == 'secrets' for kind, _ in self.events), 'other views refresh')
        for method in core.methods():
            self.assertFalse(method.startswith('secret_') and method not in ('secret_set', 'secret_delete', 'secret_status'),
                             f'no way to read a key back: {method}')
        self.assertEqual(core.secret_delete(cloud.SECRET_DASHSCOPE_API_KEY)['secrets'][cloud.SECRET_DASHSCOPE_API_KEY],
                         {'set': False, 'masked': ''})
        self.assertIsNone(core.secrets.load(cloud.SECRET_DASHSCOPE_API_KEY))

    def test_the_rpc_stream_only_ever_carries_the_masked_key(self):
        import io
        from bridge.rpc import RpcServer
        core = self.core()
        raw = 'sk-STREAM' + uuid.uuid4().hex
        out = io.BytesIO()
        rpc = RpcServer(io.BytesIO(), out, core.methods())
        self.addCleanup(rpc.pool.shutdown, wait=True)
        core._emit = lambda kind, value: rpc.emit(kind, value)   # events share the stream, as in core_main
        for ident, method, params in ((1, 'secret_set', {'name': cloud.SECRET_LLM_API_KEY, 'value': raw}),
                                      (2, 'secret_status', {}), (3, 'snapshot', {}), (4, 'secret_get', {'name': cloud.SECRET_LLM_API_KEY})):
            rpc.handle_line(json.dumps({'id': ident, 'method': method, 'params': params}).encode('utf-8'))
        # Every answer, by its id (the snapshot itself carries "id": keys, so never just count them).
        wait_for(lambda: all(b'{"id":%d,' % ident in out.getvalue() for ident in (1, 2, 3, 4)), 5)
        stream = out.getvalue().decode('utf-8')
        self.assertNotIn(raw, stream)
        self.assertIn('••••' + raw[-4:], stream)
        self.assertIn('未知方法：secret_get', stream, 'there is no way to read a key back')

    def test_only_engine_keys_in_a_sane_format(self):
        core = self.core()
        self.assertEqual(set(SECRET_NAMES), {cloud.SECRET_VOLC_API_KEY, cloud.SECRET_VOLC_ACCESS_TOKEN,
                                             cloud.SECRET_DASHSCOPE_API_KEY, cloud.SECRET_LLM_API_KEY})
        for name in ('other.key', '../secrets', '', None):
            self.assertEqual(core.secret_set(name, 'value-1234'), {'error': '不支持的密钥名称'})
            self.assertEqual(core.secret_delete(name), {'error': '不支持的密钥名称'})
        for value in ('has space 123', '中文密钥', 'line\nbreak', 'x' * 2000):
            self.assertIn('error', core.secret_set(cloud.SECRET_LLM_API_KEY, value), value)
        self.assertIn('error', core.secret_set(cloud.SECRET_LLM_API_KEY, 12345))
        self.assertNotIn('error', core.secret_set(cloud.SECRET_LLM_API_KEY, 'llm-key-123456'))
        self.assertFalse(core.secret_set(cloud.SECRET_LLM_API_KEY, '   ')['secrets'][cloud.SECRET_LLM_API_KEY]['set'],
                         'an empty value deletes')


# -- controller tweaks ---------------------------------------------------------------------------
class ControllerEngineTests(unittest.TestCase):
    def control(self, backend, destination='input'):
        self.notes = []
        control = Controller(lambda *value: self.notes.append(value), backend, FakeCapture)
        self.addCleanup(control.close)
        control.set_destination(destination)
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        return control

    def kinds(self, kind):
        return [value for k, value in self.notes if k == kind]

    def test_an_external_app_is_never_cancelled_for_silence(self):
        backend = ExternalBackend('闪电说')
        control = self.control(backend)
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        for _ in range(10):
            control._audio(bytes(3200))   # 1 s that DotVoice hears as silence
        control.command('stop')
        wait_for(lambda: control.phase == 'idle' and self.kinds('result'))
        self.assertEqual(self.kinds('empty'), [])
        self.assertNotIn('cancel', backend.calls)
        result = self.kinds('result')[-1]
        self.assertEqual((result['text'], result['external'], result['app']), ('', True, '闪电说'))
        states = self.kinds('state')
        self.assertEqual(states[-1]['message'], '已交给闪电说')
        self.assertIn('正在唤起闪电说', [s['message'] for s in states])
        self.assertEqual(handed_to('Typeless'), '已交给 Typeless')

    def test_the_real_hotkey_bridge_taps_twice_through_silence(self):
        sent = []
        backend = HotkeyBackend(get_profile('typeless', done_ms=30), sender=lambda events: sent.extend(events),
                                sleep=lambda seconds: None)
        control = self.control(backend)
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        for _ in range(10):
            control._audio(bytes(3200))
        control.command('stop')
        wait_for(lambda: control.phase == 'idle' and self.kinds('result'))
        self.assertEqual(len(sent), 4, 'one tap to start, one to stop, no cancel')
        self.assertEqual(self.kinds('result')[-1]['app'], 'Typeless')

    def test_a_note_with_an_external_app_is_a_notice_not_a_failure(self):
        backend = ExternalBackend()
        control = self.control(backend, destination='notes')
        capture = control.capture
        control.command('start_note')
        wait_for(lambda: self.kinds('refused'))
        wait_for(lambda: control.phase == 'idle')
        self.assertEqual(self.kinds('refused')[-1]['message'], EXTERNAL_NOTES_NOTICE)
        self.assertNotIn('error', [s['phase'] for s in self.kinds('state')])
        self.assertNotIn('starting', [s['phase'] for s in self.kinds('state')], 'refused before a session shows')
        self.assertIs(control.capture, capture, 'the microphone stays open: no reconnect cycle')
        self.assertNotIn('close', backend.calls)
        self.assertEqual(backend.insert_seen, [], 'no key was sent to the app')
        control.set_destination('input')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        self.assertEqual(backend.insert_seen, [True])

    def test_an_engine_that_refuses_a_session_by_itself_is_not_a_failure_either(self):
        class Refusing(FakeBackend):
            def start(self):
                super().start()
                raise ExternalAppOnly('「闪电说」会把文字直接输入到光标处，不能保存为点声笔记；请切换到输入模式')
        backend = Refusing()
        control = self.control(backend)
        control.command('start')
        wait_for(lambda: self.kinds('refused'))
        wait_for(lambda: control.phase == 'idle')
        self.assertNotIn('error', [s['phase'] for s in self.kinds('state')])
        self.assertNotIn('close', backend.calls)

    def test_engine_notices_are_forwarded(self):
        backend = LabelledBackend()
        control = self.control(backend)
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        control.command('stop')
        wait_for(lambda: control.phase == 'processing')
        backend.incoming.put({'kind': 'notice', 'message': '润色服务响应超时（超过 20 秒）；已改用识别原文', 'source': 'cloud'})
        backend.incoming.put({'kind': 'notice', 'message': ''})
        backend.incoming.put({'kind': 'polishing', 'source': 'cloud'})
        wait_for(lambda: self.kinds('notice'))
        wait_for(lambda: control.message == '正在润色文字')
        backend.incoming.put({'type': 'result', 'text': '好的。', 'source': 'cloud'})
        wait_for(lambda: control.phase == 'idle')
        self.assertEqual(self.kinds('notice'), ['润色服务响应超时（超过 20 秒）；已改用识别原文'],
                         'an empty remark and the polishing step stay quiet')

    def test_status_text_names_the_engine(self):
        backend = LabelledBackend('豆包语音')
        control = self.control(backend)
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        control.command('stop')
        wait_for(lambda: control.phase == 'processing')
        messages = [s['message'] for s in self.kinds('state')]
        self.assertIn('正在连接豆包语音，开头音频会暂存于内存', messages)
        self.assertEqual(control.message, '豆包语音 正在识别并输入文字')
        capture = control.capture
        backend.incoming.put({'kind': 'error', 'source': 'cloud'})
        wait_for(lambda: control.phase == 'idle')
        self.assertEqual(control.message, '豆包语音 识别失败')
        self.assertIs(control.capture, capture, 'one failed recording: the pen stays armed')

        plain = self.control(FakeBackend())
        plain.command('start')
        wait_for(lambda: plain.phase == 'recording')
        plain.command('stop')
        wait_for(lambda: plain.phase == 'processing')
        self.assertEqual(plain.message, 'OpenLess 正在识别并输入文字', 'OpenLess wording unchanged')

    def test_a_cloud_engine_that_reaches_the_input_limit_types_what_it_heard(self):
        # Review 2026-10-14: ten minutes of dictation were thrown away although the built-in
        # engine already had the text. OpenLess keeps the old rule (test_field_fixes).
        backend = LabelledBackend()
        control = self.control(backend)
        with mock.patch('bridge.controller.INPUT_LIMIT', .3):
            control.command('start')
            wait_for(lambda: control.phase == 'recording')
            wait_for(lambda: control.phase == 'processing', timeout=3)
        self.assertIn('stop', backend.calls)
        self.assertNotIn('cancel', backend.calls)
        self.assertIn(CLOUD_LIMIT_NOTICE.format(minutes=0), self.kinds('notice'))
        backend.incoming.put({'type': 'result', 'text': '十分钟的话', 'source': 'cloud'})
        wait_for(lambda: control.phase == 'idle')
        self.assertEqual(self.kinds('result')[-1]['text'], '十分钟的话')

    def test_silence_with_a_cloud_engine_says_nothing_is_typed(self):
        backend = LabelledBackend()
        control = self.control(backend)
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        for _ in range(10):
            control._audio(bytes(3200))
        control.command('stop')
        wait_for(lambda: self.kinds('empty'))
        self.assertEqual(self.kinds('empty')[-1]['message'], '没有听到说话，本段已取消，不会输入文字')
        self.assertIn('cancel', backend.calls)


class CoreEngineEventTests(TempCase):
    def test_refusal_reaches_the_user_with_a_sound_and_an_external_result_is_not_empty(self):
        core = self.core(engine_factory=lambda kind, engine: ExternalBackend())
        core.prefs_set({'engine': {'kind': 'hotkey'}})
        control = core.controller
        self.arm(control)
        core.set_mode('notes')
        control.command('start')
        wait_for(lambda: EXTERNAL_NOTES_NOTICE in self.emitted('notice'))
        self.assertIn('error', self.sounds)
        self.assertNotIn('error', [s['phase'] for s in self.emitted('state')])
        core.set_mode('input')
        self.sounds.clear()
        self.record(control)
        wait_for(lambda: control.phase == 'idle' and self.emitted('result'))
        result = self.emitted('result')[-1]
        self.assertTrue(result['external'])
        self.assertNotIn('empty_text', result, 'handed over, not "nothing recognised"')
        self.assertIn('done_input', self.sounds)
        self.assertEqual(core.snapshot()['state']['message'], '已交给闪电说')


    def test_one_failed_cloud_recording_keeps_the_pen_armed(self):
        # Review 2026-10-14: a network hiccup in one recording closed the microphone, played the
        # error cue and re-armed 5 s later. Now only that recording ends, and it says why.
        backend = LabelledBackend()
        core = self.core(engine_factory=lambda kind, engine: backend)
        core.prefs_set({'engine': {'kind': 'volc'}})
        control = core.controller
        self.arm(control)
        capture = control.capture
        self.record(control, [SPEECH] * 3)
        self.sounds.clear()
        backend.incoming.put({'kind': 'error', 'message': '与豆包语音的连接中断，本段未完成', 'source': 'cloud'})
        wait_for(lambda: control.phase == 'idle')
        self.assertIs(control.capture, capture)
        self.assertTrue(control.want_armed)
        self.assertIn('与豆包语音的连接中断，本段未完成', self.emitted('notice'))
        self.assertEqual(self.emitted('session_failed')[-1], {'message': '与豆包语音的连接中断，本段未完成'})
        self.assertIn('error', self.sounds)
        self.assertNotIn('armed', self.sounds, 'no reconnect cycle')
        phases = [s['phase'] for s in self.emitted('state')]
        self.assertNotIn('error', phases)
        self.assertNotIn('disconnected', phases[phases.index('idle'):])
        self.assertIn('cancel', backend.calls)
        self.record(control, [SPEECH] * 3)   # the next press just works
        backend.incoming.put({'type': 'result', 'text': '好了', 'source': 'cloud'})
        wait_for(lambda: self.emitted('result'))
        # A failed test recording is shown in the wizard, where its result would be.
        core.wizard_test(True)
        self.record(control, [SPEECH] * 3)
        backend.incoming.put({'kind': 'error', 'message': '豆包语音鉴权失败，请检查 API Key', 'source': 'cloud'})
        wait_for(lambda: self.emitted('wizard_result'))
        self.assertEqual(self.emitted('wizard_result')[-1]['error'], '豆包语音鉴权失败，请检查 API Key')


# -- setup wizard ------------------------------------------------------------------------------------
class WizardTests(TempCase):
    def test_a_test_recording_types_nothing_saves_nothing_and_goes_to_the_wizard(self):
        backend = LabelledBackend()
        core = self.core(engine_factory=lambda kind, engine: backend)
        core.prefs_set({'engine': {'kind': 'volc'}})
        core.set_mode('notes')
        self.assertEqual(core.wizard_test(True), {'ok': True, 'enabled': True})
        control = core.controller
        self.arm(control)
        self.record(control, [SPEECH] * 3)
        self.assertEqual(backend.insert_seen, [False], 'nothing is typed at the cursor')
        self.assertIn('试说', control.message)
        backend.incoming.put({'type': 'result', 'text': '试一句', 'source': 'cloud'})
        wait_for(lambda: self.emitted('wizard_result'))
        wait_for(lambda: control.phase == 'idle')
        test = self.emitted('wizard_result')[-1]
        self.assertEqual((test['text'], test['test'], test['destination']), ('试一句', True, 'input'))
        self.assertEqual(self.emitted('result'), [], 'the voice page and capsule never see it')
        self.assertIsNone(core.snapshot()['result'])
        self.assertEqual(core.notes()['items'], [], 'not saved as a note in notes mode')
        self.assertNotIn('done_input', self.sounds, 'never the "typed at the cursor" chime for a test')
        self.assertEqual(control.message, '试说完成；文字没有输入到光标处')
        # Wizard closed: the next recording is a normal one again.
        core.wizard_test(False)
        self.record(control, [SPEECH] * 3)
        self.assertEqual(backend.insert_seen, [False, False], 'notes mode: a note, not typed')
        backend.incoming.put({'type': 'result', 'text': '正式笔记', 'source': 'cloud'})
        wait_for(lambda: self.emitted('result'))
        wait_for(lambda: core.notes()['items'])

    def test_an_external_app_types_into_the_wizards_own_box(self):
        backend = ExternalBackend('Typeless')
        core = self.core(engine_factory=lambda kind, engine: backend)
        core.prefs_set({'engine': {'kind': 'hotkey'}})
        core.set_mode('notes')
        core.wizard_test(True)
        control = core.controller
        self.arm(control)
        self.record(control)
        wait_for(lambda: self.emitted('wizard_result'))
        self.assertEqual(backend.insert_seen, [True], 'a test is an input session: the app may type')
        test = self.emitted('wizard_result')[-1]
        self.assertEqual((test['external'], test['app'], test['test']), (True, 'Typeless', True))

    def test_test_mode_lapses_by_itself_and_does_not_insert(self):
        backend = FakeBackend()
        control = Controller(lambda *value: None, backend, FakeCapture)
        self.addCleanup(control.close)
        control.test_mode(True)
        self.arm(control)
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        self.assertTrue(control.session_test)
        self.assertIn('start', backend.calls)
        self.assertFalse(backend.insert_text)
        control.test_mode(True, seconds=0.05)
        time.sleep(0.1)
        self.assertFalse(control._testing(), 'a wizard that vanished cannot keep recordings from being typed')

    def test_meter_reports_levels_while_paused_and_while_idle(self):
        RecordingCapture.opened = []
        notes = []
        control = Controller(lambda *value: notes.append(value), FakeBackend(), RecordingCapture)
        self.addCleanup(control.close)
        levels = lambda: [v for k, v in notes if k == 'level']
        control.command('start')   # paused, no wizard: the usual notice
        wait_for(lambda: any(k == 'notice' for k, _ in notes))
        notes.clear()
        control.meter(5)
        wait_for(lambda: RecordingCapture.opened)
        meter = RecordingCapture.opened[0]
        self.assertEqual(meter.callback, control._meter_audio, 'a capture only for the meter')
        self.assertEqual([v for k, v in notes if k == 'meter'], [{'ok': True, 'message': ''}])
        meter.callback(SPEECH)
        self.assertEqual(len(levels()), 1)
        self.assertGreater(levels()[0], 0.2)
        control.command('start')   # the wizard shows the key press itself: no "not ready" toast
        time.sleep(0.1)
        self.assertFalse(any(k == 'notice' for k, _ in notes))
        self.arm(control)
        self.assertTrue(meter.closed, 'the session capture takes over the microphone')
        session = control.capture
        self.assertIsNot(session, meter)
        control.last_meter_idle = 0
        control._audio(SPEECH)
        self.assertEqual(len(levels()), 2, 'idle audio is metered while the wizard asks')
        control.meter(0)
        control.last_meter_idle = 0
        control._audio(SPEECH)
        self.assertEqual(len(levels()), 2, 'and not otherwise')
        control.command('disarm')
        wait_for(lambda: control.phase == 'disconnected')
        time.sleep(0.3)
        self.assertEqual(len(RecordingCapture.opened), 2, 'no meter capture once the wizard stopped asking')

    def test_a_meter_that_cannot_open_never_touches_the_voice(self):
        notes = []

        def broken(callback, error, target=None):
            raise RuntimeError('未连接所选设备：倍思 BS-OH085')
        control = Controller(lambda *value: notes.append(value), FakeBackend(), broken)
        self.addCleanup(control.close)
        control.meter(5)
        wait_for(lambda: any(k == 'meter' for k, _ in notes))
        self.assertEqual([v for k, v in notes if k == 'meter'][0], {'ok': False, 'message': '未连接所选设备：倍思 BS-OH085'})
        self.assertEqual(control.phase, 'disconnected')
        self.assertFalse(control.want_armed)

    def test_hiding_the_window_ends_test_mode_and_the_meter(self):
        # Review 2026-10-14: the wizard left open while the window sat in the tray renewed test
        # mode forever - every dictation became a test (nothing typed) - and kept the pen
        # microphone open for its meter. Hidden = neither, whatever the page still renews.
        backend = LabelledBackend()
        core = self.core(engine_factory=lambda kind, engine: backend)
        core.prefs_set({'engine': {'kind': 'volc'}})
        control = core.controller
        core.wizard_test(True)
        core.mic_meter(20)
        self.assertTrue(control._testing() and control._metering())
        core.set_visible(False)
        self.assertFalse(control._testing(), 'hidden: dictation is typed again')
        self.assertFalse(control._metering(), 'hidden: the meter lets go of the microphone')
        # A page that keeps renewing while hidden cannot take them back.
        self.assertEqual(core.wizard_test(True), {'ok': True, 'enabled': False})
        core.mic_meter(20)
        self.assertFalse(control._testing() or control._metering())
        self.arm(control)
        self.record(control, [SPEECH] * 3)
        self.assertEqual(backend.insert_seen, [True])
        backend.incoming.put({'type': 'result', 'text': '正常输入', 'source': 'cloud'})
        wait_for(lambda: self.emitted('result'))
        self.assertEqual(self.emitted('wizard_result'), [])
        self.assertIn('done_input', self.sounds)

    def test_a_key_test_in_step_one_records_nothing_and_its_latch_is_no_lost_start(self):
        backend = FakeBackend()
        core = self.core(backend)
        control = core.controller
        self.arm(control)
        core.mic_meter(20)
        for code in (0x74, 0x00):   # step 1: "按一下 ⑥"
            core.on_hid(event(code))
        wait_for(lambda: core.snapshot()['last_key'] is not None)
        time.sleep(.15)
        self.assertNotIn('start', backend.calls, 'a key test: no session, no audio to any engine')
        self.assertNotIn('starting', [s['phase'] for s in self.emitted('state')])
        self.assertEqual(control.phase, 'idle')
        # Pressed again while the wizard still tests the keys: the latch goes off, no hint.
        for code in (0x75, 0x00):
            core.on_hid(event(code))
        time.sleep(.15)
        self.assertEqual(self.emitted('notice'), [])
        # Tested once more, then the wizard moved on: the next short press only switches the
        # microphone off - said as it is, never as a lost start signal.
        for code in (0x74, 0x00):
            core.on_hid(event(code))
        time.sleep(.1)
        core.mic_meter(0)
        for code in (0x75, 0x00):
            core.on_hid(event(code))
        wait_for(lambda: self.emitted('notice'))
        self.assertEqual(self.emitted('notice'), [TESTED_MIC_OFF_NOTICE])
        self.assertNotIn('start', backend.calls)
        for code in (0x74, 0x00):   # and the next press records as usual
            core.on_hid(event(code))
        wait_for(lambda: control.phase == 'recording')

    def test_wizard_rpcs(self):
        core = self.core()
        self.assertEqual(core.mic_meter(10), {'ok': True})
        self.assertGreater(core.controller.meter_until, time.monotonic())
        self.assertEqual(core.mic_meter(0), {'ok': True})
        for bad in ('10', 500, -1, True, None):
            self.assertIn('error', core.mic_meter(bad))
        self.assertEqual(core.wizard_test(False), {'ok': True, 'enabled': False})
        self.assertFalse(core.controller._testing())
        core.smoke = True
        self.assertIn('error', core.mic_meter(10), 'the smoke test never opens a microphone')
        self.assertIn('error', core.engine_test('volc'), 'nor the network')


# -- connection tests, detection, help links ---------------------------------------------------------
class ProbeAndDetectTests(TempCase):
    def test_connection_test_reports_inline(self):
        calls = []

        def probe(kind, engine):
            calls.append((kind, engine['kind']))
            if kind == 'polish':
                raise ValueError('boom')
            return True, f'{kind} 连接正常'
        core = self.core(probe_factory=probe)
        answer = core.engine_test('volc')
        self.assertEqual({k: answer[k] for k in ('kind', 'ok', 'message')}, {'kind': 'volc', 'ok': True, 'message': 'volc 连接正常'})
        self.assertIsInstance(answer['seconds'], float)
        self.assertEqual(calls, [('volc', 'openless')], 'tests the saved settings even while another engine runs')
        self.assertEqual(core.engine_test('polish')['message'], '测试没有完成（ValueError）')
        for kind in ('openless', 'hotkey', '', None):
            self.assertIn('error', core.engine_test(kind))

    def test_real_probes_without_keys_answer_at_once_without_connecting(self):
        core = self.core()
        with mock.patch('websocket.create_connection', side_effect=AssertionError('no network')), \
                mock.patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('no network')):
            volc = core.engine_test('volc')
            dash = core.engine_test('dashscope')
            polish = core.engine_test('polish')
        self.assertFalse(volc['ok'])
        self.assertIn('请填写', volc['message'])
        self.assertFalse(dash['ok'])
        self.assertIn('API Key', dash['message'])
        self.assertEqual((polish['ok'], polish['message']), (True, '原文模式不调用大模型'))
        core.prefs_set({'engine': {'polish': {'style': 'light', 'base_url': 'https://api.example.com/v1', 'model': 'm'}}})
        polish = core.engine_test('polish')
        self.assertEqual((polish['ok'], polish['message']), (False, '润色：请填写 API Key'))
        self.assertLess(max(volc['seconds'], dash['seconds']), 2)

    def test_detected_apps_come_first_with_the_pens_microphone(self):
        core = self.core()
        core.hotkey_scan = lambda: {
            'shandianshuo': {'installed': True, 'running': True, 'version': '0.7.8', 'supported': None},
            'wechat': {'installed': True, 'running': True, 'version': '3.9.12', 'supported': False},
            'wechat_handsfree': {'installed': True, 'running': True, 'version': '3.9.12', 'supported': False},
            'windows': {'installed': True, 'running': False, 'version': None, 'supported': True},
        }
        core._mic_name = lambda: '麦克风 (BaseusPresent)'
        found = core.hotkey_detect()
        apps = found['apps']
        self.assertEqual({app['profile'] for app in apps}, set(hotkey.PROFILES))
        self.assertEqual([app['profile'] for app in apps[:2]], ['shandianshuo', 'windows'], 'detected, built-in last')
        self.assertFalse(next(a for a in apps if a['profile'] == 'wechat')['detected'], 'too old for voice input')
        first = apps[0]
        self.assertEqual((first['running'], first['combo'], first['style'], first['cancel']), (True, 'Ctrl + Win', 'toggle', 'Esc'))
        self.assertIn('麦克风 (BaseusPresent)', first['mic_hint'])
        self.assertFalse(any('{mic}' in app['mic_hint'] for app in apps))
        self.assertIn('麦克风 (BaseusPresent)', found['custom_mic_hint'])
        self.assertNotIn('path', first, 'install paths are not needed by the UI')
        core.hotkey_scan = lambda: (_ for _ in ()).throw(OSError('registry'))
        self.assertEqual(len(core.hotkey_detect()['apps']), len(hotkey.PROFILES), 'a failed scan still lists the apps')

    def test_openless_status_is_read_only_and_never_carries_the_pin(self):
        backend = FakeBackend()
        folder = self.base / 'openless'
        folder.mkdir()
        backend.preferences_file = folder / 'preferences.json'
        backend.pin_file = folder / 'remote-input-pin.txt'
        core = self.core(backend)
        core.process_names = lambda: set()
        self.assertEqual(core.openless_status()['installed'], False)
        backend.preferences_file.write_text(json.dumps({'remoteInputEnabled': True, 'remoteInputPort': 9443}), encoding='utf-8')
        backend.pin_file.write_text('482913', encoding='utf-8')
        before = {p: p.read_bytes() for p in (backend.preferences_file, backend.pin_file)}
        core.process_names = lambda: {'openless.exe', 'explorer.exe'}
        status = core.openless_status()
        self.assertEqual({k: status[k] for k in ('installed', 'running', 'remote_input', 'paired', 'port', 'active')},
                         {'installed': True, 'running': True, 'remote_input': True, 'paired': True, 'port': 9443, 'active': True})
        self.assertNotIn('482913', json.dumps(status))
        backend.preferences_file.write_text(json.dumps({'remoteInputEnabled': False}), encoding='utf-8')
        self.assertFalse(core.openless_status()['remote_input'])
        backend.preferences_file.write_bytes(before[backend.preferences_file])
        self.assertEqual({p: p.read_bytes() for p in before}, before, 'OpenLess files untouched')

    def test_help_links_open_only_known_pages(self):
        core = self.core()
        with mock.patch('bridge.core.os.startfile') as start:
            for url in HELP_URLS:
                self.assertTrue(url.startswith('https://'))
                self.assertEqual(core.open_url(url), {'ok': True})
            self.assertEqual(core.open_url('https://github.com/openless/x'), {'ok': True})
            for url in ('https://example.com/', 'http://www.volcengine.com/docs/6561/1354869', HELP_URLS[0] + '?x=1', 7):
                self.assertIn('error', core.open_url(url))
        self.assertEqual(start.call_count, len(HELP_URLS) + 1)


if __name__ == '__main__':
    unittest.main()
