"""The CTEM gates that guard no tool call are held by the case tools (#1644).

``ctem.production_change``, ``ctem.accept_risk`` and ``ctem.close_without_retest``
declare no tool, so ``gated_tool_rules`` never paused them and a wait pin was
only stated to the agent. ``ctem_tool_less_gates`` holds them on the case: a
gate that waits is never recorded passed automatically, a case closes without a
passing re-test only once ``ctem.close_without_retest`` is answered, and a case
with a waited gate unanswered refuses every move until the answer is recorded.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.agents.builtin_fleet_docs import CTEM_GATE_KEYS, _ctem_gates
from app.models.ctem_case import CtemCaseEvent
from app.services import ctem_tool_less_gates as tool_less
from app.services.ctem_cases import CaseRuleError
from app.services.fleet_autonomy import PIN_WAIT, gate_waits, gated_tool_rules
from tests.test_ctem_case_tools import AGENT, env  # noqa: F401  (the tools fixture)

NOW = datetime(2026, 11, 24, 5, 0, tzinfo=UTC)


def _gate(key: str, outcome: str, minutes: int) -> CtemCaseEvent:
    return CtemCaseEvent(
        case_id=uuid.uuid4(),
        actor="agent",
        kind="gate",
        at=NOW + timedelta(minutes=minutes),
        from_value=key,
        to_value=outcome,
    )


def test_the_three_gates_are_the_catalog_gates_without_tools() -> None:
    gates = _ctem_gates(launch_tools=True, mobilize_tools=True, mobilize_action="Mobilize")
    tool_less_keys = tuple(g["key"] for g in gates if not g.get("tools"))
    assert tool_less_keys == tool_less.TOOL_LESS_GATES
    assert set(tool_less.TOOL_LESS_GATES) <= set(CTEM_GATE_KEYS)


def test_a_wait_pin_on_a_tool_less_gate_pauses_no_tool() -> None:
    """The gap itself: the pin waits, yet the tool rules hold nothing for it."""
    gate = {"key": "ctem.accept_risk", "impact": "high", "pin": PIN_WAIT}
    assert gate_waits("autonomous", gate)
    assert gated_tool_rules("autonomous", [gate]) == []


def test_a_gate_that_waits_is_never_recorded_passed_automatically() -> None:
    with pytest.raises(CaseRuleError, match=r"ctem.production_change waits"):
        tool_less.check_record("ctem.production_change", "passed_automatically", True)
    tool_less.check_record("ctem.production_change", "waited", True)
    tool_less.check_record("ctem.production_change", "answered", True)
    tool_less.check_record("ctem.production_change", "passed_automatically", False)
    tool_less.check_record("ctem.production_change", "passed_automatically", None)
    # A gate with tools is left to the tool rules.
    tool_less.check_record("ctem.mobilize", "passed_automatically", True)


def test_pending_reads_the_newest_outcome_of_each_gate() -> None:
    history = [
        _gate("ctem.production_change", "waited", 1),
        _gate("ctem.accept_risk", "waited", 2),
        _gate("ctem.accept_risk", "answered", 3),
        _gate("ctem.mobilize", "waited", 4),
    ]
    assert tool_less.pending(history) == ["ctem.production_change"]
    # Dicts (the annotated history) read the same; an older event listed later does not win.
    as_dicts = [
        {
            "kind": "gate",
            "from": "ctem.close_without_retest",
            "to": "answered",
            "at": (NOW + timedelta(minutes=5)).isoformat(),
        },
        {"kind": "gate", "from": "ctem.close_without_retest", "to": "waited", "at": NOW.isoformat()},
    ]
    assert tool_less.pending(as_dicts) == []


def test_a_waited_gate_holds_the_case_until_answered() -> None:
    waiting = tool_less.pending([_gate("ctem.production_change", "waited", 1)])
    with pytest.raises(CaseRuleError, match=r"waits on a person at gate ctem.production_change"):
        tool_less.check_hold(waiting, "record its ticket")
    # The call that records the answer of that gate passes.
    tool_less.check_hold(waiting, "move the case", answering="ctem.production_change")
    tool_less.check_hold([], "record its ticket")


def test_closing_without_a_passing_retest_needs_the_answer_when_the_gate_waits() -> None:
    with pytest.raises(CaseRuleError, match=r"ctem.close_without_retest"):
        tool_less.check_close("resolved", proven=False, waits=True, history=[])
    with pytest.raises(CaseRuleError, match=r"ctem.close_without_retest"):
        tool_less.check_close(
            "mitigated_watching", proven=False, waits=True, history=[_gate("ctem.close_without_retest", "waited", 1)]
        )
    # Answered on the case, or in this very call.
    tool_less.check_close(
        "resolved", proven=False, waits=True, history=[_gate("ctem.close_without_retest", "answered", 1)]
    )
    tool_less.check_close("resolved", proven=False, waits=True, history=[], answering="ctem.close_without_retest")
    # A passing re-test, a gate that runs, an undeclared gate or a non-closing state: nothing to hold.
    tool_less.check_close("resolved", proven=True, waits=True, history=[])
    tool_less.check_close("resolved", proven=False, waits=False, history=[])
    tool_less.check_close("resolved", proven=False, waits=None, history=[])
    tool_less.check_close("remediating", proven=False, waits=True, history=[])
    tool_less.check_close(None, proven=False, waits=True, history=[])


def _fleet(monkeypatch: pytest.MonkeyPatch, *, level: str, pins: dict[str, str]) -> None:
    """The agent's home fleet declares the three tool-less gates at *level* with *pins*."""
    from app.services import fleet_autonomy, fleet_runs

    fleet_id = uuid.uuid4()

    async def _homes(_db: object, _agents: object) -> dict[uuid.UUID, SimpleNamespace]:
        return {uuid.UUID(AGENT): SimpleNamespace(fleet_id=fleet_id)}

    async def _autonomy(_db: object, _fleet: object) -> tuple[str, list[dict[str, str]]]:
        gates = []
        for key in tool_less.TOOL_LESS_GATES:
            gate = {"key": key, "impact": "high"}
            if key in pins:
                gate["pin"] = pins[key]
            gates.append(gate)
        return level, gates

    monkeypatch.setattr(fleet_runs, "home_fleet_defaults_of", _homes)
    monkeypatch.setattr(fleet_autonomy, "fleet_autonomy_for_run", _autonomy)


