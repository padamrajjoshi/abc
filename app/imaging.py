"""Bounded image decoding shared by all processing paths."""
import base64
import binascii
import io
import warnings

import cv2
import numpy as np
from PIL import Image

MAX_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 4_000_000
MODES = {
    'right4': ['Right index', 'Right middle', 'Right ring', 'Right little'],
    'left4': ['Left little', 'Left ring', 'Left middle', 'Left index'],
    'thumbs': ['Left thumb', 'Right thumb'],
}


def decode_png(data: bytes) -> np.ndarray:
    if not data or len(data) > MAX_BYTES:
        raise ValueError('Image must contain 1 byte to 8 MiB.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(data)) as im:
                if im.format != 'PNG' or im.width * im.height > MAX_PIXELS:
                    raise ValueError('Use a PNG image with at most 4 million pixels.')
                if min(im.size) < 32:
                    raise ValueError('Image dimensions must be at least 32 pixels.')
                return np.asarray(im.convert('L'), dtype=np.uint8).copy()
        except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError('Invalid or oversized PNG image.') from exc


def encode_png(image: np.ndarray, dpi: int = 500) -> bytes:
    """Encode an 8-bit grayscale image to 100% mathematically lossless PNG with exact 500 DPI metadata."""
    im = Image.fromarray(image, mode='L')
    buf = io.BytesIO()
    im.save(buf, format='PNG', dpi=(int(dpi), int(dpi)), optimize=False)
    return buf.getvalue()



def from_base64(value: str) -> bytes:
    if len(value) > (MAX_BYTES * 4 // 3 + 100):
        raise ValueError('Encoded image exceeds size limit.')
    if value.startswith('data:'):
        if not value.startswith('data:image/png;base64,'):
            raise ValueError('Only PNG data URLs are accepted.')
        value = value.split(',', 1)[1]
    try:
        data = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError('Invalid base64 image.') from exc
    decode_png(data)
    return data


def to_base64(data: bytes) -> str:
    return base64.b64encode(data).decode('ascii')


def validate_mode(mode: str) -> list[str]:
    if mode not in MODES:
        raise ValueError('Capture mode must be right4, left4, or thumbs.')
    return MODES[mode]


def convert_image_format(data: bytes, target_format: str = 'TIFF', dpi: int = 500, quality: int = 100) -> tuple[bytes, str, str]:
    """Convert fingerprint image to requested format with 100% full quality and 500 DPI metadata.
    
    Supported formats:
    - TIFF: Lossless LZW compressed, 500 DPI (FBI/NIST standard archival)
    - BMP: Lossless uncompressed Windows Bitmap, 500 DPI (Hardware AFIS SDK standard)
    - PNG: Lossless Deflate compressed, 500 DPI (Universal standard)
    - JPEG / JPG: Maximum fidelity (quality=100, zero chroma subsampling 4:4:4), 500 DPI
    - WEBP: Lossless WebP, 500 DPI
    - RAW / BIN: Uncompressed 8-bit grayscale pixel stream (Width x Height raw raster bytes)
    """
    target = target_format.strip().upper()
    with Image.open(io.BytesIO(data)) as im:
        if im.mode != 'L':
            im = im.convert('L')

        buf = io.BytesIO()
        dpi_tuple = (int(dpi), int(dpi))

        if target in ('TIFF', 'TIF'):
            im.save(buf, format='TIFF', dpi=dpi_tuple, compression='tiff_lzw')
            return buf.getvalue(), 'image/tiff', 'tiff'

        elif target == 'BMP':
            im.save(buf, format='BMP', dpi=dpi_tuple)
            return buf.getvalue(), 'image/bmp', 'bmp'

        elif target == 'PNG':
            im.save(buf, format='PNG', dpi=dpi_tuple, optimize=True)
            return buf.getvalue(), 'image/png', 'png'

        elif target in ('JPEG', 'JPG'):
            im.save(buf, format='JPEG', dpi=dpi_tuple, quality=100, subsampling=0)
            return buf.getvalue(), 'image/jpeg', 'jpg'

        elif target == 'WEBP':
            im.save(buf, format='WEBP', lossless=True)
            return buf.getvalue(), 'image/webp', 'webp'

        elif target in ('RAW', 'BIN', 'GRAY'):
            raw_bytes = im.tobytes()
            return raw_bytes, 'application/octet-stream', 'raw'

        else:
            raise ValueError(f'Unsupported conversion format: {target_format}. Supported: TIFF, BMP, PNG, JPEG, WEBP, RAW.')

