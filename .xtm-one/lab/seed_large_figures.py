"""The lab's fleet and home figures of 1,000 and more, under ``LAB_FIGURES_SCALE=large`` (#10734).

Started by ``lab.py`` (``seed_large_figures``) with the backend's interpreter, from ``backend/`` and
with the backend's environment. Every other lab seed stays under 1,000 on the fleet About page and
on the Copilot home: no seeded fleet stores ``structured.quota`` (every Quota estimate tile reads
"None yet"), the busiest fleet has a few dozen runs, and ``GET /activity/stats`` answers agents,
conversations, runs this month and documents under 100. So the K compaction of those tiles and
cards, and the exact value on hover, rendered only through a ``window.fetch`` override. This
writes, through the backend's own models and writers:

* on each of the three fleets *lab.py* created (one member with one automation switched on), a
  documentation version whose quota lines sum to 1,000 and more (``fleet_docs.save_documentation``):
  the first one fixed schedule (the tile reads "About 1.2K"), the second one open-ended event line
  (a floor), the third a schedule range;
* ``FLEET_RUNS`` runs of the first fleet's member over the last ``RUN_DAYS`` days, owned by the lab
  administrator (``run_visible_filter`` shows them): the fleet's "Runs, last 30 days" and the home's
  "runs this month" both read in K;
* ``HOME_AGENTS`` agents, ``HOME_CONVERSATIONS`` conversations and ``HOME_DOCUMENTS`` ready documents
  in one knowledge base, all owned by the administrator, so the Copilot home KPI cards read in K.

Arguments: the administrator's id, the three fleet ids, the first fleet's member agent id, and the
JSON file the counts are written to. Idempotent for the home rows (found by name, never written
twice); the fleets are new at every lab start. No model call.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

SEEDED_BY = {"seeded_by": "lab"}
#: The runs of the first fleet, over the last RUN_DAYS days (inside the About page's 30-day window).
FLEET_RUNS = 1240
RUN_DAYS = 28
#: One run in FAILED_EVERY fails, so the success rate is not 100%.
FAILED_EVERY = 20
#: The home rows: agents, conversations and knowledge documents of the administrator.
HOME_AGENTS = 1060
HOME_CONVERSATIONS = 1130
HOME_DOCUMENTS = 1050
AGENT_PREFIX = "Lab scale agent "
CONVERSATION_PREFIX = "Lab scale conversation "
KNOWLEDGE_BASE = "Lab scale knowledge base"

SECTIONS = {
    "purpose": "Seeded by the lab: a fleet whose About page shows figures of 1,000 and more.",
}


def quota_lines(index: int) -> list[dict]:
    """The quota lines of fleet *index* (0 about, 1 open-ended floor, 2 range); each sums to 1,000 and more."""
    if index == 0:
        return [{"trigger": "Lab hourly triage", "basis": "Estimate: one run per tick of the schedule over 30 days",
                 "runs_min": 720, "runs_max": 720, "kinds": ["schedule"]},
                {"trigger": "Lab half-hourly digest", "basis": "Estimate: one run per tick of the schedule over 30 days",
                 "runs_min": 520, "runs_max": 520, "kinds": ["schedule"]}]
    if index == 1:
        return [{"trigger": "Lab new indicator", "basis": "Observed: 1,180 runs in the last 30 days; event-driven, no fixed top",
                 "runs_min": 1180, "runs_max": 1180, "open_ended": True, "kinds": ["event"], "observed": True}]
    return [{"trigger": "Lab business-hours sweep", "basis": "Estimate: between 1,050 and 2,400 runs over 30 days",
             "runs_min": 1050, "runs_max": 2400, "kinds": ["schedule"]}]


def quota_total(lines: list[dict]) -> tuple[int, int]:
    """The (min, max) the About page sums from *lines*."""
    return sum(q["runs_min"] for q in lines), sum(q["runs_max"] for q in lines)


def run_rows(agent: str, user: str, now: datetime) -> list[dict]:
    """The ``AssignmentRun`` columns of the first fleet's runs, newest first, spread over RUN_DAYS days."""
    step = timedelta(days=RUN_DAYS) / FLEET_RUNS
    rows = []
    for n in range(FLEET_RUNS):
        started = now - timedelta(minutes=5) - step * n
        failed = n % FAILED_EVERY == FAILED_EVERY - 1
        rows.append({
            "agent_id": uuid.UUID(agent), "user_id": uuid.UUID(user),
            "status": "failed" if failed else "completed",
            "error": "Seeded by the lab: the agent's own work failed." if failed else None,
            "trigger_payload": SEEDED_BY, "trigger_context": {"type": "schedule", **SEEDED_BY},
            "result_summary": "Seeded by the lab: one of the large fleet's runs.",
            "created_at": started, "started_at": started, "completed_at": started + timedelta(minutes=1),
        })
    return rows


