"""Clone copies memory on request, drops reserved tags; intent bind names the winner (#12345)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.agent import Agent
from app.models.agent_copy import (
    build_agent_copy_kwargs,
    is_reserved_agent_tag,
    split_reserved_agent_tags,
)
from app.models.agent_memory_entry import AgentMemoryEntry
from app.models.intent import IntentBinding
from app.services import persistent_memory
from app.services.builtin_tools.intent_bindings import winner_line
from app.services.intent_binding_writes import (
    WINNER_RULE,
    IntentWinner,
    effective_priority,
    intent_verticals,
    rank_bindings,
)

# ── reserved tags ───────────────────────────────────────────────────────────


def test_reserved_tags_are_builtin_and_org_fill():
    assert is_reserved_agent_tag("builtin")
    assert is_reserved_agent_tag("org-fill:3f2a")
    assert not is_reserved_agent_tag("cti")
    assert not is_reserved_agent_tag("built-in")
    assert not is_reserved_agent_tag(None)


def test_split_reserved_tags_keeps_order_and_none():
    kept, dropped = split_reserved_agent_tags(["builtin", "xtm-one", "org-fill:x", "assistant"])
    assert kept == ["xtm-one", "assistant"]
    assert dropped == ["builtin", "org-fill:x"]
    assert split_reserved_agent_tags(None) == (None, [])
    assert split_reserved_agent_tags(["builtin"]) == ([], ["builtin"])


def test_copy_kwargs_drop_the_builtin_tag():
    source = Agent(name="General assistant", slug="general", tags=["builtin", "xtm-one", "assistant"])
    kwargs = build_agent_copy_kwargs(source, name="Clone", slug="clone")
    assert kwargs["tags"] == ["xtm-one", "assistant"]
    # The source row is untouched.
    assert source.tags == ["builtin", "xtm-one", "assistant"]


# ── memory copy ─────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self.rows = rows
        self.added: list = []
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows)

    def add_all(self, objs):
        self.added.extend(objs)

    async def flush(self):
        return None


def _entry(agent_id, *, user_id=None, kind="fact", subject=None, created=None, superseded_by=None):
    created = created or datetime(2026, 11, 1, tzinfo=UTC)
    return AgentMemoryEntry(
        id=uuid.uuid4(),
        agent_id=agent_id,
        user_id=user_id,
        content=f"{kind} entry",
        memory_type=kind,
        source="chat",
        subject_key=subject,
        superseded_at=created + timedelta(days=1) if superseded_by else None,
        superseded_by_id=superseded_by,
        embedding=[0.1, 0.2],
        embedding_model="m",
        embedding_dimensions=2,
        created_at=created,
        updated_at=created,
    )


def _people():
    owner = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)
    admin = SimpleNamespace(id=uuid.uuid4(), role="admin", is_admin=True)
    member = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)
    return owner, admin, member


@pytest.mark.asyncio
async def test_copy_memory_copies_every_entry_with_its_metadata():
    owner, _admin, _member = _people()
    source = SimpleNamespace(id=uuid.uuid4(), user_id=owner.id, is_company_managed=False)
    newer = _entry(source.id, kind="state", subject="doc:1", created=datetime(2026, 11, 3, tzinfo=UTC))
    older = _entry(source.id, kind="state", subject="doc:1", superseded_by=newer.id)
    personal = _entry(source.id, user_id=owner.id, kind="preference")
    db = _FakeSession([older, newer, personal])
    target = uuid.uuid4()

    count = await persistent_memory.copy_agent_memories(db, source=source, target_agent_id=target, user=owner)

    assert count == 3
    copies = {c.content + str(c.created_at): c for c in db.added}
    assert all(c.agent_id == target for c in db.added)
    assert {c.id for c in db.added}.isdisjoint({older.id, newer.id, personal.id})
    by_old = dict(zip([older.id, newer.id, personal.id], db.added, strict=True))
    assert by_old[older.id].superseded_by_id == by_old[newer.id].id
    assert by_old[older.id].superseded_at == older.superseded_at
    assert by_old[newer.id].subject_key == "doc:1" and by_old[newer.id].memory_type == "state"
    assert by_old[newer.id].created_at == newer.created_at
    assert by_old[personal.id].user_id == owner.id
    assert by_old[personal.id].embedding == [0.1, 0.2]
    assert len(copies) == 3


@pytest.mark.asyncio
async def test_copy_memory_with_no_entries_adds_nothing():
    owner, _admin, _member = _people()
    source = SimpleNamespace(id=uuid.uuid4(), user_id=owner.id, is_company_managed=False)
    db = _FakeSession([])
    assert await persistent_memory.copy_agent_memories(db, source=source, target_agent_id=uuid.uuid4(), user=owner) == 0
    assert db.added == []


@pytest.mark.asyncio
async def test_copy_memory_of_a_company_agent_is_an_administrators():
    _owner, admin, member = _people()
    source = SimpleNamespace(id=uuid.uuid4(), user_id=None, is_company_managed=True)
    with pytest.raises(persistent_memory.MemoryCopyRefusedError):
        await persistent_memory.copy_agent_memories(
            _FakeSession([]), source=source, target_agent_id=uuid.uuid4(), user=member
        )
    db = _FakeSession([_entry(source.id)])
    assert await persistent_memory.copy_agent_memories(db, source=source, target_agent_id=uuid.uuid4(), user=admin) == 1


@pytest.mark.asyncio
async def test_copy_memory_scopes_a_non_owner_to_the_pools_they_read():
    owner, _admin, member = _people()
    source = SimpleNamespace(id=uuid.uuid4(), user_id=owner.id, is_company_managed=False)
    db = _FakeSession([])
    await persistent_memory.copy_agent_memories(db, source=source, target_agent_id=uuid.uuid4(), user=member)
    sql = str(db.statements[0].compile(compile_kwargs={"literal_binds": False}))
    assert "agent_memory_entries.user_id IS NULL" in sql
    db_owner = _FakeSession([])
    await persistent_memory.copy_agent_memories(db_owner, source=source, target_agent_id=uuid.uuid4(), user=owner)
    assert "user_id IS NULL" not in str(db_owner.statements[0].compile())


def test_memory_read_rule_matches_the_memory_api():
    from app.api.v1 import agent_memory

    owner, admin, member = _people()
    private = SimpleNamespace(user_id=owner.id, is_company_managed=False)
    company = SimpleNamespace(user_id=None, is_company_managed=True)
    for agent in (private, company):
        for user in (owner, admin, member):
            assert agent_memory._is_memory_restricted(agent, user) == persistent_memory.memory_content_restricted(
                agent, user
            )
            assert (agent_memory._memory_scope_filter(agent, user) is None) == persistent_memory.memory_full_visibility(
                agent, user
            )


# ── intent winner ───────────────────────────────────────────────────────────


def _self(binding):
    return binding


def _binding(priority, *, vertical=None, overrides=None, created=None):
    return IntentBinding(
        id=uuid.uuid4(),
        intent_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        vertical=vertical,
        priority=priority,
        priority_overrides=overrides,
        created_at=created or datetime(2026, 11, 1, tzinfo=UTC),
    )


def test_effective_priority_uses_the_vertical_override():
    b = _binding(4, overrides={"cti": 1})
    assert effective_priority(b, None) == 4
    assert effective_priority(b, "cti") == 1
    assert effective_priority(b, "aev") == 4


def test_higher_priority_wins():
    builtin = _binding(4)
    clone = _binding(1, created=datetime(2026, 11, 17, tzinfo=UTC))
    assert rank_bindings([clone, builtin], None, key=_self)[0] is builtin


def test_a_vertical_override_changes_the_winner_in_that_vertical_only():
    builtin = _binding(4, overrides={"cti": 0})
    clone = _binding(1)
    assert rank_bindings([builtin, clone], "cti", key=_self)[0] is clone
    assert rank_bindings([builtin, clone], None, key=_self)[0] is builtin


def test_a_tie_goes_to_the_binding_made_first():
    first = _binding(2, created=datetime(2026, 11, 1, tzinfo=UTC))
    later = _binding(2, created=datetime(2026, 11, 17, tzinfo=UTC))
    assert rank_bindings([later, first], None, key=_self)[0] is first
    assert rank_bindings([first, later], None, key=_self)[0] is first
    assert "tie" in WINNER_RULE and "higher effective priority wins" in WINNER_RULE


def test_intent_verticals_lists_no_filter_then_named_ones():
    assert intent_verticals([_binding(1, vertical="cti"), _binding(2, overrides={"aev": 3})]) == [None, "aev", "cti"]


def test_winner_line_names_the_binding_that_beats_the_agent_and_the_priority_needed():
    clone_id, builtin_id = uuid.uuid4(), uuid.uuid4()
    winner = IntentWinner(None, _binding(4), builtin_id, "XTM One assistant", 4)
    line = winner_line("general.assistant", None, winner, clone_id)
    assert "**XTM One assistant**" in line and str(builtin_id) in line
    assert "effective priority 4 wins" in line
    assert "at least 5" in line


def test_winner_line_when_the_agent_wins_and_when_nobody_is_listed():
    agent_id = uuid.uuid4()
    mine = IntentWinner("cti", _binding(9), agent_id, "Clone", 9)
    assert "this agent wins" in winner_line("x", "cti", mine, agent_id)
    assert "vertical cti" in winner_line("x", "cti", mine, agent_id)
    assert "none wins" in winner_line("x", None, None, agent_id)


# ── tool descriptions ───────────────────────────────────────────────────────


def test_tool_descriptions_state_the_rule_and_the_option():
    from app.services.builtin_tools.intent_bindings import _definitions

    defs = {d.name: d for d in _definitions()}
    assert "higher priority wins" in defs["list_agent_intent_bindings"].description.lower()
    assert "priority needed" in defs["bind_agent_to_intent"].description
