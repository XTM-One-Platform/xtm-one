"""Tests for generalized handover cycle detection (delegate/consult/transfer).

Cycle detection used to exist only for chat transfers (the ``transfer_chain``
check in ``agent_chat.py``).  It is now generalized into an ordered
``visited_agents`` chain carried through ``handover_context`` for ALL three
handover types across both the chat and assignment paths:

* ``handover_cycle_error`` — shared check returning a friendly tool-result
  error string (never raises) when the target is already in the chain.
* ``extract_visited_agents`` — chain extraction with backward compatibility
  for legacy ``transfer_chain`` payloads.
* ``build_sub_agent_tools`` / ``spawn_background_task`` — rejects delegation
  to an agent already in the chain and forwards the chain (current agent
  included) to ``create_sub_agent_task`` so nested delegations inherit it.
* ``create_sub_agent_task`` — defense-in-depth guard raising
  ``HandoverCycleError`` before any DB work.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.builtin_tools import (
    build_sub_agent_tools,
    extract_visited_agents,
    handover_cycle_error,
    normalize_visited_agents,
)
from app.services.sub_agent import HandoverCycleError, create_sub_agent_task


def _make_agent(name: str = "Worker") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        slug=name.lower(),
        handover_types={},
    )


def _make_fake_sub_task() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), status="pending")


# ── handover_cycle_error (shared check) ──────────────────────────────────────


def test_cycle_error_none_when_target_not_in_chain():
    """A non-cyclic handover passes (returns None)."""
    a, b, c = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    assert handover_cycle_error(c, [a, b]) is None


def test_cycle_error_none_when_chain_empty():
    assert handover_cycle_error(str(uuid.uuid4()), []) is None
    assert handover_cycle_error(str(uuid.uuid4()), None) is None


def test_cycle_error_detects_direct_cycle():
    """A -> B -> A is rejected with a friendly error string."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    msg = handover_cycle_error(a, [a, b])
    assert msg is not None
    assert msg.startswith("Handover rejected:")
    assert "already in this delegation chain" in msg
    assert "Complete the work yourself or choose a different agent" in msg


