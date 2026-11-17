"""One-off pass over past chat deliverables (#10544, slice S3) against real PostgreSQL.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.chat import Conversation, Message
from app.models.file_attachment import FileAttachment
from app.models.presentation import Presentation
from app.models.report import Report
from app.models.roi import RoiChatRecord
from app.models.user import User
from app.services import roi_chat, roi_chat_backfill
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_EDIT = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
_NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


@asynccontextmanager
async def _session(prefix: str) -> AsyncIterator[AsyncSession]:
    async with (
        scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        yield session


async def _seed(session: AsyncSession):
    user = User(id=uuid.uuid4(), email=f"cb-{uuid.uuid4().hex[:8]}@test.local", full_name="C", hashed_password="x")
    session.add(user)
    await session.flush()
    conv = Conversation(id=uuid.uuid4(), user_id=user.id, title="All Hands")
    session.add(conv)
    await session.flush()
    # 19 messages a minute apart, then one after a long pause: 18 + 10 (capped) = 28 measured minutes.
    for i in range(19):
        session.add(
            Message(
                id=uuid.uuid4(),
                conversation_id=conv.id,
                role="user",
                content="go",
                sequence=i,
                created_at=_EDIT + timedelta(minutes=i),
            )
        )
    session.add(
        Message(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            role="user",
            content="more",
            sequence=19,
            created_at=_EDIT + timedelta(hours=5),
        )
    )
    deck = Presentation(
        id=uuid.uuid4(),
        title="All Hands",
        spec={"slides": []},
        revision=1,
        user_id=user.id,
        conversation_id=conv.id,
        slide_count=30,
        created_at=_EDIT,
        updated_at=_EDIT,
    )
    run_deck = Presentation(
        id=uuid.uuid4(),
        title="Weekly",
        spec={"slides": []},
        revision=1,
        user_id=user.id,
        conversation_id=None,
        slide_count=5,
        created_at=_EDIT,
        updated_at=_EDIT,
    )
    report = Report(
        id=uuid.uuid4(),
        title="Q3",
        spec={},
        user_id=user.id,
        conversation_id=conv.id,
        block_count=12,
        created_at=_EDIT,
        updated_at=_EDIT,
    )
    files = [
        FileAttachment(
            id=uuid.uuid4(),
            user_id=user.id,
            conversation_id=conv.id,
            filename="brief.pdf",
            content_type="application/pdf",
            s3_object_name=f"t/{uuid.uuid4()}",
            source="agent_download",
            created_at=_EDIT + timedelta(minutes=m),
        )
        for m in (0, 5)
    ]
    upload = FileAttachment(
        id=uuid.uuid4(),
        user_id=user.id,
        conversation_id=conv.id,
        filename="mine.pdf",
        content_type="application/pdf",
        s3_object_name=f"t/{uuid.uuid4()}",
        source="chat",
        created_at=_EDIT,
    )
    session.add_all([deck, run_deck, report, *files, upload])
    await session.flush()
    return user, conv, deck, report, files


async def _rows(session: AsyncSession) -> list[RoiChatRecord]:
    return list((await session.execute(select(RoiChatRecord))).scalars().all())


async def test_a_dry_run_writes_nothing_and_the_pass_writes_each_past_deliverable_once():
    async with _session("roicb1") as session:
        _, conv, deck, report, files = await _seed(session)

        preview = await roi_chat_backfill.backfill_chat_records(session, dry_run=True, now=_NOW)
        totals = preview.as_dict()["totals"]
        # The deck, the report and one document (two revisions of brief.pdf); the run's deck and the upload are not.
        assert (totals["scanned"], totals["to_create"], totals["counted_now"]) == (3, 3, 3)
        assert await _rows(session) == []
        decks = preview.kinds[roi_chat.KIND_PRESENTATION]
        expected = roi_chat.estimate(roi_chat.KIND_PRESENTATION, 30, user_min=28.0)
        assert decks.counted_min == pytest.approx(expected.counted_min)
        assert decks.counted_min > 0

        done = await roi_chat_backfill.backfill_chat_records(session, dry_run=False, now=_NOW)
        assert done.records_created == 3
        rows = {r.kind: r for r in await _rows(session)}
        assert rows["presentation"].deliverable_id == str(deck.id)
        assert rows["presentation"].size == 30.0
        assert rows["presentation"].backfilled_at == _NOW
        assert rows["presentation"].created_at == _EDIT
        assert rows["presentation"].conversation_id == conv.id
        assert rows["report"].deliverable_id == str(report.id)
        assert rows["report"].size == 2.0
        assert rows["file"].deliverable_id == str(files[1].id)

        again = await roi_chat_backfill.backfill_chat_records(session, dry_run=False, now=_NOW)
        assert again.records_created == 0
        assert again.as_dict()["totals"]["already_recorded"] == 3
        assert len(await _rows(session)) == 3


async def test_a_record_the_hooks_wrote_is_never_touched():
    async with _session("roicb2") as session:
        user, conv, deck, _, _ = await _seed(session)
        from app.services.roi_chat_records import record_deliverable

        await record_deliverable(
            session,
            kind="presentation",
            deliverable_id=deck.id,
            conversation_id=conv.id,
            user_id=user.id,
            title="All Hands v2",
            size=31,
            at=_NOW,
        )
        result = await roi_chat_backfill.backfill_chat_records(session, dry_run=False, now=_NOW)
        assert result.kinds[roi_chat.KIND_PRESENTATION].already_recorded == 1
        row = (
            await session.execute(select(RoiChatRecord).where(RoiChatRecord.record_key == f"presentation:{deck.id}"))
        ).scalar_one()
        assert (row.title, row.size, row.backfilled_at) == ("All Hands v2", 31.0, None)


async def test_the_series_reads_the_backfilled_deck_low_and_a_correction_wins():
    """S4: the All Hands deck comes out counted in the chat series, separate from automation."""
    from app.models.roi import RoiRunRecord
    from app.services import roi_chat_series

    async with _session("roicb3") as session:
        user, _, deck, _, _ = await _seed(session)
        await roi_chat_backfill.backfill_chat_records(session, dry_run=False, now=_NOW)
        mine = roi_chat_series.Reader(user_id=user.id)
        series = await roi_chat_series.load_series(session, reader=mine, window="all", now=_NOW)
        assert series["label"] == roi_chat.SERIES_LABEL
        assert series["totals"]["deliverables"] == 3
        [line] = [x for x in series["lines"] if x["kind"] == "presentation"]
        expected = roi_chat.estimate(roi_chat.KIND_PRESENTATION, 30, user_min=28.0)
        assert line["deliverable_id"] == str(deck.id)
        assert line["user_name"] == user.full_name  # S5: the line names the person it is attributed to
        assert line["minutes"] == pytest.approx(round(expected.counted_min, 1))
        assert 0 < line["minutes"] <= roi_chat.CAP_MIN["presentation"]
        # Nobody else reads it.
        stranger = roi_chat_series.Reader(user_id=uuid.uuid4())
        assert (await roi_chat_series.load_series(session, reader=stranger, window="all", now=_NOW))["lines"] == []
        # A correction replaces the estimate; automation records stay untouched.
        row = await roi_chat_series.get_line(session, uuid.UUID(line["id"]))
        roi_chat_series.apply_correction(row, minutes=600, user_id=user.id, now=_NOW)
        await session.flush()
        again = await roi_chat_series.load_series(session, reader=mine, window="all", now=_NOW)
        [corrected] = [x for x in again["lines"] if x["kind"] == "presentation"]
        assert corrected["minutes"] == 600.0
        assert again["totals"]["corrected"] == 1
        assert (await session.execute(select(RoiRunRecord))).scalars().all() == []
        # S5: a fleet's member agents narrow the series; a fleet without this agent reads nothing.
        other = await roi_chat_series.load_series(
            session, reader=mine, window="all", agent_ids=frozenset({uuid.uuid4()}), now=_NOW
        )
        assert other["lines"] == [] and other["totals"]["deliverables"] == 0
        if corrected["agent_id"]:
            members = frozenset({uuid.UUID(corrected["agent_id"])})
            fleet = await roi_chat_series.load_series(session, reader=mine, window="all", agent_ids=members, now=_NOW)
            assert corrected["id"] in {x["id"] for x in fleet["lines"]}
