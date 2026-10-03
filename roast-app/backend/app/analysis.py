"""NumPy analysis pipeline: rate-of-rise, gap handling, phase metrics.

All temperature transformations live here and are *pure functions of the raw
samples*.  Nothing in this module writes back to the database: smoothing and
interpolation parameters only change derived outputs, never measured data.

Rate-of-rise (RoR) definition
-----------------------------
Sampling is uneven, so a naive ``(T[i+1]-T[i]) / dt`` is meaningless.
Instead, at every observed time ``t`` we fit an ordinary (time-weighted)
least-squares line to the measured bean-temperature points inside a
**centred time window** ``[t - window_s/2, t + window_s/2]`` and take its
slope, converted to °C/min:

    slope = sum w (x-x̄)(y-ȳ) / sum w (x-x̄)²      with x in seconds, w=1

The window is reported alongside every output so consumers always know the
basis of the number.  Points inside a probe-dropout gap are *not* used: only
measured samples count, and a slope is only emitted when at least
``min_points`` measured points spanning at least ``min_span_s`` lie in the
window.  At the series edges the window is truncated (still centred as far as
the data allows); ``edge`` flags those points.

Interpolation
-------------
Missing bean temperatures (probe loss) are bridged with a **linear
interpolation between measured endpoints, marked ``is_interpolated=True``**.
Gaps wider than ``max_gap_fill_s`` are deliberately NOT bridged — the chart
shows a break instead of inventing data.  Interpolated points are never fed
to the RoR regression as if measured.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

EVENT_TIME_KEYS = (
    "charge",
    "turning_point",
    "first_crack_start",
    "first_crack_end",
    "drop",
)


@dataclass(frozen=True)
class RoRConfig:
    window_s: float = 30.0
    min_points: int = 4
    min_span_s: float = 10.0
    # Extra smoothing applied to the *displayed* RoR trace only.
    display_smooth_s: float = 0.0


# ---------------------------------------------------------------------------
# Calibration ledger (pure functions — the ledger rows live in the DB, the
# correction itself is derived state computed here, never written back).
# ---------------------------------------------------------------------------

def normalize_calibration(c: dict) -> dict:
    """Project a calibration record (ORM row or export-payload dict) to the
    canonical dict the pipeline understands."""
    params = c.get("params") or {}
    return {
        "id": c.get("id"),
        "channel": c["channel"],
        "t_start_s": float(c["t_start_s"]),
        "t_end_s": float(c["t_end_s"]),
        "formula": c.get("formula", "affine"),
        "scale": float(c.get("scale", params.get("scale", 1.0))),
        "offset_c": float(c.get("offset_c", params.get("offset_c", 0.0))),
        "version": c.get("version"),
        "status": c.get("status", "active"),
        "created_by": c.get("created_by", ""),
    }


def find_calibration_conflicts(calibrations: list[dict]) -> list[dict]:
    """Overlapping *active* ranges on the same channel are a visible conflict.

    The system never silently picks one of two overlapping calibrations: the
    caller is expected to surface these and refuse derived output until an
    operator adjudicates (withdraw / supersede).
    """
    act = [normalize_calibration(c) for c in calibrations if c.get("status", "active") == "active"]
    conflicts: list[dict] = []
    for i in range(len(act)):
        for j in range(i + 1, len(act)):
            a, b = act[i], act[j]
            if a["channel"] != b["channel"]:
                continue
            lo = max(a["t_start_s"], b["t_start_s"])
            hi = min(a["t_end_s"], b["t_end_s"])
            if lo <= hi:
                conflicts.append(
                    {
                        "channel": a["channel"],
                        "overlap_t_start_s": lo,
                        "overlap_t_end_s": hi,
                        "calibrations": [
                            {"id": a["id"], "version": a["version"],
                             "t_start_s": a["t_start_s"], "t_end_s": a["t_end_s"]},
                            {"id": b["id"], "version": b["version"],
                             "t_start_s": b["t_start_s"], "t_end_s": b["t_end_s"]},
                        ],
                    }
                )
    return conflicts


def apply_calibrations(
    t: np.ndarray,
    temp: np.ndarray,
    calibrations: list[dict],
    channel: str,
) -> np.ndarray:
    """Corrected copy of ``temp``; measured points only, NaN stays NaN.

    A missing reading has no value to correct — it remains missing, so a
    dropout can never be disguised as a measurement by calibration.
    """
    out = temp.astype(float).copy()
    measured = ~np.isnan(temp)
    for c in calibrations:
        c = normalize_calibration(c)
        if c["channel"] != channel or c.get("status", "active") != "active":
            continue
        if c["formula"] != "affine":
            raise ValueError(f"unsupported calibration formula: {c['formula']}")
        m = measured & (t >= c["t_start_s"]) & (t <= c["t_end_s"])
        out[m] = c["scale"] * temp[m] + c["offset_c"]
    return out


def _linear_fill(
    t: np.ndarray,
    temp: np.ndarray,
    max_gap_fill_s: float,
    channel: str = "bean",
) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """Return (filled_temp, is_interpolated_mask, gap_records).

    Only internal gaps no wider than ``max_gap_fill_s`` are filled.  Edge
    missing values and oversized gaps stay NaN.
    """
    filled = temp.astype(float).copy()
    is_interp = np.zeros_like(t, dtype=bool)
    measured = ~np.isnan(temp)
    gaps: list[dict] = []

    idx = np.arange(len(t))
    miss_idx = idx[~measured]
    if len(miss_idx) == 0:
        return filled, is_interp, gaps

    # Group consecutive missing indices into runs.
    starts: list[int] = []
    ends: list[int] = []
    run_start = int(miss_idx[0])
    prev = run_start
    for j in miss_idx[1:]:
        j = int(j)
        if j != prev + 1:
            starts.append(run_start)
            ends.append(prev)
            run_start = j
        prev = j
    starts.append(run_start)
    ends.append(prev)

    for a, b in zip(starts, ends):
        # Width is measured between the measured neighbours — what an
        # interpolation would actually span.
        gap_width = float(t[b + 1] - t[a - 1]) if 0 < a and b < len(t) - 1 else None
        if a == 0 or b == len(t) - 1:
            kind = "edge_unfilled"
        elif gap_width > max_gap_fill_s:
            kind = "wide_unfilled"
        else:
            kind = "interpolated"
            t0, t1 = t[a - 1], t[b + 1]
            y0, y1 = temp[a - 1], temp[b + 1]
            seg_t = t[a : b + 1]
            filled[a : b + 1] = y0 + (y1 - y0) * (seg_t - t0) / (t1 - t0)
            is_interp[a : b + 1] = True
        gaps.append(
            {
                "channel": channel,
                "t_start_s": float(t[a]),
                "t_end_s": float(t[b]),
                "n_missing": int(b - a + 1),
                "span_s": float(t[b] - t[a]),
                "neighbour_span_s": gap_width,
                "status": kind,
            }
        )
    return filled, is_interp, gaps


def rate_of_rise(
    t: np.ndarray,
    temp: np.ndarray,
    cfg: RoRConfig,
) -> dict:
    """Centred moving-window least-squares RoR over measured points only.

    Returns raw per-point RoR (°C/min) plus an optional trailing-mean display
    trace.  Both share the same regression window; the display trace adds an
    independent smoothing pass whose parameter is surfaced in metadata.
    """
    t = np.asarray(t, dtype=float)
    temp = np.asarray(temp, dtype=float)
    n = len(t)
    half = cfg.window_s / 2.0
    ror = np.full(n, np.nan)
    n_used = np.zeros(n, dtype=int)
    spans = np.full(n, np.nan)
    edge = np.zeros(n, dtype=bool)

    measured = ~np.isnan(temp)
    t_m = t[measured]
    y_m = temp[measured]

    for i in range(n):
        if not measured[i]:
            continue
        lo = t[i] - half
        hi = t[i] + half
        inside = (t_m >= lo) & (t_m <= hi)
        xs = t_m[inside]
        ys = y_m[inside]
        span = xs.max() - xs.min() if len(xs) >= 2 else 0.0
        n_used[i] = len(xs)
        spans[i] = span
        if t[i] - half < t_m.min() - 1e-9 or t[i] + half > t_m.max() + 1e-9:
            edge[i] = True
        if len(xs) >= cfg.min_points and span >= cfg.min_span_s:
            x = xs - xs.mean()
            denom = float((x * x).sum())
            if denom > 0:
                ror[i] = 60.0 * float((x * (ys - ys.mean())).sum()) / denom

    display = ror.copy()
    if cfg.display_smooth_s > 0:
        display = _time_weighted_smooth(t, ror, cfg.display_smooth_s, measured)

    return {
        "ror_raw": ror,
        "ror_display": display,
        "n_points_used": n_used,
        "window_span_s": spans,
        "edge": edge,
        "window_s": cfg.window_s,
        "display_smooth_s": cfg.display_smooth_s,
        "min_points": cfg.min_points,
        "min_span_s": cfg.min_span_s,
    }


def _time_weighted_smooth(
    t: np.ndarray, ror: np.ndarray, smooth_s: float, measured: np.ndarray
) -> np.ndarray:
    """Centred trailing-mean of the RoR over ``smooth_s`` (display only).

    NaN RoR points (not enough data in window) are skipped rather than
    propagating.
    """
    out = np.full_like(ror, np.nan)
    half = smooth_s / 2.0
    valid = ~np.isnan(ror)
    for i in range(len(t)):
        if not measured[i]:
            continue
        m = valid & (np.abs(t - t[i]) <= half)
        if m.any():
            out[i] = float(np.mean(ror[m]))
    return out


def build_series(
    samples: list[dict],
    *,
    ror_cfg: RoRConfig,
    max_gap_fill_s: float,
    calibrations: list[dict] | None = None,
) -> dict:
    """Assemble the full derived series for one batch from raw sample dicts.

    ``samples`` items need keys ``t_s``, ``bean_temp_c``, ``env_temp_c``.
    Measured temperatures stay untouched; all derived arrays are separate.

    ``calibrations`` (active ledger entries) produce a *parallel* corrected
    basis: raw values remain in every point, corrected values are added next
    to them, and RoR / guide lines are computed on the corrected basis while
    the raw-basis RoR and guides are kept for side-by-side audit.  Missing
    readings stay missing on both bases.
    """
    samples = sorted(samples, key=lambda s: s["t_s"])
    t = np.array([s["t_s"] for s in samples], dtype=float)
    bean = np.array(
        [np.nan if s["bean_temp_c"] is None else s["bean_temp_c"] for s in samples],
        dtype=float,
    )
    env = np.array(
        [np.nan if s["env_temp_c"] is None else s["env_temp_c"] for s in samples],
        dtype=float,
    )

    cals = [normalize_calibration(c) for c in (calibrations or [])]
    active = [c for c in cals if c["status"] == "active"]
    bean_c = apply_calibrations(t, bean, active, "bean")
    env_c = apply_calibrations(t, env, active, "env")

    bean_filled, bean_interp, bean_gaps = _linear_fill(
        t, bean_c, max_gap_fill_s, channel="bean"
    )
    env_filled, env_interp, env_gaps = _linear_fill(
        t, env_c, max_gap_fill_s, channel="env"
    )
    bean_filled_raw, _, _ = _linear_fill(t, bean, max_gap_fill_s, channel="bean")
    env_filled_raw, _, _ = _linear_fill(t, env, max_gap_fill_s, channel="env")

    ror = rate_of_rise(t, bean_c, ror_cfg)
    ror_raw = rate_of_rise(t, bean, ror_cfg)

    raw_points = [
        {
            "t_s": float(t[i]),
            # RAW measured values — immutable, exactly as stored.
            "bean_temp_c": None if np.isnan(bean[i]) else float(bean[i]),
            "env_temp_c": None if np.isnan(env[i]) else float(env[i]),
            # CORRECTED values (== raw where no active calibration covers t).
            "bean_temp_corrected_c": None if np.isnan(bean_c[i]) else float(bean_c[i]),
            "env_temp_corrected_c": None if np.isnan(env_c[i]) else float(env_c[i]),
            # RoR on the corrected basis is the current analytical view...
            "ror_c_per_min": None if np.isnan(ror["ror_raw"][i]) else float(ror["ror_raw"][i]),
            "ror_display": None if np.isnan(ror["ror_display"][i]) else float(ror["ror_display"][i]),
            # ...while the raw-basis RoR stays available for comparison.
            "ror_raw_basis_c_per_min": None if np.isnan(ror_raw["ror_raw"][i]) else float(ror_raw["ror_raw"][i]),
            "ror_display_raw_basis": None if np.isnan(ror_raw["ror_display"][i]) else float(ror_raw["ror_display"][i]),
            "ror_n_points": int(ror["n_points_used"][i]),
            "ror_edge": bool(ror["edge"][i]),
            "is_interpolated": bool(bean_interp[i]),
        }
        for i in range(len(t))
    ]

    applied = [
        {
            "id": c["id"],
            "channel": c["channel"],
            "version": c["version"],
            "t_start_s": c["t_start_s"],
            "t_end_s": c["t_end_s"],
            "formula": c["formula"],
            "scale": c["scale"],
            "offset_c": c["offset_c"],
            "created_by": c["created_by"],
        }
        for c in active
    ]

    return {
        "raw_points": raw_points,
        # Continuous guide line (measured + flagged interpolated), NaN across
        # unfilled gaps so the chart renders a break.  Corrected basis.
        "guide_bean_temp": [None if np.isnan(v) else float(v) for v in bean_filled],
        "guide_env_temp": [None if np.isnan(v) else float(v) for v in env_filled],
        # Same guides on the raw basis, for the side-by-side audit view.
        "guide_bean_temp_raw": [None if np.isnan(v) else float(v) for v in bean_filled_raw],
        "guide_env_temp_raw": [None if np.isnan(v) else float(v) for v in env_filled_raw],
        "interpolated_t_s": [float(x) for x in t[bean_interp | env_interp]],
        "missing_segments": bean_gaps + env_gaps,
        "ror_window": {
            "method": "centred_least_squares_slope_on_measured_points",
            "window_s": ror["window_s"],
            "display_smooth_s": ror["display_smooth_s"],
            "min_points": ror["min_points"],
            "min_span_s": ror["min_span_s"],
            "units": "C/min",
            "basis": "corrected" if applied else "raw",
        },
        "calibration": {
            "basis": "corrected" if applied else "raw",
            "applied": applied,
            "raw_is_immutable": True,
        },
    }


def current_events(events: list[dict]) -> dict[str, dict]:
    """Map event_type -> the latest non-superseded event row (as dict)."""
    out: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: (e["t_s"], e["id"])):
        if not e["superseded"]:
            out[e["event_type"]] = e
    return out


def anchor_temperatures(
    events: list[dict],
    points: list[dict],
    tol_s: float = 10.0,
) -> dict[str, dict | None]:
    """Bean temperature at each phase anchor, on BOTH bases.

    Durations never depend on calibration, but the temperature read at an
    anchor does — so every anchor reports the raw and the corrected value
    (nearest measured sample within ``tol_s``; interpolated/missing points
    are never used as a temperature source).
    """
    cur = current_events(events)
    out: dict[str, dict | None] = {}
    for kind in EVENT_TIME_KEYS:
        e = cur.get(kind)
        if e is None:
            out[kind] = None
            continue
        best = None
        for p in points:
            if p.get("bean_temp_c") is None:
                continue
            d = abs(p["t_s"] - e["t_s"])
            if d <= tol_s and (best is None or d < best[0]):
                best = (d, p)
        if best is None:
            out[kind] = None
            continue
        p = best[1]
        out[kind] = {
            "sample_t_s": float(p["t_s"]),
            "bean_temp_raw_c": float(p["bean_temp_c"]),
            "bean_temp_corrected_c": float(
                p.get("bean_temp_corrected_c", p["bean_temp_c"])
            ),
        }
    return out


def phase_metrics(events: list[dict], points: list[dict] | None = None) -> dict:
    """Development-time ratio etc., computed over explicit event intervals.

    Intervals (all anchored on operator-visible, source-labelled events):
      drying:      charge -> turning point
      maillard:    turning point -> first crack start
      development: first crack start -> drop
      total:       charge -> drop
    development_ratio = development / total.

    Returns ``None`` fields (never a guessed value) when a boundary event is
    missing, plus the exact events used so the computation is auditable.
    When ``points`` (series raw_points) are given, anchor temperatures are
    reported on both the raw and the corrected basis.
    """
    cur = current_events(events)

    def pt(kind: str):
        e = cur.get(kind)
        return None if e is None else {"t_s": float(e["t_s"]), "source": e["source"]}

    charge = pt("charge")
    tp = pt("turning_point")
    fc = pt("first_crack_start")
    fce = pt("first_crack_end")
    drop = pt("drop")

    def span(a, b):
        if a is None or b is None:
            return None
        return round(b["t_s"] - a["t_s"], 3)

    drying = span(charge, tp)
    maillard = span(tp, fc)
    development = span(fc, drop)
    total = span(charge, drop)
    crack_window = span(fc, fce)

    ratio = None
    if development is not None and total not in (None, 0):
        ratio = round(development / total, 4)

    return {
        "drying_s": drying,
        "maillard_s": maillard,
        "development_s": development,
        "first_crack_window_s": crack_window,
        "total_s": total,
        "development_ratio": ratio,
        "anchor_temps": anchor_temperatures(events, points) if points is not None else None,
        "temperature_basis": "raw_and_corrected" if points is not None else None,
        "interval_definition": {
            "drying": "charge -> turning_point",
            "maillard": "turning_point -> first_crack_start",
            "development": "first_crack_start -> drop",
            "development_ratio": "development_s / total_s (charge -> drop)",
        },
        "anchors": {
            "charge": charge,
            "turning_point": tp,
            "first_crack_start": fc,
            "first_crack_end": fce,
            "drop": drop,
        },
    }


def detect_turning_point(samples: list[dict], after_s: float = 30.0) -> float | None:
    """Suggest a turning point: first local minimum of measured bean temp.

    Detection is a *suggestion* only; it is stored with source='auto' and the
    operator can supersede it with a manual event.
    """
    pts = sorted(
        (s for s in samples if s["bean_temp_c"] is not None),
        key=lambda s: s["t_s"],
    )
    pts = [s for s in pts if s["t_s"] >= after_s]
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        if a["bean_temp_c"] >= b["bean_temp_c"] < c["bean_temp_c"]:
            return float(b["t_s"])
    return None
