"""Tests for the Camware preset reader and the ESP301 command layer (fake transport, no hardware)."""

import pytest

from thz2d.camware_ini import read_camware_ini
from thz2d.hardware.stage import ESP301, StageError, VisaTransport, list_gpib_resources


# ----------------------------------------------------------------------------- Camware presets
def write(tmp_path, text, name="p.ini"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_preset_sdk_codes(tmp_path):
    p = write(tmp_path, """
[Camera]
Name=pco.edge 4.2
ExposureTime=250
ExposureTimebase=1
DelayTime=5
DelayTimebase=1
TriggerMode=2
PixelRate=272250000
TimestampMode=1
[ROI]
RoiX0=1
RoiY0=993
RoiX1=2048
RoiY1=1056
""")
    upd, rep = read_camware_ini(p)
    assert upd["exposure_us"] == pytest.approx(250)
    assert upd["delay_us"] == pytest.approx(5)
    assert upd["roi"] == (1, 993, 2048, 1056)
    assert upd["trigger"] == "external exposure start & software trigger"
    assert upd["pixel_rate_hz"] == 272250000
    assert upd["timestamp"] == "binary"
    assert not any("NOT FOUND" in r for r in rep)


def test_preset_text_values_and_units(tmp_path):
    p = write(tmp_path, """
exposure = 0.3 ms
trigger mode = External Exposure Start
roi = 1, 961, 2048, 1088
pixel rate = 272.25 MHz
timestamp = off
binning = 1
""")
    upd, rep = read_camware_ini(p)
    assert upd["exposure_us"] == pytest.approx(300)
    assert upd["roi"] == (1, 961, 2048, 1088)
    assert upd["trigger"] == "external exposure start & software trigger"
    assert upd["pixel_rate_hz"] == 272250000
    assert upd["timestamp"] == "binary"          # forced on for dropped-frame detection
    assert any("binning" in r for r in rep)


def test_preset_timebase_ms_and_missing(tmp_path):
    p = write(tmp_path, "[Timing]\nExposure=2\nExpTimeBase=2\nSomethingElse=7\n")
    upd, rep = read_camware_ini(p)
    assert upd["exposure_us"] == pytest.approx(2000)
    assert "roi" not in upd and "trigger" not in upd
    assert any("NOT FOUND" in r and "roi" in r for r in rep)
    assert any("SomethingElse" in r for r in rep)


# ----------------------------------------------------------------------------- ESP301
class FakeESP:
    """Minimal ESP301 command interpreter."""

    def __init__(self):
        self.pos = {1: 10.0, 2: 20.0}
        self.sent = []
        self.errors = []
        self.opened = False

    def describe(self):
        return "fake"

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False

    def write(self, cmd):
        self.sent.append(cmd)
        ax = int(cmd[0]) if cmd[0].isdigit() else None
        if cmd[1:3] == "PA":
            target = float(cmd[3:])
            if target > 100:
                self.errors.append("7, 0, PARAMETER OUT OF RANGE")
            else:
                self.pos[ax] = target

    def query(self, cmd):
        self.sent.append(cmd)
        if cmd == "VE?":
            return "ESP301 Version 3.0.1"
        if cmd == "TB?":
            return self.errors.pop(0) if self.errors else "0, 0, NO ERROR DETECTED"
        if cmd.endswith("SN?"):
            return "2"
        if cmd.endswith("TP"):
            return f"{self.pos[int(cmd[0])]:.6f}"
        if cmd.endswith("MD?"):
            return "1"
        if cmd.endswith("SL?"):
            return "-5"
        if cmd.endswith("SR?"):
            return "100"
        return ""


def test_esp301_commands():
    fake = FakeESP()
    st = ESP301(fake)
    assert "ESP301" in st.connect()
    assert st.position(1) == pytest.approx(10.0)
    assert st.move_absolute(2, 25.5) == pytest.approx(25.5)
    assert "2PA25.500000" in fake.sent
    assert st.move_relative(1, -0.25) == pytest.approx(9.75)
    with pytest.raises(StageError):              # outside the software limits
        st.move_absolute(1, 150.0)
    st.stop(1)
    assert fake.sent[-1] == "1ST"
    st.close()
    with pytest.raises(StageError):
        st.position(1)


def test_esp301_wrong_units():
    fake = FakeESP()
    fake.query = (lambda orig: (lambda c: "3" if c.endswith("SN?") else orig(c)))(fake.query)
    with pytest.raises(StageError, match="units"):
        ESP301(fake).connect()


def test_visa_transport_settings():
    class Dev:
        timeout = None
        write_termination = read_termination = None

        def clear(self):
            pass

        def query(self, c):
            raise Exception("VI_ERROR_TMO (-1073807339): Timeout expired")

    t = VisaTransport("GPIB0::1::INSTR", timeout=1.5)
    t.dev = Dev()
    assert t.query("VE?") == ""                  # timeouts look like an empty reply


def test_list_gpib_resources_without_visa_is_a_clear_error():
    try:
        res = list_gpib_resources()
    except StageError as e:
        assert "VISA" in str(e) or "pyvisa" in str(e).lower()
    else:
        assert isinstance(res, list)
