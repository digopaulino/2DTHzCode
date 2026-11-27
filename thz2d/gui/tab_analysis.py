"""Tab 3 - one channel in detail: time-domain trace on top, FFT amplitude below."""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets

from .. import processing as P
from .common import CH_COLORS, BigNumber, Model, dspin, ispin, plot_widget


class AnalysisTab(QtWidgets.QWidget):
    def __init__(self, model: Model, parent=None):
        super().__init__(parent)
        self.model = model
        self.hold = None      # (t, S, f, A) of a held trace
        outer = QtWidgets.QVBoxLayout(self)

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("Channel"))
        self.cmb_ch = QtWidgets.QComboBox()
        self.cmb_ch.addItems(list(P.CHANNELS))
        self.cmb_ch.setCurrentText("A")
        bar.addWidget(self.cmb_ch)
        bar.addSpacing(16)
        bar.addWidget(QtWidgets.QLabel("FFT scale"))
        self.cmb_scale = QtWidgets.QComboBox()
        self.cmb_scale.addItems(["linear", "log"])
        bar.addWidget(self.cmb_scale)
        bar.addWidget(QtWidgets.QLabel("window"))
        self.cmb_win = QtWidgets.QComboBox()
        self.cmb_win.addItems(["hann", "tukey", "none"])
        self.cmb_win.setCurrentText("tukey")
        bar.addWidget(self.cmb_win)
        bar.addWidget(QtWidgets.QLabel("zero-pad ×"))
        self.sp_pad = ispin(4, 1, 64)
        bar.addWidget(self.sp_pad)
        bar.addWidget(QtWidgets.QLabel("f max"))
        self.sp_fmax = dspin(4.0, 0.1, 100, 2, 0.5, "THz")
        bar.addWidget(self.sp_fmax)
        bar.addSpacing(16)
        self.btn_hold = QtWidgets.QPushButton("Hold trace")
        self.btn_unhold = QtWidgets.QPushButton("Clear held")
        bar.addWidget(self.btn_hold)
        bar.addWidget(self.btn_unhold)
        bar.addStretch(1)
        outer.addLayout(bar)

        body = QtWidgets.QHBoxLayout()
        plots = QtWidgets.QVBoxLayout()
        self.pw_t = plot_widget("time (ps)", "ΔI/I")
        self.pw_f = plot_widget("frequency (THz)", "|FFT| (arb.)")
        self.c_hold_t = self.pw_t.plot(pen=pg.mkPen("#888", width=1.5, style=QtCore.Qt.DashLine))
        self.c_t = self.pw_t.plot(pen=pg.mkPen(CH_COLORS["A"], width=2))
        self.c_hold_f = self.pw_f.plot(pen=pg.mkPen("#888", width=1.5, style=QtCore.Qt.DashLine))
        self.c_f = self.pw_f.plot(pen=pg.mkPen(CH_COLORS["A"], width=2))
        self.mark_t = pg.ScatterPlotItem(size=10, pen=pg.mkPen("w"), brush=None)
        self.mark_f = pg.ScatterPlotItem(size=10, pen=pg.mkPen("w"), brush=None)
        self.pw_t.addItem(self.mark_t)
        self.pw_f.addItem(self.mark_f)
        plots.addWidget(self.pw_t, 1)
        plots.addWidget(self.pw_f, 1)
        body.addLayout(plots, 1)

        side = QtWidgets.QVBoxLayout()
        self.num_t = BigNumber("max |ΔI/I|  (time domain)", CH_COLORS["A"], "×10⁻³", size=38)
        self.num_f = BigNumber("max |FFT|  (spectrum)", CH_COLORS["A"], "arb. units", size=38)
        self.num_bw = BigNumber("spectral range above noise", "#cccccc", "THz", size=22)
        side.addWidget(self.num_t, 1)
        side.addWidget(self.num_f, 1)
        side.addWidget(self.num_bw)
        side.addStretch(1)
        w = QtWidgets.QWidget()
        w.setLayout(side)
        w.setFixedWidth(240)
        body.addWidget(w)
        outer.addLayout(body, 1)

        for sig in (self.cmb_ch.currentTextChanged, self.cmb_scale.currentTextChanged,
                    self.cmb_win.currentTextChanged, self.sp_pad.valueChanged, self.sp_fmax.valueChanged):
            sig.connect(lambda *_: self.refresh())
        self.btn_hold.clicked.connect(self._hold)
        self.btn_unhold.clicked.connect(self._unhold)
        model.result_changed.connect(self.refresh)
        model.time_axis_changed.connect(self.refresh)

    def _compute(self):
        tr = self.model.trace(self.cmb_ch.currentText())
        if tr is None:
            return None
        t, S, sem, _ = tr
        f, A = P.spectrum(t, S, self.cmb_win.currentText(), self.sp_pad.value())
        # noise floor estimate: spectrum of the s.e.m. band (as if it were a signal)
        _, An = P.spectrum(t, sem * np.sign(np.random.default_rng(0).normal(size=sem.size)),
                           self.cmb_win.currentText(), self.sp_pad.value())
        return t, S, f, A, An

    def _hold(self):
        r = self._compute()
        if r is not None:
            self.hold = r[:4]
            self.refresh()

    def _unhold(self):
        self.hold = None
        self.c_hold_t.setData([], [])
        self.c_hold_f.setData([], [])
        self.refresh()

    def refresh(self, *_):
        r = self._compute()
        if r is None:
            return
        t, S, f, A, An = r
        ch = self.cmb_ch.currentText()
        col = CH_COLORS[ch]
        for c in (self.c_t, self.c_f):
            c.setPen(pg.mkPen(col, width=2))
        for n in (self.num_t, self.num_f):
            n.title.setStyleSheet(f"color:{col}; font-weight:bold; font-size:13px;")
            n.value.setStyleSheet(f"color:{col}; font-size:38px; font-weight:bold;")
        log = self.cmb_scale.currentText() == "log"
        fmax = self.sp_fmax.value()
        sel = f <= fmax
        floor = max(A[sel].max() * 1e-6, 1e-30) if A[sel].size else 1e-30
        self.c_t.setData(t, S)
        self.c_f.setData(f[sel], np.maximum(A[sel], floor) if log else A[sel])
        self.pw_f.setLogMode(x=False, y=log)
        if self.hold is not None:
            ht, hS, hf, hA = self.hold
            hs = hf <= fmax
            self.c_hold_t.setData(ht, hS)
            self.c_hold_f.setData(hf[hs], np.maximum(hA[hs], floor) if log else hA[hs])

        i = int(np.argmax(np.abs(S)))
        self.num_t.set(abs(S[i]) * 1e3, f"at t = {t[i]:.3f} ps")
        self.mark_t.setData([t[i]], [S[i]])
        band = sel & (f > 0.05)
        if band.any():
            j = np.flatnonzero(band)[np.argmax(A[band])]
            self.num_f.set(A[j], f"at f = {f[j]:.3f} THz", fmt="{:.4g}")
            yj = np.log10(max(A[j], floor)) if log else A[j]
            self.mark_f.setData([f[j]], [yj])
            above = band & (A > 3 * np.maximum(An, 1e-30))
            if above.any():
                idx = np.flatnonzero(above)
                self.num_bw.set(f[idx[-1]], f"from {f[idx[0]]:.2f} THz (|FFT| > 3× noise)", fmt="{:.2f}")
            else:
                self.num_bw.set(None, "signal below noise")
        self.pw_t.setTitle(f"{ch}: time domain")
        self.pw_f.setTitle(f"{ch}: Fourier amplitude ({'log' if log else 'linear'})")
