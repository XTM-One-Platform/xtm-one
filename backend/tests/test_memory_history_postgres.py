"""Memory Steward history: undo, holds and purge on PostgreSQL (#8258).

A consolidated-away entry stays restorable for 30 days (B19): its undo puts
back exactly what the change flagged, or nothing; the memory hygiene passes
leave it to the consolidation purge; a user's opt-out or own deletion purges
at once. Rows are recorded the way a pass records them
(``tests/memory_steward_world.applied``).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.models.agent_memory_entry import AgentMemoryEntry
from app.models.memory_consolidation import MemoryConsolidation, MemoryConsolidationTombstone, MemoryLaneCheckpoint
from app.models.user import User
from app.services import persistent_memory as pm
from app.services.health import memory_history as history
from app.services.health.consent import Revocation, apply_revocation
from tests.memory_steward_world import applied, count, entry, make_world, state, world_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@asynccontextmanager
async def _world():
    async with world_database() as (db, session):
        yield await make_world(session, db.sessions)


async def _owner_lane(world) -> dict[str, uuid.UUID]:
    """The owner's lane after a pass: a merge, a remove, a demote; untouched
    entries in the agent lane and another user's lane."""
    ids = {
        "dup_old": await entry(world, "Prefers answers in French", user=world.owner, days_old=9),
        "dup_new": await entry(world, "prefers answers in french", user=world.owner, days_old=1),
        "edr_old": await entry(world, "Their EDR is Defender", user=world.owner, days_old=20),
        "edr_new": await entry(world, "Their EDR is CrowdStrike", user=world.owner, days_old=2),
        "trip": await entry(world, "Travels to Tokyo next Monday", user=world.owner, days_old=12),
        "agent_fact": await entry(world, "Runs every morning", user=None, days_old=3),
        "other_fact": await entry(world, "Prefers answers in French", user=world.other, days_old=3),
    }
    ids["merge"] = await applied(world, "merge", user=world.owner, before=(ids["dup_old"],), after=ids["dup_new"])
    ids["remove"] = await applied(world, "remove", user=world.owner, before=(ids["edr_old"],), after=ids["edr_new"])
    ids["demote"] = await applied(world, "demote", user=world.owner, before=(ids["trip"],))
    return ids


async def _undo(world, row_id, **lane):
    async with world.sessions() as session:
        actor = await session.get(User, world.owner)
        try:
            return await history.undo_consolidation(session, row_id, actor=actor, **lane)
        finally:
            await session.commit()


async def test_undo_restores_exactly_what_the_change_flagged_once() -> None:
    async with _world() as world:
        ids = await _owner_lane(world)

        row = await _undo(world, ids["merge"], user_id=world.owner)

        assert row.undone_at is not None
        assert (await state(world))[ids["dup_old"]] == (False, None)
        assert (await state(world))[ids["edr_old"]] == (True, ids["edr_new"])
        with pytest.raises(history.UndoRefusedError):
            await _undo(world, ids["merge"], user_id=world.owner)


@pytest.mark.parametrize("lane", ["other_user", "agent_lane"])
async def test_a_row_is_found_only_in_the_lane_the_caller_names(lane: str) -> None:
    async with _world() as world:
        ids = await _owner_lane(world)
        named = {"user_id": world.other} if lane == "other_user" else {"agent_id": world.agent}

        with pytest.raises(history.ConsolidationNotFoundError):
            await _undo(world, ids["merge"], **named)

        assert (await state(world))[ids["dup_old"]] == (True, ids["dup_new"])


async def test_undo_refuses_when_an_entry_changed_since() -> None:
    async with _world() as world:
        ids = await _owner_lane(world)
        async with world.sessions() as session:
            await session.execute(
                update(AgentMemoryEntry).where(AgentMemoryEntry.id == ids["edr_old"]).values(superseded_by_id=ids["trip"])
            )
            await session.commit()

        with pytest.raises(history.UndoRefusedError):
            await _undo(world, ids["remove"], user_id=world.owner)

        assert (await state(world))[ids["edr_old"]] == (True, ids["trip"])
        async with world.sessions() as session:
            assert (await session.get(MemoryConsolidation, ids["remove"])).undone_at is None


