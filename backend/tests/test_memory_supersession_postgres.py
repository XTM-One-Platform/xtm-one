"""State entries, supersession and retirement on PostgreSQL.

An entry describing an object that changes (a document, a deck, a ticket) used
to stay in every prompt after the object changed, side by side with the newer
observation. A ``state`` entry names its object with a ``subject_key``: the
newer one on a subject supersedes the older, which is kept, flagged, ranked
last by ``search_memory`` and never injected into a prompt. ``invalidate_memory``
retires entries that no longer hold.

Runs the real tools and the prompt loader against PostgreSQL + pgvector: the
supersession is SQL (a correlated EXISTS, a row comparison, UPDATE ...
RETURNING), which no fake session can stand in for. Only the embedding is
stubbed, with vectors close to the query and far from one another.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.agent_memory_entry import AgentMemoryEntry
from app.models.user import User
from app.services import persistent_memory as pm
from app.services.builtin_tools import build_memory_search_tool, build_memory_store_tool
from app.services.builtin_tools import memory as memory_tools
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_DIMENSIONS = 33
_QUERY = "what does doc X say"


@dataclass
class _World:
    sessions: object
    agent_id: str
    user_id: str
    other_user_id: str


def _stub_embeddings(monkeypatch) -> None:
    """Each stored text one axis of its own, the query on the shared one.

    Every entry scores 0.42 to 0.55 against the query (relevant), the text
    stored first the highest - so an outdated entry is the one a relevance
    search would pick if nothing excluded it - and two different texts under
    0.31 against each other (never duplicates).
    """
    axes: dict[str, int] = {}

    async def _embed_stored(_db, content, **_kwargs):
        axis = axes.setdefault(content, len(axes) + 1)
        vector = [0.0] * _DIMENSIONS
        vector[0], vector[axis] = 1.0, 1.5 + 0.05 * axis
        return vector, "test-embedding"

    async def _embed_query(_db, _text, **_kwargs):
        return [1.0] + [0.0] * (_DIMENSIONS - 1)

    monkeypatch.setattr(pm, "embed_text_with_model", _embed_stored)
    monkeypatch.setattr(pm, "embed_text", _embed_query)
    monkeypatch.setattr(memory_tools, "_resolve_provider_model", AsyncMock(return_value=None))
    monkeypatch.setattr(memory_tools, "_spawn_memory_screen", lambda *_a, **_k: None)


@asynccontextmanager
async def _world(monkeypatch):
    _stub_embeddings(monkeypatch)
    async with (
        scratch_database(
            "memsup", tables=ALL_TABLES, extensions=("vector",), refuse_other_databases=True
        ) as db,
        db.sessions() as session,
    ):
        owner, other = (
            User(email=f"{name}-{uuid.uuid4().hex[:8]}@test.local", full_name=name, hashed_password="x")
            for name in ("owner", "other")
        )
        session.add_all([owner, other])
        await session.flush()
        agent = Agent(
            name="Deck Desk",
            slug=f"deck-desk-{uuid.uuid4().hex[:8]}",
            persona="You keep decks up to date.",
            enabled=True,
            user_id=owner.id,
            is_builtin=False,
            memory_persistent_enabled=True,
        )
        session.add(agent)
        await session.commit()
        yield _World(
            sessions=db.sessions,
            agent_id=str(agent.id),
            user_id=str(owner.id),
            other_user_id=str(other.id),
        )


async def _call(world: _World, tool: str, user_id: str, **arguments) -> str:
    builder = build_memory_search_tool if tool == "search_memory" else build_memory_store_tool
    _defs, executor = builder(world.agent_id, user_id)
    result = await executor.execute_tool(ToolCall(id="call-1", name=tool, arguments=arguments))
    return result.content


async def _store(world: _World, content: str, *, user_id: str, **arguments) -> str:
    return await _call(world, "store_memory", user_id, content=content, **arguments)


async def _prompt(world: _World, user_id: str | None, **kwargs) -> str:
    async with world.sessions() as session:
        return await pm.load_persistent_context(
            session, agent_id=world.agent_id, user_id=user_id, **kwargs
        )


async def _search(world: _World, user_id: str | None) -> list[dict]:
    async with world.sessions() as session:
        return await pm.search_agent_memories(
            session, agent_id=world.agent_id, query=_QUERY, user_id=user_id, limit=10
        )


async def _entry(world: _World, content: str) -> AgentMemoryEntry:
    async with world.sessions() as session:
        return (
            await session.execute(select(AgentMemoryEntry).where(AgentMemoryEntry.content == content))
        ).scalar_one()


async def _entries(world: _World) -> list[AgentMemoryEntry]:
    async with world.sessions() as session:
        return list((await session.execute(select(AgentMemoryEntry))).scalars().all())


def _by_content(rows: list[dict]) -> dict[str, dict]:
    return {row["content"]: row for row in rows}


# ── The scenario of the issue ─────────────────────────────────────────────────


async def test_a_newer_state_on_a_subject_is_the_only_active_one(monkeypatch):
    async with _world(monkeypatch) as world:
        await _store(
            world, "doc X: footer reads Draft", user_id=world.user_id, memory_type="state", subject_key="doc:X"
        )
        answer = await _store(
            world, "doc X: footer reads Final", user_id=world.user_id, memory_type="state", subject_key="doc:X"
        )
        draft = await _entry(world, "doc X: footer reads Draft")
        final = await _entry(world, "doc X: footer reads Final")

        assert answer.startswith("Memory stored successfully (type: state).")
        assert f"Entry id: {final.id}, subject_key: doc:X." in answer
        assert f"It supersedes 1 earlier entry ({draft.id})" in answer
        # The older one is kept, flagged, and points at its successor.
        assert draft.superseded_at is not None
        assert draft.superseded_by_id == final.id
        assert final.superseded_at is None

        # The prompt block, by recency and by relevance (a limit below what is
        # stored takes the semantic path).
        for kwargs in ({}, {"query": _QUERY}, {"query": _QUERY, "limit": 1}):
            block = await _prompt(world, world.user_id, **kwargs)
            assert "doc X: footer reads Final" in block, kwargs
            assert "footer reads Draft" not in block, kwargs
            assert "• [state: doc:X] as of " in block
            assert ", verify live: doc X: footer reads Final" in block
            assert pm.STATE_GUIDANCE in block

        # The search lists both, the active one first, the older one superseded.
        rows = await _search(world, world.user_id)
        assert [row["content"] for row in rows] == [
            "doc X: footer reads Final",
            "doc X: footer reads Draft",
        ]
        assert [(row["superseded"], row["superseded_by"]) for row in rows] == [
            (False, None),
            (True, str(final.id)),
        ]
        assert all(row["subject_key"] == "doc:X" and row["stored_at"].endswith("Z") for row in rows)

        # And so does the tool, in its own words.
        found = await _call(world, "search_memory", world.user_id, query=_QUERY)
        assert f"id={final.id} " in found
        assert "subject_key=doc:X superseded=false" in found
        assert f"superseded=true superseded_by={final.id}" in found
        assert found.index("footer reads Final") < found.index("footer reads Draft")
        assert "superseded=true: the entry no longer holds" in found
        assert pm.STATE_GUIDANCE in found


async def test_entries_without_a_subject_behave_as_before(monkeypatch):
    async with _world(monkeypatch) as world:
        first = await _store(world, "The team ships on Thursdays.", user_id=world.user_id)
        second = await _store(world, "Release notes go to the wiki.", user_id=world.user_id, memory_type="fact")

        assert first == "Memory stored successfully (type: learned)."
        assert second == "Memory stored successfully (type: fact)."
        block = await _prompt(world, world.user_id)
        assert "• [learned] The team ships on Thursdays." in block
        assert "• [fact] Release notes go to the wiki." in block
        assert pm.STATE_GUIDANCE not in block
        assert all(
            (row["superseded"], row["subject_key"]) == (False, None) for row in await _search(world, world.user_id)
        )


async def test_a_state_on_another_subject_is_left_alone(monkeypatch):
    async with _world(monkeypatch) as world:
        for subject, content in (("doc:X", "doc X: 3 pages"), ("doc:Y", "doc Y: 5 pages")):
            await _store(world, content, user_id=world.user_id, memory_type="state", subject_key=subject)

        block = await _prompt(world, world.user_id)
        assert "doc X: 3 pages" in block and "doc Y: 5 pages" in block


# ── supersedes=<entry_id> ─────────────────────────────────────────────────────


async def test_supersedes_replaces_the_entry_it_names_whatever_its_type(monkeypatch):
    async with _world(monkeypatch) as world:
        await _store(world, "The deck has 12 slides.", user_id=world.user_id, memory_type="fact")
        old = await _entry(world, "The deck has 12 slides.")

        answer = await _store(
            world, "The deck has 14 slides.", user_id=world.user_id, memory_type="fact", supersedes=str(old.id)
        )
        new = await _entry(world, "The deck has 14 slides.")

        assert f"It supersedes 1 earlier entry ({old.id})" in answer
        assert (await _entry(world, "The deck has 12 slides.")).superseded_by_id == new.id
        block = await _prompt(world, world.user_id)
        assert "14 slides" in block and "12 slides" not in block


async def test_a_superseded_entry_leaves_every_memory_tier_of_the_next_prompt(monkeypatch):
    """#11861: store A, supersede it with B; the next system prompt carries B, never A."""
    import json

    from app.services import agent_memory, memory_superseded

    rule_a = "Post the UX verdict as a PR review."
    rule_b = "Post the UX verdict as a work item note."
    async with _world(monkeypatch) as world:
        await _store(world, rule_a, user_id=world.user_id, memory_type="fact")
        old = await _entry(world, rule_a)
        await _store(world, rule_b, user_id=world.user_id, memory_type="fact", supersedes=str(old.id))

        # The Redis tiers were written before the supersede and still quote A.
        class _Redis:
            async def lrange(self, *_args):
                return [json.dumps({"summary": f"Learned: {rule_a}", "timestamp": "2026-11-13T19:00"})]

            async def get(self, _key):
                return f"- {rule_a}\n- Captures are 1728x1080."

        monkeypatch.setattr(agent_memory, "redis_client", AsyncMock(return_value=_Redis()))
        monkeypatch.setattr(memory_superseded, "_sessions", lambda: world.sessions)

        redis_block = agent_memory.format_memory_for_prompt(
            await agent_memory.load_memory(world.agent_id, scope_id=world.user_id)
        )
        persistent_block = await _prompt(world, world.user_id)

        assert "PR review" not in redis_block
        assert "Captures are 1728x1080." in redis_block
        assert rule_b in persistent_block and rule_a not in persistent_block


