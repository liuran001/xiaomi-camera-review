"""Authenticated same-origin API. No endpoint accepts a filesystem path."""
import hmac
import math
import os
import threading
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, g, jsonify, render_template_string, request, send_file, session

from .catalog import MEDIA_NAME, catalog_from_env
from .dsm_auth import authenticate as authenticate_dsm, has_identity


def public_error(value):
    if value and ('timed out' in str(value).lower() or 'timeoutexpired' in str(value).lower()):
        return '生成画面超时，后台会自动重试'
    return value


def create_app(catalog=None, password=None, secret=None):
    password = password if password is not None else os.environ.get('APP_PASSWORD')
    secret = secret if secret is not None else os.environ.get('SESSION_SECRET')
    if not password or not secret:
        raise RuntimeError('APP_PASSWORD and SESSION_SECRET are required')
    cat = catalog or catalog_from_env()
    app = Flask(__name__, static_folder='../static', static_url_path='/static')
    app.secret_key = secret
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      SESSION_COOKIE_SECURE=os.environ.get('COOKIE_SECURE', '0') == '1',
                      MAX_CONTENT_LENGTH=4096)
    attempts, lock = {}, threading.Lock()
    dsm_secret_file = os.environ.get('DSM_GATEWAY_SECRET_FILE')
    dsm_prefix = os.environ.get('DSM_APP_PREFIX', '')

    @app.before_request
    def guard():
        g.dsm_user = None
        g.app_base = ''
        if has_identity(request.headers):
            g.dsm_user = authenticate_dsm(request, dsm_secret_file, dsm_prefix)
            if not g.dsm_user:
                return jsonify(error='DSM 登录已失效，请重新登录 DSM', auth='dsm', login_url='/?launchApp=com.xiaomi.camera.review'), 401
            g.app_base = dsm_prefix
        if request.path.startswith('/api/'):
            if request.method not in ('GET', 'HEAD'):
                origin = request.headers.get('Origin')
                parsed = urlsplit(origin or '')
                if parsed.scheme not in ('http', 'https') or parsed.netloc != request.host:
                    return jsonify(error='同源校验失败'), 403
                if request.method == 'POST' and request.path not in ('/api/pause', '/api/resume', '/api/logout') and not request.is_json:
                    return jsonify(error='请使用 JSON 请求'), 415
            if g.dsm_user and request.path in ('/api/login', '/api/logout'):
                return jsonify(error='请通过 DSM 管理登录', auth='dsm', login_url='/?launchApp=com.xiaomi.camera.review'), 409
            if request.path != '/api/login' and not g.dsm_user and not session.get('authenticated'):
                return jsonify(error='请先登录'), 401

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self'; media-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'"
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(LookupError)
    def missing(error):
        return jsonify(error=str(error)), 404

    @app.errorhandler(RuntimeError)
    def conflict(error):
        return jsonify(error=str(error)), 409

    def filters():
        day, channel = request.args.get('date'), request.args.get('channel')
        if day:
            try:
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError()
            except ValueError:
                raise ValueError('invalid date; use YYYY-MM-DD')
        if channel and len(channel) > 512:
            raise ValueError('invalid channel')
        return day, channel

    def public_clip(clip):
        return {**{k: clip[k] for k in ('id', 'generation', 'camera', 'channel', 'date', 'start', 'end', 'duration', 'media_duration', 'status', 'completed', 'error', 'thumbnail_error')},
                'playable_duration': cat.playable_duration(clip),
                'error': public_error(clip['error']), 'thumbnail_error': public_error(clip['thumbnail_error']),
                'progress': round(100 * clip['completed'] / cat.playable_duration(clip), 1),
                'thumbnail': g.app_base + '/api/media/' + clip['thumbnail'] if clip['thumbnail'] else None}

    def public_job(job):
        clip = cat.clip(job['clip_id'])
        if clip['generation'] != job['generation']:
            raise RuntimeError('source changed; request a new preview')
        return {**{k: job[k] for k in ('id', 'clip_id', 'generation', 'offset', 'status', 'error')},
                'duration': max(0, min(job['requested_seconds'], cat.playable_duration(clip) - job['offset'])),
                'playable_duration': cat.playable_duration(clip), 'media_duration': clip['media_duration'],
                'error': public_error(job['error']),
                'url': g.app_base + '/api/media/' + job['media'] if job['status'] == 'ready' and job['media'] else None}

    @app.get('/')
    def index():
        return render_template_string((Path(app.static_folder) / 'index.html').read_text(encoding='utf-8'),
                                      app_base=g.app_base, auth_mode='dsm' if g.dsm_user else 'password')

    @app.post('/api/login')
    def login():
        address, now = request.remote_addr, time.monotonic()
        with lock:
            # Bound memory even when clients rotate addresses; never trust forwarded IP.
            for key in list(attempts):
                if attempts[key][1] <= now:
                    del attempts[key]
            count, until = attempts.get(address, (0, now + 60))
            if count >= 8:
                return jsonify(error='尝试过多，请稍后再试'), 429
            body = request.get_json(silent=True)
            supplied = body.get('password', '') if isinstance(body, dict) else ''
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), password.encode()):
                if len(attempts) >= 2048 and address not in attempts:
                    return jsonify(error='尝试过多，请稍后再试'), 429
                attempts[address] = (count + 1, until)
                return jsonify(error='密码不正确'), 401
            attempts.pop(address, None)
        session.clear()
        session['authenticated'] = True
        return jsonify(ok=True)

    @app.post('/api/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get('/api/status')
    def status():
        with cat.connect() as db:
            counts = {r['status']: r['n'] for r in db.execute("SELECT status,COUNT(*) n FROM clips WHERE status!='missing' GROUP BY status")}
            progress = db.execute("SELECT COALESCE(SUM(completed),0),COALESCE(SUM(MIN(duration,COALESCE(media_duration,duration))),0),COALESCE(SUM(duration),0) FROM clips WHERE status!='missing'").fetchone()
            queue = {r['status']: r['n'] for r in db.execute('SELECT status,COUNT(*) n FROM previews GROUP BY status')}
            errors = [dict(r) for r in db.execute("SELECT id,error,retry_at FROM clips WHERE error IS NOT NULL ORDER BY start DESC LIMIT 20")]
            cover_errors = [dict(r) for r in db.execute("SELECT id,thumbnail_error AS error,thumbnail_retry_at AS retry_at FROM clips WHERE thumbnail_error IS NOT NULL ORDER BY start DESC LIMIT 20")]
        heartbeat = cat.state('heartbeat')
        for error in errors + cover_errors:
            error['error'] = public_error(error['error'])
        return jsonify(clip_counts=counts, clip_count=sum(counts.values()), completed_seconds=progress[0], total_seconds=progress[1],
                       queue=queue, paused=cat.state('paused', False), heartbeat=heartbeat,
                       worker_alive=bool(heartbeat and time.time() - heartbeat < 180), worker_error=public_error(cat.state('worker_error')),
                       last_scan=cat.state('last_scan'), scan_error=cat.state('scan_error'), source_timezone=cat.source_tz, errors=errors, thumbnail_errors=cover_errors,
                       filename_seconds=progress[2],
                       detection='关键帧抽样人物出现，时间可能偏移，非完整运动跟踪')

    @app.get('/api/days')
    def days():
        return jsonify(cat.days())

    @app.get('/api/channels')
    def channels():
        with cat.connect() as db:
            return jsonify([r[0] for r in db.execute("SELECT DISTINCT channel FROM clips WHERE status!='missing' ORDER BY channel")])

    @app.get('/api/clips')
    def clips():
        return jsonify([public_clip(c) for c in cat.clips(*filters())])

    @app.get('/api/events')
    def events():
        return jsonify([{**{k: e[k] for k in ('id', 'clip_id', 'start_offset', 'end_offset', 'score', 'start', 'channel')},
                         'thumb': g.app_base + '/api/media/' + e['thumb'] if e['thumb'] else None} for e in cat.events(*filters())])

    @app.get('/api/original/<ident>')
    def original(ident):
        clip = cat.clip(ident)
        if clip['status'] == 'unstable':
            raise RuntimeError('source is still changing')
        return send_file(cat.current_source(clip), mimetype='video/mp4', conditional=True)

    @app.post('/api/preview')
    def preview():
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            raise ValueError('expected JSON object')
        offset = data.get('offset')
        if isinstance(offset, bool) or not isinstance(offset, (float, int)) or not math.isfinite(offset):
            raise ValueError('offset must be finite seconds')
        return jsonify(public_job(cat.enqueue_preview(data.get('clip_id'), offset, seconds=data.get('seconds', 30), prefetch=data.get('prefetch', False), reuse=data.get('reuse', False)))), 202

    @app.get('/api/preview/<ident>')
    def preview_status(ident):
        job = cat.preview(ident)
        if not cat.valid_generation(job['clip_id'], job['generation']):
            raise RuntimeError('source changed; request a new preview')
        return jsonify(public_job(job))

    @app.get('/api/media/<name>')
    def media(name):
        if not MEDIA_NAME.fullmatch(name):
            raise LookupError('media not found')
        # Old generation cache remains unreadable immediately after scan. Match only
        # a current catalog reference, never treat knowledge of a filename as access.
        with cat.connect() as db:
            row = db.execute('''SELECT c.* FROM clips c WHERE c.thumbnail=?
                UNION SELECT c.* FROM clips c JOIN events e ON e.clip_id=c.id AND e.generation=c.generation WHERE e.thumb=?
                UNION SELECT c.* FROM clips c JOIN previews p ON p.clip_id=c.id AND p.generation=c.generation WHERE p.media=? AND p.status='ready' LIMIT 1''', (name, name, name)).fetchone()
        path = cat.readable_media(name)
        if not row or path is None:
            raise LookupError('media not found')
        cat.current_source(dict(row))
        if name.endswith('.mp4'):
            os.utime(path, None)
        return send_file(path, conditional=True)

    @app.post('/api/pause')
    def pause():
        cat.set_state('paused', True)
        return jsonify(paused=True)

    @app.post('/api/resume')
    def resume():
        cat.set_state('paused', False)
        return jsonify(paused=False)

    return app


app = create_app()
