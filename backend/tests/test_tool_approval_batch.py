"""Batch pre-screen for per-tool approval.

The single most dangerous failure this feature can have. When a turn contains
several tool calls and one of them needs approval, gating from inside dispatch
is too late:

* In the parallel path ``asyncio.gather`` has already executed every call by the
  time the pause propagates — so calls after the gated one take effect WITHOUT
  approval, which is precisely what the feature exists to prevent.
* The post-gather loop appends tool results in ``tool_calls`` order and re-raises
  at the first pause, leaving earlier ``tool_use`` blocks with no matching
  ``tool_result``. Strict-mode Anthropic/OpenAI reject that conversation on
  resume, so the run cannot be continued at all.

This is mostly latent today because ``request_human_input`` is the only pausing
tool and is normally called alone. It becomes the common case the moment
arbitrary tools can gate — hence screening the whole turn up front.

These tests drive the real ``CompositeToolExecutor`` and the real gate.
"""

from __future__ import annotations

import pytest

from app.tools.approval import evaluate_gate, fingerprint
from app.tools.base import (
    CallableToolExecutor,
    CompositeToolExecutor,
    HumanInputRequestedError,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
)

SCHEMA = {"type": "object", "properties": {"target": {"type": "string"}}}


def _build(*names: str, whitelist: frozenset[str]):
    executed: list[str] = []
    callable_exec = CallableToolExecutor()
    definitions = []
    for name in names:

        async def _handler(_name=name, **kwargs):
            executed.append(_name)
            return f"{_name} ran"

        callable_exec.register(name, _handler)
        definitions.append(
            ToolDefinition(
                name=name,
                description=f"does {name}",
                input_schema=SCHEMA,
                source="test",
            )
        )

    composite = CompositeToolExecutor()
    composite.add_tools(definitions, callable_exec)

    def gate(call: ToolCall) -> None:
        decision = evaluate_gate(
            call.name,
            hitl_enabled=True,
            whitelist=whitelist,
            active_exceptions={},
            current_fingerprint=fingerprint(SCHEMA),
        )
        if decision.action == "pause":
            raise HumanInputRequestedError(
                question=f"Approve {call.name}?",
                tool_call_id=call.id,
                conversation=[],
            )

    composite.set_approval_gate(gate)
    return composite, executed, definitions


def _calls(*pairs: tuple[str, str]) -> list[ToolCall]:
    return [ToolCall(id=cid, name=name, arguments={}) for cid, name in pairs]


def test_screening_a_clear_batch_returns_no_proposals():
    composite, _executed, _ = _build(
        "search_a", "search_b", whitelist=frozenset({"search_a", "search_b"})
    )

    proposals = composite.screen_for_approval(
        _calls(("c1", "search_a"), ("c2", "search_b"))
    )

    assert proposals == []


def test_screening_never_executes_anything():
    """Screening must be side-effect free — it runs before approval, by design."""
    composite, executed, _ = _build(
        "search_a", "delete_b", whitelist=frozenset({"search_a"})
    )

    composite.screen_for_approval(_calls(("c1", "search_a"), ("c2", "delete_b")))

    assert executed == [], "screening executed a tool"


def test_one_gated_call_surfaces_only_that_proposal():
    composite, _executed, _ = _build(
        "search_a", "delete_b", whitelist=frozenset({"search_a"})
    )

    proposals = composite.screen_for_approval(
        _calls(("c1", "search_a"), ("c2", "delete_b"))
    )

    assert [p["tool_name"] for p in proposals] == ["delete_b"]
    assert proposals[0]["tool_call_id"] == "c2"


def test_all_gated_calls_are_surfaced_together():
    """A reviewer sees the whole turn at once, not one prompt per call."""
    composite, _executed, _ = _build(
        "delete_a", "delete_b", "delete_c", whitelist=frozenset()
    )

    proposals = composite.screen_for_approval(
        _calls(("c1", "delete_a"), ("c2", "delete_b"), ("c3", "delete_c"))
    )

    assert [p["tool_call_id"] for p in proposals] == ["c1", "c2", "c3"]


def test_repeated_calls_to_one_tool_stay_distinct():
    """Proposals key on tool_call_id — a turn can call the same tool twice.

    Matching on tool name instead would collapse these into one decision and
    then apply it to both calls, which may have very different arguments.
    """
    composite, _executed, _ = _build("delete_a", whitelist=frozenset())

    proposals = composite.screen_for_approval(
        _calls(("c1", "delete_a"), ("c2", "delete_a"))
    )

    assert [p["tool_call_id"] for p in proposals] == ["c1", "c2"]


def test_proposal_carries_what_the_reviewer_needs():
    composite, _executed, _ = _build("delete_a", whitelist=frozenset())

    proposal = composite.screen_for_approval(
        [ToolCall(id="c1", name="delete_a", arguments={"target": "prod-db"})]
    )[0]

    assert proposal["tool_description"] == "does delete_a"
    assert proposal["arguments"] == {"target": "prod-db"}
    # The schema travels with the values so the UI can show each argument's
    # own description, not just its name.
    assert proposal["input_schema"] == SCHEMA
    assert proposal["source"] == "test"


