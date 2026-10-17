import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from camera_review import media
from camera_review.media import analyze_chunk, make_preview, make_thumbnail
from camera_review.catalog import Catalog
from unittest.mock import patch
import time

try:
    from camera_review.worker import Worker
except ImportError:
    Worker = None


class MediaTests(unittest.TestCase):
    def test_worker_exists(self):
        self.assertIsNotNone(Worker, 'worker executes priority previews and bounded analysis')

    def test_preview_priority_and_paused_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'recordings'
            source.mkdir()
            file = source / '00_20261017090000_20261017090200.mp4'
            file.write_bytes(b'fixture')
            os.utime(file, (time.time()-300, time.time()-300))
            cat = Catalog(source, root / 'data', stable_seconds=0)
            cat.scan(); cat.scan()
            clip = cat.clips()[0]
            job = cat.enqueue_preview(clip['id'], 0)
            worker = Worker(cat, detector=object())
            cat.set_state('paused', True)
            with patch('camera_review.worker.ensure_seek_source', return_value=file), patch('camera_review.worker.probe_duration', return_value=120), patch('camera_review.worker.make_preview', return_value='a' * 32 + '.mp4') as preview, patch('camera_review.worker.analyze_chunk') as analysis:
                self.assertTrue(worker.step())
                preview.assert_called_once()
                analysis.assert_not_called()
                self.assertEqual(cat.preview(job['id'])['status'], 'ready')

    def test_catalog_thumbnails_do_not_starve_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'recordings'; source.mkdir()
            for day in [16, 17]:
                path = source / f'00_202610{day}090000_202610{day}090200.mp4'
                path.write_bytes(b'fixture')
                os.utime(path, (time.time()-300, time.time()-300))
            cat = Catalog(source, root / 'data', stable_seconds=0); cat.scan(); cat.scan()
            worker = Worker(cat, detector=object())
            with patch('camera_review.worker.ensure_seek_source', return_value=path), patch('camera_review.worker.probe_duration', return_value=120), patch('camera_review.worker.make_thumbnail', return_value='a' * 32 + '.jpg'), patch('camera_review.worker.analyze_chunk', return_value=[]) as analysis:
                worker.step(); worker.step()
                self.assertEqual(analysis.call_count, 1)
                self.assertEqual(cat.next_analysis()['completed'], 60)

    def test_cover_failure_has_independent_retry_and_does_not_block_analysis(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'recordings'; source.mkdir()
            path = source / '00_20261017090000_20261017090200.mp4'
            path.write_bytes(b'fixture'); os.utime(path, (time.time()-300, time.time()-300))
            cat = Catalog(source, root / 'data', stable_seconds=0); cat.scan(); cat.scan()
            worker = Worker(cat, detector=object())
            with patch('camera_review.worker.ensure_seek_source', return_value=path), patch('camera_review.worker.probe_duration', return_value=120), patch('camera_review.worker.make_thumbnail', side_effect=RuntimeError('cover failed')), patch('camera_review.worker.analyze_chunk', return_value=[]) as analysis:
                with self.assertLogs('camera-review', level='ERROR'):
                    worker.step()
                clip = cat.clips()[0]
                self.assertEqual(clip['status'], 'pending')
                self.assertIsNone(clip['error'])
                self.assertEqual(clip['thumbnail_error'], 'cover failed')
                worker.step()
                analysis.assert_called_once()
                self.assertEqual(cat.clips()[0]['completed'], 60)

    def test_speculative_encoding_yields_and_requeues_for_new_foreground(self):
        from camera_review.media import MediaCancelled
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'recordings'; source.mkdir()
            file=source/'00_20261017090000_20261017090200.mp4'
            file.write_bytes(b'fixture'); os.utime(file,(time.time()-300,time.time()-300))
            cat=Catalog(source,root/'data',stable_seconds=0); cat.scan(); cat.scan()
            clip=cat.clips()[0]; cat.publish_media_duration(clip,120)
            speculative=cat.enqueue_preview(clip['id'],8,seconds=20,prefetch=True)
            def encode(source,cache,offset,duration,cancelled=None):
                self.assertEqual(duration,20)
                foreground=cat.enqueue_preview(clip['id'],0,seconds=8)
                self.assertTrue(cancelled())
                raise MediaCancelled()
            worker=Worker(cat,detector=object())
            with patch('camera_review.worker.ensure_seek_source',return_value=file), patch('camera_review.worker.make_preview',side_effect=encode):
                worker.step()
            self.assertEqual(cat.preview(speculative['id'])['status'],'pending')
            self.assertIsNone(cat.preview(speculative['id'])['error'])
            self.assertEqual(cat.claim_preview()['offset'],0)

    def test_analysis_and_cover_cancellation_do_not_backoff_or_publish(self):
        from camera_review.media import MediaCancelled
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); source=root/'recordings'; source.mkdir()
            file=source/'00_20261017090000_20261017090200.mp4'
            file.write_bytes(b'fixture'); os.utime(file,(time.time()-300,time.time()-300))
            cat=Catalog(source,root/'data',stable_seconds=0); cat.scan(); cat.scan()
            worker=Worker(cat,detector=object())
            with patch('camera_review.worker.ensure_seek_source',return_value=file), patch('camera_review.worker.probe_duration',return_value=120), patch('camera_review.worker.make_thumbnail',side_effect=MediaCancelled()), patch('camera_review.worker.analyze_chunk',side_effect=MediaCancelled()):
                worker.step(); worker.step()
            clip=cat.clips()[0]
            self.assertEqual((clip['completed'],clip['attempts'],clip['thumbnail_attempts']),(0,0,0))
            self.assertIsNone(clip['error']); self.assertIsNone(clip['thumbnail_error'])
            self.assertEqual(cat.events(),[])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg unavailable')
    def test_decoder_failure_discards_partial_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'invalid.mp4'; source.write_bytes(b'not a video')
            with self.assertRaises(RuntimeError):
                analyze_chunk(source, root, 0, 6, object())
            self.assertEqual(list(root.glob('*.jpg')), [])

    @unittest.skipUnless(shutil.which(media.FFMPEG), 'FFmpeg unavailable')
    def test_sampling_precedes_scaling_with_identical_frames_and_offsets(self):
        # Compare real sampled BGR bytes with the former scale/pad/fps pipeline.
        # Changing content and a non-16:9 source exercise frame choice and padding.
        cases = [
            (6, 6, 60, 0, 60, 20, False),
            # Preserve the existing fps EOF behavior for sparse keyframes.
            (12, 96, 62, 0, 60, 19, False),
            (6, 48, 61, 60, 1, 1, True),
            (6, 48, 62, 60, 2, 1, True),
        ]
        for rate, gop, length, start, duration, count, fallback in cases:
            with self.subTest(rate=rate, gop=gop, length=length, start=start), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'sampling.mp4'
                subprocess.run(media.BASE + ['-f', 'lavfi', '-i', f'testsrc2=size=160x100:rate={rate}',
                                '-t', str(length), '-c:v', 'libx264', '-threads', '1', '-g', str(gop),
                                '-keyint_min', str(gop), '-sc_threshold', '0', str(source)], check=True)

                def previous_samples(keyframes_only):
                    seek = ['-ss', str(start)] + (['-skip_frame', 'nokey'] if keyframes_only else [])
                    return subprocess.run(media.BASE + seek + ['-i', str(source), '-t', str(duration),
                                          '-an', '-sn', '-vf',
                                          'scale=640:360:force_original_aspect_ratio=decrease,'
                                          'pad=640:360:(ow-iw)/2:(oh-ih)/2,fps=1/3:eof_action=pass',
                                          '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-threads', '1', '-'],
                                          capture_output=True, check=True).stdout

                reference = previous_samples(True)
                if fallback:
                    self.assertEqual(reference, b'', 'tail must exercise the normal-decoder fallback')
                    reference = previous_samples(False)
                self.assertEqual(len(reference), count * 640 * 360 * 3)

                class Detector:
                    def __init__(self):
                        self.frames = []

                    def score(self, frame):
                        self.frames.append(frame.tobytes())
                        return .8

                detector = Detector()
                with patch('camera_review.media.subprocess.Popen', wraps=subprocess.Popen) as decode:
                    hits = analyze_chunk(source, root, start, duration, detector)

                self.assertEqual(len(detector.frames), count)
                self.assertEqual(b''.join(detector.frames), reference)
                self.assertEqual([hit[0] for hit in hits], [start + index * 3 for index in range(count)])
                self.assertEqual(decode.call_count, 2 if fallback else 1)
                for invocation in decode.call_args_list:
                    command = invocation.args[0]
                    filters = command[command.index('-vf') + 1].split(',')
                    self.assertEqual(filters[0], 'fps=1/3:eof_action=pass',
                                     'discard unused samples before resizing decoded frames')

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg unavailable')
    def test_nonkeyframe_short_tails_are_analyzed_before_completing(self):
        # A chunk boundary can land after the last keyframe but before real EOF.
        for length in [61, 62]:
            with self.subTest(length=length), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); recordings = root / 'recordings'; recordings.mkdir()
                source = recordings / '00_20261017090000_20261017090200.mp4'
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:size=160x90:rate=6',
                                '-t', str(length), '-c:v', 'libx264', '-threads', '1', '-g', '48',
                                '-keyint_min', '48', '-sc_threshold', '0', str(source)], check=True)
                os.utime(source, (time.time()-300, time.time()-300))
                original = source.read_bytes(), source.stat().st_mtime_ns
                cat = Catalog(recordings, root / 'data', stable_seconds=0); cat.scan(); cat.scan()
                clip = cat.clips()[0]
                cat.publish_media_duration(clip, length)
                cat.publish_chunk(clip['id'], clip['generation'], 60, [])

                class Person:
                    calls = 0
                    def score(self, frame):
                        self.calls += 1
                        return .8

                detector = Person()
                worker = Worker(cat, detector=detector); worker.cover_turn = False
                worker.step()
                result = cat.clip(clip['id'])
                self.assertEqual((result['status'], result['completed'], result['error']), ('complete', length, None))
                self.assertEqual(detector.calls, 1, 'tail must run real detection before publishing progress')
                self.assertEqual([event['first_hit'] for event in cat.events()], [60])
                self.assertEqual(cat.state('analysis_last_chunk')['sampled_frames'], 1)
                self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), original)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg unavailable')
    def test_real_sixty_one_and_sixty_two_second_files_complete_short_tail(self):
        for length in [61, 62]:
            with self.subTest(length=length), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); recordings = root / 'recordings'; recordings.mkdir()
                source = recordings / '00_20261017090000_20261017090200.mp4'
                subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=black:size=160x90:rate=6',
                                '-t', str(length), '-c:v', 'libx264', '-threads', '1', '-g', '6', str(source)], check=True)
                os.utime(source, (time.time()-300, time.time()-300))
                cat = Catalog(recordings, root / 'data', stable_seconds=0); cat.scan(); cat.scan()

                class NoPerson:
                    def score(self, frame):
                        return 0

                worker = Worker(cat, detector=NoPerson())
                worker.step(); worker.step(); worker.step()
                clip = cat.clips()[0]
                self.assertEqual(clip['completed'], length)
                self.assertEqual(clip['status'], 'complete')
                self.assertIsNone(clip['error'])
                self.assertEqual(cat.events(), [])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg unavailable')
    def test_synthetic_preview_thumbnail_and_sampled_frames(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'synthetic.mp4'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                            'testsrc2=size=320x180:rate=12', '-f', 'lavfi', '-i', 'sine=frequency=440',
                            '-t', '7', '-c:v', 'libx264', '-threads', '1', '-g', '12', '-c:a', 'aac', str(source)], check=True)
            preview = root / make_preview(source, root, 1, 4)
            probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                                    'stream=codec_name,width,height,r_frame_rate', '-of', 'csv=p=0', str(preview)], capture_output=True, text=True, check=True)
            self.assertIn('h264,320,180,12/1', probe.stdout)
            thumb = root / make_thumbnail(source, root, 3.5)
            self.assertGreater(thumb.stat().st_size, 1000)
            with self.assertRaises(RuntimeError):
                make_preview(source, root, 8, 1)
            with self.assertRaisesRegex(RuntimeError, '没有解码到任何抽样帧'):
                analyze_chunk(source, root, 60, 60, object())

            class Detector:
                def score(self, frame):
                    self.shape = frame.shape
                    return .8

            detector = Detector()
            hits = analyze_chunk(source, root, 0, 6, detector)
            self.assertEqual(detector.shape, (360, 640, 3))
            self.assertEqual([h[0] for h in hits], [0, 3])
            self.assertTrue(all((root / hit[2]).exists() for hit in hits))
            self.assertTrue(analyze_chunk(source, root, 6, 1, detector))

            # Real seven-second media must never count the filename's120s as analysed.
            recordings = root / 'recordings'; recordings.mkdir()
            renamed = recordings / '00_20261017090000_20261017090200.mp4'
            source.replace(renamed); os.utime(renamed, (time.time()-300, time.time()-300))
            cat = Catalog(recordings, root / 'data', stable_seconds=0); cat.scan(); cat.scan()
            worker = Worker(cat, detector=detector)
            worker.step(); worker.step()
            clip = cat.clips()[0]
            self.assertEqual(clip['duration'], 120)
            self.assertAlmostEqual(clip['media_duration'], 7, places=1)
            self.assertAlmostEqual(clip['completed'], 7, places=1)
            self.assertEqual(clip['status'], 'complete')
            self.assertIsNone(cat.next_analysis())
            with self.assertRaises(ValueError):
                cat.enqueue_preview(clip['id'], 8)
