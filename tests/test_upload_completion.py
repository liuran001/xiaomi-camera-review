import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from camera_review.catalog import Catalog, catalog_from_env


class UploadCompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'recordings'
        self.source.mkdir()
        self.file = self.source / '00_20261017090000_20261017090200.mp4'
        self.file.write_bytes(b'0123456789')
        os.utime(self.file, (100, 100))
        self.cat = Catalog(self.source, self.root / 'data')
        # Policy configured independently so RED demonstrates behavior, not signature errors.
        self.cat.complete_file_bytes = 10

    def scan(self, now):
        with patch('camera_review.catalog.time.time', return_value=now):
            self.cat.scan()
        return self.cat.clips()[0]

    def test_old_mtime_requires_observed_window_then_worker_selection(self):
        first = self.scan(1000)
        second = self.scan(1045)
        self.assertEqual(second['status'], 'unstable')
        self.assertEqual(second['stable_since'], 1000)
        self.assertIsNone(self.cat.next_analysis())
        self.assertEqual(self.scan(1179)['status'], 'unstable')
        ready = self.scan(1180)
        self.assertEqual(ready['status'], 'pending')
        self.assertEqual(ready['generation'], first['generation'])
        self.assertEqual(self.cat.next_analysis()['id'], ready['id'])

    def test_paused_under_and_oversized_files_never_promote(self):
        for payload in (b'123', b'12345678901'):
            self.file.write_bytes(payload)
            os.utime(self.file, (100, 100))
            self.scan(1000)
            clip = self.scan(2000)
            self.assertEqual(clip['status'], 'unstable')
            self.assertIsNone(self.cat.next_analysis())

    def test_growth_resets_window_generation_and_prior_results(self):
        self.scan(1000)
        ready = self.scan(1180)
        self.cat.publish_chunk(ready['id'], ready['generation'], 60, [(3, .8, None)])
        self.cat.enqueue_preview(ready['id'], 0)
        self.file.write_bytes(b'123')
        os.utime(self.file, (100, 100))
        changed = self.scan(1200)
        self.assertNotEqual(changed['generation'], ready['generation'])
        self.assertEqual(changed['completed'], 0)
        self.assertEqual(self.cat.events(), [])
        self.file.write_bytes(b'0123456789')
        os.utime(self.file, (100, 100))
        full = self.scan(1245)
        self.assertNotEqual(full['generation'], changed['generation'])
        self.assertEqual(self.scan(1290)['status'], 'unstable')
        self.assertEqual(self.scan(1425)['status'], 'pending')

    def test_upgrade_preserves_complete_but_restarts_unstable_window(self):
        self.cat.stable_seconds = 0
        self.scan(1000)
        ready = self.scan(1001)
        self.cat.publish_chunk(ready['id'], ready['generation'], 120, [(3, .8, None)])
        with self.cat.connect() as db:
            db.execute('ALTER TABLE clips DROP COLUMN stable_since')
        for _ in range(2):
            self.cat = Catalog(self.source, self.root / 'data', complete_file_bytes=10)
            self.assertEqual(self.cat.clip(ready['id'])['completed'], 120)
        self.scan(2000)
        result = self.scan(2045)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['completed'], 120)
        self.assertEqual(result['generation'], ready['generation'])
        self.assertEqual(len(self.cat.events()), 1)
        with self.cat.connect() as db:
            db.execute("UPDATE clips SET status='unstable'")
            db.execute('ALTER TABLE clips DROP COLUMN stable_since')
        self.cat = Catalog(self.source, self.root / 'data', complete_file_bytes=10)
        self.scan(3000)
        self.assertEqual(self.scan(3045)['status'], 'unstable')
        self.assertEqual(self.scan(3180)['status'], 'pending')

    def test_strict_config_blocks_existing_wrong_size_before_scan_and_invalidates(self):
        self.cat.stable_seconds = 0
        self.cat.complete_file_bytes = 0
        self.scan(1000)
        ready = self.scan(1001)
        self.cat.publish_chunk(ready['id'], ready['generation'], 60, [(3, .8, None)])
        self.cat.complete_file_bytes = 11
        with self.assertRaises(RuntimeError):
            self.cat.current_source(ready)
        with self.assertRaises(RuntimeError):
            self.cat.enqueue_preview(ready['id'], 0)
        self.assertFalse(self.cat.valid_generation(ready['id'], ready['generation']))
        invalid = self.scan(2000)
        self.assertEqual(invalid['status'], 'unstable')
        self.assertEqual(invalid['completed'], 0)
        self.assertEqual(self.cat.events(), [])
        self.assertNotEqual(invalid['generation'], ready['generation'])

    def test_reappearance_restarts_window_even_with_identical_stat(self):
        self.scan(1000)
        original = self.scan(1180)
        content = self.file.read_bytes()
        self.file.unlink()
        with patch('camera_review.catalog.time.time', return_value=1200):
            self.cat.scan()
        self.file.write_bytes(content)
        os.utime(self.file, (100, 100))
        returned = self.scan(1300)
        self.assertNotEqual(returned['generation'], original['generation'])
        self.assertEqual(self.scan(1345)['status'], 'unstable')
        self.assertEqual(self.scan(1480)['status'], 'pending')

    def test_environment_configures_expected_size(self):
        with patch.dict(os.environ, {'RECORDINGS_ROOT': str(self.source), 'DATA_ROOT': str(self.root / 'env'), 'COMPLETE_FILE_BYTES': '10'}):
            cat = catalog_from_env()
        self.assertEqual(cat.complete_file_bytes, 10)
