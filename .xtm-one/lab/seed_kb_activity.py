"""The lab's knowledge base activity log with every event kind, so the Activity tab renders on real rows (#61).

Started by ``lab.py`` (``seed_kb_activity``) with the backend's interpreter, from ``backend/`` and with
the backend's environment, once ``POST /api/v1/knowledge-bases`` created the knowledge base. Only a
sync, an upload, an embedding pass or a reprocess writes a ``KBActivityLog`` row, and the lab runs
none of them to completion, so this writes the rows through the backend's model:

* one row of every event kind of :data:`EVENTS` (the kinds ``KBActivityLog.event`` documents);
* one row whose details run past 300 characters (:data:`LONG_DETAILS`), so the wrapped line renders;
* :data:`TOTAL` rows in all, more than one page of the tab (50 rows), spread one hour apart back
  from now, so the pager and the time column render.

The knowledge base's earlier rows are replaced: the seed is idempotent. Arguments: the knowledge
base id and the JSON file the answer is written to, ``{"knowledge_base": id, "rows": n, "events": [kinds]}``.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: Every event kind the backend writes into ``kb_activity_logs`` (``KBActivityLog.event``), with its
#: source type (``None``: not a sync) and a details line.
EVENTS: tuple[tuple[str, str | None, str], ...] = (
    ("sync_started", "sharepoint", "Sync started for the SharePoint source Threat reports."),
    ("sync_completed", "sharepoint", "Sync completed: 12 documents added, 3 updated, 1 deleted."),
    ("sync_failed", "github", "Sync failed: the GitHub source answered 404 for the repository."),
    ("sync_alert", "google_drive", "Sync alert: 40% of the folder's files were removed since the last sync."),
    ("sync_quarantined", "google_drive", "Sync quarantined: the deletion ratio is over the safety threshold."),
    ("sync_config_error", "drata", "Sync configuration error: the Drata integration is not connected."),
    ("force_resync", "sharepoint", "Full resync requested: every document is fetched again."),
    ("document_added", None, "Document added: apt-report-2026-q3.pdf"),
    ("document_updated", None, "Document updated: incident-runbook.md"),
    ("document_deleted", None, "Document deleted: obsolete-iocs.csv"),
    ("embedding_started", None, "Embedding started: 3 documents queued."),
    ("embedding_completed", None, "Embedding completed: apt-report-2026-q3.pdf, 48 chunks."),
    ("embedding_failed", None, "Embedding failed: scanned-memo.pdf has no extractable text."),
    ("reprocess_triggered", None, "Reprocess triggered: 3 documents re-embedded."),
)
#: How many rows the seed writes: more than one page of the Activity tab (50 rows).
TOTAL = 64
#: The one details line past 300 characters, so the wrapped details render.
LONG_DETAILS = (
    "Sync completed with warnings: 214 documents were read from the SharePoint source Threat intelligence "
    "reports archive, 37 were added, 12 were updated and 4 were deleted; 9 files were skipped because their "
    "format is not supported (.one, .vsdx), 2 were skipped because they are over the size limit, and the "
    "next scheduled sync runs in 6 hours."
)


def plan(now: datetime) -> list[dict]:
    """The rows to write, newest first: every kind once, the long line, then the kinds again up to :data:`TOTAL`."""
    kinds = [*EVENTS, ("sync_completed", "sharepoint", LONG_DETAILS)]
    while len(kinds) < TOTAL:
        kinds.append(EVENTS[len(kinds) % len(EVENTS)])
    return [
        {
            "event": event,
            "source_type": source,
            "details": details,
            "document_count": 12 if event == "sync_completed" else (3 if event.startswith("embedding_") else None),
            "created_at": now - timedelta(hours=index),
        }
        for index, (event, source, details) in enumerate(kinds[:TOTAL])
    ]


async def seed(kb_id: str) -> dict:
    """Replace the knowledge base's activity rows with :func:`plan`; answer the count and the kinds."""
    from sqlalchemy import delete

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.knowledge_base import KBActivityLog, KnowledgeBase

    kb = uuid.UUID(kb_id)
    rows = plan(datetime.now(UTC))
    async with async_session() as db:
        if await db.get(KnowledgeBase, kb) is None:
            raise SystemExit(f"no knowledge base {kb_id}")
        await db.execute(delete(KBActivityLog).where(KBActivityLog.knowledge_base_id == kb))
        db.add_all(KBActivityLog(knowledge_base_id=kb, **row) for row in rows)
        await db.commit()
    await engine.dispose()
    return {"knowledge_base": kb_id, "rows": len(rows), "events": sorted({row["event"] for row in rows})}


def main() -> None:
    kb_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(kb_id))))


if __name__ == "__main__":
    main()
