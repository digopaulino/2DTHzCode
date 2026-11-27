"""Camera image strip shown at the top of the window, with the two polarization windows drawn on it.

The rectangles can be dragged/resized; they stay in sync with the Setup tab.
"""

from __future__ import annotations

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets

from .common import MINUS_COLOR, PLUS_COLOR, Model


class LiveView(QtWidgets.QWidget):
    def __init__(self, model: Model, parent=None):
        super().__init__(parent)
        self.model, self.store = model, model.store
        lay = QtWidgets.QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.imv = pg.ImageView()
        self.imv.ui.roiBtn.hide()
        self.imv.ui.menuBtn.hide()
        self.imv.getView().invertY(True)
        self.imv.getView().setAspectLocked(False)
        lay.addWidget(self.imv, 1)
        side = QtWidgets.QVBoxLayout()
        self.chk_auto = QtWidgets.QCheckBox("auto levels")
        self.chk_auto.setChecked(True)
        self.lbl = QtWidgets.QLabel("")
        self.lbl.setStyleSheet("color:#aaa;")
        self.lbl.setWordWrap(True)
        side.addWidget(self.chk_auto)
        side.addWidget(self.lbl)
        side.addStretch(1)
        w = QtWidgets.QWidget()
        w.setLayout(side)
        w.setFixedWidth(150)
        lay.addWidget(w)

        self._sync = False
        self.roi_minus = self._make_roi(MINUS_COLOR)
        self.roi_plus = self._make_roi(PLUS_COLOR)
        self.txt_minus = pg.TextItem("I(−)", color=MINUS_COLOR, anchor=(0, 1))
        self.txt_plus = pg.TextItem("I(+)", color=PLUS_COLOR, anchor=(0, 1))
        for it in (self.txt_minus, self.txt_plus):
            self.imv.getView().addItem(it)
        self.roi_minus.sigRegionChangeFinished.connect(lambda: self._roi_moved("minus"))
        self.roi_plus.sigRegionChangeFinished.connect(lambda: self._roi_moved("plus"))
        model.windows_changed.connect(self.update_rois)
        self._first = True
        self.update_rois()

    def _make_roi(self, color):
        roi = pg.ROI([0, 0], [10, 10], pen=pg.mkPen(color, width=2), snapSize=1.0,
                     translateSnap=True, scaleSnap=True)
        roi.addScaleHandle([1, 1], [0, 0])
        roi.addScaleHandle([0, 0], [1, 1])
        self.imv.getView().addItem(roi)
        return roi

    def set_frame(self, frame: np.ndarray):
        auto = self.chk_auto.isChecked() or self._first
        self.imv.setImage(frame, autoLevels=auto, autoRange=self._first, autoHistogramRange=auto)
        self._first = False
        self.lbl.setText(f"{frame.shape[1]} × {frame.shape[0]} px\nmax {int(frame.max())} counts"
                         + ("\n⚠ near saturation" if frame.max() > 60000 else ""))

    def update_rois(self):
        ws = self.store.get("windows")
        self._sync = True
        try:
            self.roi_minus.setPos([ws.x0_minus, ws.y0_minus], finish=False)
            self.roi_minus.setSize([ws.length, ws.height], finish=False)
            self.roi_plus.setPos([ws.x0_plus, ws.y0_plus], finish=False)
            self.roi_plus.setSize([ws.length, ws.height], finish=False)
            self.txt_minus.setPos(ws.x0_minus, ws.y0_minus)
            self.txt_plus.setPos(ws.x0_plus, ws.y0_plus)
        finally:
            self._sync = False

    def _roi_moved(self, which):
        if self._sync:
            return
        roi = self.roi_minus if which == "minus" else self.roi_plus
        x, y = (int(round(v)) for v in roi.pos())
        L, H = (max(2, int(round(v))) for v in roi.size())
        kw = {f"x0_{which}": max(0, x), f"y0_{which}": max(0, y), "length": L, "height": max(1, H)}
        self.store.update("windows", **kw)
        self.model.windows_changed.emit()
