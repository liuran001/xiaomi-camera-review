import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from camera_review.catalog import Catalog
from camera_review.media import MediaCancelled
from camera_review.worker import Worker


class NoPerson:
    def score(self, frame):
        return 0


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg unavailable')
class MotionWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = self.root / 'recordings'
        source.mkdir()
        self.file = source / '00_20261017090000_20261017090100.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=gray:size=640x360:rate=6',
                        '-t', '60', '-c:v', 'libx264', '-threads', '1', '-g', '6', str(self.file)], check=True)
        os.utime(self.file, (time.time() - 300, time.time() - 300))
        self.cat = Catalog(source, self.root / 'data', stable_seconds=0)
        self.cat.scan(); self.cat.scan()
        self.clip = self.cat.clips()[0]
        self.cat.publish_media_duration(self.clip, 60)

    def worker(self, enabled=True, detector=None):
        with patch.dict(os.environ, {'EMPTY_SCENE_GATE': '1' if enabled else '0'}):
            worker = Worker(self.cat, detector or NoPerson())
        worker.cover_turn = False
        return worker

    def step(self, worker):
        with patch('camera_review.worker.ensure_seek_source', return_value=self.file):
            worker.step()

    def test_enabled_worker_publishes_actual_skip_counters_after_progress(self):
        self.step(self.worker())
        self.assertEqual(self.cat.clip(self.clip['id'])['completed'], 60)
        stats = self.cat.state('analysis_last_chunk')
        self.assertIsNotNone(stats, 'publish successful analysis model/skip counts')
        self.assertEqual((stats['sampled_frames'], stats['model_frames'], stats['skipped_frames']), (20, 4, 16))
        self.assertEqual(stats['clip_id'], self.clip['id'])
        self.assertEqual(stats['source_seconds'], 60)
        self.assertTrue(stats['gate_enabled'])

    def test_disabled_worker_records_every_sample_as_inferred(self):
        self.step(self.worker(enabled=False))
        stats = self.cat.state('analysis_last_chunk')
        self.assertIsNotNone(stats, 'disabled mode still records model counts')
        self.assertEqual((stats['model_frames'], stats['skipped_frames']), (20, 0))
        self.assertFalse(stats['gate_enabled'])

    def test_cancelled_chunk_publishes_no_counters_and_retry_uses_fresh_gate(self):
        class Yield:
            def __init__(self):
                self.calls = 0
            def score(self, frame):
                self.calls += 1
                if self.calls == 2:
                    raise MediaCancelled()
                return 0
        worker = self.worker(detector=Yield())
        self.step(worker)
        self.assertEqual(self.cat.clip(self.clip['id'])['completed'], 0)
        self.assertIsNone(self.cat.state('analysis_last_chunk'))
        worker.cover_turn = False
        self.step(worker)
        stats = self.cat.state('analysis_last_chunk')
        self.assertIsNotNone(stats, 'successful retry records only its own samples')
        self.assertEqual((stats['model_frames'], stats['skipped_frames']), (4, 16))
        self.assertIsNone(self.cat.clip(self.clip['id'])['error'])

    def test_rejected_generation_never_publishes_counters(self):
        with patch.object(self.cat, 'publish_chunk', return_value=False):
            self.step(self.worker())
        self.assertEqual(self.cat.clip(self.clip['id'])['completed'], 0)
        self.assertIsNone(self.cat.state('analysis_last_chunk'))

    def test_metrics_failure_cannot_fail_already_published_progress(self):
        original = self.cat.set_state
        def store(key, value):
            if key == 'analysis_last_chunk':
                raise OSError('metrics storage unavailable')
            return original(key, value)
        with patch.object(self.cat, 'set_state', side_effect=store), self.assertLogs('camera-review', level='WARNING'):
            self.step(self.worker())
        clip = self.cat.clip(self.clip['id'])
        self.assertEqual((clip['status'], clip['completed'], clip['error']), ('complete', 60, None))
