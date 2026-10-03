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

Calibration
-----------
Reviewed calibration records (see the ledger API) describe a probe's zero /
gain drift as ``corrected = gain * raw + offset`` over a valid time range.
Calibration is applied here as a *derived view only*:

* raw measured values are returned untouched alongside corrected ones;
* a missing reading stays missing in both bases — calibration can never
  fill a dropout or turn an interpolated point into a measurement;
* RoR is recomputed on the corrected measured points, so both bases are
  reported with the same window parameters;
* overlapping calibrations on one channel are rejected upstream (visible
  conflict + explicit adjudication); this module assumes a resolved,
  non-overlapping set.
"""
from __future__ import annotations

import math
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
# calibration (derived corrected view; raw data never modified)
# ---------------------------------------------------------------------------

CHANNELS = ("bean", "env")


def normalize_calibration(rec: dict) -> dict:
    """Validate/coerce one calibration record into the canonical dict shape
    used by the pipeline.  Raises ``ValueError`` on anything malformed."""
    try:
        channel = str(rec["channel"])
        frm = float(rec["valid_from_s"])
        to = float(rec["valid_to_s"])
        formula = str(rec.get("formula", "linear"))
        params = rec.get("params") or {}
        gain = float(params.get("gain", rec.get("gain", 1.0)))
        offset = float(params.get("offset", rec.get("offset", 0.0)))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"calibration record missing/invalid field: {exc}")
    if channel not in CHANNELS:
        raise ValueError(f"channel must be one of {CHANNELS}, got {channel!r}")
    if formula != "linear":
        raise ValueError(f"unsupported formula {formula!r} (only 'linear')")
    if not to > frm:
        raise ValueError("valid_to_s must be greater than valid_from_s")
    if not (math.isfinite(gain) and math.isfinite(offset)):
        raise ValueError("gain/offset must be finite")
    if gain <= 0:
        raise ValueError("gain must be positive")
    return {
        "id": rec.get("id"),
        "channel": channel,
        "valid_from_s": frm,
        "valid_to_s": to,
        "formula": formula,
        "params": {"gain": gain, "offset": offset},
        "version": rec.get("version"),
        "created_by": rec.get("created_by", ""),
        "status": rec.get("status", ""),
    }


def find_calibration_conflicts(calibrations: list[dict]) -> list[dict]:
    """Group calibrations per channel and report overlapping clusters.

    Ranges are inclusive, so records merely touching at one endpoint still
    contest that instant and are reported.  Returns one entry per overlapping
    cluster: the cluster span, the common contested region (when every record
    overlaps every other) and the full candidate records — enough for a UI to
    render the conflict without a second query.
    """
    by_channel: dict[str, list[dict]] = {}
    for c in calibrations:
        by_channel.setdefault(c["channel"], []).append(c)

    conflicts: list[dict] = []
    for channel, rows in by_channel.items():
        rows = sorted(rows, key=lambda c: (c["valid_from_s"], c["valid_to_s"], c.get("id") or 0))
        cluster: list[dict] = []
        cluster_end = -math.inf

        def flush() -> None:
            if len(cluster) < 2:
                return
            common_from = max(c["valid_from_s"] for c in cluster)
            common_to = min(c["valid_to_s"] for c in cluster)
            conflicts.append(
                {
                    "channel": channel,
                    "from_s": float(min(c["valid_from_s"] for c in cluster)),
                    "to_s": float(max(c["valid_to_s"] for c in cluster)),
                    "overlap_from_s": float(common_from) if common_from <= common_to else None,
                    "overlap_to_s": float(common_to) if common_from <= common_to else None,
                    "candidates": [dict(c) for c in cluster],
                }
            )

        for c in rows:
            if cluster and c["valid_from_s"] > cluster_end:
                flush()
                cluster = []
                cluster_end = -math.inf
            cluster.append(c)
            cluster_end = max(cluster_end, c["valid_to_s"])
        flush()
    return conflicts


def apply_calibrations(
    t: np.ndarray,
    temp: np.ndarray,
    calibrations: list[dict],
) -> tuple[np.ndarray, list]:
    """Return ``(corrected, cal_id_per_point)`` for one channel.

    Corrections are computed from the RAW values (never chained) and NaN
    readings stay NaN — a calibration can never manufacture a measurement.
    ``calibrations`` must already be conflict-free for this channel.
    """
    out = temp.astype(float).copy()
    ids: list = [None] * len(t)
    for c in sorted(calibrations, key=lambda c: c.get("id") or 0):
        gain = float(c["params"]["gain"])
        offset = float(c["params"]["offset"])
        m = (t >= c["valid_from_s"]) & (t <= c["valid_to_s"]) & ~np.isnan(temp)
        out[m] = gain * temp[m] + offset
        for i in np.flatnonzero(m):
            ids[i] = c.get("id")
    return out, ids


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

    ``calibrations`` (already adjudicated, non-overlapping per channel) add a
    parallel *corrected* view: ``*_cal_*`` point fields, corrected guide
    lines and a corrected RoR trace.  The raw-basis outputs are identical
    whether or not calibrations are supplied.
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
    bean_cals = [c for c in cals if c["channel"] == "bean"]
    env_cals = [c for c in cals if c["channel"] == "env"]

    bean_filled, bean_interp, bean_gaps = _linear_fill(
        t, bean, max_gap_fill_s, channel="bean"
    )
    env_filled, env_interp, env_gaps = _linear_fill(
        t, env, max_gap_fill_s, channel="env"
    )

    # Corrected view.  NaN pattern is identical to the raw basis, so the
    # interpolation mask and gap records computed above hold for both.
    bean_cal, bean_cal_ids = apply_calibrations(t, bean, bean_cals)
    env_cal, env_cal_ids = apply_calibrations(t, env, env_cals)
    bean_cal_filled, _, _ = _linear_fill(t, bean_cal, max_gap_fill_s, channel="bean")
    env_cal_filled, _, _ = _linear_fill(t, env_cal, max_gap_fill_s, channel="env")

    ror = rate_of_rise(t, bean, ror_cfg)
    ror_cal = rate_of_rise(t, bean_cal, ror_cfg)

    raw_points = [
        {
            "t_s": float(t[i]),
            "bean_temp_c": None if np.isnan(bean[i]) else float(bean[i]),
            "env_temp_c": None if np.isnan(env[i]) else float(env[i]),
            "bean_temp_cal_c": None if np.isnan(bean_cal[i]) else float(bean_cal[i]),
            "env_temp_cal_c": None if np.isnan(env_cal[i]) else float(env_cal[i]),
            "ror_c_per_min": None if np.isnan(ror["ror_raw"][i]) else float(ror["ror_raw"][i]),
            "ror_display": None if np.isnan(ror["ror_display"][i]) else float(ror["ror_display"][i]),
            "ror_cal_c_per_min": None if np.isnan(ror_cal["ror_raw"][i]) else float(ror_cal["ror_raw"][i]),
            "ror_cal_display": None if np.isnan(ror_cal["ror_display"][i]) else float(ror_cal["ror_display"][i]),
            "ror_n_points": int(ror["n_points_used"][i]),
            "ror_edge": bool(ror["edge"][i]),
            "is_interpolated": bool(bean_interp[i]),
            "bean_cal_id": bean_cal_ids[i],
            "env_cal_id": env_cal_ids[i],
        }
        for i in range(len(t))
    ]

    return {
        "raw_points": raw_points,
        # Continuous guide line (measured + flagged interpolated), NaN across
        # unfilled gaps so the chart renders a break.
        "guide_bean_temp": [None if np.isnan(v) else float(v) for v in bean_filled],
        "guide_env_temp": [None if np.isnan(v) else float(v) for v in env_filled],
        # Corrected-basis guide lines; identical break/flag structure.
        "guide_bean_cal_temp": [None if np.isnan(v) else float(v) for v in bean_cal_filled],
        "guide_env_cal_temp": [None if np.isnan(v) else float(v) for v in env_cal_filled],
        "interpolated_t_s": [float(x) for x in t[bean_interp | env_interp]],
        "missing_segments": bean_gaps + env_gaps,
        "ror_window": {
            "method": "centred_least_squares_slope_on_measured_points",
            "window_s": ror["window_s"],
            "display_smooth_s": ror["display_smooth_s"],
            "min_points": ror["min_points"],
            "min_span_s": ror["min_span_s"],
            "units": "C/min",
        },
        "calibration": {
            "mode": "corrected" if cals else "raw",
            "applied": cals,
        },
    }


def current_events(events: list[dict]) -> dict[str, dict]:
    """Map event_type -> the latest non-superseded event row (as dict)."""
    out: dict[str, dict] = {}
    for e in sorted(events, key=lambda e: (e["t_s"], e["id"])):
        if not e["superseded"]:
            out[e["event_type"]] = e
    return out


def phase_metrics(events: list[dict]) -> dict:
    """Development-time ratio etc., computed over explicit event intervals.

    Intervals (all anchored on operator-visible, source-labelled events):
      drying:      charge -> turning point
      maillard:    turning point -> first crack start
      development: first crack start -> drop
      total:       charge -> drop
    development_ratio = development / total.

    Returns ``None`` fields (never a guessed value) when a boundary event is
    missing, plus the exact events used so the computation is auditable.
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


