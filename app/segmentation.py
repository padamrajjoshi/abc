"""NFSEG plain-image mode and rotation-aware OpenCV segmentation."""
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np

from .imaging import decode_png, encode_png, validate_mode

log = logging.getLogger(__name__)


class SlapSegmenter:
    def __init__(self, binary=None):
        self.binary = binary or shutil.which('nfseg')

    def segment(self, image, mode='right4') -> list[dict]:
        names = validate_mode(mode)
        if isinstance(image, bytes):
            image = decode_png(image)
        if image.dtype != np.uint8 or image.ndim != 2:
            raise ValueError('Slap must be an 8-bit grayscale image.')
        if self.binary:
            try:
                return self._nfseg(image, mode, names)
            except (OSError, subprocess.SubprocessError, ValueError):
                log.warning('NFSEG failed; using OpenCV segmentation.')
        return self._opencv(image, names)

    @staticmethod
    def _record(index, crop, box, name, engine):
        return {'finger_index': index + 1, 'finger_name': name,
                'image_bytes': encode_png(crop), 'box': [int(v) for v in box],
                'segmentation_engine': engine}

    def _nfseg(self, image, mode, names):
        with tempfile.TemporaryDirectory(prefix='nfseg-') as directory:
            root = Path(directory)
            path = root / 'slap.jpg'
            if not cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 100]):
                raise ValueError('Cannot encode NFSEG input.')
            # Documented positional syntax, raw output, rotation search and correction.
            result = subprocess.run([self.binary, str({'right4': 13, 'left4': 14, 'thumbs': 15}[mode]),
                                     '1', '1', '3', '1', str(path)], cwd=root,
                                    capture_output=True, text=True, timeout=45, check=True)
            rows = []
            pattern = r'(\S+\.raw)\s*->\s*(\d+)\s+(\d+)\s+(\d+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)'
            for match in re.finditer(pattern, result.stdout):
                filename, error, width, height, cx, cy, angle = match.groups()
                if int(error) != 0:
                    raise ValueError('NFSEG reported a segmentation error.')
                width, height = int(width), int(height)
                if not 32 <= width <= 2000 or not 32 <= height <= 2000:
                    raise ValueError('NFSEG output dimensions out of range.')
                output = root / Path(filename).name
                data = output.read_bytes()
                if len(data) != width * height:
                    raise ValueError('Unexpected NFSEG raw output size.')
                crop = np.frombuffer(data, dtype=np.uint8).reshape(height, width).copy()
                # Axis-aligned enclosure of NFSEG's rotated rectangle for the overlay.
                points = cv2.boxPoints(((float(cx), float(cy)), (width, height), float(angle)))
                x0, y0 = np.floor(points.min(axis=0)).astype(int)
                x1, y1 = np.ceil(points.max(axis=0)).astype(int)
                x0, y0 = max(0, x0), max(0, y0)
                x1, y1 = min(image.shape[1], x1), min(image.shape[0], y1)
                rows.append((float(cx), crop, [x0, y0, x1-x0, y1-y0]))
            if len(rows) != len(names):
                raise ValueError('NFSEG did not return the expected finger count.')
            return [self._record(i, crop, box, names[i], 'nist-nfseg')
                    for i, (_, crop, box) in enumerate(sorted(rows, key=lambda r: r[0]))]

    def _opencv(self, image, names):
        blur = cv2.GaussianBlur(image, (7, 7), 0)
        _, mask = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                               cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25)), iterations=2)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                               cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        minimum = max(2000, image.size * .009)
        contours = [c for c in contours if cv2.contourArea(c) > minimum]
        if len(contours) != len(names):
            raise ValueError(f'Detected {len(contours)} fingers; expected {len(names)}. Separate fingers and recapture.')
        contours.sort(key=lambda c: cv2.boundingRect(c)[0] + cv2.boundingRect(c)[2] / 2)
        records = []
        for i, contour in enumerate(contours):
            rect = cv2.minAreaRect(contour)
            (cx, cy), (rw, rh), angle = rect
            if rw > rh:
                rw, rh, angle = rh, rw, angle + 90
            rw, rh = int(round(rw)) + 40, int(round(rh)) + 40
            if min(rw, rh) < 64:
                raise ValueError('Finger region too small.')
            # Isolate each contour before rotating so margins cannot include a neighbor.
            isolated_mask = np.zeros_like(image)
            cv2.drawContours(isolated_mask, [contour], -1, 255, -1)
            isolated = np.where(isolated_mask > 0, image, 255).astype(np.uint8)
            rotation = cv2.getRotationMatrix2D((cx, cy), angle, 1)
            rotated = cv2.warpAffine(isolated, rotation, (image.shape[1], image.shape[0]), borderValue=255)
            # Padded source ensures getRectSubPix never repeats edge pixels.
            rotated = cv2.copyMakeBorder(rotated, rh, rh, rw, rw, cv2.BORDER_CONSTANT, value=255)
            crop = cv2.getRectSubPix(rotated, (rw, rh), (cx + rw, cy + rh))
            x, y, w, h = cv2.boundingRect(contour)
            x0, y0 = max(0, x-20), max(0, y-20)
            x1, y1 = min(image.shape[1], x+w+20), min(image.shape[0], y+h+20)
            records.append(self._record(i, crop, [x0, y0, x1-x0, y1-y0], names[i], 'opencv'))
        return records
