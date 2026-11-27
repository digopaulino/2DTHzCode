"""Tab 1 - camera set-up, the two polarization windows and the pixel -> time calibration."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pyqtgraph as pg
from pyqtgraph.Qt import QtCore, QtWidgets

from .. import processing as P
from .common import CH_COLORS, MINUS_COLOR, PLUS_COLOR, Model, dspin, ispin, plot_widget

TRIGGERS = ["external exposure start & software trigger", "auto sequence", "software trigger",
            "external exposure control", "external synchronized", "fast external exposure control",
            "slow external exposure control", "external CDS control"]
INTERFACES = ["", "USB 3.0", "CLHS", "Camera Link ME4", "Camera Link MTX", "Camera Link NAT", "GigE"]
SENSOR_ROWS = 2048       # pco.edge 4.2


class SetupTab(QtWidgets.QWidget):
    def __init__(self, model: Model, worker, parent=None):
        super().__init__(parent)
        self.model, self.worker, self.store = model, worker, model.store
        self._sync = False
        outer = QtWidgets.QHBoxLayout(self)

        # ================================================================ left column
        left = QtWidgets.QWidget()
        llay = QtWidgets.QVBoxLayout(left)
        llay.addWidget(self._camera_group())
        llay.addWidget(self._windows_group())
        llay.addWidget(self._dark_group())
        llay.addWidget(self._time_group())
        llay.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidget(left)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(420)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        # ================================================================ plots
        right = QtWidgets.QVBoxLayout()
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Show shot type:"))
        self.bin_group = QtWidgets.QButtonGroup(self)
        for i, lab in enumerate(P.PHASES):
            rb = QtWidgets.QRadioButton(lab)
            rb.setStyleSheet(f"color:{CH_COLORS[lab]}; font-weight:bold;")
            self.bin_group.addButton(rb, i)
            row.addWidget(rb)
        self.bin_group.button(1).setChecked(True)
        self.bin_group.idToggled.connect(lambda *_: self.refresh())
        row.addStretch(1)
        self.lbl_hint = QtWidgets.QLabel("drag the shaded regions to move a window; drag an edge to change the length")
        self.lbl_hint.setStyleSheet("color:#888;")
        row.addWidget(self.lbl_hint)
        right.addLayout(row)

        prow = QtWidgets.QHBoxLayout()
        self.pw_minus = plot_widget("pixel", "counts (row sum)", "Window I(−)")
        self.pw_plus = plot_widget("pixel", "counts (row sum)", "Window I(+)")
        for pw in (self.pw_minus, self.pw_plus):
            pw.addLegend(offset=(-10, 10))
        self.c_minus = self.pw_minus.plot(pen=pg.mkPen(MINUS_COLOR, width=2), name="I(−) shot")
        self.c_minus_ref = self.pw_minus.plot(pen=pg.mkPen("w", width=1.5, style=QtCore.Qt.DashLine),
                                              name="I(−) REF")
        self.c_plus = self.pw_plus.plot(pen=pg.mkPen(PLUS_COLOR, width=2), name="I(+) shot")
        self.c_plus_ref = self.pw_plus.plot(pen=pg.mkPen("w", width=1.5, style=QtCore.Qt.DashLine),
                                            name="I(+) REF")
        self.reg_minus = pg.LinearRegionItem(brush=pg.mkBrush(140, 140, 255, 40),
                                             pen=pg.mkPen(MINUS_COLOR, width=2))
        self.reg_plus = pg.LinearRegionItem(brush=pg.mkBrush(255, 255, 60, 30), pen=pg.mkPen(PLUS_COLOR, width=2))
        self.pw_minus.addItem(self.reg_minus)
        self.pw_plus.addItem(self.reg_plus)
        self.reg_minus.sigRegionChangeFinished.connect(lambda: self._region_moved("minus"))
        self.reg_plus.sigRegionChangeFinished.connect(lambda: self._region_moved("plus"))
        prow.addWidget(self.pw_minus)
        prow.addWidget(self.pw_plus)
        right.addLayout(prow, 3)

        brow = QtWidgets.QHBoxLayout()
        self.pw_win = plot_widget("pixel inside window", "ΔI/I", "Inside the windows: shot vs REF")
        self.pw_win.addLegend(offset=(-10, 10))
        self.c_sp = self.pw_win.plot(pen=pg.mkPen(PLUS_COLOR, width=1.5), name="ΔI+/I+")
        self.c_sm = self.pw_win.plot(pen=pg.mkPen(MINUS_COLOR, width=1.5), name="−ΔI−/I−")
        self.c_bal = self.pw_win.plot(pen=pg.mkPen("w", width=2), name="balanced")
        self.t0_line = pg.InfiniteLine(angle=90, pen=pg.mkPen("#7fd88f", style=QtCore.Qt.DotLine))
        self.pw_win.addItem(self.t0_line)
        self.pw_cal = plot_widget("", "", "Time calibration")
        self.pw_cal.addLegend(offset=(-10, 10))
        brow.addWidget(self.pw_win, 3)
        brow.addWidget(self.pw_cal, 2)
        right.addLayout(brow, 2)
        outer.addLayout(right, 1)

        model.result_changed.connect(self.refresh)
        model.windows_changed.connect(self._windows_from_store)
        model.windows_changed.connect(self.refresh)
        model.time_axis_changed.connect(self._time_from_store)
        worker.camera_state.connect(self._camera_state)
        worker.calib_done.connect(self._calib_done)
        worker.busy_changed.connect(self._busy)
        worker.dark_ready.connect(self._dark_ready)
        self._windows_from_store()
        self._time_from_store()
        self._show_calibration()

    # ================================================================ groups
    def _camera_group(self):
        cs = self.store.get("camera")
        g = QtWidgets.QGroupBox("Camera (pco.edge 4.2)")
        f = QtWidgets.QFormLayout(g)
        self.cmb_cam = QtWidgets.QComboBox()
        self.cmb_cam.addItems(["pco", "simulated"])
        self.cmb_cam.setCurrentText(cs.backend)
        self.cmb_if = QtWidgets.QComboBox()
        self.cmb_if.addItems(INTERFACES)
        self.cmb_if.setCurrentText(cs.interface)
        self.cmb_if.setToolTip("empty = scan all interfaces (slower)")
        f.addRow("Backend", self.cmb_cam)
        f.addRow("Interface", self.cmb_if)
        row = QtWidgets.QHBoxLayout()
        self.btn_cam_on = QtWidgets.QPushButton("Connect")
        self.btn_cam_off = QtWidgets.QPushButton("Disconnect")
        row.addWidget(self.btn_cam_on)
        row.addWidget(self.btn_cam_off)
        f.addRow(row)
        self.lbl_cam = QtWidgets.QLabel("not connected")
        self.lbl_cam.setWordWrap(True)
        self.lbl_cam.setStyleSheet("color:#aaa;")
        f.addRow(self.lbl_cam)

        self.sp_exp = dspin(cs.exposure_us, 0.01, 1e6, 2, 10, "µs")
        self.sp_del = dspin(cs.delay_us, 0, 1e6, 2, 1, "µs")
        f.addRow("Exposure", self.sp_exp)
        f.addRow("Trigger delay", self.sp_del)
        self.sp_roi = [ispin(v, 1, 2048) for v in cs.roi]
        r = QtWidgets.QHBoxLayout()
        for lab, s in zip(("x0", "y0", "x1", "y1"), self.sp_roi):
            s.setToolTip(f"ROI {lab} (1-based, inclusive)")
            r.addWidget(s)
        f.addRow("ROI x0 y0 x1 y1", r)
        r = QtWidgets.QHBoxLayout()
        self.sp_rows = ispin(cs.roi[3] - cs.roi[1] + 1, 8, 2048, 2)
        btn_c = QtWidgets.QPushButton("Centre ROI vertically")
        btn_c.clicked.connect(self._centre_roi)
        r.addWidget(self.sp_rows)
        r.addWidget(btn_c)
        f.addRow("Rows", r)
        self.cmb_trig = QtWidgets.QComboBox()
        self.cmb_trig.addItems(TRIGGERS)
        self.cmb_trig.setCurrentText(cs.trigger)
        f.addRow("Trigger", self.cmb_trig)
        self.cmb_rec = QtWidgets.QComboBox()
        self.cmb_rec.addItems(["fifo", "sequence"])
        self.cmb_rec.setCurrentText(cs.record_mode)
        self.cmb_rec.setToolTip("fifo: one continuous stream (no re-arming between blocks)\n"
                                "sequence: re-arm the recorder for each block of 4N frames")
        self.sp_fifo = ispin(cs.fifo_frames, 8, 100000, 50)
        f.addRow("Record mode", self.cmb_rec)
        f.addRow("FIFO buffer", self.sp_fifo)
        self.btn_apply = QtWidgets.QPushButton("Apply camera settings")
        f.addRow(self.btn_apply)
        self.btn_cam_on.clicked.connect(self._connect_camera)
        self.btn_cam_off.clicked.connect(lambda: self.worker.submit("disconnect_camera"))
        self.btn_apply.clicked.connect(self._apply_camera)
        return g

    def _windows_group(self):
        g = QtWidgets.QGroupBox("Polarization windows (pixels in the camera image)")
        f = QtWidgets.QFormLayout(g)
        self.sp_x0m, self.sp_y0m = ispin(0, 0, 4096), ispin(0, 0, 4096)
        self.sp_x0p, self.sp_y0p = ispin(0, 0, 4096), ispin(0, 0, 4096)
        self.sp_len, self.sp_h = ispin(2, 2, 4096), ispin(1, 1, 4096)
        self.chk_flip = QtWidgets.QCheckBox("mirror the I(−) window")
        for lab, a, b in (("I(−) start x / y", self.sp_x0m, self.sp_y0m),
                          ("I(+) start x / y", self.sp_x0p, self.sp_y0p),
                          ("Length / height", self.sp_len, self.sp_h)):
            r = QtWidgets.QHBoxLayout()
            r.addWidget(a)
            r.addWidget(b)
            f.addRow(lab, r)
        f.addRow(self.chk_flip)
        self.sp_mask = dspin(100 * self.store.get("processing").min_ref_fraction, 0, 90, 1, 1, "%")
        self.sp_mask.setToolTip("pixels where the REF intensity is below this fraction of its maximum\n"
                                "are excluded (dim beam edges only add noise)")
        self.sp_mask.valueChanged.connect(
            lambda v: (self.store.update("processing", min_ref_fraction=v / 100), self.refresh()))
        f.addRow("Mask pixels with REF <", self.sp_mask)
        r = QtWidgets.QHBoxLayout()
        b1 = QtWidgets.QPushButton("Auto-find stripes")
        b2 = QtWidgets.QPushButton("Swap ±")
        b1.clicked.connect(self._auto_windows)
        b2.clicked.connect(self._swap_windows)
        r.addWidget(b1)
        r.addWidget(b2)
        f.addRow(r)
        for s in (self.sp_x0m, self.sp_y0m, self.sp_x0p, self.sp_y0p, self.sp_len, self.sp_h):
            s.valueChanged.connect(self._windows_to_store)
        self.chk_flip.toggled.connect(self._windows_to_store)
        return g

    def _dark_group(self):
        g = QtWidgets.QGroupBox("Dark / background image")
        f = QtWidgets.QFormLayout(g)
        self.sp_dark = ispin(200, 4, 100000, 100)
        self.btn_dark = QtWidgets.QPushButton("Record dark (block the probe!)")
        self.chk_dark = QtWidgets.QCheckBox("subtract dark")
        self.chk_dark.setChecked(self.store.get("processing").subtract_dark)
        btn_clear = QtWidgets.QPushButton("Clear")
        f.addRow("Frames", self.sp_dark)
        f.addRow(self.btn_dark)
        r = QtWidgets.QHBoxLayout()
        r.addWidget(self.chk_dark)
        r.addWidget(btn_clear)
        f.addRow(r)
        self.lbl_dark = QtWidgets.QLabel("no dark image")
        self.lbl_dark.setStyleSheet("color:#aaa;")
        f.addRow(self.lbl_dark)
        self.btn_dark.clicked.connect(lambda: self.worker.submit("dark", n_frames=self.sp_dark.value()))
        btn_clear.clicked.connect(lambda: self.worker.submit("clear_dark"))
        self.chk_dark.toggled.connect(lambda v: self.store.update("processing", subtract_dark=v))
        return g

    def _time_group(self):
        ts = self.store.get("time_axis")
        g = QtWidgets.QGroupBox("Pixel → time")
        f = QtWidgets.QFormLayout(g)
        self.cmb_tmode = QtWidgets.QComboBox()
        self.cmb_tmode.addItem("linear (fs per pixel)", "linear")
        self.cmb_tmode.addItem("per-pixel calibration (Gao et al.)", "pixel")
        f.addRow("Mode", self.cmb_tmode)
        self.sp_fspx = dspin(ts.fs_per_px, -1000, 1000, 4, 0.1, "fs/px")
        self.sp_t0 = dspin(ts.t0_px, -1e5, 1e5, 1, 1, "px")
        btn_t0 = QtWidgets.QPushButton("t = 0 at peak of A")
        btn_t0.clicked.connect(self._t0_at_peak)
        f.addRow("Factor", self.sp_fspx)
        r = QtWidgets.QHBoxLayout()
        r.addWidget(self.sp_t0)
        r.addWidget(btn_t0)
        f.addRow("t = 0 pixel", r)
        self.cmb_tmode.currentIndexChanged.connect(self._time_to_store)
        self.sp_fspx.valueChanged.connect(self._time_to_store)
        self.sp_t0.valueChanged.connect(self._time_to_store)

        # common scan parameters
        sep = QtWidgets.QLabel("<b>Calibrate with the delay stage</b>")
        f.addRow(sep)
        self.cmb_cal_axis = QtWidgets.QComboBox()
        self.cmb_cal_axis.addItems(["1", "2"])
        self.cmb_cal_axis.setCurrentText(str(self.store.get("stage").axis))
        self.cmb_cal_ch = QtWidgets.QComboBox()
        self.cmb_cal_ch.addItems(["A", "B", "AB"])
        self.sp_cal_n = ispin(25, 1, 10000)
        r = QtWidgets.QHBoxLayout()
        r.addWidget(QtWidgets.QLabel("motor"))
        r.addWidget(self.cmb_cal_axis)
        r.addWidget(QtWidgets.QLabel("channel"))
        r.addWidget(self.cmb_cal_ch)
        r.addWidget(QtWidgets.QLabel("N"))
        r.addWidget(self.sp_cal_n)
        f.addRow(r)

        self.sp_lin_step = dspin(500, -1e5, 1e5, 1, 50, "fs")
        self.sp_lin_n = ispin(6, 1, 100)
        r = QtWidgets.QHBoxLayout()
        r.addWidget(self.sp_lin_step)
        r.addWidget(QtWidgets.QLabel("×"))
        r.addWidget(self.sp_lin_n)
        f.addRow("Linear: step", r)
        self.btn_lin = QtWidgets.QPushButton("Measure fs/px (shift vs delay)")
        f.addRow(self.btn_lin)

        self.sp_px_start = dspin(-12000, -1e6, 1e6, 0, 500, "fs")
        self.sp_px_stop = dspin(12000, -1e6, 1e6, 0, 500, "fs")
        self.sp_px_step = dspin(40, 1, 1e5, 1, 5, "fs")
        r = QtWidgets.QHBoxLayout()
        r.addWidget(self.sp_px_start)
        r.addWidget(self.sp_px_stop)
        f.addRow("Per-pixel: range", r)
        f.addRow("Per-pixel: step", self.sp_px_step)
        self.lbl_px_time = QtWidgets.QLabel()
        self.lbl_px_time.setStyleSheet("color:#aaa;")
        f.addRow(self.lbl_px_time)
        self.btn_px = QtWidgets.QPushButton("Run per-pixel calibration scan")
        f.addRow(self.btn_px)
        r = QtWidgets.QHBoxLayout()
        btn_load = QtWidgets.QPushButton("Load calibration…")
        btn_load.clicked.connect(self._load_calib)
        r.addWidget(btn_load)
        f.addRow(r)
        self.lbl_calfile = QtWidgets.QLabel(Path(ts.calib_file).name if ts.calib_file else "no per-pixel file")
        self.lbl_calfile.setStyleSheet("color:#aaa;")
        self.lbl_calfile.setWordWrap(True)
        f.addRow(self.lbl_calfile)
        for s in (self.sp_px_start, self.sp_px_stop, self.sp_px_step, self.sp_cal_n):
            s.valueChanged.connect(self._px_time_estimate)
        self._px_time_estimate()
        self.btn_lin.clicked.connect(self._run_linear)
        self.btn_px.clicked.connect(self._run_pixel)
        return g

    # ================================================================ camera
    def _camera_to_store(self):
        self.store.update("camera", backend=self.cmb_cam.currentText(), interface=self.cmb_if.currentText(),
                          exposure_us=self.sp_exp.value(), delay_us=self.sp_del.value(),
                          roi=tuple(s.value() for s in self.sp_roi), trigger=self.cmb_trig.currentText(),
                          record_mode=self.cmb_rec.currentText(), fifo_frames=self.sp_fifo.value())

    def _connect_camera(self):
        self._camera_to_store()
        self.lbl_cam.setText("connecting …")
        self.worker.submit("connect_camera")

    def _apply_camera(self):
        self._camera_to_store()
        self.worker.submit("configure_camera")

    def _centre_roi(self):
        h = self.sp_rows.value() // 2 * 2
        y0 = SENSOR_ROWS // 2 - h // 2 + 1
        self.sp_roi[1].setValue(y0)
        self.sp_roi[3].setValue(y0 + h - 1)

    def _camera_state(self, ok, info):
        if ok and isinstance(info, dict):
            self.lbl_cam.setText("\n".join(f"{k}: {v}" for k, v in info.items()))
        else:
            self.lbl_cam.setText(str(info))

    def _dark_ready(self, dark):
        self.lbl_dark.setText("no dark image" if dark is None else
                              f"dark: {dark.shape[1]}×{dark.shape[0]}, mean {dark.mean():.1f} counts")

    # ================================================================ windows
    def _windows_from_store(self):
        ws = self.store.get("windows")
        self._sync = True
        try:
            for s, v in ((self.sp_x0m, ws.x0_minus), (self.sp_y0m, ws.y0_minus), (self.sp_x0p, ws.x0_plus),
                         (self.sp_y0p, ws.y0_plus), (self.sp_len, ws.length), (self.sp_h, ws.height)):
                s.setValue(v)
            self.chk_flip.setChecked(ws.flip_minus)
            self.reg_minus.setRegion((ws.x0_minus, ws.x0_minus + ws.length))
            self.reg_plus.setRegion((ws.x0_plus, ws.x0_plus + ws.length))
        finally:
            self._sync = False

    def _windows_to_store(self, *_):
        if self._sync:
            return
        self.store.update("windows", x0_minus=self.sp_x0m.value(), y0_minus=self.sp_y0m.value(),
                          x0_plus=self.sp_x0p.value(), y0_plus=self.sp_y0p.value(), length=self.sp_len.value(),
                          height=self.sp_h.value(), flip_minus=self.chk_flip.isChecked())
        self.model.windows_changed.emit()

    def _region_moved(self, which):
        if self._sync:
            return
        reg = self.reg_minus if which == "minus" else self.reg_plus
        lo, hi = reg.getRegion()
        x0 = max(0, int(round(lo)))
        L = max(2, int(round(hi - lo)))
        self.store.update("windows", **{f"x0_{which}": x0, "length": L})
        self.model.windows_changed.emit()

    def _auto_windows(self):
        res = self.model.result
        if res is None or not res.mean_images:
            QtWidgets.QMessageBox.information(self, "Auto-find", "Acquire one block first (Single).")
            return
        try:
            st = P.find_stripes(res.mean_images["REF"])
        except ValueError as e:
            QtWidgets.QMessageBox.warning(self, "Auto-find", str(e))
            return
        ws = self.store.get("windows")
        # keep the current ± assignment if the user already swapped: "+" = bottom stripe by default
        plus_bottom = ws.y0_plus >= ws.y0_minus
        top = dict(x0=st["x0_top"], y0=st["y0_top"])
        bot = dict(x0=st["x0_bottom"], y0=st["y0_bottom"])
        p, m = (bot, top) if plus_bottom else (top, bot)
        self.store.update("windows", x0_plus=p["x0"], y0_plus=p["y0"], x0_minus=m["x0"], y0_minus=m["y0"],
                          length=st["length"], height=st["height"])
        self.model.windows_changed.emit()

    def _swap_windows(self):
        ws = self.store.get("windows")
        self.store.update("windows", x0_plus=ws.x0_minus, y0_plus=ws.y0_minus,
                          x0_minus=ws.x0_plus, y0_minus=ws.y0_plus)
        self.model.windows_changed.emit()

    # ================================================================ time axis
    def _time_from_store(self):
        ts = self.store.get("time_axis")
        self._sync = True
        try:
            self.cmb_tmode.setCurrentIndex(0 if ts.mode == "linear" else 1)
            self.sp_fspx.setValue(ts.fs_per_px)
            self.sp_t0.setValue(ts.t0_px)
            self.lbl_calfile.setText(Path(ts.calib_file).name if ts.calib_file else "no per-pixel file")
        finally:
            self._sync = False
        self._show_calibration()
        self.refresh()

    def _time_to_store(self, *_):
        if self._sync:
            return
        self.store.update("time_axis", mode=self.cmb_tmode.currentData(), fs_per_px=self.sp_fspx.value(),
                          t0_px=self.sp_t0.value())
        self.model.time_axis_changed.emit()

    def _t0_at_peak(self):
        res = self.model.result
        if res is None:
            return
        S = np.where(res.valid, res.S["A"], 0.0)
        self.sp_t0.setValue(float(np.argmax(np.abs(S))))

    def _px_time_estimate(self, *_):
        n_pts = int(abs(self.sp_px_stop.value() - self.sp_px_start.value()) / max(1e-9, self.sp_px_step.value())) + 1
        sec = n_pts * (4 * self.sp_cal_n.value() / 1000 + 0.3)
        self.lbl_px_time.setText(f"{n_pts} points, ≈ {sec / 60:.1f} min at 1 kHz")

    def _run_linear(self):
        self.worker.submit("calib_linear", axis=int(self.cmb_cal_axis.currentText()),
                           step_fs=self.sp_lin_step.value(), n_steps=self.sp_lin_n.value(),
                           channel=self.cmb_cal_ch.currentText(), n_avg=self.sp_cal_n.value())

    def _run_pixel(self):
        out = Path(self.store.get("acquisition").autosave_dir) / "calibration"
        self.worker.submit("calib_pixel", axis=int(self.cmb_cal_axis.currentText()),
                           start_fs=self.sp_px_start.value(), stop_fs=self.sp_px_stop.value(),
                           step_fs=self.sp_px_step.value(), channel=self.cmb_cal_ch.currentText(),
                           n_avg=self.sp_cal_n.value(), out_dir=str(out))

    def _load_calib(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Per-pixel calibration", "", "numpy (*.npz)")
        if not path:
            return
        try:
            self.model.load_calibration(path)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "Calibration", f"could not load {path}: {e}")

    def _calib_done(self, d):
        if d["kind"] == "linear":
            self._last_linear = d
            self.store.update("time_axis", mode="linear", fs_per_px=d["fs_per_px"])
            self.model.time_axis_changed.emit()
        else:
            self.model.set_calibration(d["calib"], d["path"])
        self._show_calibration(d)

    def _show_calibration(self, d=None):
        pw = self.pw_cal
        pw.clear()
        if d is None:
            d = getattr(self, "_last_linear", None)
            if self.store.get("time_axis").mode == "pixel" and self.model.calib is not None:
                d = {"kind": "pixel", "calib": self.model.calib}
        if d is None:
            pw.setTitle("Time calibration (none measured yet)")
            return
        if d["kind"] == "linear":
            pw.setLabel("bottom", "stage delay (fs)")
            pw.setLabel("left", "trace shift (px)")
            pw.plot(d["delays_fs"], d["shifts_px"], pen=None, symbol="o", symbolBrush="#7fd88f", name="measured")
            k = d["fs_per_px"]
            xx = np.array([d["delays_fs"].min(), d["delays_fs"].max()])
            pw.plot(xx, -xx / k + d["shifts_px"][0], pen=pg.mkPen("w", style=QtCore.Qt.DashLine),
                    name=f"{k:.4f} fs/px")
            pw.setTitle(f"Linear calibration: {k:.4f} fs/px")
        else:
            c = d["calib"]
            x = np.arange(c["tp"].size)
            pw.setLabel("bottom", "pixel")
            pw.setLabel("left", "t*(x) (ps)")
            if "tp_raw" in c:
                pw.plot(x, c["tp_raw"] * 1e-3, pen=None, symbol="o", symbolSize=2, symbolPen=None,
                        symbolBrush=PLUS_COLOR)
                pw.plot(x, c["tm_raw"] * 1e-3, pen=None, symbol="o", symbolSize=2, symbolPen=None,
                        symbolBrush=MINUS_COLOR)
            pw.plot(x, c["tp"] * 1e-3, pen=pg.mkPen(PLUS_COLOR, width=2), name="t*+(x)")
            pw.plot(x, c["tm"] * 1e-3, pen=pg.mkPen(MINUS_COLOR, width=2), name="t*−(x)")
            slope = np.median(np.diff(c["tp"]))
            pw.setTitle(f"Per-pixel calibration (≈ {slope:.3f} fs/px at centre)")

    # ================================================================ plots
    def refresh(self, *_):
        res = self.model.result
        if res is None or not res.mean_images:
            return
        lab = P.PHASES[max(0, self.bin_group.checkedId())]
        wm, wp = P.windows_from_settings(self.store.get("windows"))
        img, ref = res.mean_images[lab], res.mean_images["REF"]
        x = np.arange(img.shape[1])
        self.c_minus.setData(x, P.profile_full_width(img, wm))
        self.c_minus_ref.setData(x, P.profile_full_width(ref, wm))
        self.c_plus.setData(x, P.profile_full_width(img, wp))
        self.c_plus_ref.setData(x, P.profile_full_width(ref, wp))
        try:
            ip, ipr = P.collapse(img, wp), P.collapse(ref, wp)
            im, imr = P.collapse(img, wm), P.collapse(ref, wm)
        except ValueError as e:
            self.pw_win.setTitle(f"⚠ {e}")
            return
        frac = self.store.get("processing").min_ref_fraction
        ok = (ipr > frac * ipr.max()) & (imr > frac * imr.max())
        with np.errstate(divide="ignore", invalid="ignore"):
            sp = np.where(ok, (ip - ipr) / ipr, np.nan)
            sm = np.where(ok, (im - imr) / imr, np.nan)
        xi = np.arange(sp.size)
        self.c_sp.setData(xi, sp, connect="finite")
        self.c_sm.setData(xi, -sm, connect="finite")
        self.c_bal.setData(xi, sp - sm, connect="finite")
        self.t0_line.setValue(self.store.get("time_axis").t0_px)
        self.pw_win.setTitle(f"Inside the windows: {lab} vs REF  (aligned windows → yellow and purple overlap)")

    def _busy(self, busy, name):
        for b in (self.btn_lin, self.btn_px, self.btn_dark, self.btn_cam_on):
            b.setEnabled(not busy)