async def test_supersedes_never_reaches_past_the_writers_own_memory(monkeypatch):
    async with _world(monkeypatch) as world:
        # Agent-level (an assignment run with no user) and another user's.
        async with world.sessions() as session:
            await pm.persist_memory(session, agent_id=world.agent_id, content="Shared: 12 slides.", memory_type="fact")
            await pm.persist_memory(
                session,
                agent_id=world.agent_id,
                content="Theirs: 12 slides.",
                memory_type="fact",
                user_id=world.other_user_id,
            )
            await session.commit()
        for content in ("Shared: 12 slides.", "Theirs: 12 slides.", "not-an-id"):
            target = content if content == "not-an-id" else str((await _entry(world, content)).id)
            answer = await _store(world, f"Mine after {content}", user_id=world.user_id, supersedes=target)
            assert f"Entry {target} was not superseded" in answer

        assert all(entry.superseded_at is None for entry in await _entries(world))


# ── invalidate_memory ─────────────────────────────────────────────────────────


async def test_invalidate_by_subject_retires_every_active_entry_on_it(monkeypatch):
    async with _world(monkeypatch) as world:
        await _store(world, "Ticket 42 is open.", user_id=world.user_id, memory_type="state", subject_key="ticket:42")
        await _store(world, "Ticket 42 blocks the release.", user_id=world.user_id, subject_key="ticket:42")
        await _store(world, "Ticket 43 is open.", user_id=world.user_id, memory_type="state", subject_key="ticket:43")

        answer = await _call(world, "invalidate_memory", world.user_id, subject_key="ticket:42")

        assert answer.startswith("Retired 2 memory entries (")
        block = await _prompt(world, world.user_id)
        assert "Ticket 42" not in block
        assert "Ticket 43 is open." in block
        rows = _by_content(await _search(world, world.user_id))
        assert rows["Ticket 42 is open."]["superseded"] is True
        assert rows["Ticket 42 is open."]["superseded_by"] is None
        assert rows["Ticket 43 is open."]["superseded"] is False
        # Ranked last.
        assert [row["superseded"] for row in await _search(world, world.user_id)] == [False, True, True]

        again = await _call(world, "invalidate_memory", world.user_id, subject_key="ticket:42")
        assert again == (
            "Nothing retired: no active entry of the memory this session writes matches subject_key ticket:42."
        )


