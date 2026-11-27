"""Unit tests for the processing pipeline (no hardware needed).   Run:  python -m pytest -q"""

import numpy as np
import pytest

from thz2d import processing as P
from thz2d.config import CameraSettings, ProcessingSettings, Settings, WindowSettings
from thz2d.hardware.camera import SimulatedCamera
from thz2d.hardware.stage import SimulatedStage


def sim_block(n_avg=25, positions=None, seed=0):
    cam = SimulatedCamera(stage_positions=(lambda: positions or {1: 0.0, 2: 0.0}), seed=seed)
    cs = CameraSettings(sim_rate_hz=1e9)
    cam.configure(cs)
    cam.start(4 * n_avg)
    frames = []
    while len(frames) < 4 * n_avg:
        fr, _ = cam.read(4 * n_avg - len(frames), timeout=1)
        frames += fr
    cam.stop()
    return cam, np.stack(frames)


def collapse_block(frames, ws=None):
    ws = ws or WindowSettings()
    wm, wp = P.windows_from_settings(ws)
    n = frames.shape[0] // 4
    Ip = P.collapse(frames, wp).reshape(n, 4, -1)
    Im = P.collapse(frames, wm).reshape(n, 4, -1)
    return Ip, Im


def test_collapse_and_window_check():
    img = np.arange(5 * 8, dtype=np.uint16).reshape(5, 8)
    w = P.Window(2, 1, 3, 2)
    np.testing.assert_allclose(P.collapse(img, w), img[1:3, 2:5].sum(0))
    w.flip = True
    np.testing.assert_allclose(P.collapse(img, w), img[1:3, 2:5].sum(0)[::-1])
    with pytest.raises(ValueError):
        P.collapse(img, P.Window(6, 0, 3, 1))


@pytest.mark.parametrize("ref", [0, 1, 2, 3])
@pytest.mark.parametrize("swap", [False, True])
def test_phase_map_structure(ref, swap):
    pm = P.PhaseMap.from_ref(ref, swap=swap)
    assert sorted([pm.ref, pm.a, pm.b, pm.ab]) == [0, 1, 2, 3]
    assert pm.b == (ref + 2) % 4            # REF and slow-only are two shots apart


@pytest.mark.parametrize("seed", range(6))
def test_detect_phase_on_simulated_camera(seed):
    cam, frames = sim_block(25, seed=seed)
    Ip, Im = collapse_block(frames)
    pm = P.detect_phase(Ip, Im)
    # simulator: block position p has type (phase0 + p) % 4 with 0 REF, 1 A, 2 B, 3 AB
    truth = {lab: (i - cam._phase0) % 4 for i, lab in enumerate(P.PHASES)}
    assert (pm.ref, pm.a, pm.b, pm.ab) == (truth["REF"], truth["A"], truth["B"], truth["AB"])
    assert pm.confidence > 2


def test_channels_and_nonlinear_signal():
    cam, frames = sim_block(50)
    Ip, Im = collapse_block(frames)
    res = P.process_block(Ip, Im, ProcessingSettings())
    ref = res.Ip_mean["REF"]
    core = ref > 0.3 * ref.max()                                 # well-illuminated pixels
    peak = {c: np.abs(res.S[c][core]).max() for c in P.CHANNELS}
    # A and B pulses: ~2 * kappa * 1 = 0.04 balanced; NL ~ 2 * kappa * 0.1 * envelope
    assert 0.03 < peak["A"] < 0.05
    assert 0.02 < peak["B"] < 0.04
    assert peak["NL"] < 0.2 * peak["A"]
    assert peak["NL"] > 5 * np.median(res.S_sem["NL"][core])     # NL is detected above noise
    # AB ~ A + B + NL
    resid = res.S["AB"] - res.S["A"] - res.S["B"] - res.S["NL"]
    assert np.abs(resid[res.valid]).max() < 1e-9


def test_dark_subtraction_changes_normalisation():
    cam, frames = sim_block(10)
    Ip, Im = collapse_block(frames)
    dark = np.full(frames.shape[1:], 100.0)
    wm, wp = P.windows_from_settings(WindowSettings())
    r1 = P.process_block(Ip, Im, ProcessingSettings())
    r2 = P.process_block(Ip, Im, ProcessingSettings(), P.collapse(dark, wp), P.collapse(dark, wm))
    assert np.abs(r2.S["A"]).max() > np.abs(r1.S["A"]).max()


