"""A work item names the items it waits for and reopens by itself.

Against a real PostgreSQL (``tests/scratch_database.py``), as a non-admin
(rule 19): A, then B created with ``blocked_on=[A]``; B is blocked and not
stale, its context reads "blocked on items" with A linked; completing A
reopens B on the next sweep with the note ``unblocked: <A> done``; cancelling
a blocker hands the waiting item back to its owner with a note, once (#762); two creates
with ``dedupe`` and the same key and title answer the same item. Marked
``integration``: CI's integration job is the one with a PostgreSQL service.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

import app.models  # noqa: F401 - every table, for ALL_TABLES
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import work_item as wi_service
from app.services import work_item_context
from app.services.company_managed_writes import person_writer
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.work_item_owner import add_owner, owner_of

pytestmark = pytest.mark.integration

WRITER = person_writer(is_admin=False)


@asynccontextmanager
async def _database() -> AsyncIterator:
    async with scratch_database("wideps", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _member(sessions) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"member-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Member",
        hashed_password="x",
    )
    async with sessions() as db:
        db.add(user)
        await db.commit()
    await add_owner(sessions, user.id)
    return user


async def _create(user: User, title: str, **kwargs) -> dict:
    kwargs.setdefault("owner_agent_id", owner_of(user.id))
    return await wi_service.create_work_item(user_id=str(user.id), title=title, writer=WRITER, **kwargs)


async def test_a_blocked_item_reopens_once_its_blocker_is_done():
    async with _database() as sessions:
        user = await _member(sessions)
        a = await _create(user, "Ship the migration", correlation_key="dev:deps")
        b = await _create(user, "Backfill the rows", correlation_key="dev:deps", blocked_on=[a["id"]])

        assert b["status"] == "blocked"
        assert b["blocked_on"] == [a["id"]]
        # Quiet for longer than the stale window: still not stale, nobody is
        # expected to hold an item that waits on another.
        async with sessions() as db:
            await db.execute(
                update(WorkItem)
                .where(WorkItem.id == uuid.UUID(b["id"]))
                .values(last_activity_at=datetime.now(UTC) - timedelta(days=3))
            )
            await db.commit()
        assert not (await wi_service.get_work_item(b["id"], user_id=str(user.id)))["is_stale"]
        stale = await wi_service.list_work_items(user_id=str(user.id), stale_only=True)
        assert b["id"] not in {item["id"] for item in stale}

        # The view: "Blocked on 1 item", A named and linked.
        [described] = await work_item_context.describe_work_items(
            [await wi_service.get_work_item(b["id"], user_id=str(user.id))], viewer=user
        )
        assert described["context"]["pickup"]["state"] == "blocked_on_items"
        assert described["context"]["blocked_on"] == [
            {"id": a["id"], "title": "Ship the migration", "status": "open", "accessible": True}
        ]

        # Not yet: A is still open.
        assert await wi_service.release_satisfied_dependencies() == 0
        await wi_service.complete_work_item(a["id"], user_id=str(user.id), writer=WRITER)
        # Released by the completion itself (#11711): the sweep finds nothing left.
        assert await wi_service.release_satisfied_dependencies() == 0

        reopened = await wi_service.get_work_item(b["id"], user_id=str(user.id))
        assert reopened["status"] == "open"
        assert reopened["blocked_on"] == []
        assert reopened["claimed_by_agent_id"] is None
        assert reopened["claim_notes"][-1]["text"] == f"unblocked: {a['id']} done"
        # Once only.
        assert await wi_service.release_satisfied_dependencies() == 0


async def test_a_cancelled_blocker_hands_the_item_back_to_its_owner_once() -> None:
    """Nothing would ever release it: back to its owner's queue, never blocked forever (#762)."""
    async with _database() as sessions:
        user = await _member(sessions)
        a = await _create(user, "Ship the migration")
        b = await _create(user, "Backfill the rows", blocked_on=[a["id"]])
        # The cancellation itself hands it back (release_dependants_of); the sweep finds nothing left.
        await wi_service.complete_work_item(a["id"], user_id=str(user.id), status="cancelled", writer=WRITER)

        assert await wi_service.release_satisfied_dependencies() == 0
        back = await wi_service.get_work_item(b["id"], user_id=str(user.id))
        assert back is not None
        assert back["status"] == "open" and back["blocked_on"] == []
        notes = [note["text"] for note in back["claim_notes"]]
        assert notes == [f"surfaced: {a['id']} cancelled; re-point blocked_on or complete it"]


