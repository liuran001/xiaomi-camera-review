import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from camera_review.catalog import Catalog

try:
    from camera_review.media import ensure_seek_source
    from camera_review.catalog import SEEK_CACHE_LIMIT, PLAYBACK_CACHE_LIMIT
except ImportError:
    ensure_seek_source = None


class SeekCacheTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(ensure_seek_source, 'generation-bound stream-copy seek cache must exist')
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.source = self.root / 'recordings'; self.source.mkdir()
        self.file = self.source / '00_20261017090000_20261017090200.mp4'
        self.file.write_bytes(b'original'); os.utime(self.file, (time.time()-300, time.time()-300))
        self.cat = Catalog(self.source, self.root / 'data', stable_seconds=0); self.cat.scan(); self.cat.scan()
        self.clip = self.cat.clips()[0]

    def fake_copy(self, command, timeout=180):
        Path(command[-1]).write_bytes(b'remuxed')

    def test_copy_command_atomic_publish_and_reuse(self):
        before = self.file.read_bytes(), self.file.stat().st_mtime_ns
        with patch('camera_review.media._run', side_effect=self.fake_copy) as run, patch('camera_review.media.probe_duration', return_value=120):
            target = ensure_seek_source(self.cat, self.clip)
            self.assertTrue(target.is_file())
            self.assertEqual(target.parent, self.cat.seek_cache)
            self.assertNotEqual(target, self.file)
            self.assertEqual(ensure_seek_source(self.cat, self.clip), target)
            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('-c')+1], 'copy')
            self.assertEqual(command[command.index('-map')+1], '0')
            self.assertNotIn('-c:v', command)
            self.assertNotIn('-c:a', command)
            self.assertIn('+faststart', command)
            self.assertEqual(list(self.cat.seek_cache.glob('*.part.mp4')), [])
        self.assertEqual((self.file.read_bytes(), self.file.stat().st_mtime_ns), before)

    def test_source_change_during_build_cannot_publish(self):
        def changed(command, timeout=180):
            self.fake_copy(command, timeout)
            self.file.write_bytes(b'changed!')
        with patch('camera_review.media._run', side_effect=changed), patch('camera_review.media.probe_duration', return_value=120):
            with self.assertRaises(RuntimeError):
                ensure_seek_source(self.cat, self.clip)
        self.assertEqual(list(self.cat.seek_cache.iterdir()), [])

    def symlink(self, path, target, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError:
            self.skipTest('Windows symlink creation privilege unavailable')

    def test_preexisting_partial_symlink_is_rejected_without_touching_target(self):
        unrelated = self.root / 'unrelated'; unrelated.write_bytes(b'never overwrite')
        partial = self.cat.seek_cache / (self.clip['id'] + '-' + self.clip['generation'] + '.part.mp4')
        self.symlink(partial, unrelated)
        with patch('camera_review.media._run', side_effect=self.fake_copy) as run:
            with self.assertRaises(RuntimeError):
                ensure_seek_source(self.cat, self.clip)
        run.assert_not_called()
        self.assertEqual(unrelated.read_bytes(), b'never overwrite')
        self.assertTrue(partial.is_symlink())
        self.assertEqual(list(self.cat.seek_cache.glob('*-*.mp4')), [partial])

    def test_seek_cache_directory_symlink_is_rejected_in_build_and_recovery(self):
        other = self.root / 'external-cache'; other.mkdir()
        owned = other / (self.clip['id'] + '-' + self.clip['generation'] + '.part.mp4')
        owned.write_bytes(b'keep')
        self.cat.seek_cache.rmdir(); self.symlink(self.cat.seek_cache, other, directory=True)
        with patch('camera_review.media._run') as run:
            with self.assertRaises(RuntimeError):
                ensure_seek_source(self.cat, self.clip)
        run.assert_not_called()
        with self.assertRaises(RuntimeError):
            self.cat.recover()
        self.assertEqual(owned.read_bytes(), b'keep')

    def test_cleanup_does_not_remove_a_replaced_partial_link(self):
        unrelated = self.root / 'unrelated'; unrelated.write_bytes(b'keep')
        partial = self.cat.seek_cache / (self.clip['id'] + '-' + self.clip['generation'] + '.part.mp4')

        def replace_partial(command, timeout=180):
            partial.unlink()
            self.symlink(partial, unrelated)

        with patch('camera_review.media._run', side_effect=replace_partial):
            with self.assertRaises(RuntimeError):
                ensure_seek_source(self.cat, self.clip)
        self.assertTrue(partial.is_symlink())
        self.assertEqual(unrelated.read_bytes(), b'keep')

    def test_invalid_video_or_timeout_cannot_publish(self):
        with patch('camera_review.media._run', side_effect=self.fake_copy), patch('camera_review.media.probe_duration', side_effect=RuntimeError('no video')):
            with self.assertRaises(RuntimeError):
                ensure_seek_source(self.cat, self.clip)
        self.assertEqual(list(self.cat.seek_cache.iterdir()), [])
        from camera_review.media import _run
        with patch('camera_review.media.subprocess.run', side_effect=subprocess.TimeoutExpired(['ffmpeg', 'private'], 180)):
            with self.assertRaisesRegex(RuntimeError, '^生成画面超时，后台会自动重试$'):
                _run(['ffmpeg', 'private'])

    def test_analysis_watchdog_kills_a_blocked_decoder_and_discards_hits(self):
        from camera_review.media import analyze_chunk
        original_popen, original_timer = subprocess.Popen, threading.Timer

        def blocked_process(command, **kwargs):
            return original_popen([sys.executable, '-c', 'import sys,time; sys.stdout.buffer.write(bytes(640*360*3)); sys.stdout.buffer.flush(); time.sleep(60)'], **kwargs)

        class Detector:
            def score(self, frame):
                return .8

        with patch('camera_review.media.subprocess.Popen', side_effect=blocked_process), patch('camera_review.media.threading.Timer', side_effect=lambda interval, callback: original_timer(.2, callback)):
            with self.assertRaisesRegex(RuntimeError, '人物分析超时'):
                analyze_chunk(self.file, self.cat.cache, 0, 60, Detector())
        self.assertEqual(list(self.cat.cache.glob('*.jpg')), [])

    def test_budget_old_generations_and_restart_partials(self):
        self.assertEqual(SEEK_CACHE_LIMIT, 512 * 1024**2)
        self.assertEqual(SEEK_CACHE_LIMIT + PLAYBACK_CACHE_LIMIT, 2 * 1024**3)
        owned = self.cat.seek_cache / (self.clip['id'] + '-' + self.clip['generation'] + '.mp4')
        owned.write_bytes(b'123456')
        orphan = self.cat.seek_cache / ('a'*32 + '-' + 'b'*32 + '.mp4'); orphan.write_bytes(b'orphan')
        unrelated = self.cat.seek_cache / 'unrelated.mp4'; unrelated.write_bytes(b'keep')
        partial = owned.with_suffix('.part.mp4'); partial.write_bytes(b'partial')
        self.cat.recover()
        self.assertFalse(partial.exists()); self.assertFalse(orphan.exists()); self.assertTrue(unrelated.exists())
        self.cat.cleanup_seek_cache(limit=5)
        self.assertFalse(owned.exists()); self.assertTrue(unrelated.exists())
        owned.write_bytes(b'old')
        self.file.write_bytes(b'newsize'); self.cat.scan()
        self.assertFalse(owned.exists())

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg unavailable')
    def test_real_copy_preserves_codec_and_supports_seek(self):
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:size=160x90:rate=6',
                        '-t', '7', '-c:v', 'libx264', '-threads', '1', '-g', '6', '-y', str(self.file)], check=True)
        os.utime(self.file, (time.time()-300, time.time()-300)); self.cat.scan(); self.cat.scan()
        clip = self.cat.clips()[0]
        before = hashlib.sha256(self.file.read_bytes()).digest(), self.file.stat().st_mtime_ns
        target = ensure_seek_source(self.cat, clip)
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-read_intervals', '5%+1',
                                 '-show_entries', 'packet=pts_time', '-of', 'csv=p=0', str(target)], capture_output=True, text=True, check=True)
        self.assertLess(abs(float(result.stdout.splitlines()[0])-5), 1)
        result = subprocess.run(['ffmpeg', '-v', 'error', '-ss', '5', '-i', str(target), '-frames:v', '1',
                                 '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'], capture_output=True, check=True)
        self.assertEqual(len(result.stdout), 160*90*3)
        self.assertGreater(result.stdout[0], 200); self.assertLess(result.stdout[1], 10)
        self.assertEqual((hashlib.sha256(self.file.read_bytes()).digest(), self.file.stat().st_mtime_ns), before)

    def test_analysis_cancellation_cleans_provisional_hits_and_reaps_decoder(self):
        from camera_review import media
        original = subprocess.Popen
        processes = []
        def decoder(command, **kwargs):
            process = original([sys.executable, '-c', 'import sys,time; sys.stdout.buffer.write(bytes(640*360*3)); sys.stdout.buffer.flush(); time.sleep(60)'], **kwargs)
            processes.append(process); return process
        class Detector:
            def score(self, frame): return .8
        with patch('camera_review.media.subprocess.Popen', side_effect=decoder):
            with self.assertRaises(media.MediaCancelled):
                media.analyze_chunk(self.file, self.cat.cache, 0, 60, Detector(), cancelled=lambda: bool(list(self.cat.cache.glob('*.jpg'))))
        self.assertEqual(list(self.cat.cache.glob('*.jpg')), [])
        self.assertIsNotNone(processes[0].poll())
        self.assertTrue(processes[0].stdout.closed)

    def test_blocking_transform_cancellation_reaps_child(self):
        from camera_review import media
        start = time.monotonic()
        with self.assertRaises(media.MediaCancelled):
            media._invoke([sys.executable, '-c', 'import time; time.sleep(60)'], capture_output=True, timeout=10,
                          cancelled=lambda: time.monotonic() - start > .2)
        self.assertLess(time.monotonic()-start, 3)

    def test_analysis_cancellation_interrupts_blocked_frame_read(self):
        from camera_review import media
        original, timer = subprocess.Popen, threading.Timer
        def decoder(command, **kwargs):
            return original([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        started=time.monotonic()
        with patch('camera_review.media.subprocess.Popen',side_effect=decoder), patch('camera_review.media.threading.Timer',side_effect=lambda interval,callback: timer(1,callback)):
            with self.assertRaises(media.MediaCancelled):
                media.analyze_chunk(self.file,self.cat.cache,0,60,object(),cancelled=lambda: time.monotonic()-started>.2)
        self.assertLess(time.monotonic()-started,.9)
