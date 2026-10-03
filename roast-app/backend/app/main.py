"""FastAPI application: batch curves, sourced events, comparison, export."""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import synth
from .analysis import (
    RoRConfig,
    build_series,
    current_events,
    find_calibration_conflicts,
    phase_metrics,
)
from .config import CORS_ORIGINS, MAX_GAP_FILL_S
from .models import Batch, Calibration, Event, Sample, engine, init_db
from .schemas import BatchMeta, CalibrationIn, CalibrationOut, EventIn, EventOut

app = FastAPI(title="Coffee Roast Batch Explorer", version="1.0.0")
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


def _calibrations_as_dicts(batch: Batch) -> list[dict]:
    return [
        {
            "id": c.id,
            "batch_id": c.batch_id,
            "channel": c.channel,
            "t_start_s": c.t_start_s,
            "t_end_s": c.t_end_s,
            "formula": c.formula,
            "scale": c.scale,
            "offset_c": c.offset_c,
            "created_by": c.created_by,
            "note": c.note,
            "version": c.version,
            "status": c.status,
            "replaces_id": c.replaces_id,
            "superseded_by_id": c.superseded_by_id,
            "created_at": c.created_at.isoformat(),
            "status_changed_at": c.status_changed_at.isoformat(),
        }
        for c in batch.calibrations
    ]


def _active_calibrations_or_409(batch: Batch) -> list[dict]:
    """Active calibrations for the batch — or a visible 409 conflict.

    Overlapping active ranges on one channel are never resolved silently:
    every derived view (series/compare/export) refuses until the operator
    adjudicates by withdrawing or superseding one of the records.
    """
    active = [c for c in _calibrations_as_dicts(batch) if c["status"] == "active"]
    conflicts = find_calibration_conflicts(active)
    if conflicts:
        raise HTTPException(
            409,
            detail={
                "error": "calibration_conflict",
                "message": (
                    "同一通道存在有效时间段相交的已启用校准，分析与导出已阻断；"
                    "请撤回或以新版本取代其中一条后再试。"
                ),
                "conflicts": conflicts,
            },
        )
    return active


def _series_payload(
    batch: Batch,
    *,
    window_s: float,
    display_smooth_s: float,
    max_gap_fill_s: float,
    include_history: bool,
) -> dict[str, Any]:
    calibrations = _active_calibrations_or_409(batch)
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
        "metrics": phase_metrics(events, series["raw_points"]),
        "calibration": series["calibration"],
        "params": {
            "ror_window_s": window_s,
            "ror_display_smooth_s": display_smooth_s,
            "max_gap_fill_s": max_gap_fill_s,
            "raw_is_immutable": True,
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
) -> dict[str, Any]:
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        return _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=max_gap_fill_s,
            include_history=include_history,
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
# calibration ledger: append-only records, draft -> active -> withdrawn |
# superseded.  Rows are never edited or deleted; a correction is a new
# version linked by replaces_id / superseded_by_id.
# ---------------------------------------------------------------------------

@app.get("/api/batches/{batch_id}/calibrations", response_model=list[CalibrationOut])
def list_calibrations(batch_id: int) -> list[Calibration]:
    """Full ledger for the batch — every status, so withdrawn/superseded
    versions and what they produced stay auditable."""
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        rows = list(
            s.scalars(
                select(Calibration)
                .where(Calibration.batch_id == b.id)
                .order_by(Calibration.id)
            )
        )
        for r in rows:  # detach-safe: load all columns before session closes
            _ = r.status_changed_at
        return rows


@app.post(
    "/api/batches/{batch_id}/calibrations",
    response_model=CalibrationOut,
    status_code=201,
)
def create_calibration(batch_id: int, body: CalibrationIn) -> Calibration:
    """Create a DRAFT calibration.  With ``replaces_id`` the draft becomes the
    next version of that record (version = old + 1); activation then
    supersedes the old row atomically."""
    with Session(engine) as s:
        _get_batch(s, batch_id)
        version = 1
        if body.replaces_id is not None:
            old = s.get(Calibration, body.replaces_id)
            if old is None or old.batch_id != batch_id:
                raise HTTPException(404, "replaces_id does not name a calibration of this batch")
            if old.channel != body.channel:
                raise HTTPException(422, "a new version must keep the same channel")
            version = old.version + 1
        row = Calibration(
            batch_id=batch_id,
            version=version,
            status="draft",
            **body.model_dump(),
        )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def _transition(cal_id: int, action: str) -> Calibration:
    with Session(engine) as s:
        row = s.get(Calibration, cal_id)
        if row is None:
            raise HTTPException(404, f"calibration {cal_id} not found")
        if action == "activate":
            if row.status != "draft":
                raise HTTPException(
                    409, f"only a draft can be activated (current status: {row.status})"
                )
            # Atomic supersede: the replaced row flips in the same commit.
            if row.replaces_id is not None:
                old = s.get(Calibration, row.replaces_id)
                if old is not None and old.status == "active":
                    old.status = "superseded"
                    old.superseded_by_id = row.id
                    old.status_changed_at = datetime.utcnow()
            row.status = "active"
        elif action == "withdraw":
            if row.status not in ("draft", "active"):
                raise HTTPException(
                    409, f"only a draft or active record can be withdrawn (current: {row.status})"
                )
            row.status = "withdrawn"
        else:  # pragma: no cover - guarded by the routes below
            raise HTTPException(400, f"unknown action {action}")
        row.status_changed_at = datetime.utcnow()
        s.commit()
        s.refresh(row)
        return row


