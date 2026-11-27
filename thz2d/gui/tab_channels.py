"""Tab 2 - time-domain traces of AB, A, B and NL with large peak readouts and a peak history."""

from __future__ import annotations

from collections import deque

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets

from .. import processing as P
from .common import CH_COLORS, BigNumber, Model, plot_widget

TITLES = {"AB": "AB  (both pumps)", "A": "A only", "B": "B only", "NL": "NL = AB − A − B + REF"}


class ChannelsTab(QtWidgets.QWidget):
    HISTORY = 200

    def __init__(self, model: Model, parent=None):
        super().__init__(parent)
        self.model = model
        grid = QtWidgets.QGridLayout(self)
        grid.setColumnStretch(0, 1)
        top = QtWidgets.QHBoxLayout()
        self.chk_err = QtWidgets.QCheckBox("show ±1 s.e.m. band")
        self.chk_err.setChecked(True)
        self.chk_link = QtWidgets.QCheckBox("same y scale for A / B / AB")
        btn_clear = QtWidgets.QPushButton("Clear history")
        top.addWidget(self.chk_err)
        top.addWidget(self.chk_link)
        top.addStretch(1)
        self.lbl_warn = QtWidgets.QLabel("")
        self.lbl_warn.setStyleSheet("color:#f2c94c;")
        top.addWidget(self.lbl_warn)
        top.addWidget(btn_clear)
        grid.addLayout(top, 0, 0, 1, 3)

        self.plots, self.curves, self.bands, self.numbers, self.hist_curves = {}, {}, {}, {}, {}
        self.history = {c: deque(maxlen=self.HISTORY) for c in P.CHANNELS}
        first = None
        for i, c in enumerate(P.CHANNELS):
            pw = plot_widget("time (ps)" if c == "NL" else "", "ΔI/I", None)
            pw.setTitle(TITLES[c], color=CH_COLORS[c])
            col = pg.mkColor(CH_COLORS[c])
            band_lo = pw.plot(pen=None)
            band_hi = pw.plot(pen=None)
            fill = pg.FillBetweenItem(band_lo, band_hi, brush=pg.mkBrush(col.red(), col.green(), col.blue(), 60))
            pw.addItem(fill, ignoreBounds=True)          # the fill item would freeze auto-range
            self.bands[c] = (band_lo, band_hi, fill)
            self.curves[c] = pw.plot(pen=pg.mkPen(col, width=2))
            pw.addItem(pg.InfiniteLine(angle=0, pen=pg.mkPen("#555")), ignoreBounds=True)
            if first is None:
                first = pw
            else:
                pw.setXLink(first)
            self.plots[c] = pw
            grid.addWidget(pw, i + 1, 0)

            num = BigNumber(f"max |{c}|", CH_COLORS[c], "×10⁻³ ΔI/I", size=34)
            self.numbers[c] = num
            grid.addWidget(num, i + 1, 1)

            hp = pg.PlotWidget()
            hp.setMaximumWidth(190)
            hp.setMinimumWidth(140)
            hp.hideAxis("bottom")
            hp.setTitle("history", size="8pt")
            self.hist_curves[c] = hp.plot(pen=pg.mkPen(col, width=1.5), symbol="o", symbolSize=3,
                                          symbolBrush=col, symbolPen=None)
            grid.addWidget(hp, i + 1, 2)
            grid.setRowStretch(i + 1, 1)

        btn_clear.clicked.connect(self._clear_history)
        self.chk_err.toggled.connect(lambda *_: self.refresh(append=False))
        self.chk_link.toggled.connect(lambda *_: self.refresh(append=False))
        model.result_changed.connect(lambda: self.refresh(append=True))
        model.time_axis_changed.connect(lambda: self.refresh(append=False))

    def _clear_history(self):
        for d in self.history.values():
            d.clear()
        for c in P.CHANNELS:
            self.hist_curves[c].setData([], [])

    def refresh(self, append=False):
        if self.model.result is None:
            return
        warn = ""
        ymax = 0.0
        for c in P.CHANNELS:
            t, S, sem, warn = self.model.trace(c)
            self.curves[c].setData(t, S)
            lo, hi, fill = self.bands[c]
            if self.chk_err.isChecked():
                lo.setData(t, S - sem)
                hi.setData(t, S + sem)
                fill.setVisible(True)
            else:
                fill.setVisible(False)
            if S.size == 0:
                continue
            i = int(np.argmax(np.abs(S)))
            peak = float(np.abs(S[i]))
            noise = float(np.median(sem)) if sem.size else 0.0
            snr = peak / noise if noise > 0 else np.inf
            self.numbers[c].set(peak * 1e3, f"at t = {t[i]:.3f} ps · sign {'+' if S[i] >= 0 else '−'}\n"
                                             f"SNR ≈ {snr:.0f}  (peak / median s.e.m.)")
            if append:
                self.history[c].append(peak * 1e3)
                h = np.array(self.history[c])
                self.hist_curves[c].setData(np.arange(h.size), h)
            if c != "NL":
                ymax = max(ymax, float(np.max(np.abs(S))))
        if self.chk_link.isChecked() and ymax > 0:
            for c in ("AB", "A", "B"):
                self.plots[c].setYRange(-1.05 * ymax, 1.05 * ymax, padding=0)
        else:
            for c in ("AB", "A", "B"):
                self.plots[c].enableAutoRange(axis="y")
        self.lbl_warn.setText(("⚠ " + warn) if warn else "")
