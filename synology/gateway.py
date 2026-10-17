"""DSM CGI authentication and bounded-memory loopback video gateway (Python 3.8)."""
import hashlib
import hmac
from html import escape
import http.client
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlsplit


MAX_BODY = 4096
CHUNK_SIZE = 64 * 1024
TOKEN_COOKIE = 'camera_dsm_token'
TOKEN_PATH = '/_dsm/token'
BOOTSTRAP_PATH = '/_dsm/bootstrap.js'
REQUEST_HEADERS = {
    'HTTP_HOST': 'Host', 'HTTP_ORIGIN': 'Origin', 'HTTP_RANGE': 'Range',
    'HTTP_IF_RANGE': 'If-Range', 'HTTP_IF_NONE_MATCH': 'If-None-Match',
    'HTTP_IF_MODIFIED_SINCE': 'If-Modified-Since', 'HTTP_ACCEPT': 'Accept',
    'HTTP_USER_AGENT': 'User-Agent', 'CONTENT_TYPE': 'Content-Type',
}
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate',
               'proxy-authorization', 'te', 'trailer', 'transfer-encoding',
               'upgrade', 'set-cookie'}


def valid_native_token(value):
    # RFC 6265 cookie-octet; excludes delimiters, controls and header injection.
    return isinstance(value, str) and re.fullmatch(r'[\x21\x23-\x2b\x2d-\x3a\x3c-\x5b\x5d-\x7e]{1,2048}', value) is not None


def authentication_environment(environ, candidate=None):
    """Transport the native token only to DSM; it never establishes an identity."""
    native = dict(environ)
    parts = native.get('HTTP_COOKIE', '').split(';')
    tokens = [part.partition('=')[2].strip() for part in parts
              if part.partition('=')[0].strip() == TOKEN_COOKIE]
    if tokens:
        native['HTTP_COOKIE'] = ';'.join(part for part in parts
            if part.partition('=')[0].strip() != TOKEN_COOKIE).strip()
    if candidate is not None:
        native['HTTP_X_SYNO_TOKEN'] = candidate
    elif 'HTTP_X_SYNO_TOKEN' not in native and len(tokens) == 1 and valid_native_token(tokens[0]):
        native['HTTP_X_SYNO_TOKEN'] = tokens[0]
    return native


def native_authenticate(environ, path):
    """Only DSM's native checker decides whether the current request is signed in."""
    try:
        result = subprocess.run([path], env=authentication_environment(environ), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=10, check=False)
        if result.returncode:
            return None
        user = result.stdout.decode('utf-8').strip()
        if user and len(user) <= 256 and not any(ord(c)<32 or ord(c)==127 for c in user):
            return user
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        pass
    return None


def reply(output, status, body, content_type='application/json; charset=utf-8', head=False, extra_headers=()):
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False).encode('utf-8')
    elif isinstance(body, str):
        body = body.encode('utf-8')
    headers = [('Content-Type', content_type), ('Cache-Control','no-store'),
               ('X-Content-Type-Options','nosniff'), ('Content-Length',str(len(body)))]
    write_headers(output, status, headers + list(extra_headers))
    if not head:
        output.write(body)
    output.flush()


def write_headers(output, status, headers):
    reason = http.client.responses.get(status, 'Response')
    lines = ['Status: %d %s' % (status, reason)]
    for name, value in headers:
        if '\r' in name+value or '\n' in name+value:
            continue
        lines.append(name+': '+value)
    output.write(('\r\n'.join(lines)+'\r\n\r\n').encode('latin-1'))
    output.flush()


