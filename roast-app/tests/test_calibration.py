"""Acceptance tests for the local calibration ledger.

Covers the five acceptance items:
 1. an active calibration changes corrected curve / RoR / phase metrics while
    every raw temperature stays byte-identical;
 2. two overlapping ACTIVE calibrations block analysis + export with a
    visible conflict; adjudication (withdraw) unblocks with the chosen version;
 3. dropouts and interpolation flags survive calibration — missing stays
    missing, interpolated stays non-measured;
 4. withdraw / supersede keeps the old versions auditable and the current
    view falls back per the rules;
 5. an export payload recomputed independently with the same calibration
    version yields identical results.
"""
import pytest
from sqlalchemy.orm import Session

from app import models


@pytest.fixture(autouse=True)
def _clean_calibrations():
    """Each test starts with an empty ledger (batches/samples stay seeded)."""
    with Session(models.engine) as s:
        s.query(models.Calibration).delete()
        s.commit()
    yield


def _seed(client):
    r = client.post("/api/seed")
    assert r.status_code == 200
    return [b["id"] for b in r.json()]


def _create(client, batch_id, **kw):
    body = {
        "channel": "bean",
        "t_start_s": 100.0,
        "t_end_s": 500.0,
        "scale": 1.05,
        "offset_c": 2.0,
        "created_by": "cal-tech",
        "note": "post-maintenance zero/span drift",
    }
    body.update(kw)
    r = client.post(f"/api/batches/{batch_id}/calibrations", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _create_active(client, batch_id, **kw):
    cal = _create(client, batch_id, **kw)
    r = client.post(f"/api/calibrations/{cal['id']}/activate")
    assert r.status_code == 200, r.text
    return r.json()


def _raw_sig(payload):
    return [
        (p["t_s"], p["bean_temp_c"], p["env_temp_c"])
        for p in payload["series"]["raw_points"]
    ]


# --- ① non-overlapping active calibration: derived changes, raw untouched ---

def test_active_calibration_changes_derived_but_never_raw(client):
    aid, _ = _seed(client)
    base = client.get(f"/api/batches/{aid}/series").json()

    cal = _create_active(client, aid, scale=1.05, offset_c=2.0,
                         t_start_s=100.0, t_end_s=600.0)
    d = client.get(f"/api/batches/{aid}/series").json()

    # raw samples byte-identical
    assert _raw_sig(d) == _raw_sig(base)

    # corrected values follow the formula inside the range, raw outside
    for p in d["series"]["raw_points"]:
        if p["bean_temp_c"] is None:
            assert p["bean_temp_corrected_c"] is None
        elif 100.0 <= p["t_s"] <= 600.0:
            assert p["bean_temp_corrected_c"] == pytest.approx(1.05 * p["bean_temp_c"] + 2.0)
        else:
            assert p["bean_temp_corrected_c"] == p["bean_temp_c"]

    # RoR and guide line moved to the corrected basis
    assert [p["ror_c_per_min"] for p in d["series"]["raw_points"]] != [
        p["ror_c_per_min"] for p in base["series"]["raw_points"]
    ]
    assert d["series"]["guide_bean_temp"] != base["series"]["guide_bean_temp"]
    # raw-basis RoR is still reported and equals the old view
    assert [p["ror_raw_basis_c_per_min"] for p in d["series"]["raw_points"]] == [
        p["ror_c_per_min"] for p in base["series"]["raw_points"]
    ]

    # phase metrics carry the new basis: durations identical (time-based),
    # anchor temperatures differ raw vs corrected
    assert d["metrics"]["drying_s"] == base["metrics"]["drying_s"]
    assert d["metrics"]["development_ratio"] == base["metrics"]["development_ratio"]
    tp = d["metrics"]["anchor_temps"]["turning_point"]
    # turning point (~1 min) is outside the calibrated range -> unchanged
    assert tp["bean_temp_corrected_c"] == tp["bean_temp_raw_c"]
    # first crack sits inside the calibrated range -> corrected differs, raw kept
    fc = d["metrics"]["anchor_temps"]["first_crack_start"]
    assert fc["bean_temp_corrected_c"] != fc["bean_temp_raw_c"]
    assert fc["bean_temp_raw_c"] == base["metrics"]["anchor_temps"]["first_crack_start"]["bean_temp_raw_c"]

    # the payload names the calibration version used
    applied = d["calibration"]["applied"]
    assert [(c["id"], c["version"]) for c in applied] == [(cal["id"], 1)]
    assert d["calibration"]["basis"] == "corrected"


def test_draft_calibration_has_no_effect(client):
    aid, _ = _seed(client)
    base = client.get(f"/api/batches/{aid}/series").json()
    _create(client, aid)  # stays a draft
    d = client.get(f"/api/batches/{aid}/series").json()
    assert d["calibration"]["basis"] == "raw"
    assert d["calibration"]["applied"] == []
    assert d["series"]["guide_bean_temp"] == base["series"]["guide_bean_temp"]


# --- ② overlapping active calibrations: visible conflict, explicit ruling ---

def test_overlapping_active_calibrations_block_analysis_until_adjudicated(client):
    aid, _ = _seed(client)
    a = _create_active(client, aid, t_start_s=100.0, t_end_s=300.0, scale=1.02)
    b = _create_active(client, aid, t_start_s=200.0, t_end_s=400.0, scale=1.07)

    for url in (f"/api/batches/{aid}/series", f"/api/batches/{aid}/export"):
        r = client.get(url)
        assert r.status_code == 409, url
        detail = r.json()["detail"]
        assert detail["error"] == "calibration_conflict"
        conf = detail["conflicts"][0]
        assert conf["channel"] == "bean"
        assert conf["overlap_t_start_s"] == 200.0
        assert conf["overlap_t_end_s"] == 300.0
        assert {c["id"] for c in conf["calibrations"]} == {a["id"], b["id"]}

    # compare is blocked too
    r = client.get("/api/compare", params={"a": aid, "b": aid})
    assert r.status_code == 409

    # adjudication: withdraw b -> only a's explicit version is used
    assert client.post(f"/api/calibrations/{b['id']}/withdraw").status_code == 200
    d = client.get(f"/api/batches/{aid}/series").json()
    assert [c["id"] for c in d["calibration"]["applied"]] == [a["id"]]
    for p in d["series"]["raw_points"]:
        if p["bean_temp_c"] is not None and 100.0 <= p["t_s"] <= 300.0:
            assert p["bean_temp_corrected_c"] == pytest.approx(1.02 * p["bean_temp_c"] + 2.0)


# --- ③ dropouts and interpolation flags survive calibration ---

def test_calibration_never_disguises_missing_or_interpolated(client):
    aid, _ = _seed(client)
    base = client.get(f"/api/batches/{aid}/series").json()
    # calibration spans both the short (bridged) and the long (open) dropout
    _create_active(client, aid, t_start_s=100.0, t_end_s=600.0, scale=1.1, offset_c=-3.0)
    d = client.get(f"/api/batches/{aid}/series").json()

    # identical gap audit: long gap still a break, short gap still interpolated
    stat = lambda p: sorted(
        (g["channel"], g["t_start_s"], g["status"]) for g in p["series"]["missing_segments"]
    )
    assert stat(d) == stat(base)
    assert d["series"]["interpolated_t_s"] == base["series"]["interpolated_t_s"]

    for pb, pc in zip(base["series"]["raw_points"], d["series"]["raw_points"]):
        assert pb["is_interpolated"] == pc["is_interpolated"]
        if pb["bean_temp_c"] is None:
            # missing stays missing on the corrected basis as well
            assert pc["bean_temp_corrected_c"] is None
    # the wide gap is still a break in the corrected guide line
    assert None in [v for v in d["series"]["guide_bean_temp"]]
    assert d["series"]["guide_bean_temp"].count(None) == base["series"]["guide_bean_temp"].count(None)


# --- ④ withdraw / supersede: history auditable, view falls back ---

def test_supersede_and_withdraw_keep_full_audit_trail(client):
    aid, _ = _seed(client)
    v1 = _create_active(client, aid, scale=1.05, offset_c=2.0)
    export_v1 = client.get(f"/api/batches/{aid}/export").json()
    assert [c["id"] for c in export_v1["calibrations"]] == [v1["id"]]

    # new version replaces v1
    v2 = _create(client, aid, scale=1.08, offset_c=1.0, replaces_id=v1["id"])
    assert v2["version"] == 2 and v2["status"] == "draft"
    r = client.post(f"/api/calibrations/{v2['id']}/activate")
    assert r.status_code == 200

    d = client.get(f"/api/batches/{aid}/series").json()
    assert [(c["id"], c["version"]) for c in d["calibration"]["applied"]] == [(v2["id"], 2)]

    # ledger keeps both: v1 superseded by v2, still fully readable
    ledger = client.get(f"/api/batches/{aid}/calibrations").json()
    by_id = {c["id"]: c for c in ledger}
    assert by_id[v1["id"]]["status"] == "superseded"
    assert by_id[v1["id"]]["superseded_by_id"] == v2["id"]
    assert by_id[v2["id"]]["status"] == "active"
    # the old export still names v1 — past results remain auditable
    assert export_v1["calibrations"][0]["version"] == 1

    # withdraw the current version -> view falls back to raw basis
    assert client.post(f"/api/calibrations/{v2['id']}/withdraw").status_code == 200
    d2 = client.get(f"/api/batches/{aid}/series").json()
    assert d2["calibration"]["basis"] == "raw"
    assert d2["calibration"]["applied"] == []
    base = client.get(f"/api/batches/{aid}/series").json()
    assert d2["series"]["guide_bean_temp"] == base["series"]["guide_bean_temp"]
    # history is still there
    ledger = client.get(f"/api/batches/{aid}/calibrations").json()
    assert {c["status"] for c in ledger} == {"superseded", "withdrawn"}


def test_invalid_transitions_and_validation_are_rejected(client):
    aid, _ = _seed(client)
    cal = _create(client, aid)
    # bad ranges / channel / scale
    assert client.post(f"/api/batches/{aid}/calibrations", json={
        "channel": "bean", "t_start_s": 10, "t_end_s": 10, "scale": 1.0}).status_code == 422
    assert client.post(f"/api/batches/{aid}/calibrations", json={
        "channel": "probe-x", "t_start_s": 0, "t_end_s": 10, "scale": 1.0}).status_code == 422
    assert client.post(f"/api/batches/{aid}/calibrations", json={
        "channel": "bean", "t_start_s": 0, "t_end_s": 10, "scale": 0}).status_code == 422
    # withdraw then illegal re-activate
    assert client.post(f"/api/calibrations/{cal['id']}/withdraw").status_code == 200
    assert client.post(f"/api/calibrations/{cal['id']}/activate").status_code == 409
    assert client.post(f"/api/calibrations/{cal['id']}/withdraw").status_code == 409


# --- ⑤ export -> independent recompute with the same version is identical ---

def test_export_recompute_uses_same_calibration_version(client):
    aid, _ = _seed(client)
    _create_active(client, aid, scale=1.05, offset_c=2.0)

    ex = client.get(f"/api/batches/{aid}/export?window_s=30&display_smooth_s=12").json()
    assert ex["calibrations"] and ex["calibrations"][0]["version"] == 1

    rc = client.post("/api/recompute", json={
        "samples": [
            {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"], "env_temp_c": p["env_temp_c"]}
            for p in ex["series"]["raw_points"]
        ],
        "events": ex["events"],
        "params": ex["params"],
        "calibrations": ex["calibrations"],
    })
    assert rc.status_code == 200, rc.text
    rc = rc.json()

    # identical corrected series + metrics from the same calibration version
    assert [p["bean_temp_corrected_c"] for p in rc["series"]["raw_points"]] == [
        p["bean_temp_corrected_c"] for p in ex["series"]["raw_points"]
    ]
    assert rc["series"]["guide_bean_temp"] == ex["series"]["guide_bean_temp"]
    assert [p["ror_c_per_min"] for p in rc["series"]["raw_points"]] == [
        p["ror_c_per_min"] for p in ex["series"]["raw_points"]
    ]
    for key in ["drying_s", "maillard_s", "development_s", "first_crack_window_s",
                "total_s", "development_ratio"]:
        assert rc["metrics"][key] == ex["metrics"][key], key
    assert rc["metrics"]["anchor_temps"] == ex["metrics"]["anchor_temps"]
    assert rc["calibration"]["applied"] == ex["calibration"]["applied"]

    # a fresh export after a "refresh" is deterministic and recomputes the same
    ex2 = client.get(f"/api/batches/{aid}/export?window_s=30&display_smooth_s=12").json()
    assert ex2["series"] == ex["series"] and ex2["calibrations"] == ex["calibrations"]

    # recompute with a conflicting payload is refused, not silently resolved
    conflict = client.post("/api/recompute", json={
        "samples": [], "events": [], "params": {},
        "calibrations": [
            {"channel": "bean", "t_start_s": 0, "t_end_s": 100, "scale": 1.0, "status": "active"},
            {"channel": "bean", "t_start_s": 50, "t_end_s": 150, "scale": 1.1, "status": "active"},
        ],
    })
    assert conflict.status_code == 409
