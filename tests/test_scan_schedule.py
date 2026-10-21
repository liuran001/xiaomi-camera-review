import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from camera_review.catalog import Catalog


class ScanScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'recordings'
        self.source.mkdir()
        self.cat = Catalog(self.source, self.root / 'data', stable_seconds=180)
        self.watch = Mock()
        self.watch.poll.return_value = False
        self.factory = Mock(return_value=self.watch)

    def scheduler(self):
        from camera_review.scanning import ScanScheduler
        scheduler = ScanScheduler(self.cat, 45, watcher_factory=self.factory)
        self.addCleanup(scheduler.close)
        return scheduler

    def tick(self, scheduler, elapsed):
        with patch('camera_review.scanning.time.monotonic', return_value=1000 + elapsed), \
                patch('camera_review.scanning.time.time', return_value=10000 + elapsed):
            return scheduler.tick()

    def upload(self, size=10):
        path = self.source / '00_20261017090000_20261017090200.mp4'
        path.write_bytes(b'x' * size)
        os.utime(path, (100, 100))
        return path

    def test_idle_does_not_scan_or_change_last_scan(self):
        scheduler = self.scheduler()
        self.assertTrue(self.tick(scheduler, 0))
        last = self.cat.state('last_scan')
        with patch.object(self.cat, 'scan', side_effect=AssertionError('idle scan')):
            for elapsed in [1, 45, 90, 600, 86400]:
                self.assertFalse(self.tick(scheduler, elapsed))
        self.assertEqual(self.cat.state('last_scan'), last)

    def test_event_is_coalesced_then_stable_deadline_promotes_without_new_events(self):
        scheduler = self.scheduler()
        self.tick(scheduler, 0)
        self.upload()
        self.watch.poll.return_value = True
        self.assertFalse(self.tick(scheduler, 1))
        self.watch.poll.return_value = False
        self.assertTrue(self.tick(scheduler, 45))
        self.assertEqual(self.cat.clips()[0]['status'], 'unstable')
        self.assertFalse(self.tick(scheduler, 224))
        self.assertTrue(self.tick(scheduler, 225))
        self.assertEqual(self.cat.clips()[0]['status'], 'pending')
        self.assertFalse(self.tick(scheduler, 1000))

    def test_incomplete_stalled_upload_does_not_schedule_periodic_scans(self):
        self.cat.complete_file_bytes = 20
        path = self.upload()
        scheduler = self.scheduler()
        self.tick(scheduler, 0)
        self.assertFalse(self.tick(scheduler, 600))
        path.write_bytes(b'x' * 20)
        os.utime(path, (100, 100))
        self.watch.poll.return_value = True
        self.tick(scheduler, 601)
        self.watch.poll.return_value = False
        self.assertTrue(self.tick(scheduler, 781))
        self.assertEqual(self.cat.clips()[0]['status'], 'pending')

    def test_watcher_failure_scans_retries_and_resumes_event_mode(self):
        scheduler = self.scheduler()
        self.tick(scheduler, 0)
        self.watch.poll.side_effect = OSError('overflow')
        self.assertFalse(self.tick(scheduler, 1))
        self.watch.close.assert_called_once()
        self.watch.poll.side_effect = None
        self.assertTrue(self.tick(scheduler, 45))
        self.assertEqual(self.factory.call_count, 2)
        self.assertFalse(self.tick(scheduler, 90))

    def test_unavailable_notifications_retain_periodic_polling(self):
        self.factory.side_effect = NotImplementedError('unsupported')
        scheduler = self.scheduler()
        self.assertTrue(self.tick(scheduler, 0))
        self.assertFalse(self.tick(scheduler, 44))
        self.assertTrue(self.tick(scheduler, 45))

    def test_failed_scan_retries_even_without_events(self):
        scheduler = self.scheduler()
        with patch.object(self.cat, 'scan', side_effect=[False, True]) as scan:
            self.assertTrue(self.tick(scheduler, 0))
            self.assertFalse(self.tick(scheduler, 44))
            self.assertTrue(self.tick(scheduler, 45))
            self.assertEqual(scan.call_count, 2)


if __name__ == '__main__':
    unittest.main()
