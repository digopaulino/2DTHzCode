"""Camera back-ends.

``PcoCamera`` wraps Excelitas PCO's official Python package (``pip install pco``,
tested against pco 2.6, which ships pco.sdk + pco.recorder for Windows/Linux).

Acquisition model used by the rest of the program::

    cam.open(); cam.configure(settings)
    cam.start(n_frames)          # arm + start recording (fifo: n_frames = buffer size)
    frames, counters = cam.read(max_n, timeout)   # oldest frames first
    cam.stop(); cam.close()

Frames are returned as uint16 arrays (h, w); counters are the camera's hardware
image counters (from the binary timestamp) or None. They are used to detect
dropped frames, which would scramble the 4-shot REF/A/B/AB cycle.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np


class CameraError(RuntimeError):
    pass


class CameraBase:
    name = "camera"

    def open(self) -> None: ...
    def close(self) -> None: ...
    def configure(self, cs) -> None: ...
    def start(self, n_frames: int) -> None: ...
    def stop(self) -> None: ...

    def read(self, max_n: int, timeout: float = 1.0) -> Tuple[List[np.ndarray], List[Optional[int]]]:
        raise NotImplementedError

    @property
    def shape(self) -> Tuple[int, int]:
        raise NotImplementedError

    def info(self) -> Dict[str, object]:
        return {}

    def snap(self, n: int = 4, timeout: float = 2.0) -> List[np.ndarray]:
        """Record a few frames (for the live view) and stop."""
        self.start(max(4, n))
        out: List[np.ndarray] = []
        t_end = time.monotonic() + timeout
        try:
            while len(out) < n and time.monotonic() < t_end:
                fr, _ = self.read(n - len(out), timeout=max(0.01, t_end - time.monotonic()))
                out.extend(fr)
        finally:
            self.stop()
        return out


# ============================================================================ PCO
class PcoCamera(CameraBase):
    """PCO.edge 4.2 through the official ``pco`` Python package."""

    name = "PCO"

    def __init__(self):
        self.cam = None
        self._mode = "fifo"
        self._seq_next = 0
        self._seq_total = 0
        self._shape = (0, 0)
        self._last_counter = None
        self._timestamp_on = False
        self.fifo_overflow = False

    # -------------------------------------------------------------- connection
    def open(self, interface: str = "", serial: int = 0) -> None:
        try:
            import pco  # official Excelitas PCO package
        except ImportError as e:  # pragma: no cover - depends on lab PC
            raise CameraError("The 'pco' package is not installed: run  pip install pco") from e
        kw = {}
        if interface:
            kw["interface"] = interface
        if serial:
            kw["serial"] = int(serial)
        try:
            self.cam = pco.Camera(**kw)
        except Exception as e:  # pragma: no cover
            raise CameraError(f"Could not open the PCO camera ({e}). Is Camware closed?") from e

    def close(self) -> None:
        if self.cam is not None:
            try:
                self.stop()
            finally:
                try:
                    self.cam.close()
                finally:
                    self.cam = None

    # -------------------------------------------------------------- settings
    def configure(self, cs) -> None:
        cam = self._need()
        if cam.is_recording:
            cam.stop()
        desc = cam.description
        x0, y0, x1, y1 = (int(v) for v in cs.roi)
        if desc.get("roi is vert symmetric"):
            H = desc["max height"]
            if y0 - 1 != H - y1:
                raise CameraError(
                    f"This camera needs a vertically symmetric ROI: y0-1 must equal {H}-y1 "
                    f"(got y0={y0}, y1={y1}). Use 'Centre ROI vertically'.")
        conf = {
            "exposure time": cs.exposure_us * 1e-6,
            "delay time": cs.delay_us * 1e-6,
            "roi": (x0, y0, x1, y1),
            "trigger": cs.trigger,
            "acquire": "auto",
        }
        if desc.get("has timestamp", True):
            conf["timestamp"] = cs.timestamp
        if cs.pixel_rate_hz:
            conf["pixel rate"] = int(cs.pixel_rate_hz)
        elif desc.get("pixelrates"):
            conf["pixel rate"] = int(max(desc["pixelrates"]))
        try:
            cam.configuration = conf
        except Exception as e:
            raise CameraError(f"Camera rejected the configuration: {e}") from e
        self._timestamp_on = cs.timestamp in ("binary", "binary & ascii")
        self._mode = cs.record_mode
        roi = cam.configuration["roi"]
        self._shape = (roi[3] - roi[1] + 1, roi[2] - roi[0] + 1)

    @property
    def shape(self):
        return self._shape

    def info(self):
        cam = self._need()
        out = {"name": cam.camera_name, "serial": cam.camera_serial, "interface": cam.interface}
        try:
            t = cam.sdk.get_temperature()
            out["sensor °C"] = t.get("sensor temperature")
            out["camera °C"] = t.get("camera temperature")
        except Exception:
            pass
        try:
            conf = cam.configuration
            out["exposure µs"] = round(conf["exposure time"] * 1e6, 3)
            out["roi"] = conf["roi"]
            out["trigger"] = conf["trigger"]
            out["pixel rate"] = conf["pixel rate"]
        except Exception:
            pass
        return out

    # -------------------------------------------------------------- acquisition
    def start(self, n_frames: int) -> None:
        cam = self._need()
        if cam.is_recording:
            cam.stop()
        self._last_counter = None
        self.fifo_overflow = False
        if self._mode == "fifo":
            # continuous stream: image(0) always returns (and releases) the oldest frame
            cam.record(number_of_images=max(4, int(n_frames)), mode="fifo")
        else:
            cam.record(number_of_images=max(1, int(n_frames)), mode="sequence non blocking")
            self._seq_next = 0
            self._seq_total = int(n_frames)

    def stop(self) -> None:
        if self.cam is not None:
            try:
                self.cam.stop()
            except Exception:
                pass

    def read(self, max_n: int, timeout: float = 1.0):
        cam = self._need()
        frames: List[np.ndarray] = []
        counters: List[Optional[int]] = []
        t_end = time.monotonic() + timeout
        while True:
            st = cam.rec.get_status()
            if st.get("dwLastError", 0):
                raise CameraError(f"pco.recorder error 0x{st['dwLastError']:08X} (buffer overflow?)")
            if self._mode == "fifo":
                if st.get("bFIFOOverflow"):
                    self.fifo_overflow = True
                avail = int(st["dwProcImgCount"])          # images waiting in the FIFO
                if avail == 0 and not st.get("bIsRunning", True):
                    raise CameraError("recording stopped unexpectedly")
            else:
                avail = int(st["dwProcImgCount"]) - self._seq_next
            take = min(avail, max_n - len(frames))
            for _ in range(take):
                idx = 0 if self._mode == "fifo" else self._seq_next
                img, meta = cam.image(image_index=idx)
                if self._mode != "fifo":
                    self._seq_next += 1
                c = self._counter(meta)
                if self._mode == "fifo" and c is not None and c == self._last_counter:
                    raise CameraError("FIFO returned the same frame twice; set record mode to 'sequence'.")
                self._last_counter = c
                if self._timestamp_on and img.shape[0] > 1:
                    img = img.copy()
                    img[0, :14] = img[1, :14]       # binary time stamp lives in the first 14 pixels
                frames.append(img)
                counters.append(c)
            if len(frames) >= max_n or time.monotonic() >= t_end:
                break
            if self._mode != "fifo" and self._seq_next >= self._seq_total:
                break
            if take == 0:
                time.sleep(0.0005)
        return frames, counters

    @staticmethod
    def _counter(meta) -> Optional[int]:
        ts = meta.get("timestamp") if isinstance(meta, dict) else None
        if ts and "image counter" in ts:
            return int(ts["image counter"])
        return None

    def _need(self):
        if self.cam is None:
            raise CameraError("camera not connected")
        return self.cam


# ============================================================================ simulator
class SimulatedCamera(CameraBase):
    """Synthetic echelon images with a REF/A/B/AB differential-chopping sequence.

    The THz fields depend on the simulated stage: axis 1 = EO probe delay,
    axis 2 = delay of pump B relative to pump A.
    """

    name = "Simulated"

    def __init__(self, stage_positions: Optional[Callable[[], Dict[int, float]]] = None, seed: int = 1):
        self.stage_positions = stage_positions or (lambda: {1: 0.0, 2: 0.0})
        self.rng = np.random.default_rng(seed)
        self.rate = 1000.0
        self._shape = (64, 2048)
        self._t0 = 0.0
        self._emitted = 0
        self._counter = 0
        self._phase0 = 0
        self._running = False
        self._n_target = 0
        self._mode = "fifo"
        self._lock = threading.Lock()
        self._exposure_us = 100.0
        self._build()

    def _build(self):
        h, w = self._shape
        x = np.arange(w)
        self.x = x
        # pixel -> probe time (fs): ~10 fs/px with a slight non-linearity, different for each polarization
        xc = (x - w / 2) / (w / 2)
        self.t_plus = 10.0 * (x - w / 2) + 150.0 * xc ** 2
        self.t_minus = 10.0 * (x - w / 2) + 150.0 * xc ** 2 + 30.0
        beam = np.exp(-((x - w / 2) / (0.42 * w)) ** 6) * (0.9 + 0.1 * np.cos(x / 37.0))
        self.prof_plus = 1600.0 * beam
        self.prof_minus = 1500.0 * beam * (1 + 0.03 * xc)
        y = np.arange(h)
        self.v_plus = np.exp(-0.5 * ((y - 0.69 * h) / 2.2) ** 2)
        self.v_minus = np.exp(-0.5 * ((y - 0.31 * h) / 2.2) ** 2)
        self.v_plus /= self.v_plus.sum() / 6
        self.v_minus /= self.v_minus.sum() / 6
        self.noise_bank = (self.rng.normal(0, 3.0, size=(4, h, w + 512))).astype(np.float32)

    # API -----------------------------------------------------------------
    def open(self, *a, **k):
        pass

    def close(self):
        self.stop()

    def configure(self, cs):
        x0, y0, x1, y1 = (int(v) for v in cs.roi)
        shape = (y1 - y0 + 1, x1 - x0 + 1)
        if shape[0] < 8 or shape[1] < 64:
            raise CameraError("simulated ROI too small")
        self.rate = float(cs.sim_rate_hz)
        self._mode = cs.record_mode
        self._exposure_us = cs.exposure_us
        if shape != self._shape:
            self._shape = shape
            self._build()

    @property
    def shape(self):
        return self._shape

    def info(self):
        return {"name": "Simulated pco.edge 4.2", "rate Hz": self.rate, "image": f"{self._shape[1]} x {self._shape[0]}"}

    def start(self, n_frames):
        with self._lock:
            self._running = True
            self._t0 = time.monotonic()
            self._emitted = 0
            self._n_target = n_frames if self._mode != "fifo" else 1 << 62
            self._phase0 = int(self.rng.integers(4))   # camera starts at a random point of the chopper cycle
            self._counter += 7                          # counters keep running between recordings

    def stop(self):
        with self._lock:
            self._running = False

    def read(self, max_n, timeout=1.0):
        frames, counters = [], []
        t_end = time.monotonic() + timeout
        while len(frames) < max_n:
            with self._lock:
                if not self._running:
                    break
                due = int((time.monotonic() - self._t0) * self.rate) - self._emitted
                due = min(due, max_n - len(frames), self._n_target - self._emitted)
            if due <= 0:
                if time.monotonic() >= t_end or self._emitted >= self._n_target:
                    break
                time.sleep(0.001)
                continue
            for _ in range(due):
                frames.append(self._frame(self._emitted))
                counters.append(self._counter)
                self._emitted += 1
                self._counter += 1
            if time.monotonic() >= t_end:
                break
        return frames, counters

    # physics ---------------------------------------------------------------
    @staticmethod
    def _pulse(t_fs, t0, amp=1.0, width=350.0):
        u = (t_fs - t0) / width
        return amp * -u * np.exp(-u * u) * 2.33

    def _fields(self, t_fs, a_on, b_on):
        pos = self.stage_positions()
        tau = pos.get(1, 0.0) * 2 / 2.99792458e-4        # probe delay (fs), double pass
        tau_ab = pos.get(2, 0.0) * 2 / 2.99792458e-4     # B relative to A (fs)
        t = t_fs + tau
        tA, tB = -3000.0, 2000.0 + tau_ab
        E = np.zeros_like(t)
        if a_on:
            E += self._pulse(t, tA, 1.0)
        if b_on:
            E += self._pulse(t, tB, 0.7)
        if a_on and b_on:   # weak nonlinear signal: damped 0.6 THz ringing after the later pulse
            ts = max(tA, tB)
            env = np.where(t > ts, np.exp(-(t - ts) / 4000.0) * (1 - np.exp(-(t - ts) / 300.0)), 0.0)
            E += 0.1 * env * np.sin(2 * np.pi * 0.6e-3 * (t - ts))
        return E

    def _frame(self, k):
        phase = (self._phase0 + k) % 4             # 0 REF, 1 A, 2 B, 3 AB   (A chopped at laser/2)
        a_on, b_on = phase in (1, 3), phase in (2, 3)
        kappa = 0.02
        Ep = self._fields(self.t_plus, a_on, b_on)
        Em = self._fields(self.t_minus, a_on, b_on)
        laser = 1 + 0.01 * self.rng.normal()
        n1 = self.rng.normal(0.0, 1.0, size=(2, self.x.size))
        Ip = self.prof_plus * laser * (1 + kappa * Ep) + 3.0 * n1[0]
        Im = self.prof_minus * laser * (1 - kappa * Em) + 3.0 * n1[1]
        img = np.outer(self.v_plus, Ip) + np.outer(self.v_minus, Im) + 100.0
        s = int(self.rng.integers(512))
        img += self.noise_bank[k % 4][:, s:s + self.x.size]
        return np.clip(img, 0, 65535).astype(np.uint16)


def make_camera(cs, stage_positions=None) -> CameraBase:
    if cs.backend == "pco":
        cam = PcoCamera()
        cam.open(cs.interface, cs.serial)
        return cam
    return SimulatedCamera(stage_positions)
