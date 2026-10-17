"""Exercise signed gateway requests at the real Flask boundary."""
import hashlib
import hmac
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

os.environ.setdefault('APP_PASSWORD', 'test-password')
os.environ.setdefault('SESSION_SECRET', 'tests-only-secret-with-at-least-32-characters')
os.environ.setdefault('DATA_ROOT', str(Path(tempfile.gettempdir()) / 'camera-review-import-tests'))

from camera_review.catalog import Catalog
from camera_review.web import create_app

PREFIX = '/webman/3rdparty/XiaomiCameraReview/index.cgi'
SECRET = 'a1' * 32


class DSMAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = self.root / 'recordings'
        source.mkdir()
        self.file = source / 'camera-A' / '00_20261017090000_20261017090200.mp4'
        self.file.parent.mkdir()
        self.file.write_bytes(b'0123456789')
        os.utime(self.file, (time.time() - 300, time.time() - 300))
        self.catalog = Catalog(source, self.root / 'data', stable_seconds=0)
        self.catalog.scan(); self.catalog.scan()
        self.clip = self.catalog.clips()[0]
        self.secret_file = self.root / 'gateway-secret'
        self.secret_file.write_text(SECRET + '\n', encoding='utf-8')
        env = patch.dict(os.environ, DSM_GATEWAY_SECRET_FILE=str(self.secret_file), DSM_APP_PREFIX=PREFIX)
        env.start(); self.addCleanup(env.stop)
        self.app = create_app(self.catalog, password='pw', secret='test-secret')
        self.app.testing = True
        self.client = self.app.test_client()

    def signed(self, path='/api/status', method='GET', username='ordinary-user', timestamp=None, host='localhost', prefix=PREFIX):
        timestamp = str(int(time.time()) if timestamp is None else timestamp)
        decoded, _, query = path.partition('?')
        from urllib.parse import unquote
        message = '\n'.join((timestamp, username, method, unquote(decoded), query, prefix, host))
        return {'X-Camera-DSM-User': quote(username, safe=''), 'X-Camera-DSM-Time': timestamp,
                'X-Camera-DSM-Prefix': prefix,
                'X-Camera-DSM-Signature': hmac.new(SECRET.encode(), message.encode(), hashlib.sha256).hexdigest()}

    def password_login(self):
        return self.client.post('/api/login', json={'password': 'pw'}, headers={'Origin': 'http://localhost'})

    def test_all_valid_dsm_users_access_without_password_or_app_cookie(self):
        for username in ('ordinary-user', 'admin', 'camera viewer', '测试用户', 'camera%41', 'camera%测试'):
            with self.subTest(username=username):
                response = self.client.get('/api/status', headers=self.signed(username=username))
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_username_header_requires_valid_utf8_percent_encoding_and_decodes_once(self):
        self.password_login()
        for encoded, signed_user in (('%FF', '\ufffd'), ('%E6%B5', '\ufffd'),
                                     ('bad%name', 'bad%name'), ('bad%', 'bad%'),
                                     ('bad%0Aname', 'bad\nname')):
            with self.subTest(encoded=encoded):
                headers = self.signed(username=signed_user) | {'X-Camera-DSM-User': encoded}
                self.assertEqual(self.client.get('/api/status', headers=headers).status_code, 401)
        # Literal percent sequences are usernames, never another decode operation.
        self.assertEqual(self.client.get('/api/status', headers=self.signed(username='camera%41')).status_code, 200)

    def test_signed_original_head_is_safe_without_origin_and_remains_request_bound(self):
        path = '/api/original/' + self.clip['id']
        with self.client.head(path, headers=self.signed(path, 'HEAD')) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b'')
            self.assertEqual(response.headers['Content-Length'], '10')
            self.assertEqual(response.headers['Content-Type'], 'video/mp4')
            self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(self.client.head(path).status_code, 401)
        self.assertEqual(self.client.head(path, headers=self.signed(path, 'GET')).status_code, 401)
        self.password_login()
        with self.client.head(path) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers['Content-Length'], '10')

    def test_forged_partial_expired_and_future_identity_never_fall_back_to_password(self):
        self.assertEqual(self.password_login().status_code, 200)
        valid = self.signed()
        cases = [dict(valid, **{'X-Camera-DSM-Signature': '0' * 64}),
                 {'X-Camera-DSM-User': 'admin'},
                 {'X-Camera-DSM-Prefix': PREFIX},
                 self.signed(timestamp=int(time.time()) - 31),
                 self.signed(timestamp=int(time.time()) + 31),
                 self.signed(timestamp='invalid'), self.signed(username='')]
        for headers in cases:
            with self.subTest(headers=list(headers)):
                response = self.client.get('/api/status', headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json['auth'], 'dsm')
                self.assertEqual(response.json['login_url'], '/?launchApp=com.xiaomi.camera.review')
        self.assertEqual(self.client.get('/api/status').status_code, 200)

    def test_signature_binds_actual_method_decoded_path_raw_query_prefix_and_host(self):
        self.password_login()
        original = self.signed('/api/clips?channel=camera%20%2F%2000&date=2026-10-17')
        for path, method, headers in [
                ('/api/events?channel=camera%20%2F%2000&date=2026-10-17', 'GET', original),
                ('/api/clips?date=2026-10-17&channel=camera%20%2F%2000', 'GET', original),
                ('/api/clips?channel=camera+%2F+00&date=2026-10-17', 'GET', original),
                ('/api/clips?channel=camera%20%2F%2000&date=2026-10-17', 'POST', original),
                ('/api/status', 'GET', self.signed(host='other.example')),
                ('/api/status', 'GET', self.signed(prefix='/forged'))]:
            with self.subTest(path=path, method=method):
                self.assertEqual(self.client.open(path, method=method, headers=headers).status_code, 401)
        response = self.client.get('/api/clips?channel=camera%20%2F%2000&date=2026-10-17', headers=original)
        self.assertEqual(response.status_code, 200)

    def test_missing_or_invalid_secret_fails_closed_even_with_direct_session(self):
        self.password_login()
        for value in ('', 'short', 'z' * 64):
            self.secret_file.write_text(value, encoding='utf-8')
            self.assertEqual(self.client.get('/api/status', headers=self.signed()).status_code, 401)
        self.secret_file.unlink()
        self.assertEqual(self.client.get('/api/status', headers=self.signed()).status_code, 401)
        self.assertEqual(self.client.get('/api/status').status_code, 200)

    def test_dsm_cannot_mint_or_clear_independent_password_session(self):
        for path in ('/api/login', '/api/logout'):
            headers = self.signed(path, 'POST') | {'Origin': 'http://localhost'}
            response = self.client.post(path, json={'password': 'pw'}, headers=headers)
            self.assertEqual(response.status_code, 409)
            self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual(self.client.get('/api/status').status_code, 401)

    def test_dsm_mutations_preserve_exact_public_origin_host_validation(self):
        path, host = '/api/pause', 'quick.example:5001'
        signed = self.signed(path, 'POST', host=host)
        for origin in ('', 'https://evil.example', 'https://quick.example', 'https://quick.example:5001.evil'):
            response = self.client.post(path, headers=signed | {'Host': host, 'Origin': origin})
            self.assertEqual(response.status_code, 403)
        response = self.client.post(path, headers=signed | {'Host': host, 'Origin': 'https://' + host})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['paused'])

    def test_index_static_clip_event_and_preview_urls_use_verified_prefix(self):
        index = self.client.get('/', headers=self.signed('/'))
        self.assertEqual(index.status_code, 200)
        html = index.get_data(as_text=True)
        self.assertIn('name="app-base" content="' + PREFIX + '"', html)
        self.assertIn('name="auth-mode" content="dsm"', html)
        for asset in ('style.css', 'timeline.js', 'app.js'):
            self.assertIn(PREFIX + '/static/' + asset, html)
        self.assertIn('href="' + PREFIX + '/"', html)
        name = 'f' * 32 + '.jpg'
        (self.catalog.cache / name).write_bytes(b'jpeg')
        self.catalog.publish_chunk(self.clip['id'], self.clip['generation'], 60, [(3, .8, name)], thumbnail=name)
        clips = self.client.get('/api/clips', headers=self.signed('/api/clips')).json
        self.assertEqual(clips[0]['thumbnail'], PREFIX + '/api/media/' + name)
        events = self.client.get('/api/events', headers=self.signed('/api/events')).json
        self.assertEqual(events[0]['thumb'], PREFIX + '/api/media/' + name)
        job = self.catalog.enqueue_preview(self.clip['id'], 0)
        media = 'a' * 32 + '.mp4'
        (self.catalog.cache / media).write_bytes(b'preview')
        self.catalog.publish_preview(job, media)
        path = '/api/preview/' + job['id']
        self.assertEqual(self.client.get(path, headers=self.signed(path)).json['url'], PREFIX + '/api/media/' + media)
        direct = self.client.get('/').get_data(as_text=True)
        self.assertIn('name="app-base" content=""', direct)
        self.assertIn('name="auth-mode" content="password"', direct)

    def test_every_original_cached_media_and_static_request_validates_identity(self):
        self.password_login()
        for path in ('/', '/static/app.js', '/api/original/' + self.clip['id'], '/api/media/' + 'f' * 32 + '.jpg'):
            headers = self.signed(path, timestamp=int(time.time()) - 31)
            self.assertEqual(self.client.get(path, headers=headers).status_code, 401)
        path = '/api/original/' + self.clip['id']
        with self.client.get(path, headers=self.signed(path) | {'Range': 'bytes=2-5'}) as response:
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.data, b'2345')


if __name__ == '__main__':
    unittest.main()
