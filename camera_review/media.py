"""Bounded FFmpeg transforms. Source files are always input-only."""
import os
import json
import math
import secrets
import subprocess
import tempfile
import time
import threading
from pathlib import Path

import cv2
import numpy as np
from .catalog import SEEK_CACHE_LIMIT, safe_source

def bounded_threads(name):
    try:
        return min(4, max(1, int(os.environ.get(name, '1'))))
    except ValueError:
        return 1


cv2.setNumThreads(bounded_threads('OPENCV_NUM_THREADS'))
PREVIEW_THREADS = str(bounded_threads('PREVIEW_THREADS'))
FFMPEG = os.environ.get('FFMPEG', 'ffmpeg')
BASE = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin', '-threads', '1', '-filter_threads', '1']


def group_hits(hits, duration):
    groups = []
    for offset, score, thumb in sorted(hits):
        if groups and offset - groups[-1]['last_hit'] <= 6:
            event = groups[-1]
            event['last_hit'], event['end_offset'] = offset, min(duration, offset + 4)
            if score > event['score']:
                event['score'], event['thumb'] = score, thumb
        else:
            groups.append(dict(first_hit=offset, last_hit=offset, start_offset=max(0, offset - 4),
                               end_offset=min(duration, offset + 4), score=score, thumb=thumb))
    return groups


def generated_name(extension):
    return secrets.token_hex(16) + '.' + extension


def jpeg(cache, frame):
    name = generated_name('jpg')
    ok, encoded = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 76])
    if not ok:
        raise RuntimeError('JPEG encoding failed')
    (Path(cache) / name).write_bytes(encoded.tobytes())
    return name


class MediaCancelled(Exception):
    """Cooperative scheduling yield; never an analysis/thumbnail failure."""


def check_cancelled(cancelled):
    if cancelled and cancelled():
        raise MediaCancelled()


def _invoke(command, cancelled=None, **kwargs):
    try:
        if cancelled is None:
            return subprocess.run(command, **kwargs)
        check_cancelled(cancelled)
        timeout = kwargs.pop('timeout', 180)
        if kwargs.pop('capture_output', False):
            kwargs['stdout'], kwargs['stderr'] = subprocess.PIPE, subprocess.PIPE
        process = subprocess.Popen(command, **kwargs)
        deadline = time.monotonic() + timeout
        try:
            while True:
                check_cancelled(cancelled)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(.1, remaining))
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
            for stream in (process.stdout, process.stderr):
                if stream:
                    stream.close()
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError('生成画面超时，后台会自动重试') from exc


def _run(command, timeout=180, cancelled=None):
    result = _invoke(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=timeout, cancelled=cancelled)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace')[-700:] or 'FFmpeg failed')


def probe_duration(source, cancelled=None):
    result = _invoke([os.environ.get('FFPROBE', 'ffprobe'), '-v', 'error', '-select_streams', 'v:0',
                             '-show_entries', 'stream=duration:format=duration', '-of', 'json', str(source)],
                            capture_output=True, timeout=30, cancelled=cancelled)
    try:
        data = json.loads(result.stdout)
        streams = data.get('streams', [])
        if result.returncode or not streams:
            raise ValueError()
        value = streams[0].get('duration')
        if value in (None, 'N/A'):
            value = data.get('format', {}).get('duration')
        duration = float(value)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError()
        return duration
    except (ValueError, TypeError, KeyError) as exc:
        raise RuntimeError('无法确认录像的实际视频时长，不会将未解码时间标记为已分析') from exc


