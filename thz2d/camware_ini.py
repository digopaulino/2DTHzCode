"""Read camera presets from .ini files saved by PCO Camware.

The exact key names Camware writes are not documented publicly, so this reader
is deliberately tolerant: it scans every section and key, matches the
parameters we need by name (case-insensitive), understands the pco.sdk codes
(time base 0/1/2 = ns/µs/ms, trigger mode 0..7, timestamp mode 0..3) as well
as plain text values, and returns a report of what it used and what it
ignored so you can check the mapping.

    updates, report = read_camware_ini("edge42_1kHz.ini")
    store.update("camera", **updates)
"""

from __future__ import annotations

import configparser
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

TRIGGER_CODES = {0: "auto sequence", 1: "software trigger", 2: "external exposure start & software trigger",
                 3: "external exposure control", 4: "external synchronized", 5: "fast external exposure control",
                 6: "external CDS control", 7: "slow external exposure control"}
TIMESTAMP_CODES = {0: "off", 1: "binary", 2: "binary & ascii", 3: "ascii"}
TIMEBASE_TO_US = {0: 1e-3, 1: 1.0, 2: 1e3, "ns": 1e-3, "us": 1.0, "µs": 1.0, "ms": 1e3, "s": 1e6}

_NUM = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _flatten(path: Path) -> List[Tuple[str, str, str]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not re.search(r"^\s*\[.+\]\s*$", text, re.M):
        text = "[root]\n" + text
    cp = configparser.ConfigParser(strict=False, interpolation=None, delimiters=("=", ":"),
                                   comment_prefixes=(";", "#", "//"), inline_comment_prefixes=(";",))
    cp.optionxform = str
    cp.read_string(text)
    out = []
    for sec in cp.sections():
        for k, v in cp.items(sec):
            out.append((sec, k.strip(), v.strip().strip('"')))
    return out


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _num(v: str) -> Optional[float]:
    m = _NUM.search(v.replace(",", "."))
    return float(m.group()) if m else None


def _unit(v: str) -> Optional[float]:
    """Time unit written after the number ('10 ms') -> factor to µs."""
    m = re.search(r"\d\s*(ns|us|µs|ms|s)\b", v.lower())
    return TIMEBASE_TO_US[m.group(1)] if m else None


def _find(items, include, exclude=()) -> Optional[Tuple[str, str, str]]:
    for sec, k, v in items:
        n = _norm(k)
        if all(re.search(p, n) for p in include) and not any(re.search(p, n) for p in exclude):
            return sec, k, v
    return None


def read_camware_ini(path) -> Tuple[Dict[str, object], List[str]]:
    """Return (CameraSettings updates, human-readable report lines)."""
    path = Path(path)
    items = _flatten(path)
    upd: Dict[str, object] = {}
    rep: List[str] = [f"file: {path.name}  ({len(items)} entries)"]
    used = set()

    def mark(hit):
        used.add((hit[0], hit[1]))
        return f"[{hit[0]}] {hit[1]} = {hit[2]}"

    # --- exposure / delay (value + optional time base) -----------------------------
    for field, word, stem in (("exposure_us", r"expos|exptime|^exp(time|value)?$", r"exp"),
                              ("delay_us", r"delay", r"del")):
        hit = _find(items, [word], [r"base", r"unit", r"min", r"max", r"auto", r"mode", r"step", r"enable",
                                    r"preset", r"table", r"trig"])
        if not hit:
            continue
        val = _num(hit[2])
        if val is None:
            continue
        factor, how = _unit(hit[2]), "unit in value"
        if factor is None:
            base = _find(items, [stem, r"(timebase|base|unit)"])
            if base is not None:
                used.add((base[0], base[1]))
                b = base[2].strip().lower()
                code = int(_num(b)) if _num(b) is not None and b.replace(".", "").isdigit() else b
                factor = TIMEBASE_TO_US.get(code)
                how = f"time base {mark(base)}"
        if factor is None:
            factor, how = 1.0, "no time base found - ASSUMED µs"
        upd[field] = val * factor
        rep.append(f"{field:12s} = {upd[field]:g} µs   <- {mark(hit)}  ({how})")

    # --- ROI ------------------------------------------------------------------------
    roi = {}
    for c in ("x0", "y0", "x1", "y1"):
        hit = _find(items, [rf"(roi)?{c}$"]) or _find(items, [r"roi", c])
        if hit and _num(hit[2]) is not None:
            roi[c] = (int(_num(hit[2])), hit)
    if len(roi) == 4:
        upd["roi"] = tuple(roi[c][0] for c in ("x0", "y0", "x1", "y1"))
        rep.append(f"{'roi':12s} = {upd['roi']}   <- " + ", ".join(mark(roi[c][1]) for c in roi))
    else:
        hit = _find(items, [r"^roi$"]) or _find(items, [r"roi"], [r"preset", r"enable", r"mode"])
        nums = [int(float(x)) for x in _NUM.findall(hit[2])] if hit else []
        if hit and len(nums) == 4:
            upd["roi"] = tuple(nums)
            rep.append(f"{'roi':12s} = {upd['roi']}   <- {mark(hit)}")

    # --- trigger --------------------------------------------------------------------
    hit = _find(items, [r"trig"], [r"edge", r"polar", r"delay", r"count", r"source"])
    if hit:
        v = hit[2].strip()
        trig = None
        if _num(v) is not None and v.lstrip("+-").isdigit():
            trig = TRIGGER_CODES.get(int(v))
        else:
            vl = v.lower()
            for name in TRIGGER_CODES.values():
                if name in vl or _norm(name) == _norm(vl):
                    trig = name
            if trig is None and "extern" in vl and "start" in vl:
                trig = TRIGGER_CODES[2]
            elif trig is None and "auto" in vl:
                trig = TRIGGER_CODES[0]
        if trig:
            upd["trigger"] = trig
            rep.append(f"{'trigger':12s} = {trig!r}   <- {mark(hit)}")
        else:
            rep.append(f"trigger: could not interpret {mark(hit)}")

    # --- pixel rate -------------------------------------------------------------------
    hit = _find(items, [r"pixel(rate|clock)|readout(freq|rate)|pixelfreq"])
    if hit and _num(hit[2]) is not None:
        val = _num(hit[2])
        if val < 1e4:              # given in MHz
            val *= 1e6
        upd["pixel_rate_hz"] = int(round(val))
        rep.append(f"{'pixel rate':12s} = {upd['pixel_rate_hz']} Hz   <- {mark(hit)}")

    # --- timestamp ----------------------------------------------------------------------
    hit = _find(items, [r"timestamp"])
    if hit:
        v = hit[2].strip().lower()
        ts = TIMESTAMP_CODES.get(int(v)) if v.isdigit() else next(
            (n for n in ("binary & ascii", "binary", "ascii", "off") if n.replace(" & ", "") in v.replace(" ", "")
             or n in v), None)
        if ts:
            # binary time stamp is needed for dropped-frame detection: keep it on
            if ts in ("off", "ascii"):
                rep.append(f"timestamp: preset says {ts!r} -> using 'binary' (needed for dropped-frame detection)")
                ts = "binary"
            upd["timestamp"] = ts
            rep.append(f"{'timestamp':12s} = {ts!r}   <- {mark(hit)}")

    # --- things we read but do not apply ------------------------------------------------
    hit = _find(items, [r"binning|bin(h|v|x|y)"])
    if hit:
        mark(hit)
        rep.append(f"note: binning in the preset ({hit[1]} = {hit[2]}) is NOT applied - use binning 1")

    missing = [f for f in ("exposure_us", "roi", "trigger") if f not in upd]
    if missing:
        rep.append("NOT FOUND in the file: " + ", ".join(missing) + " (current values are kept)")
    ignored = [f"[{s}] {k} = {v}" for s, k, v in items if (s, k) not in used]
    if ignored:
        rep.append(f"ignored {len(ignored)} other entries, e.g.: " + "; ".join(ignored[:12]))
    return upd, rep


def list_presets(folder) -> List[Path]:
    folder = Path(folder).expanduser()
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.ini"))
