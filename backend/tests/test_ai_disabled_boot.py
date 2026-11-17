"""A platform with AI disabled starts, seeds nothing and dispatches nothing.

US1. SC-001 requires *counting* — an empty UI on a fresh install proves
nothing, because a fresh install is empty either way. What distinguishes the
mode from "AI enabled but misconfigured" is whether anything **tried**.
"""

from __future__ import annotations

import inspect

import pytest

from app.services import autonomous


def test_the_four_ai_schedulers_are_gated():
    """FR-006. Missing one is a job that runs on an AI-disabled platform."""
    source = inspect.getsource(autonomous._run_autonomous_tick_inner)
    for step in (
        "_process_scheduled_assignments",
        "process_scheduled_kb_syncs",
        "process_scheduled_nova_syncs",
        # Evaluation's own gate sits in this step beside the AI one.
        "_run_scheduled_evaluations",
    ):
        call = source.index(f"{step}(db")
        window = source[max(0, call - 120) : call + 60]
        assert "_ai_on" in window, f"{step} dispatches regardless of the AI state"


def test_the_flow_scheduler_is_not_gated():
    """FR-014: deterministic flows keep running, so the scheduler is not gated.

    **This does not mean AI flows are held back.** Per-flow gating is not
    implemented yet (T039-T042), so a graph with an LLM node still dispatches
    on an AI-disabled platform. An earlier version of this docstring claimed
    flow_triggers held them back, which made the absence of a guard here read
    as coverage of a case nothing covers.
    """
    source = inspect.getsource(autonomous._run_autonomous_tick_inner)
    call = source.index("process_scheduled_flows(db)")
    window = source[max(0, call - 120) : call + 60]
    assert "_ai_on" not in window, (
        "gating the flow scheduler stops deterministic automation too"
    )


def test_the_state_is_read_once_per_tick_not_per_process():
    """A module-level or process-lifetime read would make the runtime toggle
    silently useless: the flip would need a restart, which FR-002 forbids.

    Read through the background gate, the AI switch AND the licensed edition,
    never the switch alone."""
    source = inspect.getsource(autonomous._run_autonomous_tick_inner)
    assert source.count("await agentic_refusal(db)") == 1, (
        "read exactly once per tick — more is wasteful, fewer is stale"
    )
    assert "is_ai_enabled" not in source


def test_boot_skips_ai_provider_provisioning(monkeypatch):
    """FR-007: no provider probe, no embedding warmup, nothing that would need
    an LLM or embedding container to be running."""
    from app import main
    from app.services import provisioning

    source = inspect.getsource(main._bootstrap_platform_data)
    # The seeders moved one level down into ``provision_env_providers`` so the
    # runtime toggle creates the same providers boot does (FR-012).
    # Follow the delegation and assert the *call site* is what carries the
    # guard, which is where the guard has to be either way.
    call = source.index("provision_env_providers()")
    assert "if _ai_on:" in source[:call], (
        "provider provisioning runs regardless of the AI state"
    )
    delegated = inspect.getsource(provisioning.provision_env_providers)
    for step in ("_seed_llm_providers_from_env", "_seed_xtm_one_embedding_server_from_env"):
        assert step in delegated, f"{step} is no longer provisioned at all"


def test_non_ai_boot_steps_run_regardless():
    """FR-021. The admin upsert in particular: skipping it leaves a fresh
    AI-disabled install with no admin user and nobody able to log in."""
    from app import main

    source = inspect.getsource(main._bootstrap_platform_data)
    admin_at = source.index("Upsert admin user from ADMIN_EMAIL")
    guard_at = source.index("if _ai_on:")
    tail = source[admin_at:]
    assert admin_at > guard_at
    assert "if _ai_on" not in tail.split("\n")[0], "the admin upsert must not be gated"