def ensure_seek_source(catalog, clip, cancelled=None):
    """Rebuild broken source seek tables using stream copy, once per generation."""
    check_cancelled(cancelled)
    cache = catalog.checked_seek_cache()
    directory_stat = cache.stat()
    source = catalog.current_source(clip)
    if not catalog.valid_generation(clip['id'], clip['generation']):
        raise RuntimeError('录像已变化，等待重新索引')
    target = cache / f'{clip["id"]}-{clip["generation"]}.mp4'
    partial = target.with_suffix('.part.mp4')
    if partial.is_symlink() or partial.exists():
        raise RuntimeError('定位缓存临时路径已存在，拒绝覆盖；请重启后台清理合法残留')
    if target.is_symlink():
        raise RuntimeError('定位缓存路径无效')
    if target.is_file():
        try:
            probe_duration(target, **({"cancelled": cancelled} if cancelled else {}))
        except MediaCancelled:
            raise
        except Exception:
            target.unlink(missing_ok=True)
        else:
            if not catalog.valid_generation(clip['id'], clip['generation']):
                raise RuntimeError('录像已变化，定位缓存失效')
            os.utime(target, None)
            return target
    size = source.stat().st_size
    if size > SEEK_CACHE_LIMIT:
        raise RuntimeError('原片超过512MiB定位缓存上限，暂无法处理')
    catalog.cleanup_seek_cache(reserve=min(SEEK_CACHE_LIMIT, size + 16 * 1024 ** 2))
    # Own the temporary inode exclusively. Never remove a preexisting path/link.
    try:
        fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError('定位缓存临时路径已存在，拒绝覆盖') from exc
    try:
        owned = os.fstat(fd)
    finally:
        os.close(fd)

    def owns_partial():
        try:
            current_dir = catalog.checked_seek_cache().stat()
            current = partial.lstat()
            return (not partial.is_symlink() and current_dir.st_dev == directory_stat.st_dev
                    and current_dir.st_ino == directory_stat.st_ino
                    and current.st_dev == owned.st_dev and current.st_ino == owned.st_ino)
        except (OSError, RuntimeError):
            return False

    try:
        _run(BASE + ['-i', str(source), '-map', '0', '-c', 'copy', '-movflags', '+faststart', '-y', str(partial)], timeout=180, **({'cancelled': cancelled} if cancelled else {}))
        if not owns_partial():
            raise RuntimeError('定位缓存临时路径已变化，拒绝发布')
        probe_duration(partial, **({"cancelled": cancelled} if cancelled else {}))  # Reject empty/no-video output even when FFmpeg exits0.
        if partial.stat().st_size > SEEK_CACHE_LIMIT:
            raise RuntimeError('定位缓存超过512MiB上限')
        if not catalog.valid_generation(clip['id'], clip['generation']):
            raise RuntimeError('录像在构建定位缓存时发生变化，结果已丢弃')
        partial.replace(target)
        catalog.cleanup_seek_cache(protected=target.name)
        return target
    finally:
        if owns_partial():
            partial.unlink(missing_ok=True)


def make_preview(source, cache, offset, duration=30, cancelled=None):
    name = generated_name('mp4')
    target = safe_source(cache, name)
    partial = target.with_suffix('.part.mp4')
    directory_stat = Path(cache).stat()
    if target.exists() or target.is_symlink():
        raise RuntimeError('画面缓存路径已存在，拒绝覆盖')
    try:
        fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError('画面缓存临时路径已存在，拒绝覆盖') from exc
    try:
        owned = os.fstat(fd)
    finally:
        os.close(fd)

    def owns_partial():
        try:
            safe_source(cache, partial.name)
            current_dir, current = Path(cache).stat(), partial.lstat()
            return (current_dir.st_dev == directory_stat.st_dev and current_dir.st_ino == directory_stat.st_ino
                    and current.st_dev == owned.st_dev and current.st_ino == owned.st_ino)
        except (OSError, ValueError):
            return False

    try:
        _run(BASE + ['-threads', PREVIEW_THREADS, '-ss', str(offset), '-i', str(source), '-t', str(min(30, duration)),
                     '-map', '0:v:0', '-map', '0:a:0?',
                     '-vf', "scale=w='min(960,iw)':h=-2,fps=12", '-c:v', 'libx264', '-threads', PREVIEW_THREADS,
                     '-preset', 'ultrafast', '-crf', '26', '-pix_fmt', 'yuv420p', '-c:a', 'aac',
                     '-ac', '1', '-b:a', '64k', '-movflags', '+faststart', '-y', str(partial)], cancelled=cancelled)
        if not owns_partial():
            raise RuntimeError('画面缓存临时路径已变化，拒绝发布')
        probe = _invoke([os.environ.get('FFPROBE', 'ffprobe'), '-v', 'error', '-select_streams', 'v:0',
                                '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', str(partial)],
                               capture_output=True, timeout=15, cancelled=cancelled)
        if probe.returncode or b'h264' not in probe.stdout:
            raise RuntimeError('此位置没有可解码画面（文件名结束时间可能晚于实际录像），请选择更早位置或下一段')
        check_cancelled(cancelled)
        if not owns_partial() or target.exists() or target.is_symlink():
            raise RuntimeError('画面缓存路径已变化，拒绝发布')
        partial.replace(target)
        return name
    finally:
        if owns_partial():
            partial.unlink(missing_ok=True)