async def seed(author: str, fleet_ids: list[str], member: str) -> dict:
    """Write the documentation, the runs and the home rows; answer the counts."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from sqlalchemy import func, insert, select

    from app.core.database import async_session, engine
    from app.models.agent import Agent
    from app.models.assignment import AssignmentRun
    from app.models.chat import Conversation
    from app.models.fleet import Fleet
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
    from app.models.user import User
    from app.services.fleet_docs import save_documentation

    now = datetime.now(UTC)
    answer: dict = {"fleets": {}}
    async with async_session() as db:
        user = await db.get(User, uuid.UUID(author))
        if user is None:
            raise RuntimeError(f"no user {author}")
        for index, fleet_id in enumerate(fleet_ids):
            fleet = await db.get(Fleet, uuid.UUID(fleet_id))
            if fleet is None:
                raise RuntimeError(f"no fleet {fleet_id}")
            lines = quota_lines(index)
            await save_documentation(
                db, fleet, dict(SECTIONS), author=user.id, base_version=None, structured={"quota": lines}
            )
            answer["fleets"][fleet_id] = list(quota_total(lines))
        await db.execute(insert(AssignmentRun), run_rows(member, author, now))

        owned = user.id
        agents = {
            name for (name,) in (await db.execute(
                select(Agent.name).where(Agent.user_id == owned, Agent.name.like(f"{AGENT_PREFIX}%"))
            )).all()
        }
        new_agents = [
            {"user_id": owned, "name": f"{AGENT_PREFIX}{n + 1:04d}", "description": "Seeded by the lab.",
             "enabled": True, "is_sandbox": False}
            for n in range(HOME_AGENTS) if f"{AGENT_PREFIX}{n + 1:04d}" not in agents
        ]
        if new_agents:
            await db.execute(insert(Agent), new_agents)
        titles = {
            title for (title,) in (await db.execute(
                select(Conversation.title).where(
                    Conversation.user_id == owned, Conversation.title.like(f"{CONVERSATION_PREFIX}%")
                )
            )).all()
        }
        new_conversations = [
            {"user_id": owned, "title": f"{CONVERSATION_PREFIX}{n + 1:04d}", "is_archived": False}
            for n in range(HOME_CONVERSATIONS) if f"{CONVERSATION_PREFIX}{n + 1:04d}" not in titles
        ]
        if new_conversations:
            await db.execute(insert(Conversation), new_conversations)
        kb = (await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.user_id == owned, KnowledgeBase.name == KNOWLEDGE_BASE)
        )).scalars().first()
        if kb is None:
            kb = KnowledgeBase(user_id=owned, name=KNOWLEDGE_BASE, description="Seeded by the lab.")
            db.add(kb)
            await db.flush()
        have = (await db.execute(
            select(func.count(KnowledgeDocument.id)).where(KnowledgeDocument.knowledge_base_id == kb.id)
        )).scalar_one()
        new_documents = [
            {"knowledge_base_id": kb.id, "filename": f"lab-scale-{n + 1:04d}.md", "source_type": "text",
             "content": "Seeded by the lab.", "status": "ready", "chunk_count": 0, "added_by_user_id": owned}
            for n in range(int(have), HOME_DOCUMENTS)
        ]
        if new_documents:
            await db.execute(insert(KnowledgeDocument), new_documents)
        await db.commit()
    await engine.dispose()
    answer.update(
        runs=FLEET_RUNS, agents=HOME_AGENTS, conversations=HOME_CONVERSATIONS, documents=HOME_DOCUMENTS,
        new_rows=FLEET_RUNS + len(new_agents) + len(new_conversations) + len(new_documents),
    )
    return answer


def main() -> None:
    author, first, second, third, member, out = sys.argv[1:7]
    Path(out).write_text(json.dumps(asyncio.run(seed(author, [first, second, third], member))))


if __name__ == "__main__":
    main()