async def test_a_dependant_of_a_dev_item_is_released_at_its_completion_not_its_deployment():
    # #11919: blocked_on releases at the completion; no deployment reference is carried by default.
    from app.services.work_item_pull_requests import record_pull_request

    async with _database() as sessions:
        user = await _member(sessions)
        a = await _create(user, "Ship the shared component")
        b = await _create(user, "Build the next slice on it", blocked_on=[a["id"]])
        await record_pull_request(
            a["id"], user_id=str(user.id), writer=WRITER, repo="XTM-One-Platform/xtm-one", number=11919
        )
        late = await _create(user, "Build another slice on it", blocked_on=[a["id"]])
        for item_id in (b["id"], late["id"]):
            waiting = await wi_service.get_work_item(item_id, user_id=str(user.id))
            assert waiting["status"] == "blocked" and waiting["blocked_on_refs"] == []
            assert not any(n["text"].startswith("carried:") for n in waiting["claim_notes"] or [])

        await wi_service.complete_work_item(a["id"], user_id=str(user.id), writer=WRITER)
        for item_id in (b["id"], late["id"]):
            released = await wi_service.get_work_item(item_id, user_id=str(user.id))
            assert released["status"] == "open" and released["blocked_on"] == []
            assert released["blocked_on_refs"] == []


async def test_a_dependant_of_a_dev_item_waits_for_its_pull_request_deployment():
    # #9943, opt-in since #11919: X completes at its merge; D, blocked on X with release_on deployed, waits for the build.
    from app.services.work_item_pull_requests import record_pull_request

    async with _database() as sessions:
        user = await _member(sessions)
        x = await _create(user, "Fix the release path")
        other = await _create(user, "Another prerequisite")
        d = await _create(user, "Retest the fix", blocked_on=[x["id"], other["id"]], blocked_on_release_on="deployed")
        e = await _create(user, "Retest on another repository", blocked_on=[x["id"]], blocked_on_release_on="deployed")

        await record_pull_request(
            x["id"], user_id=str(user.id), writer=WRITER, repo="XTM-One-Platform/xtm-one", number=9944
        )
        carried = await wi_service.get_work_item(d["id"], user_id=str(user.id))
        assert carried["status"] == "blocked"
        assert carried["blocked_on"] == [x["id"], other["id"]]
        [ref] = carried["blocked_on_refs"]
        assert ref["kind"] == "deployment" and ref["number"] == 9944 and ref["carried_by"] == x["id"]
        assert carried["claim_notes"][-1]["text"].startswith("carried: waits for the deployment of")
        # Named twice: carried once.
        await record_pull_request(
            x["id"], user_id=str(user.id), writer=WRITER, repo="XTM-One-Platform/xtm-one", number=9944
        )
        again = await wi_service.get_work_item(d["id"], user_id=str(user.id))
        assert len(again["blocked_on_refs"]) == 1 and len(again["claim_notes"]) == len(carried["claim_notes"])

        # A repository without observed deployments carries nothing.
        y = await _create(user, "Fix elsewhere")
        f = await _create(user, "Retest elsewhere", blocked_on=[y["id"]], blocked_on_release_on="deployed")
        await record_pull_request(
            y["id"], user_id=str(user.id), writer=WRITER, repo="OpenCTI-Platform/opencti", number=5
        )
        assert (await wi_service.get_work_item(f["id"], user_id=str(user.id)))["blocked_on_refs"] == []

        # X completes at the merge: the dependency goes, D and E stay blocked on the deployment.
        await wi_service.complete_work_item(x["id"], user_id=str(user.id), writer=WRITER)
        await wi_service.complete_work_item(other["id"], user_id=str(user.id), writer=WRITER)
        await wi_service.complete_work_item(y["id"], user_id=str(user.id), writer=WRITER)
        await wi_service.release_satisfied_dependencies()
        for item_id in (d["id"], e["id"]):
            waiting = await wi_service.get_work_item(item_id, user_id=str(user.id))
            assert waiting["status"] == "blocked" and waiting["blocked_on"] == []
            assert [r["number"] for r in waiting["blocked_on_refs"]] == [9944]
        # The other repository: released at the completion, as before.
        assert (await wi_service.get_work_item(f["id"], user_id=str(user.id)))["status"] == "open"


