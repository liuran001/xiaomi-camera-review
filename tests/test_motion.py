import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from camera_review.media import analyze_chunk

try:
    from camera_review.motion import EmptySceneGate
except ImportError:
    EmptySceneGate = None


class Recorder:
    def __init__(self, value=0):
        self.value, self.calls = value, 0

    def score(self, frame):
        self.calls += 1
        return self.value


class MotionTests(unittest.TestCase):
    def gate(self, **kwargs):
        self.assertIsNotNone(EmptySceneGate, 'empty scenes need a bounded model gate')
        return EmptySceneGate(**kwargs)

    def frame(self):
        return np.full((360, 640, 3), 80, np.uint8)

    def test_empty_scene_forces_first_and_fifteen_second_rechecks(self):
        gate, detector = self.gate(), Recorder()
        inferred = []
        for offset in range(0, 60, 3):
            before = detector.calls
            self.assertEqual(gate.score(self.frame(), offset, detector), 0)
            if detector.calls != before:
                inferred.append(offset)
        self.assertEqual(inferred, [0, 15, 30, 45])
        self.assertEqual((gate.sampled_frames, gate.model_frames, gate.skipped_frames), (20, 4, 16))

    def test_clock_and_small_sensor_noise_do_not_trigger_the_model(self):
        gate, detector = self.gate(), Recorder()
        rng = np.random.default_rng(17)
        for offset in range(0, 15, 3):
            frame = np.clip(self.frame().astype(np.int16) + rng.integers(-6, 7, (360, 640, 3)), 0, 255).astype(np.uint8)
            cv2.putText(frame, f'2026/10/17 03:00:{offset:02}', (10, 24), cv2.FONT_HERSHEY_SIMPLEX, .5, (255, 255, 255), 1)
            gate.score(frame, offset, detector)
        self.assertEqual(detector.calls, 1)

    def test_small_change_outside_the_clock_mask_triggers_immediately(self):
        gate, detector = self.gate(), Recorder()
        gate.score(self.frame(), 0, detector)
        frame = self.frame()
        frame[120:144, 400:416] = 200
        detector.value = .8
        self.assertEqual(gate.score(frame, 3, detector), .8)
        self.assertEqual(detector.calls, 2)

    def test_slow_change_accumulates_against_last_inferred_reference(self):
        gate, detector = self.gate(), Recorder()
        gate.score(self.frame(), 0, detector)
        for offset in [3, 6]:
            frame = self.frame()
            frame[160:220, 360:440] += offset
            gate.score(frame, offset, detector)
        self.assertEqual(detector.calls, 1)
        frame = self.frame()
        frame[160:220, 360:440] += 9
        gate.score(frame, 9, detector)
        self.assertEqual(detector.calls, 2)

    def test_stationary_detected_person_is_never_skipped(self):
        gate, detector = self.gate(), Recorder(.8)
        scores = [gate.score(self.frame(), offset, detector) for offset in range(0, 60, 3)]
        self.assertEqual(scores, [.8] * 20)
        self.assertEqual((gate.model_frames, gate.skipped_frames), (20, 0))

    def test_person_hold_keeps_dense_checks_after_last_hit_without_inventing_hits(self):
        gate, detector = self.gate(), Recorder(.8)
        self.assertEqual(gate.score(self.frame(), 0, detector), .8)
        detector.value = 0
        for offset in range(3, 31, 3):
            self.assertEqual(gate.score(self.frame(), offset, detector), 0)
        self.assertEqual(detector.calls, 11)
        gate.score(self.frame(), 33, detector)
        self.assertEqual(detector.calls, 11)

    def test_forced_recheck_can_find_person_in_ignored_motion_region(self):
        gate, detector = self.gate(), Recorder()
        gate.score(self.frame(), 0, detector)
        frame = self.frame()
        frame[8:24, 40:120] = 200
        detector.value = .8
        for offset in [3, 6, 9, 12]:
            self.assertEqual(gate.score(frame, offset, detector), 0)
        self.assertEqual(gate.score(frame, 15, detector), .8)
        self.assertEqual(detector.calls, 2)

    def test_disabled_gate_runs_every_model_call(self):
        gate, detector = self.gate(enabled=False), Recorder(.8)
        for offset in range(0, 60, 3):
            self.assertEqual(gate.score(self.frame(), offset, detector), .8)
        self.assertEqual((gate.model_frames, gate.skipped_frames), (20, 0))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg unavailable')
    def test_real_decoder_reduces_calls_without_inventing_hits(self):
        gate = self.gate()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'empty.mp4'
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=gray:size=640x360:rate=6',
                            '-t', '60', '-c:v', 'libx264', '-threads', '1', '-g', '6', str(source)], check=True)
            before = source.stat().st_size, source.stat().st_mtime_ns
            baseline, gated = Recorder(), Recorder()
            self.assertEqual(analyze_chunk(source, root, 0, 60, baseline), [])
            self.assertEqual(analyze_chunk(source, root, 0, 60, gated, gate=gate), [])
            self.assertEqual((baseline.calls, gated.calls, gate.skipped_frames), (20, 4, 16))
            self.assertEqual(before, (source.stat().st_size, source.stat().st_mtime_ns))
            self.assertEqual(list(root.glob('*.jpg')), [])
