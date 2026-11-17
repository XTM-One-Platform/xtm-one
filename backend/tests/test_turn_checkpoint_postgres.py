"""The turn checkpoint on real PostgreSQL: upsert, read back, delete.

Skips rather than fails when PostgreSQL is unreachable, matching the other
``integration``-marked tests here.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.llm.base import ChatMessage
from app.models.chat import ChatTurnCheckpoint, Conversation
from app.models.user import User
from app.services import turn_checkpoint
from app.services.turn_checkpoint import ResumedWork, TurnCheckpoint, load_resumable
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _round(call_id: str, result: str) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name="read_file", arguments={"path": f"/{call_id}"})],
        ),
        ChatMessage(role="tool", content=result, tool_call_id=call_id, name="read_file"),
    ]


async def test_a_checkpoint_survives_its_turn_and_goes_with_the_next_answer():
    async with scratch_database(
        "turn_checkpoint",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
    ) as scratch:
        async with scratch.sessions() as db:
            user = User(email="checkpoint@example.com", full_name="Checkpoint")
            db.add(user)
            await db.flush()
            conv = Conversation(user_id=user.id, channel="web")
            db.add(conv)
            await db.commit()
            conv_id = conv.id

        # The turn that dies: its writer uses the app's own sessions.
        first = TurnCheckpoint(conv_id, turn_id="turn-1")
        first.offer(_round("a", "contents of a"))
        await first.aclose(flush=True)

        async with scratch.sessions() as db:
            resumed = await load_resumable(db, conv_id)
        assert [(m.role, m.tool_call_id) for m in resumed.messages] == [
            ("assistant", None),
            ("tool", "a"),
        ]
        assert resumed.messages[0].tool_calls[0].arguments == {"path": "/a"}
        assert resumed.messages[1].content == "contents of a"

        # The next turn resumes it and dies too: one row, both rounds.
        second = TurnCheckpoint(conv_id, turn_id="turn-2", resumed=resumed)
        second.offer(_round("b", "contents of b"))
        await second.aclose(flush=True)
        async with scratch.sessions() as db:
            (row,) = (await db.execute(select(ChatTurnCheckpoint))).scalars().all()
        assert row.conversation_id == conv_id
        assert row.turn_id == "turn-2"
        assert [e.get("tool_call_id") for e in row.entries] == [None, "a", None, "b"]
        # What the conversation shows next to its interrupted turn.
        async with scratch.sessions() as db:
            assert await turn_checkpoint.kept_tool_calls(db, conv_id) == 2

        # The turn that stores its answer deletes it in that transaction.
        async with scratch.sessions() as db:
            await turn_checkpoint.discard(db, conv_id)
            await db.commit()
        async with scratch.sessions() as db:
            assert await load_resumable(db, conv_id) == ResumedWork()
            assert await turn_checkpoint.kept_tool_calls(db, conv_id) == 0

        # A deleted conversation takes its checkpoint with it.
        third = TurnCheckpoint(conv_id, turn_id="turn-3")
        third.offer(_round("c", "contents of c"))
        await third.aclose(flush=True)
        async with scratch.sessions() as db:
            await db.execute(delete(Conversation).where(Conversation.id == conv_id))
            await db.commit()
        async with scratch.sessions() as db:
            assert (await db.execute(select(ChatTurnCheckpoint))).scalars().all() == []
