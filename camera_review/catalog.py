"""SQLite catalog; short transactions, independent connections, immutable generations."""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import stat as stat_mode
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

NAME = re.compile(r'^(?P<channel>\d{2})_(?P<start>\d{14})_(?P<end>\d{14})\.mp4$')
IDENT = re.compile(r'^[0-9a-f]{32}$')
MEDIA_NAME = re.compile(r'^[0-9a-f]{32}\.(?:mp4|jpg)$')
SEEK_NAME = re.compile(r'^[0-9a-f]{32}-[0-9a-f]{32}\.mp4$')
SEEK_PART = re.compile(r'^[0-9a-f]{32}-[0-9a-f]{32}\.part\.mp4$')
SEEK_CACHE_LIMIT = 512 * 1024 ** 2
PLAYBACK_CACHE_LIMIT = 1536 * 1024 ** 2
BEIJING = timezone(timedelta(hours=8))


def day_bounds(date):
    start = datetime.strptime(date, '%Y-%m-%d').replace(tzinfo=BEIJING).timestamp()
    return start, start + 86400


def safe_source(root, relative):
    root = Path(root).absolute()
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValueError('invalid source path')
    # Check the mount path too; resolve containment alone would permit in-root links.
    for part in [root, *root.parents]:
        if part.is_symlink():
            raise ValueError('symlink root forbidden')
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('symlink forbidden')
    try:
        current.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError('source escapes root') from exc
    return current


def parse_recording(relative, source_tz='Asia/Shanghai'):
    relative = Path(relative)
    if any(p.startswith(('.', '@')) or p.lower() == '#recycle' for p in relative.parts):
        return None
    match = NAME.fullmatch(relative.name)
    if not match:
        return None
    try:
        start, end = [datetime.strptime(match[k], '%Y%m%d%H%M%S').replace(tzinfo=ZoneInfo(source_tz)) for k in ('start', 'end')]
    except ValueError:
        return None
    if end <= start:
        return None
    camera = relative.parent.as_posix()
    return dict(id=hashlib.sha256(relative.as_posix().encode()).hexdigest()[:32], relative=relative.as_posix(),
                camera=camera, channel=f'{camera} / {match["channel"]}', date=start.date().isoformat(),
                start=start.timestamp(), end=end.timestamp(), duration=(end - start).total_seconds())


