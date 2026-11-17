"""The lab's fleet "Modify with AI" conversations: the pause confirm card and the raise refusal card.

Started by ``lab.py`` (``seed_fleet_chat_cards``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the backend answers and the fleets
are seeded. The lab model is a stub that calls no tool, so no turn ever stores a fleet
tool's answer and the panel's cards (``fleetChatCard``, #10162) never render. This writes,
through the platform's own models, two conversations bound to the fleet with ``fleet_id``
whose last assistant message stores the tool's real answer in ``tool_call_trace``:

* ``confirm``: ``set_fleet_automations(action="pause")`` without ``confirm``, the summary
  ``builtin_tools/fleets.py`` answers, its count read with ``pausable_count`` on the fleet;
* ``raise_refused``: ``update_fleet`` refusing an autonomy raise, the backend's own
  ``_AUTONOMY_RAISE_REFUSAL``.

Arguments: the owner (the lab administrator), the fleet id, and the JSON file the answer
(``{kind: conversation id}``) is written to. No model call; nothing reads them but a person.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

SEEDED_BY = "lab"


def pause_summary(count: int, fleet_name: str) -> str:
    """The answer of ``set_fleet_automations(action="pause")`` without ``confirm``, word for word."""
    return (
        f"Not applied. Pause all would switch off {count} automation(s) of fleet **{fleet_name}** "
        "(runs in flight continue; Resume switches back on exactly these). Show this to the user and "
        "call again with confirm=true only once they agree."
    )


def plan(fleet_id: str, fleet_name: str, count: int, refusal: str) -> list[dict]:
    """The two conversations: kind, title, the user's ask, the trace entry and the agent's answer."""
    return [
        {
            "kind": "confirm",
            "title": "Pause the automations of this fleet",
            "ask": "Pause all the automations of this fleet.",
            "trace": {
                "name": "set_fleet_automations",
                "input": json.dumps({"fleet_id": fleet_id, "action": "pause"}),
                "output": pause_summary(count, fleet_name),
                "success": True,
            },
            "answer": f"Pausing would switch off {count} automation(s) of {fleet_name}. Confirm to apply.",
        },
        {
            "kind": "raise_refused",
            "title": "Raise the autonomy of this fleet",
            "ask": "Set the autonomy level of this fleet to High.",
            "trace": {
                "name": "update_fleet",
                "input": json.dumps({"fleet_id": fleet_id, "autonomy_level": "high"}),
                "output": refusal,
                "success": True,
            },
            "answer": "I can only lower the autonomy level. Raise it from the fleet page's autonomy control.",
        },
    ]


async def seed(user_id: str, fleet_id: str) -> dict[str, str]:
    """Create the two conversations on *fleet_id*; answer ``{kind: conversation id}``."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.chat import Conversation, Message
    from app.models.fleet import Fleet
    from app.services.builtin_tools.fleets import _AUTONOMY_RAISE_REFUSAL
    from app.services.fleet_control import pausable_count

    owner = uuid.UUID(user_id)
    answer: dict[str, str] = {}
    async with async_session() as db:
        fleet = await db.get(Fleet, uuid.UUID(fleet_id))
        if fleet is None:
            raise RuntimeError(f"no fleet {fleet_id}")
        count = await pausable_count(db, fleet)
        for row in plan(str(fleet.id), fleet.name, count, _AUTONOMY_RAISE_REFUSAL):
            conversation = Conversation(user_id=owner, channel="web", title=row["title"], fleet_id=fleet.id)
            db.add(conversation)
            await db.flush()
            db.add_all([
                Message(conversation_id=conversation.id, role="user", content=row["ask"], channel="web", sequence=1),
                Message(
                    conversation_id=conversation.id, role="assistant", content=row["answer"], channel="web",
                    sequence=2, metadata_json=json.dumps({"tool_call_trace": [row["trace"]], "seeded_by": SEEDED_BY}),
                ),
            ])
            answer[row["kind"]] = str(conversation.id)
        await db.commit()
    await engine.dispose()
    return answer


def main() -> None:
    user_id, fleet_id, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id, fleet_id))))


if __name__ == "__main__":
    main()