def request_target(environ, prefix):
    raw = environ.get('REQUEST_URI', '')
    raw_path, sep, query = raw.partition('?')
    if not raw_path.startswith(prefix+'/'):
        raise LookupError()
    raw_path = raw_path[len(prefix):]
    path = unquote(raw_path, errors='strict')
    if any(ord(c)<32 or ord(c)==127 for c in path) or '\\' in path:
        raise ValueError()
    if any(part in ('.','..') for part in path.split('/')):
        raise ValueError()
    # Send the original ASCII-encoded URI; sign what Flask actually sees.
    target = raw_path+('?' + query if sep else '')
    target.encode('ascii')
    # Native tokens have a header/cookie transport, never an upstream URL.
    if any(name.lower() == 'synotoken' for name, _ in parse_qsl(query)):
        raise ValueError()
    host = environ.get('HTTP_HOST','')
    if not host or not re.fullmatch(r'[a-zA-Z0-9.\-:\[\]]+',host):
        raise ValueError()
    return target, path, query, host


def request_scheme(environ):
    # CGI server values describe the public request; forwarded headers are untrusted.
    if environ.get('HTTPS', '').lower() in ('on', '1', 'true'):
        return 'https'
    scheme = environ.get('REQUEST_SCHEME', 'http').lower()
    return scheme if scheme in ('http', 'https') else None


def read_body(environ, input_stream):
    try:
        length = int(environ.get('CONTENT_LENGTH') or '0')
        if length < 0 or environ.get('HTTP_TRANSFER_ENCODING'):
            raise ValueError()
    except ValueError:
        return None, 400
    if length > MAX_BODY:
        return None, 413
    body = input_stream.read(length) if length else b''
    return (body, None) if len(body) == length else (None, 400)


def bridge_token(environ, input_stream, output, config, host, query, authenticate):
    scheme = request_scheme(environ)
    if not scheme or environ.get('HTTP_ORIGIN') != scheme+'://'+host:
        return reply(output,403,{'error':'请求来源无效'})
    if query:
        return reply(output,400,{'error':'请求地址无效'})
    if environ.get('CONTENT_TYPE','').split(';',1)[0].strip().lower() != 'application/json':
        return reply(output,415,{'error':'请求格式无效'})
    body, error = read_body(environ,input_stream)
    if error:
        return reply(output,error,{'error':'请求长度无效'})
    try:
        data = json.loads(body)
        if not isinstance(data,dict) or set(data) != {'token'} or not valid_native_token(data['token']):
            raise ValueError()
    except (ValueError, UnicodeError, RecursionError):
        return reply(output,400,{'error':'请求格式无效'})
    native = authentication_environment(environ,data['token'])
    if not authenticate(native, config['authenticate_cgi']):
        return reply(output,401,{'error':'DSM 登录已过期，请重新登录','dsm_login':'/'})
    # Session-only native token transport for <video>/<img>, which cannot set headers.
    cookie = TOKEN_COOKIE+'='+data['token']+'; Path='+config['prefix']+'/; HttpOnly; SameSite=Strict'
    if scheme == 'https':
        cookie += '; Secure'
    return reply(output,200,{'ok':True},extra_headers=[('Set-Cookie',cookie)])


def bootstrap(output, prefix):
    body = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<title>录像回看 · DSM 登录</title>'
            '<p id="dsm-status">正在验证 DSM 登录…</p>'
            '<a id="dsm-signin" href="/?launchApp=com.xiaomi.camera.review">登录 DSM</a> '
            '<a id="dsm-open" hidden href="'+escape(prefix+'/',quote=True)+'">打开录像回看</a>'
            '<script src="'+escape(prefix+BOOTSTRAP_PATH,quote=True)+'" defer></script></html>')
    policy = "default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    return reply(output,200,body,'text/html; charset=utf-8',extra_headers=[
        ('Content-Security-Policy',policy),('Referrer-Policy','no-referrer')])


