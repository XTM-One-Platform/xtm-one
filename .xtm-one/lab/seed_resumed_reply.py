"""Store the rows of a turn the platform cut and resumed by itself, for ``lab:resuming ... cut``.

Run by ``lab.py`` (``store_cut_reply`` then ``mark_resumed_answer``) with the backend's interpreter
and environment, while the scripted turn holds its answer:

    python seed_resumed_reply.py interrupted <conversation id> <cause>
    python seed_resumed_reply.py resumed <conversation id> <after sequence> <cause> <resume> <of> <cut at>

``interrupted`` stores the assistant message a cut turn leaves (``_store_cut_turn`` in
``api/v1/chat.py``): ``interrupted_turn.interrupted_reply`` as its text, one completed tool call in
its trace, the ``interrupted`` keys and ``auto_resume`` (queued for its automatic resume). It
prints the stored sequence.

``resumed`` waits for the scripted turn's own answer (an assistant reply after that sequence) and
merges ``resumed_from`` into its metadata, as ``parent_wake.resume_cut_turn`` stores
``cut_turn_resume.resumed_from_meta`` on the reply of a real resume. Prints the patched id.

The transcript then holds real rows: the cut-turn card goes resuming, then "resumed by itself",
with no fetch override. Lab only, never a deployment.
dev-docs/reference/agentic-labs.md, "Live resuming turn".
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid

#: The completed tool call the interrupted reply names.
CUT_TOOL = "search_knowledge"
#: How long ``resumed`` waits for the turn's answer: the longest hold plus the turn's store.
ANSWER_WAIT_SECONDS = 180
ANSWER_POLL_SECONDS = 0.1


def progress():
    """The tool progress of the cut turn: one completed call."""
    from app.services.interrupted_turn import TurnProgress

    turn = TurnProgress()
    turn.observe({"status": "thinking", "iteration": 1})
    turn.observe({"status": "tool_start", "tools": [CUT_TOOL]})
    turn.observe({"status": "tool_done", "tool": CUT_TOOL, "tool_input": '{"query": "lab"}',
                  "tool_output": "Lab result", "success": True})
    return turn


def interrupted_meta(cause: str) -> dict:
    """The metadata of the interrupted reply, in the keys ``_store_cut_turn`` writes."""
    from app.services.cut_turn_resume import AUTO_RESUME_META_KEY
    from app.services.interrupted_turn import (
        INTERRUPTED_CAUSE_META_KEY,
        INTERRUPTED_META_KEY,
        INTERRUPTED_STATE_META_KEY,
    )

    return {
        **progress().meta(),
        INTERRUPTED_META_KEY: True,
        INTERRUPTED_CAUSE_META_KEY: cause,
        INTERRUPTED_STATE_META_KEY: "interrupted",
        AUTO_RESUME_META_KEY: True,
    }


def resumed_meta(cause: str, resume: int, of: int, cut_at: str) -> dict:
    """The ``resumed_from`` record of the answer, in the keys ``resumed_from_meta`` writes."""
    from app.services.cut_turn_resume import RESUMED_FROM_META_KEY

    return {
        RESUMED_FROM_META_KEY: {
            "turn_id": None, "cause": cause, "cut_at": cut_at, "resume": resume, "of": of, "resumed_as": None,
        }
    }


async def store_interrupted(conversation_id: str, cause: str) -> int:
    """Store the interrupted reply; its sequence."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.channels.router import store_message
    from app.core.database import async_session, engine
    from app.services.interrupted_turn import interrupted_reply

    async with async_session() as db:
        message = await store_message(
            db, uuid.UUID(conversation_id), role="assistant", content=interrupted_reply(progress(), cause),
            channel="web", metadata_json=json.dumps(interrupted_meta(cause)),
        )
        sequence = message.sequence
        await db.commit()
    await engine.dispose()
    return sequence


async def mark_resumed(conversation_id: str, after: int, meta: dict) -> str | None:
    """Merge *meta* into the first answer stored after *after*; its id, or ``None`` past the wait."""
    import app.models  # noqa: F401
    from sqlalchemy import select

    from app.core.database import async_session, engine
    from app.models.chat import Message

    cid = uuid.UUID(conversation_id)
    deadline = time.monotonic() + ANSWER_WAIT_SECONDS
    patched = None
    while patched is None and time.monotonic() < deadline:
        async with async_session() as db:
            answer = (
                await db.execute(
                    select(Message)
                    .where(Message.conversation_id == cid, Message.role == "assistant", Message.sequence > after)
                    .order_by(Message.sequence)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if answer is not None:
                stored = json.loads(answer.metadata_json or "{}")
                answer.metadata_json = json.dumps({**(stored if isinstance(stored, dict) else {}), **meta})
                await db.commit()
                patched = str(answer.id)
        if patched is None:
            await asyncio.sleep(ANSWER_POLL_SECONDS)
    await engine.dispose()
    return patched


def main() -> None:
    mode, conversation_id, *rest = sys.argv[1:]
    if mode == "interrupted":
        print(asyncio.run(store_interrupted(conversation_id, rest[0])))
        return
    after, cause, resume, of, cut_at = rest
    patched = asyncio.run(mark_resumed(conversation_id, int(after), resumed_meta(cause, int(resume), int(of), cut_at)))
    print(patched or "no answer stored in time")
    sys.exit(0 if patched else 1)


if __name__ == "__main__":
    main()
