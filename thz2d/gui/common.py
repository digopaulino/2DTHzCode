"""Shared GUI pieces: the data model, colours, the big-number readout and small helpers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from .. import processing as P
from ..config import SettingsStore

CH_COLORS = {"AB": "#f2c94c", "A": "#9b9bff", "B": "#56ccf2", "NL": "#ff6b6b", "REF": "#dddddd"}
MINUS_COLOR = "#8c8cff"      # I(-) window (as in the lab's LabVIEW screenshots)
PLUS_COLOR = "#ffff3c"       # I(+) window


class Model(QtCore.QObject):
    """Holds the latest result and the time axis; tabs listen to its signals."""

    windows_changed = QtCore.Signal()
    time_axis_changed = QtCore.Signal()
    result_changed = QtCore.Signal()

    def __init__(self, store: SettingsStore):
        super().__init__()
        self.store = store
        self.result: Optional[P.BlockResult] = None
        self.calib: Optional[dict] = None
        f = store.get("time_axis").calib_file
        if f:
            try:
                self.load_calibration(f, emit=False)
            except Exception:
                store.update("time_axis", calib_file="", mode="linear")

    # ------------------------------------------------------------------ results
    def set_result(self, res: P.BlockResult):
        self.result = res
        self.result_changed.emit()

    def time_axis(self) -> P.TimeAxis:
        return P.TimeAxis.from_settings(self.store.get("time_axis"), self.calib)

    def trace(self, channel: str, res: Optional[P.BlockResult] = None):
        """Balanced waveform of a channel on the current time axis: (t_ps, S, sem, warning)."""
        res = res or self.result
        if res is None:
            return None
        ax = self.time_axis()
        valid = res.valid
        t, S = ax.combine(res.Sp[channel], res.Sm[channel], valid)
        warn = ax.warning
        if ax.mode == "pixel" and not warn:
            _, sem = ax.combine(res.S_sem[channel], np.zeros_like(res.S_sem[channel]), valid)
            return t, S, np.abs(sem), warn
        sem = res.S_sem[channel]
        v = valid
        if ax.fs_per_px < 0:
            sem, v = sem[::-1], v[::-1]
        idx = np.flatnonzero(v)
        if idx.size >= 2:
            sl = slice(idx[0], idx[-1] + 1)
            t, S, sem = t[sl], S[sl], sem[sl]
        return t, S, sem, warn

    # ------------------------------------------------------------------ calibration
    def load_calibration(self, path: str, emit: bool = True):
        d = np.load(path, allow_pickle=False)
        self.calib = {k: d[k] for k in ("tp", "tm", "ap", "am", "tp_raw", "tm_raw", "x0", "taus_fs") if k in d}
        self.store.update("time_axis", calib_file=str(path))
        if emit:
            self.time_axis_changed.emit()

    def set_calibration(self, calib: dict, path: str):
        self.calib = calib
        self.store.update("time_axis", calib_file=str(path), mode="pixel")
        self.time_axis_changed.emit()

    # ------------------------------------------------------------------ saving
    def save_result(self, path: Path, res: Optional[P.BlockResult] = None):
        res = res or self.result
        if res is None:
            raise ValueError("nothing to save yet")
        out = {}
        for c in P.CHANNELS:
            t, S, sem, _ = self.trace(c, res)
            out[f"t_{c}_ps"] = t
            out[f"S_{c}"] = S
            out[f"S_{c}_sem"] = sem
            out[f"Sp_{c}_px"] = res.Sp[c]
            out[f"Sm_{c}_px"] = res.Sm[c]
        for lab in P.PHASES:
            out[f"Ip_{lab}_px"] = res.Ip_mean[lab]
            out[f"Im_{lab}_px"] = res.Im_mean[lab]
        pm = res.phase_map
        out["phase_map_ref_a_b_ab"] = np.array([pm.ref, pm.a, pm.b, pm.ab])
        out["n_cycles"] = res.n_cycles
        out["timestamp"] = res.timestamp
        out["stage_mm"] = np.array([res.stage_mm.get(1, np.nan), res.stage_mm.get(2, np.nan)])
        out["valid"] = res.valid
        out["settings_json"] = json.dumps(self.store.snapshot().to_dict())
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        np.savez(path, **out)
        return path


class BigNumber(QtWidgets.QFrame):
    """Large numeric readout: title / value / unit / small subtitle."""

    def __init__(self, title: str, color: str = "#ffffff", unit: str = "", size: int = 30, parent=None):
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.StyledPanel)
        self.setMinimumWidth(170)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(8, 4, 8, 4)
        lay.setSpacing(0)
        self.title = QtWidgets.QLabel(title)
        self.title.setStyleSheet(f"color:{color}; font-weight:bold; font-size:13px;")
        self.value = QtWidgets.QLabel("—")
        self.value.setStyleSheet(f"color:{color}; font-size:{size}px; font-weight:bold;")
        self.value.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.unit = QtWidgets.QLabel(unit)
        self.unit.setStyleSheet("color:#aaaaaa; font-size:11px;")
        self.sub = QtWidgets.QLabel("")
        self.sub.setStyleSheet("color:#bbbbbb; font-size:11px;")
        for w in (self.title, self.value, self.unit, self.sub):
            lay.addWidget(w)
        lay.addStretch(1)

    def set(self, value: Optional[float], sub: str = "", fmt: str = "{:.3g}"):
        self.value.setText("—" if value is None or not np.isfinite(value) else fmt.format(value))
        self.sub.setText(sub)


def dspin(value=0.0, lo=-1e9, hi=1e9, decimals=3, step=1.0, suffix="", width=None):
    s = QtWidgets.QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(decimals)
    s.setSingleStep(step)
    s.setValue(value)
    s.setKeyboardTracking(False)
    if suffix:
        s.setSuffix(" " + suffix)
    if width:
        s.setMinimumWidth(width)
    return s


def ispin(value=0, lo=0, hi=100000, step=1, suffix=""):
    s = QtWidgets.QSpinBox()
    s.setRange(lo, hi)
    s.setSingleStep(step)
    s.setValue(int(value))
    s.setKeyboardTracking(False)
    if suffix:
        s.setSuffix(" " + suffix)
    return s


def plot_widget(xlabel="", ylabel="", title=None):
    pw = pg.PlotWidget(title=title)
    pw.showGrid(x=True, y=True, alpha=0.25)
    if xlabel:
        pw.setLabel("bottom", xlabel)
    if ylabel:
        pw.setLabel("left", ylabel)
    return pw


def fmt_e3(v: float) -> str:
    return f"{v * 1e3:.3g}"


def timestamp_name(prefix="thz", ext=".npz") -> str:
    return time.strftime(f"{prefix}_%Y%m%d_%H%M%S") + ext