async def test_invalidate_by_entry_id_retires_that_entry(monkeypatch):
    async with _world(monkeypatch) as world:
        await _store(world, "The launch is on Monday.", user_id=world.user_id, memory_type="fact")
        await _store(world, "The launch venue is Paris.", user_id=world.user_id, memory_type="fact")
        stale = await _entry(world, "The launch is on Monday.")

        answer = await _call(world, "invalidate_memory", world.user_id, entry_id=str(stale.id))

        assert answer.startswith(f"Retired 1 memory entry ({stale.id}).")
        block = await _prompt(world, world.user_id)
        assert "Monday" not in block and "Paris" in block


async def test_invalidate_never_retires_another_users_or_the_shared_memory(monkeypatch):
    async with _world(monkeypatch) as world:
        async with world.sessions() as session:
            await pm.persist_memory(
                session,
                agent_id=world.agent_id,
                content="Shared: doc X is in review.",
                memory_type="state",
                subject_key="doc:X",
            )
            await pm.persist_memory(
                session,
                agent_id=world.agent_id,
                content="Theirs: doc X is in review.",
                memory_type="state",
                subject_key="doc:X",
                user_id=world.other_user_id,
            )
            await pm.persist_memory(
                session,
                agent_id=world.agent_id,
                content="Shared: doc Y is in review.",
                memory_type="state",
                subject_key="doc:Y",
            )
            await session.commit()

        # A user who does not manage the agent retires nothing of its shared memory.
        refused = await _call(world, "invalidate_memory", world.other_user_id, subject_key="doc:Y")
        assert refused.startswith("Nothing retired")
        assert "1 matching entry of the agent's shared memory stays active" in refused
        assert all(entry.superseded_at is None for entry in await _entries(world))

        # The agent's owner (world.user_id) retires the shared entry, never another user's own one.
        answer = await _call(world, "invalidate_memory", world.user_id, subject_key="doc:X")

        shared = await _entry(world, "Shared: doc X is in review.")
        assert answer.startswith(f"Retired 1 memory entry ({shared.id}).")
        assert shared.superseded_at is not None
        assert (await _entry(world, "Theirs: doc X is in review.")).superseded_at is None
        assert (await _entry(world, "Shared: doc Y is in review.")).superseded_at is None


