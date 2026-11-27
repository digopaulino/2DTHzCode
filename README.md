# thz2d — single-shot 2D THz acquisition (replaces the LabVIEW VI)

Python/Qt program for echelon-based single-shot 2D THz spectroscopy with a
**PCO.edge 4.2** camera and a **Newport ESP301** delay-stage controller.
Processing follows Gao *et al.*, Opt. Lett. **47**, 3479 (2022) and its
Supplementary Note 1.

```
python -m thz2d          # real hardware (settings remembered in ~/.thz2d/settings.json)
python -m thz2d --sim    # simulated camera + stage, for trying it out on any computer
```

## Install on the lab PC (Windows)

1. Install Python ≥ 3.10 (64-bit) from python.org.
2. Install PCO's camera drivers/runtime for your interface (USB 3.x or CLHS / Camera Link
   frame grabber) — the same install Camware uses. Close Camware before starting this
   program: only one program can open the camera.
3. In this folder:
   ```bat
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
   `pco` is Excelitas PCO's official Python package. It bundles `pco.sdk` and
   `pco.recorder`, so no separate SDK install is needed.
4. Double-click `run_thz2d.bat`, or run `.venv\Scripts\python -m thz2d`.
5. Tests (no hardware needed): `.venv\Scripts\python -m pytest -q`
6. First time on the real hardware: follow [LAB_TEST.md](LAB_TEST.md). It starts with
   `python -m thz2d.diagnose camera|stage|ports`, which checks the camera and the
   ESP301 without the GUI.

## What happens to each laser shot

1. The camera is triggered by the laser (`external exposure start & software trigger`).
   It streams frames through the pco.recorder **FIFO**, so blocks follow each other with no
   re-arming. `sequence` mode, which re-arms for every block, is available as a fallback.
2. Each frame is split into the two polarization windows **I(−)** and **I(+)**. Each window
   is collapsed vertically (summed over its rows) into a 1D trace. If a dark image was
   recorded, it is subtracted first.
3. A block holds **4N frames** = N repetitions of the 4-shot chopper cycle (REF, A, B, AB).
   The program works out which position in the cycle is which (see below).
4. For each cycle *k* and each polarization (shot-to-shot referencing):
   `S±_c = (I±_c − I±_REF) / I±_REF` for c = AB, A, B, and
   `S±_NL = (I±_AB − I±_A − I±_B + I±_REF) / I±_REF`.
5. Balanced signal `S_c = S+_c − S−_c`, averaged over the N cycles. The standard error of
   the mean is kept too and shown as the shaded band.
6. Pixel → time, using either a linear fs/px factor or the per-pixel calibration of the
   supplement (below).

### Finding REF / A / B / AB

The order of the four shots is assumed fixed. The camera can start anywhere in the cycle,
so the cycle position is worked out again for every block:

* **REF** is the cycle position with the smallest THz modulation. The measure is the energy
  of the high-pass filtered balanced trace (I+ − I−)/(I+ + I−). A static polarization
  imbalance is smooth, so the high-pass removes it.
* With A chopped at laser/2 (500 Hz) and B at laser/4 (250 Hz), the fast chopper toggles
  every shot. So **B-only is always two shots after REF**. The other two positions are A
  and AB. They are told apart with the linearity check AB − A − B + REF ≈ 0: the wrong
  assignment leaves a residual of about 2B.
* The side panel shows the detected order and a confidence value (the ratio of the
  lowest to the second-lowest energy). Below about 2× (THz blocked, say) it warns you. You
  can switch **Shot order** to *manual* and set the REF position yourself. If your
  choppers are the other way round, untick *A chopped at laser/2*.

Dropped frames would scramble the cycle. With the binary timestamp on (the default), the
camera's image counter is checked. If there is a gap, or the FIFO overflows, the block is
restarted.

## The window

* **Top — Live feed.** The camera image with the two windows drawn on it. You can drag or
  resize the windows here. When no averaging is running, a few frames are grabbed at the
  chosen refresh rate.
* **Right side (every tab).**
  * Averaging: **N = 25 / N = 500** presets (or any N), *Run* (continuous), *Single*,
    *Stop*, the shot-order setting, *Save current…* and *autosave* (one `.npz` per block).
  * ESP301 stage: pick **Motor 1 or 2**, go to an **absolute position in mm**, or step by
    a **delay in fs** (−τ / +τ). You can also set a Δτ = 0 reference, Motor ON, and STOP.
    The fs → mm conversion uses *optical passes* (2 for a retro-reflector) and *direction*.
    During a continuous run, stage moves are carried out between two blocks.
* **Tab 1 · Setup & calibration.**
  * Camera settings: exposure, trigger delay, ROI, trigger mode, record mode.
  * Window positions and lengths. Each window's full-width profile is shown for the chosen
    shot type, with REF as a dashed white line. Drag the shaded region to move a window,
    or drag an edge to change the shared length.
  * A "shot vs REF" plot inside the windows: when the windows line up, the yellow ΔI+/I+
    and the purple −ΔI−/I− lie on top of each other.
  * Dark image recording.
  * Pixel → time calibration.
* **Tab 2 · Channels.** AB, A, B and NL traces against time, each with a large readout of
  max |ΔI/I|, the time of the peak, an SNR estimate, and a history plot of the peak value
  over recent blocks (handy when aligning).
* **Tab 3 · Analysis.** One channel: time trace on top, FFT amplitude (linear or log) below.
  Large readouts give the time-domain maximum and the spectral maximum with its frequency.
  *Hold trace* overlays a reference trace for comparison.

## Time calibration

* **Linear (fs/px).** Enter the factor by hand, or press *Measure fs/px*. The stage steps
  by known delays, the program measures how far the trace shifts in pixels
  (cross-correlation), and a straight-line fit gives the factor. Its sign carries the
  direction of the time axis.
* **Per-pixel (Gao et al., Supp. Note 1).** *Run per-pixel calibration scan* moves the EO
  delay τ across a range and records S±(x, τ) at every point. For each pixel, t*(x) is the
  lag of the peak cross-correlation with the reference trace S+(x0, τ), and a(x) is the
  normalized correlation amplitude. A weighted polynomial with outlier rejection smooths
  t*(x). Each polarization is then interpolated onto a uniform time axis as S±(x)/a±(x)
  before the two are subtracted.
  * Make the scan range larger than the window's time span plus the pulse length (the
    defaults are ±12 ps in 40 fs steps).
  * The result is saved to `<autosave folder>/calibration/calib_*.npz`. It is reloaded on
    the next start and stays valid until the optics are realigned.

## Camera notes (pco.edge 4.2)

* To trigger at 1 kHz, use a **short ROI** (64–128 rows). The edge reads out from the sensor
  centre, so the ROI must be **vertically symmetric**; use *Centre ROI vertically*.
* With a rolling shutter, the exposure must cover the line-to-line delay across the ROI as
  well as the trigger jitter, while staying below 1 ms minus the readout time. Start
  around 100–300 µs and adjust the trigger delay until all rows see the probe pulse.
* The first 14 pixels of row 0 hold the binary timestamp. They are overwritten before
  processing.
* If frames are dropped (shown in the side panel), reduce the ROI height, or increase the
  FIFO buffer.

## Files

| file | content |
|---|---|
| `thz2d/processing.py` | windows, phase detection, channels, calibrations, FFT — pure numpy, unit-tested |
| `thz2d/hardware/camera.py` | `PcoCamera` (official `pco` package) and a physics-based `SimulatedCamera` |
| `thz2d/hardware/stage.py` | `ESP301` (ASCII over serial: PA/PR/TP/MD?/ST/TB?…) and `SimulatedStage` |
| `thz2d/acquisition.py` | acquisition thread (owns the hardware, job queue, block acquisition, calibration scans) |
| `thz2d/gui/…` | main window, live view, side panel, the three tabs |
| `tests/test_processing.py` | 25 tests incl. phase detection, NL extraction, both calibrations |

Saved `.npz` blocks contain, for each channel:

* the time axis, S and its standard error;
* the pixel-domain S+ and S−;
* the mean intensity of each shot type.

They also hold the phase map, the stage positions, and all settings as JSON.
