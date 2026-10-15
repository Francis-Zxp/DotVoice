"""Tidepool observer tests use temporary files and fake port ownership only."""
import json
from pathlib import Path
import tempfile
import importlib.util
import os
import threading
import time
import unittest
from unittest.mock import patch

from bridge.tidepool import TidepoolAdapter, matches_receiver


class TidepoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        receiver = self.root / "receiver"
        receiver.mkdir()
        self.source = receiver / "tidepool_receiver.py"
        self.source.write_text('VERSION = "4.0.5"\n', encoding="utf-8")
        (self.root / "config.json").write_text(json.dumps({"port": 17876}), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def owner(self, **changes):
        result = {"pid": 123, "exe": r"D:\python\python.exe",
                  "command": f'"D:\\python\\python.exe" -u "{self.source}" --port 17876'}
        result.update(changes)
        return result

    def test_live_receiver_does_not_mean_device_online(self):
        adapter = TidepoolAdapter(self.root, lambda port: [self.owner()])
        snapshot = adapter.snapshot()
        self.assertTrue(snapshot["receiver_running"])
        self.assertEqual(snapshot["receiver_pid"], 123)
        self.assertEqual(snapshot["receiver_version"], "4.0.5")
        self.assertIsNone(snapshot["device_online"])
        self.assertIsNone(snapshot["battery_percent"])
        self.assertIsNone(snapshot["recording"])
        self.assertFalse(snapshot["control_supported"])

    def test_other_process_on_port_is_conflict(self):
        adapter = TidepoolAdapter(self.root, lambda port: [self.owner(command="unrelated.py")])
        snapshot = adapter.snapshot()
        self.assertFalse(snapshot["receiver_running"])
        self.assertTrue(snapshot["port_conflict"])

    def test_path_prefix_and_nonpython_process_rejected(self):
        self.assertFalse(matches_receiver(self.owner(command=str(self.source) + ".other"), self.source))
        self.assertFalse(matches_receiver(self.owner(exe="other.exe"), self.source))

    def test_missing_installation_never_probes_ports(self):
        def forbidden(port):
            self.fail("must not probe an unconfigured receiver")
        snapshot = TidepoolAdapter(self.root / "missing", forbidden).snapshot()
        self.assertFalse(snapshot["installed"])
        self.assertIsNone(snapshot["receiver_running"])

    def test_probe_failure_remains_unknown(self):
        def failing(port):
            raise OSError("fake denied")
        snapshot = TidepoolAdapter(self.root, failing).snapshot()
        self.assertIsNone(snapshot["receiver_running"])

    def test_control_cannot_write_debug_command_or_open_route(self):
        adapter = TidepoolAdapter(self.root, lambda port: [])
        before = sorted(str(path) for path in self.root.rglob("*"))
        for action in ("start", "stop", "wake", "pause"):
            with self.assertRaises(RuntimeError):
                adapter.control(action)
        self.assertEqual(before, sorted(str(path) for path in self.root.rglob("*")))

    def test_live_api_snapshot_does_not_probe_or_invent_battery(self):
        def no_probe(port):
            self.fail("live API does not need process enumeration")
        live = {"ok": True, "fresh": True, "control_supported": True, "enabled": False,
                "recording": False, "device_online": True, "usb_connected": True,
                "link": "usb", "battery_percent": None, "receiver_running": True}
        snapshot = TidepoolAdapter(self.root, no_probe, lambda *args: live).snapshot()
        self.assertTrue(snapshot["control_supported"])
        self.assertFalse(snapshot["enabled"])
        self.assertFalse(snapshot["api_upgrade_required"])
        self.assertIsNone(snapshot["battery_percent"])

    def test_live_api_passes_reported_battery_through(self):
        live = {"ok": True, "fresh": True, "control_supported": True, "device_online": True,
                "battery_percent": 64, "battery_charging": True, "battery_reported_at": 1760000000.5}
        snapshot = TidepoolAdapter(self.root, lambda port: [], lambda *args: live).snapshot()
        self.assertEqual((snapshot["battery_percent"], snapshot["battery_charging"],
                          snapshot["battery_reported_at"]), (64, True, 1760000000.5))

    def test_pause_waits_for_its_cancel_generation(self):
        states = iter([
            {"fresh": True, "control_supported": True},
            {"ok": True, "cancel_generation": 2},
            {"fresh": True, "enabled": False, "cancel_pending": False, "cancel_generation": 1},
            {"fresh": True, "enabled": False, "cancel_pending": False, "cancel_generation": 2},
        ])
        seen = []
        def request(*args):
            seen.append(args)
            return next(states)
        result = TidepoolAdapter(self.root, request=request).control("pause")
        self.assertFalse(result["cancel_pending"])
        self.assertEqual(len(seen), 4)

    def pause_adapter(self, enabled=True):
        state = dict(ok=True, fresh=True, control_supported=True, receiver_pid=123,
                     enabled=enabled, cancel_pending=False, cancel_generation=0, cancel_error=None)
        actions = []
        def request(method, route, payload=None):
            if method == 'POST':
                action = payload['action']
                actions.append(action)
                state['enabled'] = action == 'resume'
                if action == 'pause':
                    state['cancel_generation'] += 1
            return dict(state)
        return TidepoolAdapter(self.root, request=request), state, actions

    def test_temporary_pause_is_restored_even_after_repeated_arm(self):
        adapter, state, actions = self.pause_adapter()
        adapter.control('pause', temporary=True)
        adapter.control('pause', temporary=True)
        self.assertFalse(state['enabled'])
        self.assertTrue(adapter.restore_presenter_pause())
        self.assertTrue(state['enabled'])
        self.assertFalse(adapter.restore_presenter_pause())
        self.assertEqual(actions, ['pause', 'pause', 'resume'])

    def test_existing_manual_pause_is_not_restored(self):
        adapter, state, actions = self.pause_adapter(enabled=False)
        adapter.control('pause', temporary=True)
        self.assertFalse(adapter.restore_presenter_pause())
        self.assertFalse(state['enabled'])
        self.assertEqual(actions, ['pause'])

    def test_manual_pause_after_handoff_takes_precedence(self):
        adapter, state, actions = self.pause_adapter()
        adapter.control('pause', temporary=True)
        adapter.control('pause')
        self.assertFalse(adapter.restore_presenter_pause())
        self.assertFalse(state['enabled'])
        self.assertNotIn('resume', actions)

    def test_changed_receiver_or_pause_generation_is_not_restored(self):
        for change in ({'receiver_pid': 456}, {'cancel_generation': 99},
                       {'fresh': False}, {'cancel_pending': True}, {'cancel_error': 'failed'}):
            with self.subTest(change=change):
                adapter, state, actions = self.pause_adapter()
                adapter.control('pause', temporary=True)
                state.update(change)
                self.assertFalse(adapter.restore_presenter_pause())
                self.assertNotIn('resume', actions)

    def test_core_exit_restores_only_after_presenter_resources_are_released(self):
        try:
            from .core_fakes import make_core
            from .test_bridge import wait_for
        except ImportError:
            from core_fakes import make_core
            from test_bridge import wait_for
        adapter, state, actions = self.pause_adapter()
        core = make_core(self.root / 'desktop')
        self.addCleanup(core.close)
        core.prefs.update({'devices': {'coexist': False}})  # the exclusive handoff
        core.tidepool = adapter
        core.tidepool_state = {'control_supported': True}
        request = adapter.request
        def checked_request(method, route, payload=None):
            if payload and payload.get('action') == 'resume':
                self.assertTrue(core.controller.shutdown_released)
                self.assertIsNone(core.controller.capture)
                self.assertIsNone(core.controller.hardware)
            return request(method, route, payload)
        adapter.request = checked_request
        self.assertTrue(core.action('arm')['ok'])
        wait_for(lambda: core.controller.phase == 'idle')
        self.assertFalse(state['enabled'])
        core.close()
        self.assertTrue(state['enabled'])
        self.assertEqual(actions, ['pause', 'resume'])
        self.assertIn('error', core.action('arm'))

    def test_coexist_arm_leaves_tidepool_running_and_turning_it_on_ends_an_old_pause(self):
        try:
            from .core_fakes import make_core
            from .test_bridge import wait_for
        except ImportError:
            from core_fakes import make_core
            from test_bridge import wait_for
        adapter, state, actions = self.pause_adapter()
        core = make_core(self.root / 'desktop')
        self.addCleanup(core.close)
        core.tidepool = adapter
        core.tidepool_state = {'control_supported': True}
        self.assertTrue(core.prefs.get('devices', 'coexist'), '随拿随用 is the default')
        self.assertTrue(core.action('arm')['ok'])
        wait_for(lambda: core.controller.phase == 'idle')
        self.assertEqual(actions, [], 'Tidepool keeps running')
        core.action('disarm')
        wait_for(lambda: core.controller.phase == 'disconnected')
        # An exclusive arm paused Tidepool; switching coexist on hands it back at once.
        core.prefs_set({'devices': {'coexist': False}})
        core.action('arm')
        self.assertFalse(state['enabled'])
        core.prefs_set({'devices': {'coexist': True}})
        wait_for(lambda: state['enabled'])
        self.assertEqual(actions, ['pause', 'resume'])
        self.assertTrue(core.controller.want_armed, 'the pen stays ready')

    def test_core_exit_does_not_resume_after_backend_release_failure(self):
        try:
            from .core_fakes import make_core
        except ImportError:
            from core_fakes import make_core
        adapter, state, actions = self.pause_adapter()
        adapter.control('pause', temporary=True)
        core = make_core(self.root / 'desktop')
        self.addCleanup(core.close)
        core.tidepool = adapter
        with patch.object(core.controller.backend, 'close', side_effect=RuntimeError('busy')):
            core.close()
        self.assertFalse(core.controller.shutdown_released)
        self.assertFalse(state['enabled'])
        self.assertNotIn('resume', actions)

    def test_cancel_error_prevents_success(self):
        states = iter([
            {"fresh": True, "control_supported": True},
            {"ok": True, "cancel_generation": 1},
            {"fresh": True, "cancel_error": "fake disconnected"},
        ])
        adapter = TidepoolAdapter(self.root, request=lambda *args: next(states))
        with self.assertRaises(RuntimeError):
            adapter.control("pause")

    def test_resume_waits_for_updated_enabled_state(self):
        states = iter([
            {"fresh": True, "control_supported": True, "enabled": False},
            {"ok": True, "enabled": True, "cancel_pending": False},
            {"fresh": True, "enabled": False, "cancel_pending": False},
            {"fresh": True, "enabled": True, "cancel_pending": False},
        ])
        seen = []
        def request(*args):
            seen.append(args)
            return next(states)
        result = TidepoolAdapter(self.root, request=request).control("resume")
        self.assertTrue(result["enabled"])
        self.assertFalse(result["cancel_pending"])
        self.assertEqual(len(seen), 4)

    def test_resume_does_not_accept_stale_or_cancelling_state(self):
        states = iter([
            {"fresh": True, "control_supported": True},
            {"ok": True, "enabled": True},
            {"fresh": False, "enabled": True, "cancel_pending": False},
            {"fresh": True, "enabled": True, "cancel_pending": True},
            {"fresh": True, "enabled": True, "cancel_pending": False},
        ])
        seen = []
        def request(*args):
            seen.append(args)
            return next(states)
        result = TidepoolAdapter(self.root, request=request).control("resume")
        self.assertTrue(result["ok"])
        self.assertEqual(len(seen), 5)

    def test_resume_confirmation_has_bounded_wait(self):
        seen = []
        def request(method, *args):
            seen.append(method)
            if method == "POST":
                return {"ok": True, "enabled": True}
            return {"fresh": True, "control_supported": True, "enabled": False,
                    "cancel_pending": False}
        adapter = TidepoolAdapter(self.root, request=request)
        with patch("bridge.tidepool.time.monotonic", side_effect=[0, 0.1, 6]), \
                patch("bridge.tidepool.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "新状态确认"):
                adapter.control("resume")
        self.assertEqual(seen, ["GET", "POST", "GET"])

    def test_authenticated_http_roundtrip_without_device_or_live_receiver(self):
        project = Path(__file__).resolve().parents[1]
        path = project / "integrations" / "tidepool" / "receiver" / "tidepool_control.py"
        if not path.is_file():
            path = project.parent / "Tidepool OS" / "v4.0.5" / "source" / "pc" / "tidepool_control.py"
        if not path.is_file():
            self.skipTest("Tidepool receiver integration source is not included")
        spec = importlib.util.spec_from_file_location("tidepool_control_http_test", path)
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        server = api.ControlServer(self.root / "control" / "endpoint.json")
        state = {"receiver_pid": os.getpid(), "receiver_running": True, "control_supported": True,
                 "enabled": True, "cancel_pending": False, "cancel_generation": 0,
                 "device_online": False, "usb_connected": False, "battery_percent": None}
        server.publish(state)
        server.start()
        stopped = threading.Event()
        def apply(request):
            state["enabled"] = request.action != "pause"
            state["cancel_generation"] += 1
            server.publish(state)
            request.finish(True, enabled=state["enabled"], cancel_pending=False,
                           cancel_generation=state["cancel_generation"])
        def pump():
            while not stopped.wait(0.01):
                server.drain(apply)
                server.publish(state)
        thread = threading.Thread(target=pump)
        thread.start()
        try:
            adapter = TidepoolAdapter(self.root)
            self.assertTrue(adapter.snapshot()["control_supported"])
            self.assertFalse(adapter.control("pause")["enabled"])
            self.assertTrue(adapter.control("resume")["enabled"])
        finally:
            stopped.set()
            thread.join(1)
            server.close()
        self.assertFalse((self.root / "control" / "endpoint.json").exists())


if __name__ == "__main__":
    unittest.main()
