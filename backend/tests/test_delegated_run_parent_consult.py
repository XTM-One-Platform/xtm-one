"""A delegated background task asks its direct parent a bounded question.

Agent A spawns a background task on agent B. The chain guard refuses every
handover from B back to A (A is in the visited chain), except one: B may
``consult_agent`` A, its direct parent, ``DELEGATED_PARENT_CONSULT_LIMIT``
times per run. A answers with no handover tool and a short iteration budget;
a background task back to A stays refused.

The chat-path tests drive the real ``run_agent_chat`` of both agents through
the harness of ``test_handover_consult_chain`` (only the model loop is
scripted); the turn of B is started as the sub-agent worker starts it, with a
``sub_agent_task`` handover context naming the agent that spawned it.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.builtin_tools.handover as handover_mod
from app.core.config import settings
from app.services.builtin_tools import build_sub_agent_tools, delegated_parent_id
from app.services.builtin_tools.handover import (
    DELEGATING_AGENT_KEY,
    PARENT_CONSULT_MAX_ITERATIONS,
    ParentConsultBudget,
)
from tests.test_handover_consult_chain import _desk, _handover_tools, _run_chain, _wire

_CHAIN_REFUSAL = "Handover rejected: agent Orchestrator is already in this delegation chain ("


def _task_context(*chain, parent) -> dict:
    """The handover context the sub-agent worker gives a background task's turn."""
    return {
        "type": "sub_agent_task",
        "source_agent": "background task",
        "original_question": "Do the task.",
        "visited_agents": [str(a.id) for a in chain],
        DELEGATING_AGENT_KEY: str(parent.id),
    }


# ── The rule ────────────────────────────────────────────────────────────────


def test_the_direct_parent_of_a_background_task_is_the_agent_that_spawned_it():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    ctx = {"type": "sub_agent_task", DELEGATING_AGENT_KEY: a}
    assert delegated_parent_id(ctx, [a, b], self_id=b) == a


@pytest.mark.parametrize(
    "case",
    ["consult-turn", "no-delegating-agent", "grandparent", "self-fanout", "chain-without-parent", "no-context"],
)
def test_no_parent_outside_a_background_task_spawned_by_the_chain_last_ancestor(case):
    g, a, b = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    ctx, chain = {"type": "sub_agent_task", DELEGATING_AGENT_KEY: a}, [g, a, b]
    if case == "consult-turn":
        ctx = {"type": "consult", DELEGATING_AGENT_KEY: a}
    elif case == "no-delegating-agent":
        ctx = {"type": "sub_agent_task"}
    elif case == "grandparent":
        ctx = {"type": "sub_agent_task", DELEGATING_AGENT_KEY: g}
    elif case == "self-fanout":
        ctx, chain = {"type": "sub_agent_task", DELEGATING_AGENT_KEY: b}, [b, b]
    elif case == "chain-without-parent":
        chain = [g, b]
    else:
        ctx = None
    assert delegated_parent_id(ctx, chain, self_id=b) is None


def test_the_budget_counts_each_question_and_names_its_limit_when_spent():
    parent = str(uuid.uuid4())
    budget = ParentConsultBudget(parent, 3)
    assert budget.is_parent(uuid.UUID(parent)) and not budget.is_parent(uuid.uuid4())
    assert [budget.take() for _ in range(3)] == [None, None, None]
    refusal = budget.take()
    assert refusal is not None and "3 question(s)" in refusal and "DELEGATED_PARENT_CONSULT_LIMIT" in refusal
    assert budget.used == 3


@pytest.mark.parametrize("parent", [None, "set"], ids=["no-parent", "limit-zero"])
def test_no_parent_question_without_a_parent_or_with_a_limit_of_zero(parent):
    pid = str(uuid.uuid4())
    budget = ParentConsultBudget(pid if parent else None, 0 if parent else 3)
    assert not budget.is_parent(pid)


def test_the_audit_line_names_the_run_the_parent_the_question_length_and_the_outcome():
    with patch.object(handover_mod, "logger") as log:
        handover_mod.log_parent_consult("run-1", "agent-a", "Which quarter?", "completed")
    fmt, *args = log.info.call_args.args
    assert fmt.startswith("parent consult:")
    assert args == ["run-1", "agent-a", len("Which quarter?"), "completed"]


def test_the_default_limit_is_three():
    from app.core.config import Settings

    assert Settings.model_fields["delegated_parent_consult_limit"].default == 3


# ── Background tasks back to the parent stay refused ────────────────────────


@pytest.mark.asyncio
async def test_a_background_task_back_to_the_parent_is_still_refused():
    parent, task_agent = _desk("Orchestrator"), _desk("Worker")
    defs_exec = build_sub_agent_tools(
        AsyncMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(task_agent.id),
        handover_agents=[parent],
        parent_run_id=str(uuid.uuid4()),
        visited_agents=[str(parent.id), str(task_agent.id)],
    )
    assert defs_exec is not None
    spawn = defs_exec[1]._handlers["spawn_background_task"]
    create = AsyncMock(side_effect=AssertionError("must not spawn back to the parent"))
    with patch("app.services.sub_agent.create_sub_agent_task", new=create):
        msg = await spawn(str(parent.id), "Do it for me.")
    assert msg.startswith(_CHAIN_REFUSAL)
    create.assert_not_called()


