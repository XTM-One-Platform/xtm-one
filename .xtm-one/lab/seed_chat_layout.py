"""The lab's chat-layout data, under ``LAB_CHAT_LAYOUT=on`` (program chat-layout, #10910).

Started by ``lab.py`` (``seed_chat_layout``) with the backend's interpreter, from ``backend/`` and
with the backend's environment. The default lab holds a handful of agents and short conversations,
so the chat sidebar, the agent picker and a long rendered thread never show on real rows. This
writes, through the backend's own models, all owned by the lab administrator:

* :data:`AGENTS` agents with distinct Lucide icons, one with the default icon and one whose name
  is over 60 characters (the picker and the header truncate it);
* :data:`WORKSPACES` workspaces with distinct icons and colours, one with a long name;
* :data:`CONVERSATIONS` conversations spread over Today, Yesterday, the last 7 days and older,
  some with long or French titles, filed in the workspaces or unfiled;
* one thread of :data:`THREAD_TURNS` turns whose answers carry Markdown (h1 to h3, a table wider
  than the column, a 200-line code block with a language tag, inline code, nested lists, a
  blockquote, links, an image, a sources list), a tool-call trace and a reasoning block, with
  attachment metadata (an image, a PDF, a CSV) on sent messages;
* feedback on three answers (thumbs up, thumbs down with a comment), and the thread readable by
  link (``link_access`` ``org_read``) and shared read-only with the peer when it was seeded;
* one "Markdown coverage" conversation (:data:`COVERAGE_TITLE`) whose answer is
  :data:`MARKDOWN_COVERAGE`: a 6-column and an 8-column table, a 3-level list with a numbered
  sub-list and a task list, a blockquote, and python, typescript, json, bash and sql blocks.

Arguments: the administrator's id, the peer's id (empty when the peer was not seeded) and the
JSON file the ids are written to. Idempotent: rows are found by name or title, never written
twice. No model call.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

PREFIX = "Lab chat "
#: (name suffix, Lucide icon): eight distinct icons, then the default one.
AGENTS = (
    ("Threat analyst", "Shield"),
    ("Malware reverser", "Bug"),
    ("Detection engineer", "Radar"),
    ("Vulnerability triage", "ShieldAlert"),
    ("Report writer", "FileText"),
    ("Hunting lead", "Crosshair"),
    ("Cloud posture", "Cloud"),
    ("Identity watch", "Fingerprint"),
    ("No icon chosen", "Bot"),
    ("Agent with a very long name that the picker and the chat header must truncate cleanly", "Sparkles"),
)
LONG_AGENT = AGENTS[-1][0]
#: (name suffix, Lucide icon, colour token).
WORKSPACES = (
    ("Incident response", "Siren", "#e5484d"),
    ("Threat intelligence", "Globe", "#3e63dd"),
    ("Red team", "Swords", "#d6409f"),
    ("Compliance", "ClipboardCheck", "#30a46c"),
    ("Cloud security", "Cloud", "#0091ff"),
    ("Vulnerability management", "ShieldAlert", "#f76b15"),
    ("Detection rules", "Radar", "#8e4ec6"),
    ("Executive briefings", "Presentation", "#ffb224"),
    ("Partners", "Handshake", "#12a594"),
    ("Workspace with a long name that the sidebar and the picker must truncate cleanly", "LayoutGrid", ""),
)
#: Conversation titles: English, French, long. Spread by :func:`conversation_plan` over the buckets.
TITLES = (
    "Ransomware campaign against European hospitals",
    "Revue des indicateurs partagés par le CERT-FR cette semaine",
    "Phishing kit with a reverse proxy",
    "Analyse du rançongiciel LockBit 4 et des règles de détection associées",
    "Sigma rule for suspicious PowerShell",
    "Préparation du comité de pilotage sécurité",
    "Weekly review of the intrusion sets targeting the energy sector in Europe, with the indicators "
    "shared by the partners and the detection rules to deploy before the next maintenance window",
    "CVE-2026-1234 exposure on the edge gateways",
    "Synthèse pour la direction générale",
    "Cloud misconfiguration findings",
)
CONVERSATIONS = 44
THREAD_TITLE = f"{PREFIX}rendering: a 30-turn thread with Markdown, tools and reasoning"
THREAD_TURNS = 32
#: Days ago of each bucket the sidebar groups by: Today, Yesterday, 7 days, older.
BUCKETS = (0, 1, 4, 20, 75)
COVERAGE_TITLE = f"{PREFIX}Markdown coverage: tables, nested lists, task list, quote, five languages"
#: Every Markdown element a chat retest checks, in one answer (also what ``lab:echo`` is sent in a retest).
MARKDOWN_COVERAGE = "\n".join((
    "# Markdown coverage",
    "",
    "## Six columns",
    "",
    "| Indicator | Type | Score | First seen | Last seen | Source |",
    "|---|---|---|---|---|---|",
    "| 198.51.100.7 | ipv4 | 82 | 2026-11-01 | 2026-11-09 | lab |",
    "| evil.example | domain | 64 | 2026-11-03 | 2026-11-10 | lab |",
    "",
    "## Eight columns, wider than the column",
    "",
    "| Indicator | Type | First seen | Last seen | Confidence | Source | Kill chain phase | Notes |",
    "|---|---|---|---|---|---|---|---|",
    "| 5f4dcc3b5aa765d61d8327deb882cf99 | md5 | 2026-11-05 | 2026-11-10 | high | partner feed | delivery | "
    "dropped by the loader before the second stage |",
    "| hxxps://update.evil.example/payload | url | 2026-11-06 | 2026-11-10 | medium | sandbox | "
    "command and control | beacon every 300 seconds with jitter |",
    "",
    "## Nested lists",
    "",
    "- Level one",
    "  - Level two",
    "    - Level three",
    "  - Numbered steps:",
    "    1. Collect the sample",
    "    2. Detonate it",
    "    3. Write the rule",
    "- Task list:",
    "  - [x] Indicators reviewed",
    "  - [ ] Rule deployed",
    "",
    "> A blockquote: the loader resolves imports by hash, so the strings are not in clear.",
    "",
    "```python",
    "def score(hits: int) -> int:",
    "    return min(100, hits * 3)",
    "```",
    "",
    "```typescript",
    "export function score(hits: number): number {",
    "  return Math.min(100, hits * 3);",
    "}",
    "```",
    "",
    "```json",
    '{"indicator": "198.51.100.7", "score": 82, "labels": ["c2", "lab"]}',
    "```",
    "",
    "```bash",
    "curl -s https://example.org/feed.json | jq '.indicators | length'",
    "```",
    "",
    "```sql",
    "SELECT indicator, score FROM observables WHERE score > 80 ORDER BY score DESC;",
    "```",
))
#: A 1 x 1 PNG, inline, so the image renders with no outbound fetch.
IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def code_block(lines: int = 200) -> str:
    """A Python block of *lines* lines with its language tag."""
    body = [f"def step_{n:03d}(value: int) -> int:  # line {n + 1}" if n % 2 == 0 else f"    return value + {n}"
            for n in range(lines)]
    return "```python\n" + "\n".join(body) + "\n```"


def markdown_answer(turn: int) -> str:
    """The Markdown of answer *turn*: every element the chat renders, spread over the thread."""
    parts = [f"# Finding {turn + 1}", "## Summary", "### Detail",
             f"The sample calls `CreateRemoteThread` then `VirtualAllocEx` (turn {turn + 1})."]
    if turn % 4 == 0:
        parts.append(
            "| Indicator | Type | First seen | Last seen | Confidence | Source | Kill chain phase | Notes |\n"
            "|---|---|---|---|---|---|---|---|\n"
            "| 185.220.101.4 | IPv4 | 2026-10-01 | 2026-11-09 | High | Partner feed | Command and control | "
            "Seen in three campaigns with a rotating certificate |\n"
            "| evil-update[.]example | Domain | 2026-09-12 | 2026-11-08 | Medium | Sandbox | Delivery | "
            "Typosquat of a software vendor's update server |"
        )
    if turn % 4 == 1:
        parts.append("- Initial access\n  - Phishing with an ISO file\n    - LNK launcher\n- Execution\n"
                     "  1. PowerShell loader\n  2. Reflective DLL")
    if turn % 4 == 2:
        parts.append("> The actor reused the same infrastructure for six weeks.\n> Block the range at the edge.")
        parts.append(f"![Lab image]({IMAGE})")
    if turn % 4 == 3:
        parts.append("Read the [MITRE ATT&CK page](https://attack.mitre.org/techniques/T1055/) and the "
                     "[vendor advisory](https://example.com/advisory).")
    if turn == 5:
        parts.append(code_block())
    parts.append("**Sources**\n1. [Partner feed, 9 Nov 2026](https://example.com/feed)\n"
                 "2. [Sandbox report](https://example.com/sandbox)")
    return "\n\n".join(parts)


#: The conversations whose last answer is a long text then a scheduled work item card (#10910, item
#: 6c501d9f): one as a plain answer, one as the reply of a turn the platform resumed by itself.
CARD_TITLES = (f"{PREFIX}answer with a work-item card", f"{PREFIX}resumed answer with a work-item card")


def card_answer(item_id: uuid.UUID) -> str:
    """A long answer (headings, paragraphs, a list of options) ending with the card marker of *item_id*."""
    paragraphs = [f"Paragraph {n + 1}: the close-today report reads every program live, then lists what is still "
                  "open with its owner, its pull request and the time it is due." for n in range(8)]
    options = "\n".join(f"- **{key}:** option {key} of the decision, explained in one line" for key in "ABC")
    return ("## Close-today report\n\n" + "\n\n".join(paragraphs) + "\n\nWhich scope do you want?\n\n"
            + options + f"\n\n[[WORK_ITEM:{str(item_id).lower()}]]")


async def seed_card_answers(db, owner: uuid.UUID, agent: uuid.UUID, now: datetime) -> dict[str, str]:
    """One scheduled one-off and the :data:`CARD_TITLES` conversations whose last answer carries its card."""
    from sqlalchemy import select

    from app.models.chat import Conversation, Message
    from app.models.work_item import WorkItem

    title = f"{PREFIX}close-today report (11:45Z)"
    item = (await db.execute(select(WorkItem).where(WorkItem.user_id == owner, WorkItem.title == title))).scalars().first()
    if item is None:
        prompt = ("Close-today deadline reached: read every program live, list what is still open with its owner, "
                  "its pull request and its due time, and post the report in the direct message.") * 2
        item = WorkItem(user_id=owner, title=title, status="open", priority="medium", source="user",
                        due_at=now + timedelta(hours=3), action_mode="instruction",
                        payload={"action": {"prompt": prompt, "target": "Teams direct message"}})
        db.add(item)
        await db.flush()
    ids = {"work item card": str(item.id)}
    for index, conv_title in enumerate(CARD_TITLES):
        found = (await db.execute(
            select(Conversation).where(Conversation.user_id == owner, Conversation.title == conv_title)
        )).scalars().first()
        if found is None:
            found = Conversation(user_id=owner, agent_id=agent, title=conv_title, created_at=now - timedelta(minutes=20),
                                 updated_at=now - timedelta(minutes=index))
            db.add(found)
            await db.flush()
            item.conversation_id = found.id if index == 0 else item.conversation_id
            meta = ({"resumed_from": {"cause": "stop", "resume": 1, "of": 3, "cut_at": (now - timedelta(minutes=5)).isoformat()}}
                    if index == 1 else None)
            rows = (("user", "Prepare the close-today report and schedule it for 11:45Z.", None),
                    ("assistant", card_answer(item.id), meta))
            for sequence, (role, content, extra) in enumerate(rows):
                db.add(Message(conversation_id=found.id, role=role, content=content, sequence=sequence,
                               metadata_json=json.dumps(extra) if extra else None,
                               created_at=now - timedelta(minutes=10 - sequence)))
        ids[conv_title] = str(found.id)
    return ids


def thread_messages(start: datetime) -> list[dict]:
    """The thread's messages in order: user, then assistant, :data:`THREAD_TURNS` times."""
    messages: list[dict] = []
    for turn in range(THREAD_TURNS):
        at = start + timedelta(minutes=2 * turn)
        meta: dict = {}
        if turn in (0, 1, 2):
            name, kind, ctype, size = (("capture.png", "png", "image/png", 48_213),
                                       ("advisory.pdf", "pdf", "application/pdf", 512_000),
                                       ("indicators.csv", "csv", "text/csv", 9_870))[turn]
            meta["attachments"] = [{"filename": name, "type": kind, "size": size, "content_type": ctype}]
        messages.append({"role": "user", "content": f"Question {turn + 1}: what does the sample do next?",
                         "metadata": meta, "at": at})
        answer_meta: dict = {}
        if turn % 3 == 0:
            answer_meta["tool_call_trace"] = [{"name": "search_knowledge", "input": '{"query": "LockBit"}',
                                               "output": "3 documents found", "success": True}]
        if turn % 3 == 1:
            answer_meta["reasoning"] = "The loader resolves imports by hash, so the strings are not in clear."
        messages.append({"role": "assistant", "content": markdown_answer(turn), "metadata": answer_meta,
                         "at": at + timedelta(seconds=40)})
    return messages


