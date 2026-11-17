"""Visitor uploads on the public chat widget, on PostgreSQL.

Same scratch-database recipe as ``test_public_chat_mcp_files_postgres.py``. A
real database because what is under test IS the SQL: the storage quota read
again under the widget's lock, the session scope of a pending upload, the bind
to the visitor's message and its rehydration on the next turn, the download
rule of the session and the retention sweep.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import public_chat
from app.channels.router import get_conversation_history, store_message
from app.llm.file_resolver import upload_header
from app.models.agent import Agent
from app.models.chat import Conversation, Message
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.services import widget_uploads
from app.services.file_access import authorize_file_access
from app.services.widget_uploads import UploadRefusedError
from app.tools.base import FileContext
from tests.test_conversation_history_postgres import _temp_database

pytestmark = pytest.mark.integration

_PDF = b"%PDF-1.7\n" + b"x" * 91
_NOTES = b"Q3 revenue grew 12% on the year."


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (15, 188, 255)).save(buffer, "PNG")
    return buffer.getvalue()


_PNG = _png()


async def _seed(session) -> SimpleNamespace:
    admin = User(id=uuid.uuid4(), email=f"admin-{uuid.uuid4().hex[:8]}@test.local", full_name="Admin", hashed_password="x", role="admin")
    owner = User(id=uuid.uuid4(), email=f"owner-{uuid.uuid4().hex[:8]}@test.local", full_name="Owner", hashed_password="x")
    session.add_all([admin, owner])
    await session.flush()

    def _agent(name: str) -> Agent:
        return Agent(
            id=uuid.uuid4(), name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}", user_id=owner.id,
            web_enabled=True, web_uploads_enabled=True, enabled=True, web_token=f"tok-{uuid.uuid4().hex}",
        )

    agent_a, agent_b = _agent("WidgetA"), _agent("WidgetB")
    session.add_all([agent_a, agent_b])
    await session.flush()

    def _conv(agent: Agent) -> Conversation:
        return Conversation(id=uuid.uuid4(), user_id=None, channel="web_embed", agent_id=agent.id, title="Web chat")

    visitor, other_visitor, other_agent_visitor = _conv(agent_a), _conv(agent_a), _conv(agent_b)
    session.add_all([visitor, other_visitor, other_agent_visitor])
    await session.commit()
    return SimpleNamespace(
        admin=admin, owner=owner, agent_a=agent_a, agent_b=agent_b,
        visitor=visitor, other_visitor=other_visitor, other_agent_visitor=other_agent_visitor,
    )


@pytest.fixture
async def world(monkeypatch):
    async with _temp_database() as (sessionmaker, _engine):
        async with sessionmaker() as session:
            seeded = await _seed(session)

        objects: dict[str, bytes] = {}
        deleted: list[str] = []

        async def _upload(*, data, filename, content_type, prefix):
            key = f"{prefix}/{uuid.uuid4().hex}-{filename}"
            objects[key] = data
            return key

        async def _download(name):
            return objects[name]

        async def _delete(name):
            deleted.append(name)
            objects.pop(name, None)

        async def _no_rate_limit(*_args, **_kwargs):
            return None

        async def _read(data, filename, content_type, limit):
            # The reader's rule in this process: its worker is tested in test_widget_document_reader.py.
            from app.services.attachment_extract import inline_attachment_text

            inline = inline_attachment_text(data, filename, content_type, False, limit)
            return inline.text, inline.total, inline.stripped_total

        async def _inspect(filename, data):
            from app.services.widget_upload_inspection import inspection_problem

            return inspection_problem(filename, data)

        monkeypatch.setattr("app.services.file_storage.file_storage.upload", _upload)
        monkeypatch.setattr("app.services.file_storage.file_storage.download", _download)
        monkeypatch.setattr("app.services.file_storage.file_storage.delete", _delete)
        monkeypatch.setattr("app.services.prompt_guards.known_unconnected", lambda: True)
        monkeypatch.setattr(public_chat, "enforce_rate_limit", _no_rate_limit)
        monkeypatch.setattr("app.services.widget_document_reader.read_document", _read)
        monkeypatch.setattr("app.services.widget_document_reader.inspect_file", _inspect)
        seeded.sessionmaker = sessionmaker
        seeded.objects = objects
        seeded.deleted = deleted
        yield seeded


async def _accept(world, conversation: Conversation, *, name: str = "report.pdf", data: bytes = _PDF, agent=None):
    return await widget_uploads.accept_upload(
        agent=agent or world.agent_a,
        conversation_id=conversation.id,
        raw_filename=name,
        data=data,
        audit_anchor_user_id=None,
    )


async def test_an_upload_belongs_to_its_session_and_to_nobody(world):
    before = datetime.now(UTC)
    row = await _accept(world, world.visitor)

    async with world.sessionmaker() as session:
        stored = await session.get(FileAttachment, row.id)
        conv = await session.get(Conversation, world.visitor.id)
        assert (stored.user_id, stored.conversation_id, stored.message_id) == (None, world.visitor.id, None)
        assert (stored.source, stored.content_type, stored.size) == ("widget_upload", "application/pdf", len(_PDF))
        assert stored.sha256 == hashlib.sha256(_PDF).hexdigest()
        assert world.objects[stored.s3_object_name] == _PDF
        # An upload is activity of the session: its retention counts from here.
        assert conv.updated_at >= before

        # On the platform, the agent's own owner cannot read it; an admin can.
        owner = await session.get(User, world.owner.id)
        admin = await session.get(User, world.admin.id)
        with pytest.raises(HTTPException):
            await authorize_file_access(stored, owner, session)
        await authorize_file_access(stored, admin, session)

    # The session's own route serves it, as a download.
    response = await public_chat.download_session_file(
        world.agent_a.web_token, str(world.visitor.id), str(row.id), request=SimpleNamespace()
    )
    assert response.body == _PDF and response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["cache-control"] == "private, no-store"
    with pytest.raises(HTTPException) as other:
        await public_chat.download_session_file(
            world.agent_a.web_token, str(world.other_visitor.id), str(row.id), request=SimpleNamespace()
        )
    assert other.value.status_code == 404


async def test_a_session_holds_at_most_its_files(world, monkeypatch):
    monkeypatch.setattr(widget_uploads, "SESSION_MAX_FILES", 2)
    await _accept(world, world.visitor)
    await _accept(world, world.visitor, name="scan.png", data=_PNG)

    with pytest.raises(UploadRefusedError) as refused:
        await _accept(world, world.visitor)
    assert refused.value.status_code == 413
    # Another session of the same widget has its own room.
    await _accept(world, world.other_visitor)


async def test_parallel_uploads_cannot_overshoot_the_quota(world, monkeypatch):
    monkeypatch.setattr(widget_uploads, "SESSION_MAX_FILES", 1)
    # Both uploads pass the unlocked read of the quota before either writes:
    # only the read again under the widget's lock stands between them.
    past_the_first_read = []
    both_past = asyncio.Event()

    async def _wait_for_the_other(**_kwargs):
        past_the_first_read.append(1)
        if len(past_the_first_read) == 2:
            both_past.set()
        await asyncio.wait_for(both_past.wait(), timeout=10)

    monkeypatch.setattr(widget_uploads, "_screen", _wait_for_the_other)

    outcomes = await asyncio.gather(
        _accept(world, world.visitor, name="one.pdf"),
        _accept(world, world.visitor, name="two.pdf"),
        return_exceptions=True,
    )

    stored = [o for o in outcomes if isinstance(o, FileAttachment)]
    refused = [o for o in outcomes if isinstance(o, UploadRefusedError)]
    assert (len(stored), len(refused)) == (1, 1) and refused[0].status_code == 413
    async with world.sessionmaker() as session:
        rows = (await session.execute(select(FileAttachment).where(FileAttachment.source == "widget_upload"))).scalars().all()
    assert [r.id for r in rows] == [stored[0].id]
    # The object of the refused upload did not stay behind.
    assert list(world.objects) == [rows[0].s3_object_name] and len(world.deleted) == 1


async def test_a_widget_holds_at_most_its_bytes_across_its_sessions(world, monkeypatch):
    monkeypatch.setattr(widget_uploads, "WIDGET_MAX_BYTES", len(_PDF) + len(_PDF) // 2)
    await _accept(world, world.visitor)

    with pytest.raises(UploadRefusedError) as refused:
        await _accept(world, world.other_visitor)
    assert refused.value.status_code == 507
    # Another widget is counted on its own.
    await _accept(world, world.other_agent_visitor, agent=world.agent_b)


async def test_a_message_attaches_only_the_pending_uploads_of_its_session(world):
    mine = await _accept(world, world.visitor)
    theirs = await _accept(world, world.other_visitor)

    agent_id = world.agent_a.id
    async with world.sessionmaker() as session:
        pending = await widget_uploads.load_pending_uploads(
            session, agent_id=agent_id, conversation_id=world.visitor.id, file_ids=[str(mine.id), str(mine.id)]
        )
        assert [row.id for row in pending] == [mine.id]
        for file_ids in ([str(theirs.id)], [str(uuid.uuid4())], ["not-a-uuid"], [str(mine.id), str(theirs.id)]):
            with pytest.raises(UploadRefusedError) as refused:
                await widget_uploads.load_pending_uploads(
                    session, agent_id=agent_id, conversation_id=world.visitor.id, file_ids=file_ids
                )
            assert (refused.value.status_code, refused.value.detail) == (400, widget_uploads.FILE_GONE_DETAIL)
        # Another widget's token never reaches this widget's session.
        with pytest.raises(UploadRefusedError):
            await widget_uploads.load_pending_uploads(
                session, agent_id=world.agent_b.id, conversation_id=world.visitor.id, file_ids=[str(mine.id)]
            )


async def test_the_files_ride_on_the_visitor_message_and_on_the_next_turns(world):
    notes = await _accept(world, world.visitor, name="notes.txt", data=_NOTES)
    picture = await _accept(world, world.visitor, name="chart.png", data=_PNG)
    # Uploaded and never sent: the visitor took it back.
    await _accept(world, world.visitor, name="draft.txt", data=b"not for the agent")
    ids = [str(notes.id), str(picture.id)]

    async with world.sessionmaker() as session:
        pending = await widget_uploads.load_pending_uploads(
            session, agent_id=world.agent_a.id, conversation_id=world.visitor.id, file_ids=ids
        )
        texts = await widget_uploads.document_texts(pending)
        message = await store_message(session, world.visitor.id, role="user", content="What does it say?", channel="web_embed")
        refs, shown = await public_chat._attach_visitor_uploads(session, message, world.visitor.id, pending, texts)
        await session.commit()

    notes_text = f"{upload_header(str(notes.id), 'notes.txt')}\n\n{_NOTES.decode()}"
    assert [ref.file_id for ref in refs] == ids and shown == [notes_text]
    async with world.sessionmaker() as session:
        stored = await session.get(Message, message.id)
        for row_id in (notes.id, picture.id):
            assert (await session.get(FileAttachment, row_id)).message_id == message.id
        # Sent once: a second message cannot attach them again.
        with pytest.raises(UploadRefusedError):
            await widget_uploads.load_pending_uploads(
                session, agent_id=world.agent_a.id, conversation_id=world.visitor.id, file_ids=ids[:1]
            )

        # The visitor's transcript shows what they typed and both chips.
        public = public_chat._public_message(stored)
        assert public["content"] == "What does it say?"
        assert [f["file_id"] for f in public["files"]] == ids
        history = await get_conversation_history(session, world.visitor.id)
    # The next turns read the document as the text its guard screened, and see the image again.
    (user_turn,) = history
    assert user_turn.content == f"What does it say?\n\n{notes_text}"
    assert [ref.file_id for ref in user_turn.files] == [str(picture.id)]

    # The files the visitor sent are the next turns' to hand to a tool; the one never sent is not.
    ctx = FileContext()
    known = await public_chat._load_session_files(world.visitor.id, ctx)
    assert known == set(ids) and all(ctx.get(file_id) is not None for file_id in ids)
    assert await public_chat._load_session_files(world.other_visitor.id, FileContext()) == set()


async def test_the_sweep_deletes_the_files_of_sessions_that_are_over(world, monkeypatch):
    monkeypatch.setattr("app.core.config.settings.public_widget_upload_retention_days", 7)
    now = datetime.now(UTC)

    unsent_old = await _accept(world, world.visitor, name="unsent-old.pdf")
    unsent_new = await _accept(world, world.visitor, name="unsent-new.pdf")
    sent_idle = await _accept(world, world.other_visitor, name="sent-idle.pdf")
    sent_active = await _accept(world, world.other_agent_visitor, name="sent-active.pdf", agent=world.agent_b)
    orphan = await _accept(world, world.visitor, name="orphan.pdf")

    async with world.sessionmaker() as session:
        # Sent with messages of their sessions.
        idle_message = await store_message(session, world.other_visitor.id, role="user", content="a", channel="web_embed")
        active_message = await store_message(session, world.other_agent_visitor.id, role="user", content="b", channel="web_embed")
        await session.execute(update(FileAttachment).where(FileAttachment.id == sent_idle.id).values(message_id=idle_message.id))
        await session.execute(update(FileAttachment).where(FileAttachment.id == sent_active.id).values(message_id=active_message.id))
        await session.execute(
            update(FileAttachment).where(FileAttachment.id == unsent_old.id).values(created_at=now - timedelta(days=2))
        )
        # A session with no message for eight days, and a file of the widget's own tools in it.
        await session.execute(
            update(Conversation).where(Conversation.id == world.other_visitor.id).values(updated_at=now - timedelta(days=8))
        )
        tool_file = FileAttachment(
            user_id=None, conversation_id=world.other_visitor.id, filename="chart.png", content_type="image/png",
            size=3, s3_object_name="widget/tool/chart.png", source="mcp",
        )
        session.add(tool_file)
        await session.commit()
        # A deleted conversation leaves its upload without one.
        await session.execute(update(FileAttachment).where(FileAttachment.id == orphan.id).values(conversation_id=None))
        await session.commit()

    deleted = await widget_uploads.purge_expired_uploads(now=now)

    async with world.sessionmaker() as session:
        left = set((await session.execute(select(FileAttachment.id))).scalars())
    assert deleted == 3
    assert left == {unsent_new.id, sent_active.id, tool_file.id}
    assert sorted(world.deleted) == sorted([unsent_old.s3_object_name, sent_idle.s3_object_name, orphan.s3_object_name])


async def test_a_store_that_stops_answering_leaves_the_object_and_its_row_for_the_next_sweep(world, monkeypatch):
    now = datetime.now(UTC)
    first = await _accept(world, world.visitor, name="first.pdf")
    second = await _accept(world, world.visitor, name="second.pdf")
    async with world.sessionmaker() as session:
        await session.execute(
            update(FileAttachment)
            .where(FileAttachment.id.in_([first.id, second.id]))
            .values(created_at=now - timedelta(days=2))
        )
        await session.commit()
    real_delete = world.objects.pop

    async def _unreachable(name):
        if name == second.s3_object_name:
            raise ConnectionError("store unreachable")
        world.deleted.append(name)
        real_delete(name, None)

    monkeypatch.setattr("app.services.file_storage.file_storage.delete", _unreachable)
    assert await widget_uploads.purge_expired_uploads(now=now) == 1

    async with world.sessionmaker() as session:
        # The row whose object may still exist stays: the next sweep deletes both.
        assert set((await session.execute(select(FileAttachment.id))).scalars()) == {second.id}
    assert world.deleted == [first.s3_object_name] and second.s3_object_name in world.objects


async def test_an_object_whose_row_was_refused_and_whose_delete_failed_is_kept_for_the_sweep(world, monkeypatch):
    monkeypatch.setattr(widget_uploads, "SESSION_MAX_FILES", 0)

    async def _unreachable(_name):
        raise ConnectionError("store unreachable")

    monkeypatch.setattr("app.services.file_storage.file_storage.delete", _unreachable)
    with pytest.raises(UploadRefusedError):
        await widget_uploads._store(
            file_id=uuid.uuid4(), agent_id=world.agent_a.id, conversation_id=world.visitor.id,
            filename="report.pdf", content_type="application/pdf", data=_PDF,
        )
    (object_name,) = list(world.objects)
    async with world.sessionmaker() as session:
        (kept,) = (await session.execute(select(FileAttachment))).scalars().all()
    # A row of no conversation, which no quota counts and nothing serves.
    assert (kept.s3_object_name, kept.conversation_id, kept.user_id, kept.source) == (
        object_name, None, None, "widget_upload"
    )

    deleted: list[str] = []

    async def _delete(name):
        deleted.append(name)

    monkeypatch.setattr("app.services.file_storage.file_storage.delete", _delete)
    assert await widget_uploads.purge_expired_uploads() == 1
    assert deleted == [object_name]


async def test_a_message_whose_file_another_message_took_first_is_refused(world):
    notes = await _accept(world, world.visitor, name="notes.txt", data=_NOTES)
    picture = await _accept(world, world.visitor, name="chart.png", data=_PNG)
    ids = [str(notes.id), str(picture.id)]

    async with world.sessionmaker() as session:
        pending = await widget_uploads.load_pending_uploads(
            session, agent_id=world.agent_a.id, conversation_id=world.visitor.id, file_ids=ids
        )
        texts = await widget_uploads.document_texts(pending)
        # A second send of the same files commits first.
        async with world.sessionmaker() as racing:
            first = await store_message(racing, world.visitor.id, role="user", content="first", channel="web_embed")
            await racing.execute(update(FileAttachment).where(FileAttachment.id == notes.id).values(message_id=first.id))
            await racing.commit()
        message = await store_message(session, world.visitor.id, role="user", content="second", channel="web_embed")
        with pytest.raises(UploadRefusedError) as refused:
            await public_chat._attach_visitor_uploads(session, message, world.visitor.id, pending, texts)
        assert (refused.value.status_code, refused.value.detail) == (400, widget_uploads.FILE_GONE_DETAIL)
        await session.rollback()

    async with world.sessionmaker() as session:
        # The refused message took nothing: the picture is still the visitor's to send.
        assert (await session.get(FileAttachment, picture.id)).message_id is None
        assert (await session.get(FileAttachment, notes.id)).message_id == first.id


async def test_an_upload_its_visitor_left_mid_store_is_written_with_its_row(world, monkeypatch):
    started, released = asyncio.Event(), asyncio.Event()

    async def _slow_upload(*, data, filename, content_type, prefix):
        started.set()
        await released.wait()
        key = f"{prefix}/{uuid.uuid4().hex}-{filename}"
        world.objects[key] = data
        return key

    monkeypatch.setattr("app.services.file_storage.file_storage.upload", _slow_upload)
    upload = asyncio.create_task(_accept(world, world.visitor, name="chart.png", data=_PNG))
    await started.wait()
    # The visitor disconnects while the object is being written.
    upload.cancel()
    with pytest.raises(asyncio.CancelledError):
        await upload
    released.set()
    while widget_uploads._abandoned:
        await asyncio.sleep(0.01)

    (object_name,) = list(world.objects)
    async with world.sessionmaker() as session:
        (row,) = (await session.execute(select(FileAttachment))).scalars().all()
    # Not an object no row names: a pending upload of the session, which the retention deletes.
    assert (row.s3_object_name, row.conversation_id, row.message_id) == (object_name, world.visitor.id, None)


async def test_messages_stored_after_the_turn_s_own_never_take_a_place_in_its_history_window(world):
    async with world.sessionmaker() as session:
        for text in ("first", "second", "third"):
            await store_message(session, world.visitor.id, role="user", content=text, channel="web_embed")
        mine = await store_message(session, world.visitor.id, role="user", content="mine", channel="web_embed")
        await session.commit()
        # A burst of sends lands between this turn's commit and its history read.
        for index in range(60):
            await store_message(session, world.visitor.id, role="user", content=f"later {index}", channel="web_embed")
        await session.commit()
        history = await public_chat._turn_history(session, world.visitor.id, mine)
    assert [turn.content for turn in history] == ["first", "second", "third"]


@pytest.mark.parametrize("streamed", [False, True])
async def test_a_stale_compaction_watermark_is_cleared_without_waiting_on_the_turn_s_own_lock(world, monkeypatch, streamed):
    """The history clears a stale watermark in a session of its own that locks the conversation.

    Read while ``store_message``'s lock was still held, that session waited on
    the turn and the turn on it: the route never answered.
    """
    async with world.sessionmaker() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.id == world.visitor.id)
            .values(compaction_state_json=json.dumps(
                {"directives": "", "summary": "Earlier talk.", "through_sequence": 50, "compacted_count": 4}
            ))
        )
        await session.commit()

    async def _run(*_args, **_kwargs):
        return "Hello."

    async def _true(*_args, **_kwargs):
        return True

    class _Redis:
        async def set(self, *_a, **_k):
            # The session's turn is free: the send takes it.
            return True

        async def delete(self, *_a):
            return None

    async def _redis():
        return _Redis()

    monkeypatch.setattr(public_chat, "_run_public_agent_chat", _run)
    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _true)
    monkeypatch.setattr("app.core.redis.redis_client", _redis)
    body = public_chat.PublicMessageRequest(content="Hi again")
    if streamed:
        response = await public_chat.send_public_message_stream(
            world.agent_a.web_token, str(world.visitor.id), body, request=SimpleNamespace()
        )

        async def _drain():
            return [chunk async for chunk in response.body_iterator]

        await asyncio.wait_for(_drain(), timeout=30)
    else:
        await asyncio.wait_for(
            public_chat.send_public_message(world.agent_a.web_token, str(world.visitor.id), body, request=SimpleNamespace()),
            timeout=30,
        )
    async with world.sessionmaker() as session:
        assert (await session.get(Conversation, world.visitor.id)).compaction_state_json is None


async def test_a_visitor_message_through_the_route_attaches_its_file(world, monkeypatch):
    row = await _accept(world, world.visitor, name="notes.txt", data=_NOTES)
    seen = {}

    async def _run(_db, _agent, history, _content, *, file_context=None, turn_files=None, turn_texts=None, **_kwargs):
        seen["history"] = history
        seen["turn_files"] = turn_files
        seen["turn_texts"] = turn_texts
        seen["context_ids"] = {ref.file_id for ref in file_context.list_files()}
        return "It is a report."

    async def _true(*_args, **_kwargs):
        return True

    class _Redis:
        async def set(self, *_a, **_k):
            # The session's turn is free: the send takes it.
            return True

        async def delete(self, *_a):
            return None

    async def _redis():
        return _Redis()

    monkeypatch.setattr(public_chat, "_run_public_agent_chat", _run)
    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _true)
    monkeypatch.setattr("app.core.redis.redis_client", _redis)

    response = await public_chat.send_public_message(
        world.agent_a.web_token,
        str(world.visitor.id),
        public_chat.PublicMessageRequest(content="What is it?", file_ids=[str(row.id)]),
        request=SimpleNamespace(),
    )

    assert json.loads(response.body) == {"role": "assistant", "content": "It is a report."}
    assert [ref.file_id for ref in seen["turn_files"]] == [str(row.id)]
    assert seen["turn_texts"] == [f"{upload_header(str(row.id), 'notes.txt')}\n\n{_NOTES.decode()}"]
    # Committed with its message before the session's files are loaded, it is
    # one of them now (an upload never sent is not: see the test above).
    assert seen["context_ids"] == {str(row.id)}
    # The message just stored is not read back in the history: the turn appends it.
    assert seen["history"] == []
    async with world.sessionmaker() as session:
        assert (await session.get(FileAttachment, row.id)).message_id is not None
