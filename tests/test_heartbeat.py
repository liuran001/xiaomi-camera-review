import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('APP_PASSWORD', 'test-password')
os.environ.setdefault('SESSION_SECRET', 'tests-only-secret-with-at-least-32-characters')
os.environ.setdefault('DATA_ROOT', str(Path(tempfile.gettempdir()) / 'camera-review-import-tests'))

from camera_review.catalog import Catalog
from camera_review.web import create_app

try:
    from camera_review import heartbeat
except ImportError:
    heartbeat = None


class HeartbeatFileTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(heartbeat, 'RAM heartbeat file support must exist')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'worker-heartbeat'

    def test_numeric_timestamp_round_trip_accepts_string_paths(self):
        for value in (1, 1792600000.125):
            with self.subTest(value=value):
                heartbeat.write_heartbeat(str(self.path), value)
                self.assertEqual(heartbeat.read_heartbeat(str(self.path)), value)
                self.assertLessEqual(self.path.stat().st_size, 64)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_update_replaces_complete_file_atomically_in_same_directory(self):
        self.path.write_text('1000\n', encoding='ascii')
        replace = os.replace

        def publish(source, destination):
            source, destination = Path(source), Path(destination)
            self.assertEqual(source.parent, self.path.parent)
            self.assertNotEqual(source, self.path)
            self.assertEqual(destination, self.path)
            self.assertEqual(self.path.read_text(encoding='ascii'), '1000\n')
            self.assertEqual(float(source.read_text(encoding='ascii')), 2000.25)
            replace(source, destination)

        with patch('camera_review.heartbeat.os.replace', side_effect=publish) as publish_file:
            heartbeat.write_heartbeat(self.path, 2000.25)
        publish_file.assert_called_once()
        self.assertEqual(heartbeat.read_heartbeat(self.path), 2000.25)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_failed_replace_preserves_previous_file_and_removes_temporary(self):
        self.path.write_text('1000\n', encoding='ascii')
        with patch('camera_review.heartbeat.os.replace', side_effect=OSError('RAM directory unavailable')):
            with self.assertRaises(OSError):
                heartbeat.write_heartbeat(self.path, 2000)
        self.assertEqual(heartbeat.read_heartbeat(self.path), 1000)
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_invalid_write_does_not_modify_file(self):
        self.path.write_text('1000\n', encoding='ascii')
        for value in (0, -1, True, None, '1000', float('nan'), float('inf'), -float('inf'), 10 ** 400):
            with self.subTest(value=str(value)[:30]):
                with self.assertRaises(ValueError):
                    heartbeat.write_heartbeat(self.path, value)
                self.assertEqual(self.path.read_text(encoding='ascii'), '1000\n')
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_missing_unreadable_and_invalid_paths_return_none(self):
        self.assertIsNone(heartbeat.read_heartbeat(self.path))
        self.assertIsNone(heartbeat.read_heartbeat(self.root))
        self.assertIsNone(heartbeat.read_heartbeat('bad\0path'))
        with patch.object(Path, 'open', side_effect=PermissionError('denied')):
            self.assertIsNone(heartbeat.read_heartbeat(self.path))

    def test_invalid_or_oversized_file_returns_none(self):
        for payload in (b'', b'0', b'-1', b'nan', b'inf', b'-inf', b'1e999', b'null',
                        b'{"timestamp":1000}', b'1000\n2000', b'\xff', b'1' * 4096):
            with self.subTest(payload=payload[:30]):
                self.path.write_bytes(payload)
                self.assertIsNone(heartbeat.read_heartbeat(self.path))

    def test_read_bounds_memory_even_for_large_malformed_file(self):
        stream = io.BytesIO(b'1' * 4096)
        with patch.object(Path, 'open', return_value=stream), \
                patch.object(stream, 'read', wraps=stream.read) as read:
            self.assertIsNone(heartbeat.read_heartbeat(self.path))
            self.assertTrue(read.call_args_list)
            for call in read.call_args_list:
                self.assertGreater(call.args[0], 0)
                self.assertLessEqual(call.args[0], 256)


class HeartbeatStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = self.root / 'recordings'
        source.mkdir()
        self.cat = Catalog(source, self.root / 'data')
        self.path = self.root / 'worker-heartbeat'
        env = patch.dict(os.environ, {'HEARTBEAT_FILE': ''})
        env.start()
        self.addCleanup(env.stop)

    def client(self, heartbeat_file=None):
        if heartbeat_file is not None:
            os.environ['HEARTBEAT_FILE'] = str(heartbeat_file)
        app = create_app(self.cat, password='pw', secret='test-secret')
        app.testing = True
        client = app.test_client()
        with patch('camera_review.web.time.time', return_value=2000):
            with client.session_transaction() as session:
                session['authenticated'] = True
        return client

    def status(self, client):
        with patch('camera_review.web.time.time', return_value=2000):
            reply = client.get('/api/status')
        self.assertEqual(reply.status_code, 200)
        return reply.json

    def test_unconfigured_file_uses_database_heartbeat(self):
        client = self.client()
        for timestamp, alive in ((None, False), (2000, True), (1821, True), (1820, False)):
            with self.subTest(timestamp=timestamp):
                self.cat.set_state('heartbeat', timestamp)
                status = self.status(client)
                self.assertEqual(status['heartbeat'], timestamp)
                self.assertEqual(status['worker_alive'], alive)

    def test_configured_file_supplies_live_timestamp(self):
        self.cat.set_state('heartbeat', 1000)
        self.path.write_text('1990.25\n', encoding='ascii')
        status = self.status(self.client(self.path))
        self.assertEqual(status['heartbeat'], 1990.25)
        self.assertTrue(status['worker_alive'])

    def test_configured_file_updates_are_visible_without_recreating_app(self):
        client = self.client(self.path)
        self.path.write_text('1000\n', encoding='ascii')
        self.assertFalse(self.status(client)['worker_alive'])
        self.path.write_text('1990\n', encoding='ascii')
        self.assertTrue(self.status(client)['worker_alive'])

    def test_configured_stale_file_does_not_fall_back_to_live_database(self):
        self.cat.set_state('heartbeat', 2000)
        self.path.write_text('1820\n', encoding='ascii')
        status = self.status(self.client(self.path))
        self.assertEqual(status['heartbeat'], 1820)
        self.assertFalse(status['worker_alive'])

    def test_configured_missing_file_does_not_fall_back_to_live_database(self):
        self.cat.set_state('heartbeat', 2000)
        status = self.status(self.client(self.path))
        self.assertIsNone(status['heartbeat'])
        self.assertFalse(status['worker_alive'])

    def test_configured_invalid_file_does_not_expose_content_or_use_database(self):
        self.cat.set_state('heartbeat', 2000)
        self.path.write_text('private malformed heartbeat', encoding='ascii')
        status = self.status(self.client(self.path))
        self.assertIsNone(status['heartbeat'])
        self.assertFalse(status['worker_alive'])
        self.assertNotIn('private malformed heartbeat', str(status))


if __name__ == '__main__':
    unittest.main()