async def test_invalidate_needs_an_entry_or_a_subject(monkeypatch):
    async with _world(monkeypatch) as world:
        answer = await _call(world, "invalidate_memory", world.user_id)
        assert answer.startswith("Rejected: name what no longer holds")


# ── Two pools a reader sees ───────────────────────────────────────────────────


async def test_the_newest_state_a_reader_sees_wins_across_the_shared_and_the_own_pool(monkeypatch):
    async with _world(monkeypatch) as world:
        async with world.sessions() as session:
            await pm.persist_memory(
                session,
                agent_id=world.agent_id,
                content="Shared: doc X footer reads Draft",
                memory_type="state",
                subject_key="doc:X",
            )
            await session.commit()
        await _store(
            world, "Mine: doc X footer reads Final", user_id=world.user_id, memory_type="state", subject_key="doc:X"
        )

        # The user's session never flags the shared entry: every other user still reads it.
        assert (await _entry(world, "Shared: doc X footer reads Draft")).superseded_at is None
        for kwargs in ({}, {"query": _QUERY, "limit": 1}):
            mine = await _prompt(world, world.user_id, **kwargs)
            assert "Final" in mine and "Draft" not in mine, kwargs
        shared = await _prompt(world, None)
        assert "Shared: doc X footer reads Draft" in shared and "Final" not in shared
        theirs = await _prompt(world, world.other_user_id)
        assert "Draft" in theirs and "Final" not in theirs

        rows = _by_content(await _search(world, world.user_id))
        assert rows["Shared: doc X footer reads Draft"]["superseded"] is True
        assert rows["Mine: doc X footer reads Final"]["superseded"] is False
        assert _by_content(await _search(world, None))["Shared: doc X footer reads Draft"]["superseded"] is False


