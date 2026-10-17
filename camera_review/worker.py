"""Single worker: scan, previews first, then one resumable analysis chunk."""
import logging
import os
import threading
import time

from .catalog import catalog_from_env
from .media import MediaCancelled, PersonDetector, analyze_chunk, make_preview, make_thumbnail, probe_duration, ensure_seek_source
from .motion import EmptySceneGate

LOG = logging.getLogger('camera-review')


class Worker:
    def __init__(self, catalog, detector=None):
        self.cat, self.detector = catalog, detector
        self.last_scan = 0
        self.cover_turn = True
        self.model_retry = 0
        self.chunk_seconds = min(60, max(1, int(os.environ.get('ANALYSIS_CHUNK_SECONDS', '60'))))
        self.sample_seconds = max(1, int(os.environ.get('SAMPLE_SECONDS', '3')))
        self.scan_seconds = max(5, int(os.environ.get('SCAN_SECONDS', '45')))
        self.empty_scene_gate = os.environ.get('EMPTY_SCENE_GATE', '0') == '1'

    def heartbeat(self):
        self.cat.set_state('heartbeat', time.time())

    def measured_source(self, clip, cancelled=None):
        source = ensure_seek_source(self.cat, clip, cancelled=cancelled)
        if clip['media_duration'] is None:
            self.cat.publish_media_duration(clip, probe_duration(source, cancelled=cancelled))
        return source

    def step(self):
        self.heartbeat()
        if time.monotonic() - self.last_scan >= self.scan_seconds:
            self.cat.scan()
            self.last_scan = time.monotonic()
        job = self.cat.claim_preview()
        if job:
            media = None
            try:
                clip = self.cat.clip(job['clip_id'])
                if clip['generation'] != job['generation']:
                    raise RuntimeError('source generation changed')
                cancelled = (lambda: self.cat.pending_previews(foreground_only=True)) if job['priority'] else None
                source = self.measured_source(clip, cancelled)
                remaining = self.cat.playable_duration(clip) - job['offset']
                if remaining <= 0:
                    raise RuntimeError('请求位置已超过实际视频末尾，请选择更早位置或下一段')
                media = make_preview(source, self.cat.cache, job['offset'], min(job['requested_seconds'], remaining), cancelled=cancelled)
                if not self.cat.publish_preview(job, media):
                    self.cat.discard_preview_media(media)
                self.cat.cleanup()
            except MediaCancelled:
                self.cat.requeue_preview(job['id'])
            except Exception as exc:
                self.cat.fail_preview(job['id'], exc)
                LOG.exception('preview failed %s', job['id'])
            finally:
                if media:
                    self.cat.discard_preview_media(media)
            return True
        # Cover generation also continues while detection is paused or model missing.
        with self.cat.connect() as db:
            row = db.execute("SELECT * FROM clips WHERE thumbnail IS NULL AND status IN ('pending','complete','error') AND thumbnail_retry_at<=? ORDER BY start DESC LIMIT 1", (time.time(),)).fetchone()
        if row and (self.cover_turn or self.cat.state('paused', False)):
            self.cover_turn = False
            clip = dict(row)
            try:
                source = self.measured_source(clip, self.cat.pending_previews)
                name = make_thumbnail(source, self.cat.cache, min(5, self.cat.playable_duration(clip) / 2), cancelled=self.cat.pending_previews)
                if self.cat.valid_generation(clip['id'], clip['generation']):
                    with self.cat.connect() as db:
                        db.execute('UPDATE clips SET thumbnail=?,thumbnail_error=NULL,thumbnail_attempts=0,thumbnail_retry_at=0 WHERE id=? AND generation=?', (name, clip['id'], clip['generation']))
                else:
                    (self.cat.cache / name).unlink(missing_ok=True)
            except MediaCancelled:
                pass
            except Exception as exc:
                self.cat.fail_thumbnail(clip, exc)
                LOG.exception('thumbnail failed %s', clip['id'])
            return True
        if self.cat.state('paused', False):
            return False
        self.cover_turn = True
        clip = self.cat.next_analysis()
        if not clip:
            return False
        if self.detector is None:
            if time.monotonic() < self.model_retry:
                return False
            try:
                self.detector = PersonDetector(os.environ.get('MODEL_ROOT', '/models'))
                self.cat.set_state('worker_error', None)
            except Exception as exc:
                self.model_retry = time.monotonic() + 60
                self.cat.set_state('worker_error', '人物模型加载失败：' + str(exc)[:600])
                LOG.exception('model loading failed')
                return False
        try:
            source = self.measured_source(clip, self.cat.pending_previews)
            length = min(self.chunk_seconds, self.cat.playable_duration(clip) - clip['completed'])
            gate = EmptySceneGate(enabled=self.empty_scene_gate)
            started = time.monotonic()
            hits = analyze_chunk(source, self.cat.cache, clip['completed'], length,
                                 self.detector, self.sample_seconds, self.heartbeat, cancelled=self.cat.pending_previews, gate=gate)
            if not self.cat.publish_chunk(clip['id'], clip['generation'], clip['completed'] + length, hits):
                for _, _, name in hits:
                    (self.cat.cache / name).unlink(missing_ok=True)
            else:
                try:
                    self.cat.set_state('analysis_last_chunk', dict(clip_id=clip['id'], generation=clip['generation'],
                                       source_start=clip['completed'], source_seconds=length,
                                       sampled_frames=gate.sampled_frames, model_frames=gate.model_frames,
                                       skipped_frames=gate.skipped_frames, gate_enabled=gate.enabled,
                                       elapsed_seconds=time.monotonic() - started, observed=time.time()))
                except Exception:
                    # Observability must not invalidate already-published progress.
                    LOG.warning('could not record analysis counters', exc_info=True)
            self.cat.prune_jpegs()
        except MediaCancelled:
            pass
        except Exception as exc:
            self.cat.fail_analysis(clip, exc)
            LOG.exception('analysis failed %s', clip['id'])
        return True


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    cat = catalog_from_env()
    # Hold an OS lock for the process lifetime: never run two decoders against DB.
    lockfile = (cat.data / 'worker.lock').open('a+b')
    if os.name == 'nt':
        import msvcrt
        lockfile.write(b'0')
        lockfile.flush()
        lockfile.seek(0)
        msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
    cat.recover()
    worker = Worker(cat)
    # Long encodes still report liveness; this thread never performs media work.
    stop = threading.Event()

    def pulse():
        while not stop.wait(10):
            try:
                worker.heartbeat()
            except Exception:
                LOG.exception('heartbeat write failed')

    threading.Thread(target=pulse, daemon=True).start()
    try:
        while True:
            try:
                if not worker.step():
                    time.sleep(1)
            except Exception as exc:
                LOG.exception('worker iteration failed')
                cat.set_state('worker_error', str(exc)[:800])
                time.sleep(5)
    finally:
        stop.set()
        lockfile.close()


if __name__ == '__main__':
    main()
