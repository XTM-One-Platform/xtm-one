"""Memory Steward passes on PostgreSQL (#8258).

A pass reads one lane and writes into it only, applies its changes as
superseded flags and rows, does nothing on a lane unchanged since its last
pass, writes nothing in dry-run, and stops when consent goes, before reading
or right before writing. Stubs: ``tests/memory_steward_pass_world.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select, update

from app.llm.provider_access import acting_user_id
from app.models.agent_memory_entry import AgentMemoryEntry
from app.models.memory_consolidation import MemoryConsolidation, MemoryLaneCheckpoint
from app.models.user import User
from app.services import persistent_memory as pm
from app.services.concierge import memory_history as history
from app.services.concierge import memory_steward as steward
from app.services.concierge import memory_steward_deps as deps
from app.services.concierge.memory_steward_plan import Verdict
from tests.memory_steward_pass_world import pass_world, run_pass, seed_owner_lane
from tests.memory_steward_world import count, entry, state

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _undo(world, row_id):
    async with world.sessions() as session:
        await history.undo_consolidation(session, row_id, actor=await session.get(User, world.owner), user_id=world.owner)
        await session.commit()


async def test_a_pass_applies_in_its_lane_only_and_is_idempotent(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        ids = await seed_owner_lane(world)

        result = await run_pass(world, world.owner)

        assert (result.status, result.counts) == ("applied", {"merge": 1, "remove": 1, "demote": 1})
        lane = await state(world)
        assert lane[ids["dup_old"]] == (True, ids["dup_new"])
        assert lane[ids["edr_old"]] == (True, ids["edr_new"])
        assert lane[ids["trip"]] == (True, None)
        for kept in ("dup_new", "edr_new", "agent_dup", "other_dup"):
            assert lane[ids[kept]] == (False, None)
        async with world.sessions() as session:
            rows = (await session.execute(select(MemoryConsolidation))).scalars().all()
        assert sorted((r.operation, r.user_id, tuple(r.before_entry_ids)) for r in rows) == sorted(
            [
                ("merge", world.owner, (str(ids["dup_old"]),)),
                ("remove", world.owner, (str(ids["edr_old"]),)),
                ("demote", world.owner, (str(ids["trip"]),)),
            ]
        )
        assert world.reads == [{"agent_id": world.agent, "user_id": world.owner}]
        assert [a["action"] for a in world.audits] == ["memory_merge", "memory_remove", "memory_demote"]
        assert all("Prefers" not in repr(a["details"]) for a in world.audits)

        again = await run_pass(world, world.owner)

        assert (again.status, again.counts) == ("unchanged", {})
        assert world.model_calls == [world.lane(world.owner)]


async def test_dry_run_writes_nothing(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        ids = await seed_owner_lane(world)
        world.dry_run = True
        before = await state(world)

        result = await run_pass(world, world.owner)

        assert (result.status, result.counts) == ("planned", {"merge": 1, "remove": 1, "demote": 1})
        assert await state(world) == before
        assert await count(world, MemoryConsolidation) == 0
        assert await count(world, MemoryLaneCheckpoint) == 0
        assert sorted(w["operation"] for w in world.intercepted) == ["demote", "merge", "remove"]
        merge = next(w for w in world.intercepted if w["operation"] == "merge")
        assert merge == {
            "agent_id": world.agent,
            "user_id": world.owner,
            "operation": "merge",
            "details": {"entries": [str(ids["dup_old"])]},
        }


async def test_consent_is_checked_before_reading_and_again_before_writing(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        await seed_owner_lane(world)
        world.readable.discard((world.agent, world.owner))

        refused = await run_pass(world, world.owner)

        assert (refused.status, refused.reason) == ("refused", "consent")
        assert world.model_calls == []
        assert world.reads == []

        # Opted out while the model answered: nothing is written.
        world.readable.add((world.agent, world.owner))
        answer = world.verdicts[world.owner]

        def revoke_then_answer(candidates):
            world.readable.discard((world.agent, world.owner))
            return answer(candidates)

        world.verdicts[world.owner] = revoke_then_answer
        before = await state(world)

        revoked = await run_pass(world, world.owner)

        assert (revoked.status, revoked.reason) == ("refused", "consent")
        assert await state(world) == before
        assert await count(world, MemoryConsolidation) == 0


async def test_an_undone_merge_is_not_made_again(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        ids = await seed_owner_lane(world)
        await run_pass(world, world.owner)
        async with world.sessions() as session:
            merge = (
                await session.execute(select(MemoryConsolidation.id).where(MemoryConsolidation.operation == "merge"))
            ).scalar_one()
        await _undo(world, merge)

        # The undo changed the lane, so a pass runs; the merge is not redone.
        again = await run_pass(world, world.owner)

        assert (again.status, again.counts) == ("applied", {})
        assert (await state(world))[ids["dup_old"]] == (False, None)


async def test_a_recurring_observation_is_promoted_and_its_undo_sticks(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        await entry(world, "Works on the SOC team", user=world.owner, days_old=5)
        for version in range(3):
            world.summaries[world.owner] = f"- Prefers short answers with a table\n- Topic {version}: phishing"
            result = await run_pass(world, world.owner)

        assert (result.status, result.counts) == ("applied", {"promote": 1})
        async with world.sessions() as session:
            promoted = (
                await session.execute(select(AgentMemoryEntry).where(AgentMemoryEntry.source == "memory_steward"))
            ).scalar_one()
            row = (await session.execute(select(MemoryConsolidation))).scalar_one()
        assert (promoted.content, promoted.user_id, promoted.agent_id, promoted.memory_type) == (
            "Prefers short answers with a table",
            world.owner,
            world.agent,
            "fact",
        )
        assert (row.operation, row.after_entry_id, row.before_entry_ids) == ("promote", promoted.id, [])

        await _undo(world, row.id)
        world.summaries[world.owner] = "- Prefers short answers with a table\n- Topic 4: DLP"
        again = await run_pass(world, world.owner)

        assert (again.status, again.counts) == ("applied", {})
        assert promoted.id not in await state(world)


async def test_the_lanes_listed_are_the_readable_ones_never_checked_first(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        await seed_owner_lane(world)
        await run_pass(world, world.owner)
        world.readable.discard((world.agent, world.other))

        async with world.sessions() as session:
            lanes = await steward.list_eligible_lanes(session)

        assert [(lane.user_id, lane.active_entries, lane.checked_at is not None) for lane in lanes] == [
            (None, 1, False),
            (world.owner, 2, True),
        ]


async def test_an_unchanged_lane_counts_as_checked_now(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        await seed_owner_lane(world)
        await run_pass(world, world.owner)
        async with world.sessions() as session:
            await session.execute(update(MemoryLaneCheckpoint).values(checked_at=datetime.now(UTC) - timedelta(days=9)))
            await session.commit()

        assert (await run_pass(world, world.owner)).status == "unchanged"

        async with world.sessions() as session:
            checked = (await session.execute(select(MemoryLaneCheckpoint.checked_at))).scalar_one()
        assert checked > datetime.now(UTC) - timedelta(minutes=5)


async def test_a_large_lane_is_weighed_in_windows_until_whole(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        monkeypatch.setattr(steward, "MAX_LANE_ENTRIES", 4)
        # Two duplicates among the oldest: outside the first window.
        old = await entry(world, "Uses a Mac at work", user=world.owner, days_old=30)
        older = await entry(world, "uses a mac at work", user=world.owner, days_old=31)
        for n in range(4):
            await entry(world, f"Fact number {n} about the SOC", user=world.owner, days_old=n)

        results = [await run_pass(world, world.owner) for _ in range(3)]

        # Window 1: the newest four. Window 2: the newest two and the oldest
        # two, the merge. Then whole, and the lane reads as unchanged.
        assert [(r.status, r.counts) for r in results] == [
            ("applied", {}),
            ("applied", {"merge": 1}),
            ("unchanged", {}),
        ]
        assert (await state(world))[older] == (True, old)


async def test_a_busy_lane_is_skipped(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        await seed_owner_lane(world)
        world.busy.add(world.lane(world.owner).key)

        result = await run_pass(world, world.owner)

        assert (result.status, result.reason) == ("skipped", "busy")
        assert world.model_calls == []


async def test_a_write_that_cannot_be_audited_does_not_happen(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        ids = await seed_owner_lane(world)

        async def no_anchor(_db, _agent_id, _user_id):
            return None

        monkeypatch.setattr(deps, "_anchor", no_anchor)

        result = await run_pass(world, world.owner)

        assert (result.status, result.reason) == ("refused", "consent")
        assert (await state(world))[ids["dup_old"]] == (False, None)
        assert await count(world, MemoryConsolidation) == 0


async def test_an_edited_promotion_is_not_undone(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        for version in range(3):
            world.summaries[world.owner] = f"- Prefers short answers with a table\n- Topic {version}: phishing"
            await run_pass(world, world.owner)
        async with world.sessions() as session:
            row = (await session.execute(select(MemoryConsolidation))).scalar_one()
            await session.execute(
                update(AgentMemoryEntry)
                .where(AgentMemoryEntry.id == row.after_entry_id)
                .values(content="Prefers short answers with a table and a summary")
            )
            await session.commit()

        with pytest.raises(history.UndoRefusedError):
            await _undo(world, row.id)

        assert row.after_entry_id in await state(world)


async def _promote(world) -> MemoryConsolidation:
    for version in range(3):
        world.summaries[world.owner] = f"- Prefers short answers with a table\n- Topic {version}: phishing"
        await run_pass(world, world.owner)
    async with world.sessions() as session:
        return (await session.execute(select(MemoryConsolidation))).scalar_one()


async def test_a_promoted_entry_deleted_by_hand_is_not_promoted_again(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        row = await _promote(world)
        async with world.sessions() as session:
            await session.execute(delete(AgentMemoryEntry).where(AgentMemoryEntry.id == row.after_entry_id))
            await history.purge_entry_consolidations(session, row.after_entry_id)
            await session.commit()

        # Still recurring in the summary: the tombstone, not the row, stops it.
        world.summaries[world.owner] = "- Prefers short answers with a table\n- Topic 4: DLP"
        again = await run_pass(world, world.owner)

        assert (again.status, again.counts) == ("applied", {})
        assert await count(world, MemoryConsolidation) == 0
        assert await count(world, AgentMemoryEntry) == 0


async def test_a_promotion_deleted_after_its_row_expired_is_not_promoted_again(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        row = await _promote(world)
        async with world.sessions() as session:
            await session.execute(
                update(MemoryConsolidation)
                .where(MemoryConsolidation.id == row.id)
                .values(applied_at=datetime.now(UTC) - timedelta(days=history.RETENTION_DAYS + 1))
            )
            await history.purge_expired_consolidations(session)
            await session.execute(delete(AgentMemoryEntry).where(AgentMemoryEntry.id == row.after_entry_id))
            await history.purge_entry_consolidations(session, row.after_entry_id)
            await session.commit()

        world.summaries[world.owner] = "- Prefers short answers with a table\n- Topic 5: DLP"
        again = await run_pass(world, world.owner)

        assert (again.status, again.counts) == ("applied", {})
        assert await count(world, AgentMemoryEntry) == 0


async def test_an_undone_change_stays_undone_past_the_rows_retention(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        ids = await seed_owner_lane(world)
        await run_pass(world, world.owner)
        async with world.sessions() as session:
            merge = (
                await session.execute(select(MemoryConsolidation.id).where(MemoryConsolidation.operation == "merge"))
            ).scalar_one()
        await _undo(world, merge)
        async with world.sessions() as session:
            await session.execute(
                update(MemoryConsolidation)
                .where(MemoryConsolidation.id == merge)
                .values(undone_at=datetime.now(UTC) - timedelta(days=91))
            )
            await history.purge_expired_consolidations(session)
            await session.commit()
        await entry(world, "Works on the SOC team", user=world.owner, days_old=0)

        again = await run_pass(world, world.owner)

        assert (again.status, again.counts) == ("applied", {})
        assert (await state(world))[ids["dup_old"]] == (False, None)


async def test_a_promotion_is_embedded_for_the_lanes_user(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        acting = []

        async def embed(_db, content, **_kwargs):
            acting.append(acting_user_id())
            return [1.0] + [0.0] * 7, "test-embedding"

        monkeypatch.setattr(pm, "embed_text_with_model", embed)

        await _promote(world)

        assert acting and set(acting) == {str(world.owner)}


async def test_a_summary_purged_during_the_pass_promotes_nothing(monkeypatch) -> None:
    async with pass_world(monkeypatch) as world:
        await entry(world, "Works on the SOC team", user=world.owner, days_old=5)
        for version in range(2):
            world.summaries[world.owner] = f"- Prefers short answers with a table\n- Topic {version}: phishing"
            await run_pass(world, world.owner)
        world.summaries[world.owner] = "- Prefers short answers with a table\n- Topic 2: phishing"

        def purge_then_answer(_candidates):
            world.summaries.pop(world.owner)
            return Verdict()

        world.verdicts[world.owner] = purge_then_answer

        result = await run_pass(world, world.owner)

        assert (result.status, result.counts) == ("applied", {})
        assert await count(world, MemoryConsolidation) == 0