def make_thumbnail(source, cache, offset, cancelled=None):
    # A midpoint seek yields a representative still, independent from playback cache.
    with tempfile.TemporaryFile() as error:
        result = _invoke(BASE + ['-ss', str(offset), '-i', str(source), '-frames:v', '1',
                                       '-vf', 'scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2',
                                       '-f', 'image2pipe', '-vcodec', 'mjpeg', '-threads', '1', '-'],
                                stdout=subprocess.PIPE, stderr=error, timeout=90, cancelled=cancelled)
        if result.returncode or not result.stdout:
            raise RuntimeError('thumbnail decoding failed')
        name = generated_name('jpg')
        (Path(cache) / name).write_bytes(result.stdout)
        return name


class PersonDetector:
    def __init__(self, model_root):
        root = Path(model_root)
        self.net = cv2.dnn.readNetFromONNX(str(root / 'object_detection_yolox_2022nov.onnx'))

    def score(self, frame):
        height, width = frame.shape[:2]
        ratio = min(416 / height, 416 / width)
        canvas = np.full((416, 416, 3), 114, dtype=np.float32)
        resized = cv2.resize(frame, (max(1, int(width * ratio)), max(1, int(height * ratio))))
        canvas[:resized.shape[0], :resized.shape[1]] = resized
        # OpenCV Zoo YOLOX expects raw BGR0..255, top-left letterbox, NCHW.
        self.net.setInput(np.ascontiguousarray(canvas.transpose(2, 0, 1)[None]))
        detections = self.net.forward()[0]
        confidence = detections[:, 4] * detections[:, 5]
        person = (detections[:, 5:].argmax(axis=1) == 0) & (confidence >= .65)
        # Presence-only indexing: boxes/grid decoding/NMS cannot alter this maximum.
        return float(confidence[person].max()) if person.any() else 0.


class _NoSampleFrames(RuntimeError):
    """Successful decoding produced no samples; keyframe-only input may be empty."""


def analyze_chunk(source, cache, start, duration, detector, sample_seconds=3, heartbeat=None, cancelled=None, gate=None):
    # Tail chunks can contain only predicted frames after the last keyframe.
    # Decode normally only when the fast path was empty; never assume completion.
    try:
        return _analyze_chunk(source, cache, start, duration, detector, sample_seconds, heartbeat, cancelled, gate, True)
    except _NoSampleFrames:
        try:
            return _analyze_chunk(source, cache, start, duration, detector, sample_seconds, heartbeat, cancelled, gate, False)
        except _NoSampleFrames:
            if start <= 0 or not 0 < duration < sample_seconds:
                raise
            # A VFR frame may start before the boundary but remain displayed in
            # this tiny tail. Read one sample interval back and let fps choose
            # the real frame at the boundary. Empty/broken decodes still fail.
            return _analyze_chunk(source, cache, start, duration, detector, sample_seconds,
                                  heartbeat, cancelled, gate, False, preroll=min(start, sample_seconds))