async def test_a_pinned_gate_is_refused_as_an_automatic_pass(
    env: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fleet(monkeypatch, level="autonomous", pins={"ctem.accept_risk": PIN_WAIT})
    gate = {"key": "ctem.accept_risk", "outcome": "passed_automatically", "by": "level autonomous"}
    refused = await env.call("update_ctem_case", case_id=str(env.case.id), gate=gate)
    assert refused.startswith("Refused: gate ctem.accept_risk waits") and env.db.commits == 0
    # The same gate without the pin runs at Full autonomous.
    _fleet(monkeypatch, level="autonomous", pins={})
    json.loads(await env.call("update_ctem_case", case_id=str(env.case.id), gate=gate))
    assert env.db.commits == 1


async def test_a_waited_gate_holds_the_case_until_the_answer_is_recorded(
    env: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fleet(monkeypatch, level="autonomous", pins={"ctem.production_change": PIN_WAIT})
    case_id = str(env.case.id)
    waited = {"key": "ctem.production_change", "outcome": "waited", "by": "pin wait"}
    json.loads(await env.call("update_ctem_case", case_id=case_id, gate=waited))
    commits = env.db.commits
    for refused in (
        await env.call("update_ctem_case", case_id=case_id, state="remediating", reason="change applied"),
        await env.call("record_ctem_case_ticket", case_id=case_id, integration="jira", key="OPS-1"),
        await env.call("sync_ctem_case_ticket", case_id=case_id, create=True),
    ):
        assert refused.startswith("Refused: the case waits on a person at gate ctem.production_change"), refused
    assert env.db.commits == commits
    # The answer, recorded with the move it allows, releases the case.
    answered = {"key": "ctem.production_change", "outcome": "answered", "by": "user-1", "option": "A"}
    body = json.loads(
        await env.call("update_ctem_case", case_id=case_id, state="remediating", reason="approved", gate=answered)
    )
    assert body["updated"]["state"] == "remediating"
    assert tool_less.pending(r for r in env.db.added if isinstance(r, CtemCaseEvent)) == []


async def test_an_agent_closes_without_a_retest_only_once_the_gate_is_answered(
    env: SimpleNamespace,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fleet(monkeypatch, level="low", pins={"ctem.close_without_retest": PIN_WAIT})
    case_id = str(env.case.id)
    refused = await env.call("update_ctem_case", case_id=case_id, state="resolved", evidence="CHG-1 deployed")
    assert refused.startswith("Refused: closing this case without a passing re-test is gate")
    assert env.case.state == "new" and env.db.commits == 0
    answered = {"key": "ctem.close_without_retest", "outcome": "answered", "by": "user-1"}
    body = json.loads(
        await env.call("update_ctem_case", case_id=case_id, state="resolved", evidence="CHG-1 deployed", gate=answered)
    )
    assert body["updated"]["state"] == "resolved"
