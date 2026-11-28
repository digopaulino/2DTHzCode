"""Right-hand panel, visible on every tab: averaging controls and the ESP301 delay stage."""

from __future__ import annotations

from pathlib import Path

from pyqtgraph.Qt import QtCore, QtWidgets

from .. import processing as P
from ..hardware import list_gpib_resources, list_serial_ports
from .common import BigNumber, Model, dspin, ispin, timestamp_name


class AcquisitionPanel(QtWidgets.QGroupBox):
    def __init__(self, model: Model, worker, parent=None):
        super().__init__("Averaging", parent)
        self.model, self.worker, self.store = model, worker, model.store
        acq = self.store.get("acquisition")
        proc = self.store.get("processing")
        lay = QtWidgets.QVBoxLayout(self)

        # --- N presets ------------------------------------------------------
        row = QtWidgets.QHBoxLayout()
        self.btn_fast = QtWidgets.QPushButton(f"N = {acq.fast_n}\nfast")
        self.btn_long = QtWidgets.QPushButton(f"N = {acq.long_n}\nlong")
        for b in (self.btn_fast, self.btn_long):
            b.setCheckable(True)
            b.setMinimumHeight(44)
            row.addWidget(b)
        lay.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("N cycles"))
        self.spin_n = ispin(acq.n_avg, 1, 100000)
        row.addWidget(self.spin_n)
        lay.addLayout(row)
        self.lbl_frames = QtWidgets.QLabel()
        self.lbl_frames.setStyleSheet("color:#aaa;")
        lay.addWidget(self.lbl_frames)
        self.btn_fast.clicked.connect(lambda: self.spin_n.setValue(self.store.get("acquisition").fast_n))
        self.btn_long.clicked.connect(lambda: self.spin_n.setValue(self.store.get("acquisition").long_n))
        self.spin_n.valueChanged.connect(self._n_changed)

        # --- run controls ----------------------------------------------------
        row = QtWidgets.QHBoxLayout()
        self.btn_run = QtWidgets.QPushButton("▶ Run")
        self.btn_single = QtWidgets.QPushButton("Single")
        self.btn_stop = QtWidgets.QPushButton("■ Stop")
        self.btn_run.setStyleSheet("font-weight:bold;")
        self.btn_stop.setStyleSheet("font-weight:bold; color:#ff6b6b;")
        for b in (self.btn_run, self.btn_single, self.btn_stop):
            row.addWidget(b)
        lay.addLayout(row)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setTextVisible(True)
        lay.addWidget(self.progress)
        self.btn_run.clicked.connect(lambda: self.worker.submit("run", continuous=True))
        self.btn_single.clicked.connect(lambda: self.worker.submit("run", continuous=False))
        self.btn_stop.clicked.connect(self.worker.request_stop)

        # --- phase -----------------------------------------------------------
        form = QtWidgets.QFormLayout()
        self.cmb_phase = QtWidgets.QComboBox()
        self.cmb_phase.addItems(["auto", "manual"])
        self.cmb_phase.setCurrentText(proc.phase_mode)
        self.spin_ref = ispin(proc.manual_ref_phase, 0, 3)
        self.chk_swap = QtWidgets.QCheckBox("swap A-only / AB")
        self.chk_swap.setChecked(proc.manual_swap)
        self.chk_afast = QtWidgets.QCheckBox("A chopped at laser/2 (500 Hz)")
        self.chk_afast.setChecked(proc.a_is_fast)
        form.addRow("Shot order", self.cmb_phase)
        form.addRow("REF position", self.spin_ref)
        form.addRow("", self.chk_swap)
        form.addRow("", self.chk_afast)
        lay.addLayout(form)
        self.cmb_phase.currentTextChanged.connect(self._phase_changed)
        self.spin_ref.valueChanged.connect(self._phase_changed)
        self.chk_swap.toggled.connect(self._phase_changed)
        self.chk_afast.toggled.connect(self._phase_changed)
        self._phase_changed()

        self.lbl_phase = QtWidgets.QLabel("cycle: —")
        self.lbl_phase.setWordWrap(True)
        self.lbl_stats = QtWidgets.QLabel("")
        self.lbl_stats.setStyleSheet("color:#aaa;")
        lay.addWidget(self.lbl_phase)
        lay.addWidget(self.lbl_stats)

        # --- saving ----------------------------------------------------------
        row = QtWidgets.QHBoxLayout()
        self.btn_save = QtWidgets.QPushButton("Save current…")
        self.chk_auto = QtWidgets.QCheckBox("autosave")
        self.chk_auto.setChecked(acq.autosave)
        self.btn_dir = QtWidgets.QPushButton("Folder…")
        row.addWidget(self.btn_save)
        row.addWidget(self.chk_auto)
        row.addWidget(self.btn_dir)
        lay.addLayout(row)
        self.lbl_dir = QtWidgets.QLabel(acq.autosave_dir)
        self.lbl_dir.setStyleSheet("color:#888; font-size:10px;")
        self.lbl_dir.setWordWrap(True)
        lay.addWidget(self.lbl_dir)
        self.btn_save.clicked.connect(self._save)
        self.btn_dir.clicked.connect(self._choose_dir)
        self.chk_auto.toggled.connect(lambda v: self.store.update("acquisition", autosave=v))

        worker.progress.connect(self._progress)
        worker.busy_changed.connect(self._busy)
        model.result_changed.connect(self._result)
        self._blocks = 0
        self._n_changed(self.spin_n.value())
        self._busy(False, "")

    # ------------------------------------------------------------------
    def _n_changed(self, n):
        self.store.update("acquisition", n_avg=int(n))
        acq = self.store.get("acquisition")
        self.btn_fast.setChecked(n == acq.fast_n)
        self.btn_long.setChecked(n == acq.long_n)
        self.lbl_frames.setText(f"camera takes 4N = {4 * n} frames  (≈ {4 * n / 1000:.2f} s at 1 kHz)")

    def _phase_changed(self, *_):
        manual = self.cmb_phase.currentText() == "manual"
        self.spin_ref.setEnabled(manual)
        self.chk_swap.setEnabled(manual)
        self.store.update("processing", phase_mode=self.cmb_phase.currentText(),
                          manual_ref_phase=self.spin_ref.value(), manual_swap=self.chk_swap.isChecked(),
                          a_is_fast=self.chk_afast.isChecked())

    def _progress(self, done, total):
        self.progress.setMaximum(max(1, total))
        self.progress.setValue(done)

    def _busy(self, busy, name):
        self.btn_run.setEnabled(not busy)
        self.btn_single.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)
        if not busy:
            self.progress.setValue(0)

    def _result(self):
        res = self.model.result
        if res is None:
            return
        self._blocks += 1
        pm = res.phase_map
        txt = pm.describe()
        if pm.auto and pm.confidence < 2:
            txt = "⚠ " + txt + " — low contrast (THz off?)"
        self.lbl_phase.setText(txt)
        self.lbl_stats.setText(f"block {self._blocks}: {res.n_cycles} cycles · {res.frame_rate:.0f} fps · "
                               f"dropped {res.dropped}")
        if self.store.get("acquisition").autosave:
            d = Path(self.store.get("acquisition").autosave_dir)
            try:
                self.model.save_result(d / timestamp_name("block"))
            except Exception as e:
                self.lbl_stats.setText(f"autosave failed: {e}")

    def _save(self):
        d = self.store.get("acquisition").autosave_dir
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save current block",
                                                        str(Path(d) / timestamp_name()), "numpy (*.npz)")
        if path:
            try:
                self.model.save_result(Path(path))
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Save", str(e))

    def _choose_dir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Autosave folder",
                                                       self.store.get("acquisition").autosave_dir)
        if d:
            self.store.update("acquisition", autosave_dir=d)
            self.lbl_dir.setText(d)


