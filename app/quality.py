"""NFIQ 1 CLI scoring with explicitly identified, uncalibrated estimates."""
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)


class QualityScorer:
    def __init__(self, binary=None):
        self.binary = binary or shutil.which('nfiq')

    def score(self, image: np.ndarray) -> dict:
        if self.binary:
            try:
                with tempfile.TemporaryDirectory(prefix='nfiq-') as directory:
                    path = Path(directory) / 'finger.raw'
                    path.write_bytes(image.tobytes())
                    height, width = image.shape
                    result = subprocess.run([self.binary, '-raw', f'{width},{height},8,500', str(path)],
                                            capture_output=True, text=True, timeout=30, check=True)
                    match = re.fullmatch(r'\s*([1-5])\s*', result.stdout)
                    if not match:
                        raise ValueError('Unexpected NFIQ output.')
                    return self._result(int(match[1]), 'nist-nfiq1', False)
            except (OSError, subprocess.SubprocessError, ValueError):
                log.warning('NFIQ failed; using an estimated quality score.')
        return self._estimate(image)

    @staticmethod
    def _result(score, engine, estimated, **metrics):
        return {'nfiq_score': score, 'status': 'PASS' if score <= 3 else 'FAIL',
                'quality_engine': engine, 'estimated': estimated, **metrics}

    def _estimate(self, image):
        gray = image.astype(np.float32)
        smooth = cv2.GaussianBlur(gray, (21, 21), 0)
        mask = smooth < 225
        if np.count_nonzero(mask) < 1000:
            return self._result(5, 'opencv-estimate', True, clarity=0.0)
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        tenengrad = float(np.var((gx * gx + gy * gy)[mask]))
        laplacian = float(np.var(cv2.Laplacian(gray, cv2.CV_32F)[mask]))
        contrast = float(np.std(gray[mask]))
        # Heuristic ranges deliberately identified as estimates, not calibrated NFIQ.
        clarity = float(np.clip(.45 * np.log1p(tenengrad) / 20 +
                                .30 * np.log1p(laplacian) / 9 + .25 * contrast / 65, 0, 1))
        score = 1 if clarity >= .88 else 2 if clarity >= .70 else 3 if clarity >= .50 else 4 if clarity >= .30 else 5
        return self._result(score, 'opencv-estimate', True, clarity=round(clarity, 4),
                            tenengrad_variance=round(tenengrad, 2), laplacian_variance=round(laplacian, 2))
