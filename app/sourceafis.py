"""Python facade over official SourceAFIS 3.18.1 using JPype.

The optional OpenCV engine is explicitly a demonstration matcher. It is never
reported as SourceAFIS and its score has no biometric error-rate calibration.
"""
import glob
import json
import os
import threading
from pathlib import Path

import cv2
import numpy as np

from .imaging import decode_png

_lock = threading.Lock()


class SourceAFISEngine:
    name = 'sourceafis-3.18.1-java'
    is_demo = False

    def __init__(self):
        import jpype
        self.jpype = jpype
        jars = glob.glob(os.getenv('SOURCEAFIS_CLASSPATH', str(Path(__file__).parents[1] / 'vendor/sourceafis/*.jar')))
        if not jars:
            raise RuntimeError('SourceAFIS JARs absent. Run scripts/setup_sourceafis.sh or use Docker.')
        with _lock:
            if not jpype.isJVMStarted():
                jpype.startJVM('-Djava.awt.headless=true', classpath=jars, convertStrings=True)
        self.Image = jpype.JClass('com.machinezoo.sourceafis.FingerprintImage')
        self.Options = jpype.JClass('com.machinezoo.sourceafis.FingerprintImageOptions')
        self.Template = jpype.JClass('com.machinezoo.sourceafis.FingerprintTemplate')
        self.Matcher = jpype.JClass('com.machinezoo.sourceafis.FingerprintMatcher')

    def _bytes(self, data):
        return self.jpype.JArray(self.jpype.JByte)(data)

    def extract(self, image_png):
        import cbor2
        template = self.Template(self.Image(self._bytes(image_png), self.Options().dpi(500)))
        serialized = bytes(template.toByteArray())
        persistent = cbor2.loads(serialized)
        points = list(zip(persistent['positionsX'], persistent['positionsY']))
        if len(points) < 8:
            raise ValueError('Too few extracted minutiae; recapture.')
        return serialized, {'minutiae_count': len(points), 'feature_count': len(points),
                            'feature_kind': 'minutiae', 'points': points}

    def prepare(self, template_bytes):
        return self.Matcher(self.Template(self._bytes(template_bytes)))

    def match(self, matcher, candidate_bytes):
        return float(matcher.match(self.Template(self._bytes(candidate_bytes))))


class OpenCVDemoEngine:
    name = 'opencv-orb-demo-v1'
    is_demo = True

    def extract(self, image_png):
        image = decode_png(image_png)
        orb = cv2.ORB_create(nfeatures=1200, edgeThreshold=20, fastThreshold=12)
        points, descriptors = orb.detectAndCompute(image, None)
        if descriptors is None or len(points) < 12:
            raise ValueError('Too few ridge features; recapture with better contact.')
        serialized = json.dumps({'points': [[p.pt[0], p.pt[1]] for p in points],
                                 'descriptors': descriptors.tolist()}, separators=(',', ':')).encode()
        return serialized, {'minutiae_count': None, 'feature_count': len(points),
                            'feature_kind': 'ORB features (demo)',
                            'points': [[round(p.pt[0], 1), round(p.pt[1], 1)] for p in points[:150]]}

    def prepare(self, template_bytes):
        data = json.loads(template_bytes)
        return np.asarray(data['points'], np.float32), np.asarray(data['descriptors'], np.uint8)

    def match(self, matcher, candidate_bytes):
        probe_points, probe_descriptors = matcher
        candidate_points, candidate_descriptors = self.prepare(candidate_bytes)
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(probe_descriptors, candidate_descriptors, k=2)
        good = [p[0] for p in pairs if len(p) == 2 and p[0].distance < .72 * p[1].distance]
        if len(good) < 8:
            return 0.0
        source = np.asarray([probe_points[m.queryIdx] for m in good])
        target = np.asarray([candidate_points[m.trainIdx] for m in good])
        _, inliers = cv2.estimateAffinePartial2D(source, target, method=cv2.RANSAC,
                                               ransacReprojThreshold=4)
        if inliers is None:
            return 0.0
        count = int(inliers.sum())
        # Geometric support and descriptor coverage prevent a few repeated ridges
        # from producing a high demonstration score.
        coverage = count / max(1, min(len(probe_points), len(candidate_points)))
        return round(float(100 * min(1, count / 100) * min(1, coverage / .35)), 3)


def create_engine():
    requested = os.getenv('MATCHER_ENGINE', 'auto')
    if requested not in ('auto', 'sourceafis', 'demo'):
        raise RuntimeError('MATCHER_ENGINE must be auto, sourceafis, or demo.')
    if requested != 'demo':
        try:
            return SourceAFISEngine()
        except Exception as exc:
            if requested == 'sourceafis':
                raise RuntimeError(f'SourceAFIS initialization failed: {exc}') from exc
    return OpenCVDemoEngine()
