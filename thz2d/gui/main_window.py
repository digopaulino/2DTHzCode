"""Main window: live view on top, three tabs in the middle, averaging + stage panel on the right."""

from __future__ import annotations

import time

from pyqtgraph.Qt import QtCore, QtGui, QtWidgets

from ..acquisition import AcquisitionThread
from ..config import Settings, SettingsStore
from .common import Model
from .live_view import LiveView
from .side_panel import AcquisitionPanel, StagePanel
from .tab_analysis import AnalysisTab
from .tab_channels import ChannelsTab
from .tab_setup import SetupTab


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, settings: Settings, settings_path=None):
        super().__init__()
        self.setWindowTitle("2D THz — echelon single-shot acquisition")
        self.settings_path = settings_path
        self.store = SettingsStore(settings)
        self.model = Model(self.store)
        self.worker = AcquisitionThread(self.store)
        self._busy = False

        central = QtWidgets.QWidget()
        vlay = QtWidgets.QVBoxLayout(central)
        vlay.setContentsMargins(6, 6, 6, 6)

        # ---- top bar -------------------------------------------------------
        bar = QtWidgets.QHBoxLayout()
        self.chk_live = QtWidgets.QCheckBox("Live feed")
        self.chk_live.setStyleSheet("font-weight:bold;")
        self.chk_live.setChecked(True)
        bar.addWidget(self.chk_live)
        bar.addWidget(QtWidgets.QLabel("refresh"))
        self.cmb_rate = QtWidgets.QComboBox()
        self.cmb_rate.addItems(["0.25 s", "0.5 s", "1 s", "2 s"])
        self.cmb_rate.setCurrentIndex(1)
        bar.addWidget(self.cmb_rate)
        bar.addSpacing(20)
        self.lbl_cam = QtWidgets.QLabel("camera: not connected")
        self.lbl_stage = QtWidgets.QLabel("stage: not connected")
        for l in (self.lbl_cam, self.lbl_stage):
            l.setStyleSheet("color:#aaa;")
            bar.addWidget(l)
            bar.addSpacing(20)
        bar.addStretch(1)
        self.lbl_busy = QtWidgets.QLabel("idle")
        self.lbl_busy.setStyleSheet("font-weight:bold; color:#7fd88f;")
        bar.addWidget(self.lbl_busy)
        vlay.addLayout(bar)

        self.live = LiveView(self.model)
        self.live.setMinimumHeight(140)

        # ---- tabs + side panel ---------------------------------------------
        self.tabs = QtWidgets.QTabWidget()
        self.tab_setup = SetupTab(self.model, self.worker)
        self.tab_channels = ChannelsTab(self.model)
        self.tab_analysis = AnalysisTab(self.model)
        self.tabs.addTab(self.tab_setup, "1 · Setup && calibration")
        self.tabs.addTab(self.tab_channels, "2 · Channels")
        self.tabs.addTab(self.tab_analysis, "3 · Analysis")

        side = QtWidgets.QWidget()
        slay = QtWidgets.QVBoxLayout(side)
        slay.setContentsMargins(0, 0, 0, 0)
        self.acq_panel = AcquisitionPanel(self.model, self.worker)
        self.stage_panel = StagePanel(self.model, self.worker)
        slay.addWidget(self.acq_panel)
        slay.addWidget(self.stage_panel)
        self.log = QtWidgets.QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(500)
        self.log.setMinimumHeight(80)
        slay.addWidget(QtWidgets.QLabel("Log"))
        slay.addWidget(self.log, 1)
        side_scroll = QtWidgets.QScrollArea()
        side_scroll.setWidget(side)
        side_scroll.setWidgetResizable(True)
        side_scroll.setFixedWidth(375)
        side_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        vsplit = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        vsplit.addWidget(self.live)
        vsplit.addWidget(self.tabs)
        vsplit.setStretchFactor(0, 1)
        vsplit.setStretchFactor(1, 4)
        vsplit.setSizes([170, 700])
        hl = QtWidgets.QHBoxLayout()
        hl.addWidget(vsplit, 1)
        hl.addWidget(side_scroll)
        vlay.addLayout(hl, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage("ready")

        # ---- wiring ----------------------------------------------------------
        w = self.worker
        w.frame_ready.connect(self.live.set_frame)
        w.block_ready.connect(self.model.set_result)
        w.status.connect(self._status)
        w.error.connect(self._error)
        w.busy_changed.connect(self._busy_changed)
        w.camera_state.connect(self._camera_state)
        w.stage_state.connect(lambda ok, msg: self._short_label(self.lbl_stage, "stage", msg))
        self.chk_live.toggled.connect(self.live.setVisible)

        self.live_timer = QtCore.QTimer(self)
        self.live_timer.timeout.connect(self._live_tick)
        self.cmb_rate.currentIndexChanged.connect(self._set_live_rate)
        self._set_live_rate()
        self.live_timer.start()
        self._camera_ok = False

        w.start()
        # auto-connect the simulated devices so the GUI is usable straight away
        if self.store.get("stage").backend == "simulated":
            w.submit("connect_stage")
        if self.store.get("camera").backend == "simulated":
            w.submit("connect_camera")

        Shortcut = getattr(QtGui, "QShortcut", None) or QtWidgets.QShortcut   # Qt6 / Qt5
        Shortcut(QtGui.QKeySequence("Ctrl+R"), self, activated=lambda: w.submit("run", continuous=True))
        Shortcut(QtGui.QKeySequence("Escape"), self, activated=w.request_stop)

    # ------------------------------------------------------------------
    def _set_live_rate(self, *_):
        sec = float(self.cmb_rate.currentText().split()[0])
        self.live_timer.setInterval(int(sec * 1000))

    def _live_tick(self):
        # "live feed": grab a few frames whenever the camera is not averaging
        if (self.chk_live.isChecked() and self._camera_ok and not self._busy
                and self.worker.jobs.qsize() == 0):
            self.worker.submit("preview", n=4)

    def _busy_changed(self, busy, name):
        self._busy = busy
        self.lbl_busy.setText({"run": "averaging…", "dark": "recording dark…",
                               "calib_linear": "calibrating…", "calib_pixel": "calibration scan…"}
                              .get(name, name) if busy else "idle")
        self.lbl_busy.setStyleSheet(f"font-weight:bold; color:{'#f2c94c' if busy else '#7fd88f'};")

    @staticmethod
    def _short_label(label, what, msg, n=60):
        msg = " ".join(str(msg).split())
        label.setText(f"{what}: {msg[:n]}{'…' if len(msg) > n else ''}")
        label.setToolTip(msg)

    def _camera_state(self, ok, info):
        self._camera_ok = ok
        if ok and isinstance(info, dict):
            self._short_label(self.lbl_cam, "camera", info.get("name", ""))
        else:
            self._short_label(self.lbl_cam, "camera", info)

    def _status(self, msg):
        self.statusBar().showMessage(msg, 8000)
        self.log.appendPlainText(time.strftime("%H:%M:%S  ") + msg)

    def _error(self, msg):
        self.statusBar().showMessage("ERROR: " + msg.splitlines()[0], 15000)
        self.log.appendPlainText(time.strftime("%H:%M:%S  ERROR: ") + msg)

    def closeEvent(self, ev):
        self.live_timer.stop()
        self.worker.shutdown()
        try:
            self.store.save(self.settings_path)
        except Exception:
            pass
        super().closeEvent(ev)