def conversation_plan(now: datetime) -> list[dict]:
    """The sidebar conversations: title, workspace index (None unfiled), agent index, last activity."""
    plan = []
    for n in range(CONVERSATIONS):
        days = BUCKETS[n % len(BUCKETS)]
        title = f"{PREFIX}{TITLES[n % len(TITLES)]}" if n < len(TITLES) else f"{PREFIX}conversation {n + 1:02d}"
        plan.append({"title": title, "workspace": None if n % 3 == 0 else n % len(WORKSPACES),
                     "agent": n % len(AGENTS), "at": now - timedelta(days=days, minutes=7 * n)})
    return plan


async def seed(admin: str, peer: str | None) -> dict[str, object]:
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from sqlalchemy import select

    from app.core.database import async_session, engine
    from app.models.agent import Agent
    from app.models.chat import Conversation, Message, MessageFeedback
    from app.models.conversation_share import ConversationShare
    from app.models.workspace import Workspace
    from app.services.workspaces import slugify

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
        workspaces: list[uuid.UUID] = []
        for name, icon, color in WORKSPACES:
            full = f"{PREFIX}{name}"
            row = (await db.execute(
                select(Workspace).where(Workspace.user_id == owner, Workspace.name == full)
            )).scalars().first()
            if row is None:
                row = Workspace(user_id=owner, name=full, slug=slugify(full), icon=icon, color=color,
                                description="Seeded by the lab.")
                db.add(row)
                await db.flush()
            workspaces.append(row.id)
        titles = {t for (t,) in (await db.execute(
            select(Conversation.title).where(Conversation.user_id == owner, Conversation.title.like(f"{PREFIX}%"))
        )).all()}
        for entry in conversation_plan(now):
            if entry["title"] in titles:
                continue
            conversation = Conversation(
                user_id=owner, agent_id=agents[entry["agent"]], title=entry["title"],
                workspace_id=None if entry["workspace"] is None else workspaces[entry["workspace"]],
                created_at=entry["at"] - timedelta(minutes=30), updated_at=entry["at"],
            )
            db.add(conversation)
            await db.flush()
            for sequence, (role, content) in enumerate((("user", "Seeded question."), ("assistant", "Seeded answer."))):
                db.add(Message(conversation_id=conversation.id, role=role, content=content, sequence=sequence,
                               created_at=entry["at"]))
        thread = (await db.execute(
            select(Conversation).where(Conversation.user_id == owner, Conversation.title == THREAD_TITLE)
        )).scalars().first()
        if thread is None:
            start = now - timedelta(hours=2)
            thread = Conversation(user_id=owner, agent_id=agents[0], workspace_id=workspaces[1], title=THREAD_TITLE,
                                  link_access="org_read", created_at=start, updated_at=now - timedelta(minutes=1))
            db.add(thread)
            await db.flush()
            answers: list[Message] = []
            for sequence, message in enumerate(thread_messages(start)):
                row = Message(conversation_id=thread.id, role=message["role"], content=message["content"],
                              metadata_json=json.dumps(message["metadata"]) if message["metadata"] else None,
                              sequence=sequence, created_at=message["at"])
                db.add(row)
                if message["role"] == "assistant":
                    answers.append(row)
            await db.flush()
            for row, rating, comment in ((answers[0], "positive", None),
                                         (answers[1], "negative", "The table misses the last seen dates."),
                                         (answers[2], "positive", "Clear and sourced.")):
                db.add(MessageFeedback(message_id=row.id, user_id=owner, rating=rating, comment=comment))
            if peer:
                db.add(ConversationShare(conversation_id=thread.id, shared_with_user_id=uuid.UUID(peer),
                                         created_by=owner))
        coverage = (await db.execute(
            select(Conversation).where(Conversation.user_id == owner, Conversation.title == COVERAGE_TITLE)
        )).scalars().first()
        if coverage is None:
            coverage = Conversation(user_id=owner, agent_id=agents[0], title=COVERAGE_TITLE,
                                    created_at=now - timedelta(minutes=5), updated_at=now)
            db.add(coverage)
            await db.flush()
            for sequence, (role, content) in enumerate((("user", "Show every Markdown element."),
                                                        ("assistant", MARKDOWN_COVERAGE))):
                db.add(Message(conversation_id=coverage.id, role=role, content=content, sequence=sequence,
                               created_at=now))
        cards = await seed_card_answers(db, owner, agents[0], now)
        await db.commit()
        answer = {"thread": str(thread.id), "coverage": str(coverage.id), **cards,
                  "agents": [str(a) for a in agents],
                  "workspaces": [str(w) for w in workspaces], "conversations": CONVERSATIONS + 2}
    await engine.dispose()
    return answer


def main() -> None:
    admin, peer, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(admin, peer or None))))


if __name__ == "__main__":
    main()
