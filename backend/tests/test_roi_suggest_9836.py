"""Suggested unit for a custom agent (#9836, slice R8a, design 10.3).

The suggestion is a deterministic token match over the catalog: the same
agent always gets the same unit, a weak match answers none, ties go to
catalog order, and nothing runs an agent or calls a model.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import roi as roi_api
from app.services import roi, roi_records, roi_suggest
from app.services.roi_suggest import CatalogUnit, suggest_unit

CTEM = roi_suggest.catalog_of(roi.builtin_baselines(xtm_one=True), [])
CORPORATE = roi_suggest.catalog_of(roi.builtin_baselines(xtm_one=False), [])


def test_terms_lower_case_strip_suffixes_and_drop_stop_words():
    assert roi_suggest.terms("Triaged the Threat Reports") == {"triag", "threat", "report"}
    assert roi_suggest.terms("threatReportTriage") == {"threat", "report", "triag"}
    assert roi_suggest.terms(None) == frozenset()


def test_a_threat_report_triage_agent_gets_the_triage_unit():
    found = suggest_unit("Report triage", "Triages incoming threat reports for relevance to our sector.", None, CTEM)
    assert found is not None and found.unit.key == "threat_report_triaged"
    assert {"threat", "report", "triag"} <= set(found.matched_terms)
    assert 0.5 <= found.score <= 1


def test_the_output_property_names_count():
    schema = {
        "type": "object",
        "properties": {
            "detection_rules": {"type": "array", "items": {"type": "object", "properties": {"kqlQuery": {}}}},
            "spl": {"type": "string"},
        },
    }
    assert roi_suggest.output_property_names(schema) == ["detection_rules", "kqlQuery", "spl"]
    assert suggest_unit("Rules helper", "Writes Sigma rules.", None, CTEM) is None
    found = suggest_unit("Rules helper", "Writes Sigma rules.", schema, CTEM)
    assert found is not None and found.unit.key == "detection_rule_written"


def test_a_weak_or_empty_match_suggests_nothing():
    assert suggest_unit("Helper", "Says hello.", None, CTEM) is None
    assert suggest_unit("", None, None, CTEM) is None
    assert suggest_unit("Threat watcher", None, None, CTEM) is None  # one shared term is under the floor


def test_the_same_agent_always_gets_the_same_suggestion():
    args = ("STIX bundle builder", "Builds a STIX bundle from a PDF document.", None, CTEM)
    first = suggest_unit(*args)
    assert first is not None
    assert all(suggest_unit(*args) == first for _ in range(5))


def test_a_tie_goes_to_the_earlier_unit_in_catalog_order():
    a = CatalogUnit(key="alpha_report", name="Weekly report drafted")
    b = CatalogUnit(key="beta_report", name="Weekly report drafted")
    assert suggest_unit("Weekly report drafter", None, None, [a, b]).unit.key == "alpha_report"
    assert suggest_unit("Weekly report drafter", None, None, [b, a]).unit.key == "beta_report"


def test_custom_units_follow_the_built_ins_and_archived_ones_are_left_out():
    live = SimpleNamespace(key="contract_reviewed", name="Contract reviewed", archived_at=None)
    gone = SimpleNamespace(key="old_unit", name="Old unit", archived_at="2026-11-01")
    catalog = roi_suggest.catalog_of(roi.builtin_baselines(xtm_one=False), [live, gone])
    keys = [u.key for u in catalog]
    assert keys[-1] == "contract_reviewed" and "old_unit" not in keys
    assert catalog[-1].builtin is False
    found = suggest_unit("Contract reviewer", "Reviews supplier contracts.", None, catalog)
    assert found is not None and found.unit.key == "contract_reviewed" and found.as_dict()["builtin"] is False


def test_every_built_in_unit_of_both_modes_can_be_matched():
    for catalog in (CTEM, CORPORATE):
        for unit in catalog:
            assert unit.unit_terms, unit.key
            found = suggest_unit(unit.name, None, None, catalog)
            assert found is not None, unit.key


def _patch_api(monkeypatch, agent, declaration=None):
    async def _readable(_db, _user, _type, subject_id):
        return uuid.UUID(subject_id)

    async def _get_declaration(_db, _type, _sid):
        return declaration

    async def _list_units(_db):
        return []

    class _Db:
        async def get(self, _model, _sid):
            return agent

    monkeypatch.setattr(roi_api, "_readable_subject", _readable)
    monkeypatch.setattr(roi_records, "get_declaration", _get_declaration)
    monkeypatch.setattr(roi_records, "list_units", _list_units)
    return _Db()


@pytest.mark.asyncio
async def test_the_api_suggests_for_a_custom_agent(monkeypatch):
    aid = uuid.uuid4()
    agent = SimpleNamespace(
        is_builtin=False, name="Report triage", description="Triages threat reports.", output_schema=None
    )
    db = _patch_api(monkeypatch, agent)
    answer = await roi_api.get_roi_suggestion(agent_id=str(aid), name=None, description=None, db=db, user=None)
    assert answer["declared"] is False
    assert answer["suggestion"]["unit"] in {b.unit for b in roi.BASELINES}


@pytest.mark.asyncio
async def test_the_api_says_declared_and_skips_built_ins(monkeypatch):
    aid = str(uuid.uuid4())
    custom = SimpleNamespace(is_builtin=False, name="Report triage", description=None, output_schema=None)
    db = _patch_api(monkeypatch, custom, declaration=object())
    assert (await roi_api.get_roi_suggestion(agent_id=aid, name=None, description=None, db=db, user=None))[
        "declared"
    ] is True
    builtin = SimpleNamespace(is_builtin=True, name="Report triage", description=None, output_schema=None)
    db = _patch_api(monkeypatch, builtin)
    answer = await roi_api.get_roi_suggestion(agent_id=aid, name=None, description=None, db=db, user=None)
    assert answer == {"agent_id": aid, "declared": True, "suggestion": None}


@pytest.mark.asyncio
async def test_the_api_reads_a_draft_and_refuses_an_empty_call(monkeypatch):
    db = _patch_api(monkeypatch, None)
    answer = await roi_api.get_roi_suggestion(
        agent_id=None, name="STIX bundle builder", description="Builds STIX bundles.", db=db, user=None
    )
    assert answer["agent_id"] is None and answer["suggestion"] is not None
    with pytest.raises(HTTPException) as refused:
        await roi_api.get_roi_suggestion(agent_id=None, name=None, description=None, db=db, user=None)
    assert refused.value.status_code == 422


def test_the_suggestion_starts_no_run_and_calls_no_model():
    source = inspect.getsource(roi_suggest) + inspect.getsource(roi_api.get_roi_suggestion)
    for forbidden in ("execute_agent", "run_agent", "llm", "completion", "litellm"):
        assert forbidden not in source
