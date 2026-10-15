import json
from pathlib import Path
import tempfile
import unittest
from bridge.usage import UsageLedger


class UsageTests(unittest.TestCase):
    def setUp(self):
        # The system temp folder: nothing may land in the project's runtime/.
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.path = Path(self.temp.name) / 'usage.json'
        self.ledger = UsageLedger(self.path, lambda: self.now, lambda: self.now)

    def step(self, seconds, phase=None):
        self.now += seconds
        self.ledger.tick(phase)

    def test_only_observed_usage_counts_and_sleep_is_excluded(self):
        self.ledger.full()
        self.ledger.tick('idle')
        self.step(1, 'recording')
        self.step(2, 'processing')
        self.ledger.completed()
        self.step(3600, 'disconnected')
        self.ledger.low()
        cycle = self.ledger.snapshot()['cycles'][0]
        self.assertEqual(cycle['armed'], 3)
        self.assertEqual(cycle['recording'], 2)
        self.assertEqual(cycle['sessions'], 1)
        self.assertEqual(cycle['ended'] - cycle['started'], 3603)

    def test_recharge_archives_partial_not_complete(self):
        self.ledger.full()
        self.step(2)
        self.ledger.full()
        self.assertFalse(self.ledger.snapshot()['cycles'][0]['complete'])
        self.assertEqual(self.ledger.snapshot()['active']['armed'], 0)

    def test_restart_does_not_count_offline_gap(self):
        self.ledger.full()
        self.step(2, 'idle')
        self.ledger.save()
        self.now += 3600
        reopened = UsageLedger(self.path, lambda: self.now, lambda: self.now)
        self.assertEqual(reopened.snapshot()['active']['observed'], 2)
        self.assertEqual(reopened.phase, 'disconnected')

    def test_no_low_mark_without_full_and_no_transcripts(self):
        with self.assertRaises(ValueError):
            self.ledger.low()
        self.ledger.full()
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(set(saved['active']), {'started','observed','armed','recording','sessions'})

    def test_invalid_file_preserved(self):
        self.path.write_text('broken', encoding='utf-8')
        reopened = UsageLedger(self.path, lambda: self.now, lambda: self.now)
        reopened.full()
        self.assertTrue(reopened.warning)
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'broken')


if __name__ == '__main__':
    unittest.main()
