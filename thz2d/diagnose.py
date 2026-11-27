"""Hardware diagnostics without the GUI - run these first on the lab PC.

    python -m thz2d.diagnose ports
    python -m thz2d.diagnose camera --trigger auto              # no laser needed
    python -m thz2d.diagnose camera --trigger external          # laser triggering the camera
    python -m thz2d.diagnose stage --port COM3                  # read-only
    python -m thz2d.diagnose stage --port COM3 --move --axis 1  # +0.010 mm and back

Everything printed is also written to diagnose_<date>_<time>.txt in the current folder.
Add --sim to exercise the script itself with the simulated devices.
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback

import numpy as np

from .config import CameraSettings
from .hardware.camera import CameraError, PcoCamera, SimulatedCamera
from .hardware.stage import ESP301, SimulatedStage, StageError, list_serial_ports

TRIGGERS = {"auto": "auto sequence", "external": "external exposure start & software trigger"}
_log_file = None


def log(msg=""):
    print(msg, flush=True)
    if _log_file is not None:
        _log_file.write(msg + "\n")
        _log_file.flush()


def ok(cond, msg):
    log(("  [ OK ] " if cond else "  [FAIL] ") + msg)
    return cond


# ============================================================================ camera
def camera(args):
    rows = args.rows // 2 * 2
    y0 = 1024 - rows // 2 + 1
    cs = CameraSettings(backend="pco", interface=args.interface, exposure_us=args.exposure,
                        roi=(1, y0, 2048, y0 + rows - 1), trigger=TRIGGERS[args.trigger],
                        timestamp="binary", fifo_frames=args.fifo, sim_rate_hz=1000.0)
    log(f"camera test: trigger={cs.trigger!r}, ROI={cs.roi}, exposure={cs.exposure_us} us")
    if args.sim:
        cam = SimulatedCamera()
    else:
        cam = PcoCamera()
        try:
            cam.open(cs.interface, 0)
        except CameraError as e:
            ok(False, f"open camera: {e}")
            return False
    ok(True, "camera opened")
    all_ok = True
    try:
        if not args.sim:
            d = cam.cam.description
            for k in ("serial", "type", "interface type", "max width", "max height", "roi steps",
                      "roi is vert symmetric", "has timestamp", "pixelrates", "min exposure time",
                      "max exposure time", "has ram"):
                log(f"    description[{k!r}] = {d.get(k)}")
        for mode in ("fifo", "sequence"):
            cs.record_mode = mode
            log(f"\n-- record mode '{mode}' --")
            try:
                cam.configure(cs)
            except CameraError as e:
                all_ok &= ok(False, f"configure: {e}")
                continue
            ok(True, f"configured, image {cam.shape[1]} x {cam.shape[0]}")
            for k, v in cam.info().items():
                log(f"    {k}: {v}")
            all_ok &= _camera_stream(cam, mode, args.frames, args.timeout)
        if args.save:
            cs.record_mode = "sequence"
            cam.configure(cs)
            fr = cam.snap(4, timeout=args.timeout)
            if fr:
                np.save(args.save, np.stack(fr))
                log(f"\n  saved {len(fr)} frames to {args.save}")
    except Exception as e:
        all_ok = ok(False, f"unexpected error: {e}\n{traceback.format_exc()}")
    finally:
        cam.close()
    log("\nCAMERA: " + ("all checks passed" if all_ok else "SOME CHECKS FAILED - send this file"))
    return all_ok


def _camera_stream(cam, mode, n, timeout):
    good = True
    cam.start(n if mode == "sequence" else max(n // 2, 64))
    frames, counters = [], []
    t0 = time.monotonic()
    t_first = None
    n_first = 0
    err = None
    try:
        while len(frames) < n and time.monotonic() - t0 < timeout:
            fr, cn = cam.read(min(64, n - len(frames)), timeout=0.5)
            if fr and t_first is None:
                t_first, n_first = time.monotonic(), len(fr)
            frames += fr
            counters += cn
    except CameraError as e:
        err = e
    t_end = time.monotonic()
    overflow = getattr(cam, "fifo_overflow", False)
    cam.stop()
    if err is not None:
        good &= ok(False, f"read: {err}")
    good &= ok(len(frames) == n, f"received {len(frames)}/{n} frames in {t_end - t0:.2f} s")
    if not frames:
        log("    no frames: check the trigger cable / laser, or try --trigger auto")
        return False
    rate = (len(frames) - n_first) / (t_end - t_first) if t_end > t_first else float("nan")
    log(f"    frame rate after the first chunk: {rate:.0f} frames/s")
    a = np.stack(frames)
    log(f"    frames: shape {a.shape}, dtype {a.dtype}, min {a.min()}, max {a.max()}, mean {a.mean():.1f}")
    good &= ok(a.max() < 65000, "no saturation (max < 65000)")
    if all(c is None for c in counters):
        good &= ok(False, "no image counters in the metadata (timestamp mode?) -> "
                          "dropped-frame detection will not work")
    else:
        c = np.array([x for x in counters if x is not None], dtype=np.int64)
        d = np.diff(c)
        good &= ok(np.all(d != 0), f"no frame returned twice (counter repeats: {int(np.sum(d == 0))})")
        good &= ok(np.all(d == 1), f"consecutive image counters (gaps: {int(np.sum(d > 1))}, "
                                   f"first {c[0]}, last {c[-1]})")
    if mode == "fifo":
        good &= ok(not overflow, "no FIFO overflow")
    # do consecutive frames differ? (identical frames would mean the same buffer is re-read)
    same = sum(np.array_equal(a[i], a[i + 1]) for i in range(min(len(a) - 1, 50)))
    good &= ok(same == 0, f"consecutive frames differ ({same} identical pairs in the first 50)")
    # 4-shot modulation visible? (only meaningful with the THz on)
    prof = a.reshape(a.shape[0], -1).mean(1)
    if a.shape[0] >= 8:
        k = a.shape[0] // 4 * 4
        m4 = prof[:k].reshape(-1, 4).mean(0)
        log(f"    mean counts per position in the 4-shot cycle: {np.round(m4, 2)}")
    return good


# ============================================================================ stage
def stage(args):
    log(f"stage test: port={args.port}, baud={args.baud}, rtscts={args.baud == 19200}")
    st = SimulatedStage() if args.sim else ESP301(args.port, args.baud, rtscts=(args.baud == 19200))
    good = True
    try:
        ver = st.connect()
        ok(True, f"connected: {ver}")
    except StageError as e:
        ok(False, f"connect: {e}")
        log("    check: port name (python -m thz2d.diagnose ports), baud (USB 921600, RS-232 19200), "
            "ESP utility closed")
        return False
    try:
        for ax in (1, 2):
            try:
                p = st.position(ax)
                lo, hi = st.limits(ax)
                extra = ""
                if isinstance(st, ESP301):
                    extra = (f", units code {st.query(f'{ax}SN?')} (2 = mm), motor on? {st.query(f'{ax}MO?')}, "
                             f"stage id {st.query(f'{ax}ID?')!r}")
                ok(True, f"axis {ax}: position {p:.5f} mm, limits [{lo:g}, {hi:g}]{extra}")
            except StageError as e:
                log(f"  [ -- ] axis {ax}: {e} (no stage on this axis?)")
        if isinstance(st, ESP301):
            st._check_error()
        if args.move:
            ax = args.axis
            st.motor_on(ax)
            p0 = st.position(ax)
            t0 = time.monotonic()
            p1 = st.move_relative(ax, 0.010, wait=True, timeout=30)
            t1 = time.monotonic()
            good &= ok(abs(p1 - p0 - 0.010) < 0.002, f"axis {ax}: +0.010 mm -> {p0:.5f} → {p1:.5f} mm "
                                                    f"in {t1 - t0:.2f} s")
            p2 = st.move_absolute(ax, p0, wait=True, timeout=30)
            good &= ok(abs(p2 - p0) < 0.002, f"axis {ax}: back to {p2:.5f} mm")
    except StageError as e:
        good = ok(False, str(e))
    finally:
        st.close()
    log("\nSTAGE: " + ("all checks passed" if good else "SOME CHECKS FAILED - send this file"))
    return good


def ports(_args):
    p = list_serial_ports()
    log("serial ports: " + (", ".join(p) if p else "none found"))
    return True


def main(argv=None):
    global _log_file
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("camera")
    c.add_argument("--trigger", choices=list(TRIGGERS), default="auto")
    c.add_argument("--interface", default="", help='e.g. "USB 3.0" or "CLHS" (default: search)')
    c.add_argument("--rows", type=int, default=64)
    c.add_argument("--exposure", type=float, default=100.0, help="µs")
    c.add_argument("--frames", type=int, default=2000)
    c.add_argument("--fifo", type=int, default=400)
    c.add_argument("--timeout", type=float, default=15.0)
    c.add_argument("--save", default="", help="save 4 frames to this .npy file")
    s = sub.add_parser("stage")
    s.add_argument("--port", default="COM3")
    s.add_argument("--baud", type=int, default=921600)
    s.add_argument("--move", action="store_true", help="move +0.010 mm and back")
    s.add_argument("--axis", type=int, default=1)
    sub.add_parser("ports")
    for p in (c, s):
        p.add_argument("--sim", action="store_true")
    args = ap.parse_args(argv)
    name = time.strftime(f"diagnose_{args.cmd}_%Y%m%d_%H%M%S.txt")
    with open(name, "w", encoding="utf-8") as f:
        _log_file = f
        log(f"thz2d diagnose {args.cmd}  {time.ctime()}  python {sys.version.split()[0]}")
        try:
            import pco
            log(f"pco package {getattr(pco, '__version__', '?')}")
        except Exception:
            log("pco package not importable")
        res = {"camera": camera, "stage": stage, "ports": ports}[args.cmd](args)
        log(f"\nlog written to {name}")
    _log_file = None
    return 0 if res else 1


if __name__ == "__main__":
    sys.exit(main())