def handle(environ, input_stream, output, config, authenticate=native_authenticate):
    method = environ.get('REQUEST_METHOD','GET')
    head = method=='HEAD'
    try:
        prefix=config['prefix']
        target, path, query, host=request_target(environ,prefix)
    except LookupError:
        return reply(output,404,{'error':'页面不存在'},head=head)
    except (ValueError, KeyError, UnicodeError):
        return reply(output,400,{'error':'请求地址无效'},head=head)
    if path == BOOTSTRAP_PATH:
        if method not in ('GET','HEAD'):
            return reply(output,405,{'error':'请求方法不支持'},head=head)
        try:
            script = Path(__file__).with_name('bootstrap.js').read_bytes()
        except OSError:
            return reply(output,503,{'error':'录像入口尚未就绪'},head=head)
        return reply(output,200,script,'application/javascript; charset=utf-8',head=head)
    if path == TOKEN_PATH:
        if method != 'POST':
            return reply(output,405,{'error':'请求方法不支持'},head=head)
        return bridge_token(environ,input_stream,output,config,host,query,authenticate)
    if method not in ('GET','HEAD','POST') or (method=='POST' and not path.startswith('/api/')):
        return reply(output,405,{'error':'请求方法不支持'},head=head)
    user = authenticate(authentication_environment(environ), config['authenticate_cgi'])
    if not user:
        if method == 'GET' and path == '/':
            return bootstrap(output,prefix)
        if path.startswith('/api/'):
            return reply(output,401,{'error':'DSM 登录已过期，请重新登录','dsm_login':'/'},head=head)
        body = '<!doctype html><meta charset="utf-8"><title>请登录 DSM</title><p>请先登录群晖 DSM，再打开录像回看。</p><a href="/?launchApp=com.xiaomi.camera.review">登录 DSM</a>'
        return reply(output,401,body,'text/html; charset=utf-8',head=head)
    body, error = read_body(environ,input_stream)
    if error:
        return reply(output,error,{'error':'请求长度无效'},head=head)
    connection=None
    headers_sent=False
    try:
        secret=Path(config['secret_file']).read_text(encoding='utf-8').strip().encode('utf-8')
        if not re.fullmatch(b'[0-9a-fA-F]{64}',secret):
            raise ValueError('invalid gateway secret')
        upstream=urlsplit(config['upstream'])
        if upstream.scheme!='http' or upstream.hostname!='127.0.0.1' or upstream.path not in ('','/') or upstream.query or upstream.username:
            raise ValueError('upstream must be fixed loopback HTTP')
        headers={name:environ[key] for key,name in REQUEST_HEADERS.items() if key in environ}
        if any('\r' in v or '\n' in v for v in headers.values()):
            raise ValueError('invalid request header')
        stamp=str(int(time.time()))
        data='\n'.join([stamp,user,method,path,query,prefix,host]).encode('utf-8')
        headers.update({'X-Camera-DSM-User':quote(user,safe=''),'X-Camera-DSM-Time':stamp,
                        'X-Camera-DSM-Prefix':prefix,
                        'X-Camera-DSM-Signature':hmac.new(secret,data,hashlib.sha256).hexdigest(),
                        'Connection':'close'})
        connection=http.client.HTTPConnection('127.0.0.1',upstream.port or 80,timeout=90)
        connection.request(method,target,body=body,headers=headers)
        response=connection.getresponse()
        blocked=HOP_HEADERS | {x.strip().lower() for x in response.getheader('Connection','').split(',')}
        response_headers=[]
        for name,value in response.getheaders():
            if name.lower() in blocked:
                continue
            if name.lower()=='location':
                if not value.startswith('/') or value.startswith('//'):
                    continue
                value=prefix+value
            response_headers.append((name,value))
        response_headers.append(('X-Accel-Buffering','no'))
        write_headers(output,response.status,response_headers)
        headers_sent=True
        if not head:
            while True:
                chunk=response.read1(CHUNK_SIZE)
                if not chunk:
                    break
                output.write(chunk)
                output.flush()
    except (BrokenPipeError, ConnectionResetError):
        pass  # Browser may cancel a seek or close the player.
    except (OSError, ValueError, http.client.HTTPException):
        if not headers_sent:
            reply(output,503,{'error':'录像服务暂时不可用，请稍后重试'},head=head)
    finally:
        if connection:
            connection.close()


def main(config_path):
    try:
        config=json.loads(Path(config_path).read_text(encoding='utf-8'))
        handle(os.environ,sys.stdin.buffer,sys.stdout.buffer,config)
    except (OSError, ValueError, KeyError):
        reply(sys.stdout.buffer,503,{'error':'录像入口尚未就绪'})