async def test_the_completion_releases_its_waiters_with_no_sweep():
    # #11711: the write that finishes the blocker reopens its waiters itself.
    async with _database() as sessions:
        user = await _member(sessions)
        a = await _create(user, "Ship the migration")
        other = await _create(user, "Ship the API")
        b = await _create(user, "Backfill the rows", blocked_on=[a["id"]])
        c = await _create(user, "Retest both", blocked_on=[a["id"], other["id"]])

        await wi_service.complete_work_item(a["id"], user_id=str(user.id), writer=WRITER)
        released = await wi_service.get_work_item(b["id"], user_id=str(user.id))
        assert released["status"] == "open" and released["blocked_on"] == []
        assert released["claim_notes"][-1]["text"] == f"unblocked: {a['id']} done"
        # Still waiting on the other one.
        assert (await wi_service.get_work_item(c["id"], user_id=str(user.id)))["status"] == "blocked"

        # A status write to done releases the same way.
        await wi_service.update_work_item(other["id"], user_id=str(user.id), status="done", writer=WRITER)
        assert (await wi_service.get_work_item(c["id"], user_id=str(user.id)))["status"] == "open"
        assert await wi_service.release_satisfied_dependencies() == 0


async def test_an_item_blocked_after_the_pull_request_is_named_still_waits_for_its_deployment():
    # #11711: the carried reference no longer depends on the order of the writes.
    from app.services.work_item_pull_requests import record_pull_request

    async with _database() as sessions:
        user = await _member(sessions)
        x = await _create(user, "Fix the release path")
        await record_pull_request(
            x["id"], user_id=str(user.id), writer=WRITER, repo="XTM-One-Platform/xtm-one", number=11711
        )
        late = await _create(user, "Retest the fix", blocked_on=[x["id"]], blocked_on_release_on="deployed")
        updated = await _create(user, "Retest it too")
        await wi_service.update_work_item(
            updated["id"],
            user_id=str(user.id),
            blocked_on=[x["id"]],
            blocked_on_release_on="deployed",
            writer=WRITER,
        )

        for item_id in (late["id"], updated["id"]):
            carried = await wi_service.get_work_item(item_id, user_id=str(user.id))
            [ref] = carried["blocked_on_refs"]
            assert ref["kind"] == "deployment" and ref["number"] == 11711 and ref["carried_by"] == x["id"]
            assert carried["claim_notes"][-1]["text"].startswith("carried: waits for the deployment of")

        # Completed at the merge: the dependency goes, both wait for the build.
        await wi_service.complete_work_item(x["id"], user_id=str(user.id), writer=WRITER)
        for item_id in (late["id"], updated["id"]):
            waiting = await wi_service.get_work_item(item_id, user_id=str(user.id))
            assert waiting["status"] == "blocked" and waiting["blocked_on"] == []

        # A repository whose deployments are not observed carries nothing.
        y = await _create(user, "Fix elsewhere")
        await record_pull_request(
            y["id"], user_id=str(user.id), writer=WRITER, repo="OpenCTI-Platform/opencti", number=5
        )
        f = await _create(user, "Retest elsewhere", blocked_on=[y["id"]], blocked_on_release_on="deployed")
        assert (await wi_service.get_work_item(f["id"], user_id=str(user.id)))["blocked_on_refs"] == []


async def test_a_reason_only_write_keeps_the_item_blocked():
    async with _database() as sessions:
        user = await _member(sessions)
        a = await _create(user, "Ship the migration")
        b = await _create(user, "Backfill the rows", blocked_on=[a["id"]])
        kept = await wi_service.update_work_item(
            b["id"], user_id=str(user.id), blocked_reason="waits for the migration", writer=WRITER
        )
        assert kept["status"] == "blocked" and kept["blocked_on"] == [a["id"]]
        assert kept["blocked_reason"] == "waits for the migration"


