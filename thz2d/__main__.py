"""Entry point:  python -m thz2d [--sim] [--settings path.json]"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pyqtgraph as pg
from pyqtgraph.Qt import QtWidgets

from .config import DEFAULT_SETTINGS_PATH, Settings


def main(argv=None):
    ap = argparse.ArgumentParser(description="2D THz echelon single-shot acquisition GUI")
    ap.add_argument("--sim", action="store_true", help="use the simulated camera and stage")
    ap.add_argument("--settings", type=Path, default=DEFAULT_SETTINGS_PATH, help="settings JSON file")
    args = ap.parse_args(argv)

    pg.setConfigOptions(imageAxisOrder="row-major", antialias=False, background="k", foreground="#dddddd")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    settings = Settings.load(args.settings)
    if args.sim:
        settings.camera.backend = "simulated"
        settings.stage.backend = "simulated"
    from .gui.main_window import MainWindow
    win = MainWindow(settings, args.settings)
    win.resize(1700, 1000)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
