"""Tests for the agent/assignment knowledge-base read/write access model."""

from types import SimpleNamespace

from app.models.agent import Agent
from app.services.assignment_helpers import resolve_assignment_kb_access

KB_A = "11111111-1111-1111-1111-111111111111"
KB_B = "22222222-2222-2222-2222-222222222222"
KB_C = "33333333-3333-3333-3333-333333333333"


def _agent(kb_ids, perms=None):
    return Agent(
        name="Test",
        knowledge_base_ids=kb_ids,
        knowledge_base_permissions=perms,
    )


def _assignment(read=None, write=None):
    return SimpleNamespace(
        read_knowledge_base_ids=read,
        write_knowledge_base_ids=write,
    )


# ── Agent.writable_knowledge_base_ids property ────────────────────────────


def test_writable_ids_only_includes_bound_write_kbs() -> None:
    agent = _agent([KB_A, KB_B], {KB_B: "write", KB_C: "write"})
    # KB_B is bound + write -> writable. KB_C is write but not bound -> ignored.
    assert agent.writable_knowledge_base_ids == [KB_B]


def test_writable_ids_defaults_to_read_only() -> None:
    agent = _agent([KB_A, KB_B], None)
    assert agent.writable_knowledge_base_ids == []
    agent_read = _agent([KB_A], {KB_A: "read"})
    assert agent_read.writable_knowledge_base_ids == []


def test_prune_orphaned_permissions_drops_unbound() -> None:
    agent = _agent([KB_A], {KB_A: "write", KB_B: "write"})
    agent.prune_orphaned_knowledge_base_permissions()
    assert agent.knowledge_base_permissions == {KB_A: "write"}

    agent_all_orphans = _agent([], {KB_A: "write"})
    agent_all_orphans.prune_orphaned_knowledge_base_permissions()
    assert agent_all_orphans.knowledge_base_permissions is None


# ── Read resolution (refines agent KBs) ───────────────────────────────────


def test_read_none_inherits_all_agent_kbs() -> None:
    agent = _agent([KB_A, KB_B])
    read, _ = resolve_assignment_kb_access(agent, _assignment(read=None))
    assert read == [KB_A, KB_B]


def test_read_subset_is_refined_and_drops_stale() -> None:
    agent = _agent([KB_A, KB_B])
    read, _ = resolve_assignment_kb_access(
        agent, _assignment(read=[KB_B, KB_C])  # KB_C not bound -> dropped
    )
    assert read == [KB_B]


def test_read_empty_inherits_all_agent_kbs() -> None:
    # Empty read selection is never "none" — it always falls back to all agent
    # KBs so an assignment can never get stuck with no RAG.
    agent = _agent([KB_A, KB_B])
    read, _ = resolve_assignment_kb_access(agent, _assignment(read=[]))
    assert read == [KB_A, KB_B]


def test_read_all_stale_selection_falls_back_to_all() -> None:
    # A subset that no longer matches any bound KB falls back to all agent KBs
    # rather than resolving to an empty (no-search) set.
    agent = _agent([KB_A, KB_B])
    read, _ = resolve_assignment_kb_access(agent, _assignment(read=[KB_C]))
    assert read == [KB_A, KB_B]


# ── Write resolution (refines agent-writable KBs) ─────────────────────────


def test_write_none_inherits_all_writable() -> None:
    agent = _agent([KB_A, KB_B], {KB_A: "write"})
    _, write = resolve_assignment_kb_access(agent, _assignment(write=None))
    assert write == [KB_A]


def test_write_none_with_no_writable_is_empty() -> None:
    agent = _agent([KB_A, KB_B])
    _, write = resolve_assignment_kb_access(agent, _assignment(write=None))
    assert write == []


def test_write_subset_cannot_exceed_agent_writable() -> None:
    agent = _agent([KB_A, KB_B], {KB_A: "write"})
    # Assignment asks to write KB_A + KB_B, but only KB_A is agent-writable.
    _, write = resolve_assignment_kb_access(
        agent, _assignment(write=[KB_A, KB_B])
    )
    assert write == [KB_A]


def test_write_empty_means_none_even_when_agent_writable() -> None:
    agent = _agent([KB_A], {KB_A: "write"})
    _, write = resolve_assignment_kb_access(agent, _assignment(write=[]))
    assert write == []
