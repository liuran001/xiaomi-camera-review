import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('APP_PASSWORD', 'test-password')
os.environ.setdefault('SESSION_SECRET', 'tests-only-secret-with-at-least-32-characters')
os.environ.setdefault('DATA_ROOT', str(Path(tempfile.gettempdir()) / 'camera-review-import-tests'))

from camera_review.catalog import Catalog
from camera_review import media
from camera_review.worker import Worker
from camera_review.web import create_app


class PreviewContinuityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'recordings'
        self.source.mkdir()
        self.file = self.source / '00_20261017090000_20261017090200.mp4'
        self.file.write_bytes(b'original')
        os.utime(self.file, (time.time() - 300, time.time() - 300))
        self.cat = Catalog(self.source, self.root / 'data', stable_seconds=0)
        self.cat.scan(); self.cat.scan()
        self.clip = self.cat.clips()[0]
        self.cat.publish_media_duration(self.clip, 120)

    def ready(self, offset=0, seconds=30, name='a' * 32 + '.mp4'):
        job = self.cat.enqueue_preview(self.clip['id'], offset, seconds=seconds)
        (self.cat.cache / name).write_bytes(b'video')
        self.assertTrue(self.cat.publish_preview(job, name))
        return self.cat.preview(job['id'])

    def client(self):
        client = create_app(self.cat, password='test-password', secret='test-secret').test_client()
        client.environ_base['HTTP_ORIGIN'] = 'http://localhost'
        client.post('/api/login', json={'password': 'test-password'})
        return client

    def test_reuse_returns_original_interval_only_when_opted_in(self):
        ready = self.ready()
        reused = self.cat.enqueue_preview(self.clip['id'], 10, seconds=8, reuse=True)
        self.assertEqual((reused['id'], reused['offset'], reused['requested_seconds']), (ready['id'], 0, 30))
        fresh = self.cat.enqueue_preview(self.clip['id'], 10, seconds=8)
        self.assertNotEqual(fresh['id'], ready['id'])
        self.assertEqual((fresh['offset'], fresh['status']), (10, 'pending'))

    def test_reuse_rejects_short_tails_but_allows_real_eof(self):
        ready = self.ready()
        self.assertEqual(self.cat.enqueue_preview(self.clip['id'], 26, seconds=8, reuse=True)['id'], ready['id'])
        self.assertNotEqual(self.cat.enqueue_preview(self.clip['id'], 26.001, seconds=8, reuse=True)['id'], ready['id'])
        self.assertNotEqual(self.cat.enqueue_preview(self.clip['id'], 30, seconds=8, reuse=True)['id'], ready['id'])
        tail = self.ready(110, 30, 'b' * 32 + '.mp4')
        self.assertEqual(self.cat.enqueue_preview(self.clip['id'], 119, seconds=8, reuse=True)['id'], tail['id'])

    def test_reuse_prefers_longest_remaining_coverage(self):
        self.ready(0, 20)
        longer = self.ready(5, 30, 'b' * 32 + '.mp4')
        self.assertEqual(self.cat.enqueue_preview(self.clip['id'], 10, reuse=True)['id'], longer['id'])

    def test_reuse_requires_readable_owned_current_media(self):
        ready = self.ready()
        (self.cat.cache / ready['media']).unlink()
        self.assertNotEqual(self.cat.enqueue_preview(self.clip['id'], 10, reuse=True)['id'], ready['id'])
        (self.cat.cache / ready['media']).mkdir()
        self.assertNotEqual(self.cat.enqueue_preview(self.clip['id'], 11, reuse=True)['id'], ready['id'])
        (self.cat.cache / ready['media']).rmdir()
        (self.cat.cache / ready['media']).write_bytes(b'')
        self.assertNotEqual(self.cat.enqueue_preview(self.clip['id'], 12, reuse=True)['id'], ready['id'])
        with self.cat.connect() as db:
            db.execute('UPDATE previews SET media=? WHERE id=?', ('../outside.mp4', ready['id']))
        self.assertNotEqual(self.cat.enqueue_preview(self.clip['id'], 13, reuse=True)['id'], ready['id'])

    def test_opted_in_exact_offset_requeues_missing_or_too_short_media(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                offset = 40 if missing else 0
                job = self.ready(offset, 1, ('b' if missing else 'a') * 32 + '.mp4')
                if missing:
                    (self.cat.cache / job['media']).unlink()
                # The old exact-offset contract remains unchanged by default.
                self.assertEqual(self.cat.enqueue_preview(self.clip['id'], offset, seconds=8)['status'], 'ready')
                replacement = self.cat.enqueue_preview(self.clip['id'], offset, seconds=8, reuse=True)
                self.assertEqual((replacement['id'], replacement['status'], replacement['requested_seconds'], replacement['media']),
                                 (job['id'], 'pending', 8, None))

    def test_opted_in_failed_retry_uses_new_duration_while_default_retains_legacy(self):
        job = self.ready(seconds=1)
        self.cat.cleanup(limit=0)
        self.assertEqual(self.cat.preview(job['id'])['status'], 'failed')
        legacy = self.cat.enqueue_preview(self.clip['id'], 0, seconds=8)
        self.assertEqual((legacy['id'], legacy['status'], legacy['requested_seconds']), (job['id'], 'pending', 1))
        self.cat.fail_preview(job['id'], 'retry failed')
        replacement = self.cat.enqueue_preview(self.clip['id'], 0, seconds=8, reuse=True)
        self.assertEqual((replacement['id'], replacement['status'], replacement['requested_seconds']), (job['id'], 'pending', 8))
        self.assertEqual(self.cat.claim_preview()['requested_seconds'], 8)
        result = self.client().post('/api/preview', json={'clip_id': self.clip['id'], 'offset': 0, 'seconds': 8, 'reuse': True})
        self.assertEqual((result.status_code, result.json['duration']), (202, 8))

    def symlink(self, path, target, directory=False):
        try:
            path.symlink_to(target, target_is_directory=directory)
        except OSError:
            self.skipTest('Windows symlink creation privilege unavailable')

    def test_reuse_and_cleanup_do_not_follow_media_links(self):
        ready = self.ready()
        path = self.cat.cache / ready['media']; path.unlink()
        self.symlink(path, self.file)
        self.assertNotEqual(self.cat.enqueue_preview(self.clip['id'], 10, reuse=True)['id'], ready['id'])
        self.cat.cleanup(limit=0)
        self.assertTrue(path.is_symlink())
        self.assertEqual(self.file.read_bytes(), b'original')

    def test_cache_directory_link_blocks_cleanup_and_reuse(self):
        ready = self.ready()
        old = self.cat.cache
        other = self.root / 'outside'; old.rename(other)
        self.symlink(old, other, directory=True)
        with self.assertRaises(RuntimeError):
            self.cat.cleanup(limit=0)
        with self.assertRaises(RuntimeError):
            self.cat.enqueue_preview(self.clip['id'], 10, reuse=True)
        self.assertTrue((other / ready['media']).exists())

    def test_api_reuse_validation_original_interval_and_generation(self):
        ready = self.ready()
        client = self.client()
        for value in (1, 'true', None, [], {}):
            self.assertEqual(client.post('/api/preview', json={'clip_id': self.clip['id'], 'offset': 10, 'reuse': value}).status_code, 400)
        result = client.post('/api/preview', json={'clip_id': self.clip['id'], 'offset': 10, 'seconds': 8, 'reuse': True})
        self.assertEqual(result.status_code, 202)
        self.assertEqual((result.json['id'], result.json['offset'], result.json['duration']), (ready['id'], 0, 30))
        self.assertEqual(result.json['generation'], self.clip['generation'])
        self.file.write_bytes(b'changed source')
        self.assertEqual(client.post('/api/preview', json={'clip_id': self.clip['id'], 'offset': 10, 'reuse': True}).status_code, 409)

    def test_reuse_and_media_reads_refresh_playback_lru(self):
        first = self.ready()
        second = self.ready(40, 30, 'b' * 32 + '.mp4')
        first_path, second_path = (self.cat.cache / job['media'] for job in (first, second))
        os.utime(first_path, (1, 1)); os.utime(second_path, (2, 2))
        self.cat.enqueue_preview(self.clip['id'], 10, reuse=True)
        self.cat.cleanup(limit=5)
        self.assertTrue(first_path.exists()); self.assertFalse(second_path.exists())
        os.utime(first_path, (1, 1))
        response = self.client().get('/api/media/' + first['media'])
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertGreater(first_path.stat().st_mtime, 2)

    def test_scan_and_restart_remove_orphan_mp4_but_keep_referenced_jpegs(self):
        ready = self.ready()
        orphan = self.cat.cache / ('b' * 32 + '.mp4'); orphan.write_bytes(b'orphan')
        jpeg = self.cat.cache / ('c' * 32 + '.jpg'); jpeg.write_bytes(b'evidence')
        unknown = self.cat.cache / 'unrelated.mp4'; unknown.write_bytes(b'keep')
        with self.cat.connect() as db:
            db.execute('INSERT INTO events(clip_id,generation,thumb) VALUES (?,?,?)', (self.clip['id'], self.clip['generation'], jpeg.name))
        self.cat.recover()
        self.assertFalse(orphan.exists())
        self.assertTrue(jpeg.exists()); self.assertTrue(unknown.exists())
        self.file.write_bytes(b'next generation')
        self.cat.scan()
        self.assertFalse((self.cat.cache / ready['media']).exists())

    def test_publish_exception_discards_only_unreferenced_output(self):
        for publish_then_raise in (False, True):
            with self.subTest(publish_then_raise=publish_then_raise):
                job = self.cat.enqueue_preview(self.clip['id'], 50 + int(publish_then_raise))
                name = ('d' if publish_then_raise else 'e') * 32 + '.mp4'
                target = self.cat.cache / name
                original_publish = self.cat.publish_preview
                def publish(job, name):
                    if publish_then_raise:
                        original_publish(job, name)
                    raise RuntimeError('publish fault')
                def encode(*args, **kwargs):
                    target.write_bytes(b'video')
                    return name
                with patch('camera_review.worker.ensure_seek_source', return_value=self.file), patch('camera_review.worker.make_preview', side_effect=encode), patch.object(self.cat, 'publish_preview', side_effect=publish):
                    with self.assertLogs('camera-review', level='ERROR'):
                        Worker(self.cat, detector=object()).step()
                self.assertEqual(target.exists(), publish_then_raise)
                self.assertEqual(self.cat.preview(job['id'])['status'], 'ready' if publish_then_raise else 'failed')

    def test_preview_partial_failure_and_cancellation_remove_only_owned_partial(self):
        for failure in (media.MediaCancelled(), RuntimeError('encode fault')):
            with self.subTest(failure=type(failure).__name__):
                def encode(command, **kwargs):
                    Path(command[-1]).write_bytes(b'partial')
                    raise failure
                with patch('camera_review.media._run', side_effect=encode):
                    with self.assertRaises(type(failure)):
                        media.make_preview(self.file, self.cat.cache, 0)
                self.assertEqual(list(self.cat.cache.glob('*.mp4')), [])

    def test_preview_probe_failure_and_late_cancellation_discard_partial(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                def encode(command, **kwargs):
                    Path(command[-1]).write_bytes(b'partial')
                def probe(*args, **kwargs):
                    if cancel:
                        raise media.MediaCancelled()
                    raise RuntimeError('probe fault')
                with patch('camera_review.media._run', side_effect=encode), patch('camera_review.media._invoke', side_effect=probe):
                    with self.assertRaises(media.MediaCancelled if cancel else RuntimeError):
                        media.make_preview(self.file, self.cat.cache, 0)
                self.assertEqual(list(self.cat.cache.iterdir()), [])

    def test_preview_replaced_partial_link_remains_untouched(self):
        def encode(command, **kwargs):
            partial = Path(command[-1]); partial.unlink()
            self.symlink(partial, self.file)
        with patch('camera_review.media._run', side_effect=encode):
            with self.assertRaises(RuntimeError):
                media.make_preview(self.file, self.cat.cache, 0)
        self.assertEqual(self.file.read_bytes(), b'original')
        self.assertTrue(next(self.cat.cache.iterdir()).is_symlink())

    def test_generation_rejection_discards_unpublished_final_output(self):
        job = self.cat.enqueue_preview(self.clip['id'], 0)
        name = 'f' * 32 + '.mp4'
        def encode(*args, **kwargs):
            (self.cat.cache / name).write_bytes(b'video')
            self.file.write_bytes(b'changed source')
            return name
        with patch('camera_review.worker.ensure_seek_source', return_value=self.file), patch('camera_review.worker.make_preview', side_effect=encode):
            Worker(self.cat, detector=object()).step()
        self.assertFalse((self.cat.cache / name).exists())
        self.assertNotEqual(self.cat.preview(job['id'])['status'], 'ready')

    def test_preview_preexisting_partial_is_never_overwritten_or_removed(self):
        name = 'f' * 32 + '.mp4'
        partial = self.cat.cache / ('f' * 32 + '.part.mp4')
        partial.write_bytes(b'unowned')
        with patch('camera_review.media.generated_name', return_value=name), patch('camera_review.media._run') as run:
            with self.assertRaises(RuntimeError):
                media.make_preview(self.file, self.cat.cache, 0)
        run.assert_not_called()
        self.assertEqual(partial.read_bytes(), b'unowned')

    def test_cleanup_ignores_owned_shaped_directories(self):
        directory = self.cat.cache / ('a' * 32 + '.mp4'); directory.mkdir()
        jpeg_directory = self.cat.cache / ('b' * 32 + '.jpg'); jpeg_directory.mkdir()
        self.cat.cleanup(limit=0)
        self.cat.recover()
        self.assertTrue(directory.is_dir())
        self.assertTrue(jpeg_directory.is_dir())
