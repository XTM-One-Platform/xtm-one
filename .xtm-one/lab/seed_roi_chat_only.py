"""The lab's chat-only ROI value: counted chat lines with no agent, and an agent and fleet with no run.

Started by ``lab.py`` (``seed_roi_chat_only``) with the backend's interpreter, from ``backend/`` and
with the backend's environment, once the backend answers. A ``roi_chat_records`` row is written by
the platform when an agent saves a deliverable in a conversation (``services/roi_chat_records.py``)
and no API creates one, so this writes them through the platform's models. Arguments: the lab
administrator, the chat-only agent, its fleet, and the JSON file the seeded ids are written to.

:func:`plan` (pure, times relative to *now*) writes counted lines (a use signal) only, all inside
the 7-day window, so every rolling window (7D, 30D, 90D, All) carries them:

* lines with ``agent_id`` NULL: the ledger keys them ``no_agent`` (``roi_figures.ROW_NO_AGENT``), so
  By agent of the organization carries a remainder row with value next to the agent rows;
* lines of the chat-only agent, stamped with its fleet: that agent and that fleet have value and no
  run, the chat-only state of the Outcomes tab, the agent page and the fleet ROI view.

No model call, no conversation: ``conversation_id`` stays NULL, so the measured time is 0.
dev-docs/reference/agentic-labs.md, "The xtm-one lab" (dev:harness-gaps lab-roi-chat-only-window-seed).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: (label, kind, size, signal, hours since the last edit, on the chat-only agent, title).
LINES: tuple[tuple[str, str, float, str, float, bool, str], ...] = (
    ("no-agent-report", "report", 6, "exported", 30, False, "Weekly exposure summary, written without an agent"),
    ("no-agent-deck", "presentation", 10, "presented", 54, False, "Board update deck, written without an agent"),
    ("chat-only-report", "report", 8, "shared", 20, True, "Phishing campaign report from the chat-only agent"),
    ("chat-only-file", "file", 2, "sent", 72, True, "Decision note from the chat-only agent"),
)


def plan(now: datetime, *, admin: str, agent: str, fleet: str) -> list[dict]:
    """The ``roi_chat_records`` columns of every seeded line, oldest edit first, times relative to *now*."""
    rows = []
    for label, kind, size, signal, hours, on_agent, title in sorted(LINES, key=lambda line: -line[4]):
        edited = now - timedelta(hours=hours)
        deliverable = str(uuid.uuid5(uuid.NAMESPACE_URL, f"xtm-one-lab:roi-chat-only:{label}:{now.isoformat()}"))
        rows.append({
            "label": label,
            "record": {
                "record_key": f"{kind}:{deliverable}", "kind": kind, "deliverable_id": deliverable,
                "user_id": admin, "agent_id": agent if on_agent else None, "fleet_id": fleet if on_agent else None,
                "fleet_backfilled": False if on_agent else None, "conversation_id": None, "title": title,
                "size": float(size), "signals": [signal], "first_signal_at": edited + timedelta(minutes=30),
                "last_edit_at": edited, "created_at": edited - timedelta(hours=1),
            },
        })
    return rows


async def seed(admin: str, agent: str, fleet: str) -> dict[str, object]:
    """Write the lines; answer their ids by label."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.roi import RoiChatRecord

    keyed = {"user_id", "agent_id", "fleet_id"}
    async with async_session() as db:
        rows = []
        for row in plan(datetime.now(UTC), admin=admin, agent=agent, fleet=fleet):
            values = {k: uuid.UUID(v) if k in keyed and isinstance(v, str) else v for k, v in row["record"].items()}
            record = RoiChatRecord(**values)
            db.add(record)
            rows.append((row["label"], record))
        await db.commit()
        written = {label: str(record.id) for label, record in rows}
    await engine.dispose()
    return {"records": written}


def main() -> None:
    admin, agent, fleet, out = sys.argv[1:5]
    Path(out).write_text(json.dumps(asyncio.run(seed(admin, agent, fleet))))


if __name__ == "__main__":
    main()