async def test_undo_refuses_past_the_retention_and_for_a_flag() -> None:
    async with _world() as world:
        a = await entry(world, "On call on Mondays", user=world.owner, days_old=9)
        b = await entry(world, "On call on Fridays", user=world.owner, days_old=1)
        old = await applied(world, "remove", user=world.owner, before=(a,), after=b, days_ago=31)
        flag = await applied(world, "flag", user=world.owner, before=(a, b))

        for row_id in (old, flag):
            with pytest.raises(history.UndoRefusedError):
                await _undo(world, row_id, user_id=world.owner)

        assert (await state(world))[a] == (True, b)


async def test_undoing_a_promotion_deletes_the_promoted_entry() -> None:
    async with _world() as world:
        text = "Prefers short answers with a table"
        promoted = await entry(world, text, user=world.owner, days_old=0)
        row_id = await applied(
            world, "promote", user=world.owner, after=promoted, fingerprint=history.promotion_fingerprint(text)
        )

        await _undo(world, row_id, user_id=world.owner)

        assert promoted not in await state(world)


async def test_held_entries_survive_the_cap_and_the_prune_until_the_purge() -> None:
    async with _world() as world:
        ids = await _owner_lane(world)
        async with world.sessions() as session:
            await session.execute(update(AgentMemoryEntry).values(created_at=datetime.now(UTC) - timedelta(days=200)))
            await session.commit()
        async with world.sessions() as session:
            await pm.enforce_memory_cap(session, agent_id=str(world.agent), max_facts=1)
            await pm.prune_old_memories(session, agent_id=str(world.agent))
            await session.commit()

        left = await state(world)
        assert {ids["dup_old"], ids["edr_old"], ids["trip"]} <= set(left)
        assert ids["agent_fact"] not in left

        async with world.sessions() as session:
            await session.execute(update(MemoryConsolidation).values(applied_at=datetime.now(UTC) - timedelta(days=31)))
            await session.commit()
        async with world.sessions() as session:
            purged = await history.purge_expired_consolidations(session)
            await session.commit()

        assert purged == {"rows": 3, "entries": 3}
        assert not {ids["dup_old"], ids["edr_old"], ids["trip"]} & set(await state(world))
        assert await count(world, MemoryConsolidation) == 0


async def test_undone_rows_are_kept_ninety_days() -> None:
    async with _world() as world:
        await _owner_lane(world)
        async with world.sessions() as session:
            await session.execute(update(MemoryConsolidation).values(undone_at=datetime.now(UTC) - timedelta(days=89)))
            await session.commit()
        async with world.sessions() as session:
            kept = await history.purge_expired_consolidations(session)
            await session.execute(update(MemoryConsolidation).values(undone_at=datetime.now(UTC) - timedelta(days=91)))
            gone = await history.purge_expired_consolidations(session)
            await session.commit()

        assert (kept, gone) == ({"rows": 0, "entries": 0}, {"rows": 3, "entries": 0})


async def test_the_users_opt_out_purges_what_is_held_at_once() -> None:
    async with _world() as world:
        ids = await _owner_lane(world)
        agent_old = await entry(world, "runs every morning", user=None, days_old=5)
        await applied(world, "merge", user=None, before=(agent_old,), after=ids["agent_fact"])
        async with world.sessions() as session:
            session.add_all(
                [
                    MemoryLaneCheckpoint(lane_key=f"{world.agent}:{world.owner}", agent_id=world.agent, user_id=world.owner, fingerprint="x"),
                    MemoryLaneCheckpoint(lane_key=f"{world.agent}:agent", agent_id=world.agent, user_id=None, fingerprint="y"),
                ]
            )
            await session.commit()

        # Level 1's opt-out runs the revocation handlers in its transaction.
        async with world.sessions() as session:
            await apply_revocation(session, Revocation(kind="user_opt_out", user_id=world.owner))
            await session.commit()

        left = await state(world)
        assert not {ids["dup_old"], ids["edr_old"], ids["trip"]} & set(left)
        assert {ids["dup_new"], ids["edr_new"], ids["agent_fact"], ids["other_fact"], agent_old} <= set(left)
        async with world.sessions() as session:
            rows = (await session.execute(select(MemoryConsolidation.user_id))).scalars().all()
            checkpoints = (await session.execute(select(MemoryLaneCheckpoint.user_id))).scalars().all()
        assert rows == [None]
        assert checkpoints == [None]