class Catalog:
    def __init__(self, recordings, data, source_tz='Asia/Shanghai', stable_seconds=180, complete_file_bytes=0):
        self.root, self.data = Path(recordings), Path(data)
        self.data.mkdir(parents=True, exist_ok=True)
        self.cache = self.data / 'cache'
        self.cache.mkdir(exist_ok=True)
        self.seek_cache = self.data / 'seek-cache'
        self.seek_cache.mkdir(exist_ok=True)
        self.db = self.data / 'catalog.sqlite3'
        self.source_tz, self.stable_seconds = source_tz, stable_seconds
        self.complete_file_bytes = complete_file_bytes
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS clips (
                  id TEXT PRIMARY KEY, relative TEXT NOT NULL, camera TEXT, channel TEXT, date TEXT,
                  start REAL, end REAL, duration REAL, size INTEGER, mtime INTEGER, generation TEXT,
                  observations INTEGER, status TEXT, completed REAL DEFAULT 0, thumbnail TEXT,
                  error TEXT, attempts INTEGER DEFAULT 0, retry_at REAL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY, clip_id TEXT, generation TEXT, start_offset REAL,
                  end_offset REAL, first_hit REAL, last_hit REAL, score REAL, thumb TEXT);
                CREATE TABLE IF NOT EXISTS previews (
                  id TEXT PRIMARY KEY, clip_id TEXT, generation TEXT, offset REAL, status TEXT,
                  media TEXT, error TEXT, created REAL, UNIQUE(clip_id,generation,offset));
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
                CREATE INDEX IF NOT EXISTS clips_date ON clips(date,channel,start);
                CREATE INDEX IF NOT EXISTS events_clip ON events(clip_id,generation);
            ''')
            db.execute('BEGIN IMMEDIATE')
            existing = {r[1] for r in db.execute('PRAGMA table_info(clips)')}
            for name, sql_type in [('stable_since', 'REAL'), ('media_duration', 'REAL'), ('thumbnail_error', 'TEXT'),
                                   ('thumbnail_attempts', 'INTEGER DEFAULT 0'), ('thumbnail_retry_at', 'REAL DEFAULT 0')]:
                if name not in existing:
                    db.execute(f'ALTER TABLE clips ADD COLUMN {name} {sql_type}')
            preview_columns = {r[1] for r in db.execute('PRAGMA table_info(previews)')}
            for name, definition in [('requested_seconds', 'INTEGER NOT NULL DEFAULT 30'), ('priority', 'INTEGER NOT NULL DEFAULT 0')]:
                if name not in preview_columns:
                    db.execute(f'ALTER TABLE previews ADD COLUMN {name} {definition}')
            if 'media_duration' not in existing:
                # Earlier builds could count filename-only EOF tails as analysed.
                db.execute("UPDATE clips SET completed=0,status='pending',error=NULL,attempts=0,retry_at=0 WHERE completed>0")
                db.execute('DELETE FROM events')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA busy_timeout=15000')
        db.execute('PRAGMA journal_mode=WAL')
        try:
            with db:
                yield db
        finally:
            db.close()

    def state(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_state(self, key, value):
        with self.connect() as db:
            db.execute('''INSERT INTO state (key,value) VALUES (?,?)
                          ON CONFLICT(key) DO UPDATE SET value=excluded.value
                          WHERE state.value IS NOT excluded.value''', (key, json.dumps(value)))

    def scan(self):
        seen, observations, errors = set(), [], []
        try:
            safe_source(self.root, '__scan_probe__')
            if not stat_mode.S_ISDIR(self.root.stat().st_mode):
                raise OSError('recordings root is not a directory')
        except (ValueError, OSError) as exc:
            self.set_state('scan_error', str(exc))
            return False
        for base, dirs, files in os.walk(self.root, followlinks=False, onerror=errors.append):
            dirs[:] = [d for d in dirs if not d.startswith(('.', '@')) and d.lower() != '#recycle'
                       and not (Path(base) / d).is_symlink()]
            for name in files:
                rel = (Path(base) / name).relative_to(self.root)
                info = parse_recording(rel, self.source_tz)
                if not info:
                    continue
                try:
                    path = safe_source(self.root, rel)
                    stat = path.stat()
                    if not stat_mode.S_ISREG(stat.st_mode):
                        continue
                except ValueError:
                    continue
                except OSError as exc:
                    errors.append(exc)
                    continue
                seen.add(info['id'])
                observations.append((info, stat))
        # A partial walk is not evidence of deletion. Do not update observations,
        # generations or missing records until all directory/file reads succeeded.
        if errors:
            self.set_state('scan_error', '; '.join(str(exc) for exc in errors)[:1600])
            return False
        # NAS fsync latency makes a transaction per file disproportionately costly.
        # Keep locks bounded while amortizing commits across up to200 observations.
        for offset in range(0, len(observations), 200):
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for info, stat in observations[offset:offset + 200]:
                    now = time.time()
                    old = db.execute('SELECT * FROM clips WHERE id=?', (info['id'],)).fetchone()
                    same = old and old['size'] == stat.st_size and old['mtime'] == stat.st_mtime_ns and old['status'] != 'missing'
                    if not same or (old['status'] != 'unstable' and not self.complete_size(stat.st_size)):
                        db.execute('DELETE FROM events WHERE clip_id=?', (info['id'],))
                        db.execute('DELETE FROM previews WHERE clip_id=?', (info['id'],))
                        columns = list(info) + ['size', 'mtime', 'generation', 'observations', 'status', 'stable_since']
                        values = list(info.values()) + [stat.st_size, stat.st_mtime_ns, secrets.token_hex(16), 1, 'unstable', now]
                        db.execute(f'INSERT OR REPLACE INTO clips ({",".join(columns)}) VALUES ({",".join("?" for _ in columns)})', values)
                    elif old['status'] == 'unstable':
                        # NULL means an unstable row predating observed-window tracking.
                        since = old['stable_since'] if old['stable_since'] is not None else now
                        ready = (self.complete_size(stat.st_size) and old['observations'] >= 1
                                 and now - since >= self.stable_seconds
                                 and now - stat.st_mtime >= self.stable_seconds)
                        # Only the first two observations establish stability. Rewriting
                        # every unchanged recording on each scan needlessly dirties pages.
                        count = max(2, old['observations'])
                        status = 'pending' if ready else 'unstable'
                        if (count, status, since) != (old['observations'], old['status'], old['stable_since']):
                            db.execute('UPDATE clips SET observations=?,status=?,stable_since=? WHERE id=?',
                                       (count, status, since, info['id']))
        with self.connect() as db:
            for row in db.execute("SELECT id FROM clips WHERE status!='missing'").fetchall():
                if row['id'] not in seen:
                    db.execute("UPDATE clips SET status='missing',generation=?,completed=0,thumbnail=NULL WHERE id=?", (secrets.token_hex(16), row['id']))
                    db.execute('DELETE FROM events WHERE clip_id=?', (row['id'],))
                    db.execute('DELETE FROM previews WHERE clip_id=?', (row['id'],))
        self.set_state('last_scan', time.time())
        self.set_state('scan_error', None)
        self.cleanup_seek_cache()
        self.cleanup()
        return True

    def next_stability_check(self):
        # A paused/incomplete upload cannot become ready merely by waiting.
        # Revisit it only when a source notification reports another change.
        with self.connect() as db:
            rows = db.execute("SELECT size,mtime,stable_since FROM clips WHERE status='unstable'").fetchall()
        deadlines = [max(row['stable_since'] if row['stable_since'] is not None else time.time(),
                         row['mtime'] / 1e9) + self.stable_seconds
                     for row in rows if self.complete_size(row['size'])]
        return min(deadlines) if deadlines else None

    def clips(self, date=None, channel=None):
        query, args = "SELECT * FROM clips WHERE status!='missing'", []
        if date:
            start, end = day_bounds(date)
            query += ' AND start<? AND start+MIN(duration,COALESCE(media_duration,duration))>?'
            args.extend([end, start])
        if channel:
            query += ' AND channel=?'
            args.append(channel)
        with self.connect() as db:
            return [dict(r) for r in db.execute(query + ' ORDER BY start,channel', args)]

    def days(self):
        days = set()
        for clip in self.clips():
            start = datetime.fromtimestamp(clip['start'], BEIJING).date()
            # Intervals are half open: a clip ending at midnight belongs to the preceding day.
            last = datetime.fromtimestamp(clip['start'] + self.playable_duration(clip) - .000001, BEIJING).date()
            while start <= last:
                days.add(start.isoformat())
                start += timedelta(days=1)
        return sorted(days, reverse=True)

    def clip(self, ident):
        if not isinstance(ident, str) or not IDENT.fullmatch(ident):
            raise ValueError('invalid clip ID')
        with self.connect() as db:
            row = db.execute("SELECT * FROM clips WHERE id=? AND status!='missing'", (ident,)).fetchone()
            if not row:
                raise LookupError('clip not found')
            return dict(row)

    def complete_size(self, size):
        return size > 0 and (not self.complete_file_bytes or size == self.complete_file_bytes)

    def current_source(self, clip):
        if clip['status'] in ('unstable', 'missing') or not self.complete_size(clip['size']):
            raise RuntimeError('source is still changing')
        path = safe_source(self.root, clip['relative'])
        try:
            stat = path.stat()
        except OSError as exc:
            raise RuntimeError('source unavailable or changed; waiting for scan') from exc
        if stat.st_size != clip['size'] or stat.st_mtime_ns != clip['mtime']:
            raise RuntimeError('source changed; waiting for scan')
        return path

    def valid_generation(self, ident, generation):
        try:
            clip = self.clip(ident)
            self.current_source(clip)
            return clip['generation'] == generation and clip['status'] != 'unstable'
        except (LookupError, ValueError, RuntimeError):
            return False

    @staticmethod
    def playable_duration(clip):
        actual = clip['media_duration']
        return min(clip['duration'], actual) if actual is not None else clip['duration']

    def publish_media_duration(self, clip, duration):
        if not self.valid_generation(clip['id'], clip['generation']):
            raise RuntimeError('source changed while probing duration')
        with self.connect() as db:
            if not db.execute('UPDATE clips SET media_duration=? WHERE id=? AND generation=?',
                              (duration, clip['id'], clip['generation'])).rowcount:
                raise RuntimeError('source generation changed')
        clip['media_duration'] = duration

    def fail_thumbnail(self, clip, error):
        attempts = clip['thumbnail_attempts'] + 1
        with self.connect() as db:
            db.execute('UPDATE clips SET thumbnail_error=?,thumbnail_attempts=?,thumbnail_retry_at=? WHERE id=? AND generation=?',
                       (str(error)[:800], attempts, time.time() + min(3600, 30 * 2 ** min(attempts, 7)), clip['id'], clip['generation']))

    def events(self, date=None, channel=None):
        with self.connect() as db:
            query = "SELECT e.*,c.start,c.channel FROM events e JOIN clips c ON e.clip_id=c.id AND e.generation=c.generation WHERE c.status!='missing'"
            args = []
            if date:
                start, end = day_bounds(date)
                query += ' AND c.start+e.start_offset<? AND c.start+e.end_offset>?'
                args.extend([end, start])
            if channel:
                query += ' AND c.channel=?'
                args.append(channel)
            return [dict(r) for r in db.execute(query + ' ORDER BY c.start,e.start_offset', args)]

    def publish_chunk(self, ident, generation, completed, hits, thumbnail=None):
        from .media import group_hits
        if not self.valid_generation(ident, generation):
            return False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            clip = db.execute('SELECT * FROM clips WHERE id=? AND generation=?', (ident, generation)).fetchone()
            if not clip:
                return False
            limit = self.playable_duration(clip)
            completed = min(completed, limit)
            groups = group_hits(hits, limit)
            for event in groups:
                previous = db.execute('SELECT * FROM events WHERE clip_id=? AND generation=? ORDER BY last_hit DESC LIMIT 1', (ident, generation)).fetchone()
                if previous and event['first_hit'] - previous['last_hit'] <= 6:
                    thumb = event['thumb'] if event['score'] > previous['score'] else previous['thumb']
                    db.execute('UPDATE events SET end_offset=?,last_hit=?,score=?,thumb=? WHERE id=?',
                               (max(previous['end_offset'], event['end_offset']), event['last_hit'], max(previous['score'], event['score']), thumb, previous['id']))
                else:
                    db.execute('INSERT INTO events(clip_id,generation,start_offset,end_offset,first_hit,last_hit,score,thumb) VALUES (?,?,?,?,?,?,?,?)',
                               (ident, generation, event['start_offset'], event['end_offset'], event['first_hit'], event['last_hit'], event['score'], event['thumb']))
            db.execute("UPDATE clips SET completed=?,status=?,error=NULL,attempts=0,retry_at=0,thumbnail=COALESCE(thumbnail,?) WHERE id=? AND generation=?",
                       (completed, 'complete' if completed >= limit else 'pending', thumbnail, ident, generation))
        return True

    def next_analysis(self):
        with self.connect() as db:
            row = db.execute("SELECT * FROM clips WHERE status IN ('pending','error') AND retry_at<=? ORDER BY start DESC LIMIT 1", (time.time(),)).fetchone()
            return dict(row) if row else None

    def fail_analysis(self, clip, error):
        with self.connect() as db:
            attempts = clip['attempts'] + 1
            db.execute("UPDATE clips SET status='error',error=?,attempts=?,retry_at=? WHERE id=? AND generation=?",
                       (str(error)[:800], attempts, time.time() + min(3600, 30 * 2 ** min(attempts, 7)), clip['id'], clip['generation']))

    def enqueue_preview(self, ident, offset, seconds=30, prefetch=False, reuse=False):
        if type(seconds) is not int or not 1 <= seconds <= 30:
            raise ValueError("seconds must be an integer in 1..30")
        if type(prefetch) is not bool:
            raise ValueError("prefetch must be boolean")
        if type(reuse) is not bool:
            raise ValueError("reuse must be boolean")
        clip = self.clip(ident)
        if clip['status'] == 'unstable':
            raise RuntimeError('source is still changing')
        self.current_source(clip)
        offset = float(offset)
        if not 0 <= offset < self.playable_duration(clip):
            raise ValueError('offset outside clip')
        # Round to milliseconds for deterministic deduplication, retaining requested seek.
        offset = round(offset, 3)
        if reuse:
            # Avoid turning a seek into a succession of tiny cached tails. True
            # EOF can be shorter, and short explicit requests need no extra media.
            minimum = min(seconds, 4, self.playable_duration(clip) - offset)
            with self.connect() as db:
                candidates = db.execute("""SELECT * FROM previews WHERE clip_id=? AND generation=?
                    AND status='ready' AND offset<=? AND offset+requested_seconds>=?
                    ORDER BY offset+requested_seconds DESC,offset DESC""",
                    (ident, clip['generation'], offset, offset + minimum)).fetchall()
            for candidate in candidates:
                path = self.readable_media(candidate['media'], extension='mp4')
                if path is not None:
                    os.utime(path, None)
                    return dict(candidate)
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO previews (id,clip_id,generation,offset,status,media,error,created,requested_seconds,priority) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (secrets.token_hex(16), ident, clip['generation'], offset, 'pending', None, None, time.time(), seconds, int(prefetch)))
            row = db.execute('SELECT * FROM previews WHERE clip_id=? AND generation=? AND offset=?', (ident, clip['generation'], offset)).fetchone()
            if reuse and row['status'] == 'ready':
                # Every usable ready candidate was considered above. An exact
                # match here is missing/unreadable or too short for this request.
                db.execute("UPDATE previews SET status='pending',media=NULL,error=NULL,requested_seconds=?,created=? WHERE id=?",
                           (seconds, time.time(), row['id']))
            if row['status'] == 'failed':
                db.execute("UPDATE previews SET status='pending',error=NULL,created=?,requested_seconds=? WHERE id=?",
                           (time.time(), seconds if reuse else row['requested_seconds'], row['id']))
            if not prefetch:
                db.execute('UPDATE previews SET priority=0 WHERE id=?', (row['id'],))
            job_id = row['id']
        return self.preview(job_id)

    def preview(self, ident):
        if not isinstance(ident, str) or not IDENT.fullmatch(ident):
            raise ValueError('invalid job ID')
        with self.connect() as db:
            row = db.execute('SELECT * FROM previews WHERE id=?', (ident,)).fetchone()
        if not row:
            raise LookupError('preview not found')
        return dict(row)

    def claim_preview(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM previews WHERE status='pending' ORDER BY priority,created LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE previews SET status='processing' WHERE id=?", (row['id'],))
                return dict(row)

    def pending_previews(self, foreground_only=False):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM previews WHERE status='pending'" +
                              (" AND priority=0" if foreground_only else "") + " LIMIT 1").fetchone() is not None

    def requeue_preview(self, ident):
        with self.connect() as db:
            db.execute("UPDATE previews SET status='pending',error=NULL WHERE id=? AND status='processing'", (ident,))

    def publish_preview(self, job, media):
        if not self.valid_generation(job['clip_id'], job['generation']):
            return False
        with self.connect() as db:
            return db.execute("UPDATE previews SET status='ready',media=?,error=NULL WHERE id=? AND generation=?", (media, job['id'], job['generation'])).rowcount == 1

    def fail_preview(self, ident, error):
        with self.connect() as db:
            # A commit can succeed even if its caller subsequently raises. Never
            # invalidate already published media while handling that exception.
            db.execute("UPDATE previews SET status='failed',error=? WHERE id=? AND status!='ready'", (str(error)[:800], ident))

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE previews SET status='pending' WHERE status='processing'")
        for path in self.checked_cache().glob('*.part.mp4'):
            if re.fullmatch(r'[0-9a-f]{32}\.part\.mp4', path.name) and not path.is_symlink() and path.is_file():
                path.unlink(missing_ok=True)
        self.prune_jpegs()
        self.cleanup_seek_cache(remove_partials=True)
        self.cleanup()

    def cleanup_seek_cache(self, limit=SEEK_CACHE_LIMIT, reserve=0, protected=None, remove_partials=False):
        self.checked_seek_cache()
        with self.connect() as db:
            live = {f'{r[0]}-{r[1]}.mp4' for r in db.execute("SELECT id,generation FROM clips WHERE status NOT IN ('missing','unstable')")}
        files = []
        for path in self.seek_cache.iterdir():
            if path.is_symlink() or not path.is_file():
                continue
            if remove_partials and SEEK_PART.fullmatch(path.name):
                path.unlink(missing_ok=True)
            elif SEEK_NAME.fullmatch(path.name):
                if path.name not in live:
                    path.unlink(missing_ok=True)
                else:
                    files.append(path)
        total = sum(p.stat().st_size for p in files)
        for path in sorted(files, key=lambda p: p.stat().st_mtime):
            if total <= max(0, limit - reserve):
                break
            if path.name == protected:
                continue
            total -= path.stat().st_size
            path.unlink(missing_ok=True)

    def checked_seek_cache(self):
        try:
            path = safe_source(self.data, 'seek-cache')
            if not stat_mode.S_ISDIR(path.stat().st_mode):
                raise ValueError('not a directory')
            return path
        except (ValueError, OSError) as exc:
            raise RuntimeError('定位缓存目录无效：不允许符号链接') from exc

    def checked_cache(self):
        try:
            path = safe_source(self.data, 'cache')
            if not stat_mode.S_ISDIR(path.stat().st_mode):
                raise ValueError('not a directory')
            return path
        except (ValueError, OSError) as exc:
            raise RuntimeError('画面缓存目录无效：不允许符号链接') from exc

    def readable_media(self, name, extension=None):
        if not isinstance(name, str) or not MEDIA_NAME.fullmatch(name) or (extension and not name.endswith('.' + extension)):
            return None
        try:
            path = safe_source(self.checked_cache(), name)
            stat = path.stat()
            if not stat_mode.S_ISREG(stat.st_mode) or stat.st_size <= 0:
                return None
            with path.open('rb') as stream:
                if not stream.read(1):
                    return None
            return path
        except (OSError, ValueError):
            return None

    def referenced_preview_media(self):
        with self.connect() as db:
            return {r[0] for r in db.execute("""SELECT p.media FROM previews p JOIN clips c
                ON p.clip_id=c.id AND p.generation=c.generation WHERE p.status='ready'
                AND c.status NOT IN ('missing','unstable') AND p.media IS NOT NULL""")}

    def discard_preview_media(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'[0-9a-f]{32}\.mp4', name):
            return
        if name in self.referenced_preview_media():
            return
        path = self.checked_cache() / name
        if not path.is_symlink() and path.is_file():
            path.unlink(missing_ok=True)

    def prune_jpegs(self):
        with self.connect() as db:
            keep = {r[0] for r in db.execute('SELECT thumbnail FROM clips WHERE thumbnail IS NOT NULL UNION SELECT thumb FROM events WHERE thumb IS NOT NULL')}
        for path in self.checked_cache().glob('*.jpg'):
            if MEDIA_NAME.fullmatch(path.name) and not path.is_symlink() and path.is_file() and path.name not in keep:
                path.unlink(missing_ok=True)

    def cleanup(self, limit=PLAYBACK_CACHE_LIMIT):
        # Only our generated mp4 names are ever deleted. JPEG event evidence persists.
        keep = self.referenced_preview_media()
        files = []
        for path in self.checked_cache().glob('*.mp4'):
            if not MEDIA_NAME.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                continue
            if path.name not in keep:
                path.unlink(missing_ok=True)
            else:
                files.append(path)
        files.sort(key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if total <= limit:
                break
            size = path.stat().st_size
            path.unlink(missing_ok=True)
            total -= size
            with self.connect() as db:
                db.execute("UPDATE previews SET status='failed',error='缓存已清理，请重新请求',media=NULL WHERE media=?", (path.name,))


def catalog_from_env():
    return Catalog(os.environ.get('RECORDINGS_ROOT', '/recordings'), os.environ.get('DATA_ROOT', '/data'),
                   os.environ.get('SOURCE_TZ', 'Asia/Shanghai'), int(os.environ.get('STABLE_SECONDS', '180')),
                   int(os.environ.get('COMPLETE_FILE_BYTES', '0')))
