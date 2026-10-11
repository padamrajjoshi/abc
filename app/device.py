"""MORPHS library discovery and deterministic, repeatable simulated acquisition.

The vendor's ABI is proprietary and is not guessed. Native calls are supported
through the versioned adapter ABI in hardware_adapter.h, never through assumed
MORPHS function names. A vendor SDK adapter must declare ABI version 1.
"""
import ctypes
import ctypes.util
import logging
import os
import platform
import threading

import cv2
import numpy as np

from .imaging import validate_mode

log = logging.getLogger(__name__)
WIDTH, HEIGHT, DPI = 1600, 1500, 500


class DeviceInitialization(ctypes.Structure):
    """Studio adapter ABI v1 configuration; NOT a vendor SDK structure."""
    _fields_ = [('size', ctypes.c_uint32), ('width', ctypes.c_uint32),
                ('height', ctypes.c_uint32), ('dpi', ctypes.c_uint32)]


class FrameBuffer(ctypes.Structure):
    """Studio-owned fixed-capacity buffer; adapter may not retain its pointer."""
    _fields_ = [('size', ctypes.c_uint32), ('width', ctypes.c_uint32),
                ('height', ctypes.c_uint32), ('stride', ctypes.c_uint32),
                ('capacity', ctypes.c_uint32), ('pixels', ctypes.POINTER(ctypes.c_uint8))]


class SyntheticSlapGenerator:
    """Identity-seeded curved ridges with impression-seeded optical noise.

    A simulation identity is a repeatable synthetic pattern, not a real subject.
    Different impression seeds alter sensor noise without altering ridge topology.
    """
    def generate(self, mode='right4', identity=1, skin='normal', impression=0):
        names = validate_mode(mode)
        if skin not in ('normal', 'dry', 'moist'):
            raise ValueError('Skin must be normal, dry, or moist.')
        rng = np.random.default_rng(impression)
        image = np.clip(250 + rng.normal(0, 0.7, (HEIGHT, WIDTH)), 0, 255).astype(np.float32)
        n = len(names)
        centers = [290, 630, 970, 1310] if n == 4 else [540, 1060]
        lengths = [760, 890, 830, 640] if mode == 'right4' else [640, 830, 890, 760]
        if n == 2:
            lengths = [720, 720]
        for index, (name, cx, length) in enumerate(zip(names, centers, lengths)):
            # Canonical anatomical id keeps a thumb/finger stable across sessions.
            canonical = {'Right index': 2, 'Right middle': 3, 'Right ring': 4,
                         'Right little': 5, 'Left index': 7, 'Left middle': 8,
                         'Left ring': 9, 'Left little': 10, 'Right thumb': 1,
                         'Left thumb': 6}[name]
            pattern_rng = np.random.default_rng(np.random.SeedSequence([identity, canonical]))
            width = 252 if n == 4 else 330
            pad = 100
            h, w = length + 2 * pad, width + 2 * pad
            y, x = np.mgrid[:h, :w].astype(np.float32)
            x -= w / 2
            y -= h / 2
            angle = pattern_rng.uniform(-0.08, 0.08)
            u, v = x * np.cos(angle) + y * np.sin(angle), -x * np.sin(angle) + y * np.cos(angle)
            radial = (u / (width / 2)) ** 2 + (v / (length / 2)) ** 2
            mask = np.clip((1 - radial) * 16, 0, 1)
            freq = pattern_rng.uniform(8.8, 11.5)
            corex, corey = pattern_rng.uniform(-50, 50), pattern_rng.uniform(-160, 60)
            phase = 2 * np.pi * (np.sqrt((u - corex) ** 2 + ((v - corey) * 0.63) ** 2) / freq)
            phase += 1.5 * np.sin(v / 90) + pattern_rng.uniform(0, 6.28)
            # Local phase dislocations create ridge endings/bifurcations.
            for _ in range(24):
                px, py = pattern_rng.uniform(-90, 90), pattern_rng.uniform(-length * .35, length * .35)
                phase += pattern_rng.choice([-1, 1]) * np.arctan2(v - py, u - px)
            ridges = (0.5 + 0.5 * np.cos(phase)) ** 2
            contrast = {'normal': 165, 'dry': 65, 'moist': 120}[skin]
            patch = 223 - contrast * ridges
            if skin == 'dry':
                patch += 22 * np.sin(u / 8 + v / 13) ** 8
            if skin == 'moist':
                patch = cv2.GaussianBlur(patch, (7, 7), 1.4)
            patch += rng.normal(0, 2, patch.shape)
            cy = 880 - (length - 640) // 3
            x0, y0 = cx - w // 2, cy - h // 2
            target = image[y0:y0+h, x0:x0+w]
            target[:] = target * (1 - mask) + patch * mask
        return np.clip(image, 0, 255).astype(np.uint8)