# ── Bounds and hygiene ────────────────────────────────────────────────────────


async def test_a_subject_keeps_a_bounded_history(monkeypatch):
    async with _world(monkeypatch) as world:
        for index in range(pm._MAX_SUPERSEDED_PER_SUBJECT + 3):
            await _store(
                world, f"Deck A has {index} slides.", user_id=world.user_id, memory_type="state", subject_key="deck:A"
            )

        entries = await _entries(world)
        active = [entry for entry in entries if entry.superseded_at is None]
        assert [entry.content for entry in active] == [f"Deck A has {pm._MAX_SUPERSEDED_PER_SUBJECT + 2} slides."]
        assert len(entries) == pm._MAX_SUPERSEDED_PER_SUBJECT + 1
        # The oldest went first.
        assert "Deck A has 0 slides." not in {entry.content for entry in entries}


async def test_a_retired_fact_can_be_stored_again(monkeypatch):
    async with _world(monkeypatch) as world:
        await _store(world, "Doc X is frozen.", user_id=world.user_id, memory_type="fact")
        await _store(world, "Doc X is frozen.", user_id=world.user_id, memory_type="fact")
        assert len(await _entries(world)) == 1  # deduplicated, as before

        frozen = (await _entries(world))[0]
        await _call(world, "invalidate_memory", world.user_id, entry_id=str(frozen.id))
        await _store(world, "Doc X is frozen.", user_id=world.user_id, memory_type="fact")

        entries = await _entries(world)
        assert sorted(entry.superseded_at is None for entry in entries) == [False, True]
        assert "Doc X is frozen." in await _prompt(world, world.user_id)


async def test_a_superseded_fact_is_never_merged_back(monkeypatch):
    merged_text = "- The active facts, merged, and nothing else."

    class _Merger:
        async def chat(self, **_kwargs):
            return SimpleNamespace(content=merged_text)

    monkeypatch.setattr(
        "app.llm.router.get_llm_for_user", AsyncMock(return_value=(_Merger(), "test-model"))
    )
    async with _world(monkeypatch) as world:
        async with world.sessions() as session:
            for index in range(4):
                await pm.persist_memory(
                    session, agent_id=world.agent_id, content=f"Old fact {index}.", memory_type="fact"
                )
            await session.commit()
        retired = await _entry(world, "Old fact 0.")
        async with world.sessions() as session:
            await pm.retire_memories(session, agent_id=world.agent_id, user_id=None, entry_id=str(retired.id))
            await session.commit()

        async with world.sessions() as session:
            merged = await pm.consolidate_agent_facts(session, agent_id=world.agent_id, min_facts=1)
            await session.commit()

        assert merged == 3
        entries = {entry.content: entry for entry in await _entries(world)}
        assert set(entries) == {"Old fact 0.", merged_text}
        assert entries["Old fact 0."].superseded_at is not None
        assert entries[merged_text].superseded_at is None
