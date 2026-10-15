"""Release metadata only: no real network, download, microphone or device access."""
import copy
from http.client import IncompleteRead
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from bridge.updates import CHECK_INTERVAL, MAX_RESPONSE, RELEASE_API, REPOSITORY, UpdateWatch, _fetch_release


def release(version='0.11.5', tag=None):
    tag = tag or 'v' + version
    name = f'DotVoice-{version}-windows-x64.zip'
    return {'tag_name': tag, 'draft': False, 'prerelease': False,
            'html_url': f'{REPOSITORY}/releases/tag/{tag}',
            'assets': [{'name': name, 'browser_download_url': f'{REPOSITORY}/releases/download/{tag}/{name}'}]}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'updates.json'
        self.now = 1000
        self.calls = 0
        self.payload = release()
        self.failure = None

    def fetch(self):
        self.calls += 1
        if self.failure:
            raise self.failure
        return json.dumps(self.payload).encode()

    def watch(self, current='0.11.4', **kwargs):
        return UpdateWatch(self.path, current, fetch=kwargs.get('fetch', self.fetch), clock=lambda: self.now)

    def test_stable_release_exposes_only_the_exact_windows_download(self):
        watch = self.watch()
        self.assertEqual(watch.snapshot()['status'], 'idle')
        state = watch.check()
        self.assertEqual(state['status'], 'available')
        self.assertTrue(state['available'])
        self.assertFalse(state['dismissed'])
        self.assertEqual(state['download_url'], self.payload['assets'][0]['browser_download_url'])
        self.assertEqual(state['last_checked'], 1000)

    def test_semantic_version_order_and_no_downgrade(self):
        for current, latest, available in [('0.9.9', '0.11.5', True), ('0.11.5', '0.11.5', False),
                                            ('0.12.0', '0.11.5', False), ('1.0.0', '0.99.99', False)]:
            with self.subTest(current=current, latest=latest):
                self.payload = release(latest)
                state = self.watch(current).check(force=True)
                self.assertEqual(state['available'], available)
                self.assertEqual(state['status'], 'available' if available else 'current')

    def test_bare_numeric_release_tags_are_accepted(self):
        self.payload = release(tag='0.11.5')
        self.assertEqual(self.watch().check()['status'], 'available')

    def test_check_is_throttled_but_force_and_clock_rollback_work(self):
        watch = self.watch()
        watch.check()
        watch.check()
        self.assertEqual(self.calls, 1)
        self.assertFalse(watch.due())
        watch.check(force=True)
        self.assertEqual(self.calls, 2)
        self.now += CHECK_INTERVAL
        self.assertTrue(watch.due())
        watch.check()
        self.now -= 1
        self.assertTrue(watch.due(), 'clock corrections must not suppress checks indefinitely')

    def test_cache_survives_restart_and_is_compared_with_the_installed_version(self):
        self.watch().check()
        restarted = self.watch()
        self.assertTrue(restarted.snapshot()['available'])
        restarted.check()
        self.assertEqual(self.calls, 1)
        upgraded = self.watch('0.11.5')
        self.assertEqual(upgraded.snapshot()['status'], 'current')
        self.assertFalse(upgraded.snapshot()['available'])

    def test_dismiss_persists_for_only_one_version_and_never_hides_a_newer_one(self):
        watch = self.watch()
        watch.check()
        self.assertTrue(watch.dismiss('0.11.5')['dismissed'])
        self.assertTrue(self.watch().snapshot()['dismissed'])
        self.payload = release('0.12.0')
        state = watch.check(force=True)
        self.assertTrue(state['available'])
        self.assertFalse(state['dismissed'])
        with self.assertRaises(ValueError):
            watch.dismiss('0.11.5')
        with self.assertRaises(ValueError):
            self.watch('9.0.0').dismiss()

    def test_errors_keep_last_known_update_and_dismissal(self):
        watch = self.watch()
        watch.check()
        watch.dismiss()
        self.failure = URLError('offline')
        state = watch.check(force=True)
        self.assertEqual(state['status'], 'error')
        self.assertTrue(state['available'])
        self.assertTrue(state['dismissed'])
        self.assertIn('网络', state['error'])
        self.assertEqual(state['download_url'], self.payload['assets'][0]['browser_download_url'])
        self.assertEqual(self.watch().snapshot()['status'], 'error')

    def test_missing_public_release_and_rate_limits_are_not_reported_as_up_to_date(self):
        for code, phrase in [(404, '尚无公开发行版'), (403, '限制'), (429, '限制'), (500, '暂不可用')]:
            with self.subTest(code=code):
                self.failure = HTTPError(RELEASE_API, code, 'error', None, None)
                state = self.watch().check(force=True)
                self.assertEqual(state['status'], 'error')
                self.assertFalse(state['available'])
                self.assertIn(phrase, state['error'])

    def test_404_preserves_a_cached_available_release(self):
        watch = self.watch()
        watch.check()
        self.failure = HTTPError(RELEASE_API, 404, 'not found', None, None)
        state = watch.check(force=True)
        self.assertTrue(state['available'])
        self.assertEqual(state['status'], 'error')

    def test_recovered_network_clears_error(self):
        self.failure = TimeoutError()
        watch = self.watch()
        self.assertEqual(watch.check()['status'], 'error')
        self.failure = None
        state = watch.check(force=True)
        self.assertEqual(state['status'], 'available')
        self.assertEqual(state['error'], '')

    def test_incomplete_http_body_reports_a_connection_error(self):
        self.failure = IncompleteRead(b'{', 20)
        state = self.watch().check()
        self.assertEqual(state['status'], 'error')
        self.assertIn('网络', state['error'])

    def test_untrusted_release_urls_names_tags_and_flags_are_rejected(self):
        original = release()
        changes = [
            lambda d: d.update(draft=True), lambda d: d.update(prerelease=True),
            lambda d: d.update(tag_name='v0.12.0-beta'), lambda d: d.update(tag_name='v00.12.0'),
            lambda d: d.update(tag_name='../../evil'), lambda d: d.update(tag_name=None),
            lambda d: d.update(html_url='https://evil.example/releases/tag/v0.11.5'),
            lambda d: d['assets'][0].update(browser_download_url='https://github.com.evil.example/DotVoice.zip'),
            lambda d: d['assets'][0].update(browser_download_url=original['assets'][0]['browser_download_url'] + '?x=1'),
            lambda d: d['assets'][0].update(browser_download_url=original['assets'][0]['browser_download_url'].replace('v0.11.5/', 'v0.11.4/')),
            lambda d: d['assets'][0].update(name='DotVoice-latest.exe'),
            lambda d: d.update(assets=[]), lambda d: d.update(assets={}),
            lambda d: d['assets'].append(copy.deepcopy(d['assets'][0])),
        ]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                self.payload = copy.deepcopy(original)
                change(self.payload)
                state = self.watch().check(force=True)
                self.assertEqual(state['status'], 'error')
                self.assertFalse(state['available'])
                self.assertEqual(state['download_url'], '')

    def test_malformed_and_oversized_responses_are_rejected(self):
        for body in [b'[]', b'{', b'null', b'\xff', b'x' * (MAX_RESPONSE + 1),
                     b'[' * 2000 + b']' * 2000, 'not bytes']:
            with self.subTest(body=repr(body[:20])):
                state = self.watch(fetch=lambda: body).check(force=True)
                self.assertEqual(state['status'], 'error')
                self.assertFalse(state['available'])

    def test_invalid_cache_cannot_supply_a_download_url(self):
        bad = {'release': {'version': '0.11.5', 'tag': 'v0.11.5',
                           'download_url': 'https://evil.example/a.exe',
                           'release_url': REPOSITORY + '/releases/tag/v0.11.5'}, 'last_checked': self.now}
        for body in ['{', 'null', '[' * 2000 + ']' * 2000, json.dumps(bad)]:
            self.path.write_text(body)
            watch = self.watch()
            self.assertFalse(watch.snapshot()['available'])
            self.assertEqual(watch.snapshot()['download_url'], '')
            self.assertTrue(watch.due())

    def test_a_corrupt_huge_cached_timestamp_cannot_prevent_startup_or_checking(self):
        self.path.write_text(json.dumps({'last_checked': 10 ** 500}), encoding='utf-8')
        watch = self.watch()
        self.assertTrue(watch.due())
        self.assertEqual(watch.snapshot()['status'], 'idle')
        self.assertEqual(watch.check()['status'], 'available')

    def test_concurrent_checks_share_one_inflight_request(self):
        entered, finish = threading.Event(), threading.Event()

        def slow():
            entered.set()
            self.assertTrue(finish.wait(2))
            return self.fetch()

        watch = self.watch(fetch=slow)
        worker = threading.Thread(target=watch.check)
        worker.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(watch.due())
            state = watch.check(force=True)
            self.assertEqual(state['status'], 'checking')
        finally:
            finish.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.calls, 1)
        self.assertEqual(watch.snapshot()['status'], 'available')

    def test_cache_write_failure_does_not_discard_a_valid_update(self):
        watch = self.watch()
        with patch.object(Path, 'replace', side_effect=PermissionError()):
            state = watch.check()
        self.assertTrue(state['available'])
        self.assertEqual(state['status'], 'error')
        self.assertIn('未能保存', state['error'])
        self.assertFalse(watch.due(), 'a locked cache must not cause frequent network retries')

    def test_transport_uses_fixed_endpoint_timeout_and_bounded_read(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self, size):
                self.size = size
                return b'{}'

        response = Response()
        with patch('bridge.updates.urlopen', return_value=response) as open_url:
            self.assertEqual(_fetch_release(), b'{}')
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, RELEASE_API)
        self.assertEqual(open_url.call_args.kwargs['timeout'], 10)
        self.assertEqual(response.size, MAX_RESPONSE + 1)
        self.assertNotIn('Authorization', request.headers)


if __name__ == '__main__':
    unittest.main()