@pytest.mark.parametrize(
    "seed_call",
    [
        "seed_builtin_agents",
        "seed_builtin_assignments",
        "seed_builtin_templates",
        "seed_builtin_skills",
        "seed_builtin_integrations",
        "seed_builtin_knowledge_bases",
        # seed_example_flow and ensure_runner_event_types are deliberately NOT
        # here: they are deterministic-flow infrastructure and run regardless
        # (FR-014), asserted by test_deterministic_flow_seeds_are_not_behind_the_ai_gate.
        "seed_builtin_template_containers",
        "seed_builtin_intent_bindings",
        "seed_builtin_datasets",
        "seed_builtin_evaluations",
        "sync_model_prices",
    ],
)
def test_every_seed_step_is_behind_the_ai_guard(seed_call):
    """SC-001: zero rows created. Each step is named individually so a newly
    added one that escapes the guard fails here rather than seeding silently.
    """
    from app import main
    from app.services import provisioning

    boot = inspect.getsource(main._bootstrap_platform_data)
    assert f"{seed_call}(" not in boot, (
        f"{seed_call} is called from boot directly, bypassing the AI guard"
    )
    provisioned = inspect.getsource(provisioning.provision_builtin_content)
    if seed_call in ("seed_builtin_datasets", "seed_builtin_evaluations"):
        # Behind evaluation's own gate too, in the function provisioning calls.
        assert "seed_builtin_evaluation_content(" in provisioned
        provisioned = inspect.getsource(provisioning.seed_builtin_evaluation_content)
    assert f"{seed_call}(" in provisioned


def test_ai_flows_are_held_back_per_flow_not_per_scheduler():
    """The gap this file used to pin is closed (T039-T042).

    The scheduler stays ungated on purpose — gating it would stop deterministic
    flows, which is the opposite of FR-014 — so the assertion is that the AI
    question is asked per flow, in the trigger paths, instead.
    """
    from app.services import autonomous, flow_triggers

    assert hasattr(flow_triggers, "hold_back_if_ai_required")
    # The background gate; its verdict form also says when a refusal is only a
    # state that could not be read, which is not recorded as one.
    source = inspect.getsource(flow_triggers.hold_back_if_ai_required)
    assert "agentic_refusal(" in source or "agentic_verdict(" in source
    # And the comment that used to describe this as a known gap is gone.
    assert "not implemented" not in inspect.getsource(autonomous._run_autonomous_tick_inner)


def test_runner_event_types_seed_before_the_ai_gate():
    """Runner event types are what let `On event` / `Wait for event` bind at
    all, so they must seed with AI disabled too (FR-014)."""
    from app import main
    from app.services import provisioning

    assert "ensure_runner_event_types" not in inspect.getsource(provisioning)
    boot = inspect.getsource(main._bootstrap_platform_data)
    assert boot.index("ensure_runner_event_types") < boot.index("if _ai_on:")


def test_the_example_flow_seeds_after_the_built_in_agents():
    """It is AI content, not deterministic infrastructure — and it has a
    dependency.

    ``seed_example_flow`` resolves the built-in ``opencti-assistant`` by slug to
    wire the CTI playbook's agent node. An earlier version of this file called
    it "deterministic-flow infrastructure" and ran it *before*
    ``seed_builtin_agents``, so on every fresh install the playbook shipped with
    an unassigned agent node — and stayed that way, because ``_seed_one_flow``
    is create-if-absent and never repairs an existing row.
    """
    import ast
    import textwrap

    from app import main

    # Parsed, not grepped: the comment above the gate legitimately *names*
    # ``seed_example_flow`` to say why it is not there, and a substring search
    # finds that mention first.
    tree = ast.parse(
        textwrap.dedent(inspect.getsource(main._bootstrap_platform_data))
    )

    def calls_in(node):
        return {
            n.func.id if isinstance(n.func, ast.Name) else n.func.attr
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name | ast.Attribute)
        }

    ai_branches = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.If) and "_ai_on" in ast.unparse(n.test)
    ]
    assert ai_branches, "no `if _ai_on:` branch found - has boot been restructured?"

    inside = set().union(*(calls_in(b) for b in ai_branches))
    assert "seed_example_flow" in inside, (
        "the example flow wires an agent node, so it must seed inside the AI "
        "branch, after provision_builtin_content()"
    )
    assert "provision_builtin_content" in inside