def _analyze_chunk(source, cache, start, duration, detector, sample_seconds, heartbeat, cancelled, gate, keyframes_only, preroll=0):
    """Only successful nonempty chunks publish hits. Offsets are approximate.

    Decoder input seeks to chunk start, discards non-keyframes, then normalizes to
    one frame / sample_seconds. ffmpeg's fps timeline approximates source times;
    long GOPs can move samples by more than3s; this is sampled presence and
    does not reconstruct motion between samples or claim precise event timing.
    """
    check_cancelled(cancelled)
    seek = ['-ss', str(start - preroll)] + (['-skip_frame', 'nokey'] if keyframes_only else [])
    sampling = f'fps=1/{sample_seconds}:eof_action=pass'
    if preroll:
        sampling = f'setpts=PTS-{preroll}/TB,' + sampling + ':start_time=0'
    # Select samples before resizing so unused decoded frames avoid scale/pad work.
    command = BASE + seek + ['-i', str(source), '-t', str(duration),
                      '-an', '-sn', '-vf', sampling + ',scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2',
                      *(['-fps_mode', 'passthrough'] if preroll else []),
                      '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-threads', '1', '-']
    hits, created = [], []
    last_beat = time.monotonic()
    try:
        with tempfile.TemporaryFile() as error:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=error)
            timed_out = threading.Event()
            yielded, stop_poll = threading.Event(), threading.Event()

            def poll_cancellation():
                while not stop_poll.wait(.1):
                    if cancelled and cancelled():
                        yielded.set()
                        if process.poll() is None:
                            process.kill()
                        return

            monitor = threading.Thread(target=poll_cancellation, daemon=True) if cancelled else None
            if monitor:
                monitor.start()

            def expire():
                timed_out.set()
                if process.poll() is None:
                    process.kill()

            watchdog = threading.Timer(180, expire)
            watchdog.daemon = True
            watchdog.start()
            try:
                index = 0
                while True:
                    check_cancelled(cancelled)
                    # Buffered read returns exactly one frame unless EOF. No video accumulation.
                    raw = process.stdout.read(640 * 360 * 3)
                    if yielded.is_set():
                        raise MediaCancelled()
                    if not raw:
                        break
                    if len(raw) != 640 * 360 * 3:
                        raise RuntimeError('incomplete decoded frame')
                    frame = np.frombuffer(raw, np.uint8).reshape(360, 640, 3)
                    offset = min(start + index * sample_seconds, start + duration)
                    score = gate.score(frame, offset, detector) if gate is not None else detector.score(frame)
                    if score:
                        thumb = jpeg(cache, frame)
                        created.append(thumb)
                        hits.append((offset, score, thumb))
                    check_cancelled(cancelled)
                    index += 1
                    if heartbeat and time.monotonic() - last_beat >= 5:
                        heartbeat()
                        last_beat = time.monotonic()
                returncode = process.wait(timeout=30)
                if yielded.is_set():
                    raise MediaCancelled()
                if timed_out.is_set():
                    raise RuntimeError('人物分析超时，本段未计入进度，后台会自动重试')
                if returncode:
                    error.seek(0)
                    raise RuntimeError(error.read().decode(errors='replace')[-700:] or 'analysis decoder failed')
                if index == 0:
                    raise _NoSampleFrames('没有解码到任何抽样帧：可能已到实际录像末尾或本段缺少关键帧；本段未计入分析进度')
            finally:
                stop_poll.set()
                if monitor:
                    monitor.join()
                watchdog.cancel()
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdout.close()
        return hits
    except Exception:
        for name in created:
            (Path(cache) / name).unlink(missing_ok=True)
        raise