class MantraDeviceManager:
    def __init__(self):
        self.lock = threading.Lock()
        if os.getenv('DEVICE_MODE', 'auto') not in ('auto', 'simulation', 'hardware'):
            raise RuntimeError('DEVICE_MODE must be auto, simulation, or hardware.')
        self.generator = SyntheticSlapGenerator()
        self.vendor_library = None
        self.adapter = None
        self.reason = 'MORPHS SDK library not found; simulation active.'
        system = platform.system()
        library = 'MorpHs.dll' if system == 'Windows' else 'libMorpHs.so'
        candidates = [os.getenv('MORPHS_LIBRARY'), ctypes.util.find_library('MorpHs'), library]
        for candidate in filter(None, candidates):
            try:
                loader = ctypes.WinDLL if system == 'Windows' else ctypes.CDLL
                self.vendor_library = loader(candidate)
                self.reason = 'Vendor library found; verified SDK adapter required before hardware capture.'
                break
            except OSError:
                continue
        adapter_path = os.getenv('MORPHS_ADAPTER')
        if adapter_path:
            try:
                self._initialize_adapter(adapter_path)
                self.reason = 'MORPHS adapter initialized.'
            except (OSError, AttributeError, RuntimeError) as exc:
                self.reason = f'Hardware adapter unavailable: {exc}'
                log.warning('%s', self.reason)
        if os.getenv('DEVICE_MODE', 'auto') == 'hardware' and self.adapter is None:
            raise RuntimeError(self.reason)

    def _initialize_adapter(self, path):
        adapter = ctypes.CDLL(path)  # adapter ABI uses cdecl on all platforms
        adapter.studio_abi_version.argtypes = []
        adapter.studio_abi_version.restype = ctypes.c_uint32
        if adapter.studio_abi_version() != 1:
            raise RuntimeError('Unsupported scanner adapter ABI.')
        adapter.studio_open.argtypes = [ctypes.POINTER(DeviceInitialization)]
        adapter.studio_open.restype = ctypes.c_int
        adapter.studio_capture.argtypes = [ctypes.c_uint32, ctypes.POINTER(FrameBuffer), ctypes.c_uint32]
        adapter.studio_capture.restype = ctypes.c_int
        adapter.studio_close.argtypes = []
        adapter.studio_close.restype = None
        config = DeviceInitialization(ctypes.sizeof(DeviceInitialization), WIDTH, HEIGHT, DPI)
        if adapter.studio_open(ctypes.byref(config)) != 0:
            adapter.studio_close()
            raise RuntimeError('Scanner initialization failed.')
        self.adapter = adapter

    def status(self):
        simulated = self.adapter is None or os.getenv('DEVICE_MODE') == 'simulation'
        return {'engine': 'synthetic' if simulated else 'morphs-adapter',
                'simulated': simulated, 'detail': self.reason, 'dpi': DPI,
                'width': WIDTH, 'height': HEIGHT}

    def capture(self, mode='right4', identity=1, skin='normal', impression=0):
        validate_mode(mode)
        with self.lock:
            if self.status()['simulated']:
                return self.generator.generate(mode, identity, skin, impression)
            image = np.empty((HEIGHT, WIDTH), dtype=np.uint8)
            frame = FrameBuffer(ctypes.sizeof(FrameBuffer), WIDTH, HEIGHT, WIDTH,
                                image.size, image.ctypes.data_as(ctypes.POINTER(ctypes.c_uint8)))
            result = self.adapter.studio_capture({'right4': 13, 'left4': 14, 'thumbs': 15}[mode],
                                                 ctypes.byref(frame), 15000)
            if result != 0:
                # Never quietly substitute a synthetic identity after hardware failure.
                raise RuntimeError(f'Scanner capture failed (adapter code {result}).')
            if (frame.width, frame.height, frame.stride) != (WIDTH, HEIGHT, WIDTH):
                raise RuntimeError('Adapter returned an unsupported image layout.')
            return image.copy()

    def close(self):
        with self.lock:
            if self.adapter is not None:
                self.adapter.studio_close()
                self.adapter = None