def test_aliased_name_is_screened_under_its_canonical_name():
    """A reordered name must not slip past the pre-screen.

    Dispatch would resolve ``a_delete`` to ``delete_a`` and run it, so screening
    on the raw name would gate nothing while the executor still executed.
    """
    composite, _executed, _ = _build("delete_a", whitelist=frozenset())

    proposals = composite.screen_for_approval(_calls(("c1", "a_delete")))

    assert [p["tool_name"] for p in proposals] == ["delete_a"]
    # The ORIGINAL call id is preserved — resume must match the id the model
    # emitted, not one we invented during resolution.
    assert proposals[0]["tool_call_id"] == "c1"


def test_unknown_tool_is_not_screened_as_an_approval():
    """An unresolvable name is a tool error, not something to approve."""
    composite, _executed, _ = _build("delete_a", whitelist=frozenset())

    proposals = composite.screen_for_approval(_calls(("c1", "no_such_tool")))

    assert proposals == []


def test_no_gate_installed_screens_nothing():
    composite, _executed, _ = _build("delete_a", whitelist=frozenset())
    composite.set_approval_gate(None)

    assert composite.screen_for_approval(_calls(("c1", "delete_a"))) == []


def test_scoped_executor_delegates_screening():
    """The scoped wrapper is what the real paths use — it must not drop gating."""
    composite, _executed, definitions = _build(
        "search_a", "delete_b", whitelist=frozenset({"search_a"})
    )
    scoped = ScopedToolExecutor(composite, definitions)

    proposals = scoped.screen_for_approval(
        _calls(("c1", "search_a"), ("c2", "delete_b"))
    )

    assert [p["tool_name"] for p in proposals] == ["delete_b"]


def test_scoped_executor_does_not_screen_refused_calls():
    """A call the scope would refuse never runs, so approving it is meaningless."""
    composite, _executed, definitions = _build(
        "search_a", "delete_b", whitelist=frozenset({"search_a"})
    )
    # Only search_a is in scope; delete_b is registered but not allowed.
    scoped = ScopedToolExecutor(
        composite, [d for d in definitions if d.name == "search_a"]
    )

    proposals = scoped.screen_for_approval(
        _calls(("c1", "search_a"), ("c2", "delete_b"))
    )

    assert proposals == []


@pytest.mark.parametrize("gated_position", [0, 1, 2])
def test_gated_call_anywhere_in_the_batch_is_caught(gated_position):
    """Position independence — the orphaning bug was position-dependent.

    The original hazard only bit when the gated call was NOT last, so a test
    that always gated the final call would have passed against the broken
    behaviour.
    """
    names = ["search_a", "search_b", "search_c"]
    names[gated_position] = "delete_x"
    composite, executed, _ = _build(
        *names, whitelist=frozenset({"search_a", "search_b", "search_c"})
    )

    calls = _calls(*[(f"c{i}", n) for i, n in enumerate(names)])
    proposals = composite.screen_for_approval(calls)

    assert [p["tool_name"] for p in proposals] == ["delete_x"]
    assert executed == [], "a sibling call executed before approval"


# --- A batch may MIX gated and cleared calls ------------------------------
#
# Found in manual testing: the pause only collects calls that FAIL the
# gate. A call that passes it — whitelisted, or covered by a standing approval —
# is never proposed, so the applier gives it no result. The loop then cleared
# the whole call list, dropping it: its ``tool_use`` block went out with no
# matching ``tool_result`` and every provider rejected the conversation
# outright. Latent until a standing approval exists, because until then every
# call in a batch gates and the set of proposals is the set of calls.


def test_screening_a_mixed_batch_proposes_only_the_gated_call():
    composite, _executed, _ = _build(
        "delete_entity", "search_a", whitelist=frozenset({"search_a"})
    )

    proposals = composite.screen_for_approval(
        _calls(("c1", "delete_entity"), ("c2", "search_a"))
    )

    assert [p["tool_name"] for p in proposals] == ["delete_entity"]


def test_the_cleared_call_of_a_mixed_batch_still_needs_dispatching():
    """The invariant the orphaned ``tool_use`` violated: every call in the batch
    ends the turn with a result, whether it was decided by a human or waved
    through by the gate."""
    composite, _executed, _ = _build(
        "delete_entity", "search_a", whitelist=frozenset({"search_a"})
    )
    calls = _calls(("c1", "delete_entity"), ("c2", "search_a"))

    proposals = composite.screen_for_approval(calls)
    proposed = {p["tool_call_id"] for p in proposals}
    # What the loop keeps for normal dispatch once the human has answered.
    remaining = [c for c in calls if c.id not in proposed]

    assert [c.name for c in remaining] == ["search_a"], (
        "the gate cleared this call, so it must still run and return a result — "
        "dropping it leaves its tool_use block orphaned"
    )


def test_the_loop_does_not_discard_the_calls_it_never_proposed():
    """Source guard, matching the loop's other coverage: the fix is a filter,
    and regressing it back to ``tool_calls = []`` is a one-character edit that
    no unit test below the loop would catch."""
    import inspect

    from app.tools import agent_loop

    source = inspect.getsource(agent_loop.run_agent_loop)
    assert "c.id not in _proposed_ids" in source, (
        "calls that passed the gate must survive the pause and be dispatched"
    )
