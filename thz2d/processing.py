"""Signal processing for echelon single-shot (differential-chopping) 2D THz detection.

Everything here is pure numpy so it can be unit-tested without hardware.

Pipeline for one block of 4*N camera frames (Gao et al. 2022, Supp. Note 1):

1. ``collapse``: sum each polarization window over its rows -> I+(x), I-(x)
   for every frame, after dark subtraction.
2. ``detect_phase``: figure out which of the 4 positions inside the repeating
   4-shot cycle is REF (both pumps off), A-only, B-only and AB.
3. ``compute_channels``: per 4-shot cycle (shot-to-shot referencing)
       S+/-_c = (I+/-_c - I+/-_REF) / I+/-_REF          c = AB, A, B
       S+/-_NL = (I_AB - I_A - I_B + I_REF) / I_REF
   and the balanced signal S_c = S+_c - S-_c, averaged over the N cycles.
4. ``TimeAxis.combine``: map pixel -> time, either with a linear fs/px factor
   or with the per-pixel calibration t*(x), a(x) of Supp. Note 1, which is
   applied to each polarization separately before subtracting them.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import numpy as np

PHASES = ("REF", "A", "B", "AB")        # labels of the 4 shots in a cycle
CHANNELS = ("AB", "A", "B", "NL")       # displayed channels
C_MM_PER_FS = 299792458.0 * 1e3 * 1e-15  # light travel in mm per fs (2.998e-4)


# ----------------------------------------------------------------------------- windows
@dataclass
class Window:
    x0: int
    y0: int
    width: int
    height: int
    flip: bool = False

    def check(self, shape: Tuple[int, int]) -> None:
        h, w = shape
        if self.width < 2 or self.height < 1:
            raise ValueError("window must be at least 2 px long and 1 px high")
        if self.x0 < 0 or self.y0 < 0 or self.x0 + self.width > w or self.y0 + self.height > h:
            raise ValueError(
                f"window x={self.x0}..{self.x0 + self.width - 1}, y={self.y0}..{self.y0 + self.height - 1} "
                f"is outside the {w} x {h} camera image")


def windows_from_settings(ws) -> Tuple[Window, Window]:
    """(minus, plus) windows from a config.WindowSettings."""
    wm = Window(ws.x0_minus, ws.y0_minus, ws.length, ws.height, ws.flip_minus)
    wp = Window(ws.x0_plus, ws.y0_plus, ws.length, ws.height, False)
    return wm, wp


def collapse(frames: np.ndarray, win: Window) -> np.ndarray:
    """Sum a window over its rows. frames: (..., h, w) -> (..., width) float64."""
    win.check(frames.shape[-2:])
    sub = frames[..., win.y0:win.y0 + win.height, win.x0:win.x0 + win.width]
    out = sub.sum(axis=-2, dtype=np.float64)
    if win.flip:
        out = out[..., ::-1]
    return out


def profile_full_width(image: np.ndarray, win: Window) -> np.ndarray:
    """Row-sum over the window's rows but across the full image width (for the Setup tab)."""
    h, w = image.shape[-2:]
    y0 = int(np.clip(win.y0, 0, h - 1))
    y1 = int(np.clip(win.y0 + win.height, y0 + 1, h))
    return image[..., y0:y1, :].sum(axis=-2, dtype=np.float64)


