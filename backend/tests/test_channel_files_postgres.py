"""A channel turn's files stay in its conversation, on PostgreSQL.

Same scratch-database recipe as ``test_conversation_history_postgres.py``. A
real database because what is under test IS the SQL: the shared bind
(``attachable_filter`` in the UPDATE), the history rehydration that reads the
``attachments`` block back, the conversation-scoped file load of the next turn
and the conversation scope a channel link is signed for.

In an older build a channel attachment was stored bound to no conversation and no
message, and a file the first turn of a new conversation produced was bound
to no conversation either: after that turn neither was in the agent's
context, the conversation showed no chip, and for an anonymous sender -
whose only scope is the conversation - no marker could ever become a link.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.channels import router as channel_router
from app.channels.base import ChannelType, InboundMessage, resolve_channel_file_markers
from app.llm.base import FileRef
from app.models.chat import Message
from app.models.file_attachment import FileAttachment
from app.services.agent_chat import load_conversation_files
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _file(name: str, source: str) -> FileAttachment:
    """A row as ``_resolve_channel_files`` / a tool of a new conversation writes it: no scope."""
    return FileAttachment(
        id=uuid.uuid4(),
        user_id=None,
        filename=name,
        content_type="application/pdf",
        size=3,
        s3_object_name=f"channel/slack/{name}",
        source=source,
    )


async def test_an_anonymous_channel_turn_keeps_its_files(monkeypatch):
    # No thread key (no Redis) and no title model (the title is best effort).
    monkeypatch.setattr(channel_router, "_get_thread_key", lambda _inbound: None)

    async def _no_llm(*_args, **_kwargs):
        raise ValueError("no provider configured")

    monkeypatch.setattr("app.llm.router.get_llm_for_user", _no_llm)

    async with scratch_database("chfiles", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            upload = _file("invoice.pdf", "channel:slack")
            produced = _file("summary.pdf", "agent_generated")
            session.add_all([upload, produced])
            await session.flush()
            upload_ref = FileRef.from_attachment(upload)
            produced_ref = FileRef.from_attachment(produced)
            await session.commit()

            conv = await channel_router._finalize_message(
                session,
                InboundMessage(channel=ChannelType.SLACK, text="see the invoice", channel_user_id="U1"),
                user=None,
                conv=None,
                target_agent_id=None,
                is_anonymous=True,
                is_external_channel=True,
                suppress=False,
                assistant_text="The summary is ready.",
                user_files=[upload_ref],
                turn_files=[produced_ref],
            )
            conv_id = conv.id

        async with db.sessions() as session:
            messages = {
                m.role: m
                for m in (
                    await session.execute(select(Message).where(Message.conversation_id == conv_id))
                ).scalars()
            }
            rows = {
                row.filename: row
                for row in (await session.execute(select(FileAttachment))).scalars()
            }
            # Bound to the conversation and to the message that shows it.
            assert rows["invoice.pdf"].conversation_id == conv_id
            assert rows["invoice.pdf"].message_id == messages["user"].id
            assert rows["summary.pdf"].conversation_id == conv_id
            assert rows["summary.pdf"].message_id == messages["assistant"].id

            # The next turn: the user message carries its file again (inlined
            # by the providers), and the turn's FileContext holds both.
            history = await channel_router.get_conversation_history(session, conv_id)
            user_turn = next(m for m in history if m.role == "user")
            assert [ref.file_id for ref in user_turn.files] == [str(upload.id)]
            context = await load_conversation_files(session, str(conv_id))
            assert {ref.file_id for ref in context.list_files()} == {str(upload.id), str(produced.id)}

            # An anonymous conversation's only scope signs both links.
            linked = await resolve_channel_file_markers(
                session,
                f"Invoice: [[FILE:{upload.id}]] Summary: [[FILE:{produced.id}]]",
                "https://xtm.example",
                conversation_id=conv_id,
            )
            assert linked.count("/public?token=") == 2
            assert "[[FILE:" not in linked


async def test_a_file_another_message_shows_is_left_on_it(monkeypatch):
    monkeypatch.setattr(channel_router, "_get_thread_key", lambda _inbound: None)

    async def _no_llm(*_args, **_kwargs):
        raise ValueError("no provider configured")

    monkeypatch.setattr("app.llm.router.get_llm_for_user", _no_llm)

    async with scratch_database("chfiles", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            earlier = _file("earlier.pdf", "agent_generated")
            session.add(earlier)
            await session.flush()
            earlier_ref = FileRef.from_attachment(earlier)
            await session.commit()

            first = await channel_router._finalize_message(
                session,
                InboundMessage(channel=ChannelType.SLACK, text="first", channel_user_id="U1"),
                user=None,
                conv=None,
                target_agent_id=None,
                is_anonymous=True,
                is_external_channel=True,
                suppress=False,
                assistant_text="first answer",
                turn_files=[earlier_ref],
            )
            first_message_id = (
                await session.execute(select(FileAttachment.message_id).where(FileAttachment.id == earlier.id))
            ).scalar_one()

            # A later turn of ANOTHER conversation names the same file: it stays
            # on the message that shows it.
            await channel_router._finalize_message(
                session,
                InboundMessage(channel=ChannelType.SLACK, text="second", channel_user_id="U2"),
                user=None,
                conv=None,
                target_agent_id=None,
                is_anonymous=True,
                is_external_channel=True,
                suppress=False,
                assistant_text="second answer",
                turn_files=[earlier_ref],
            )

        async with db.sessions() as session:
            row = (await session.execute(select(FileAttachment).where(FileAttachment.id == earlier.id))).scalar_one()
            assert row.conversation_id == first.id
            assert row.message_id == first_message_id
