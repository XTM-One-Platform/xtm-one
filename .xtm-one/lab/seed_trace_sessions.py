"""The lab's trace sessions: conversations whose traces the /trace-sessions page lists on real rows.

Started by ``lab.py`` (``seed_trace_sessions``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the analytics seed wrote
``analytics_ids.json`` and the peer is created: no API writes an ``llm_traces`` row
(the trace sink does, from a real LLM call), so this writes the conversations, their
traces and one ``GENERATION`` span per trace through the platform's models.
Arguments: the ``analytics_ids.json`` file (``agents`` and ``users`` created through
the API by ``seed_analytics_traces``), the peer's user id (empty when the peer was not
seeded) and the JSON file the counts are written to.

* :data:`SESSIONS` conversations, 2 to 30 traces each, on one to three analytics
  agents and one to four models, so every sortable column of the list (last activity,
  conversation, traces, errors, agents, models, tokens, cost) orders differently;
* failed traces in a quarter of the sessions, 1 to 4 each (the Errors badge);
* one conversation with a title over 200 characters (:data:`LONG_TITLE`);
* one conversation owned by the peer (``lab-peer@filigran.io``);
* the last activities spread over the last :data:`DAYS` days.

So ``GET /traces/sessions`` answers more than one page of ten, with no fetch
override (issue #9742). The analytics rollups are built before these traces: the
/analytics page counts the analytics seed alone.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from seed_analytics_traces import ERRORS, MODELS, _row  # noqa: E402 - the sibling seed, by path

SESSIONS = 14
DAYS = 60
#: The session the peer owns (index in :func:`plan`).
PEER_SESSION = 2
CHANNELS = ("web", "slack", "teams")
LONG_TITLE = (
    "Seeded by the lab: weekly review of the ransomware campaigns targeting the energy sector in Europe, "
    "with the intrusion sets, the indicators shared by the partners and the detection rules to deploy "
    "before the next maintenance window"
)


def plan(agents: list[str], users: list[str], peer: str | None, now: datetime) -> list[dict]:
    """Every session to write, newest first: its owner, title, channel and traces (times relative to *now*)."""
    sessions: list[dict] = []
    for n in range(SESSIONS):
        owner = peer if n == PEER_SESSION and peer else users[n % len(users)]
        count = 2 + (7 * n) % 29  # 2 to 30 traces, a different count per session
        last = now - timedelta(days=1 + n * (DAYS - 2) // (SESSIONS - 1), hours=n % 5, minutes=3 * n)
        traces = []
        for t in range(count):
            agent = agents[(n + t % (1 + n % 3)) % len(agents)]
            model, price_in, price_out = MODELS[(n + t % (1 + n % 4)) % len(MODELS)]
            started = last - timedelta(minutes=9 * (count - 1 - t))
            input_tokens = 600 + 53 * n + 17 * t
            output_tokens = 150 + 29 * n + 7 * t
            row = _row(agent, owner, model, started, input_tokens, output_tokens, price_in, price_out, "agent-chat")
            row["name"] = "lab session turn"
            if n % 4 == 1 and t < 1 + n // 4:  # 1, 2, 3 then 4 failed traces: the Errors column sorts
                row.update(status="error", error_message=ERRORS[(n + t) % len(ERRORS)], level="ERROR")
            traces.append(row)
        sessions.append({
            "owner": owner,
            "agent": traces[0]["agent_id"],
            "channel": CHANNELS[n % len(CHANNELS)],
            "title": LONG_TITLE if n == 0 else f"Seeded by the lab: trace session {n + 1:02d}",
            "created_at": traces[0]["started_at"],
            "updated_at": traces[-1]["started_at"],
            "traces": traces,
        })
    return sessions


async def seed(agents: list[str], users: list[str], peer: str | None) -> dict[str, object]:
    """Write the conversations, their traces and spans; answer the counts and the conversation ids."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.chat import Conversation
    from app.models.llm_trace import LLMSpan, LLMTrace

    sessions = plan(agents, users, peer, datetime.now(UTC))
    conversations: list[str] = []
    async with async_session() as db:
        for session in sessions:
            conversation = Conversation(
                user_id=uuid.UUID(session["owner"]), agent_id=uuid.UUID(session["agent"]),
                channel=session["channel"], title=session["title"],
                created_at=session["created_at"], updated_at=session["updated_at"],
            )
            db.add(conversation)
            await db.flush()
            conversations.append(str(conversation.id))
            for row in session["traces"]:
                trace = LLMTrace(
                    **{**row, "agent_id": uuid.UUID(row["agent_id"]), "user_id": uuid.UUID(row["user_id"]),
                       "attributed_user_id": uuid.UUID(row["attributed_user_id"])},
                    conversation_id=conversation.id, department_resolved_at=row["started_at"],
                )
                db.add(trace)
                await db.flush()
                db.add(LLMSpan(
                    trace_id=trace.id, type="GENERATION", name="llm", model=row["model"], level=row["level"],
                    status_message=row["error_message"], started_at=row["started_at"], ended_at=row["ended_at"],
                    duration_ms=row["duration_ms"], input_tokens=row["input_tokens"],
                    output_tokens=row["output_tokens"], input_cost=row["input_cost"],
                    output_cost=row["output_cost"], total_cost=row["total_cost"],
                ))
        await db.commit()
    await engine.dispose()
    return {
        "sessions": len(sessions),
        "traces": sum(len(s["traces"]) for s in sessions),
        "conversations": conversations,
    }


def main() -> None:
    ids_file, peer, out = sys.argv[1:4]
    ids = json.loads(Path(ids_file).read_text())
    Path(out).write_text(json.dumps(asyncio.run(seed(ids["agents"], ids["users"], peer or None))))


if __name__ == "__main__":
    main()