def find_stripes(image: np.ndarray, smooth: int = 3, threshold: float = 0.1):
    """Locate two horizontal beam-array stripes (beam displacer geometry of Gao et al. Fig. 1c).

    Returns dict(y0_top, y0_bottom, height, x0_top, x0_bottom, length) or raises ValueError.
    The boundary between the stripes is the minimum of the smoothed row profile between
    the two maxima (the paper's y_mid, taken constant along x).
    """
    img = np.asarray(image, dtype=np.float64)
    img = img - np.median(img)
    rows = _smooth(img.sum(axis=1), smooth)
    h = rows.size
    p1 = int(np.argmax(rows))
    # second peak: maximum outside the first peak's lobe
    lobe = _lobe(rows, p1)
    masked = rows.copy()
    masked[lobe[0]:lobe[1] + 1] = -np.inf
    if not np.isfinite(masked).any():
        raise ValueError("could not find two stripes")
    p2 = int(np.argmax(masked))
    top, bot = sorted((p1, p2))
    if rows[p2] < 0.2 * rows[p1]:
        raise ValueError("second stripe is too weak (<20% of the first)")
    mid = top + int(np.argmin(rows[top:bot + 1]))
    half = max(1, min(mid - top, bot - mid))
    height = max(1, 2 * half - 1)
    y_top = max(0, top - half + 1)
    y_bot = max(0, bot - half + 1)
    y_bot = min(y_bot, h - height)

    def xrange_(y0):
        cols = _smooth(img[y0:y0 + height].sum(axis=0), 9)
        on = np.flatnonzero(cols > threshold * cols.max())
        return int(on[0]), int(on[-1])

    xt = xrange_(y_top)
    xb = xrange_(y_bot)
    length = min(xt[1] - xt[0], xb[1] - xb[0]) + 1
    return dict(y0_top=y_top, y0_bottom=y_bot, height=height,
                x0_top=xt[0], x0_bottom=xb[0], length=int(length), y_mid=mid)


def _lobe(y: np.ndarray, p: int):
    lo = p
    while lo > 0 and y[lo - 1] <= y[lo]:
        lo -= 1
    hi = p
    while hi < y.size - 1 and y[hi + 1] <= y[hi]:
        hi += 1
    return lo, hi


def _smooth(y: np.ndarray, w: int) -> np.ndarray:
    """Moving average with edge padding along the last axis."""
    w = int(max(1, w))
    if w == 1:
        return np.asarray(y, dtype=np.float64)
    k = np.ones(w) / w
    pad = w // 2
    y = np.asarray(y, dtype=np.float64)
    if y.ndim == 1:
        yp = np.pad(y, (pad, w - 1 - pad), mode="edge")
        return np.convolve(yp, k, mode="valid")
    return np.stack([_smooth(r, w) for r in y.reshape(-1, y.shape[-1])]).reshape(y.shape)


# ----------------------------------------------------------------------------- phase detection
@dataclass
class PhaseMap:
    """Position (0..3) of each shot type inside the repeating 4-shot cycle."""
    ref: int
    a: int
    b: int
    ab: int
    auto: bool = True
    confidence: float = float("inf")   # E(2nd lowest) / E(lowest); >~2 is trustworthy
    energies: Tuple[float, ...] = ()

    def index(self, label: str) -> int:
        return {"REF": self.ref, "A": self.a, "B": self.b, "AB": self.ab}[label]

    def describe(self) -> str:
        order = [""] * 4
        for lab in PHASES:
            order[self.index(lab)] = lab
        src = f"auto, confidence {self.confidence:.1f}x" if self.auto else "manual"
        return "cycle = " + " / ".join(order) + f"  ({src})"

    @classmethod
    def from_ref(cls, ref: int, swap: bool = False, a_is_fast: bool = True, **kw) -> "PhaseMap":
        """Build the map from the REF position.

        With a fast (laser/2) and a slow (laser/4) chopper the cycle is always
        REF, X, slow-only, Y with {X, Y} = {fast-only, both}: the fast chopper
        toggles every shot so REF and slow-only are two shots apart.
        """
        slow_only = (ref + 2) % 4
        x, y = (ref + 1) % 4, (ref + 3) % 4
        fast_only, both = (y, x) if swap else (x, y)
        if a_is_fast:
            a, b = fast_only, slow_only
        else:
            a, b = slow_only, fast_only
        return cls(ref=ref % 4, a=a, b=b, ab=both, **kw)