async def test_deleting_an_entry_by_hand_drops_the_rows_naming_it() -> None:
    async with _world() as world:
        ids = await _owner_lane(world)
        async with world.sessions() as session:
            await session.execute(
                AgentMemoryEntry.__table__.delete().where(AgentMemoryEntry.id == ids["dup_new"])
            )
            await history.purge_entry_consolidations(session, ids["dup_new"])
            await session.commit()

        async with world.sessions() as session:
            left = set((await session.execute(select(MemoryConsolidation.id))).scalars().all())
        assert left == {ids["remove"], ids["demote"]}
        assert ids["dup_old"] not in await state(world)


async def test_deleting_the_winner_of_a_contradiction_restores_the_older_entry() -> None:
    async with _world() as world:
        ids = await _owner_lane(world)
        async with world.sessions() as session:
            await session.execute(AgentMemoryEntry.__table__.delete().where(AgentMemoryEntry.id == ids["edr_new"]))
            await history.purge_entry_consolidations(session, ids["edr_new"])
            await session.commit()

        assert (await state(world))[ids["edr_old"]] == (False, None)
        async with world.sessions() as session:
            left = set((await session.execute(select(MemoryConsolidation.id))).scalars().all())
        assert left == {ids["merge"], ids["demote"]}


async def test_tombstones_outlive_their_rows_until_the_users_purge() -> None:
    async with _world() as world:
        ids = await _owner_lane(world)
        text = "Prefers short answers with a table"
        promoted = await entry(world, text, user=world.owner, days_old=0)
        fingerprint = history.promotion_fingerprint(text)
        await applied(world, "promote", user=world.owner, after=promoted, fingerprint=fingerprint)
        merge_fingerprint = (await _undo(world, ids["merge"], user_id=world.owner)).fingerprint
        async with world.sessions() as session:
            await session.execute(AgentMemoryEntry.__table__.delete().where(AgentMemoryEntry.id == promoted))
            await history.purge_entry_consolidations(session, promoted)
            await session.execute(update(MemoryConsolidation).values(undone_at=datetime.now(UTC) - timedelta(days=91)))
            await history.purge_expired_consolidations(session)
            await session.commit()

        assert await count(world, MemoryConsolidation) == 0
        async with world.sessions() as session:
            lane = history.Lane(world.agent, world.owner)
            assert await history.known_fingerprints(session, lane) == {fingerprint, merge_fingerprint}
            await history.purge_lane_consolidations(session, user_id=world.owner)
            await session.commit()
        assert await count(world, MemoryConsolidationTombstone) == 0


async def test_the_owners_list_is_their_lanes_only() -> None:
    async with _world() as world:
        ids = await _owner_lane(world)
        await applied(world, "merge", user=None, before=(), after=ids["agent_fact"])
        async with world.sessions() as session:
            mine = await history.list_consolidations(session, user_id=world.owner)
            theirs = await history.list_consolidations(session, user_id=world.other)
            agent_lane = await history.list_consolidations(session, agent_id=world.agent, agent_lane_only=True)

        assert {row.id for row in mine} == {ids["merge"], ids["remove"], ids["demote"]}
        assert theirs == []
        assert [row.user_id for row in agent_lane] == [None]


async def test_the_digest_counts_what_is_applied_and_not_undone() -> None:
    async with _world() as world:
        ids = await _owner_lane(world)
        await _undo(world, ids["merge"], user_id=world.owner)
        async with world.sessions() as session:
            counts = await history.consolidation_counts(session, since=datetime.now(UTC) - timedelta(hours=1))

        assert counts == {"remove": 1, "demote": 1}