async def test_update_sets_and_clears_the_dependency():
    async with _database() as sessions:
        user = await _member(sessions)
        a = await _create(user, "Ship the migration")
        b = await _create(user, "Backfill the rows")
        assert b["status"] == "open"

        blocked = await wi_service.update_work_item(
            b["id"], user_id=str(user.id), blocked_on=[a["id"], b["id"]], writer=WRITER
        )
        # Its own id is dropped; the rest makes it blocked.
        assert blocked["status"] == "blocked" and blocked["blocked_on"] == [a["id"]]

        cleared = await wi_service.update_work_item(
            b["id"], user_id=str(user.id), blocked_on=[], status="open", writer=WRITER
        )
        assert cleared["status"] == "open" and cleared["blocked_on"] == []

        with pytest.raises(ValueError, match="cannot see"):
            await wi_service.update_work_item(
                b["id"], user_id=str(user.id), blocked_on=[str(uuid.uuid4())], writer=WRITER
            )


async def test_an_agent_write_dropping_an_open_blocker_is_refused():
    # #9714: blocked_on replaces the list; dropping an open item while the item
    # stays blocked (here: swapped for a deployment) is refused, the item named.
    async with _database() as sessions:
        user = await _member(sessions)
        a = await _create(user, "Ship the migration")
        c = await _create(user, "Ship the API")
        b = await _create(user, "Backfill the rows", blocked_on=[a["id"], c["id"]])
        # A repository whose deployments the platform observes (#9879 refuses the others).
        deployment = [{"repo": "XTM-One-Platform/xtm-one", "kind": "deployment", "number": 7, "release_on": "deployed"}]
        run = str(uuid.uuid4())

        with pytest.raises(ValueError, match=f"still open: {a['id']} \\(open\\), {c['id']} \\(open\\)"):
            await wi_service.update_work_item(
                b["id"],
                user_id=str(user.id),
                blocked_on=[],
                blocked_on_refs=deployment,
                writer=WRITER,
                writer_run_ids=[run],
            )
        kept = await wi_service.get_work_item(b["id"], user_id=str(user.id))
        assert kept["blocked_on"] == [a["id"], c["id"]] and kept["blocked_on_refs"] == []

        # A blocker the writer's own run holds may go (it hands over to its deployment).
        async with sessions() as db:
            await db.execute(
                update(WorkItem).where(WorkItem.id == uuid.UUID(a["id"])).values(claimed_by_run_id=uuid.UUID(run))
            )
            await db.commit()
        swapped = await wi_service.update_work_item(
            b["id"],
            user_id=str(user.id),
            blocked_on=[c["id"]],
            blocked_on_refs=deployment,
            writer=WRITER,
            writer_run_ids=[run],
        )
        assert swapped["status"] == "blocked" and swapped["blocked_on"] == [c["id"]]

        # On purpose: the flag drops it; leaving blocked is never refused.
        dropped = await wi_service.update_work_item(
            b["id"],
            user_id=str(user.id),
            blocked_on=[],
            writer=WRITER,
            writer_run_ids=[run],
            drop_open_blockers=True,
        )
        assert dropped["status"] == "blocked" and dropped["blocked_on"] == []
        reopened = await wi_service.update_work_item(
            b["id"], user_id=str(user.id), blocked_on=[c["id"]], writer=WRITER, writer_run_ids=[run]
        )
        released = await wi_service.update_work_item(
            reopened["id"],
            user_id=str(user.id),
            blocked_on=[],
            blocked_on_refs=[],
            status="in_progress",
            writer=WRITER,
            writer_run_ids=[run],
        )
        assert released["status"] == "in_progress" and released["blocked_on"] == []


async def test_dedupe_answers_the_existing_unfinished_item():
    async with _database() as sessions:
        user = await _member(sessions)
        first = await _create(user, "Split the remainder of item 1", correlation_key="dev:x", dedupe=True)
        second = await _create(user, "  split the REMAINDER of item 1 ", correlation_key="dev:x", dedupe=True)
        assert second["id"] == first["id"] and second["deduplicated"] is True

        # Another key, or no dedupe, creates.
        other = await _create(user, "Split the remainder of item 1", correlation_key="dev:y", dedupe=True)
        assert other["id"] != first["id"]
        plain = await _create(user, "Split the remainder of item 1", correlation_key="dev:x")
        assert plain["id"] != first["id"]

        # A finished item is not a duplicate.
        for item_id in (first["id"], plain["id"]):
            await wi_service.complete_work_item(item_id, user_id=str(user.id), writer=WRITER)
        fresh = await _create(user, "Split the remainder of item 1", correlation_key="dev:x", dedupe=True)
        assert fresh["id"] not in (first["id"], plain["id"])
        assert "deduplicated" not in fresh


