"""Calibration ledger tests — mapped to the acceptance criteria:

1. enabling a non-overlapping calibration rebases corrected curve/RoR/metrics
   while every raw temperature stays exactly the same;
2. overlapping *active* calibrations block analysis and export with a visible
   conflict until an explicit adjudication picks one version;
3. long dropouts and interpolation flags survive calibration — a correction
   can never masquerade as a measurement;
4. withdraw / supersede keep the old versions (and their past results)
   auditable while the current view falls back per the rules;
5. an export recomputed independently with the same calibration version
   yields identical results.
"""
import numpy as np
import pytest
from sqlalchemy.orm import Session

from app import models
from app.analysis import (
    RoRConfig,
    apply_calibrations,
    build_series,
    find_calibration_conflicts,
    guide_value_at,
)


@pytest.fixture(autouse=True)
def clean_calibrations():
    """Isolate the ledger between tests (batches are shared, seeded once)."""
    with Session(models.engine) as s:
        s.query(models.CalibrationHistory).delete()
        s.query(models.Calibration).delete()
        s.commit()
    yield


def _seed(client):
    r = client.post("/api/seed")
    assert r.status_code == 200
    return r.json()[0]["id"]


def _create(client, bid, **kw):
    body = {
        "channel": "bean",
        "valid_from_s": 0,
        "valid_to_s": 120,
        "gain": 1.0,
        "offset": 5.0,
        "created_by": "tester",
        "note": "",
    }
    body.update(kw)
    r = client.post(f"/api/batches/{bid}/calibrations", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _activate(client, cid, acted_by="tester"):
    r = client.post(f"/api/calibrations/{cid}/activate", json={"acted_by": acted_by})
    assert r.status_code == 200, r.text
    return r.json()


def _series(client, bid, **params):
    r = client.get(f"/api/batches/{bid}/series", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _export_body(ex):
    return {
        "samples": [
            {"t_s": p["t_s"], "bean_temp_c": p["bean_temp_c"], "env_temp_c": p["env_temp_c"]}
            for p in ex["series"]["raw_points"]
        ],
        "events": ex["events"],
        "params": ex["params"],
        "calibrations": ex["calibration"]["applied"],
    }


# ---------------------------------------------------------------------------
# ① non-overlapping active calibration: corrected view rebased, raw untouched
# ---------------------------------------------------------------------------

def test_active_calibration_rebases_corrected_view_but_never_raw(client):
    bid = _seed(client)
    before = _series(client, bid)
    assert before["calibration"]["mode"] == "raw"

    cal = _activate(client, _create(client, bid, gain=1.1, offset=2.0,
                                    valid_from_s=0, valid_to_s=120)["id"])

    after = _series(client, bid)
    assert after["calibration"]["mode"] == "corrected"
    assert [c["id"] for c in after["calibration"]["applied"]] == [cal["id"]]
    assert after["calibration"]["applied"][0]["version"] == 1
    assert after["params"]["calibration_ids"] == [cal["id"]]

    pa, pb = before["series"]["raw_points"], after["series"]["raw_points"]
    # raw temperatures identical point by point
    assert [(p["t_s"], p["bean_temp_c"], p["env_temp_c"]) for p in pa] == [
        (p["t_s"], p["bean_temp_c"], p["env_temp_c"]) for p in pb
    ]

    inside = [p for p in pb if p["t_s"] <= 120 and p["bean_temp_c"] is not None]
    outside = [p for p in pb if p["t_s"] > 120 and p["bean_temp_c"] is not None]
    assert inside and outside
    for p in inside:
        assert p["bean_temp_cal_c"] == pytest.approx(1.1 * p["bean_temp_c"] + 2.0)
        assert p["bean_cal_id"] == cal["id"]
    for p in outside:
        assert p["bean_temp_cal_c"] == pytest.approx(p["bean_temp_c"])
        assert p["bean_cal_id"] is None
    # env channel untouched by a bean calibration
    assert all(p["env_cal_id"] is None for p in pb)
    assert all(
        p["env_temp_cal_c"] == p["env_temp_c"]
        for p in pb
        if p["env_temp_c"] is not None
    )

    # RoR follows the new basis: gain scales the slope, offset does not
    for p in pb:
        if p["t_s"] <= 105 and p["ror_c_per_min"] is not None:
            assert p["ror_cal_c_per_min"] == pytest.approx(1.1 * p["ror_c_per_min"], rel=1e-6)
    ror_raw = [p["ror_c_per_min"] for p in pb]
    ror_cal = [p["ror_cal_c_per_min"] for p in pb]
    assert ror_raw != ror_cal

    # metrics carry the calibration basis and rebased anchor temperatures
    m = after["metrics"]
    assert m["calibration_basis"]["mode"] == "corrected"
    assert m["calibration_basis"]["applied"] == [
        {"id": cal["id"], "version": 1, "channel": "bean"}
    ]
    tp = m["anchor_temps_c"]["turning_point"]
    assert tp["cal_bean_c"] == pytest.approx(1.1 * tp["raw_bean_c"] + 2.0, abs=2e-3)
    # durations are pure event intervals — unchanged by calibration
    assert m["drying_s"] == before["metrics"]["drying_s"]
    assert m["development_ratio"] == before["metrics"]["development_ratio"]


def test_draft_calibration_does_not_affect_analysis(client):
    bid = _seed(client)
    _create(client, bid, offset=50.0)  # draft only, never activated
    d = _series(client, bid)
    assert d["calibration"]["mode"] == "raw"
    assert d["calibration"]["applied"] == []
    for p in d["series"]["raw_points"]:
        if p["bean_temp_c"] is not None:
            assert p["bean_temp_cal_c"] == p["bean_temp_c"]


# ---------------------------------------------------------------------------
# ② overlapping active calibrations: visible conflict, explicit adjudication
# ---------------------------------------------------------------------------

def test_overlapping_active_calibrations_block_until_adjudicated(client):
    bid = _seed(client)
    c1 = _activate(client, _create(client, bid, valid_from_s=100, valid_to_s=300,
                                   offset=3.0)["id"])
    c2 = _activate(client, _create(client, bid, valid_from_s=200, valid_to_s=400,
                                   offset=-2.0)["id"])

    # analysis blocked with a visible conflict
    r = client.get(f"/api/batches/{bid}/series")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "calibration_conflict"
    conf = detail["conflicts"][0]
    assert conf["channel"] == "bean"
    assert conf["overlap_from_s"] == 200 and conf["overlap_to_s"] == 300
    assert {c1["id"], c2["id"]} == {c["id"] for c in conf["candidates"]}
    versions = {c["id"]: c["version"] for c in conf["candidates"]}
    assert versions == {c1["id"]: 1, c2["id"]: 1}

    # export blocked too
    assert client.get(f"/api/batches/{bid}/export").status_code == 409

    # a selection that still overlaps is not a valid adjudication
    r = client.get(f"/api/batches/{bid}/series?cal_ids={c1['id']},{c2['id']}")
    assert r.status_code == 400
    # unknown / non-active ids rejected
    assert client.get(f"/api/batches/{bid}/series?cal_ids=99999").status_code == 400

    # explicit adjudication: only the chosen version is used
    d = client.get(f"/api/batches/{bid}/series?cal_ids={c2['id']}").json()
    assert [c["id"] for c in d["calibration"]["applied"]] == [c2["id"]]
    assert d["calibration"]["selection"] == [c2["id"]]
    p = next(
        p for p in d["series"]["raw_points"]
        if 250 <= p["t_s"] <= 280 and p["bean_temp_c"] is not None
    )
    assert p["bean_temp_cal_c"] == pytest.approx(p["bean_temp_c"] - 2.0)
    assert p["bean_cal_id"] == c2["id"]
    # c1's range alone (100-200) is NOT corrected — selection is exactly c2
    q = next(
        p for p in d["series"]["raw_points"]
        if 120 <= p["t_s"] <= 150 and p["bean_temp_c"] is not None
    )
    assert q["bean_temp_cal_c"] == pytest.approx(q["bean_temp_c"])

    # export works under the same adjudication and records it
    ex = client.get(f"/api/batches/{bid}/export?cal_ids={c2['id']}").json()
    assert ex["params"]["calibration_ids"] == [c2["id"]]
    assert ex["calibration"]["selection"] == [c2["id"]]

    # withdrawing one side also resolves the conflict (state-based ruling)
    r = client.post(f"/api/calibrations/{c2['id']}/withdraw", json={"acted_by": "tester"})
    assert r.status_code == 200
    d = _series(client, bid)
    assert [c["id"] for c in d["calibration"]["applied"]] == [c1["id"]]


# ---------------------------------------------------------------------------
# ③ gaps and interpolation flags survive calibration
# ---------------------------------------------------------------------------

def test_calibration_never_disguises_gaps_or_interpolation(client):
    bid = _seed(client)
    raw_view = _series(client, bid)
    cal = _activate(client, _create(client, bid, valid_from_s=100, valid_to_s=500,
                                    gain=1.05, offset=1.0)["id"])
    d = _series(client, bid)
    pts = d["series"]["raw_points"]

    # missing readings stay missing in BOTH bases; no corrected RoR there
    missing = [p for p in pts if p["bean_temp_c"] is None]
    assert missing, "seeded batch must contain dropouts"
    for p in missing:
        assert p["bean_temp_cal_c"] is None
        assert p["ror_cal_c_per_min"] is None
        assert p["bean_cal_id"] is None

    # gap audit is byte-identical to the raw view (calibration adds nothing)
    assert d["series"]["missing_segments"] == raw_view["series"]["missing_segments"]
    assert d["series"]["interpolated_t_s"] == raw_view["series"]["interpolated_t_s"]

    # interpolated points keep their non-measured identity under correction
    interp = [p for p in pts if p["is_interpolated"]]
    assert interp
    for p in interp:
        assert p["bean_temp_cal_c"] is None

    # wide dropout still breaks the corrected guide line
    wide = next(
        g for g in d["series"]["missing_segments"]
        if g["channel"] == "bean" and g["status"] == "wide_unfilled"
    )
    idx_wide = [
        i for i, p in enumerate(pts)
        if wide["t_start_s"] <= p["t_s"] <= wide["t_end_s"]
    ]
    assert idx_wide
    assert all(d["series"]["guide_bean_cal_temp"][i] is None for i in idx_wide)

    # short gap is bridged in the corrected guide but stays flagged
    short = next(
        g for g in d["series"]["missing_segments"]
        if g["channel"] == "bean" and g["status"] == "interpolated"
    )
    idx_short = [
        i for i, p in enumerate(pts)
        if short["t_start_s"] <= p["t_s"] <= short["t_end_s"]
    ]
    assert idx_short
    assert all(d["series"]["guide_bean_cal_temp"][i] is not None for i in idx_short)
    assert all(pts[i]["is_interpolated"] for i in idx_short)
    # and the corrected bridge interpolates the *corrected* endpoints
    assert cal["id"] is not None


# ---------------------------------------------------------------------------
# ④ withdraw / supersede: fallback for the current view, full auditability
# ---------------------------------------------------------------------------

def test_withdraw_and_supersede_fall_back_but_stay_auditable(client):
    bid = _seed(client)
    v1 = _activate(client, _create(client, bid, offset=10.0, valid_from_s=0,
                                   valid_to_s=120)["id"])
    d1 = _series(client, bid)
    assert [c["id"] for c in d1["calibration"]["applied"]] == [v1["id"]]
    ex1 = client.get(f"/api/batches/{bid}/export").json()
    assert ex1["calibration"]["applied"][0]["version"] == 1

    # supersede v1 -> v2 created as DRAFT; current view falls back to raw
    r = client.post(
        f"/api/calibrations/{v1['id']}/supersede",
        json={"channel": "bean", "valid_from_s": 0, "valid_to_s": 120,
              "gain": 1.0, "offset": 20.0, "created_by": "reviewer",
              "note": "零点复测后修正"},
    )
    assert r.status_code == 201, r.text
    v2 = r.json()
    assert v2["version"] == 2 and v2["status"] == "draft"
    assert v2["supersedes_id"] == v1["id"]

    d2 = _series(client, bid)
    assert d2["calibration"]["mode"] == "raw"
    assert d2["calibration"]["applied"] == []
    for p in d2["series"]["raw_points"]:
        if p["bean_temp_c"] is not None:
            assert p["bean_temp_cal_c"] == p["bean_temp_c"]

    # old version remains fully auditable in the ledger
    ledger = client.get(f"/api/batches/{bid}/calibrations").json()
    old = next(c for c in ledger if c["id"] == v1["id"])
    assert old["status"] == "superseded"
    assert old["superseded_by_id"] == v2["id"]
    transitions = [(h["from_status"], h["to_status"]) for h in old["history"]]
    assert (None, "draft") in transitions
    assert ("draft", "active") in transitions
    assert ("active", "superseded") in transitions

    # results the old version produced stay reproducible from the old export
    rc = client.post("/api/recompute", json=_export_body(ex1)).json()
    sig = lambda pts: [(p["t_s"], p["bean_temp_cal_c"], p["ror_cal_c_per_min"]) for p in pts]
    assert sig(rc["series"]["raw_points"]) == sig(ex1["series"]["raw_points"])
    assert rc["calibration"]["applied"][0]["id"] == v1["id"]

    # activate v2 -> the new basis applies
    _activate(client, v2["id"], acted_by="reviewer")
    d3 = _series(client, bid)
    assert [c["id"] for c in d3["calibration"]["applied"]] == [v2["id"]]
    p = next(p for p in d3["series"]["raw_points"]
             if p["t_s"] <= 100 and p["bean_temp_c"] is not None)
    assert p["bean_temp_cal_c"] == pytest.approx(p["bean_temp_c"] + 20.0)

    # withdraw the current calibration -> view falls back to raw again
    r = client.post(f"/api/calibrations/{v2['id']}/withdraw",
                    json={"acted_by": "reviewer", "note": "复测存疑"})
    assert r.status_code == 200
    d4 = _series(client, bid)
    assert d4["calibration"]["mode"] == "raw"
    ledger = client.get(f"/api/batches/{bid}/calibrations").json()
    gone = next(c for c in ledger if c["id"] == v2["id"])
    assert gone["status"] == "withdrawn"
    assert [(h["from_status"], h["to_status"]) for h in gone["history"]][-1] == (
        "active", "withdrawn")


def test_calibration_lifecycle_rules(client):
    bid = _seed(client)
    c = _create(client, bid)
    assert c["status"] == "draft" and c["version"] == 1
    assert c["history"][0]["to_status"] == "draft"

    _activate(client, c["id"])
    # illegal transitions are rejected, nothing is silently rewritten
    assert client.post(f"/api/calibrations/{c['id']}/activate", json={}).status_code == 409
    d = _create(client, bid, note="second draft")
    r = client.post(f"/api/calibrations/{d['id']}/supersede", json={
        "channel": "bean", "valid_from_s": 0, "valid_to_s": 60, "created_by": "x"})
    assert r.status_code == 409  # only an active record can be superseded
    r = client.post(f"/api/calibrations/{c['id']}/withdraw", json={})
    assert r.status_code == 200
    assert client.post(f"/api/calibrations/{c['id']}/withdraw", json={}).status_code == 409
    # ledger keeps everything
    ledger = client.get(f"/api/batches/{bid}/calibrations").json()
    assert len(ledger) == 2


def test_calibration_validation(client):
    bid = _seed(client)
    bad = [
        {"channel": "probe1"},                       # unknown channel
        {"valid_from_s": 100, "valid_to_s": 100},    # empty range
        {"valid_from_s": 200, "valid_to_s": 100},    # inverted range
        {"gain": 0},                                 # non-positive gain
        {"gain": -1.2},
        {"formula": "quadratic"},                    # unsupported formula
        {"created_by": "  "},                        # creator required
    ]
    for patch in bad:
        body = {"channel": "bean", "valid_from_s": 0, "valid_to_s": 60,
                "created_by": "tester", **patch}
        r = client.post(f"/api/batches/{bid}/calibrations", json=body)
        assert r.status_code == 422, (patch, r.text)


# ---------------------------------------------------------------------------
# ⑤ export + independent recompute with the same calibration version
# ---------------------------------------------------------------------------

def test_export_recompute_with_same_calibration_version_is_identical(client):
    bid = _seed(client)
    cal = _activate(client, _create(client, bid, gain=1.08, offset=-1.5,
                                    valid_from_s=50, valid_to_s=350)["id"])
    ex1 = client.get(f"/api/batches/{bid}/export").json()
    # "刷新后" — a second, independent export must be byte-identical
    ex2 = client.get(f"/api/batches/{bid}/export").json()
    assert ex1["series"]["raw_points"] == ex2["series"]["raw_points"]
    assert ex1["calibration"]["applied"] == ex2["calibration"]["applied"]
    assert ex1["metrics"] == ex2["metrics"]

    r = client.post("/api/recompute", json=_export_body(ex1))
    assert r.status_code == 200, r.text
    rc = r.json()
    # same calibration version was used
    assert [c["id"] for c in rc["calibration"]["applied"]] == [cal["id"]]
    assert rc["calibration"]["applied"][0]["version"] == 1
    # corrected view, raw view and RoR reproduce point by point
    sig = lambda pts: [
        (p["t_s"], p["bean_temp_c"], p["env_temp_c"], p["bean_temp_cal_c"],
         p["env_temp_cal_c"], p["ror_c_per_min"], p["ror_cal_c_per_min"],
         p["is_interpolated"], p["bean_cal_id"])
        for p in pts
    ]
    assert sig(rc["series"]["raw_points"]) == sig(ex1["series"]["raw_points"])
    assert rc["series"]["guide_bean_cal_temp"] == ex1["series"]["guide_bean_cal_temp"]
    # every stage metric reproduces
    assert rc["metrics"] == ex1["metrics"]


def test_recompute_rejects_conflicting_calibration_payload(client):
    bid = _seed(client)
    ex = client.get(f"/api/batches/{bid}/export").json()
    body = _export_body(ex)
    body["calibrations"] = [
        {"id": 1, "channel": "bean", "valid_from_s": 0, "valid_to_s": 100,
         "params": {"gain": 1.0, "offset": 1.0}},
        {"id": 2, "channel": "bean", "valid_from_s": 50, "valid_to_s": 150,
         "params": {"gain": 1.0, "offset": 2.0}},
    ]
    r = client.post("/api/recompute", json=body)
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "calibration_conflict"


# ---------------------------------------------------------------------------
# pure-function units
# ---------------------------------------------------------------------------

def test_apply_calibrations_pure_nan_safe_and_unchained():
    t = np.array([0.0, 10.0, 20.0, 30.0, 40.0])
    temp = np.array([100.0, np.nan, 120.0, 130.0, 140.0])
    cals = [
        {"id": 7, "channel": "bean", "valid_from_s": 10, "valid_to_s": 30,
         "params": {"gain": 2.0, "offset": 1.0}},
    ]
    out, ids = apply_calibrations(t, temp, cals)
    assert out[0] == 100.0 and ids[0] is None          # outside range untouched
    assert np.isnan(out[1]) and ids[1] is None         # NaN stays NaN
    assert out[2] == 241.0 and ids[2] == 7             # 2*120+1
    assert out[4] == 140.0 and ids[4] is None


def test_find_calibration_conflicts_groups_and_channels():
    mk = lambda i, ch, a, b: {"id": i, "channel": ch, "valid_from_s": a, "valid_to_s": b}
    conflicts = find_calibration_conflicts([
        mk(1, "bean", 0, 100), mk(2, "bean", 50, 150),   # overlap on bean
        mk(3, "env", 50, 150),                            # env: alone, fine
        mk(4, "bean", 200, 250),                          # disjoint, fine
    ])
    assert len(conflicts) == 1
    assert conflicts[0]["channel"] == "bean"
    assert {c["id"] for c in conflicts[0]["candidates"]} == {1, 2}
    assert conflicts[0]["overlap_from_s"] == 50
    assert conflicts[0]["overlap_to_s"] == 100
    # touching ranges (inclusive ends) also conflict
    assert find_calibration_conflicts([mk(1, "bean", 0, 100), mk(2, "bean", 100, 200)])
    # disjoint -> none
    assert not find_calibration_conflicts([mk(1, "bean", 0, 99), mk(2, "bean", 100, 200)])


def test_build_series_calibration_leaves_raw_basis_identical():
    samples = [
        {"t_s": float(i * 10), "bean_temp_c": 100.0 + i, "env_temp_c": 200.0 + i}
        for i in range(20)
    ]
    samples[5]["bean_temp_c"] = None  # dropout inside the calibrated range
    cals = [{"id": 3, "channel": "bean", "valid_from_s": 0, "valid_to_s": 100,
             "params": {"gain": 1.2, "offset": 0.5}, "version": 1}]
    plain = build_series(samples, ror_cfg=RoRConfig(), max_gap_fill_s=45)
    cal = build_series(samples, ror_cfg=RoRConfig(), max_gap_fill_s=45, calibrations=cals)
    sig = lambda pts: [(p["t_s"], p["bean_temp_c"], p["env_temp_c"]) for p in pts]
    assert sig(plain["raw_points"]) == sig(cal["raw_points"])
    assert plain["missing_segments"] == cal["missing_segments"]
    assert cal["calibration"]["mode"] == "corrected"
    p = cal["raw_points"][5]
    assert p["bean_temp_c"] is None and p["bean_temp_cal_c"] is None


def test_guide_value_at_honours_gaps():
    t = np.array([0.0, 10.0, 20.0, 30.0])
    guide = [100.0, 110.0, None, 130.0]
    assert guide_value_at(t, guide, 5.0) == 105.0
    assert guide_value_at(t, guide, 15.0) is None   # inside a break
    assert guide_value_at(t, guide, 40.0) is None   # outside range
    assert guide_value_at(t, guide, 10.0) == 110.0
