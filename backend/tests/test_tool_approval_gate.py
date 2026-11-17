"""Unit tests for the approval gate decision table.

``evaluate_gate`` decides whether a single tool call pauses for a human. It is
the safety centre of per-tool HITL, so these tests pin every row of the decision
table plus the two properties the rest of the design leans on:

1. The function is TOTAL — it never raises, for any input combination.
2. The decision does NOT depend on run interactivity. A user's standing approval
   applies to their scheduled runs exactly as it does to interactive ones, and
   a regression here would silently diverge the two.
"""

from app.tools.approval import (
    OUTCOME_HITL_OFF,
    OUTCOME_STANDING_EXCEPTION,
    OUTCOME_WHITELISTED,
    evaluate_gate,
    fingerprint,
)

FP = fingerprint({"type": "object", "properties": {"target": {"type": "string"}}})
OTHER_FP = fingerprint({"type": "object", "properties": {"target": {"type": "int"}}})


def _evaluate(**overrides):
    kwargs = {
        "tool_key": "delete_entity",
        "hitl_enabled": True,
        "whitelist": frozenset(),
        "active_exceptions": {},
        "current_fingerprint": FP,
    }
    kwargs.update(overrides)
    return evaluate_gate(kwargs.pop("tool_key"), **kwargs)


# --- Row 1: the HITL switch is a hard override ---------------------------


def test_hitl_disabled_executes_even_when_not_whitelisted():
    decision = _evaluate(hitl_enabled=False)
    assert decision.action == "execute"
    assert decision.log_outcome == OUTCOME_HITL_OFF


def test_hitl_disabled_beats_a_stale_standing_approval():
    """Switch off wins outright — no pause, and no preference invalidation."""
    decision = _evaluate(hitl_enabled=False, active_exceptions={"delete_entity": OTHER_FP})
    assert decision.action == "execute"
    assert decision.invalidated_preference is False


# --- Row 2: whitelisted tools run --------------------------------------


def test_whitelisted_tool_executes():
    decision = _evaluate(whitelist=frozenset({"delete_entity"}))
    assert decision.action == "execute"
    assert decision.log_outcome == OUTCOME_WHITELISTED


def test_whitelist_is_checked_before_standing_approvals():
    """A whitelisted tool must not be reported as running on a standing approval.

    Both would execute, so this only shows up in the audit trail — but "ran
    because it is whitelisted" and "ran because this user approved it once" are
    different facts and must not be conflated.
    """
    decision = _evaluate(
        whitelist=frozenset({"delete_entity"}),
        active_exceptions={"delete_entity": FP},
    )
    assert decision.log_outcome == OUTCOME_WHITELISTED


# --- Row 3: a matching standing approval runs ---------------------------


def test_matching_standing_approval_executes():
    decision = _evaluate(active_exceptions={"delete_entity": FP})
    assert decision.action == "execute"
    assert decision.log_outcome == OUTCOME_STANDING_EXCEPTION
    assert decision.invalidated_preference is False


def test_standing_approval_for_a_different_tool_does_not_apply():
    decision = _evaluate(active_exceptions={"some_other_tool": FP})
    assert decision.action == "pause"


# --- Row 4: a stale standing approval gates and invalidates -------------


def test_changed_tool_gates_and_flags_the_preference_stale():
    decision = _evaluate(active_exceptions={"delete_entity": OTHER_FP})
    assert decision.action == "pause"
    assert decision.invalidated_preference is True


# --- Row 5: absent from the whitelist gates -----------------------------


def test_unknown_tool_gates_by_default():
    """Absence is the safe state — a tool nobody has considered still pauses."""
    decision = _evaluate(tool_key="a_tool_nobody_has_classified")
    assert decision.action == "pause"
    assert decision.invalidated_preference is False


# --- Properties the rest of the design relies on ------------------------


def test_decision_is_identical_for_interactive_and_autonomous_runs():
    """Interactivity is not an input, and must never become one.

    ``evaluate_gate`` takes no interactivity argument at all, so this test pins
    the contract at the signature level: adding such a parameter would break it.
    """
    import inspect

    params = set(inspect.signature(evaluate_gate).parameters)
    assert "is_interactive" not in params
    assert "interactive" not in params


def test_gate_never_raises_across_the_input_space():
    """Totality. A raise here would surface as a tool failure, not a decision."""
    weird_values = [None, "", "unknown_tool", "delete_entity"]
    for tool_key in weird_values:
        for hitl in (True, False):
            for wl in (frozenset(), frozenset({"delete_entity"}), frozenset({""})):
                for exc in ({}, {"delete_entity": FP}, {None: None}, {"": ""}):
                    decision = evaluate_gate(
                        tool_key,
                        hitl_enabled=hitl,
                        whitelist=wl,
                        active_exceptions=exc,
                        current_fingerprint=FP,
                    )
                    assert decision.action in ("execute", "pause")


# --- Integration-level exemption (FR-021) -------------------------------


def test_exempt_integration_runs_without_pausing():
    """One toggle covers a whole connection.

    Coarser than the whitelist deliberately: per-tool curation across ~2,350
    integration tools is deferred, so the operator's unit of trust is the
    integration itself.
    """
    from app.tools.approval import OUTCOME_INTEGRATION_EXEMPT

    decision = _evaluate(
        tool_source="integration:notion",
        exempt_sources=frozenset({"integration:notion"}),
    )
    assert decision.action == "execute"
    assert decision.log_outcome == OUTCOME_INTEGRATION_EXEMPT


def test_a_different_integration_still_gates():
    decision = _evaluate(
        tool_source="integration:opencti",
        exempt_sources=frozenset({"integration:notion"}),
    )
    assert decision.action == "pause"


def test_exemption_does_not_leak_to_builtin_tools():
    """A source of "" must never accidentally match an exempt entry."""
    decision = _evaluate(
        tool_source="", exempt_sources=frozenset({"", "integration:notion"})
    )
    assert decision.action == "pause"


def test_integration_exemption_is_distinguishable_in_the_audit_trail():
    """"the operator exempted this connection" is not "this tool is whitelisted"."""
    from app.tools.approval import OUTCOME_INTEGRATION_EXEMPT, OUTCOME_WHITELISTED

    assert OUTCOME_INTEGRATION_EXEMPT != OUTCOME_WHITELISTED


def test_hitl_off_still_beats_an_integration_exemption():
    decision = _evaluate(
        hitl_enabled=False,
        tool_source="integration:notion",
        exempt_sources=frozenset({"integration:notion"}),
    )
    assert decision.log_outcome == OUTCOME_HITL_OFF
