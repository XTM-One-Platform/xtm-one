"""Assignment runs must offer ``spawn_background_task`` like chat does.

Self-fan-out (``spawn_background_task(agent_id="self")``) was wired only in the
chat path: ``agent_chat.py`` passed ``self_agent`` / ``allow_self_fanout`` to
``build_sub_agent_tools`` while the assignment engine built the sub-agent tools
inside the ``if agent.handover_agent_ids:`` block, without the self-fan-out
arguments.  An agent with no delegate-typed peers therefore had NO background
task tools at all in assignment runs — while the exact same agent had them in
chat.

These are source-contract tests (the repo's established pattern for the
engine's giant ``execute_assignment_run`` body — see
``test_assignment_engine_agent_builder.py``): they pin the gate, the
self-fan-out arguments, the prompt guidance, and — for the quota question —
that background sub-agent tasks stay metered (``meter_quota`` default True)
while nested consult/transfer stay unmetered.
"""

import inspect
import re

import app.services.assignment_engine as assignment_engine
import app.services.sub_agent as sub_agent


def _engine_source() -> str:
    return inspect.getsource(assignment_engine.execute_assignment_run)


def _background_task_block(source: str) -> str:
    """The engine segment from the self-fan-out gate to the file-context step."""
    assert "_self_fanout_allowed" in source, (
        "execute_assignment_run must compute _self_fanout_allowed "
        "(chat/assignment parity for spawn_background_task)"
    )
    block = source.split("_self_fanout_allowed = (", 1)[1]
    return block.split("Inject file context", 1)[0]


def test_engine_builds_sub_agent_tools_with_self_fanout():
    """The engine must pass self_agent + allow_self_fanout so an agent can
    spawn parallel copies of itself in an assignment run, not only in chat."""
    block = _background_task_block(_engine_source())
    assert "build_sub_agent_tools" in block
    assert "self_agent=agent" in block
    assert "allow_self_fanout=_self_fanout_allowed" in block


def test_engine_gate_does_not_require_peer_handover_agents():
    """The background-task block must run for ``_self_fanout_allowed OR
    _needs_delegate`` (or an agent-builder agent's owned targets) — it
    must NOT live inside the peers-only ``if agent.handover_agent_ids:`` block
    (the original bug) — and the whole block must additionally require a
    user to record the tasks under (``SubAgentTask.user_id`` is a users FK, so
    an ownerless run could never spawn; don't advertise a broken tool): the
    effective user, or for the Concierge acting alone its agent's owner of
    record (``identity.concierge_delegation_owner_id``)."""
    source = _engine_source()
    block = _background_task_block(source)
    assert "_delegation_user_id = effective_user_id or await concierge_delegation_owner_id(db, agent)" in source
    assert re.search(
        r"if \(\s*_self_fanout_allowed or _needs_delegate"
        r"(?: or _owned_resolver is not None)?\s*\) "
        r"and _delegation_user_id is not None:",
        block,
    ), (
        "the background-task tools must be injected when self-fan-out is "
        "enabled even with zero peer handover agents, but only when the run "
        "has a resolvable effective user"
    )
    # The build call must come AFTER the peers-only handover try/except ends.
    peers_block_end = source.index("Failed to load handover tools for assignment")
    build_call = source.index("build_sub_agent_tools")
    assert build_call > peers_block_end, (
        "build_sub_agent_tools must be called outside the "
        "`if agent.handover_agent_ids:` block"
    )


def test_engine_honors_self_fanout_setting():
    """Self-fan-out in assignments must respect AGENT_SELF_FANOUT_ENABLED and
    the agent's own ``allow_self_fanout``, both read by ``self_fanout_enabled``."""
    block = _background_task_block(_engine_source())
    assert "self_fanout_enabled(agent)" in block


def test_engine_injects_self_fanout_prompt_before_safety_anchor():
    """The conservative guidance must be injected like the delegation block:
    BEFORE the trailing safety anchor, never appended after it.  The engine
    delegates the insert to ``prompt_guard.insert_before_safety_anchor``
    (single source of truth for the anchor contract)."""
    block = _background_task_block(_engine_source())
    assert "format_self_fanout_prompt" in block
    assert "insert_before_safety_anchor" in block


def test_engine_uses_effective_user_identity_for_spawn():
    """SubAgentTask.user_id is a users FK — the spawn identity must be the
    run's effective user (run owner, falling back to the agent owner), the
    same identity used for quota accounting and the agent-builder tool, or,
    for the Concierge acting alone, its agent's owner of record.
    The old ``agent.id`` fallback (a guaranteed FK violation) must be gone."""
    block = _background_task_block(_engine_source())
    call = block.split("build_sub_agent_tools(", 1)[1].split("composite.add_tools", 1)[0]
    assert "user_id=str(_delegation_user_id)" in call
    assert "agent.id" not in call.split("parent_agent_id", 1)[0], (
        "the user_id argument must never fall back to agent.id — "
        "SubAgentTask.user_id is a users FK"
    )


def test_background_tasks_are_quota_metered():
    """Each background sub-agent task must consume one agentic execution:
    ``_execute_sub_agent_task`` calls ``run_agent_chat`` with the default
    ``meter_quota=True`` (background tasks are real autonomous work units,
    per the OBS-3 contract in agent_chat.py)."""
    source = inspect.getsource(sub_agent._execute_sub_agent_task)
    assert "run_agent_chat(" in source
    assert "meter_quota=False" not in source, (
        "background sub-agent tasks must stay quota-metered — only nested "
        "consult/transfer handovers are exempt (OBS-3)"
    )


def test_nested_consult_and_transfer_stay_unmetered_in_assignments():
    """The parent assignment run is already metered — nested consult/transfer
    calls must keep passing meter_quota=False so one logical request is
    metered exactly once (OBS-3)."""
    source = _engine_source()
    assert source.count("meter_quota=False") >= 2, (
        "assignment-path consult_agent and transfer_to_agent must both pass "
        "meter_quota=False"
    )
