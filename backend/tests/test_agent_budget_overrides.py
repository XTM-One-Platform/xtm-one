"""Per-agent and per-flow run budgets, and the admin ceiling.

The run budget used to have exactly one configuration surface — the platform
environment. This suite covers the three things the override layer added, in
the order they can break:

* **resolution** — nearest-wins per dimension, so a layer that pins one limit
  does not silently unbound the other three;
* **authority** — a non-admin may tighten anything and raise nothing, which is
  the entire reason the env value exists. Every write path that can produce a
  Budget node has to enforce it, so the gate is tested at the shared helper
  both the agent endpoint and the flow endpoints call;
* **propagation** — a Budget node wired to an agent bounds that agent, and one
  wired to a Flow node bounds every agent of the called flow, down the chain.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.helpers import assert_budget_within_ceiling
from app.services.budget_policy import (
    BudgetOverrides,
    ceiling_violations,
    merge_overrides,
    parse_overrides,
    platform_ceilings,
    resolve_effective,
)
from app.tools.run_budget import build_run_budget


@pytest.fixture
def _budget_settings(monkeypatch):
    """Platform budget settings reset to their shipped defaults (all unbounded).

    Every test states the ceiling it needs, so none of them depends on the
    developer's own ``.env``.
    """
    from app.core.config import settings

    for field, value in (
        ("agent_loop_max_cost_usd", 0.0),
        ("agent_loop_max_total_tokens", 0),
        ("agent_loop_max_tool_calls", 0),
        ("agent_loop_max_duration_seconds", 0),
        ("agent_loop_budget_final_answer_reserve", 0.1),
        ("agent_loop_budget_fallback_enabled", False),
    ):
        monkeypatch.setattr(settings, field, value)
    return settings


def _user(is_admin: bool = False):
    return SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin)


# ── Parsing ──────────────────────────────────────────────────────────────────


def test_zero_reads_as_unset_not_as_a_zero_limit():
    """``0`` is how the platform settings spell "no limit".

    Storing it as a literal ``0`` limit would mean the exact opposite — a
    budget that can never pay for a single call, i.e. every run stopping
    immediately.
    """
    parsed = parse_overrides({"max_cost_usd": 0, "max_total_tokens": "0"})
    assert parsed.max_cost_usd is None
    assert parsed.max_total_tokens is None
    assert parsed.is_empty


def test_garbage_reads_as_unset_rather_than_failing():
    """A JSON column and a node's config dict can hold anything an older UI
    wrote. An unparseable value must defer to the next layer, not break a run.
    """
    parsed = parse_overrides(
        {"max_cost_usd": "not a number", "max_tool_calls": None, "junk": 1}
    )
    assert parsed.is_empty
    assert parse_overrides("nonsense").is_empty
    assert parse_overrides(None).is_empty


def test_reserve_is_clamped_to_the_loops_own_range():
    """A reserve above half the budget would leave the work with less than the
    closing synthesis — the loop clamps it, so the policy layer must agree.
    """
    assert parse_overrides({"final_answer_reserve": 0.9}).final_answer_reserve == 0.5
    assert parse_overrides({"final_answer_reserve": -1}).final_answer_reserve == 0.0


def test_only_set_fields_round_trip():
    """``to_dict`` stores the pins and nothing else, so an agent that cares
    about money alone keeps tracking the platform's other limits instead of
    freezing a copy of them.
    """
    stored = parse_overrides({"max_cost_usd": 0.5}).to_dict()
    assert stored == {"max_cost_usd": 0.5}
    assert parse_overrides(stored).max_cost_usd == 0.5


# ── Resolution ───────────────────────────────────────────────────────────────


def test_nearest_layer_wins_per_dimension():
    """Not per OBJECT: a nearer layer pinning cost must not unbound the tokens
    a further one set. That is the difference between an override and a reset.
    """
    merged = merge_overrides(
        BudgetOverrides(max_cost_usd=0.5),
        BudgetOverrides(max_cost_usd=2.0, max_total_tokens=100_000),
    )
    assert merged.max_cost_usd == 0.5
    assert merged.max_total_tokens == 100_000


def test_the_platform_is_the_last_layer(_budget_settings):
    _budget_settings.agent_loop_max_total_tokens = 100_000
    effective = resolve_effective(BudgetOverrides(max_cost_usd=0.5))
    assert effective.max_cost_usd == 0.5
    assert effective.max_total_tokens == 100_000


def test_an_override_activates_a_budget_on_an_unbounded_platform(_budget_settings):
    """The point of a per-agent budget on a deployment that never set a global
    one: without this the override would resolve, then be discarded because
    "nothing is configured".
    """
    assert build_run_budget() is None
    budget = build_run_budget(overrides=BudgetOverrides(max_tool_calls=25))
    assert budget is not None
    assert budget.max_tool_calls == 25


def test_an_override_can_tighten_a_platform_limit(_budget_settings):
    _budget_settings.agent_loop_max_tool_calls = 100
    budget = build_run_budget(overrides=BudgetOverrides(max_tool_calls=10))
    assert budget is not None and budget.max_tool_calls == 10


def test_the_reserve_falls_back_to_the_platform_value(_budget_settings):
    _budget_settings.agent_loop_budget_final_answer_reserve = 0.25
    budget = build_run_budget(overrides=BudgetOverrides(max_cost_usd=1.0))
    assert budget is not None
    assert budget.final_answer_reserve_fraction == 0.25


# ── Authority ────────────────────────────────────────────────────────────────


def test_a_non_admin_may_always_tighten(_budget_settings):
    _budget_settings.agent_loop_max_cost_usd = 1.0
    assert (
        ceiling_violations(
            BudgetOverrides(max_cost_usd=0.25), is_admin=False
        )
        == []
    )


def test_a_non_admin_may_not_raise_above_the_platform_ceiling(_budget_settings):
    _budget_settings.agent_loop_max_cost_usd = 1.0
    violations = ceiling_violations(
        BudgetOverrides(max_cost_usd=5.0), is_admin=False
    )
    assert [v.key for v in violations] == ["max_cost_usd"]
    # The message quotes both numbers: "refused" without "refused against
    # what" is unactionable for someone who cannot see the env file.
    assert "$5" in violations[0].message()
    assert "$1" in violations[0].message()


def test_an_admin_may_raise_it(_budget_settings):
    _budget_settings.agent_loop_max_cost_usd = 1.0
    assert (
        ceiling_violations(BudgetOverrides(max_cost_usd=5.0), is_admin=True) == []
    )


def test_every_offending_dimension_is_reported_at_once(_budget_settings):
    """Fixing four numbers one 403 at a time is a miserable way to configure a
    budget.
    """
    _budget_settings.agent_loop_max_cost_usd = 1.0
    _budget_settings.agent_loop_max_total_tokens = 100_000
    violations = ceiling_violations(
        BudgetOverrides(max_cost_usd=5.0, max_total_tokens=500_000),
        is_admin=False,
    )
    assert {v.key for v in violations} == {"max_cost_usd", "max_total_tokens"}


def test_an_unchanged_above_ceiling_value_is_not_re_litigated(_budget_settings):
    """An admin raised it once. A non-admin editing the agent's persona sends
    the whole object back, and must not be blocked by a value they never
    touched — otherwise one admin-raised budget locks everyone out of the
    agent.
    """
    _budget_settings.agent_loop_max_cost_usd = 1.0
    stored = BudgetOverrides(max_cost_usd=5.0)
    assert ceiling_violations(stored, previous=stored, is_admin=False) == []
    # Changing it further still needs an admin.
    assert ceiling_violations(
        BudgetOverrides(max_cost_usd=6.0), previous=stored, is_admin=False
    )


def test_setting_a_limit_the_platform_leaves_unbounded_is_a_tightening(
    _budget_settings,
):
    """``0`` on the platform means "no limit", so any finite per-agent limit is
    more restrictive than what applies today. There is no ceiling to exceed.
    """
    assert platform_ceilings().max_cost_usd is None
    assert (
        ceiling_violations(BudgetOverrides(max_cost_usd=999.0), is_admin=False)
        == []
    )


def test_the_ungated_fields_need_no_admin(_budget_settings):
    """None of them can increase what a run spends: the reserve only moves the
    in-loop gate DOWN, and the cheaper-model failsafe is re-priced under the
    same cost ceiling.
    """
    _budget_settings.agent_loop_max_cost_usd = 1.0
    assert (
        ceiling_violations(
            BudgetOverrides(
                final_answer_reserve=0.0,
                fallback_enabled=True,
                fallback_model="cheap-model",
            ),
            is_admin=False,
        )
        == []
    )


# ── The API gate both the agent and the flow endpoints share ─────────────────


def test_the_helper_refuses_a_non_admin_escalation_with_403(_budget_settings):
    _budget_settings.agent_loop_max_cost_usd = 1.0
    with pytest.raises(HTTPException) as excinfo:
        assert_budget_within_ceiling({"max_cost_usd": 5.0}, user=_user())
    assert excinfo.value.status_code == 403
    assert "administrator" in str(excinfo.value.detail)


def test_the_helper_normalises_what_it_stores(_budget_settings):
    """"No override" has ONE representation (NULL), so an empty object must not
    persist as ``{}`` — otherwise "has a budget" becomes ambiguous everywhere
    downstream.
    """
    assert assert_budget_within_ceiling({}, user=_user()) is None
    assert assert_budget_within_ceiling(None, user=_user()) is None
    assert assert_budget_within_ceiling(
        {"max_cost_usd": 0, "max_tool_calls": 5}, user=_user()
    ) == {"max_tool_calls": 5}


def test_the_helper_accepts_a_pydantic_model_too(_budget_settings):
    """The agent endpoint hands it an ``AgentBudgetConfig``; the flow endpoints
    hand it a raw node config dict. One helper, both shapes.
    """
    from app.api.v1.schemas.agent import AgentBudgetConfig

    stored = assert_budget_within_ceiling(
        AgentBudgetConfig(max_tool_calls=7), user=_user()
    )
    assert stored == {"max_tool_calls": 7}


def test_a_flow_budget_node_is_gated_like_an_agent_budget(_budget_settings):
    """A flow is just another place to write a spending policy. If the canvas
    were not gated, wiring a Budget node would be a way around the platform
    ceiling that a plain agent save refuses.
    """
    from app.api.v1.flows import _assert_budget_nodes_authorised

    _budget_settings.agent_loop_max_cost_usd = 1.0
    graph = {"nodes": [_node("b1", "capability.budget", max_cost_usd=5.0)]}
    with pytest.raises(HTTPException) as excinfo:
        _assert_budget_nodes_authorised(graph, None, _user())
    assert excinfo.value.status_code == 403
    # Admin-authored, then re-saved by a non-admin who touched something else.
    _assert_budget_nodes_authorised(graph, graph, _user())
    _assert_budget_nodes_authorised(graph, None, _user(is_admin=True))


# ── Propagation on the canvas ────────────────────────────────────────────────


def _runner_with(graph, *, trigger_context=None):
    """A FlowWalker bound to a graph, with nothing else wired.

    The budget resolvers are pure graph/run reads, so they need no DB, no user
    and no LLM — constructing the real object would drag all three in.
    """
    from app.services.flow_runner import FlowWalker, _nodes_by_id

    runner = FlowWalker.__new__(FlowWalker)
    runner.graph = graph
    runner.nodes = _nodes_by_id(graph)
    runner.run = SimpleNamespace(trigger_context=trigger_context)
    return runner


def _node(node_id: str, kind: str, **config):
    return {
        "id": node_id,
        "kind": kind,
        "position": {"x": 0, "y": 0},
        "config": config,
    }


def _budget_graph(target_id: str, target_kind: str):
    return {
        "nodes": [
            _node("b1", "capability.budget", max_cost_usd=2.0),
            _node(target_id, target_kind),
        ],
        "edges": [
            {
                "id": "e1",
                "source": "b1",
                "source_handle": "provides",
                "target": target_id,
                "target_handle": "budget",
                "kind": "capability",
            }
        ],
    }


def test_a_budget_node_wired_to_an_agent_bounds_that_agent():
    runner = _runner_with(_budget_graph("a1", "agent"))
    assert runner._wired_budget("a1") == {"max_cost_usd": 2.0}


def test_one_budget_node_fans_out_to_several_agents():
    """The sharing IS the wiring — that is what makes a Budget node worth
    having rather than four numbers retyped on every agent.
    """
    graph = _budget_graph("a1", "agent")
    graph["nodes"].append(_node("a2", "agent"))
    graph["edges"].append(
        {
            "id": "e2",
            "source": "b1",
            "source_handle": "provides",
            "target": "a2",
            "target_handle": "budget",
            "kind": "capability",
        }
    )
    runner = _runner_with(graph)
    assert runner._wired_budget("a1") == runner._wired_budget("a2")


def test_a_budget_reaching_a_node_through_the_flow_port_is_ignored():
    """Only the dedicated socket counts. Otherwise a Budget node placed in the
    control-flow path would bound agents nobody wired it to.
    """
    graph = _budget_graph("a1", "agent")
    graph["edges"][0]["target_handle"] = "in"
    assert _runner_with(graph)._wired_budget("a1") is None


def test_an_agent_with_nothing_wired_inherits_the_runs_budget():
    runner = _runner_with(
        {"nodes": [_node("a1", "agent")], "edges": []},
        trigger_context={"budget_overrides": {"max_cost_usd": 3.0}},
    )
    assert runner._wired_budget("a1") is None
    assert runner._inherited_budget() == {"max_cost_usd": 3.0}


def test_an_empty_inherited_budget_reads_as_no_budget():
    """So the agent's own Budget tab still applies, instead of an empty dict
    shadowing it.
    """
    runner = _runner_with(
        {"nodes": [], "edges": []}, trigger_context={"budget_overrides": {}}
    )
    assert runner._inherited_budget() is None
    assert _runner_with({"nodes": [], "edges": []})._inherited_budget() is None


def test_the_flow_call_node_takes_a_budget_socket():
    """Wiring a budget into a Flow node is what hands one ceiling to every
    agent of the called flow — the same node type the agents take.
    """
    from app.services.flow_catalog import PORT_CAP_BUDGET, get_node_kind

    socket = next(
        p for p in get_node_kind("flow.call").inputs if p.id == "budget"
    )
    assert PORT_CAP_BUDGET in socket.types
    assert socket.multiple, "one Budget node must be able to feed several calls"
    runner = _runner_with(_budget_graph("f1", "flow.call"))
    assert runner._wired_budget("f1") == {"max_cost_usd": 2.0}


def test_a_second_budget_on_one_agent_is_flagged():
    """The socket is `multiple` for fan-OUT, not so an agent can carry two
    budgets: the walker takes the first, so the second is a silent no-op
    without this warning.
    """
    from app.services.flow_graph import validate_graph

    graph = _budget_graph("a1", "agent")
    graph["nodes"].append(_node("b2", "capability.budget", max_cost_usd=9.0))
    graph["edges"].append(
        {
            "id": "e2",
            "source": "b2",
            "source_handle": "provides",
            "target": "a1",
            "target_handle": "budget",
            "kind": "capability",
        }
    )
    codes = {i.code for i in validate_graph(graph).warnings}
    assert "agent.multiple_budgets" in codes


# ── The two-clause rule for copies ───────────────────────────────────────────


def test_a_copied_budget_is_lowered_not_refused(_budget_settings):
    """Duplicating an agent, importing one, restoring a version — the caller
    did not type the number, and refusing the whole action would block routine
    work (a duplicate is how you adapt a company-managed agent). Lowering
    prevents the same escalation without the block.
    """
    from app.api.v1.helpers import lower_budget_to_ceiling

    _budget_settings.agent_loop_max_cost_usd = 1.0
    assert lower_budget_to_ceiling(
        {"max_cost_usd": 5.0, "max_tool_calls": 3}, user=_user()
    ) == {"max_cost_usd": 1.0, "max_tool_calls": 3}


def test_an_admins_copy_keeps_its_values(_budget_settings):
    """They could have typed the value anyway, so there is nothing to lower."""
    from app.api.v1.helpers import lower_budget_to_ceiling

    _budget_settings.agent_loop_max_cost_usd = 1.0
    assert lower_budget_to_ceiling({"max_cost_usd": 5.0}, user=_user(is_admin=True)) == {
        "max_cost_usd": 5.0
    }


def test_a_dimension_the_platform_leaves_unbounded_is_not_lowered(_budget_settings):
    """There is no ceiling to lower to — and a finite limit is a tightening."""
    from app.api.v1.helpers import lower_budget_to_ceiling

    assert lower_budget_to_ceiling({"max_cost_usd": 999.0}, user=_user()) == {
        "max_cost_usd": 999.0
    }


def test_a_copied_flow_graph_has_its_budget_nodes_lowered(_budget_settings):
    """Same clause, applied to a canvas. The node's non-budget config must
    survive: lowering a limit is not a licence to drop keys the node carries.
    """
    from app.api.v1.flows import _lower_budget_nodes_to_ceiling

    _budget_settings.agent_loop_max_cost_usd = 1.0
    graph = {
        "nodes": [
            _node("b1", "capability.budget", max_cost_usd=5.0, fallback_enabled=True),
            _node("a1", "agent"),
        ],
        "edges": [],
    }
    _lower_budget_nodes_to_ceiling(graph, _user())
    assert graph["nodes"][0]["config"] == {
        "max_cost_usd": 1.0,
        "fallback_enabled": True,
    }
    # Untouched for an admin, and the agent node is never rewritten.
    admin_graph = {
        "nodes": [_node("b1", "capability.budget", max_cost_usd=5.0)],
        "edges": [],
    }
    _lower_budget_nodes_to_ceiling(admin_graph, _user(is_admin=True))
    assert admin_graph["nodes"][0]["config"]["max_cost_usd"] == 5.0


# ── The agent's budget is a BOUND, not a default ─────────────────────────────
#
# The hole the first cut of this feature left open: authority was only ever
# checked against the platform ceiling, so a flow's Budget node could WIDEN a
# budget an admin had pinned on the agent as long as it stayed under the
# platform value. `budget_config` was advisory and the env value was the only
# real bound — which contradicted every docstring claiming otherwise.


def test_a_flow_node_cannot_widen_an_admin_tightened_agent_budget(_budget_settings):
    """Admin pins the triage bot at $0.10 under a $10.00 platform ceiling; a
    non-admin wires a Budget node at $9.99. The ceiling check passes it (it IS
    under the platform value), so only the resolution layer can stop it.
    """
    from app.services.budget_policy import bound_by

    _budget_settings.agent_loop_max_cost_usd = 10.00
    flow_node = BudgetOverrides(max_cost_usd=9.99)
    agent_own = BudgetOverrides(max_cost_usd=0.10)

    # The write is legitimately allowed — $9.99 is under the platform ceiling.
    assert ceiling_violations(flow_node, is_admin=False) == []
    # ...and the run is still bounded by what the admin set for this agent.
    assert bound_by(flow_node, agent_own).max_cost_usd == 0.10


def test_a_flow_node_may_still_tighten_it(_budget_settings):
    """Tightening is the whole point of a per-flow budget; the bound must not
    turn into "the agent's value always wins".
    """
    from app.services.budget_policy import bound_by

    composed = bound_by(
        BudgetOverrides(max_cost_usd=0.05), BudgetOverrides(max_cost_usd=0.10)
    )
    assert composed.max_cost_usd == 0.05


def test_the_bound_is_per_dimension(_budget_settings):
    """Each dimension is bounded on its own: a node tightening cost must not
    inherit-and-drop a token limit, and a dimension only one layer sets applies.
    """
    from app.services.budget_policy import bound_by

    composed = bound_by(
        BudgetOverrides(max_cost_usd=0.05, max_tool_calls=50),
        BudgetOverrides(max_cost_usd=0.10, max_total_tokens=1_000),
    )
    assert composed.max_cost_usd == 0.05          # node is tighter
    assert composed.max_total_tokens == 1_000     # only the agent sets it
    assert composed.max_tool_calls == 50          # only the node sets it


def test_the_ungated_fields_keep_nearest_wins(_budget_settings):
    """They cannot increase spend, so there is nothing to bound — and a flow
    that wants a cheaper failsafe model must be able to say so.
    """
    from app.services.budget_policy import bound_by

    composed = bound_by(
        BudgetOverrides(fallback_enabled=True, fallback_model="cheap"),
        BudgetOverrides(fallback_enabled=False, fallback_model="other"),
    )
    assert composed.fallback_enabled is True
    assert composed.fallback_model == "cheap"


def test_the_canvas_layers_merge_per_dimension_not_whole(_budget_settings):
    """A Budget node wired to the agent must not drop the dimensions the
    calling flow's budget set — picking one dict over the other silently
    unbounds everything the nearer layer happens not to pin.
    """
    graph = _budget_graph("a1", "agent")
    graph["nodes"][0]["config"] = {"max_cost_usd": 0.5}
    runner = _runner_with(
        graph,
        trigger_context={
            "budget_overrides": {"max_cost_usd": 9.0, "max_total_tokens": 100_000}
        },
    )
    composed = runner._compose_budget("a1")
    assert composed == {"max_cost_usd": 0.5, "max_total_tokens": 100_000}


def test_composing_nothing_reads_as_no_budget(_budget_settings):
    """So the agent's own Budget tab still applies downstream, instead of an
    empty dict shadowing it.
    """
    runner = _runner_with({"nodes": [_node("a1", "agent")], "edges": []})
    assert runner._compose_budget("a1") is None


# ── Re-wiring an above-ceiling Budget node ───────────────────────────────────


def _wire(edge_id: str, source: str, target: str, handle: str = "budget"):
    return {
        "id": edge_id,
        "source": source,
        "source_handle": "provides",
        "target": target,
        "target_handle": handle,
        "kind": "capability",
    }


def _graph_with_two_agents(cost: float, targets: tuple[str, ...]):
    return {
        "nodes": [
            _node("b1", "capability.budget", max_cost_usd=cost),
            _node("a1", "agent"),
            _node("a2", "agent"),
        ],
        "edges": [_wire(f"e{i}", "b1", t) for i, t in enumerate(targets)],
    }


def test_a_non_admin_cannot_rewire_an_above_ceiling_budget_to_more_agents(
    _budget_settings,
):
    """"The wiring IS the distribution" — so gating only the VALUE left an
    admin-authored exception free to be spread across the canvas by anyone:
    identical config, zero value violations, every agent on the ceiling's
    exception.
    """
    from app.api.v1.flows import _assert_budget_nodes_authorised

    _budget_settings.agent_loop_max_cost_usd = 1.0
    stored = _graph_with_two_agents(5.0, ("a1",))
    widened = _graph_with_two_agents(5.0, ("a1", "a2"))

    with pytest.raises(HTTPException) as excinfo:
        _assert_budget_nodes_authorised(widened, stored, _user())
    assert excinfo.value.status_code == 403
    assert "administrator" in str(excinfo.value.detail)


def test_removing_a_target_is_always_allowed(_budget_settings):
    """That only tightens the distribution."""
    from app.api.v1.flows import _assert_budget_nodes_authorised

    _budget_settings.agent_loop_max_cost_usd = 1.0
    stored = _graph_with_two_agents(5.0, ("a1", "a2"))
    narrowed = _graph_with_two_agents(5.0, ("a1",))
    _assert_budget_nodes_authorised(narrowed, stored, _user())


def test_an_admin_may_rewire_it(_budget_settings):
    from app.api.v1.flows import _assert_budget_nodes_authorised

    _budget_settings.agent_loop_max_cost_usd = 1.0
    stored = _graph_with_two_agents(5.0, ("a1",))
    widened = _graph_with_two_agents(5.0, ("a1", "a2"))
    _assert_budget_nodes_authorised(widened, stored, _user(is_admin=True))


def test_a_within_ceiling_budget_node_may_be_wired_freely(_budget_settings):
    """A node any user could have drawn from scratch carries no exception, so
    where it is wired is nobody's business but the author's.
    """
    from app.api.v1.flows import _assert_budget_nodes_authorised

    _budget_settings.agent_loop_max_cost_usd = 10.0
    stored = _graph_with_two_agents(1.0, ("a1",))
    widened = _graph_with_two_agents(1.0, ("a1", "a2"))
    _assert_budget_nodes_authorised(widened, stored, _user())


def test_dropped_override_keys_are_logged(caplog):
    """Fail-open is right for a stored column, but a mistyped key on a flow
    node would otherwise leave a dimension the author believed they had capped
    silently resolving to the next layer. Nothing fails; it must be findable.
    """
    import logging

    # WARNING, not DEBUG: DEBUG is off in production, where a mistyped
    # key has to be findable.
    with caplog.at_level(logging.WARNING, logger="app.services.budget_policy"):
        parsed = parse_overrides({"max_cost": 5.0, "max_tool_calls": "abc"})

    assert parsed.is_empty
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    logged = " ".join(r.getMessage() for r in warned)
    # Quoted: a bare "max_cost" also matches "max_cost_usd" in the known-keys
    # list, which would pass without the unknown key being named at all.
    assert "'max_cost'" in logged, "an unknown key must be named"
    assert "max_tool_calls" in logged, "an unusable value must be named"


def test_an_unknown_key_cannot_forge_a_log_line(caplog):
    """The keys are author-supplied and now reach a production log: a CR / LF
    in one must stay inside its own line, and a huge one must not flood it."""
    import logging

    with caplog.at_level(logging.WARNING, logger="app.services.budget_policy"):
        parse_overrides({"max_cost\nERROR forged line": 5.0, "x" * 10_000: 1})

    [record] = [r for r in caplog.records if "unknown run-budget" in r.getMessage()]
    message = record.getMessage()
    assert "\n" not in message and "\r" not in message
    assert "\\n" in message, "the key is still named, escaped"
    assert len(message) < 1_000


def test_a_non_object_override_stays_at_debug(caplog):
    """A NULL / absent column is routine, not an author error."""
    import logging

    with caplog.at_level(logging.DEBUG, logger="app.services.budget_policy"):
        parse_overrides(None)
        parse_overrides(["max_cost_usd"])

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_zero_override_emits_no_warning(caplog):
    import logging

    with caplog.at_level(logging.DEBUG, logger="app.services.budget_policy"):
        parsed = parse_overrides({"max_cost_usd": 0})

    assert parsed.is_empty
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_zero_is_not_reported_as_dropped(caplog):
    """``0`` is the documented spelling of "no limit", so dropping it is the
    intended reading rather than a mistake worth a log line.
    """
    import logging

    with caplog.at_level(logging.DEBUG, logger="app.services.budget_policy"):
        parse_overrides({"max_cost_usd": 0})

    assert not [r for r in caplog.records if "unusable" in r.getMessage()]
