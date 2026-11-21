"""A waiter on a UX review item reopens only once the verdict is on the pull request.

The UX review item (``ux:<repo>-<N>``) completes when the verdict is decided and
creates the item that posts it (``dev:ux-verdict-<N>-comment``). The source item
blocked on the review used to reopen at that same moment and run without the
verdict. The pure selection here; the release path against PostgreSQL below.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from app.services import work_item as wi_service

USER, OTHER = str(uuid.uuid4()), str(uuid.uuid4())
REVIEW, SOURCE, COMMENT = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())


def test_a_review_item_is_read_from_its_ux_key_only():
    assert wi_service._ux_review_of("ux:opencrq-3179", "UX review: opencrq #3179 on d7f2a939") == {
        "repo": None,
        "name": "opencrq",
        "number": 3179,
    }
    assert wi_service._ux_review_of("ux:gate:xtm-one-12", "")["number"] == 12
    assert wi_service._ux_review_of("dev:ux-verdict-12-comment", "") is None
    assert wi_service._ux_review_of("dev:deps", "") is None
    assert wi_service._ux_review_of(None, "") is None


def test_the_waiter_follows_the_unfinished_verdict_comment_of_the_same_pull_request():
    reviews = {REVIEW: (USER, {"repo": None, "name": "opencrq", "number": 3179})}
    pending = [(COMMENT, "dev:ux-verdict-3179-comment", "post the verdict on OpenCRQ-Platform/opencrq#3179", USER)]
    assert wi_service._pending_verdict_comments(SOURCE, [REVIEW], reviews, pending) == [COMMENT]


def test_no_follower_for_another_owner_repository_or_a_plain_blocker():
    reviews = {REVIEW: (USER, {"repo": None, "name": "opencrq", "number": 3179})}
    other_owner = [(COMMENT, "dev:ux-verdict-3179-comment", "", OTHER)]
    other_repo = [(COMMENT, "dev:ux-verdict-3179-comment", "post it on OpenCTI-Platform/opencti#3179", USER)]
    assert wi_service._pending_verdict_comments(SOURCE, [REVIEW], reviews, other_owner) == []
    assert wi_service._pending_verdict_comments(SOURCE, [REVIEW], reviews, other_repo) == []
    assert wi_service._pending_verdict_comments(SOURCE, [str(uuid.uuid4())], reviews, other_owner) == []
    # The comment item itself, blocked on the review, never waits on itself.
    mine = [(COMMENT, "dev:ux-verdict-3179-comment", "", USER)]
    assert wi_service._pending_verdict_comments(COMMENT, [REVIEW], reviews, mine) == []


@asynccontextmanager
async def _database() -> AsyncIterator:
    from tests.scratch_database import ALL_TABLES, scratch_database

    async with scratch_database("wiuxverdict", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


@pytest.mark.integration
async def test_the_source_reopens_after_the_verdict_comment_not_at_the_review_completion():
    import app.models  # noqa: F401 - every table, for ALL_TABLES
    from app.models.user import User
    from app.services.company_managed_writes import person_writer
    from tests.work_item_owner import add_owner

    writer = person_writer(is_admin=False)
    async with _database() as sessions:
        user = User(id=uuid.uuid4(), email=f"m-{uuid.uuid4().hex[:8]}@test.local", full_name="M", hashed_password="x")
        async with sessions() as db:
            db.add(user)
            await db.commit()
        uid = str(user.id)
        owner = await add_owner(sessions, user.id)

        async def create(title: str, **kwargs) -> dict:
            kwargs.setdefault("owner_agent_id", owner)
            return await wi_service.create_work_item(user_id=uid, title=title, writer=writer, **kwargs)

        review = await create("UX review: opencrq #3179 on d7f2a939", correlation_key="ux:opencrq-3179")
        source = await create("Mark the PR ready", correlation_key="dev:assets-table", blocked_on=[review["id"]])
        # The review run creates the comment item, then completes the review.
        comment = await create(
            "post the verdict on OpenCRQ-Platform/opencrq#3179", correlation_key="dev:ux-verdict-3179-comment"
        )
        await wi_service.complete_work_item(review["id"], user_id=uid, writer=writer)

        waiting = await wi_service.get_work_item(source["id"], user_id=uid)
        assert waiting["status"] == "blocked"
        assert waiting["blocked_on"] == [comment["id"]]
        assert waiting["claim_notes"][-1]["text"] == (
            f"{review['id']} done; waits for the verdict comment {comment['id']}"
        )

        await wi_service.complete_work_item(comment["id"], user_id=uid, writer=writer)
        reopened = await wi_service.get_work_item(source["id"], user_id=uid)
        assert reopened["status"] == "open"
        assert reopened["claim_notes"][-1]["text"] == f"unblocked: {comment['id']} done"
        assert await wi_service.release_satisfied_dependencies() == 0


@pytest.mark.integration
async def test_a_review_without_a_pending_verdict_comment_releases_at_once():
    import app.models  # noqa: F401 - every table, for ALL_TABLES
    from app.models.user import User
    from app.services.company_managed_writes import person_writer
    from tests.work_item_owner import add_owner

    writer = person_writer(is_admin=False)
    async with _database() as sessions:
        user = User(id=uuid.uuid4(), email=f"m-{uuid.uuid4().hex[:8]}@test.local", full_name="M", hashed_password="x")
        async with sessions() as db:
            db.add(user)
            await db.commit()
        uid = str(user.id)
        review = await wi_service.create_work_item(
            user_id=uid, title="UX review: xtm-one #12", correlation_key="ux:xtm-one-12", writer=writer
        )
        source = await wi_service.create_work_item(
            user_id=uid,
            title="Merge it",
            owner_agent_id=await add_owner(sessions, user.id),
            blocked_on=[review["id"]],
            writer=writer,
        )
        await wi_service.complete_work_item(review["id"], user_id=uid, writer=writer)
        assert (await wi_service.get_work_item(source["id"], user_id=uid))["status"] == "open"
