"""OpenLess live-text reminder: waits for merge, then for a newer release, once."""
from pathlib import Path
import tempfile
import unittest

from bridge.upstream import INTERVAL, UpstreamWatch


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'watch.json'
        self.now = [1000.0]
        self.pr = {'state': 'open', 'merged_at': None}
        self.releases = []

    def fetch(self, url):
        return self.pr if '/pulls/' in url else self.releases

    def make(self):
        return UpstreamWatch(self.path, fetch=self.fetch, clock=lambda: self.now[0])

    def test_reminds_once_after_merge_and_a_newer_release(self):
        watch = self.make()
        self.assertTrue(watch.due())
        self.assertIsNone(watch.check())
        self.assertFalse(watch.due(), 'checks twice a day at most')
        self.now[0] += INTERVAL
        self.pr = {'state': 'closed', 'merged_at': '2026-10-10T00:00:00Z'}
        self.releases = [{'tag_name': 'v2.0.0-Beta.4', 'published_at': '2026-09-30T00:00:00Z'}]
        self.assertIsNone(watch.check(), 'merged but not released yet')
        self.now[0] += INTERVAL
        self.releases.append({'tag_name': 'v2.0.0-Beta.5', 'published_at': '2026-10-12T00:00:00Z',
                              'html_url': 'https://example.invalid/r'})
        reminder = watch.check()
        self.assertEqual(reminder['version'], 'v2.0.0-Beta.5')
        self.now[0] += INTERVAL
        self.assertFalse(self.make().due(), 'state survives a restart; never twice')

    def test_stops_when_closed_unmerged_or_already_supported(self):
        watch = self.make()
        self.pr = {'state': 'closed', 'merged_at': None}
        self.assertEqual(watch.check()['kind'], 'closed')
        self.assertTrue(watch.finished)
        other = UpstreamWatch(Path(self.path.parent) / 'other.json', fetch=self.fetch, clock=lambda: self.now[0])
        other.supported()
        self.assertFalse(other.due())

    def test_network_errors_are_quiet(self):
        def broken(url):
            raise OSError('offline')
        watch = UpstreamWatch(self.path, fetch=broken, clock=lambda: self.now[0])
        self.assertIsNone(watch.check())
        self.assertFalse(watch.finished)


if __name__ == '__main__':
    unittest.main()
