"""Seeded fleets never gate a tool: gates are decision items, agents decide (#1675, Sam, 24 Nov 2026).

- No built-in gate declares ``tools`` or ``grant``; the tools stay offered
  (``apply_opencti_draft`` by the analyst review's key).
- ``after_gate``: a person's answer never moves the case by code; waited and
  passed keep their bookkeeping.
- ``check_transition``: an answered ``ctem.close_without_retest`` (or a
  person's own move) closes a case with a validation plan.
- ``ctem_gate_order``: a mobilize decision filed ahead of its review is a
  warning, never a refusal.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.agents.builtin_fleet_docs import BUILTIN_FLEET_STRUCTURED, GATE_OFFERED_TOOLS
from app.services import ctem_gate_order as order
from app.services import fleet_autonomy
from app.services.ctem_case_status import after_gate
from app.services.ctem_cases import CaseRuleError, check_transition

NOW = datetime(2026, 11, 24, 6, 0, tzinfo=UTC)


def test_no_builtin_gate_declares_tools_or_grant():
    for slug, structured in BUILTIN_FLEET_STRUCTURED.items():
        for gate in structured.get("gates") or ():
            assert "tools" not in gate, (slug, gate["key"])
            assert "grant" not in gate, (slug, gate["key"])
            assert fleet_autonomy.gated_tool_rules("full_gates", [gate]) == [], (slug, gate["key"])


def test_analyst_review_still_offers_apply_opencti_draft():
    assert GATE_OFFERED_TOOLS["ctem.analyst_review"] == ("apply_opencti_draft",)
    gate = {"key": "ctem.analyst_review", "action": "Review", "impact": "low"}
    assert "apply_opencti_draft" in fleet_autonomy.gate_tool_names([gate])
    assert fleet_autonomy.gate_tool_names([{"key": "ctem.mobilize"}]) == frozenset()


def test_member_prompt_never_says_tool_calls_pause_unenforced():
    block = fleet_autonomy.fleet_autonomy_block("low", enforced=False)
    assert "never a pause inside a run" in block
    assert "pauses the tool calls" not in block


def _case(state: str) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), state=state, state_changed_at=None, updated_at=None)


@pytest.mark.parametrize("key", ["ctem.validation.non_readonly", "ctem.analyst_review", "ctem.mobilize"])
def test_an_answer_never_moves_the_case(key):
    case = _case("waiting_decision")
    assert after_gate(case, NOW, key=key, outcome="answered", by="Ana", actor="agent:x") is None
    assert case.state == "waiting_decision"


def test_a_reject_answer_never_lands_in_validating():
    # The bug #1654 pinned: a rejected validation gate went on to validating.
    case = _case("waiting_decision")
    after_gate(case, NOW, key="ctem.validation.non_readonly", outcome="answered", by="Ana", actor="agent:x")
    assert case.state != "validating"


def test_waited_and_passed_keep_their_bookkeeping():
    case = _case("analyzing")
    event = after_gate(case, NOW, key="ctem.mobilize", outcome="waited", by="Ana", actor="agent:x")
    assert event is not None and case.state == "waiting_decision"
    event = after_gate(case, NOW, key="ctem.mobilize", outcome="passed_automatically", by="", actor="agent:x")
    assert event is not None and case.state != "waiting_decision"


def _transition(**kw):
    base = {
        "current": "remediating",
        "target": "resolved",
        "actor_kind": "agent",
        "reason": "fixed",
        "evidence": "ticket done",
        "now": NOW,
        "validation_plan": True,
        "validations": [],
    }
    base.update(kw)
    return check_transition(**base)


def test_validation_plan_still_closes_only_on_proof_without_a_decision():
    with pytest.raises(CaseRuleError):
        _transition()


def test_answered_close_without_retest_lets_the_agent_close():
    _transition(close_without_retest_answered=True)
    _transition(target="mitigated_watching", evidence=None, close_without_retest_answered=True)


def test_a_person_closes_without_the_retest():
    _transition(actor_kind="user")


def test_mobilize_ahead_of_review_is_a_message_not_a_refusal():
    text = order.mobilize_blocked_message(["ctem:review:abc"])
    assert "ctem:review:abc" in text and "nothing was written" not in text
    assert not hasattr(order, "refuse_mobilize_before_review")


@pytest.mark.asyncio
async def test_warning_helper_ignores_other_keys():
    assert await order.mobilize_before_review_warning(None, "ctem:review:x", True, user_id=None, workspace_id=None) is None
    assert await order.mobilize_before_review_warning(None, "ctem:f2:mobilize:x", False, user_id=None, workspace_id=None) is None