def test_cycle_error_uses_friendly_names_and_chain_order():
    """The chain is rendered in order with display names (A -> B -> A)."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    names = {a: "Alpha", b: "Beta"}
    msg = handover_cycle_error(a, [a, b], agent_names=names, target_name="Alpha")
    assert msg is not None
    assert "(Alpha -> Beta -> Alpha)" in msg


def test_cycle_error_accepts_non_string_ids():
    """UUID objects in the chain are normalized to strings before comparison."""
    a = uuid.uuid4()
    msg = handover_cycle_error(str(a), [a])
    assert msg is not None


def test_cycle_error_ignores_non_sequence_chain():
    """A bare string chain must NOT be iterated character-by-character —
    it is treated like no chain at all (mirrors extract_visited_agents)."""
    # "a" is a character of the bogus string chain — without the type guard
    # this would be a false positive.
    assert handover_cycle_error("a", "abc") is None
    assert handover_cycle_error(str(uuid.uuid4()), {"not": "a-list"}) is None


# ── extract_visited_agents (chain extraction + legacy compat) ────────────────


def test_extract_prefers_explicit_transfer_chain_param():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    chain = extract_visited_agents({"visited_agents": [b]}, transfer_chain=[a])
    assert chain == [a]


def test_extract_reads_visited_agents_from_context():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    chain = extract_visited_agents({"visited_agents": [a, b]})
    assert chain == [a, b]


def test_extract_accepts_legacy_transfer_chain_context_key():
    """Old payloads carrying ``transfer_chain`` in the context still work."""
    a = str(uuid.uuid4())
    chain = extract_visited_agents({"transfer_chain": [a]})
    assert chain == [a]


def test_extract_returns_empty_for_missing_or_malformed_chain():
    assert extract_visited_agents(None) == []
    assert extract_visited_agents({}) == []
    assert extract_visited_agents({"visited_agents": "not-a-list"}) == []


def test_extract_corrupted_context_chain_falls_back_to_legacy_key():
    """A corrupted (stringified) ``visited_agents`` context value must not
    mask a valid legacy ``transfer_chain`` carried in the same context."""
    a = str(uuid.uuid4())
    chain = extract_visited_agents(
        {"visited_agents": "not-a-list", "transfer_chain": [a]}
    )
    assert chain == [a]


def test_extract_ignores_non_sequence_transfer_chain():
    """A bare string ``transfer_chain`` must NOT be iterated character-by-
    character — it is treated like ``None`` and falls back to the context."""
    a = str(uuid.uuid4())
    assert extract_visited_agents({"visited_agents": [a]}, transfer_chain="abc") == [a]
    assert extract_visited_agents(None, transfer_chain="abc") == []


def test_extract_accepts_tuple_chains():
    """Tuples are valid ordered chains for both sources."""
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert extract_visited_agents(None, transfer_chain=(a, b)) == [a, b]
    assert extract_visited_agents({"visited_agents": (a,)}) == [a]


def test_extract_empty_transfer_chain_falls_back_to_context():
    """An EMPTY explicit ``transfer_chain`` carries no chain information and
    must NOT silently discard an inherited context chain (there is no
    "reset the chain" semantic — the chain only ever grows)."""
    a = str(uuid.uuid4())
    assert extract_visited_agents({"visited_agents": [a]}, transfer_chain=[]) == [a]


def test_extract_returns_new_list_and_stringifies():
    """The extracted chain never aliases the input list."""
    a = uuid.uuid4()
    source = [a]
    chain = extract_visited_agents({"visited_agents": source})
    assert chain == [str(a)]
    chain.append("x")
    assert source == [a]


# ── normalize_visited_agents (shared chain normalization) ───────────────────


def test_normalize_returns_stringified_copy():
    """Lists are stringified into a NEW list (never aliases the input)."""
    a = uuid.uuid4()
    source = [a]
    chain = normalize_visited_agents(source)
    assert chain == [str(a)]
    chain.append("x")
    assert source == [a]


def test_normalize_accepts_tuples():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert normalize_visited_agents((a, b)) == [a, b]


def test_normalize_drops_non_sequence_values():
    """Non-sequence values (the untrusted-boundary case: a corrupted payload
    string, a dict, None) yield an empty chain instead of a bogus
    per-character chain."""
    assert normalize_visited_agents(None) == []
    assert normalize_visited_agents("abc") == []
    assert normalize_visited_agents({"not": "a-list"}) == []
    assert normalize_visited_agents(42) == []


# ── spawn_background_task (delegate path) ────────────────────────────────────


def _build_spawn(
    *,
    parent_agent_id: str | None = None,
    visited_agents: object = None,
):
    agent = _make_agent()
    db = MagicMock()
    result = build_sub_agent_tools(
        db,
        user_id=str(uuid.uuid4()),
        parent_agent_id=parent_agent_id or str(uuid.uuid4()),
        handover_agents=[agent],
        parent_run_id=str(uuid.uuid4()),
        visited_agents=visited_agents,
    )
    assert result is not None
    _defs, executor = result
    return agent, executor._handlers["spawn_background_task"]


async def test_spawn_rejects_target_already_in_chain():
    """Delegating to an agent already in the visited chain returns a friendly
    error string (no exception) and never reaches the engine."""
    agent = _make_agent("Cyclic")
    db = MagicMock()
    # Direct cycle: the visited chain already contains the target agent.
    result = build_sub_agent_tools(
        db,
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[agent],
        parent_run_id=str(uuid.uuid4()),
        visited_agents=[str(agent.id)],
    )
    assert result is not None
    _defs, executor = result
    spawn = executor._handlers["spawn_background_task"]

    create_mock = AsyncMock(side_effect=AssertionError("must not spawn on cycle"))
    with (
        patch(
            "app.services.sub_agent.count_active_sub_agent_executions",
            new=AsyncMock(return_value=0),
        ),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    ):
        msg = await spawn(str(agent.id), "cyclic task")

    assert "Handover rejected" in msg
    assert "already in this delegation chain" in msg
    create_mock.assert_not_called()


async def test_spawn_forwards_inherited_chain_with_parent_appended():
    """Non-cyclic spawns forward the visited chain — including the current
    (parent) agent — to ``create_sub_agent_task`` so nested delegations
    inherit it."""
    ancestor = str(uuid.uuid4())
    parent = str(uuid.uuid4())
    target, spawn = _build_spawn(
        parent_agent_id=parent, visited_agents=[ancestor, parent]
    )

    create_mock = AsyncMock(return_value=_make_fake_sub_task())
    with (
        patch(
            "app.services.sub_agent.count_active_sub_agent_executions",
            new=AsyncMock(return_value=0),
        ),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    ):
        msg = await spawn(str(target.id), "nested task")

    assert "spawned successfully" in msg
    create_mock.assert_awaited_once()
    forwarded = create_mock.await_args.kwargs.get("visited_agents")
    assert forwarded == [ancestor, parent]


async def test_spawn_appends_parent_when_chain_missing_it():
    """Callers that pass a chain without the current agent still produce a
    chain containing the parent (defensive append)."""
    ancestor = str(uuid.uuid4())
    parent = str(uuid.uuid4())
    target, spawn = _build_spawn(
        parent_agent_id=parent, visited_agents=[ancestor]
    )

    create_mock = AsyncMock(return_value=_make_fake_sub_task())
    with (
        patch(
            "app.services.sub_agent.count_active_sub_agent_executions",
            new=AsyncMock(return_value=0),
        ),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    ):
        msg = await spawn(str(target.id), "task")

    assert "spawned successfully" in msg
    forwarded = create_mock.await_args.kwargs.get("visited_agents")
    assert forwarded == [ancestor, parent]


async def test_spawn_ignores_non_sequence_chain():
    """A corrupted non-sequence ``visited_agents`` (e.g. a payload string)
    is dropped instead of being iterated character-by-character — the
    forwarded chain contains just the parent agent."""
    parent = str(uuid.uuid4())
    target, spawn = _build_spawn(
        parent_agent_id=parent, visited_agents="bogus-chain"
    )

    create_mock = AsyncMock(return_value=_make_fake_sub_task())
    with (
        patch(
            "app.services.sub_agent.count_active_sub_agent_executions",
            new=AsyncMock(return_value=0),
        ),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    ):
        msg = await spawn(str(target.id), "task")

    assert "spawned successfully" in msg
    forwarded = create_mock.await_args.kwargs.get("visited_agents")
    assert forwarded == [parent]


async def test_spawn_without_chain_still_works():
    """No visited chain (legacy callers) — spawn proceeds and the chain
    contains just the parent agent."""
    parent = str(uuid.uuid4())
    target, spawn = _build_spawn(parent_agent_id=parent)

    create_mock = AsyncMock(return_value=_make_fake_sub_task())
    with (
        patch(
            "app.services.sub_agent.count_active_sub_agent_executions",
            new=AsyncMock(return_value=0),
        ),
        patch("app.services.sub_agent.create_sub_agent_task", new=create_mock),
    ):
        msg = await spawn(str(target.id), "legacy task")

    assert "spawned successfully" in msg
    forwarded = create_mock.await_args.kwargs.get("visited_agents")
    assert forwarded == [parent]


# ── create_sub_agent_task (defense-in-depth guard) ───────────────────────────


async def test_create_sub_agent_task_raises_on_cycle_before_db_work():
    """The guard fires before any session is opened — a MagicMock db and no
    patched session factory prove no DB work happens."""
    target = str(uuid.uuid4())
    parent = str(uuid.uuid4())
    with pytest.raises(HandoverCycleError) as exc_info:
        await create_sub_agent_task(
            MagicMock(),
            user_id=str(uuid.uuid4()),
            parent_agent_id=parent,
            target_agent_id=target,
            task_description="cyclic",
            visited_agents=[target, parent],
        )
    assert "Handover rejected" in str(exc_info.value)
    assert "already in this delegation chain" in str(exc_info.value)


async def test_create_sub_agent_task_rejects_self_delegation_without_chain():
    """The guard extends the chain with the parent BEFORE checking the
    target, so a call site passing no chain at all (``visited_agents=None``)
    still cannot delegate back to the parent (A -> A)."""
    parent = str(uuid.uuid4())
    with pytest.raises(HandoverCycleError):
        await create_sub_agent_task(
            MagicMock(),
            user_id=str(uuid.uuid4()),
            parent_agent_id=parent,
            target_agent_id=parent,
            task_description="self-delegation",
            visited_agents=None,
        )


async def test_create_sub_agent_task_ignores_non_sequence_chain():
    """A corrupted non-sequence chain (e.g. a payload string) is dropped —
    not iterated character-by-character — while the parent-extend guard
    still catches self-delegation.  The rendered chain proves the bogus
    value never polluted it (only the parent appears)."""
    parent = str(uuid.uuid4())
    with pytest.raises(HandoverCycleError) as exc_info:
        await create_sub_agent_task(
            MagicMock(),
            user_id=str(uuid.uuid4()),
            parent_agent_id=parent,
            target_agent_id=parent,
            task_description="self-delegation with corrupted chain",
            visited_agents="bogus-chain",  # type: ignore[arg-type]
        )
    assert f"({parent} -> {parent})" in str(exc_info.value)
