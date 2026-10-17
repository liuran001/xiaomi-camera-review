"""Conservative per-chunk filtering of redundant empty-scene model calls."""
import cv2
import numpy as np


class EmptySceneGate:
    """Keep decoding every sample; only defer expensive checks of quiet negatives.

    This heuristic cannot prove absence. First/periodic checks and a positive
    hold bound the risk; no score or person event is inferred from motion alone.
    Construct a fresh instance for every analysis chunk (including retries).
    """

    def __init__(self, enabled=True):
        self.enabled = enabled
        self.sampled_frames = self.model_frames = self.skipped_frames = 0
        self.reference = None
        self.last_model = None
        self.person_until = float('-inf')

    def score(self, frame, offset, detector):
        self.sampled_frames += 1
        view = None
        if self.enabled:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            view = cv2.resize(gray, (160, 90), interpolation=cv2.INTER_AREA)
            view = cv2.GaussianBlur(view, (3, 3), 0)
            # Xiaomi's clock is excluded only from motion, never from inference.
            view[:9, :64] = 0
            forced = self.last_model is None or offset - self.last_model >= 15
            held = offset <= self.person_until
            changed = self.reference is None or np.count_nonzero(cv2.absdiff(view, self.reference) >= 8) >= 8
            if not (forced or held or changed):
                self.skipped_frames += 1
                return 0.
        score = detector.score(frame)
        self.model_frames += 1
        self.last_model = offset
        if score:
            self.person_until = offset + 30
        else:
            # Skipped samples never update this reference: slow drift accumulates.
            self.reference = view
        return score