class StagePanel(QtWidgets.QGroupBox):
    """ESP301: pick motor 1 or 2, go to an absolute position (mm) or step by a delay (fs)."""

    def __init__(self, model: Model, worker, parent=None):
        super().__init__("Delay stage (Newport ESP301)", parent)
        self.model, self.worker, self.store = model, worker, model.store
        ss = self.store.get("stage")
        self.pos = {1: None, 2: None}
        self.zero = {1: None, 2: None}
        lay = QtWidgets.QVBoxLayout(self)

        # --- connection --------------------------------------------------------
        form = QtWidgets.QFormLayout()
        self.cmb_backend = QtWidgets.QComboBox()
        self.cmb_backend.addItems(["esp301", "simulated"])
        self.cmb_backend.setCurrentText(ss.backend)
        self.cmb_iface = QtWidgets.QComboBox()
        self.cmb_iface.addItem("GPIB", "gpib")
        self.cmb_iface.addItem("Serial / USB", "serial")
        self.cmb_iface.setCurrentIndex(0 if ss.interface == "gpib" else 1)
        self.cmb_port = QtWidgets.QComboBox()
        self.cmb_port.setEditable(True)
        self.cmb_port.setToolTip("GPIB: VISA resource, e.g. GPIB0::1::INSTR (board 0, address 1)\n"
                                 "Serial: COM port")
        self.cmb_port.setCurrentText(ss.resource if ss.interface == "gpib" else ss.port)
        self.cmb_baud = QtWidgets.QComboBox()
        self.cmb_baud.setEditable(True)
        self.cmb_baud.addItems(["921600", "19200"])
        self.cmb_baud.setCurrentText(str(ss.baudrate))
        form.addRow("Controller", self.cmb_backend)
        form.addRow("Connection", self.cmb_iface)
        pr = QtWidgets.QHBoxLayout()
        pr.addWidget(self.cmb_port, 1)
        btn_ref = QtWidgets.QPushButton("Refresh")
        btn_ref.setToolTip("list the GPIB instruments (or serial ports) that are found")
        btn_ref.clicked.connect(self._refresh_ports)
        pr.addWidget(btn_ref)
        self.lbl_port = QtWidgets.QLabel("Address")
        form.addRow(self.lbl_port, pr)
        self.lbl_baud = QtWidgets.QLabel("Baud")
        form.addRow(self.lbl_baud, self.cmb_baud)
        self.lbl_found = QtWidgets.QLabel("")
        self.lbl_found.setStyleSheet("color:#aaa;")
        self.lbl_found.setWordWrap(True)
        form.addRow(self.lbl_found)
        self.cmb_iface.currentIndexChanged.connect(self._iface_changed)
        lay.addLayout(form)
        row = QtWidgets.QHBoxLayout()
        self.btn_connect = QtWidgets.QPushButton("Connect")
        self.btn_disconnect = QtWidgets.QPushButton("Disconnect")
        row.addWidget(self.btn_connect)
        row.addWidget(self.btn_disconnect)
        lay.addLayout(row)
        self.lbl_state = QtWidgets.QLabel("not connected")
        self.lbl_state.setStyleSheet("color:#aaa;")
        self.lbl_state.setWordWrap(True)
        lay.addWidget(self.lbl_state)
        self.btn_connect.clicked.connect(self._connect)
        self.btn_disconnect.clicked.connect(lambda: self.worker.submit("disconnect_stage"))

        # --- motor selection + readout --------------------------------------------
        row = QtWidgets.QHBoxLayout()
        self.rb1 = QtWidgets.QRadioButton("Motor 1")
        self.rb2 = QtWidgets.QRadioButton("Motor 2")
        (self.rb1 if ss.axis == 1 else self.rb2).setChecked(True)
        row.addWidget(self.rb1)
        row.addWidget(self.rb2)
        lay.addLayout(row)
        self.rb1.toggled.connect(self._axis_changed)
        self.readout = BigNumber("Position", "#7fd88f", "mm", size=24)
        lay.addWidget(self.readout)
        row = QtWidgets.QHBoxLayout()
        self.btn_zero = QtWidgets.QPushButton("Set Δτ = 0 here")
        self.btn_read = QtWidgets.QPushButton("Read")
        row.addWidget(self.btn_zero)
        row.addWidget(self.btn_read)
        lay.addLayout(row)
        self.btn_zero.clicked.connect(self._set_zero)
        self.btn_read.clicked.connect(lambda: self.worker.submit("stage_read"))

        # --- absolute move -------------------------------------------------------
        row = QtWidgets.QHBoxLayout()
        self.spin_abs = dspin(25.0, -1000, 1000, 5, 0.01, "mm")
        self.btn_abs = QtWidgets.QPushButton("Go to")
        row.addWidget(self.spin_abs, 1)
        row.addWidget(self.btn_abs)
        lay.addLayout(row)
        self.btn_abs.clicked.connect(lambda: self._move("abs", self.spin_abs.value()))

        # --- delay step in fs -------------------------------------------------------
        row = QtWidgets.QHBoxLayout()
        self.spin_fs = dspin(100.0, 0, 1e7, 1, 10, "fs")
        self.btn_minus = QtWidgets.QPushButton("− τ")
        self.btn_plus = QtWidgets.QPushButton("+ τ")
        row.addWidget(self.spin_fs, 1)
        row.addWidget(self.btn_minus)
        row.addWidget(self.btn_plus)
        lay.addLayout(row)
        self.lbl_mm = QtWidgets.QLabel()
        self.lbl_mm.setStyleSheet("color:#aaa;")
        lay.addWidget(self.lbl_mm)
        self.btn_minus.clicked.connect(lambda: self._move("rel_fs", -self.spin_fs.value()))
        self.btn_plus.clicked.connect(lambda: self._move("rel_fs", self.spin_fs.value()))

        form = QtWidgets.QFormLayout()
        self.spin_pass = ispin(ss.passes, 1, 8)
        self.cmb_dir = QtWidgets.QComboBox()
        self.cmb_dir.addItems(["+1", "-1"])
        self.cmb_dir.setCurrentText("+1" if ss.direction > 0 else "-1")
        form.addRow("Optical passes", self.spin_pass)
        form.addRow("Direction (+fs → mm)", self.cmb_dir)
        lay.addLayout(form)
        self.spin_fs.valueChanged.connect(self._update_mm_label)
        self.spin_pass.valueChanged.connect(self._stage_cfg)
        self.cmb_dir.currentTextChanged.connect(self._stage_cfg)

        row = QtWidgets.QHBoxLayout()
        self.btn_on = QtWidgets.QPushButton("Motor ON")
        self.btn_stop = QtWidgets.QPushButton("STOP")
        self.btn_stop.setStyleSheet("background:#8b1e1e; color:white; font-weight:bold;")
        row.addWidget(self.btn_on)
        row.addWidget(self.btn_stop)
        lay.addLayout(row)
        self.btn_on.clicked.connect(lambda: self.worker.submit("stage_motor_on", axis=self.axis()))
        self.btn_stop.clicked.connect(self.worker.stop_stage_now)

        self._iface_changed()
        worker.stage_state.connect(self._state)
        worker.stage_position.connect(self._position)
        self._update_mm_label()
        self._set_enabled(False)

    # ------------------------------------------------------------------
    def axis(self) -> int:
        return 1 if self.rb1.isChecked() else 2

    def _iface_changed(self, *_):
        gpib = self.cmb_iface.currentData() == "gpib"
        self.lbl_baud.setVisible(not gpib)
        self.cmb_baud.setVisible(not gpib)
        self.lbl_port.setText("Address" if gpib else "Port")
        ss = self.store.get("stage")
        self.cmb_port.clear()
        self.cmb_port.setCurrentText(ss.resource if gpib else ss.port)
        self.lbl_found.setText("")

    def _refresh_ports(self):
        """List what is attached: GPIB instruments through VISA, or serial ports."""
        cur = self.cmb_port.currentText()
        if self.cmb_iface.currentData() == "gpib":
            try:
                found = list_gpib_resources()
            except Exception as e:
                self.lbl_found.setText(f"⚠ {e}")
                return
            msg = (f"{len(found)} GPIB instrument(s): " + ", ".join(found)) if found else \
                "no GPIB instruments found (cable, power, NI-488.2 driver?)"
        else:
            found = list_serial_ports()
            msg = ("serial ports: " + ", ".join(found)) if found else "no serial ports found"
        self.lbl_found.setText(msg)
        self.cmb_port.clear()
        self.cmb_port.addItems(found)
        if cur and (cur in found or not found):
            self.cmb_port.setCurrentText(cur)

    def _connect(self):
        try:
            baud = int(self.cmb_baud.currentText())
        except ValueError:
            baud = 921600
        iface = self.cmb_iface.currentData()
        addr = self.cmb_port.currentText().strip()
        kw = dict(backend=self.cmb_backend.currentText(), interface=iface)
        if iface == "gpib":
            kw["resource"] = addr
        else:
            kw.update(port=addr, baudrate=baud, rtscts=(baud == 19200))
        self.store.update("stage", **kw)
        self.lbl_state.setText("connecting …")
        self.worker.submit("connect_stage")

    def _stage_cfg(self, *_):
        self.store.update("stage", passes=self.spin_pass.value(),
                          direction=1 if self.cmb_dir.currentText() == "+1" else -1)
        self._update_mm_label()
        self._refresh_readout()

    def _update_mm_label(self, *_):
        ss = self.store.get("stage")
        mm = P.fs_to_mm(self.spin_fs.value(), ss.passes, ss.direction)
        self.lbl_mm.setText(f"step = {mm * 1e3:.4f} µm of stage travel")

    def _axis_changed(self, *_):
        self.store.update("stage", axis=self.axis())
        self._refresh_readout()

    def _move(self, mode, value):
        self.worker.submit("stage_move", axis=self.axis(), mode=mode, value=value)

    def _set_zero(self):
        ax = self.axis()
        if self.pos[ax] is not None:
            self.zero[ax] = self.pos[ax]
        self._refresh_readout()

    def _state(self, ok, msg):
        self.lbl_state.setText(msg)
        self._set_enabled(ok)

    def _set_enabled(self, ok):
        for w in (self.btn_abs, self.btn_minus, self.btn_plus, self.btn_on, self.btn_read, self.btn_zero,
                  self.btn_disconnect):
            w.setEnabled(ok)

    def _position(self, axis, mm):
        self.pos[axis] = mm
        if self.zero[axis] is None:
            self.zero[axis] = mm
        if axis == self.axis() and abs(self.spin_abs.value() - mm) > 1e-9 and not self.spin_abs.hasFocus():
            self.spin_abs.setValue(mm)
        self._refresh_readout()

    def _refresh_readout(self):
        ax = self.axis()
        p = self.pos[ax]
        if p is None:
            self.readout.set(None, "")
            return
        ss = self.store.get("stage")
        dfs = P.mm_to_fs(p - (self.zero[ax] if self.zero[ax] is not None else p), ss.passes, ss.direction)
        self.readout.title.setText(f"Motor {ax} position")
        self.readout.set(p, f"Δτ = {dfs:+.1f} fs from zero", fmt="{:.5f}")
