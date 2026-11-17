"""Every tool call carries its gate outcome (FR-020 / SC-005).

"Why did this run without asking me?" has to be answerable from the run itself.
That means recording an outcome for *every* call, not only the ones that paused
— a call that ran because it was whitelisted, because the user had already
approved it always, or because HITL was switched off entirely are three very
different facts, and telling them apart after the fact is the whole point.

Outcomes live on ``actions_taken`` rather than in ``AuditLog``. A row per tool
call would swamp a table built for administrative actions — a single read-heavy
run makes dozens of calls — while ``actions_taken`` already records every call
with its arguments and source, so the outcome belongs beside them.
"""

from __future__ import annotations

import inspect

import app.services.assignment_engine as engine
from app.tools.approval import (
    OUTCOME_HITL_OFF,
    OUTCOME_STANDING_EXCEPTION,
    OUTCOME_WHITELISTED,
    evaluate_gate,
    fingerprint,
)

FP = fingerprint({"type": "object", "properties": {}})


def _outcome(**overrides) -> str | None:
    kwargs = {
        "hitl_enabled": True,
        "whitelist": frozenset(),
        "active_exceptions": {},
        "current_fingerprint": FP,
    }
    kwargs.update(overrides)
    return evaluate_gate("some_tool", **kwargs).log_outcome


# --- the outcomes themselves are distinguishable -------------------------


def test_switched_off_is_distinguishable_from_whitelisted():
    """Both ran without asking, for completely different reasons.

    Conflating them would hide an operator having disabled gating entirely
    behind what looks like a routine exemption.
    """
    assert _outcome(hitl_enabled=False) == OUTCOME_HITL_OFF
    assert _outcome(whitelist=frozenset({"some_tool"})) == OUTCOME_WHITELISTED
    assert OUTCOME_HITL_OFF != OUTCOME_WHITELISTED


def test_standing_approval_is_distinguishable_from_whitelisted():
    """"This user trusts it" is not "the platform exempts it"."""
    assert (
        _outcome(active_exceptions={"some_tool": FP}) == OUTCOME_STANDING_EXCEPTION
    )
    assert OUTCOME_STANDING_EXCEPTION != OUTCOME_WHITELISTED


def test_a_paused_call_has_no_outcome_yet():
    """It gets one from the human's decision, not from the gate."""
    assert _outcome() is None


# --- the engine records them --------------------------------------------


def _engine_source() -> str:
    # The entries are built by ``_actions_taken``; the run hands it the outcomes.
    run = inspect.getsource(engine.execute_assignment_run)
    assert "outcomes=_gate_outcomes" in run, "the run no longer hands its gate outcomes over"
    return run + inspect.getsource(engine._actions_taken)


def test_every_recorded_action_carries_a_gate_outcome():
    source = _engine_source()

    assert '"gate_outcome"' in source, (
        "actions_taken entries carry no gate outcome — the run cannot answer "
        "why a tool ran without asking"
    )


def test_outcome_falls_back_when_no_gate_was_installed():
    """FR-020 says every call, including runs where gating was off.

    No gate is installed when HITL is off, so nothing populates the outcome map
    and the entry would otherwise be blank for exactly the runs where "why did
    this run unattended?" matters most.

    Asserted through the shared resolver rather than by scraping the engine's
    expression: that expression used to be copied into the chat path too, and
    both copies carried the same blank for loop-handled meta-calls. It now lives
    in one place, so this pins the behaviour and
    ``tests/test_gate_outcome_recording.py`` covers the rest of the table.
    """
    from app.tools.approval import resolve_log_outcome

    assert "resolve_log_outcome" in _engine_source(), (
        "the run path no longer uses the shared resolver — it has either grown "
        "its own copy of the fallback logic or dropped it"
    )
    assert (
        resolve_log_outcome("any_tool", "c1", outcomes={}, hitl_enabled=False)
        == OUTCOME_HITL_OFF
    )


def test_outcomes_are_keyed_by_tool_call_id():
    """A turn can call one tool twice with different arguments and outcomes."""
    source = _engine_source()

    assert "_gate_outcomes[call.id]" in source
    assert "outcomes=_gate_outcomes" in source, (
        "the recorded outcome is no longer looked up per tool_call_id, so two "
        "calls to the same tool in one turn would share a verdict"
    )


def test_human_decisions_become_outcomes_on_resume():
    """An approved or rejected call must not be left blank in the trail."""
    source = inspect.getsource(engine._apply_approval_decisions)

    assert 'outcomes[call_id] = f"human_{verdict}"' in source, (
        "a call resolved by a human records no outcome, so the audit trail "
        "cannot show whether it was approved, edited or rejected"
    )


def test_resume_is_given_the_outcome_sink():
    """The wiring, not just the capability."""
    assert "outcomes=_gate_outcomes" in _engine_source()
