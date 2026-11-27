"""Application settings (dataclasses) with JSON persistence.

All pixel coordinates are 0-based and relative to the image delivered by the
camera, i.e. relative to the camera ROI (not to the full 2048 x 2048 sensor).
"""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Optional

DEFAULT_SETTINGS_PATH = Path.home() / ".thz2d" / "settings.json"


@dataclass
class CameraSettings:
    backend: str = "simulated"          # "pco" or "simulated"
    interface: str = ""                 # "" = auto, else e.g. "USB 3.0", "CLHS", "Camera Link ME4"
    serial: int = 0                     # 0 = first camera found
    exposure_us: float = 100.0          # exposure time per frame
    delay_us: float = 0.0               # delay between trigger and exposure start
    # Camera ROI, pco convention: 1-based, inclusive (x0, y0, x1, y1).
    # pco.edge requires a vertically symmetric ROI around the sensor centre
    # (rows 1024/1025) - use "Centre ROI vertically" in the Setup tab.
    roi: tuple = (1, 993, 2048, 1056)   # 64 rows -> easily > 1 kHz
    trigger: str = "external exposure start & software trigger"
    pixel_rate_hz: int = 0              # 0 = fastest available
    timestamp: str = "binary"           # binary timestamp -> frame counter for dropped-frame detection
    record_mode: str = "fifo"           # "fifo" (continuous stream) or "sequence" (re-armed every block)
    fifo_frames: int = 400              # driver buffer size in fifo mode
    frame_timeout_s: float = 2.0        # warn if no frame arrives within this time
    sim_rate_hz: float = 1000.0         # simulated camera only


@dataclass
class WindowSettings:
    # window "-" and "+" share the same length and height so they can be subtracted pixel by pixel
    x0_minus: int = 112
    y0_minus: int = 14
    x0_plus: int = 112
    y0_plus: int = 38
    length: int = 1824
    height: int = 12
    flip_minus: bool = False            # mirror the "-" window if the imaging flips it


@dataclass
class ProcessingSettings:
    phase_mode: str = "auto"            # "auto" or "manual"
    manual_ref_phase: int = 0           # 0..3, position of the REF shot inside each 4-shot cycle
    manual_swap: bool = False           # swap the A-only / AB assignment in manual mode
    a_is_fast: bool = True              # pump A chopped at laser/2 (500 Hz), pump B at laser/4 (250 Hz)
    subtract_dark: bool = True
    min_ref_fraction: float = 0.10      # pixels with REF intensity below this fraction of max are masked


@dataclass
class TimeAxisSettings:
    mode: str = "linear"                # "linear" or "pixel" (per-pixel calibration of Gao et al.)
    fs_per_px: float = 10.0
    t0_px: float = 912.0                # pixel (inside the window) that is t = 0
    calib_file: str = ""                # .npz produced by the per-pixel calibration scan


@dataclass
class StageSettings:
    backend: str = "simulated"          # "esp301" or "simulated"
    port: str = "COM3"
    baudrate: int = 921600              # USB virtual COM port; RS-232 is 19200
    rtscts: bool = False                # hardware handshake (needed on the RS-232 port)
    axis: int = 1
    passes: int = 2                     # optical passes per mm of stage travel (retro-reflector = 2)
    direction: int = 1                  # +1 / -1: sign relating +fs to +mm
    move_timeout_s: float = 60.0


@dataclass
class AcquisitionSettings:
    n_avg: int = 25                     # number of 4-shot cycles per block (camera takes 4*N frames)
    fast_n: int = 25
    long_n: int = 500
    autosave: bool = False
    autosave_dir: str = str(Path.home() / "thz2d_data")


@dataclass
class Settings:
    camera: CameraSettings = field(default_factory=CameraSettings)
    windows: WindowSettings = field(default_factory=WindowSettings)
    processing: ProcessingSettings = field(default_factory=ProcessingSettings)
    time_axis: TimeAxisSettings = field(default_factory=TimeAxisSettings)
    stage: StageSettings = field(default_factory=StageSettings)
    acquisition: AcquisitionSettings = field(default_factory=AcquisitionSettings)

    # ------------------------------------------------------------------ io
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        s = cls()
        for f in fields(s):
            sub = getattr(s, f.name)
            src = d.get(f.name, {}) or {}
            for sf in fields(sub):
                if sf.name in src:
                    val = src[sf.name]
                    if isinstance(getattr(sub, sf.name), tuple) and isinstance(val, list):
                        val = tuple(val)
                    setattr(sub, sf.name, val)
        return s

    def save(self, path: Optional[Path] = None) -> None:
        path = Path(path or DEFAULT_SETTINGS_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Settings":
        path = Path(path or DEFAULT_SETTINGS_PATH)
        if path.exists():
            try:
                return cls.from_dict(json.loads(path.read_text()))
            except Exception:  # corrupt file -> defaults
                pass
        return cls()


class SettingsStore:
    """Thread-safe holder: the GUI edits, the acquisition thread takes snapshots."""

    def __init__(self, settings: Settings):
        self._s = settings
        self._lock = threading.Lock()

    def snapshot(self) -> Settings:
        with self._lock:
            return copy.deepcopy(self._s)

    def update(self, section: str, **kwargs) -> None:
        with self._lock:
            sub = getattr(self._s, section)
            for k, v in kwargs.items():
                if not hasattr(sub, k):
                    raise AttributeError(f"{section}.{k}")
                setattr(sub, k, v)

    def get(self, section: str):
        with self._lock:
            return copy.deepcopy(getattr(self._s, section))

    def save(self, path=None):
        with self._lock:
            self._s.save(path)
