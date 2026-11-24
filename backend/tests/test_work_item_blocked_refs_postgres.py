"""A pull request or issue closing releases the work items blocked on it.

Against a real PostgreSQL (``tests/scratch_database.py``), as a non-admin
(rule 19). One ``pull_request`` closed (merged) delivery releases two items
(``release_on`` merged, and merged_or_closed) and leaves a third, on another
pull request, blocked; a pull request closed unmerged (superseded) surfaces an
item that wanted it merged, back to open with a note; the item's own pull request closed and
replaced releases it; an issue closed as a duplicate releases the item waiting
on it. The refusal of a blocked status with no why goes through the service.
Marked ``integration``: CI's integration job is the one with a PostgreSQL
service.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

import app.models  # noqa: F401 - every table, for ALL_TABLES
from app.integrations.pull_request_events import PullRequestEvent
from app.models.user import User
from app.services import work_item as wi_service
from app.services.company_managed_writes import person_writer
from app.services.work_item_pull_requests import handle_pull_request_event
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.work_item_owner import add_owner, owner_of

pytestmark = pytest.mark.integration

WRITER = person_writer(is_admin=False)
REPO = "xtm-one-platform/xtm-one"


@asynccontextmanager
async def _database() -> AsyncIterator:
    async with scratch_database("blkrefs", tables=ALL_TABLES, extensions=("vector",)) as db:
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


async def _blocked(user: User, title: str, **ref) -> dict:
    return await wi_service.create_work_item(
        user_id=str(user.id),
        title=title,
        description="the brief",
        owner_agent_id=owner_of(user.id),
        blocked_on_refs=[{"repo": REPO, **ref}],
        writer=WRITER,
    )


async def _get(user: User, item: dict) -> dict:
    return await wi_service.get_work_item(item["id"], user_id=str(user.id))


async def test_a_merged_pull_request_releases_its_items_and_only_them():
    async with _database() as sessions:
        user = await _member(sessions)
        merged = await _blocked(user, "Retest after the merge", number=200, release_on="merged")
        either = await _blocked(user, "Rebase once it lands", number=200)
        other = await _blocked(user, "Waits on another PR", number=201, release_on="merged")
        assert merged["status"] == "blocked"
        assert merged["blocked_why"] == {"text": f"Waits for: {REPO}#200 (merged)", "missing": False}

        out = await handle_pull_request_event(
            PullRequestEvent(
                kind="closed",
                repo=REPO,
                pr_numbers=(200,),
                merged=True,
                merge_sha="abc",
                closed_at="2026-11-04T00:59:04Z",
            )
        )
        assert out["released"] == {"released": 2, "noted": 0}

        for item in (merged, either):
            now = await _get(user, item)
            assert now["status"] == "open"
            assert now["claimed_by_agent_id"] is None
            assert now["blocked_on_refs"] == [] and now["blocked_why"] is None
            # The merge time GitHub states, then the release time.
            assert now["description"].startswith(f"released: {REPO}#200 merged at 2026-11-04T00:59:04Z; released at ")
            assert now["description"].endswith("the brief")
        assert (await _get(user, other))["status"] == "blocked"

        # The same delivery again moves nothing.
        again = await handle_pull_request_event(
            PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(200,), merged=True)
        )
        assert again["released"] == {"released": 0, "noted": 0}


async def test_a_pull_request_closed_unmerged_surfaces_an_item_that_wanted_it_merged():
    async with _database() as sessions:
        user = await _member(sessions)
        wants_merge = await _blocked(user, "Retest after the merge", number=210, release_on="merged")
        replaced = await _blocked(user, "Own PR, closed and replaced", number=210, release_on="merged_or_closed")

        out = await handle_pull_request_event(
            PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(210,), merged=False)
        )
        assert out["released"] == {"released": 1, "noted": 0, "surfaced": 1}

        surfaced = await _get(user, wants_merge)
        # Never left blocked on a wait that cannot hold: back to the owner, saying why.
        assert surfaced["status"] == "open"
        assert surfaced["claimed_by_agent_id"] is None
        assert surfaced["blocked_on_refs"] == [] and surfaced["blocked_why"] is None
        assert surfaced["description"].startswith(f"surfaced: {REPO}#210 closed, release_on merged, so it can never")
        assert surfaced["description"].endswith("the brief")
        assert surfaced["claim_notes"][-1]["text"].startswith("surfaced: ")
        assert (await _get(user, replaced))["status"] == "open"

        # The same delivery again moves nothing: the item is no longer blocked.
        again = await handle_pull_request_event(
            PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(210,), merged=False)
        )
        assert again["released"] == {"released": 0, "noted": 0}


async def test_an_issue_closed_as_duplicate_releases_the_item():
    async with _database() as sessions:
        user = await _member(sessions)
        item = await _blocked(user, "Waits on the issue", kind="issue", number=220)

        out = await handle_pull_request_event(
            PullRequestEvent(kind="issue_closed", repo=REPO, pr_numbers=(220,))
        )
        assert out["released"] == {"released": 1, "noted": 0}
        now = await _get(user, item)
        assert now["status"] == "open"
        # No closing time in the event: the release time is never shown as the close time.
        assert now["description"].startswith(f"released: {REPO} issue #220 closed; released at ")


async def test_the_polling_fallback_releases_an_item_no_delivery_released(monkeypatch):
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import update

    from app.integrations import reference_state
    from app.models.work_item import WorkItem
    from app.services import work_item_blocked_refs as refs_mod

    async with _database() as sessions:
        user = await _member(sessions)
        quiet = await _blocked(user, "Merged before the note", number=230, release_on="merged")
        fresh = await _blocked(user, "Blocked a second ago", number=230, release_on="merged")
        async with sessions() as db:
            await db.execute(
                update(WorkItem)
                .where(WorkItem.id == uuid.UUID(quiet["id"]))
                .values(last_activity_at=datetime.now(UTC) - timedelta(hours=1))
            )
            await db.commit()

        reads: list = []

        async def reader(repo, kind, number, token):
            reads.append((repo, kind, number, token))
            return PullRequestEvent(
                kind="closed", repo=repo, pr_numbers=(number,), merged=True, closed_at="2026-11-04T00:59:04Z"
            )

        async def token(db, provider, user_id):
            return "tok" if str(user_id) == str(user.id) else None

        monkeypatch.setattr(reference_state, "_READERS", {"github": reader})
        monkeypatch.setattr(refs_mod, "reference_token", token)
        monkeypatch.setattr(refs_mod, "_last_polled", {})

        out = await refs_mod.poll_blocked_on_refs()
        assert out == {"read": 1, "released": 1, "noted": 0}
        assert reads == [(REPO, "pull_request", 230, "tok")]
        now = await _get(user, quiet)
        assert now["status"] == "open"
        assert now["description"].startswith(f"released: {REPO}#230 merged at 2026-11-04T00:59:04Z; released at ")
        assert (await _get(user, fresh))["status"] == "blocked"


async def test_a_blocked_status_needs_a_why_and_leaving_blocked_clears_it():
    async with _database() as sessions:
        user = await _member(sessions)
        with pytest.raises(ValueError, match="blocked_on_refs"):
            await wi_service.create_work_item(
                user_id=str(user.id), title="No why", status="blocked", writer=WRITER
            )
        item = await wi_service.create_work_item(
            user_id=str(user.id), title="Plain", owner_agent_id=owner_of(user.id), writer=WRITER
        )
        with pytest.raises(ValueError, match="blocked_reason is context, never the blocker"):
            await wi_service.update_work_item(item["id"], user_id=str(user.id), status="blocked", writer=WRITER)
        # A reason alone is no blocker (#762).
        with pytest.raises(ValueError, match="no blocker the platform releases"):
            await wi_service.update_work_item(
                item["id"], user_id=str(user.id), status="blocked", blocked_reason="the UX verdict", writer=WRITER
            )
        assert (await _get(user, item))["status"] == "open"

        blocked = await wi_service.update_work_item(
            item["id"],
            user_id=str(user.id),
            status="blocked",
            blocked_reason="the UX verdict, released by the UX Director",
            blocked_on_refs=[{"repo": REPO, "kind": "pull_request", "number": 7, "release_on": "merged"}],
            writer=WRITER,
        )
        assert blocked is not None
        why = blocked["blocked_why"]
        assert why["missing"] is False and why["text"].startswith("Waits for: ")
        assert why["text"].endswith("; the UX verdict, released by the UX Director")
        reopened = await wi_service.update_work_item(item["id"], user_id=str(user.id), status="open", writer=WRITER)
        assert reopened["blocked_reason"] is None and reopened["blocked_why"] is None
