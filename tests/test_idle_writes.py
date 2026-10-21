import os
import sqlite3
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from camera_review.catalog import Catalog
from camera_review.worker import Worker, main


class IdleWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'recordings'
        self.source.mkdir()
        self.cat = Catalog(self.source, self.root / 'data', stable_seconds=0)

    def recording(self):
        path = self.source / '00_20261017090000_20261017090200.mp4'
        path.write_bytes(b'0123456789')
        os.utime(path, (100, 100))
        self.cat.scan()
        self.cat.scan()
        return self.cat.clips()[0]

    def test_repeated_state_value_does_not_commit_an_update(self):
        self.cat.set_state('scan_error', None)
        with closing(sqlite3.connect(self.cat.db)) as observer:
            version = observer.execute('PRAGMA data_version').fetchone()[0]
            self.cat.set_state('scan_error', None)
            self.assertEqual(observer.execute('PRAGMA data_version').fetchone()[0], version)
            self.cat.set_state('scan_error', 'source unavailable')
            self.assertNotEqual(observer.execute('PRAGMA data_version').fetchone()[0], version)
        self.assertEqual(self.cat.state('scan_error'), 'source unavailable')

    def test_scan_preserves_unchanged_complete_record_and_events(self):
        clip = self.recording()
        self.cat.publish_chunk(clip['id'], clip['generation'], 120, [(3, .8, None)])
        before = self.cat.clip(clip['id'])
        events = self.cat.events()
        for _ in range(3):
            self.cat.scan()
        self.assertEqual(self.cat.clip(clip['id']), before)
        self.assertEqual(self.cat.events(), events)

    def test_stalled_upload_stops_updating_after_second_observation(self):
        self.cat.complete_file_bytes = 20
        clip = self.recording()
        self.assertEqual(clip['status'], 'unstable')
        for _ in range(3):
            self.cat.scan()
        self.assertEqual(self.cat.clip(clip['id']), clip)
        self.assertIsNone(self.cat.next_analysis())

    def test_heartbeat_is_limited_to_once_per_thirty_seconds(self):
        worker = Worker(self.cat)
        for elapsed in [0, 1, 10, 29.9, 30, 31]:
            with patch('camera_review.worker.time.monotonic', return_value=100 + elapsed), \
                    patch('camera_review.worker.time.time', return_value=1000 + elapsed):
                worker.heartbeat()
            expected = 1000 if elapsed < 30 else 1030
            self.assertEqual(self.cat.state('heartbeat'), expected)

    def test_failed_heartbeat_can_retry_without_waiting(self):
        worker = Worker(self.cat)
        original = self.cat.set_state
        with patch('camera_review.worker.time.monotonic', return_value=100):
            with patch.object(self.cat, 'set_state', side_effect=OSError('disk unavailable')):
                with self.assertRaises(OSError):
                    worker.heartbeat()
            with patch.object(self.cat, 'set_state', wraps=original) as write:
                worker.heartbeat()
                write.assert_called_once()
        self.assertIsNotNone(self.cat.state('heartbeat'))

    def test_concurrent_heartbeats_commit_only_once(self):
        worker = Worker(self.cat)
        barrier = threading.Barrier(8)
        original = self.cat.set_state

        def slow_write(*args):
            time.sleep(.02)
            return original(*args)

        def report(_index):
            barrier.wait(timeout=5)
            worker.heartbeat()

        with patch.object(self.cat, 'set_state', side_effect=slow_write) as write:
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(report, range(8)))
            write.assert_called_once()
        self.assertIsNotNone(self.cat.state('heartbeat'))

    def test_scan_preserves_legacy_observation_counts(self):
        clip = self.recording()
        self.cat.stable_seconds = 180
        for status in ['complete', 'unstable']:
            with self.subTest(status=status):
                with self.cat.connect() as db:
                    db.execute('UPDATE clips SET observations=500,status=?,stable_since=? WHERE id=?',
                               (status, time.time(), clip['id']))
                before = self.cat.clip(clip['id'])
                self.cat.scan()
                self.assertEqual(self.cat.clip(clip['id']), before)

    def test_main_keeps_wal_between_idle_iterations_and_closes_on_exit(self):
        wal = Path(str(self.cat.db) + '-wal')
        samples = []

        def idle_sleep(_seconds):
            samples.append(wal.stat().st_ino if wal.exists() else None)
            if len(samples) == 2:
                raise KeyboardInterrupt()

        with patch('camera_review.worker.catalog_from_env', return_value=self.cat), \
                patch('camera_review.worker.time.sleep', side_effect=idle_sleep):
            with self.assertRaises(KeyboardInterrupt):
                main()
        self.assertIsNotNone(samples[0], 'idle polling must keep the WAL open')
        self.assertEqual(samples[0], samples[1], 'idle polling must not recreate the WAL')
        self.assertFalse(wal.exists(), 'the keeper connection must close on shutdown')

    def test_idle_worker_only_checks_shared_database_version(self):
        with self.cat.connect() as monitor:
            worker = Worker(self.cat, monitor=monitor)
            with patch.object(worker.scanner, 'tick', return_value=False):
                self.assertFalse(worker.step())
                # Heartbeat's first write may advance the monitor version once.
                self.assertFalse(worker.step())
                with patch.object(self.cat, 'connect', side_effect=AssertionError('idle catalog read')):
                    for _ in range(5):
                        self.assertFalse(worker.step())

    def test_external_commit_wakes_idle_worker_without_waiting_for_scan(self):
        with self.cat.connect() as monitor:
            worker = Worker(self.cat, monitor=monitor)
            with patch.object(worker.scanner, 'tick', return_value=False):
                self.assertFalse(worker.step())
                self.assertFalse(worker.step())
                self.cat.set_state('paused', True)
                with patch.object(self.cat, 'claim_preview', wraps=self.cat.claim_preview) as claim:
                    self.assertFalse(worker.step())
                    claim.assert_called_once()

    def test_ram_heartbeat_leaves_database_state_unchanged(self):
        from camera_review.heartbeat import read_heartbeat
        heartbeat = self.root / 'heartbeat'
        with patch.dict(os.environ, {'HEARTBEAT_FILE': str(heartbeat)}):
            worker = Worker(self.cat)
        with patch.object(self.cat, 'set_state', side_effect=AssertionError('database heartbeat')):
            worker.heartbeat()
        self.assertIsNotNone(read_heartbeat(heartbeat))
        self.assertIsNone(self.cat.state('heartbeat'))

    def test_idle_retry_deadline_wakes_without_external_commit(self):
        clip = self.recording()
        with self.cat.connect() as db:
            db.execute("UPDATE clips SET thumbnail='cover.jpg',retry_at=1100 WHERE id=?", (clip['id'],))
        with self.cat.connect() as monitor:
            worker = Worker(self.cat, detector=object(), monitor=monitor)
            with patch.object(worker.scanner, 'tick', return_value=False), \
                    patch.object(worker, 'heartbeat'), patch.object(worker, '_work', return_value=False) as work:
                for monotonic, wall, calls in [(100, 1000, 1), (150, 1050, 1), (200, 1100, 2)]:
                    with patch('camera_review.worker.time.monotonic', return_value=monotonic), \
                            patch('camera_review.worker.time.time', return_value=wall):
                        self.assertFalse(worker.step())
                    self.assertEqual(work.call_count, calls)

    def test_commit_during_queue_check_is_not_hidden_by_idle_snapshot(self):
        with self.cat.connect() as monitor:
            worker = Worker(self.cat, monitor=monitor)
            def raced_work():
                self.cat.set_state('paused', True)
                return False
            with patch.object(worker.scanner, 'tick', return_value=False), \
                    patch.object(worker, 'heartbeat'), patch.object(worker, '_work', side_effect=raced_work) as work:
                self.assertFalse(worker.step())
                self.assertFalse(worker.step())
                self.assertEqual(work.call_count, 2)


if __name__ == '__main__':
    unittest.main()
