"""What gets recorded against a call the gate never decided (FR-020).

FR-020 asks for every tool call and its gate outcome. Three kinds of call never
reach the gate at all, and they must not look alike in the trail:

* the HITL switch was off, so no gate was installed;
* the agent loop answered the call itself and stripped it before dispatch
  (``load_tools``, the tool-gating meta-tool);
* a gate that **failed to install** — the one case that must stay a blank,
  because naming it would hide the only shape a real defect takes here.

The third is why this file exists. ``load_tools`` appears on nearly every turn
where tool gating is active, and it was recording ``None`` — so an auditor
scanning for nulls found the benign case constantly and had no way to tell it
from a gate that never ran. Discovered walking quickstart Scenario 5c, not by
any test, because every test asserted the gate's *decisions* and none asserted
what the absence of a decision looks like.
"""

import pytest

from app.tools.approval import (
    LOOP_HANDLED_TOOLS,
    OUTCOME_HITL_OFF,
    OUTCOME_META_NOT_DISPATCHED,
    OUTCOME_PAUSED,
    OUTCOME_WHITELISTED,
    resolve_log_outcome,
)


def test_a_decision_the_gate_made_wins():
    assert (
        resolve_log_outcome(
            "search_opencti",
            "c1",
            outcomes={"c1": OUTCOME_WHITELISTED},
            hitl_enabled=True,
        )
        == OUTCOME_WHITELISTED
    )


def test_a_pause_is_reported_as_stamped_never_reinferred():
    """``mark_paused`` owns this; the resolver must pass it through untouched."""
    assert (
        resolve_log_outcome(
            "manage_opencti_pir", "c1", outcomes={"c1": OUTCOME_PAUSED}, hitl_enabled=True
        )
        == OUTCOME_PAUSED
    )


def test_the_switch_being_off_outranks_the_meta_case():
    """With HITL off no gate exists for anything, meta-calls included.

    Order matters: reporting ``meta_not_dispatched`` here would describe how the
    call was executed while hiding the fact that nothing was gating that run.
    """
    assert (
        resolve_log_outcome("load_tools", "c1", outcomes={}, hitl_enabled=False)
        == OUTCOME_HITL_OFF
    )


def test_a_loop_handled_meta_call_is_named():
    assert (
        resolve_log_outcome("load_tools", "c1", outcomes={}, hitl_enabled=True)
        == OUTCOME_META_NOT_DISPATCHED
    )


def test_an_undecided_ordinary_call_stays_blank():
    """The blank that must survive.

    A gated tool that ran with no recorded decision means the gate did not
    install. That is a defect, and it is only visible because nothing else in
    this function returns ``None``. If a catch-all is ever added here, this
    assertion is the one that should stop it.
    """
    assert (
        resolve_log_outcome("search_opencti", "c1", outcomes={}, hitl_enabled=True)
        is None
    )


def test_loop_handled_tools_matches_the_defining_constant():
    """Pins the literal against its source, as the constant's docstring promises.

    ``approval.py`` keeps itself free of package imports, so the name is spelled
    twice. A rename in ``tool_gating`` would otherwise silently return these
    rows to the blank this whole file exists to remove.
    """
    from app.tools.tool_gating import LOAD_TOOLS_TOOL_NAME

    assert LOAD_TOOLS_TOOL_NAME in LOOP_HANDLED_TOOLS


def test_every_loop_handled_tool_is_actually_stripped_before_dispatch():
    """A name here that the loop still dispatches would mislabel a real call.

    The claim being recorded is "the gate never saw this because the loop
    answered it". If the loop stopped stripping the call, that sentence becomes
    false and the row would excuse a genuinely ungated dispatch.
    """
    from pathlib import Path

    loop = (
        Path(__file__).resolve().parents[1] / "app" / "tools" / "agent_loop.py"
    ).read_text(encoding="utf-8")
    assert "tc.name != LOAD_TOOLS_TOOL_NAME" in loop, (
        "the agent loop no longer filters load_tools out of the dispatch list. "
        "Either it is gated now (drop it from LOOP_HANDLED_TOOLS) or the filter "
        "moved (re-point this test)."
    )


@pytest.mark.parametrize(
    "outcome",
    [
        OUTCOME_HITL_OFF,
        OUTCOME_WHITELISTED,
        OUTCOME_META_NOT_DISPATCHED,
        OUTCOME_PAUSED,
    ],
)
def test_outcomes_are_distinct_strings(outcome):
    """Two outcomes sharing a value would silently merge in the audit screen."""
    from app.tools import approval

    values = [
        getattr(approval, n) for n in dir(approval) if n.startswith("OUTCOME_")
    ]
    assert values.count(outcome) == 1