def test_pixel_shift_and_linear_fit():
    x = np.arange(1000)
    f = lambda s: np.exp(-((x - 400 - s) / 20.0) ** 2) * np.sin((x - 400 - s) / 6.0)
    assert P.pixel_shift(f(0), f(12.3)) == pytest.approx(12.3, abs=0.1)
    delays = np.array([0, 200, 400, 600])
    shifts = -delays / 9.5          # 9.5 fs/px
    assert P.fit_fs_per_px(delays, shifts) == pytest.approx(9.5)


def test_linear_calibration_with_simulator():
    """Moving the probe delay must shift the trace by ~delay / 10 fs/px (simulator ~10 fs/px)."""
    traces = []
    delays = [0.0, 500.0, 1000.0]
    for d in delays:
        mm = P.fs_to_mm(d, passes=2)
        _, frames = sim_block(10, positions={1: mm, 2: 0.0})
        Ip, Im = collapse_block(frames)
        traces.append(P.process_block(Ip, Im, ProcessingSettings()).S["A"])
    shifts = [P.pixel_shift(traces[0], t) for t in traces]
    k = P.fit_fs_per_px(delays, shifts)
    assert 8.5 < k < 11.5


def test_pixel_calibration_recovers_axis():
    L, M = 400, 120
    x = np.arange(L)
    t_true_p = 12.0 * (x - L // 2) + 0.004 * (x - L // 2) ** 2       # fs
    t_true_m = t_true_p + 25.0
    a_true = 1 + 0.3 * np.cos(x / 60.0)
    taus = np.arange(M) * 40.0 - 0.5 * M * 40.0
    E = lambda t: -t / 300 * np.exp(-(t / 300) ** 2)
    Sp = a_true * E(taus[:, None] + t_true_p[None, :])
    Sm = -0.9 * a_true * E(taus[:, None] + t_true_m[None, :])
    cal = P.pixel_calibration(taus, Sp, Sm)
    inner = slice(60, L - 60)
    ref = t_true_p - t_true_p[cal["x0"]]
    assert np.abs(cal["tp"][inner] - ref[inner]).max() < 10
    assert np.abs(cal["tm"][inner] - (ref[inner] + 25)).max() < 10
    # apply: both polarizations of a single shot combine into one clean, uniformly sampled trace
    shot_p = a_true * E(t_true_p)
    shot_m = -0.9 * a_true * E(t_true_m)
    t, S = P.apply_pixel_calibration(shot_p, shot_m, cal)
    assert np.allclose(np.diff(t), np.diff(t)[0])
    i = np.argmax(S)                              # S = 2 E(t): positive lobe at -212 fs
    assert abs(t[i] * 1e3 - (-212)) < 30          # extremum of -t*exp(-t^2) at t = -300/sqrt(2)


def test_spectrum_peak():
    t = np.arange(0, 20, 0.01)          # ps
    s = np.sin(2 * np.pi * 0.8 * t) * np.exp(-((t - 10) / 3) ** 2)
    f, a = P.spectrum(t, s)
    assert f[np.argmax(a)] == pytest.approx(0.8, abs=0.02)


def test_fs_mm_roundtrip():
    assert P.fs_to_mm(1000.0, passes=2) == pytest.approx(0.1499, rel=1e-3)
    assert P.mm_to_fs(P.fs_to_mm(123.0, 2, -1), 2, -1) == pytest.approx(123.0)


def test_find_stripes():
    cam, frames = sim_block(1)
    st = P.find_stripes(frames.mean(0))
    assert abs(st["y0_top"] + st["height"] / 2 - 0.31 * 64) < 3
    assert abs(st["y0_bottom"] + st["height"] / 2 - 0.69 * 64) < 3


def test_settings_roundtrip(tmp_path):
    s = Settings()
    s.windows.length = 1234
    s.camera.roi = (1, 900, 2048, 1149)
    s.save(tmp_path / "s.json")
    s2 = Settings.load(tmp_path / "s.json")
    assert s2.windows.length == 1234 and s2.camera.roi == (1, 900, 2048, 1149)


def test_simulated_stage_limits():
    st = SimulatedStage()
    st.move_absolute(1, 25.1)
    assert st.position(1) == pytest.approx(25.1)
    with pytest.raises(Exception):
        st.move_absolute(1, 100.0)
