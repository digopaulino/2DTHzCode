"""Acquisition thread: owns the camera and the stage, runs jobs from a queue.

All hardware access happens in this one thread, so the camera and the ESP301
never see concurrent commands (the only exception is the STOP button, which
calls ESP301.stop() directly; that driver is lock-protected).

Results are sent to the GUI with Qt signals (queued across threads).
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Optional

import numpy as np
from pyqtgraph.Qt import QtCore

from . import processing as P
from .config import SettingsStore
from .hardware import CameraError, SimulatedStage, StageError, make_camera, make_stage

# jobs that may run between two blocks of a continuous run
_BETWEEN_BLOCK_JOBS = {"stage_move", "stage_read", "stage_motor_on", "configure_camera"}


class AcquisitionThread(QtCore.QThread):
    frame_ready = QtCore.Signal(object)          # np.ndarray (live view)
    block_ready = QtCore.Signal(object)          # processing.BlockResult
    progress = QtCore.Signal(int, int)           # frames done, frames total
    status = QtCore.Signal(str)
    error = QtCore.Signal(str)
    busy_changed = QtCore.Signal(bool, str)      # busy?, job name
    camera_state = QtCore.Signal(bool, object)   # connected?, info dict or message
    stage_state = QtCore.Signal(bool, str)
    stage_position = QtCore.Signal(int, float)   # axis, mm
    dark_ready = QtCore.Signal(object)
    calib_done = QtCore.Signal(object)

    def __init__(self, store: SettingsStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.jobs: "queue.Queue[tuple]" = queue.Queue()
        self.stop_event = threading.Event()
        self.camera = None
        self.stage = None
        self.dark: Optional[np.ndarray] = None
        self._recording = False
        self._stage_pos = {}
        self._last_live = 0.0

    # ------------------------------------------------------------------ API (GUI thread)
    def submit(self, name: str, **kw):
        self.jobs.put((name, kw))

    def request_stop(self):
        self.stop_event.set()

    def stop_stage_now(self):
        """Emergency stop, bypasses the job queue."""
        if self.stage is not None:
            try:
                self.stage.stop()
            except Exception as e:
                self.error.emit(f"stage stop: {e}")

    def shutdown(self):
        self.stop_event.set()
        self.jobs.put(("quit", {}))
        self.wait(5000)

    # ------------------------------------------------------------------ thread loop
    def run(self):
        while True:
            name, kw = self.jobs.get()
            if name == "quit":
                break
            self._execute(name, kw)
        self._close_all()

    def _execute(self, name, kw):
        fn = getattr(self, "_job_" + name, None)
        if fn is None:
            self.error.emit(f"unknown job {name}")
            return
        long_job = name in ("run", "dark", "calib_linear", "calib_pixel")
        if long_job:
            self.stop_event.clear()
            self.busy_changed.emit(True, name)
        try:
            fn(**kw)
        except (CameraError, StageError, ValueError) as e:
            self.error.emit(str(e))
        except Exception as e:  # pragma: no cover - unexpected
            self.error.emit(f"{name}: {e}\n{traceback.format_exc()}")
        finally:
            if long_job:
                self._stop_recording()
                self.busy_changed.emit(False, name)

    def _close_all(self):
        for dev in (self.camera, self.stage):
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------ camera jobs
    def _job_connect_camera(self):
        if self.camera is not None:
            self._stop_recording()
            self.camera.close()
            self.camera = None
        snap = self.store.snapshot()
        cam = make_camera(snap.camera, stage_positions=lambda: self._sim_positions())
        cam.configure(snap.camera)
        self.camera = cam
        self.dark = None
        self.camera_state.emit(True, cam.info())
        self.status.emit(f"camera connected: {cam.name}, image {cam.shape[1]} x {cam.shape[0]}")

    def _sim_positions(self):
        if isinstance(self.stage, SimulatedStage):
            return self.stage.relative_positions()
        return {1: 0.0, 2: 0.0}

    def _job_disconnect_camera(self):
        if self.camera is not None:
            self._stop_recording()
            self.camera.close()
            self.camera = None
        self.camera_state.emit(False, "disconnected")

    def _job_configure_camera(self):
        cam = self._need_camera()
        self._stop_recording()
        snap = self.store.snapshot()
        old_shape = cam.shape
        cam.configure(snap.camera)
        if cam.shape != old_shape:
            self.dark = None
        self.camera_state.emit(True, cam.info())
        self.status.emit("camera settings applied")

    def _job_preview(self, n: int = 4):
        cam = self.camera
        if cam is None:
            return
        frames = cam.snap(n, timeout=self.store.get("camera").frame_timeout_s)
        if frames:
            self.frame_ready.emit(frames[-1])
        else:
            self.status.emit("live view: no frames (no trigger?)")

    def _job_dark(self, n_frames: int = 200):
        cam = self._need_camera()
        cam.start(n_frames)
        self._recording = True
        acc = None
        got = 0
        timeout = self.store.get("camera").frame_timeout_s
        t_last = time.monotonic()
        while got < n_frames and not self.stop_event.is_set():
            frames, _ = cam.read(min(64, n_frames - got), timeout=0.25)
            if not frames:
                if time.monotonic() - t_last > timeout:
                    self.status.emit("dark: waiting for camera triggers ...")
                    t_last = time.monotonic()
                continue
            t_last = time.monotonic()
            arr = np.stack(frames).astype(np.float64)
            acc = arr.sum(0) if acc is None else acc + arr.sum(0)
            got += len(frames)
            self.progress.emit(got, n_frames)
        self._stop_recording()
        if got == 0:
            return
        self.dark = acc / got
        self.dark_ready.emit(self.dark)
        self.status.emit(f"dark image recorded ({got} frames, mean {self.dark.mean():.1f} counts)")

    def _job_clear_dark(self):
        self.dark = None
        self.dark_ready.emit(None)

    # ------------------------------------------------------------------ stage jobs
    def _job_connect_stage(self):
        if self.stage is not None:
            self.stage.close()
            self.stage = None
        ss = self.store.get("stage")
        st = make_stage(ss)
        ver = st.connect()
        self.stage = st
        self.stage_state.emit(True, f"{st.name}: {ver}")
        self._job_stage_read()

    def _job_disconnect_stage(self):
        if self.stage is not None:
            self.stage.close()
            self.stage = None
        self.stage_state.emit(False, "disconnected")

    def _job_stage_read(self):
        st = self._need_stage()
        for ax in st.axes:
            try:
                p = st.position(ax)
            except StageError:
                continue
            self._stage_pos[ax] = p
            self.stage_position.emit(ax, p)

    def _job_stage_motor_on(self, axis: int):
        self._need_stage().motor_on(axis)
        self.status.emit(f"motor {axis} on")

    def _job_stage_move(self, axis: int, mode: str, value: float):
        """mode: 'abs' (mm), 'rel_mm' (mm) or 'rel_fs' (optical delay in fs)."""
        st = self._need_stage()
        ss = self.store.get("stage")
        if mode == "abs":
            target = float(value)
        elif mode == "rel_mm":
            target = st.position(axis) + float(value)
        elif mode == "rel_fs":
            target = st.position(axis) + P.fs_to_mm(float(value), ss.passes, ss.direction)
        else:
            raise ValueError(mode)
        self.status.emit(f"moving motor {axis} to {target:.5f} mm ...")
        pos = st.move_absolute(axis, target, wait=True, timeout=ss.move_timeout_s)
        self._stage_pos[axis] = pos
        self.stage_position.emit(axis, pos)
        self.status.emit(f"motor {axis} at {pos:.5f} mm")

    # ------------------------------------------------------------------ averaging
    def _job_run(self, continuous: bool = True):
        self._need_camera()
        restart = True
        nblocks = 0
        while not self.stop_event.is_set():
            snap = self.store.snapshot()
            res = self._acquire_block(snap.acquisition.n_avg, snap, restart=restart)
            restart = snap.camera.record_mode != "fifo"
            if res is None:
                break
            nblocks += 1
            self.block_ready.emit(res)
            if not continuous:
                break
            if self._run_between_block_jobs():
                restart = True

    def _run_between_block_jobs(self) -> bool:
        """Execute queued stage/camera jobs between two blocks. Returns True if anything ran."""
        ran, deferred = False, []
        while True:
            try:
                name, kw = self.jobs.get_nowait()
            except queue.Empty:
                break
            if name in _BETWEEN_BLOCK_JOBS:
                self._stop_recording()
                try:
                    getattr(self, "_job_" + name)(**kw)
                except (CameraError, StageError, ValueError) as e:
                    self.error.emit(str(e))
                ran = True
            elif name == "preview":
                pass                            # the run already feeds the live view
            else:
                deferred.append((name, kw))
                if name == "quit":
                    self.stop_event.set()
        for job in deferred:
            self.jobs.put(job)
        return ran

    def _acquire_block(self, n_avg: int, snap, restart: bool):
        """Record 4*n_avg consecutive frames and process them. Returns BlockResult or None if stopped."""
        cam = self._need_camera()
        cs = snap.camera
        n_frames = 4 * int(n_avg)
        wm, wp = P.windows_from_settings(snap.windows)
        wm.check(cam.shape)
        wp.check(cam.shape)
        L = wp.width
        h, w = cam.shape

        if restart or not self._recording:
            self._stop_recording()
            cam.start(cs.fifo_frames if cs.record_mode == "fifo" else n_frames)
            self._recording = True

        Ip = np.empty((n_frames, L))
        Im = np.empty((n_frames, L))
        img_sum = np.zeros((4, h, w))
        got = 0
        dropped = 0
        last_counter = None
        t_first = t_last_frame = None
        n_first = 0
        t_wait = time.monotonic()
        last_prog = 0.0

        while got < n_frames:
            if self.stop_event.is_set():
                return None
            frames, counters = cam.read(min(64, n_frames - got), timeout=0.25)
            now = time.monotonic()
            if not frames:
                if now - t_wait > cs.frame_timeout_s:
                    self.status.emit("waiting for camera triggers ... (laser / trigger cable?)")
                    t_wait = now
                if cs.record_mode != "fifo" and got > 0 and getattr(cam, "_seq_next", 0) >= n_frames:
                    raise CameraError("sequence recording ended early")
                continue
            t_wait = now
            if t_first is None:
                t_first = now               # frames of the first chunk may have queued up: not timed
                n_first = len(frames)
            t_last_frame = now
            arr = np.stack(frames)

            # dropped-frame detection: a gap breaks the REF/A/B/AB bookkeeping -> restart the block
            gap_at = None
            for i, c in enumerate(counters):
                if c is not None and last_counter is not None and c != last_counter + 1:
                    gap_at = i
                    dropped += (c - last_counter - 1) if c > last_counter else 1
                if c is not None:
                    last_counter = c
            if getattr(cam, "fifo_overflow", False):
                cam.fifo_overflow = False
                self.status.emit("camera FIFO overflow - frames lost, restarting block "
                                 "(reduce ROI height or increase fifo buffer)")
                self._stop_recording()
                cam.start(cs.fifo_frames)
                self._recording = True
                got, img_sum[:] = 0, 0.0
                last_counter = None
                continue
            if gap_at is not None:
                self.status.emit(f"{dropped} dropped frame(s) detected - restarting block")
                arr = arr[gap_at:]
                got, img_sum[:] = 0, 0.0

            k = arr.shape[0]
            Ip[got:got + k] = P.collapse(arr, wp)
            Im[got:got + k] = P.collapse(arr, wm)
            for p in range(4):
                sel = arr[(p - got) % 4::4]
                if sel.shape[0]:
                    img_sum[p] += sel.sum(0, dtype=np.float64)
            got += k

            if now - self._last_live > 0.2:
                self._last_live = now
                self.frame_ready.emit(arr[-1].copy())
            if now - last_prog > 0.1:
                last_prog = now
                self.progress.emit(got, n_frames)
        self.progress.emit(n_frames, n_frames)

        Ip = Ip.reshape(n_avg, 4, L)
        Im = Im.reshape(n_avg, 4, L)
        proc = snap.processing
        dark = self.dark if (proc.subtract_dark and self.dark is not None
                             and self.dark.shape == (h, w)) else None
        dark_p = P.collapse(dark, wp) if dark is not None else None
        dark_m = P.collapse(dark, wm) if dark is not None else None
        res = P.process_block(Ip, Im, proc, dark_p, dark_m)
        pm = res.phase_map
        res.mean_images = {lab: img_sum[pm.index(lab)] / n_avg - (dark if dark is not None else 0.0)
                           for lab in P.PHASES}
        res.last_frame = arr[-1].copy()
        res.frame_rate = ((n_frames - n_first) / (t_last_frame - t_first)
                          if t_last_frame and t_last_frame > t_first else 0.0)
        res.dropped = dropped
        res.stage_mm = dict(self._stage_pos)
        res.windows = (wm, wp)
        return res

    def _stop_recording(self):
        if self.camera is not None and self._recording:
            try:
                self.camera.stop()
            except Exception:
                pass
        self._recording = False

    # ------------------------------------------------------------------ calibrations
    def _scan(self, axis, delays_fs, n_avg, label):
        """Move through delays (relative to the current position), acquire one block per point."""
        st = self._need_stage()
        ss = self.store.get("stage")
        p0 = st.position(axis)
        results = []
        try:
            for i, d in enumerate(delays_fs):
                if self.stop_event.is_set():
                    break
                target = p0 + P.fs_to_mm(d, ss.passes, ss.direction)
                pos = st.move_absolute(axis, target, wait=True, timeout=ss.move_timeout_s)
                self._stage_pos[axis] = pos
                self.stage_position.emit(axis, pos)
                self.status.emit(f"{label}: point {i + 1}/{len(delays_fs)}  (delay {d:+.0f} fs)")
                snap = self.store.snapshot()
                res = self._acquire_block(n_avg, snap, restart=True)
                if res is None:
                    break
                self.block_ready.emit(res)
                results.append(res)
        finally:
            self._stop_recording()
            pos = st.move_absolute(axis, p0, wait=True, timeout=ss.move_timeout_s)
            self._stage_pos[axis] = pos
            self.stage_position.emit(axis, pos)
        return results

    def _job_calib_linear(self, axis: int, step_fs: float, n_steps: int, channel: str, n_avg: int):
        delays = [i * step_fs for i in range(int(n_steps) + 1)]
        res = self._scan(axis, delays, n_avg, "fs/px calibration")
        if len(res) < 2:
            self.status.emit("calibration aborted")
            return
        delays = np.array(delays[:len(res)], float)
        traces = [r.S[channel] for r in res]
        shifts = np.array([P.pixel_shift(traces[0], tr) for tr in traces])
        fs_per_px = P.fit_fs_per_px(delays, shifts)
        peak_px = float(np.argmax(np.abs(traces[0])))
        self.calib_done.emit(dict(kind="linear", delays_fs=delays, shifts_px=shifts,
                                  fs_per_px=fs_per_px, peak_px=peak_px))

    def _job_calib_pixel(self, axis: int, start_fs: float, stop_fs: float, step_fs: float,
                         channel: str, n_avg: int, out_dir: str):
        delays = np.arange(start_fs, stop_fs + 0.5 * step_fs, step_fs)
        res = self._scan(axis, list(delays), n_avg, "per-pixel calibration")
        if len(res) < 8:
            self.status.emit("per-pixel calibration aborted (need >= 8 points)")
            return
        delays = delays[:len(res)]
        Sp = np.stack([r.Sp[channel] for r in res])
        Sm = np.stack([r.Sm[channel] for r in res])
        calib = P.pixel_calibration(delays, Sp, Sm)
        out = Path(out_dir).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        path = out / time.strftime("calib_%Y%m%d_%H%M%S.npz")
        np.savez(path, Sp_scan=Sp, Sm_scan=Sm, channel=channel, **calib)
        self.calib_done.emit(dict(kind="pixel", path=str(path), calib=calib))

    # ------------------------------------------------------------------ helpers
    def _need_camera(self):
        if self.camera is None:
            raise CameraError("camera not connected")
        return self.camera

    def _need_stage(self):
        if self.stage is None:
            raise StageError("stage not connected")
        return self.stage
