# First lab test — checklist

Plan for about 2–3 hours. Run the steps in order: each one only relies on what the earlier
steps have already shown to work. The `diagnose` commands write a `diagnose_*.txt` file. If
anything fails, keep that file and the text of the GUI's **Log** box.

Leave the LabVIEW program installed and closed. Only one program at a time can open the
camera, and the same goes for the ESP301 COM port.

---

## 0. Install (once, ~10 min)

```bat
cd <folder with this file>
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m pytest -q
```
☐ The tests end with `25 passed`.
☐ `.venv\Scripts\python -m thz2d --sim` opens the GUI and *Run* shows traces. Close it again.

## 1. Camera on its own (laser not needed)

Close Camware, then:
```bat
.venv\Scripts\python -m thz2d.diagnose camera --trigger auto --frames 2000
```
☐ `camera opened`. If not: pass `--interface "USB 3.0"` or `--interface CLHS`, and check
  that Camware is closed.
☐ `description['roi is vert symmetric']` is listed. The program centres the ROI
  automatically.
☐ In **both** `fifo` and `sequence` mode, every line reads `[ OK ]`. In particular:
  - *no frame returned twice*
  - *consecutive image counters*
  - *consecutive frames differ*

  If only fifo fails: in the GUI, set **Record mode = sequence** (Setup tab) and carry on.
  Make a note of this.

## 2. Camera triggered by the laser

The trigger cable goes from the laser timing output to the camera's trigger input
(as for LabVIEW). The probe beam should be on the camera.
```bat
.venv\Scripts\python -m thz2d.diagnose camera --trigger external --frames 4000 --exposure 100 --save frames.npy
```
☐ The frame rate is **≈ 1000 frames/s**, with 0 gaps and no FIFO overflow.
☐ If it stays at 0 frames: check the cable and the trigger edge polarity (compare with
  Camware's settings).
☐ If there are gaps or an overflow: try `--rows 32` and/or `--fifo 1000`, and note which
  helps.
☐ `max` is well below 65000 (not saturated) but clearly above the dark level. If not,
  change `--exposure`.

## 3. Delay stage

```bat
.venv\Scripts\python -m thz2d.diagnose ports
.venv\Scripts\python -m thz2d.diagnose stage --port COMx
```
☐ `connected: <firmware version>`, and both axes report a position.
  - If it fails: USB port → baud 921600; RS-232 port → add `--baud 19200`.
  - If it says *units code ≠ 2*: set the axis to mm in the ESP utility.

Make sure there is room for a 10 µm move, then:
```bat
.venv\Scripts\python -m thz2d.diagnose stage --port COMx --move --axis 1
.venv\Scripts\python -m thz2d.diagnose stage --port COMx --move --axis 2
```
☐ Each axis moves +0.010 mm and returns.

## 4. GUI with the real hardware

`run_thz2d.bat` (or `.venv\Scripts\python -m thz2d`).

1. **Setup tab → Camera:**
   - Backend `pco`, Interface as in step 1.
   - Rows = 64 → *Centre ROI vertically*.
   - Exposure as found in step 2.
   - Trigger `external exposure start…`, Record mode `fifo` (or `sequence` if step 1 said so).
   - *Connect*.

   ☐ The live feed at the top shows the two beam-array stripes.
2. **Stage panel:**
   - Controller `esp301`, Port `COMx`, *Connect*.

   ☐ The position matches the ESP301 front panel.
   - Try *Go to* (a nearby position), then −τ / +τ with 100 fs.

   ☐ Each 100 fs step moves the stage 14.99 µm (with *Optical passes* = 2). If your delay
   line folds the beam differently, change *Optical passes*.
3. **N = 25 → Single:**
   - Then *Auto-find stripes*. Or drag the windows on the live image / the shaded
     regions.

   ☐ The two profile plots each show the full beam array, with the REF trace (dashed)
   underneath.
4. **Window alignment:** pick shot type **A**.

   ☐ In "Inside the windows", the yellow and purple traces lie on top of each other.
   If one is shifted, move one window's start x. If one is reversed, tick *mirror the
   I(−) window*.
5. **Dark image:**
   - Block the probe beam (lasers still triggering) → *Record dark* → unblock.

   ☐ The label shows a mean of ≈ 100 counts (the camera offset).

## 5. Is REF / A / B / AB assigned correctly?  ← the most important physics check

Use the **Channels** tab with *Run* (N = 25):

| condition | expected |
|---|---|
| both pumps on | A, B and AB show pulses, NL is small. The side panel shows the cycle order with confidence > 2. |
| block pump **A** | the A trace is flat, AB ≈ B |
| block pump **B** | the B trace is flat, AB ≈ A |
| block both | ⚠ low-contrast warning (expected: there is no THz to find REF from) |

☐ If A and B come out swapped: untick *A chopped at laser/2 (500 Hz)*.
☐ If the order looks wrong or keeps changing with both pumps on, write down what you see.
  Then switch *Shot order* to **manual** and try REF positions 0–3 until the table above
  holds. Note the position and whether *swap A-only / AB* was needed.

## 6. Compare with LabVIEW (same alignment)

1. Close this program and take a normal measurement with LabVIEW. Save it.
2. Close LabVIEW. Run N = 500 here and *Save current…*.

☐ Compare the A waveform shape and amplitude. A flipped overall sign or a scale factor is
  fine (different conventions), but note it.

## 7. Time axis

On the **Setup tab**, choose the motor that delays the probe relative to the THz.
- *Measure fs/px*, with step 500 fs × 6 and channel A.

☐ The result is roughly the echelon step delay divided by the pixels per step (paper:
  ~40 fs per step). The fit points lie on a line.
☐ *t = 0 at peak of A*. Then +τ by 1000 fs in the stage panel: the pulse in the Channels
  tab moves by ≈ 1 ps.

Optional (≈ 20 min, uses the time estimate shown):
- Run the **per-pixel calibration scan**, ±12 ps in 40 fs steps, N = 25.

☐ The t*(x) curves are smooth and close together. The mode switches to per-pixel, and the
  Channels traces stay clean.

## 8. Long run

Run N = 500 continuously for ~10 min with *autosave* on.

☐ *dropped 0* in the side panel. The fps stays at ≈ 1000, the GUI stays responsive, and
  memory use in Task Manager stays flat.
☐ While it runs, step the stage: the move happens between blocks.
☐ Press *Stop* during a block: it stops within a second.
☐ Start a long *Go to* and press **STOP**: the stage stops.

---

## What to bring back

* All the `diagnose_*.txt` files.
* `frames.npy` from step 2.
* The text of the Log box. Copy it if there were errors.
* Saved `.npz` files from step 5 (both pumps on, A blocked, B blocked) and step 6, plus the
  LabVIEW file from step 6.
* Notes on anything you changed: record mode, *A chopped at laser/2*, manual REF position,
  mirror, optical passes, baud.
* A screenshot of each tab.
