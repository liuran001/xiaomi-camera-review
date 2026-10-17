"""CGI boundary tests with an actual loopback HTTP upstream."""
import hashlib
import hmac
import io
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import SimpleCookie

try:
    from synology import gateway
except ImportError:
    gateway = None


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(gateway, 'streaming CGI gateway has not been implemented')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.secret = Path(self.temp.name)/'secret'
        self.secret.write_text('a'*64)
        self.config = dict(prefix='/webman/3rdparty/XiaomiCameraReview/index.cgi',
                           secret_file=str(self.secret), upstream='http://127.0.0.1:8090',
                           authenticate_cgi='/trusted/authenticate.cgi')
        self.env = dict(REQUEST_METHOD='GET', REQUEST_URI=self.config['prefix']+'/api/status?q=%E4%B8%AD',
                        QUERY_STRING='q=%E4%B8%AD', HTTP_HOST='example.quickconnect.to',
                        REMOTE_ADDR='127.0.0.1', HTTP_COOKIE='id=private-dsm-cookie')

    def call(self, user='dsm-user', body=b'', authenticate=None):
        output = io.BytesIO()
        gateway.handle(self.env, io.BytesIO(body), output, self.config,
                       authenticate=authenticate or (lambda env, path:user))
        header, body = output.getvalue().split(b'\r\n\r\n', 1)
        return header.decode(), body

    def upstream(self, responder):
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.received = (self.path, dict(self.headers))
                responder(self)
            do_HEAD = do_GET
            def do_POST(self):
                outer.post_body = self.rfile.read(int(self.headers.get('Content-Length','0')))
                self.do_GET()
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.config['upstream'] = 'http://127.0.0.1:'+str(server.server_port)

    def test_no_session_denies_api_and_bootstraps_only_index_navigation(self):
        head, body = self.call(user='')
        self.assertIn('401', head)
        self.assertIn('error', json.loads(body))
        self.env['REQUEST_URI'] = self.config['prefix']+'/'
        head, body = self.call(user=None)
        self.assertIn('200', head)
        self.assertIn("default-src 'none'", head)
        self.assertIn("script-src 'self'", head)
        self.assertIn("connect-src 'self'", head)
        self.assertIn("frame-ancestors 'none'", head)
        self.assertIn('Referrer-Policy: no-referrer', head)
        self.assertIn(b'/_dsm/bootstrap.js', body)
        self.assertIn(b'href="/', body)
        self.assertNotIn(b'private-dsm-cookie', body)
        self.env['REQUEST_URI'] = self.config['prefix']+'/static/app.js'
        self.assertIn('401', self.call(user=None)[0])

    def prepare_bridge(self, token='native-token'):
        self.env.update(REQUEST_METHOD='POST', REQUEST_URI=self.config['prefix']+'/_dsm/token',
                        QUERY_STRING='', REQUEST_SCHEME='https', HTTPS='on', SERVER_PORT='5001',
                        HTTP_ORIGIN='https://example.quickconnect.to', CONTENT_TYPE='application/json',
                        HTTP_COOKIE='_SSID=native-dsm-cookie')
        body=json.dumps({'token':token}).encode()
        self.env['CONTENT_LENGTH']=str(len(body))
        return body

    def test_bootstrap_script_is_public_but_contains_no_request_secrets(self):
        self.env['REQUEST_URI']=self.config['prefix']+'/_dsm/bootstrap.js'
        head, body=self.call(user=None)
        self.assertIn('200',head)
        self.assertIn('application/javascript',head)
        self.assertIn('Cache-Control: no-store',head)
        self.assertIn(b'api=SYNO.API.Auth&version=6&method=token',body)
        self.assertNotIn(b'private-dsm-cookie',body)
        self.assertNotIn(b'a'*64,body)
        self.env['REQUEST_METHOD']='POST'
        self.assertIn('405',self.call()[0])

    def test_bridge_rejects_cross_origin_missing_origin_and_scheme_confusion(self):
        body=self.prepare_bridge()
        def forbidden(env,path):
            self.fail('origin rejection must happen before native authentication')
        for origin in ('https://evil.test','http://example.quickconnect.to',
                       'https://example.quickconnect.to.evil.test','null','',
                       'https://example.quickconnect.to:443'):
            with self.subTest(origin=origin):
                self.env['HTTP_ORIGIN']=origin
                self.assertIn('403',self.call(body=body,authenticate=forbidden)[0])
        self.env.update(HTTP_ORIGIN='https://example.quickconnect.to', HTTPS='off',
                        REQUEST_SCHEME='http', HTTP_X_FORWARDED_PROTO='https')
        self.assertIn('403',self.call(body=body,authenticate=forbidden)[0])

    def test_bridge_validates_candidate_with_current_native_cookie_and_sets_session_cookie(self):
        body=self.prepare_bridge()
        checked=[]
        def native(env,path):
            checked.append(env)
            return 'ordinary-user' if env.get('HTTP_COOKIE')=='_SSID=native-dsm-cookie' and env.get('HTTP_X_SYNO_TOKEN')=='native-token' else None
        self.env['HTTP_COOKIE']+='; camera_dsm_token=expired-token'
        head, payload=self.call(body=body,authenticate=native)
        self.assertIn('200',head)
        self.assertEqual({'ok':True},json.loads(payload))
        cookie=SimpleCookie()
        cookie.load(next(line.partition(': ')[2] for line in head.split('\r\n') if line.startswith('Set-Cookie:')))
        morsel=cookie['camera_dsm_token']
        self.assertEqual('native-token',morsel.value)
        self.assertEqual(self.config['prefix']+'/',morsel['path'])
        self.assertTrue(morsel['httponly'])
        self.assertTrue(morsel['secure'])
        self.assertEqual('Strict',morsel['samesite'])
        for name in ('domain','expires','max-age'):
            self.assertFalse(morsel[name])
        self.assertEqual(1,len(checked))
        self.assertNotIn(b'native-token',payload)
        self.assertNotIn(b'ordinary-user',payload)

    def test_bridge_http_cookie_is_usable_on_lan_and_does_not_trust_forwarded_scheme(self):
        body=self.prepare_bridge()
        self.env.update(HTTP_HOST='nas.test:5000', HTTP_ORIGIN='http://nas.test:5000',
                        HTTPS='off', REQUEST_SCHEME='http', HTTP_X_FORWARDED_PROTO='https')
        head,_=self.call(body=body)
        self.assertIn('200',head)
        self.assertNotIn('; Secure',head)

    def test_wrong_token_or_revoked_native_cookie_never_issues_transport(self):
        for cookie,token in (('_SSID=native-dsm-cookie','wrong-token'),
                             ('_SSID=revoked','native-token')):
            body=self.prepare_bridge(token)
            self.env['HTTP_COOKIE']=cookie
            def native(env,path):
                return 'user' if env.get('HTTP_COOKIE')=='_SSID=native-dsm-cookie' and env.get('HTTP_X_SYNO_TOKEN')=='native-token' else None
            head,payload=self.call(body=body,authenticate=native)
            self.assertIn('401',head)
            self.assertNotIn('Set-Cookie:',head)
            self.assertNotIn(token.encode(),payload)

    def test_bridge_body_is_bounded_and_fixed_json_only(self):
        body=self.prepare_bridge()
        def forbidden(env,path):
            self.fail('bad input must not reach native authentication')
        for length,status in (('4097','413'),('-1','400'),('invalid','400')):
            self.env['CONTENT_LENGTH']=length
            self.assertIn(status,self.call(body=body,authenticate=forbidden)[0])
        for payload in (b'{}',b'[]',b'{',b'{"token":123}',b'{"token":"bad;cookie"}',
                        b'{"token":"native-token","url":"http://evil.test"}',b'{"token":""}'):
            self.env['CONTENT_LENGTH']=str(len(payload))
            self.assertIn('400',self.call(body=payload,authenticate=forbidden)[0])
        self.env['CONTENT_LENGTH']=str(len(body)+1)
        self.assertIn('400',self.call(body=body,authenticate=forbidden)[0])
        self.env['CONTENT_LENGTH']=str(len(body))
        self.env['CONTENT_TYPE']='text/plain'
        self.assertIn('415',self.call(body=body,authenticate=forbidden)[0])
        self.env['CONTENT_TYPE']='application/json'
        self.env['HTTP_TRANSFER_ENCODING']='chunked'
        self.assertIn('400',self.call(body=body,authenticate=forbidden)[0])
        self.env.pop('HTTP_TRANSFER_ENCODING')
        self.env['REQUEST_URI']+='?token=forbidden'
        self.assertIn('400',self.call(body=body,authenticate=forbidden)[0])

    def test_transport_native_checks_every_api_media_thumbnail_and_static_request(self):
        def respond(handler):
            handler.send_response(200)
            handler.send_header('Content-Length','2')
            handler.end_headers()
            handler.wfile.write(b'{}')
        self.upstream(respond)
        self.env.update(HTTP_COOKIE='_SSID=native-dsm-cookie; camera_dsm_token=native-token',
                        QUERY_STRING='')
        checked=[]
        def native(env,path):
            checked.append(dict(env))
            return 'ordinary-user' if env.get('HTTP_COOKIE')=='_SSID=native-dsm-cookie' and env.get('HTTP_X_SYNO_TOKEN')=='native-token' else None
        for path in ('/api/status','/api/media/clip.mp4','/api/original/clip','/api/thumb/clip.jpg','/static/app.js'):
            self.env['REQUEST_URI']=self.config['prefix']+path
            head,_=self.call(authenticate=native)
            self.assertIn('200',head)
            self.assertNotIn('Cookie',self.received[1])
            self.assertNotIn('X-SYNO-TOKEN',self.received[1])
            self.assertNotIn('native-token',str(self.received))
        self.assertEqual(5,len(checked))
        self.env['HTTP_COOKIE']='_SSID=revoked; camera_dsm_token=native-token'
        self.env['REQUEST_URI']=self.config['prefix']+'/api/status'
        self.assertIn('401',self.call(authenticate=native)[0])
        self.assertEqual(6,len(checked))

    def test_explicit_native_header_takes_precedence_over_transport_cookie(self):
        self.env.update(HTTP_COOKIE='_SSID=native-dsm-cookie; camera_dsm_token=native-token',
                        HTTP_X_SYNO_TOKEN='invalid-explicit-token')
        def native(env,path):
            self.assertEqual('invalid-explicit-token',env.get('HTTP_X_SYNO_TOKEN'))
            self.assertEqual('_SSID=native-dsm-cookie',env['HTTP_COOKIE'])
            return None
        self.assertIn('401',self.call(authenticate=native)[0])

    def test_rejects_paths_outside_prefix_traversal_and_unsafe_methods(self):
        for tail in ['/../data', '/%2e%2e/data', '/%5cdata', '/x%00']:
            with self.subTest(tail=tail):
                self.env['REQUEST_URI']=self.config['prefix']+tail
                self.assertIn('400', self.call()[0])
        self.env['REQUEST_URI']='/other/api/status'
        self.assertIn('404', self.call()[0])
        self.env['REQUEST_URI']=self.config['prefix']+'/api/status'
        self.env['REQUEST_METHOD']='DELETE'
        self.assertIn('405', self.call()[0])

    def test_signed_request_range_and_no_cookie_leak(self):
        payload=b'video-bytes'
        def respond(handler):
            handler.send_response(206)
            handler.send_header('Content-Type','video/mp4')
            handler.send_header('Content-Length',str(len(payload)))
            handler.send_header('Content-Range','bytes 0-10/100')
            handler.send_header('Set-Cookie','forbidden=1')
            handler.end_headers()
            handler.wfile.write(payload)
        self.upstream(respond)
        self.env.update(HTTP_RANGE='bytes=0-10', HTTP_ORIGIN='https://example.quickconnect.to',
                        HTTP_X_CAMERA_DSM_USER='forged', HTTP_AUTHORIZATION='private',
                        HTTP_X_FORWARDED_HOST='evil.test')
        head, body=self.call()
        self.assertIn('206',head)
        self.assertIn('Content-Range: bytes 0-10/100',head)
        self.assertNotIn('Set-Cookie',head)
        self.assertEqual(payload,body)
        path,headers=self.received
        self.assertEqual('/api/status?q=%E4%B8%AD',path)
        self.assertEqual('bytes=0-10',headers['Range'])
        self.assertNotIn('Cookie',headers)
        self.assertNotIn('Authorization',headers)
        self.assertNotIn('X-Forwarded-Host',headers)
        self.assertEqual('dsm-user',headers['X-Camera-DSM-User'])
        stamp=headers['X-Camera-DSM-Time']
        data='\n'.join([stamp,'dsm-user','GET','/api/status','q=%E4%B8%AD',self.config['prefix'],self.env['HTTP_HOST']])
        self.assertEqual(hmac.new(b'a'*64,data.encode(),hashlib.sha256).hexdigest(),headers['X-Camera-DSM-Signature'])

    def test_bounded_json_post_preserves_origin(self):
        def respond(handler):
            handler.send_response(200)
            handler.send_header('Content-Length','2')
            handler.end_headers()
            handler.wfile.write(b'{}')
        self.upstream(respond)
        self.env.update(REQUEST_METHOD='POST', CONTENT_TYPE='application/json', CONTENT_LENGTH='2',
                        HTTP_ORIGIN='https://example.quickconnect.to')
        self.assertIn('200',self.call(body=b'{}')[0])
        self.assertEqual(b'{}',self.post_body)
        self.assertEqual(self.env['HTTP_ORIGIN'],self.received[1]['Origin'])
        self.env['CONTENT_LENGTH']='4097'
        self.assertIn('413',self.call(body=b'{}')[0])
        self.env['CONTENT_LENGTH']='-1'
        self.assertIn('400',self.call(body=b'{}')[0])

    def test_native_auth_failure_is_closed_and_environment_inherited(self):
        from unittest.mock import patch
        import subprocess
        with patch('subprocess.run',side_effect=subprocess.TimeoutExpired('auth',10)):
            self.assertIsNone(gateway.native_authenticate(self.env,'/trusted/authenticate.cgi'))
        result = subprocess.CompletedProcess([],0,stdout=b'ordinary-user\n',stderr=b'')
        with patch('subprocess.run',return_value=result) as run:
            self.assertEqual('ordinary-user',gateway.native_authenticate(self.env,'/trusted/authenticate.cgi'))
            self.assertEqual(self.env,run.call_args.kwargs['env'])

    def test_native_subprocess_receives_only_dsm_cookies_and_native_token_header(self):
        from unittest.mock import patch
        import subprocess
        self.env['HTTP_COOKIE']='_SSID=native-cookie; camera_dsm_token=native-token; theme=dark'
        original=dict(self.env)
        result=subprocess.CompletedProcess([],0,stdout=b'ordinary-user\n',stderr=b'')
        with patch('subprocess.run',return_value=result) as run:
            self.assertEqual('ordinary-user',gateway.native_authenticate(self.env,'/trusted/authenticate.cgi'))
            native=run.call_args.kwargs['env']
            self.assertEqual('_SSID=native-cookie; theme=dark',native['HTTP_COOKIE'])
            self.assertEqual('native-token',native['HTTP_X_SYNO_TOKEN'])
            self.assertEqual(subprocess.DEVNULL,run.call_args.kwargs['stdin'])
        self.assertEqual(original,self.env,'the CGI request environment must not be mutated')

    def test_oversized_bridge_body_is_rejected_without_reading_input(self):
        class Unreadable:
            def read(self,*args):
                raise AssertionError('oversized bridge body must never be read')
        self.prepare_bridge()
        self.env['CONTENT_LENGTH']='4097'
        output=io.BytesIO()
        gateway.handle(self.env,Unreadable(),output,self.config,
                       authenticate=lambda env,path:self.fail('must reject before native authentication'))
        self.assertIn(b'Status: 413',output.getvalue())

    def test_deeply_nested_bridge_json_fails_as_bad_request(self):
        self.prepare_bridge()
        payload=b'['*1500+b'0'+b']'*1500
        self.env['CONTENT_LENGTH']=str(len(payload))
        self.assertIn('400',self.call(body=payload)[0])

    def test_native_token_query_is_never_forwarded(self):
        for query in ('SynoToken=native-token','%53ynoToken=native-token','synotoken=native-token'):
            self.env['REQUEST_URI']=self.config['prefix']+'/api/status?'+query
            self.assertIn('400',self.call()[0])

    def test_invalid_secret_and_upstream_fail_without_sensitive_details(self):
        self.secret.write_text('broken')
        head,body=self.call()
        self.assertIn('503',head)
        self.assertNotIn(str(self.secret).encode(),body)

    def test_unicode_native_user_is_ascii_encoded_on_wire_but_signed_as_utf8(self):
        from urllib.parse import quote
        def respond(handler):
            handler.send_response(200)
            handler.send_header('Content-Length','2')
            handler.end_headers()
            handler.wfile.write(b'{}')
        self.upstream(respond)
        user='测试用户%'
        head,body=self.call(user=user)
        self.assertIn('200',head)
        headers=self.received[1]
        self.assertEqual(quote(user,safe=''),headers['X-Camera-DSM-User'])
        payload='\n'.join([headers['X-Camera-DSM-Time'],user,'GET','/api/status','q=%E4%B8%AD',self.config['prefix'],self.env['HTTP_HOST']])
        self.assertEqual(hmac.new(b'a'*64,payload.encode(),hashlib.sha256).hexdigest(),headers['X-Camera-DSM-Signature'])

    def test_head_has_headers_without_body(self):
        def respond(handler):
            handler.send_response(200)
            handler.send_header('Content-Length','100')
            handler.end_headers()
        self.upstream(respond)
        self.env['REQUEST_METHOD']='HEAD'
        head,body=self.call()
        self.assertIn('Content-Length: 100',head)
        self.assertEqual(b'',body)

    def test_streams_first_chunk_before_upstream_finishes(self):
        class ObservedOutput(io.BytesIO):
            def write(self, value):
                result=super().write(value)
                if b'first-frame' in value:
                    first_seen.set()
                return result
        first_seen=threading.Event()
        def respond(handler):
            handler.send_response(200)
            handler.send_header('Content-Length','22')
            handler.end_headers()
            handler.wfile.write(b'first-frame')
            handler.wfile.flush()
            self.assertTrue(first_seen.wait(2),'gateway buffered whole video')
            handler.wfile.write(b'last--frame')
        self.upstream(respond)
        output=ObservedOutput()
        gateway.handle(self.env,io.BytesIO(),output,self.config,authenticate=lambda env,path:'dsm-user')
        self.assertTrue(first_seen.is_set())
        self.assertTrue(output.getvalue().endswith(b'first-framelast--frame'))


if __name__=='__main__':
    unittest.main()
