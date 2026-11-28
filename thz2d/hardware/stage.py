"""Newport ESP301 motion controller (ASCII command set over GPIB, RS-232 or USB).

Commands used (ESP301 user manual, section 3):
    xxMO      motor on                  xxMF      motor off
    xxPAnn    move to absolute position xxPRnn    move relative
    xxTP      tell actual position      xxMD?     motion done (1 = done)
    xxST      stop motion               xxSN?     units (2 = mm)
    xxSL?     left software limit       xxSR?     right software limit
    TB?       read error buffer (code, timestamp, message)
    VE?       firmware version
Commands are terminated with CR; replies end with CR LF.

Connections
* GPIB (default in this lab): through VISA (PyVISA + NI-VISA / NI-488.2), resource names
  like ``GPIB0::1::INSTR`` (board 0, primary address 1 - the address is set on the ESP301).
* Serial: the USB port appears as a virtual COM port at 921600 baud; the RS-232 port
  defaults to 19200 baud with RTS/CTS hardware handshake.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional


class StageError(RuntimeError):
    pass


def list_serial_ports() -> List[str]:
    try:
        from serial.tools import list_ports
        return [p.device for p in list_ports.comports()]
    except Exception:
        return []


def _resource_manager():
    """PyVISA resource manager: NI-VISA (or Keysight VISA) first, pure-python backend as fallback."""
    try:
        import pyvisa
    except ImportError as e:
        raise StageError("PyVISA is not installed: pip install pyvisa") from e
    errors = []
    for backend in ("", "@py"):
        try:
            return pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()
        except Exception as e:  # no VISA library found
            errors.append(f"{backend or 'system VISA'}: {e}")
    raise StageError("no VISA library found (install NI-VISA / NI-488.2). " + " | ".join(errors))


def list_gpib_resources() -> List[str]:
    """GPIB instruments seen by VISA, e.g. ['GPIB0::1::INSTR']. Raises StageError if VISA is missing."""
    rm = _resource_manager()
    try:
        found = list(rm.list_resources("GPIB?*::INSTR"))
    except Exception:
        found = []          # list_resources raises when nothing is found on some VISA versions
    return sorted(found)


class SerialTransport:
    def __init__(self, port: str, baudrate: int = 921600, rtscts: bool = False, timeout: float = 1.0):
        self.port, self.baudrate, self.rtscts, self.timeout = port, baudrate, rtscts, timeout
        self.dev = None

    def describe(self) -> str:
        return f"{self.port} at {self.baudrate} baud"

    def open(self):
        try:
            import serial
        except ImportError as e:
            raise StageError("pyserial is not installed: pip install pyserial") from e
        try:
            self.dev = serial.Serial(self.port, self.baudrate, bytesize=8, parity="N", stopbits=1,
                                     timeout=self.timeout, rtscts=self.rtscts)
        except Exception as e:
            raise StageError(f"cannot open {self.port}: {e}") from e
        self.dev.reset_input_buffer()

    def close(self):
        if self.dev is not None:
            try:
                self.dev.close()
            finally:
                self.dev = None

    def write(self, cmd: str):
        self.dev.write((cmd + "\r").encode("ascii"))

    def query(self, cmd: str) -> str:
        self.dev.reset_input_buffer()
        self.dev.write((cmd + "\r").encode("ascii"))
        return self.dev.readline().decode("ascii", errors="replace").strip()


class VisaTransport:
    """GPIB (or any VISA resource) through PyVISA."""

    def __init__(self, resource: str, timeout: float = 2.0):
        self.resource, self.timeout = resource, timeout
        self.dev = None
        self.rm = None

    def describe(self) -> str:
        return self.resource

    def open(self):
        self.rm = _resource_manager()
        try:
            self.dev = self.rm.open_resource(self.resource)
        except Exception as e:
            raise StageError(f"cannot open {self.resource}: {e}") from e
        self.dev.timeout = int(self.timeout * 1000)
        self.dev.write_termination = "\r"
        self.dev.read_termination = "\r\n"
        try:
            self.dev.clear()          # GPIB device clear: flushes half-finished replies
        except Exception:
            pass

    def close(self):
        if self.dev is not None:
            try:
                self.dev.close()
            finally:
                self.dev = None

    def write(self, cmd: str):
        self.dev.write(cmd)

    def query(self, cmd: str) -> str:
        try:
            return self.dev.query(cmd).strip()
        except Exception as e:     # VISA timeout -> behave like an empty serial reply
            if "timeout" in str(e).lower() or "VI_ERROR_TMO" in str(e):
                return ""
            raise StageError(f"{self.resource}: {e}") from e


class StageBase:
    name = "stage"
    axes = (1, 2)

    def connect(self) -> str: ...
    def close(self) -> None: ...
    def position(self, axis: int) -> float: ...
    def move_absolute(self, axis: int, mm: float, wait: bool = True, timeout: float = 60.0) -> float: ...
    def move_relative(self, axis: int, mm: float, wait: bool = True, timeout: float = 60.0) -> float: ...
    def stop(self, axis: Optional[int] = None) -> None: ...
    def motor_on(self, axis: int) -> None: ...
    def limits(self, axis: int):
        return (-float("inf"), float("inf"))

    def positions(self) -> Dict[int, float]:
        return {a: self.position(a) for a in self.axes}


class ESP301(StageBase):
    name = "ESP301"

    def __init__(self, transport):
        self.transport = transport
        self._open = False
        self._lock = threading.RLock()     # GUI (stop) and acquisition thread may both talk to it
        self._abort = threading.Event()

    @classmethod
    def gpib(cls, resource: str = "GPIB0::1::INSTR", timeout: float = 2.0) -> "ESP301":
        return cls(VisaTransport(resource, timeout))

    @classmethod
    def serial(cls, port: str, baudrate: int = 921600, rtscts: bool = False) -> "ESP301":
        return cls(SerialTransport(port, baudrate, rtscts))

    # ------------------------------------------------------------------ low level
    def connect(self) -> str:
        with self._lock:
            self.transport.open()
            self._open = True
        ver = self.query("VE?")
        if not ver:
            self.close()
            raise StageError(f"no answer from the ESP301 on {self.transport.describe()} "
                             "(check the GPIB address / cable, or the baud rate for serial)")
        self._check_error()
        bad_units = []
        for ax in self.axes:
            try:
                units = int(float(self.query(f"{ax}SN?")))
                if units != 2:
                    bad_units.append(f"axis {ax} units code is {units}, expected 2 (mm); "
                                     f"set it with '{ax}SN2' in the ESP utility")
            except ValueError:
                pass            # axis without a stage attached
        for _ in range(10):     # clear errors from unpopulated axes
            r = self.query("TB?")
            if not r or r.split(",")[0].strip() == "0":
                break
        if bad_units:
            self.close()
            raise StageError("; ".join(bad_units))
        return ver

    def close(self):
        with self._lock:
            if self._open:
                try:
                    self.transport.close()
                finally:
                    self._open = False

    def write(self, cmd: str) -> None:
        with self._lock:
            if not self._open:
                raise StageError("stage not connected")
            self.transport.write(cmd)

    def query(self, cmd: str) -> str:
        with self._lock:
            if not self._open:
                raise StageError("stage not connected")
            return self.transport.query(cmd)

    def _check_error(self) -> None:
        """Read the ESP error buffer; raise on a non-zero code."""
        msg = self.query("TB?")
        if not msg:
            return
        code = msg.split(",")[0].strip()
        try:
            code_i = int(code)
        except ValueError:
            return
        if code_i != 0:
            # drain the remaining queued errors
            for _ in range(10):
                more = self.query("TB?")
                if not more or more.split(",")[0].strip() == "0":
                    break
            raise StageError(f"ESP301 error: {msg}")

    # ------------------------------------------------------------------ high level
    def motor_on(self, axis: int) -> None:
        self.write(f"{axis}MO")
        self._check_error()

    def position(self, axis: int) -> float:
        r = self.query(f"{axis}TP")
        try:
            return float(r)
        except ValueError as e:
            raise StageError(f"bad position reply {r!r}") from e

    def limits(self, axis: int):
        try:
            return float(self.query(f"{axis}SL?")), float(self.query(f"{axis}SR?"))
        except ValueError:
            return (-float("inf"), float("inf"))

    def is_done(self, axis: int) -> bool:
        return self.query(f"{axis}MD?").strip() == "1"

    def wait(self, axis: int, timeout: float = 60.0) -> float:
        t_end = time.monotonic() + timeout
        self._abort.clear()
        while not self.is_done(axis):
            if self._abort.is_set():
                raise StageError("move aborted")
            if time.monotonic() > t_end:
                self.stop(axis)
                raise StageError(f"axis {axis} did not finish moving within {timeout:.0f} s")
            time.sleep(0.02)
        return self.position(axis)

    def move_absolute(self, axis, mm, wait=True, timeout=60.0):
        lo, hi = self.limits(axis)
        if not (lo <= mm <= hi):
            raise StageError(f"target {mm:.4f} mm outside software limits [{lo:g}, {hi:g}] mm")
        self.write(f"{axis}PA{mm:.6f}")
        self._check_error()
        return self.wait(axis, timeout) if wait else mm

    def move_relative(self, axis, mm, wait=True, timeout=60.0):
        target = self.position(axis) + mm
        return self.move_absolute(axis, target, wait, timeout)

    def stop(self, axis=None):
        self._abort.set()
        if axis is None:
            for ax in self.axes:
                self.write(f"{ax}ST")
        else:
            self.write(f"{axis}ST")


class SimulatedStage(StageBase):
    name = "Simulated stage"

    def __init__(self, speed_mm_s: float = 5.0):
        self._pos = {1: 25.0, 2: 25.0}
        self._origin = dict(self._pos)
        self.speed = speed_mm_s
        self._lock = threading.Lock()

    def connect(self):
        return "Simulated ESP301"

    def close(self):
        pass

    def position(self, axis):
        with self._lock:
            return self._pos[axis]

    def relative_positions(self) -> Dict[int, float]:
        """Offsets from the start position (what the simulated camera uses)."""
        with self._lock:
            return {a: self._pos[a] - self._origin[a] for a in self._pos}

    def limits(self, axis):
        return (0.0, 50.0)

    def move_absolute(self, axis, mm, wait=True, timeout=60.0):
        lo, hi = self.limits(axis)
        if not (lo <= mm <= hi):
            raise StageError(f"target {mm:.4f} mm outside limits [{lo}, {hi}] mm")
        dist = abs(mm - self.position(axis))
        time.sleep(min(dist / self.speed, 0.5) + 0.01)
        with self._lock:
            self._pos[axis] = float(mm)
        return float(mm)

    def move_relative(self, axis, mm, wait=True, timeout=60.0):
        return self.move_absolute(axis, self.position(axis) + mm, wait, timeout)

    def stop(self, axis=None):
        pass

    def motor_on(self, axis):
        pass


def make_stage(ss) -> StageBase:
    if ss.backend == "esp301":
        if ss.interface == "gpib":
            return ESP301.gpib(ss.resource)
        return ESP301.serial(ss.port, ss.baudrate, ss.rtscts)
    return SimulatedStage()
