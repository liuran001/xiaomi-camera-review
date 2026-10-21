"""Single worker: scan, previews first, then one resumable analysis chunk."""
import logging
import os
import threading
import time

from .catalog import catalog_from_env
from .media import MediaCancelled, PersonDetector, analyze_chunk, make_preview, make_thumbnail, probe_duration, ensure_seek_source
from .motion import EmptySceneGate
from .heartbeat import write_heartbeat
from .scanning import ScanScheduler

LOG = logging.getLogger('camera-review')


class Worker:
    def __init__(self, catalog, detector=None, monitor=None):
        self.cat, self.detector = catalog, detector
        self.monitor = monitor
        self._idle_version = None
        self._work_at = 0
        self.cover_turn = True
        self.model_retry = 0
        self._last_heartbeat = None
        self._heartbeat_lock = threading.Lock()
        self.chunk_seconds = min(60, max(1, int(os.environ.get('ANALYSIS_CHUNK_SECONDS', '60'))))
        self.sample_seconds = max(1, int(os.environ.get('SAMPLE_SECONDS', '3')))
        self.scan_seconds = max(5, int(os.environ.get('SCAN_SECONDS', '45')))
        self.scanner = ScanScheduler(catalog, self.scan_seconds)
        self.heartbeat_file = os.environ.get('HEARTBEAT_FILE')
        self.empty_scene_gate = os.environ.get('EMPTY_SCENE_GATE', '0') == '1'

    def heartbeat(self):
        # Both the pulse thread and media callbacks report liveness. Coalesce them
        # without postponing a retry if the database write fails.
        with self._heartbeat_lock:
            now = time.monotonic()
            if self._last_heartbeat is None or now - self._last_heartbeat >= 30:
                if self.heartbeat_file:
                    write_heartbeat(self.heartbeat_file, time.time())
                else:
                    self.cat.set_state('heartbeat', time.time())
                self._last_heartbeat = now

    def measured_source(self, clip, cancelled=None):
        source = ensure_seek_source(self.cat, clip, cancelled=cancelled)
        if clip['media_duration'] is None:
            self.cat.publish_media_duration(clip, probe_duration(source, cancelled=cancelled))
        return source

    def step(self):
        self.heartbeat()
        scanned = self.scanner.tick()
        version = self.monitor.execute('PRAGMA data_version').fetchone()[0] if self.monitor is not None else None
        if (not scanned and version is not None and version == self._idle_version
                and time.monotonic() < self._work_at):
            return False
        worked = self._work()
        if not worked and self.monitor is not None:
            # Capture the version BEFORE querying queues. A web request committed
            # during this iteration must wake the next one rather than be missed.
            self._idle_version = version
            self._work_at = time.monotonic() + self._retry_delay()
        else:
            self._idle_version = None
        return worked

    def _retry_delay(self):
        with self.cat.connect() as db:
            cover = db.execute("SELECT MIN(thumbnail_retry_at) FROM clips WHERE thumbnail IS NULL AND status IN ('pending','complete','error')").fetchone()[0]
            analysis = db.execute("SELECT MIN(retry_at) FROM clips WHERE status IN ('pending','error')").fetchone()[0]
        delays = []
        if cover is not None:
            delays.append(cover - time.time())
        if analysis is not None and not self.cat.state('paused', False):
            delay = analysis - time.time()
            if self.detector is None:
                delay = max(delay, self.model_retry - time.monotonic())
            delays.append(delay)
        return max(1, min(delays)) if delays else float('inf')

    def _work(self):
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
    try:
        # Keep one idle connection for the worker lifetime. It never starts a
        # transaction; operations still use independent short connections. Closing
        # the last connection after every poll otherwise recreates WAL/SHM files
        # and checkpoints even tiny heartbeat writes on the NAS filesystem.
        with cat.connect() as monitor:
            cat.recover()
            worker = Worker(cat, monitor=monitor)
            stop = threading.Event()

            def pulse():
                while not stop.wait(10):
                    try:
                        worker.heartbeat()
                    except Exception:
                        LOG.exception('heartbeat write failed')

            pulse_thread = threading.Thread(target=pulse, daemon=True)
            pulse_thread.start()
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
                pulse_thread.join()
                worker.scanner.close()
    finally:
        lockfile.close()


if __name__ == '__main__':
    main()
