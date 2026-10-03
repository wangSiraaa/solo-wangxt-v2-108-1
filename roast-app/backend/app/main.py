"""FastAPI application: batch curves, sourced events, calibration ledger,
comparison, export."""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import synth
from .analysis import (
    RoRConfig,
    anchor_temperatures,
    build_series,
    current_events,
    find_calibration_conflicts,
    normalize_calibration,
    phase_metrics,
)
from .config import CORS_ORIGINS, MAX_GAP_FILL_S
from .models import (
    Batch,
    Calibration,
    CalibrationHistory,
    Event,
    Sample,
    engine,
    init_db,
)
from .schemas import (
    BatchMeta,
    CalibrationActionIn,
    CalibrationIn,
    CalibrationOut,
    EventIn,
    EventOut,
)

app = FastAPI(title="Coffee Roast Batch Explorer", version="1.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    init_db()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _get_batch(session: Session, batch_id: int) -> Batch:
    b = session.get(Batch, batch_id)
    if b is None:
        raise HTTPException(404, f"batch {batch_id} not found")
    return b


def _samples_as_dicts(batch: Batch) -> list[dict]:
    return [
        {
            "t_s": s.t_s,
            "bean_temp_c": s.bean_temp_c,
            "env_temp_c": s.env_temp_c,
        }
        for s in batch.samples
    ]


def _events_as_dicts(batch: Batch, *, include_history: bool) -> list[dict]:
    rows = []
    for e in batch.events:
        if not include_history and e.superseded:
            continue
        rows.append(
            {
                "id": e.id,
                "batch_id": e.batch_id,
                "event_type": e.event_type,
                "t_s": e.t_s,
                "label": e.label,
                "source": e.source,
                "created_by": e.created_by,
                "value_num": e.value_num,
                "note": e.note,
                "superseded": e.superseded,
                "superseded_by_id": e.superseded_by_id,
                "created_at": e.created_at.isoformat(),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# calibration ledger helpers
# ---------------------------------------------------------------------------

def _cal_public_dict(row: Calibration) -> dict:
    """Analysis-facing shape of a ledger record (embedded in exports so an
    independent recompute needs no database access)."""
    return {
        "id": row.id,
        "batch_id": row.batch_id,
        "channel": row.channel,
        "valid_from_s": row.valid_from_s,
        "valid_to_s": row.valid_to_s,
        "formula": row.formula,
        "params": {"gain": row.gain, "offset": row.offset},
        "version": row.version,
        "status": row.status,
        "created_by": row.created_by,
        "note": row.note,
        "supersedes_id": row.supersedes_id,
        "superseded_by_id": row.superseded_by_id,
    }


def _cal_out(row: Calibration) -> dict:
    """Ledger API shape, including the full status-transition history.
    Must be called while the session is still open (lazy history load)."""
    d = _cal_public_dict(row)
    d.update(
        {
            "gain": row.gain,
            "offset": row.offset,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "history": [
                {
                    "id": h.id,
                    "from_status": h.from_status,
                    "to_status": h.to_status,
                    "acted_by": h.acted_by,
                    "note": h.note,
                    "created_at": h.created_at.isoformat(),
                }
                for h in row.history
            ],
        }
    )
    return d


def _parse_cal_ids(cal_ids: str | None) -> list[int] | None:
    """Explicit adjudication arrives as ``?cal_ids=3,7``.  Empty/absent means
    'no explicit selection' (auto = every active record)."""
    if cal_ids is None or not cal_ids.strip():
        return None
    try:
        return [int(x) for x in cal_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "cal_ids 必须是逗号分隔的整数 id 列表")


def _resolve_calibrations(
    s: Session,
    batch_id: int,
    cal_ids: list[int] | None,
    extra_valid_ids: frozenset[int] = frozenset(),
) -> tuple[list[dict], list[int] | None, list[int]]:
    """Decide which calibrations apply to one batch for this request.

    Returns ``(effective_records, explicit_selection_or_None, active_ids)``.

    * No explicit selection: every *active* record applies.  Overlapping
      active ranges on one channel are a **visible conflict -> HTTP 409**,
      never a silent pick.
    * Explicit selection: exactly those records apply.  Each id must be an
      active record of this batch (or of a compared batch, passed via
      ``extra_valid_ids``) and the selection itself must be conflict-free.
    """
    actives = list(
        s.scalars(
            select(Calibration).where(
                Calibration.batch_id == batch_id,
                Calibration.status == "active",
            )
        )
    )
    active_ids = [r.id for r in actives]

    if cal_ids is None:
        eff = [_cal_public_dict(r) for r in actives]
        conflicts = find_calibration_conflicts(eff)
        if conflicts:
            raise HTTPException(
                409,
                detail={
                    "error": "calibration_conflict",
                    "batch_id": batch_id,
                    "message": (
                        "同一通道存在重叠的已启用校准，分析与导出已阻断。"
                        "请明确裁决：用 cal_ids 指定唯一版本，或撤回其中之一。"
                    ),
                    "conflicts": conflicts,
                    "active_ids": active_ids,
                },
            )
        return eff, None, active_ids

    valid = set(active_ids) | set(extra_valid_ids)
    unknown = [i for i in cal_ids if i not in valid]
    if unknown:
        raise HTTPException(
            400,
            f"cal_ids 含不存在或非启用中的校准记录: {unknown}（只能用启用中的记录裁决）",
        )
    eff = [_cal_public_dict(r) for r in actives if r.id in set(cal_ids)]
    conflicts = find_calibration_conflicts(eff)
    if conflicts:
        raise HTTPException(
            400,
            detail={
                "error": "calibration_selection_conflict",
                "message": "所选校准版本自身仍重叠，裁决无效，请只保留每条通道一个版本。",
                "conflicts": conflicts,
            },
        )
    return eff, sorted(r["id"] for r in eff), active_ids


def _metrics_payload(events: list[dict], series: dict) -> dict:
    """Phase metrics + anchor temperatures in both bases + calibration basis.

    Durations are pure event intervals (calibration-independent); anchor
    temperatures are read off the raw and corrected guide lines, so the
    metrics block visibly follows the selected calibration basis.
    """
    metrics = phase_metrics(events)
    t = [p["t_s"] for p in series["raw_points"]]
    guides = {
        "raw_bean_c": series["guide_bean_temp"],
        "cal_bean_c": series["guide_bean_cal_temp"],
        "raw_env_c": series["guide_env_temp"],
        "cal_env_c": series["guide_env_cal_temp"],
    }
    interp_bean = [
        (g["t_start_s"], g["t_end_s"])
        for g in series["missing_segments"]
        if g["channel"] == "bean" and g["status"] == "interpolated"
    ]
    metrics["anchor_temps_c"] = anchor_temperatures(events, t, guides, interp_bean)
    metrics["calibration_basis"] = {
        "mode": series["calibration"]["mode"],
        "applied": [
            {"id": c["id"], "version": c["version"], "channel": c["channel"]}
            for c in series["calibration"]["applied"]
        ],
    }
    return metrics


def _series_payload(
    batch: Batch,
    *,
    window_s: float,
    display_smooth_s: float,
    max_gap_fill_s: float,
    include_history: bool,
    calibrations: list[dict],
    cal_selection: list[int] | None,
    active_cal_ids: list[int],
) -> dict[str, Any]:
    series = build_series(
        _samples_as_dicts(batch),
        ror_cfg=RoRConfig(window_s=window_s, display_smooth_s=display_smooth_s),
        max_gap_fill_s=max_gap_fill_s,
        calibrations=calibrations,
    )
    events = _events_as_dicts(batch, include_history=include_history)
    return {
        "batch": BatchMeta.model_validate(batch).model_dump(mode="json"),
        "series": series,
        "events": events,
        "metrics": _metrics_payload(events, series),
        "params": {
            "ror_window_s": window_s,
            "ror_display_smooth_s": display_smooth_s,
            "max_gap_fill_s": max_gap_fill_s,
            "raw_is_immutable": True,
            "calibration_ids": [c["id"] for c in calibrations],
            "calibration_mode": series["calibration"]["mode"],
        },
        "calibration": {
            **series["calibration"],
            "selection": cal_selection,
            "active_ids": active_cal_ids,
        },
    }


# ---------------------------------------------------------------------------
# batches / seeding
# ---------------------------------------------------------------------------

@app.get("/api/batches", response_model=list[BatchMeta])
def list_batches() -> list[Batch]:
    with Session(engine) as s:
        return list(s.scalars(select(Batch).order_by(Batch.id)))


@app.post("/api/seed", response_model=list[BatchMeta])
def seed_demo() -> list[Batch]:
    """Load the two synthetic demo batches (noise + dropouts, no machine)."""
    with Session(engine) as s:
        created: list[Batch] = []
        for spec in synth.two_demo_batches():
            existing = s.scalar(select(Batch).where(Batch.name == spec["name"]))
            if existing is not None:
                created.append(existing)
                continue
            b = Batch(
                name=spec["name"],
                roaster=spec["roaster"],
                bean=spec["bean"],
                charge_at=spec["charge_at"],
                charge_temp_c=spec["charge_temp_c"],
                ambient_temp_c=spec["ambient_temp_c"],
                target_drop_temp_c=spec["target_drop_temp_c"],
                note=spec["note"],
            )
            b.samples = [
                Sample(
                    t_s=sp["t_s"],
                    bean_temp_c=sp["bean_temp_c"],
                    env_temp_c=sp["env_temp_c"],
                )
                for sp in spec["samples"]
            ]
            b.events = [Event(**ev) for ev in spec["events"]]
            s.add(b)
            created.append(b)
        s.commit()
        for b in created:
            s.refresh(b)
        return created


@app.get("/api/batches/{batch_id}/series")
def get_series(
    batch_id: int,
    window_s: float = Query(30.0, gt=0, le=300),
    display_smooth_s: float = Query(12.0, ge=0, le=180),
    max_gap_fill_s: float = Query(MAX_GAP_FILL_S, gt=0, le=600),
    include_history: bool = Query(False),
    cal_ids: str | None = Query(
        None, description="显式裁决：逗号分隔的校准记录 id；缺省=全部启用记录"
    ),
) -> dict[str, Any]:
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        cals, selection, active_ids = _resolve_calibrations(
            s, batch_id, _parse_cal_ids(cal_ids)
        )
        return _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=max_gap_fill_s,
            include_history=include_history,
            calibrations=cals,
            cal_selection=selection,
            active_cal_ids=active_ids,
        )


# ---------------------------------------------------------------------------
# events: append-only corrections with provenance
# ---------------------------------------------------------------------------

@app.post("/api/batches/{batch_id}/events", response_model=EventOut)
def add_event(batch_id: int, ev: EventIn) -> Event:
    with Session(engine) as s:
        _get_batch(s, batch_id)
        row = Event(batch_id=batch_id, **ev.model_dump())
        s.add(row)
        s.flush()
        # Only one *current* event per type: supersede the previous current one.
        if row.event_type != "damper_change":
            prev = s.scalars(
                select(Event).where(
                    Event.batch_id == batch_id,
                    Event.event_type == row.event_type,
                    Event.superseded.is_(False),
                    Event.id != row.id,
                )
            ).all()
            for p in prev:
                p.superseded = True
                p.superseded_by_id = row.id
        s.commit()
        s.refresh(row)
        return row


@app.get("/api/batches/{batch_id}/events", response_model=list[EventOut])
def list_events(batch_id: int, include_history: bool = Query(False)) -> list[Event]:
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        q = select(Event).where(Event.batch_id == batch_id)
        if not include_history:
            q = q.where(Event.superseded.is_(False))
        return list(s.scalars(q.order_by(Event.t_s)))


# ---------------------------------------------------------------------------
# calibration ledger: draft -> active -> withdrawn | superseded, append-only
# ---------------------------------------------------------------------------

def _get_calibration(session: Session, cal_id: int) -> Calibration:
    c = session.get(Calibration, cal_id)
    if c is None:
        raise HTTPException(404, f"calibration {cal_id} not found")
    return c


_UNSET = object()


def _transition(
    s: Session,
    cal: Calibration,
    to_status: str,
    acted_by: str,
    note: str = "",
    from_status=_UNSET,
) -> None:
    """Append a status transition to the ledger history (never edit history).

    ``from_status`` defaults to the record's current status; creation passes
    an explicit ``None`` because the column default already reads 'draft'.
    """
    s.add(
        CalibrationHistory(
            calibration_id=cal.id,
            from_status=cal.status if from_status is _UNSET else from_status,
            to_status=to_status,
            acted_by=acted_by,
            note=note,
        )
    )
    cal.status = to_status


@app.get("/api/batches/{batch_id}/calibrations", response_model=list[CalibrationOut])
def list_calibrations(batch_id: int) -> list[dict]:
    """The full ledger for one batch — every status, every version, with
    transition history.  Records are never deleted."""
    with Session(engine) as s:
        _get_batch(s, batch_id)
        rows = list(
            s.scalars(
                select(Calibration)
                .where(Calibration.batch_id == batch_id)
                .order_by(Calibration.id)
            )
        )
        return [_cal_out(r) for r in rows]


@app.post(
    "/api/batches/{batch_id}/calibrations",
    response_model=CalibrationOut,
    status_code=201,
)
def create_calibration(batch_id: int, body: CalibrationIn) -> dict:
    """Record a new calibration as a DRAFT.  It only affects analysis after
    an explicit activate transition."""
    with Session(engine) as s:
        _get_batch(s, batch_id)
        row = Calibration(batch_id=batch_id, **body.model_dump())
        s.add(row)
        s.flush()
        _transition(s, row, "draft", body.created_by, "创建校准草稿", from_status=None)
        s.commit()
        s.refresh(row)
        return _cal_out(row)


@app.post("/api/calibrations/{cal_id}/activate", response_model=CalibrationOut)
def activate_calibration(cal_id: int, body: CalibrationActionIn | None = None) -> dict:
    """draft -> active.  Overlapping actives are allowed to exist; analysis
    then reports a visible conflict instead of silently picking one."""
    acted_by = body.acted_by if body else "operator"
    note = body.note if body else ""
    with Session(engine) as s:
        row = _get_calibration(s, cal_id)
        if row.status != "draft":
            raise HTTPException(409, f"仅草稿可启用（当前状态 {row.status}）")
        _transition(s, row, "active", acted_by, note or "启用校准")
        s.commit()
        s.refresh(row)
        return _cal_out(row)


@app.post("/api/calibrations/{cal_id}/withdraw", response_model=CalibrationOut)
def withdraw_calibration(cal_id: int, body: CalibrationActionIn | None = None) -> dict:
    """draft|active -> withdrawn.  The record stays in the ledger and past
    exports that embedded it remain reproducible."""
    acted_by = body.acted_by if body else "operator"
    note = body.note if body else ""
    with Session(engine) as s:
        row = _get_calibration(s, cal_id)
        if row.status not in ("draft", "active"):
            raise HTTPException(409, f"仅草稿或启用中的记录可撤回（当前状态 {row.status}）")
        _transition(s, row, "withdrawn", acted_by, note or "撤回校准")
        s.commit()
        s.refresh(row)
        return _cal_out(row)


@app.post(
    "/api/calibrations/{cal_id}/supersede",
    response_model=CalibrationOut,
    status_code=201,
)
def supersede_calibration(cal_id: int, body: CalibrationIn) -> dict:
    """Replace an ACTIVE record with a new version.

    The old record becomes ``superseded`` (kept for audit, linked via
    ``superseded_by_id``); the new version is created as a DRAFT, so the
    current view falls back to the remaining rules until the new version is
    explicitly activated.
    """
    with Session(engine) as s:
        old = _get_calibration(s, cal_id)
        if old.status != "active":
            raise HTTPException(409, f"仅启用中的记录可被新版本取代（当前状态 {old.status}）")
        new = Calibration(
            batch_id=old.batch_id,
            **body.model_dump(),
            version=old.version + 1,
            supersedes_id=old.id,
        )
        s.add(new)
        s.flush()
        _transition(
            s, new, "draft", body.created_by,
            f"取代 #{old.id} v{old.version} 的新版本草稿", from_status=None,
        )
        _transition(s, old, "superseded", body.created_by, f"被 #{new.id} v{new.version} 取代")
        old.superseded_by_id = new.id
        s.commit()
        s.refresh(new)
        return _cal_out(new)


# ---------------------------------------------------------------------------
# comparison (no causal claims) + export / recompute
# ---------------------------------------------------------------------------

@app.get("/api/compare")
def compare(
    a: int = Query(..., description="first batch id"),
    b: int = Query(..., description="second batch id"),
    window_s: float = Query(30.0, gt=0, le=300),
    display_smooth_s: float = Query(12.0, ge=0, le=180),
    max_gap_fill_s: float = Query(MAX_GAP_FILL_S, gt=0, le=600),
    cal_ids: str | None = Query(None, description="显式裁决的校准 id（跨两个批次）"),
) -> dict[str, Any]:
    """Overlay two batches on charge-relative time. Damper changes are shown
    as marks so the operator can eyeball before/after shape; the API attaches
    an explicit non-causal note."""
    with Session(engine) as s:
        ba, bb = _get_batch(s, a), _get_batch(s, b)
        ids = _parse_cal_ids(cal_ids)
        # An explicit selection may span both batches; validate against the
        # union of their active records, then apply per batch.
        extra: frozenset[int] = frozenset()
        if ids is not None:
            extra = frozenset(
                s.scalars(
                    select(Calibration.id).where(
                        Calibration.batch_id.in_([a, b]),
                        Calibration.status == "active",
                    )
                )
            )
        cals_a, sel_a, act_a = _resolve_calibrations(s, a, ids, extra)
        cals_b, sel_b, act_b = _resolve_calibrations(s, b, ids, extra)
        payload = {
            "batches": [
                _series_payload(
                    ba,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                    calibrations=cals_a,
                    cal_selection=sel_a,
                    active_cal_ids=act_a,
                ),
                _series_payload(
                    bb,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                    calibrations=cals_b,
                    cal_selection=sel_b,
                    active_cal_ids=act_b,
                ),
            ],
            "interpretation": (
                "曲线按开火/下豆时刻对齐叠加。风门变化以标记线显示，"
                "前后形态仅供观察对比，不构成因果结论（无对照、无重复、无统计检验）。"
            ),
        }
        return payload


@app.get("/api/batches/{batch_id}/export")
def export_batch(
    batch_id: int,
    window_s: float = 30.0,
    display_smooth_s: float = 12.0,
    cal_ids: str | None = Query(None),
) -> dict[str, Any]:
    """Self-contained export: raw samples, sourced events, parameters, the
    applied calibration records (id + version + formula), and the derived
    phase metrics.  The metrics and the corrected view can be reproduced from
    raw + events + calibrations + the stated window (see /api/recompute)."""
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        cals, selection, active_ids = _resolve_calibrations(
            s, batch_id, _parse_cal_ids(cal_ids)
        )
        payload = _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=MAX_GAP_FILL_S,
            include_history=True,
            calibrations=cals,
            cal_selection=selection,
            active_cal_ids=active_ids,
        )
        payload["export_version"] = 2
        payload["reproducibility"] = {
            "raw_samples_are_source_of_truth": True,
            "metrics_depend_on": [
                "raw_samples",
                "current(non-superseded) events",
                "ror_window_s",
                "calibration records embedded under calibration.applied (id + version)",
            ],
            "pipeline": "numpy centred least-squares RoR; linear gap fill flagged; "
            "calibration = gain*raw+offset on measured points only",
        }
        return payload


@app.post("/api/recompute")
def recompute(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-derive series + metrics from an export-style payload.

    Used to verify an export reproduces every stage metric without touching
    the database.  Body: {"samples": [...], "events": [...], "params": {...},
    "calibrations": [...]}.  ``calibrations`` are the records embedded in the
    export (same id + version); they must be conflict-free per channel.
    """
    try:
        samples = payload["samples"]
        events = payload.get("events", [])
        params = payload.get("params", {})
    except KeyError as exc:
        raise HTTPException(422, f"missing field: {exc}")
    try:
        cals = [normalize_calibration(c) for c in (payload.get("calibrations") or [])]
    except ValueError as exc:
        raise HTTPException(422, f"invalid calibration record: {exc}")
    conflicts = find_calibration_conflicts(cals)
    if conflicts:
        raise HTTPException(
            422,
            detail={
                "error": "calibration_conflict",
                "message": "重算载荷中的校准记录在同一通道上重叠，无法确定口径。",
                "conflicts": conflicts,
            },
        )
    cfg = RoRConfig(
        window_s=float(params.get("ror_window_s", 30.0)),
        display_smooth_s=float(params.get("ror_display_smooth_s", 12.0)),
    )
    series = build_series(
        samples,
        ror_cfg=cfg,
        max_gap_fill_s=float(params.get("max_gap_fill_s", MAX_GAP_FILL_S)),
        calibrations=cals,
    )
    return {
        "series": series,
        "metrics": _metrics_payload(events, series),
        "current_events": current_events(events),
        "calibration": series["calibration"],
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "machine_connection": "none (synthetic/offline only)"}