def detect_phase(Ip: np.ndarray, Im: np.ndarray, a_is_fast: bool = True) -> PhaseMap:
    """Find REF/A/B/AB from collapsed traces of one block.

    Ip, Im: (n_cycles, 4, L) collapsed intensities, frame k of the block -> [k // 4, k % 4].

    REF = the cycle position with the smallest THz modulation, measured as the
    energy of the high-pass filtered normalized balanced signal (I+ - I-)/(I+ + I-)
    (static polarization imbalance is smooth and is removed by the high-pass).
    The two candidates for fast-only / both are then resolved with the linearity
    relation AB - A - B + REF = NL (small): the wrong assignment leaves a residual of ~2B.
    """
    mp = Ip.mean(axis=0)
    mm = Im.mean(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        D = (mp - mm) / (mp + mm)
    D = np.nan_to_num(D)
    L = D.shape[-1]
    w = max(5, L // 25)
    hp = D - _smooth(D, w)
    trim = slice(w, L - w) if L > 4 * w else slice(None)
    E = (hp[:, trim] ** 2).mean(axis=-1)
    order = np.argsort(E)
    ref = int(order[0])
    conf = float(E[order[1]] / E[order[0]]) if E[order[0]] > 0 else float("inf")

    x, s, y = (ref + 1) % 4, (ref + 2) % 4, (ref + 3) % 4
    d = D - D[ref]
    res_xy = np.linalg.norm(d[y] - d[x] - d[s])   # x = fast-only, y = both
    res_yx = np.linalg.norm(d[x] - d[y] - d[s])   # y = fast-only, x = both
    swap = bool(res_yx < res_xy)
    return PhaseMap.from_ref(ref, swap=swap, a_is_fast=a_is_fast, auto=True,
                             confidence=conf, energies=tuple(float(e) for e in E))


# ----------------------------------------------------------------------------- channels
@dataclass
class BlockResult:
    n_cycles: int
    phase_map: PhaseMap
    # per-polarization, pixel-domain signals (mean over cycles) and standard errors
    Sp: Dict[str, np.ndarray]
    Sm: Dict[str, np.ndarray]
    S: Dict[str, np.ndarray]            # balanced S+ - S-, pixel domain
    S_sem: Dict[str, np.ndarray]        # standard error of the balanced signal
    Ip_mean: Dict[str, np.ndarray]      # mean collapsed intensity per shot type (dark subtracted)
    Im_mean: Dict[str, np.ndarray]
    valid: np.ndarray                   # bool mask of usable pixels
    mean_images: Dict[str, np.ndarray] = field(default_factory=dict)  # per shot type, full frame
    last_frame: Optional[np.ndarray] = None
    stage_mm: Dict[int, float] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    frame_rate: float = 0.0
    dropped: int = 0
    windows: Tuple = ()


def compute_channels(Ip: np.ndarray, Im: np.ndarray, pm: PhaseMap,
                     dark_p: Optional[np.ndarray] = None, dark_m: Optional[np.ndarray] = None,
                     min_ref_fraction: float = 0.02):
    """Shot-to-shot referenced channels. Ip, Im: (n_cycles, 4, L) raw collapsed intensities.

    Returns (Sp, Sm, S, S_sem, Ip_mean, Im_mean, valid).
    """
    Ip = np.asarray(Ip, dtype=np.float64)
    Im = np.asarray(Im, dtype=np.float64)
    if dark_p is not None:
        Ip = Ip - dark_p
    if dark_m is not None:
        Im = Im - dark_m
    n = Ip.shape[0]

    def parts(I):
        return {lab: I[:, pm.index(lab), :] for lab in PHASES}

    P, M = parts(Ip), parts(Im)
    refp_mean = P["REF"].mean(0)
    refm_mean = M["REF"].mean(0)
    valid = (refp_mean > min_ref_fraction * max(refp_mean.max(), 1e-12)) & \
            (refm_mean > min_ref_fraction * max(refm_mean.max(), 1e-12))

    def ratio(num, R):
        with np.errstate(divide="ignore", invalid="ignore"):
            out = np.where(valid, num / R, 0.0)
        return np.nan_to_num(out, nan=0.0, posinf=0.0, neginf=0.0)

    def per_cycle(X):
        R = X["REF"]
        return {"AB": ratio(X["AB"] - R, R), "A": ratio(X["A"] - R, R), "B": ratio(X["B"] - R, R),
                "NL": ratio(X["AB"] - X["A"] - X["B"] + R, R)}

    per_p = per_cycle(P)
    per_m = per_cycle(M)

    Sp = {c: per_p[c].mean(0) for c in CHANNELS}
    Sm = {c: per_m[c].mean(0) for c in CHANNELS}
    S, S_sem = {}, {}
    for c in CHANNELS:
        bal = per_p[c] - per_m[c]
        S[c] = bal.mean(0)
        S_sem[c] = bal.std(0, ddof=1) / np.sqrt(n) if n > 1 else np.zeros_like(S[c])
    Ip_mean = {lab: P[lab].mean(0) for lab in PHASES}
    Im_mean = {lab: M[lab].mean(0) for lab in PHASES}
    return Sp, Sm, S, S_sem, Ip_mean, Im_mean, valid


def process_block(Ip: np.ndarray, Im: np.ndarray, proc, dark_p=None, dark_m=None) -> BlockResult:
    """Phase detection + channel computation for one block.

    Ip, Im: (n_cycles, 4, L) raw collapsed intensities; proc: config.ProcessingSettings.
    """
    if Ip.shape[0] < 1:
        raise ValueError("need at least one complete 4-shot cycle")
    if proc.phase_mode == "manual":
        pm = PhaseMap.from_ref(proc.manual_ref_phase, swap=proc.manual_swap,
                               a_is_fast=proc.a_is_fast, auto=False)
    else:
        dp = Ip - dark_p if dark_p is not None else Ip
        dm = Im - dark_m if dark_m is not None else Im
        pm = detect_phase(dp, dm, a_is_fast=proc.a_is_fast)
    Sp, Sm, S, S_sem, Ip_mean, Im_mean, valid = compute_channels(
        Ip, Im, pm, dark_p, dark_m, proc.min_ref_fraction)
    return BlockResult(n_cycles=Ip.shape[0], phase_map=pm, Sp=Sp, Sm=Sm, S=S, S_sem=S_sem,
                       Ip_mean=Ip_mean, Im_mean=Im_mean, valid=valid)


# ----------------------------------------------------------------------------- time axis
class TimeAxis:
    """Pixel -> time mapping. Times are returned in ps."""

    def __init__(self, mode="linear", fs_per_px=10.0, t0_px=0.0, calib: Optional[dict] = None):
        self.mode = mode
        self.fs_per_px = float(fs_per_px)
        self.t0_px = float(t0_px)
        self.calib = calib
        self.warning = ""

    @classmethod
    def from_settings(cls, ts, calib: Optional[dict] = None) -> "TimeAxis":
        return cls(ts.mode, ts.fs_per_px, ts.t0_px, calib)

    def linear_axis(self, L: int) -> np.ndarray:
        return (np.arange(L) - self.t0_px) * self.fs_per_px * 1e-3

    def combine(self, Sp: np.ndarray, Sm: np.ndarray, valid: Optional[np.ndarray] = None):
        """Balanced waveform on a uniform time axis: returns (t_ps, S)."""
        L = Sp.size
        self.warning = ""
        if self.mode == "pixel":
            c = self.calib
            if c is None:
                self.warning = "no per-pixel calibration loaded - using linear axis"
            elif c["tp"].size != L:
                self.warning = (f"calibration is for windows of {c['tp'].size} px, current windows are {L} px "
                                "- using linear axis")
            else:
                return apply_pixel_calibration(Sp, Sm, c, valid)
        t = self.linear_axis(L)
        S = Sp - Sm
        if self.fs_per_px < 0:
            t, S = t[::-1], S[::-1]
        return t, S


def apply_pixel_calibration(Sp, Sm, calib, valid=None):
    """Supp. Note 1: S+/-(t) = Interp[S+/-(x)/a+/-(x) at t*+/-(x) -> t];  dI/I(t) = S+(t) - S-(t)."""
    tp, tm = calib["tp"], calib["tm"]          # fs
    ap, am = calib["ap"], calib["am"]
    okp = np.isfinite(tp) & np.isfinite(ap) & (ap > 0)
    okm = np.isfinite(tm) & np.isfinite(am) & (am > 0)
    if valid is not None:
        okp &= valid
        okm &= valid
    if okp.sum() < 4 or okm.sum() < 4:
        L = Sp.size
        return np.arange(L, dtype=float), Sp - Sm
    lo = max(np.min(tp[okp]), np.min(tm[okm]))
    hi = min(np.max(tp[okp]), np.max(tm[okm]))
    dt = float(np.median(np.abs(np.diff(np.sort(tp[okp])))))
    dt = dt if dt > 0 else 1.0
    t = np.arange(lo, hi + 0.5 * dt, dt)

    def interp(tt, S, a, ok):
        o = np.argsort(tt[ok])
        return np.interp(t, tt[ok][o], (S[ok] / a[ok])[o])

    S = interp(tp, Sp, ap, okp) - interp(tm, Sm, am, okm)
    return t * 1e-3, S


def pixel_calibration(taus_fs: np.ndarray, Sp: np.ndarray, Sm: np.ndarray, x0: Optional[int] = None,
                      smooth_order: int = 3, min_amp: float = 0.1) -> dict:
    """Per-pixel time calibration from an EO-delay scan (Gao et al., Supp. Note 1).

    taus_fs: (M,) uniformly spaced stage delays in fs.
    Sp, Sm:  (M, L) single-shot signals S+/-(x, tau) (pixel domain).

    For each pixel the relative delay t*(x) maximizes the cross-correlation of its
    EO trace with the reference trace S+(x0, tau); a(x) is the normalized
    cross-correlation amplitude at t*. A polynomial of order `smooth_order`
    (weighted by a) is fitted to t*(x) to give a smooth monotonic axis.
    Returns dict(tp, tm, ap, am, tp_raw, tm_raw, x0, taus_fs).
    """
    taus = np.asarray(taus_fs, dtype=np.float64)
    Sp = np.asarray(Sp, dtype=np.float64)
    Sm = np.asarray(Sm, dtype=np.float64)
    M, L = Sp.shape
    if M < 4:
        raise ValueError("need at least 4 delay points")
    dtau = float(np.median(np.diff(taus)))
    if x0 is None:
        x0 = L // 2
    # A single polarization still contains the shot-to-shot laser-intensity fluctuation (it only
    # cancels in the balanced S+ - S-). It is the same for every pixel of a given delay point and
    # would dominate the correlation at zero lag, so remove the per-delay spatial median first.
    Sp = Sp - np.median(Sp, axis=1, keepdims=True)
    Sm = Sm - np.median(Sm, axis=1, keepdims=True)
    R = Sp[:, x0] - Sp[:, x0].mean()
    rr = float(np.dot(R, R)) or 1.0
    nfft = int(2 ** np.ceil(np.log2(2 * M)))
    FR = np.conj(np.fft.rfft(R, nfft))
    max_lag = M - 1

    def analyse(S):
        T = S - S.mean(axis=0, keepdims=True)
        c = np.fft.irfft(np.fft.rfft(T, nfft, axis=0) * FR[:, None], nfft, axis=0)   # (nfft, L)
        # c[k] = sum_n T[n + k] R[n]; lags -max_lag..max_lag
        c = np.concatenate([c[nfft - max_lag:], c[:max_lag + 1]], axis=0)
        lags = np.arange(-max_lag, max_lag + 1)
        k = np.argmax(np.abs(c), axis=0)
        cols = np.arange(L)
        kc = np.clip(k, 1, c.shape[0] - 2)
        y0, y1, y2 = np.abs(c[kc - 1, cols]), np.abs(c[kc, cols]), np.abs(c[kc + 1, cols])
        den = y0 - 2 * y1 + y2
        with np.errstate(divide="ignore", invalid="ignore"):
            frac = np.where(den != 0, 0.5 * (y0 - y2) / den, 0.0)
        frac = np.clip(np.nan_to_num(frac), -0.5, 0.5)
        lag = lags[kc] + frac
        # T(tau) = a R(tau + t*)  ->  correlation peaks at k = -t*/dtau
        tstar = -lag * dtau
        amp = np.abs(c[k, cols]) / rr
        return tstar, amp

    tp_raw, ap = analyse(Sp)
    tm_raw, am = analyse(Sm)
    offset = tp_raw[x0]          # ~0 by construction (autocorrelation of the reference)
    tp_raw = tp_raw - offset
    tm_raw = tm_raw - offset

    def fit(traw, a):
        """Weighted polynomial fit with iterative outlier rejection (edge pixels whose
        EO trace is truncated by the scan range give biased lags)."""
        good = a > min_amp * np.nanmax(a)
        x = np.arange(L)
        if good.sum() <= smooth_order + 1:
            return traw.copy(), good
        for _ in range(5):
            coef = np.polyfit(x[good], traw[good], smooth_order, w=a[good])
            resid = traw - np.polyval(coef, x)
            mad = 1.4826 * np.median(np.abs(resid[good] - np.median(resid[good])))
            new_good = good & (np.abs(resid) <= max(3 * mad, 0.5 * abs(dtau)))
            if new_good.sum() <= smooth_order + 1 or np.array_equal(new_good, good):
                break
            good = new_good
        coef = np.polyfit(x[good], traw[good], smooth_order, w=a[good])
        return np.polyval(coef, x), good

    tp, gp = fit(tp_raw, ap)
    tm, gm = fit(tm_raw, am)
    ap = np.where(gp, ap, np.nan)
    am = np.where(gm, am, np.nan)
    return dict(tp=tp, tm=tm, ap=ap, am=am, tp_raw=tp_raw, tm_raw=tm_raw, x0=int(x0), taus_fs=taus)


def pixel_shift(ref: np.ndarray, sig: np.ndarray) -> float:
    """Sub-pixel shift s such that sig(x) ~ ref(x - s) (positive = moved to higher pixels)."""
    a = np.asarray(ref, float) - np.mean(ref)
    b = np.asarray(sig, float) - np.mean(sig)
    n = a.size
    nfft = int(2 ** np.ceil(np.log2(2 * n)))
    c = np.fft.irfft(np.fft.rfft(b, nfft) * np.conj(np.fft.rfft(a, nfft)), nfft)
    c = np.concatenate([c[nfft - (n - 1):], c[:n]])
    lags = np.arange(-(n - 1), n)
    k = int(np.argmax(c))
    if 0 < k < c.size - 1:
        y0, y1, y2 = c[k - 1], c[k], c[k + 1]
        den = y0 - 2 * y1 + y2
        frac = 0.5 * (y0 - y2) / den if den != 0 else 0.0
    else:
        frac = 0.0
    return float(lags[k] + frac)


def fit_fs_per_px(delays_fs: np.ndarray, shifts_px: np.ndarray) -> float:
    """Linear calibration from a stage scan.

    Pixel x samples the THz field at time t(x) + tau, so a feature moves by
    dx = -dtau / (dt/dx): fs_per_px = -dtau/dx.
    """
    slope = np.polyfit(np.asarray(shifts_px, float), np.asarray(delays_fs, float), 1)[0]
    return float(-slope)


# ----------------------------------------------------------------------------- spectra
def spectrum(t_ps: np.ndarray, s: np.ndarray, window: str = "hann", pad: int = 4):
    """Amplitude spectrum |FFT| on a uniform time axis. Returns (f_THz, amplitude)."""
    t_ps = np.asarray(t_ps, float)
    s = np.asarray(s, float)
    if s.size < 4:
        return np.zeros(1), np.zeros(1)
    dt = float(np.median(np.diff(t_ps)))
    s = s - np.mean(s[: max(2, s.size // 20)])  # remove offset using the leading baseline
    if window == "hann":
        w = np.hanning(s.size)
    elif window == "tukey":
        w = _tukey(s.size, 0.25)
    else:
        w = np.ones(s.size)
    nfft = int(2 ** np.ceil(np.log2(s.size * max(1, pad))))
    amp = np.abs(np.fft.rfft(s * w, nfft)) * abs(dt)
    f = np.fft.rfftfreq(nfft, abs(dt))
    return f, amp


def _tukey(n: int, alpha: float) -> np.ndarray:
    w = np.ones(n)
    m = int(alpha * (n - 1) / 2)
    if m > 0:
        r = 0.5 * (1 - np.cos(np.pi * np.arange(m) / m))
        w[:m] = r
        w[-m:] = r[::-1]
    return w


def fs_to_mm(delay_fs: float, passes: int = 2, direction: int = 1) -> float:
    """Stage displacement (mm) that changes the optical delay by delay_fs."""
    return direction * delay_fs * C_MM_PER_FS / max(1, passes)


def mm_to_fs(mm: float, passes: int = 2, direction: int = 1) -> float:
    return direction * mm * max(1, passes) / C_MM_PER_FS
