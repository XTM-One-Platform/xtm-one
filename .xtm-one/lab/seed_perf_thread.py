"""The lab's long conversations: a 200-message thread and a short one, for chat timings.

Started by ``lab.py`` (``seed_perf_thread``) with the backend's interpreter, from ``backend/``
and with the backend's environment, once the backend answers, when ``LAB_PERF_THREAD=on``.
No API writes stored history and the lab model answers one short line, so a long thread
cannot be grown through the chat: this writes, through the platform's own models, two
conversations of the administrator:

* ``long``: "Perf 200-message thread", 100 turns whose answers mix headings, lists, tables
  and code blocks (the Markdown the message list renders), for scroll, open and switch timings;
* ``short``: "Perf short thread", 2 turns, the other end of a thread switch.

Arguments: the owner (the lab administrator) and the JSON file the answer
(``{kind: conversation id}``) is written to. No model call.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

SEEDED_BY = "lab"
LONG_TITLE = "Perf 200-message thread"
SHORT_TITLE = "Perf short thread"
LONG_TURNS = 100
SHORT_TURNS = 1


def answer(turn: int) -> str:
    """The assistant's answer of *turn*: one of four Markdown shapes, so every renderer is measured."""
    shape = turn % 4
    if shape == 0:
        rows = "\n".join(f"| T{turn}.{i} | {i * 7 % 13} | {'open' if i % 2 else 'closed'} |" for i in range(1, 9))
        return f"### Turn {turn}: table\n\n| Item | Score | State |\n|---|---|---|\n{rows}\n"
    if shape == 1:
        items = "\n".join(f"- Point {i} of turn {turn}, with **bold** and `code` inline" for i in range(1, 7))
        nested = "\n".join(f"  {i}. Nested step {i}" for i in range(1, 4))
        return f"### Turn {turn}: list\n\n{items}\n{nested}\n"
    if shape == 2:
        body = "\n".join(f"    value_{i} = compute({turn}, {i})" for i in range(1, 12))
        return f"### Turn {turn}: code\n\n```python\ndef turn_{turn}():\n{body}\n    return value_1\n```\n"
    return (
        f"### Turn {turn}: prose\n\n"
        + " ".join(f"Sentence {i} of a long paragraph that wraps over several lines in the message list." for i in range(1, 9))
        + "\n\n> A quote closes the answer."
    )


def plan() -> list[dict]:
    """The two conversations: kind, title and the (ask, answer) pairs, in order."""
    return [
        {
            "kind": "long", "title": LONG_TITLE,
            "turns": [(f"Question {t}: show me the next result.", answer(t)) for t in range(1, LONG_TURNS + 1)],
        },
        {
            "kind": "short", "title": SHORT_TITLE,
            "turns": [(f"Short question {t}.", f"Short answer {t}.") for t in range(1, SHORT_TURNS + 1)],
        },
    ]


async def seed(user_id: str) -> dict[str, str]:
    """Create the two conversations of *user_id*; answer ``{kind: conversation id}``."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.chat import Conversation, Message

    owner = uuid.UUID(user_id)
    result: dict[str, str] = {}
    metadata = json.dumps({"seeded_by": SEEDED_BY})
    async with async_session() as db:
        for row in plan():
            conversation = Conversation(user_id=owner, channel="web", title=row["title"])
            db.add(conversation)
            await db.flush()
            messages = []
            for index, (ask, reply) in enumerate(row["turns"]):
                messages.append(Message(
                    conversation_id=conversation.id, role="user", content=ask, channel="web", sequence=2 * index + 1,
                ))
                messages.append(Message(
                    conversation_id=conversation.id, role="assistant", content=reply, channel="web",
                    sequence=2 * index + 2, metadata_json=metadata,
                ))
            db.add_all(messages)
            result[row["kind"]] = str(conversation.id)
        await db.commit()
    await engine.dispose()
    return result


def main() -> None:
    user_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id))))


if __name__ == "__main__":
    main()
