"""The CTEM organization context states of the lab (#83, dev:harness-gaps lab-seed-org-context-missing-state)."""

from __future__ import annotations

import pytest

from tests.test_lab_seed_declarations import LAB, _lab_namespace, _lab_source, _services

pytestmark = pytest.mark.skipif(not LAB.is_file(), reason="no .xtm-one next to the backend")

BASE = "http://localhost:3100"
NAMES = (
    "LAB_ORG_PROFILE",
    "LAB_ORG_TECH_STACK",
    "LAB_ORG_COMPLETE_PROFILE",
    "LAB_ORG_SETUP_PROMPT_STATES",
    "LAB_ORG_CONTEXT_STATES",
    "LAB_CTEM_KEV_TAILORED_STATES",
    "LAB_CTEM_OPENCTI_STATES",
    "lab_switch",
    "seed_org_ai_draft_stub",
    "seed_org_context",
)


def _api(state: str, calls: list):
    def api(method, path, token=None, body=None):
        calls.append((method, path, body))
        if path == "/organization-context" and method == "GET":
            return {"version": None}
        if path == "/organization-context" and method == "PUT":
            return {"version": 1}
        if path.startswith("/ctem-metrics/posture"):
            return {"context": {"state": state, "missing_fields": [] if state == "complete" else ["software"]}}
        return {}

    return api


def test_missing_saves_no_profile_and_dismisses_the_setup_dialog():
    calls: list = []
    ns = _lab_namespace(*NAMES, api=_api("missing", calls), env={"LAB_ORG_CONTEXT": "missing"})
    assert ns["seed_org_ai_draft_stub"]("t", BASE)["id"] == "profile version None"
    assert ns["seed_org_context"]("t", BASE) == {"id": "missing, 1 field(s) missing", "url": f"{BASE}/ctem/scoping"}
    assert not [c for c in calls if c[0] == "PUT"]
    assert ("POST", "/organization-context/setup-prompt", {"outcome": "dismissed"}) in calls


def test_complete_saves_every_field_a_ctem_stage_reads():
    from app.services import organization_context as org_context
    from app.services.ctem_context_scoring import CONTEXT_FIELDS

    calls: list = []
    ns = _lab_namespace(*NAMES, api=_api("complete", calls), env={"LAB_ORG_CONTEXT": "complete"})
    assert ns["seed_org_context"]("t", BASE)["id"] == "complete, 0 field(s) missing"
    saved = [c[2]["profile"] for c in calls if c[0] == "PUT"]
    assert saved == [ns["LAB_ORG_COMPLETE_PROFILE"]]
    missing = set(org_context.missing_fields(org_context.parse_profile(saved[0])))
    assert not missing & set(CONTEXT_FIELDS)


def test_a_state_the_posture_does_not_answer_raises_and_default_does_nothing():
    calls: list = []
    ns = _lab_namespace(*NAMES, api=_api("partial", calls), env={"LAB_ORG_CONTEXT": "complete"})
    with pytest.raises(RuntimeError, match="expected the state 'complete'"):
        ns["seed_org_context"]("t", BASE)
    with pytest.raises(RuntimeError, match="default"):
        _lab_namespace(*NAMES, api=_api("partial", calls))["seed_org_context"]("t", BASE)
    assert (
        _lab_namespace(*NAMES, api=_api("partial", calls), env={"LAB_ORG_CONTEXT": "partial"})["seed_org_context"](
            "t", BASE
        )["id"]
        == "partial, 1 field(s) missing"
    )


def test_the_declared_labs_are_the_screens_lab_with_the_switch():
    services = _services()
    screens = services["lab-ctem-screens"]
    for name, switches in {
        "lab-ctem-context-missing": {"LAB_ORG_CONTEXT": "missing"},
        "lab-ctem-context-missing-light": {"LAB_ORG_CONTEXT": "missing", "LAB_THEME": "light"},
        "lab-ctem-context-complete": {"LAB_ORG_CONTEXT": "complete"},
    }.items():
        service = services[name]
        assert service["env"] == {**screens["env"], **switches}, name
        assert {k: v for k, v in service.items() if k not in ("env", "description")} == {
            k: v for k, v in screens.items() if k not in ("env", "description")
        }, name
    assert not any("LAB_ORG_CONTEXT" in s.get("env", {}) for n, s in services.items() if "context" not in n)


def test_every_xtm_one_lab_runs_the_seed_after_the_profile_stub():
    seed = _lab_source("seed_entities")
    assert "seeded[f'org context {state}'] = seed_org_context(token, base)" in seed
    assert "skipped['org context']" in seed
    assert seed.index("seed_org_ai_draft_stub(token, base)") < seed.index("seed_org_context(token, base)")
    assert "lab_switch('LAB_ORG_CONTEXT', LAB_ORG_CONTEXT_STATES) != 'missing'" in _lab_source("seed_org_ai_draft_stub")
