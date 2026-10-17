import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

os.environ.setdefault('APP_PASSWORD', 'test-password')
os.environ.setdefault('SESSION_SECRET', 'tests-only-secret-with-at-least-32-characters')
os.environ.setdefault('DATA_ROOT', str(Path(tempfile.gettempdir()) / 'camera-review-import-tests'))

try:
    from camera_review.catalog import Catalog, parse_recording, safe_source
    from camera_review.media import group_hits
    from camera_review.web import create_app
except ImportError:
    Catalog = None


class AppTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Catalog, 'application modules must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'recordings'
        self.source.mkdir()
        self.file = self.source / 'camera-A' / '00_20261017090000_20261017090200.mp4'
        self.file.parent.mkdir()
        self.file.write_bytes(b'0123456789')
        os.utime(self.file, (time.time() - 300, time.time() - 300))
        self.catalog = Catalog(self.source, self.root / 'data', stable_seconds=0)

    def stable(self):
        self.catalog.scan()
        self.catalog.scan()
        return self.catalog.clips()[0]

    def test_time_channel_parse(self):
        item = parse_recording(self.file.relative_to(self.source), 'Asia/Shanghai')
        self.assertEqual(item['channel'], 'camera-A / 00')
        self.assertEqual(item['duration'], 120)
        self.assertEqual(item['start'], 1792198800)
        self.assertIsNone(parse_recording(Path('x/invalid.mp4'), 'Asia/Shanghai'))

    def test_beijing_day_filters_include_only_playable_cross_midnight_coverage(self):
        late = self.file.parent / '00_20261016235940_20261017000200.mp4'
        late.write_bytes(b'late')
        os.utime(late, (time.time() - 300, time.time() - 300))
        self.stable()
        clip = next(c for c in self.catalog.clips() if c['relative'].endswith(late.name))
        self.catalog.publish_chunk(clip['id'], clip['generation'], 140, [(10, .8, None), (30, .9, None)])
        client = self.client(); self.login(client)
        clips = client.get('/api/clips?date=2026-10-17').json
        self.assertIn(clip['id'], [c['id'] for c in clips])
        events = client.get('/api/events?date=2026-10-17').json
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['start_offset'], 26)
        self.catalog.publish_media_duration(clip, 10)
        self.assertNotIn(clip['id'], [c['id'] for c in client.get('/api/clips?date=2026-10-17').json])

    def test_days_include_day_reached_by_cross_midnight_file_without_new_file(self):
        self.file.unlink()
        late = self.file.parent / '00_20261016235940_20261017000200.mp4'
        late.write_bytes(b'late'); os.utime(late, (time.time() - 300, time.time() - 300))
        clip = self.stable()
        client = self.client(); self.login(client)
        self.assertEqual(client.get('/api/days').json, ['2026-10-17', '2026-10-16'])
        self.catalog.publish_media_duration(clip, 20)
        self.assertEqual(client.get('/api/days').json, ['2026-10-16'])

    def test_source_traversal_and_symlink_rejected(self):
        with self.assertRaises(ValueError):
            safe_source(self.source, '../outside')
        outside = self.root / 'outside'
        outside.write_bytes(b'bad')
        try:
            (self.source / 'link.mp4').symlink_to(outside)
        except OSError:
            self.skipTest('Windows symlink creation privilege unavailable')
        with self.assertRaises(ValueError):
            safe_source(self.source, 'link.mp4')

    def test_stability_and_same_size_change_invalidate(self):
        self.catalog.scan()
        self.assertEqual(self.catalog.clips()[0]['status'], 'unstable')
        old = self.stable()
        self.assertEqual(old['status'], 'pending')
        self.assertTrue(self.catalog.publish_chunk(old['id'], old['generation'], 60, [(3, .8, None)]))
        self.assertEqual(len(self.catalog.events()), 1)
        self.file.write_bytes(b'abcdefghij')
        os.utime(self.file, (time.time() - 250, time.time() - 250))
        self.catalog.scan()
        new = self.catalog.clips()[0]
        self.assertNotEqual(old['generation'], new['generation'])
        self.assertEqual(new['completed'], 0)
        self.assertEqual(self.catalog.events(), [])
        self.assertFalse(self.catalog.publish_chunk(old['id'], old['generation'], 120, []))

    def test_changed_source_before_scan_blocks_publication(self):
        clip = self.stable()
        self.file.write_bytes(b'changedxxx')
        self.assertFalse(self.catalog.publish_chunk(clip['id'], clip['generation'], 60, [(3, .8, None)]))

    def test_groups_cross_chunk_boundary(self):
        clip = self.stable()
        self.catalog.publish_chunk(clip['id'], clip['generation'], 60, [(57, .8, None)])
        self.catalog.publish_chunk(clip['id'], clip['generation'], 120, [(60, .9, None), (90, .7, None)])
        events = self.catalog.events()
        self.assertEqual(len(events), 2)
        self.assertEqual((events[0]['start_offset'], events[0]['end_offset']), (53, 64))
        self.assertEqual(group_hits([(0, .8, None), (6, .9, None), (15, .7, None)], 20)[0]['end_offset'], 10)

    def test_worker_restart_and_failed_job_retry(self):
        clip = self.stable()
        job = self.catalog.enqueue_preview(clip['id'], 4)
        claimed = self.catalog.claim_preview()
        self.assertEqual(claimed['id'], job['id'])
        self.catalog.recover()
        self.assertEqual(self.catalog.preview(job['id'])['status'], 'pending')
        self.catalog.claim_preview()
        self.catalog.fail_preview(job['id'], 'decoder failed')
        self.assertEqual(self.catalog.enqueue_preview(clip['id'], 4)['status'], 'pending')

    def client(self):
        app = create_app(self.catalog, password='pw', secret='test-secret')
        app.testing = True
        client = app.test_client()
        client.environ_base['HTTP_ORIGIN'] = 'http://localhost'
        return client

    def login(self, client):
        return client.post('/api/login', json={'password': 'pw'}, headers={'Origin': 'http://localhost'})

    def test_auth_range_and_source_change(self):
        clip = self.stable()
        client = self.client()
        self.assertEqual(client.get('/api/status').status_code, 401)
        self.assertEqual(client.get('/api/original/' + clip['id']).status_code, 401)
        self.assertEqual(self.login(client).status_code, 200)
        reply = client.get('/api/original/' + clip['id'], headers={'Range': 'bytes=2-5'})
        self.assertEqual(reply.status_code, 206)
        self.assertEqual(reply.data, b'2345')
        self.assertEqual(reply.headers['Content-Range'], 'bytes 2-5/10')
        reply.close()
        self.file.write_bytes(b'abcdefghij')
        self.assertEqual(client.get('/api/original/' + clip['id']).status_code, 409)

    def test_strict_size_policy_blocks_all_direct_media_apis_before_scan(self):
        clip = self.stable()
        name = 'f' * 32 + '.jpg'
        (self.catalog.cache / name).write_bytes(b'jpeg')
        self.catalog.publish_chunk(clip['id'], clip['generation'], 60, [(3, .8, name)])
        self.catalog.complete_file_bytes = 11
        client = self.client(); self.login(client)
        self.assertEqual(client.get('/api/original/' + clip['id']).status_code, 409)
        self.assertEqual(client.get('/api/media/' + name).status_code, 409)
        self.assertEqual(client.post('/api/preview', json={'clip_id': clip['id'], 'offset': 0}).status_code, 409)

    def test_invalid_filter_offset_origin_and_cache(self):
        clip = self.stable()
        client = self.client()
        self.login(client)
        self.assertEqual(client.get('/api/clips?date=2026-02-30').status_code, 400)
        for offset in [-1, 120, 'nan', True, 'oops']:
            self.assertEqual(client.post('/api/preview', json={'clip_id': clip['id'], 'offset': offset}).status_code, 400)
        self.assertEqual(client.post('/api/pause', headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(client.get('/api/media/../../outside').status_code, 404)
        self.assertEqual(client.post('/api/preview', json={'clip_id': '../bad', 'offset': 0}).status_code, 400)
        self.assertEqual(client.post('/api/preview', json=['invalid']).status_code, 400)

    def test_mutations_require_origin_and_login_is_throttled(self):
        client = self.client()
        self.assertEqual(client.post('/api/login', json={'password': 'pw'}, headers={'Origin': ''}).status_code, 403)
        for _ in range(8):
            self.assertEqual(client.post('/api/login', json={'password': 'wrong'}).status_code, 401)
        self.assertEqual(self.login(client).status_code, 429)

    def test_channel_filter_and_newest_priority(self):
        other = self.file.with_name('10_20261018090000_20261018090200.mp4')
        other.write_bytes(b'other')
        os.utime(other, (time.time() - 300, time.time() - 300))
        self.stable()
        self.assertEqual(len(self.catalog.clips(channel='camera-A / 00')), 1)
        self.assertEqual(self.catalog.next_analysis()['channel'], 'camera-A / 10')
        for clip in self.catalog.clips():
            self.catalog.publish_chunk(clip['id'], clip['generation'], 60, [(3, .8, None)])
        self.assertEqual(len(self.catalog.events(channel='camera-A / 00')), 1)
        self.assertEqual(self.catalog.events(channel='camera-A / 00')[0]['channel'], 'camera-A / 00')

    def test_recent_files_and_hidden_directories_not_processed(self):
        self.catalog.stable_seconds = 180
        os.utime(self.file, None)
        hidden = self.source / '@eaDir'; hidden.mkdir()
        (hidden / self.file.name).write_bytes(b'ignored')
        self.catalog.scan(); self.catalog.scan()
        self.assertEqual(len(self.catalog.clips()), 1)
        self.assertEqual(self.catalog.clips()[0]['status'], 'unstable')
        self.assertIsNone(self.catalog.next_analysis())

    def test_authenticated_cached_media_and_pause_status(self):
        clip = self.stable()
        name = 'f' * 32 + '.jpg'
        (self.catalog.cache / name).write_bytes(b'jpeg')
        self.catalog.publish_chunk(clip['id'], clip['generation'], 60, [(3, .8, name)])
        client = self.client()
        self.assertEqual(client.get('/api/media/' + name).status_code, 401)
        self.login(client)
        with client.get('/api/media/' + name) as response:
            self.assertEqual(response.data, b'jpeg')
        self.assertTrue(client.post('/api/pause', json={}).json['paused'])
        status = client.get('/api/status').json
        self.assertTrue(status['paused'])
        self.assertEqual(status['completed_seconds'], 60)
        self.assertFalse(client.post('/api/resume', json={}).json['paused'])
        self.file.write_bytes(b'abcdefghij')
        self.assertEqual(client.get('/api/media/' + name).status_code, 409)

    def test_backoff_and_generation_preview_publish(self):
        clip = self.stable()
        self.catalog.fail_analysis(clip, 'decoding failed')
        self.assertIsNone(self.catalog.next_analysis())
        self.assertEqual(self.catalog.clip(clip['id'])['status'], 'error')
        job = self.catalog.enqueue_preview(clip['id'], 3)
        self.file.write_bytes(b'abcdefghij')
        self.assertFalse(self.catalog.publish_preview(job, 'f' * 32 + '.mp4'))

    def test_authenticated_clip_and_preview_expose_matching_source_generation(self):
        clip = self.stable()
        client = self.client()
        self.assertEqual(client.get('/api/clips').status_code, 401)
        self.login(client)
        row = client.get('/api/clips').json[0]
        self.assertIn('generation', row)
        self.assertEqual(row['generation'], clip['generation'])
        self.assertRegex(row['generation'], r'^[0-9a-f]{32}$')
        job = client.post('/api/preview', json={'clip_id': clip['id'], 'offset': 3}).json
        self.assertIn('generation', job)
        self.assertEqual(job['generation'], row['generation'])
        self.assertEqual(client.get('/api/preview/' + job['id']).json['generation'], row['generation'])
        self.file.write_bytes(b'new source generation')
        os.utime(self.file, (time.time() - 300, time.time() - 300))
        self.catalog.scan(); self.catalog.scan()
        fresh = client.get('/api/clips').json[0]
        self.assertEqual(fresh['id'], row['id'])
        self.assertNotEqual(fresh['generation'], row['generation'])
        new_job = client.post('/api/preview', json={'clip_id': clip['id'], 'offset': 3}).json
        self.assertEqual(new_job['generation'], fresh['generation'])
        self.assertEqual(client.get('/api/preview/' + job['id']).status_code, 404)

    def test_preview_generation_cannot_be_relabelled_after_source_changes(self):
        clip = self.stable()
        job = self.catalog.enqueue_preview(clip['id'], 0)
        self.file.write_bytes(b'new source generation')
        os.utime(self.file, (time.time() - 300, time.time() - 300))
        self.catalog.scan(); self.catalog.scan()
        client = self.client(); self.login(client)
        # Simulate a source generation change between enqueue and JSON serialization.
        with patch.object(self.catalog, 'enqueue_preview', return_value=job):
            response = client.post('/api/preview', json={'clip_id': clip['id'], 'offset': 0})
        self.assertEqual(response.status_code, 409)

    def test_actual_duration_and_cover_errors_are_exposed_separately(self):
        clip = self.stable()
        self.catalog.publish_media_duration(clip, 7)
        self.catalog.publish_chunk(clip['id'], clip['generation'], 7, [])
        self.catalog.fail_thumbnail(clip, 'cover failed')
        client = self.client(); self.login(client)
        row = client.get('/api/clips').json[0]
        self.assertEqual(row['duration'], 120)
        self.assertEqual(row['playable_duration'], 7)
        self.assertEqual(row['progress'], 100)
        self.assertEqual(row['status'], 'complete')
        self.assertIsNone(row['error'])
        self.assertEqual(row['thumbnail_error'], 'cover failed')
        status = client.get('/api/status').json
        self.assertEqual(status['completed_seconds'], 7)
        self.assertEqual(status['total_seconds'], 7)
        self.assertEqual(status['filename_seconds'], 120)
        self.assertEqual(status['thumbnail_errors'][0]['error'], 'cover failed')

    def test_legacy_unmeasured_progress_is_invalidated_on_upgrade(self):
        clip = self.stable()
        self.catalog.publish_chunk(clip['id'], clip['generation'], 120, [(3, .8, None)])
        with self.catalog.connect() as db:
            db.execute('ALTER TABLE clips DROP COLUMN media_duration')
        upgraded = Catalog(self.source, self.root / 'data')
        self.assertEqual(upgraded.clip(clip['id'])['completed'], 0)
        self.assertEqual(upgraded.clip(clip['id'])['status'], 'pending')
        self.assertEqual(upgraded.events(), [])

    def test_scan_failure_preserves_index_and_reports_error(self):
        clip = self.stable()
        self.catalog.publish_chunk(clip['id'], clip['generation'], 60, [(3, .8, None)])
        job = self.catalog.enqueue_preview(clip['id'], 0)
        last_scan = self.catalog.state('last_scan')

        def failed_walk(root, **kwargs):
            if kwargs.get('onerror'):
                kwargs['onerror'](PermissionError('camera directory denied'))
            return iter(())

        with patch('camera_review.catalog.os.walk', side_effect=failed_walk):
            self.catalog.scan()
        self.assertEqual(self.catalog.clip(clip['id'])['completed'], 60)
        self.assertEqual(len(self.catalog.events()), 1)
        self.assertEqual(self.catalog.preview(job['id'])['status'], 'pending')
        self.assertEqual(self.catalog.state('last_scan'), last_scan)
        self.assertIn('denied', self.catalog.state('scan_error'))
        self.catalog.scan()
        self.assertIsNone(self.catalog.state('scan_error'))

    def test_missing_mount_does_not_erase_index(self):
        clip = self.stable()
        original = self.catalog.root
        self.catalog.root = self.root / 'not-mounted'
        self.catalog.scan()
        self.assertEqual(self.catalog.clip(clip['id'])['generation'], clip['generation'])
        self.assertIsNotNone(self.catalog.state('scan_error'))
        with self.assertRaises(RuntimeError):
            self.catalog.current_source(clip)
        self.catalog.root = original

    def test_file_stat_io_failure_preserves_index(self):
        clip = self.stable()
        original_stat = Path.stat

        def denied(path, *args, **kwargs):
            if path == self.file:
                raise PermissionError('source stat denied')
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, 'stat', new=denied):
            self.assertFalse(self.catalog.scan())
        self.assertEqual(self.catalog.clip(clip['id'])['generation'], clip['generation'])
        self.assertIn('denied', self.catalog.state('scan_error'))

    def test_persisted_timeout_errors_hide_commands_and_seek_cache_is_private(self):
        clip = self.stable()
        self.catalog.fail_thumbnail(clip, "Command '['ffmpeg', '-i', '/recordings/private.mp4']' timed out after 90 seconds")
        client = self.client(); self.login(client)
        row = client.get('/api/clips').json[0]
        self.assertEqual(row['thumbnail_error'], '生成画面超时，后台会自动重试')
        self.assertNotIn('ffmpeg', str(client.get('/api/status').json))
        name = clip['id'] + '-' + clip['generation'] + '.mp4'
        self.assertEqual(client.get('/api/media/' + name).status_code, 404)

    def test_event_thumbnails_keep_only_representative(self):
        clip = self.stable()
        names = [str(index) * 32 + '.jpg' for index in [1, 2]]
        for name in names:
            (self.catalog.cache / name).write_bytes(b'jpeg')
        self.catalog.publish_chunk(clip['id'], clip['generation'], 60, [(3, .7, names[0]), (6, .9, names[1])])
        self.catalog.prune_jpegs()
        self.assertFalse((self.catalog.cache / names[0]).exists())
        self.assertTrue((self.catalog.cache / names[1]).exists())

    def test_cache_budget_removes_only_owned_files_and_allows_retry(self):
        clip = self.stable()
        job = self.catalog.enqueue_preview(clip['id'], 0)
        name = 'a' * 32 + '.mp4'
        (self.catalog.cache / name).write_bytes(b'12345')
        (self.catalog.cache / 'unrelated.mp4').write_bytes(b'12345')
        self.catalog.publish_preview(job, name)
        self.catalog.cleanup(limit=0)
        self.assertFalse((self.catalog.cache / name).exists())
        self.assertTrue((self.catalog.cache / 'unrelated.mp4').exists())
        self.assertEqual(self.catalog.enqueue_preview(clip['id'], 0)['status'], 'pending')

    def test_short_preview_priority_promotion_and_reuse(self):
        clip = self.stable()
        speculative = self.catalog.enqueue_preview(clip['id'], 8, seconds=20, prefetch=True)
        foreground = self.catalog.enqueue_preview(clip['id'], 0, seconds=8)
        self.assertEqual(self.catalog.claim_preview()['id'], foreground['id'])
        reused = self.catalog.enqueue_preview(clip['id'], 8, seconds=8)
        self.assertEqual(reused['id'], speculative['id'])
        self.assertEqual(reused['requested_seconds'], 20)
        self.assertEqual(reused['priority'], 0)
        self.catalog.requeue_preview(foreground['id'])
        self.assertTrue(self.catalog.pending_previews(foreground_only=True))

    def test_preview_api_validates_length_and_prefetch(self):
        clip = self.stable(); client = self.client(); self.login(client)
        for value in [True, 0, 31, 8.5, '8', None]:
            self.assertEqual(client.post('/api/preview', json={'clip_id':clip['id'], 'offset':0, 'seconds':value}).status_code, 400)
        self.assertEqual(client.post('/api/preview', json={'clip_id':clip['id'], 'offset':0, 'prefetch':1}).status_code, 400)
        job = client.post('/api/preview', json={'clip_id':clip['id'], 'offset':0, 'seconds':8}).json
        self.assertEqual(job['duration'], 8)
        self.assertEqual(client.post('/api/preview', json={'clip_id':clip['id'], 'offset':0}).json['duration'], 8)

    def test_real_legacy_previews_migration_is_idempotent_and_keeps_long_cache(self):
        import sqlite3
        clip = self.stable()
        with self.catalog.connect() as db:
            db.execute('DROP TABLE previews')
            db.execute('CREATE TABLE previews (id TEXT PRIMARY KEY,clip_id TEXT,generation TEXT,offset REAL,status TEXT,media TEXT,error TEXT,created REAL,UNIQUE(clip_id,generation,offset))')
            db.execute('INSERT INTO previews VALUES (?,?,?,?,?,?,?,?)', ('a'*32,clip['id'],clip['generation'],0,'ready','b'*32+'.mp4',None,1))
        for _ in range(2):
            self.catalog = Catalog(self.source,self.root/'data')
            job = self.catalog.enqueue_preview(clip['id'],0,seconds=8)
            self.assertEqual(job['requested_seconds'],30)
            self.assertEqual(job['status'],'ready')
        client=self.client(); self.login(client)
        self.assertEqual(client.post('/api/preview',json={'clip_id':clip['id'],'offset':0,'seconds':8}).json['duration'],30)


if __name__ == '__main__':
    unittest.main()
