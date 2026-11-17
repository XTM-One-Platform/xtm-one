"""The additive viz payloads of the CTEM stage heroes (dev-docs/ctem-stage-viz.md, S1).

* Scoping: ``attack_domains``, one entry per core OpenAEV attack domain.
* Validation: ``outcome_matrix``, exercised techniques by kill chain phase for
  whichever frameworks the data carries (never an ATT&CK constant).

Both are viz only: the stage scores read neither, so ``compute_posture`` is
identical with or without them.
"""

from __future__ import annotations

from app.api.v1.ctem_metrics import _stage_viz
from app.services import ctem_metrics_collector as collector
from app.services.ctem_scoring import compute_posture


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


# Two non-ATT&CK frameworks (DISARM for FIMI, ATLAS for AI) and ATT&CK, as OpenAEV lists their phases.
_PHASES = [
    {"phase_id": "d-plan", "phase_name": "Plan", "phase_order": 1, "phase_kill_chain_name": "disarm"},
    {"phase_id": "d-prepare", "phase_name": "Prepare", "phase_order": 2, "phase_kill_chain_name": "disarm"},
    {"phase_id": "d-execute", "phase_name": "Execute", "phase_order": 3, "phase_kill_chain_name": "disarm"},
    {"phase_id": "a-recon", "phase_name": "Reconnaissance", "phase_order": 1, "phase_kill_chain_name": "mitre-atlas"},
    {"phase_id": "a-ml", "phase_name": "ML Model Access", "phase_order": 2, "phase_kill_chain_name": "mitre-atlas"},
    {"phase_id": "m-ia", "phase_name": "Initial Access", "phase_order": 3, "phase_kill_chain_name": "mitre-attack"},
]


def _row(tid, phases, ps=0, pt=0, ds=0, dt=0, ext=""):
    return {
        "attack_pattern_id": tid,
        "attack_pattern_external_id": ext,
        "attack_pattern_name": f"name {tid}",
        "kill_chain_phases": [{"phase_id": p, "phase_name": p} for p in phases],
        "prevention_success": ps,
        "prevention_total": pt,
        "detection_success": ds,
        "detection_total": dt,
    }


def test_outcome_matrix_reads_any_framework_and_defaults_to_the_one_with_most_data():
    rows = [
        _row("t1", ["d-plan"], ps=2, pt=3, ext="T0001"),
        _row("t2", ["d-prepare"], ds=1, dt=2),
        _row("t3", ["d-execute"], pt=4, dt=1),
        _row("t4", ["a-ml"], ps=1, pt=1),
    ]
    matrix = collector._outcome_matrix(rows, _PHASES)

    chains = {c["id"]: c for c in matrix["chains"]}
    assert set(chains) == {"disarm", "mitre-atlas", "mitre-attack"}
    # DISARM has 3 exercised techniques: it is the default and comes first.
    assert matrix["chains"][0]["id"] == "disarm"
    assert matrix["chains"][0]["default"] is True
    assert sum(1 for c in matrix["chains"] if c.get("default")) == 1
    assert chains["disarm"]["count"] == 3
    assert chains["mitre-atlas"]["count"] == 1
    assert chains["mitre-attack"]["count"] == 0
    # Phases in phase order, labelled as the data names them.
    assert [g["label"] for g in chains["disarm"]["groups"]] == ["Plan", "Prepare", "Execute"]
    assert [g["label"] for g in chains["mitre-atlas"]["groups"]] == ["Reconnaissance", "ML Model Access"]

    outcomes = {i["id"]: i["outcome"] for i in matrix["items"]}
    assert outcomes == {"t1": "prevented", "t2": "detected", "t3": "missed", "t4": "prevented"}
    t1 = next(i for i in matrix["items"] if i["id"] == "t1")
    assert t1["prevention"] == {"success": 2, "total": 3}
    assert t1["success"] == 2 and t1["failed"] == 1
    assert t1["code"] == "T0001"


