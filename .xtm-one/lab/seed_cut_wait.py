"""Leave a ``wait`` round cut by a worker stop in a lab conversation, and queue its turn's automatic resume.

Run by ``lab.py`` (``journal_cut_wait``) for a ``lab:cut-wait [seconds]`` chat message, once that
turn stored its answer, with the backend's interpreter and environment:

    python seed_cut_wait.py <conversation id> <seconds>

The conversation's turn checkpoint (``chat_turn_checkpoints``) is written as a turn handed back during a
pause leaves it: one assistant message calling ``wait``, then the wait's result marked ``wait_cut``
(started now, granted *seconds*, nothing watched). The cut turn is queued like a stopping worker queues it
(``cut_turn_resume.enqueue_cut_turn``), on the person's last message, with the cause ``recycle``: a
``stop`` cut on the very build that runs the lab would wait ``SAME_BUILD_DEFER_SECONDS`` for another
build. The next sweep (``SWEEP_INTERVAL_SECONDS``) resumes the turn, whose agent loop finishes the
pause through ``_continue_cut_wait``: ``turn_resuming``, ``wait_start``, then ``wait_continued``.
Prints the queued state. Lab only, never a deployment.
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid

#: The id of the cut ``wait`` call, and its reason on the card.
CALL_ID = "call_lab_cut_wait"
REASON = "Lab cut wait"


def entries(seconds: int, now: float) -> list[dict]:
    """The checkpoint entries of a turn cut during a *seconds* pause that started at *now* (epoch)."""
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": CALL_ID, "name": "wait", "arguments": {"duration": str(seconds), "reason": REASON}}],
        },
        {
            "role": "tool",
            "content": "Wait cut short: the worker is stopping; the resumed turn finishes it.",
            "tool_call_id": CALL_ID,
            "name": "wait",
            "wait_cut": {"started_at": now, "granted": float(seconds), "requested": seconds, "until": [], "elapsed": 0.0},
        },
    ]


async def seed(conversation_id: str, seconds: int) -> bool:
    """Write the checkpoint and queue the resume; whether the resume was queued."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from sqlalchemy import select

    from app.core.database import async_session, engine
    from app.models.chat import ChatTurnCheckpoint, Conversation, Message
    from app.services.cut_turn_resume import enqueue_cut_turn

    cid = uuid.UUID(conversation_id)
    async with async_session() as db:
        conversation = (await db.execute(select(Conversation).where(Conversation.id == cid))).scalar_one()
        message = (
            await db.execute(
                select(Message)
                .where(Message.conversation_id == cid, Message.role == "user")
                .order_by(Message.sequence.desc())
                .limit(1)
            )
        ).scalar_one()
        await db.merge(ChatTurnCheckpoint(conversation_id=cid, turn_id=None, entries=entries(seconds, time.time())))
        await db.commit()
        user_id, message_id = conversation.user_id, message.id
    queued = await enqueue_cut_turn(cid, user_id=user_id, user_message_id=message_id, turn_id=None, cause="recycle")
    await engine.dispose()
    return queued


def main() -> None:
    conversation_id, seconds = sys.argv[1], int(sys.argv[2])
    queued = asyncio.run(seed(conversation_id, seconds))
    print("queued for its automatic resume" if queued else "not queued (see the backend log)")
    sys.exit(0 if queued else 1)


if __name__ == "__main__":
    main()