def guide_value_at(t: np.ndarray, guide: list, t_q: float) -> float | None:
    """Piecewise-linear value of a guide line at ``t_q``.

    Returns ``None`` outside the sampled range or across an unfilled break —
    the same honesty rules as the chart: never read a value out of a gap.
    """
    t = np.asarray(t, dtype=float)
    n = len(t)
    if n == 0 or t_q < t[0] or t_q > t[-1]:
        return None
    j = int(np.searchsorted(t, t_q, side="left"))
    if j < n and abs(t[j] - t_q) < 1e-9:
        v = guide[j]
        return None if v is None else round(float(v), 3)
    if j == 0:
        return None
    v0, v1 = guide[j - 1], guide[j]
    if v0 is None or v1 is None:
        return None
    w = (t_q - t[j - 1]) / (t[j] - t[j - 1])
    return round(float(v0 + (v1 - v0) * w), 3)


def anchor_temperatures(
    events: list[dict],
    t,
    guides: dict[str, list],
    interp_ranges=(),
) -> dict:
    """Temperature at each current phase-anchor event, per basis.

    ``guides`` maps an output key (e.g. ``raw_bean_c`` / ``cal_bean_c``) to a
    guide-line array.  Values are read off the same guide line the chart
    draws, so a figure on the metrics panel always matches the curve.
    ``on_interpolated_segment`` flags anchors that fall inside a bridged gap
    (the value is then interpolation-based, not a measurement).
    """
    cur = current_events(events)
    t_arr = np.asarray(list(t), dtype=float)
    out: dict[str, dict] = {}
    for kind in EVENT_TIME_KEYS:
        e = cur.get(kind)
        if e is None:
            continue
        t_q = float(e["t_s"])
        entry = {name: guide_value_at(t_arr, vals, t_q) for name, vals in guides.items()}
        entry["on_interpolated_segment"] = any(
            lo - 1e-9 <= t_q <= hi + 1e-9 for lo, hi in interp_ranges
        )
        out[kind] = entry
    return out
