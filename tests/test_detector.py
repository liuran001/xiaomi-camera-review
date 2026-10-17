import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from camera_review.media import PersonDetector


class FakeNet:
    def __init__(self):
        self.output = np.zeros((1, 3549, 85), dtype=np.float32)

    def setInput(self, blob):
        self.blob = blob

    def forward(self):
        return self.output


class DetectorTests(unittest.TestCase):
    def detector(self):
        net = FakeNet()
        with patch('camera_review.media.cv2.dnn.readNetFromONNX', return_value=net), patch('camera_review.media.cv2.dnn.readNetFromCaffe', return_value=net):
            detector = PersonDetector(Path('models'))
        return detector, net

    def test_loads_yolox_onnx(self):
        with patch('camera_review.media.cv2.dnn.readNetFromONNX', return_value=FakeNet()) as onnx, patch('camera_review.media.cv2.dnn.readNetFromCaffe') as caffe:
            PersonDetector(Path('models'))
            onnx.assert_called_once_with(str(Path('models') / 'object_detection_yolox_2022nov.onnx'))
            caffe.assert_not_called()

    def test_bgr_float32_top_left_letterbox_without_normalizing(self):
        detector, net = self.detector()
        frame = np.full((360, 640, 3), (10, 20, 30), dtype=np.uint8)
        self.assertEqual(detector.score(frame), 0)
        self.assertEqual(net.blob.shape, (1, 3, 416, 416))
        self.assertEqual(net.blob.dtype, np.float32)
        np.testing.assert_array_equal(net.blob[0, :, 0, 0], [10, 20, 30])
        np.testing.assert_array_equal(net.blob[0, :, 233, 415], [10, 20, 30])
        np.testing.assert_array_equal(net.blob[0, :, 234, 0], [114, 114, 114])
        np.testing.assert_array_equal(net.blob[0, :, 415, 415], [114, 114, 114])

    def test_person_class_objectness_product_and_threshold(self):
        detector, net = self.detector()
        # Suppress a nonperson row even when its person score would pass.
        net.output[0, 0, 4:7] = [.9, .8, .95]
        # Person probability alone is insufficient: .5*.9=.45.
        net.output[0, 1, 4:6] = [.5, .9]
        self.assertEqual(detector.score(np.zeros((360, 640, 3), np.uint8)), 0)
        net.output[0, 2, 4:6] = [1, .64]
        self.assertEqual(detector.score(np.zeros((360, 640, 3), np.uint8)), 0)
        net.output[0, 2, 4:6] = [1, .65]
        self.assertAlmostEqual(detector.score(np.zeros((360, 640, 3), np.uint8)), .65)
        net.output[0, 3, 4:6] = [.9, .9]
        self.assertAlmostEqual(detector.score(np.zeros((360, 640, 3), np.uint8)), .81, places=6)

    @unittest.skipUnless((Path(__file__).resolve().parents[1] / 'models' / 'object_detection_yolox_2022nov.onnx').is_file(), 'local ignored YOLOX model unavailable')
    def test_real_onnx_load_and_forward_smoke(self):
        with patch('camera_review.media.cv2.dnn.readNetFromCaffe', side_effect=AssertionError('must load ONNX')):
            detector = PersonDetector(Path(__file__).resolve().parents[1] / 'models')
        score = detector.score(np.zeros((360, 640, 3), np.uint8))
        self.assertGreaterEqual(score, 0)
        self.assertLessEqual(score, 1)