@app.post("/api/calibrations/{cal_id}/activate", response_model=CalibrationOut)
def activate_calibration(cal_id: int) -> Calibration:
    return _transition(cal_id, "activate")


@app.post("/api/calibrations/{cal_id}/withdraw", response_model=CalibrationOut)
def withdraw_calibration(cal_id: int) -> Calibration:
    return _transition(cal_id, "withdraw")


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
) -> dict[str, Any]:
    """Overlay two batches on charge-relative time. Damper changes are shown
    as marks so the operator can eyeball before/after shape; the API attaches
    an explicit non-causal note."""
    with Session(engine) as s:
        ba, bb = _get_batch(s, a), _get_batch(s, b)
        payload = {
            "batches": [
                _series_payload(
                    ba,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                ),
                _series_payload(
                    bb,
                    window_s=window_s,
                    display_smooth_s=display_smooth_s,
                    max_gap_fill_s=max_gap_fill_s,
                    include_history=False,
                ),
            ],
            "interpretation": (
                "曲线按开火/下豆时刻对齐叠加。风门变化以标记线显示，"
                "前后形态仅供观察对比，不构成因果结论（无对照、无重复、无统计检验）。"
            ),
        }
        return payload


@app.get("/api/batches/{batch_id}/export")
def export_batch(batch_id: int, window_s: float = 30.0, display_smooth_s: float = 12.0) -> dict[str, Any]:
    """Self-contained export: raw samples, sourced events, the exact
    calibration versions applied (plus the full ledger for audit),
    parameters, and the derived phase metrics.  The metrics can be
    reproduced from raw + events + calibrations + the stated window
    (see /api/recompute)."""
    with Session(engine) as s:
        b = _get_batch(s, batch_id)
        payload = _series_payload(
            b,
            window_s=window_s,
            display_smooth_s=display_smooth_s,
            max_gap_fill_s=MAX_GAP_FILL_S,
            include_history=True,
        )
        payload["export_version"] = 2
        # The exact calibration versions this export was computed with —
        # recompute must use THESE, not whatever is active later.
        payload["calibrations"] = payload["calibration"]["applied"]
        payload["calibration_ledger"] = _calibrations_as_dicts(b)
        payload["reproducibility"] = {
            "raw_samples_are_source_of_truth": True,
            "metrics_depend_on": [
                "raw_samples",
                "current(non-superseded) events",
                "ror_window_s",
                "calibrations (exact versions embedded above)",
            ],
            "pipeline": "numpy centred least-squares RoR; linear gap fill flagged; affine calibration on measured points only",
        }
        return payload


@app.post("/api/recompute")
def recompute(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-derive series + metrics from an export-style payload.

    Used to verify an export reproduces every stage metric without touching
    the database.  Body: {"samples": [...], "events": [...], "params": {...},
    "calibrations": [...]}.  The calibration versions embedded in the export
    are applied as-is; overlapping active ranges are a 409 here too.
    """
    try:
        samples = payload["samples"]
        events = payload.get("events", [])
        params = payload.get("params", {})
        calibrations = payload.get("calibrations", [])
    except KeyError as exc:
        raise HTTPException(422, f"missing field: {exc}")
    conflicts = find_calibration_conflicts(calibrations)
    if conflicts:
        raise HTTPException(
            409,
            detail={
                "error": "calibration_conflict",
                "message": "导出载荷中的已启用校准存在相交有效段，独立重算被阻断。",
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
        calibrations=calibrations,
    )
    return {
        "series": series,
        "metrics": phase_metrics(events, series["raw_points"]),
        "current_events": current_events(events),
        "calibration": series["calibration"],
    }


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "machine_connection": "none (synthetic/offline only)"}