# ── The chat path ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_background_task_asks_its_parent_and_gets_the_answer_inline():
    orchestrator, worker = _desk("Orchestrator"), _desk("Worker")
    _wire(worker, consult=[orchestrator])
    # The parent's own peer, the task's agent: not reachable while it answers.
    _wire(orchestrator, consult=[worker])
    question = "Which quarter do you mean?"
    audit = MagicMock(wraps=handover_mod.log_parent_consult)

    with patch.object(handover_mod, "log_parent_consult", new=audit):
        chain = await _run_chain(
            worker,
            [orchestrator, worker],
            plan={"Worker": [("orchestrator", question)]},
            answers={"Orchestrator": "Q3."},
            handover_context=_task_context(orchestrator, parent=orchestrator),
            delegation_depth=1,
        )

    assert chain["turns"]["Worker"]["results"] == ["[Response from Orchestrator]:\nQ3."]
    # The parent answers with no handover tool and a short budget: it cannot
    # consult nor delegate back to the task inside the consult.
    parent_turn = chain["turns"]["Orchestrator"]
    assert not _handover_tools(parent_turn)
    assert parent_turn["kwargs"]["allow_handover"] is False
    assert parent_turn["kwargs"]["max_iterations_override"] == PARENT_CONSULT_MAX_ITERATIONS
    # Recorded: the child run, the delegation row and the audit line.
    [run] = [r for r in chain["runs"] if r.agent_id == orchestrator.id]
    assert run.trigger_payload["parent_consult"] is True
    assert run.trigger_payload["parent_agent_id"] == str(worker.id)
    [row] = [d for d in chain["delegations"] if d.target_agent_id == orchestrator.id]
    assert row.metadata_json["parent_consult"] is True
    [call] = audit.call_args_list
    _run_id, parent_id, asked, outcome = call.args
    assert (parent_id, asked, outcome) == (str(orchestrator.id), question, "completed")


@pytest.mark.asyncio
async def test_the_question_past_the_limit_is_refused_naming_it():
    orchestrator, worker = _desk("Orchestrator"), _desk("Worker")
    _wire(worker, consult=[orchestrator])

    chain = await _run_chain(
        worker,
        [orchestrator, worker],
        plan={"Worker": [("orchestrator", f"Question {i}?") for i in range(4)]},
        answers={"Orchestrator": "Yes."},
        handover_context=_task_context(orchestrator, parent=orchestrator),
        delegation_depth=1,
    )

    results = chain["turns"]["Worker"]["results"]
    assert results[:3] == ["[Response from Orchestrator]:\nYes."] * 3
    assert results[3].startswith("Consultation refused:") and "3 question(s)" in results[3]
    assert len([r for r in chain["runs"] if r.agent_id == orchestrator.id]) == 3


@pytest.mark.asyncio
async def test_a_limit_of_zero_keeps_the_chain_refusal(monkeypatch):
    monkeypatch.setattr(settings, "delegated_parent_consult_limit", 0)
    orchestrator, worker = _desk("Orchestrator"), _desk("Worker")
    _wire(worker, consult=[orchestrator])

    chain = await _run_chain(
        worker,
        [orchestrator, worker],
        plan={"Worker": [("orchestrator", "Which quarter?")]},
        answers={},
        handover_context=_task_context(orchestrator, parent=orchestrator),
        delegation_depth=1,
    )

    assert chain["turns"]["Worker"]["results"][0].startswith(_CHAIN_REFUSAL)
    assert "Orchestrator" not in chain["turns"]


@pytest.mark.asyncio
async def test_a_grandparent_stays_refused():
    orchestrator, manager, worker = _desk("Orchestrator"), _desk("Manager"), _desk("Worker")
    _wire(worker, consult=[orchestrator, manager])

    chain = await _run_chain(
        worker,
        [orchestrator, manager, worker],
        plan={"Worker": [("orchestrator", "Which quarter?"), ("manager", "And you?")]},
        answers={"Manager": "Q4."},
        handover_context=_task_context(orchestrator, manager, parent=manager),
        delegation_depth=2,
    )

    results = chain["turns"]["Worker"]["results"]
    assert results[0].startswith(_CHAIN_REFUSAL)
    assert results[1] == "[Response from Manager]:\nQ4."
    assert "Orchestrator" not in chain["turns"]


@pytest.mark.asyncio
async def test_a_consulted_turn_still_refuses_a_consult_back_to_its_consulter():
    """Not a background task: the chain guard applies as before."""
    orchestrator, clerk = _desk("Orchestrator"), _desk("Clerk")
    _wire(orchestrator, consult=[clerk])
    _wire(clerk, consult=[orchestrator])

    chain = await _run_chain(
        orchestrator,
        [orchestrator, clerk],
        plan={"Orchestrator": [("clerk", "Ask around.")], "Clerk": [("orchestrator", "And you?")]},
        answers={},
    )

    assert chain["turns"]["Clerk"]["results"][-1].startswith(_CHAIN_REFUSAL)