async def test_dedupe_by_key_answers_the_unfinished_item_whatever_its_title():
    async with _database() as sessions:
        user = await _member(sessions)
        first = await _create(user, "Retest the partial checkout", correlation_key="dev:k", dedupe=True)
        reworded = await _create(
            user, "Fix the retest FAIL of the partial checkout", correlation_key="dev:k", dedupe=True, dedupe_by="key"
        )
        assert reworded["id"] == first["id"]
        assert reworded["deduplicated"] is True and reworded["dedupe_by"] == "key"
        listed = await wi_service.list_work_items(user_id=str(user.id), correlation_key="dev:k")
        assert [item["id"] for item in listed] == [first["id"]]

        # The default still matches the title: a reworded title creates.
        titled = await _create(user, "Another title", correlation_key="dev:k", dedupe=True)
        assert titled["id"] != first["id"] and "deduplicated" not in titled

        # The oldest unfinished item answers once the first is finished.
        await wi_service.complete_work_item(first["id"], user_id=str(user.id), writer=WRITER)
        again = await _create(user, "Third title", correlation_key="dev:k", dedupe=True, dedupe_by="key")
        assert again["id"] == titled["id"] and again["deduplicated"] is True


async def test_dedupe_similar_answers_a_near_duplicate_slug_of_the_same_owner():
    async with _database() as sessions:
        user = await _member(sessions)
        first = await _create(
            user,
            "Main-red trigger: skip fork PR runs",
            correlation_key="fleet:main-red-trigger-fork-filter",
            dedupe=True,
            dedupe_by="similar",
        )
        twin = await _create(
            user,
            "Main-red trigger filter: skip workflow runs of fork PRs",
            correlation_key="fleet:main-red-trigger-fork-pr-filter",
            dedupe=True,
            dedupe_by="similar",
        )
        assert twin["id"] == first["id"]
        assert twin["deduplicated"] is True and twin["dedupe_by"] == "similar"

        # The default mode still files the second slug; a child key is a new item.
        exact = await _create(user, "Other", correlation_key="fleet:main-red-trigger-fork-pr-filter", dedupe=True)
        assert exact["id"] != first["id"]
        child = await _create(
            user,
            "Main-red trigger: skip fork PR runs",
            correlation_key="fleet:main-red-trigger-fork-filter-8919",
            dedupe=True,
            dedupe_by="similar",
        )
        assert child["id"] != first["id"] and "deduplicated" not in child


async def test_dedupe_similar_answers_a_recent_twin_under_another_key():
    async with _database() as sessions:
        user = await _member(sessions)
        first = await _create(
            user,
            "Inbox finalizer / PR-linked item: no blocked_on_refs merged_or_closed on a UI PR awaiting its UX round",
            correlation_key="dev:test-a",
            dedupe=True,
            dedupe_by="similar",
        )
        twin = await _create(
            user,
            "The inbox finalizer parks a UI PR item on merged_or_closed while its UX round is owed",
            correlation_key="dev:test-b",
            dedupe=True,
            dedupe_by="similar",
        )
        assert twin["id"] == first["id"] and twin["deduplicated"] is True

        # Outside the 30 minute window the second key is filed.
        async with sessions() as db:
            await db.execute(
                update(WorkItem)
                .where(WorkItem.id == uuid.UUID(first["id"]))
                .values(created_at=datetime.now(UTC) - timedelta(minutes=31))
            )
            await db.commit()
        late = await _create(
            user,
            "The inbox finalizer parks a UI PR item on merged_or_closed while its UX round is owed",
            correlation_key="dev:test-b",
            dedupe=True,
            dedupe_by="similar",
        )
        assert late["id"] != first["id"] and "deduplicated" not in late


async def test_parallel_dedupe_filings_converge_on_one_item():
    import asyncio

    async with _database() as sessions:
        user = await _member(sessions)
        results = await asyncio.gather(
            *(
                _create(
                    user,
                    "Main-red trigger: skip fork PR runs",
                    correlation_key="fleet:main-red-trigger-fork-filter",
                    dedupe=True,
                )
                for _ in range(4)
            )
        )
        assert len({item["id"] for item in results}) == 1
