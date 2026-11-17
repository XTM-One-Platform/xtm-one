"""The lab's chat sidebar conversations, seeded on every lab (#11257).

Started by ``lab.py`` (``seed_chat_sidebar``) with the backend's interpreter, from ``backend/`` and
with the backend's environment. Without it the administrator's sidebar holds two conversations of
today with no agent, so a sidebar review (date groups, agent icons, the bot glyph) needed a
``window.fetch`` override. This writes, through the backend's own models, all owned by the lab
administrator:

* :data:`AGENTS` agents: emoji icons, Lucide icons, the default ``Bot`` icon and no icon at all
  (the panel draws its bot glyph);
* :data:`CONVERSATIONS` conversations, each bound to one of those agents, whose last activity
  falls on Today, Yesterday and the previous 7 days, one title over 80 characters, one Slack and
  one Teams conversation.

Arguments: the administrator's id and the JSON file the ids are written to. Idempotent: rows are
found by name or title, never written twice. No model call.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

PREFIX = "Lab sidebar "
#: (name suffix, icon): emoji, Lucide names, the default icon and an empty icon (the bot glyph).
AGENTS = (
    ("Threat intel analyst", "\U0001f575\ufe0f"),
    ("Incident responder", "\U0001f6a8"),
    ("Malware analyst", "\U0001f9a0"),
    ("Detection engineer", "Radar"),
    ("Vulnerability triage", "ShieldAlert"),
    ("Default assistant", "Bot"),
    ("Assistant with no icon", ""),
)
#: (title, agent index, days ago, channel). Days 0, 1 and 2 to 6 land in Today, Yesterday and
#: Previous 7 days of the sidebar.
CONVERSATIONS = (
    ("Ransomware note found on a file server", 0, 0, "web"),
    ("Triage of the overnight EDR alerts", 1, 0, "slack"),
    ("Sample hash lookup for the phishing attachment", 2, 0, "web"),
    ("Sigma rule review for suspicious PowerShell", 3, 1, "teams"),
    ("CVE exposure on the edge gateways", 4, 1, "web"),
    ("Weekly summary of the intrusion sets targeting the energy sector, with the indicators shared by "
     "the partners and the detection rules to deploy", 0, 2, "web"),
    ("Questions about the incident response plan", 5, 3, "web"),
    ("Draft of the board security update", 6, 4, "web"),
    ("Hunting for lateral movement over SMB", 1, 5, "web"),
    ("Patch priority for the next maintenance window", 4, 6, "web"),
    ("Unknown binary in the startup folder", 6, 6, "web"),
)


#: The thread id of a seeded Slack / Teams row, per channel: a direct-message thread (Slack DM channel
#: ids start with ``D``, Teams personal chats with ``a:``), the only channel threads the web conversation
#: list shows (``app.services.channel_continuation.web_listable_clause``). ``{n}`` is the row's index.
THREAD_IDS = {"slack": "D0LABSIDEBAR:{n}.000100", "teams": "a:lab-sidebar-{n}"}


def _row_offset(now: datetime, n: int) -> timedelta:
    """How far row ``n`` sits before ``now`` on its own day, never past that day's midnight.

    Rows are spread 11 minutes apart; when ``now`` is too close to midnight for that spread, they
    are spread over the time elapsed since midnight instead, so a lab seeded at 00:05 UTC still has
    a Yesterday group (at midnight itself the rows of a day share one instant).
    """
    elapsed = now - now.replace(hour=0, minute=0, second=0, microsecond=0)
    spread = timedelta(minutes=11 * (len(CONVERSATIONS) - 1))
    return timedelta(minutes=11 * n) if spread <= elapsed else elapsed * n / len(CONVERSATIONS)


def conversation_plan(now: datetime) -> list[dict]:
    """The sidebar conversations: full title, agent index, channel, thread id and last activity."""
    return [
        {"title": f"{PREFIX}{title}", "agent": agent, "channel": channel,
         "thread": THREAD_IDS[channel].format(n=n) if channel in THREAD_IDS else None,
         "at": now - timedelta(days=days) - _row_offset(now, n)}
        for n, (title, agent, days, channel) in enumerate(CONVERSATIONS)
    ]


async def seed(admin: str) -> dict[str, object]:
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from sqlalchemy import select

    from app.core.database import async_session, engine
    from app.models.agent import Agent
    from app.models.chat import Conversation, Message

    owner = uuid.UUID(admin)
    now = datetime.now(UTC)
    async with async_session() as db:
        agents: list[uuid.UUID] = []
        for name, icon in AGENTS:
            full = f"{PREFIX}{name}"
            row = (await db.execute(select(Agent).where(Agent.user_id == owner, Agent.name == full))).scalars().first()
            if row is None:
                row = Agent(user_id=owner, name=full, icon=icon, description="Seeded by the lab.", enabled=True,
                            is_sandbox=False)
                db.add(row)
                await db.flush()
            agents.append(row.id)
        existing = {row.title: row for row in (await db.execute(
            select(Conversation).where(Conversation.user_id == owner, Conversation.title.like(f"{PREFIX}%"))
        )).scalars().all()}
        for entry in conversation_plan(now):
            if (row := existing.get(entry["title"])) is not None:
                # A row an earlier seed wrote without its thread id is not listed: give it one.
                if entry["thread"] and not row.channel_thread_id:
                    row.channel_thread_id = entry["thread"]
                continue
            conversation = Conversation(
                user_id=owner, agent_id=agents[entry["agent"]], title=entry["title"], channel=entry["channel"],
                channel_thread_id=entry["thread"],
                created_at=entry["at"] - timedelta(minutes=20), updated_at=entry["at"],
            )
            db.add(conversation)
            await db.flush()
            for sequence, (role, content) in enumerate((("user", "Seeded question."), ("assistant", "Seeded answer."))):
                db.add(Message(conversation_id=conversation.id, role=role, content=content, sequence=sequence,
                               channel=entry["channel"], created_at=entry["at"]))
        await db.commit()
        answer = {"agents": [str(a) for a in agents], "titles": [e["title"] for e in conversation_plan(now)]}
    await engine.dispose()
    return answer


def main() -> None:
    admin, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(admin))))


if __name__ == "__main__":
    main()