def test_outcome_matrix_none_when_coverage_unreadable_and_empty_items_when_nothing_exercised():
    assert collector._outcome_matrix(None, _PHASES) is None
    matrix = collector._outcome_matrix([], _PHASES)
    assert matrix["items"] == []
    # The frameworks are still listed (with count 0) so the selector can show "no data".
    assert all(c["count"] == 0 for c in matrix["chains"])


def test_outcome_matrix_keeps_a_phase_the_phase_list_missed():
    matrix = collector._outcome_matrix([_row("t9", ["x-unknown"], pt=1)], [])
    assert matrix["chains"][0]["groups"] == [{"id": "x-unknown", "label": "x-unknown"}]
    assert matrix["items"][0]["outcome"] == "missed"


async def test_domain_coverage_breakdown_lists_every_core_domain(monkeypatch):
    async def fake_api(base_url, headers, method, path, action, *, json=None, params=None, client=None):
        if path == "/api/domains":
            return _FakeResp([
                {"domain_id": "u-end", "domain_name": "Endpoint"},
                {"domain_id": "u-cloud", "domain_name": "Cloud"},
            ]), None
        if path == "/api/injector_contracts/domain-counts":
            return _FakeResp([{"domain": "u-end", "count": 12}, {"domain": "u-cloud", "count": 0}]), None
        raise AssertionError(path)

    monkeypatch.setattr(collector, "_api", fake_api)
    covered, total, breakdown = await collector._aev_domain_coverage("http://fake", {}, None)
    assert (covered, total) == (1, 7)
    assert [d["name"] for d in breakdown] == list(collector._AEV_CORE_DOMAINS)
    assert sum(1 for d in breakdown if d["covered"]) == covered
    endpoint = next(d for d in breakdown if d["name"] == "Endpoint")
    assert endpoint == {"name": "Endpoint", "covered": True, "contracts": 12}


async def test_domain_coverage_breakdown_none_when_unresolved(monkeypatch):
    async def fake_api(base_url, headers, method, path, action, *, json=None, params=None, client=None):
        if path == "/api/domains":
            return None, "unavailable"
        return _FakeResp([{"domain": "uuid-x", "count": 3}]), None

    monkeypatch.setattr(collector, "_api", fake_api)
    covered, _total, breakdown = await collector._aev_domain_coverage("http://fake", {}, None)
    assert covered is None and breakdown is None


def test_stage_viz_carries_the_new_payloads_additively():
    domains = [{"name": "Endpoint", "covered": True, "contracts": 3}]
    assert _stage_viz("scoping", {"attack_domains": domains}, "cyber") == {"attack_domains": domains}
    assert _stage_viz("scoping", {}, "cyber") == {}
    matrix = {"chains": [], "items": []}
    buckets = {"PREVENTION": {"SUCCESS": 1}}
    viz = _stage_viz("validation", {"expectation_buckets": buckets, "outcome_matrix": matrix | {"items": [1]}}, "cyber")
    assert viz["expectation_buckets"] == buckets
    assert viz["outcome_matrix"]["items"] == [1]


def test_scores_identical_with_or_without_the_viz_payloads():
    base = {
        "scoping": {"scope_breadth_pct": 75.0, "attack_domains_covered": 3, "attack_domains_total": 7},
        "validation": {"prevention_success_pct": 54.0, "detection_success_pct": 40.0, "finished_simulations": 5},
    }
    extra = {
        "scoping": {**base["scoping"], "attack_domains": [{"name": "Endpoint", "covered": True, "contracts": 1}]},
        "validation": {**base["validation"], "outcome_matrix": collector._outcome_matrix([_row("t1", ["d-plan"], ps=1, pt=1)], _PHASES)},
    }
    present = {"opencti", "openaev"}
    before = compute_posture(base, present, "cyber")
    after = compute_posture(extra, present, "cyber")
    assert before["posture_score"] == after["posture_score"]
    assert [s["score"] for s in before["stages"]] == [s["score"] for s in after["stages"]]
