"""The lab's "From chat, estimated" lines: ``roi_chat_records`` rows the ROI chat card renders (#10696).

Started by ``lab.py`` (``seed_roi_chat_records``) with the backend's interpreter, from ``backend/``
and with the backend's environment, once the backend answers: a ``roi_chat_records`` row is written
by the platform when an agent saves a deck, a report, a video or a document in a conversation
(``services/roi_chat_records.py``) and no API creates one, so this writes them through the
platform's models. Arguments: a JSON file of the ids the lines are attributed to (``admin``,
``peer``, ``long_user``, ``review_agent``, ``fleet_agent``, ``long_agent``) and the JSON file the
seeded ids are written to.

:func:`plan` (pure, times relative to *now*) covers every state the card and its Adjust dialog
show, all inside the default 30-day window:

* every kind (presentation, report, video, file);
* counted lines (a use signal, or settled :data:`roi_chat.SETTLE_DAYS` after the last edit) and
  lines waiting for use;
* corrected lines (``adjusted_min`` set, so the dialog offers Back to the estimate): one at the
  correction ceiling of 720 hours and one at 360 hours, so the totals pass 1,000 hours and every
  figure is compact (a single line cannot: corrections stop at 720 hours, estimates at 24);
* a title over 100 characters, an agent and a person named over 40 characters;
* lines of a member of "Lab sample fleet" (the fleet scope narrows to them), of the ROI review
  agent, and lines owned by the lab peer (the Mine scope of a non-administrator).

Every line carries its agent's home fleet as the platform's writer stamps it at the first write
(``fleet_runs.home_fleet_defaults_of``, #12077), ``fleet_backfilled`` false: the trace maintenance
that backfills older rows does not run in the lab, so an unstamped line would never reach the fleet
scope (dev:harness-gaps lab-seed-roi-chat-records-fleet-stamp).

No model call, no conversation: ``conversation_id`` stays NULL, so the measured time is 0.
dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: The ids a line is attributed to, as the input JSON names them.
ROLES = ("admin", "peer", "long_user", "review_agent", "fleet_agent", "long_agent")

LONG_TITLE = (
    "Quarterly threat landscape briefing for the executive committee: ransomware groups, "
    "initial access brokers and the sector exposure of the last ninety days"
)

#: (label, kind, size, signals, hours since the last edit, user role, agent role, adjusted minutes, title).
LINES: tuple[tuple[str, str, float, tuple[str, ...], float, str, str, float | None, str], ...] = (
    ("deck-presented", "presentation", 14, ("presented",), 30, "admin", "review_agent", None, LONG_TITLE),
    ("report-corrected", "report", 12, ("exported", "shared"), 50, "admin", "fleet_agent", 43200.0,
     "Consolidated vulnerability report for the board"),
    ("video-public-link", "video", 3, ("public_link",), 20, "peer", "fleet_agent", None,
     "Two-minute explainer on the phishing campaign"),
    ("brief-settled", "file", 2, (), 9 * 24, "long_user", "long_agent", None, "Decision brief: renew the EDR contract"),
    ("deck-waiting", "presentation", 8, (), 24, "peer", "review_agent", None, "Customer kickoff deck, draft"),
    ("report-peer-corrected", "report", 10, ("sent",), 70, "peer", "fleet_agent", 21600.0,
     "Incident post-mortem report for the regulator"),
    ("file-waiting", "file", 1, (), 2, "admin", "review_agent", None, "One-page note on the patch backlog"),
    ("video-attached", "video", 2, ("attached",), 5, "admin", "review_agent", None, "Product walkthrough recording"),
)


def plan(now: datetime, ids: dict[str, str], fleets: dict[str, str] | None = None) -> list[dict]:
    """The ``roi_chat_records`` columns of every seeded line, oldest edit first, times relative to *now*.

    *fleets* maps an agent id to its home fleet id; an agent with none leaves ``fleet_id`` NULL.
    """
    fleets = fleets or {}
    rows = []
    for label, kind, size, signals, hours, user, agent, adjusted, title in sorted(LINES, key=lambda line: -line[4]):
        edited = now - timedelta(hours=hours)
        deliverable = str(uuid.uuid5(uuid.NAMESPACE_URL, f"xtm-one-lab:roi-chat:{label}:{now.isoformat()}"))
        rows.append({
            "label": label,
            "record": {
                "record_key": f"{kind}:{deliverable}", "kind": kind, "deliverable_id": deliverable,
                "user_id": ids[user], "agent_id": ids[agent], "fleet_id": fleets.get(ids[agent]),
                "fleet_backfilled": False, "conversation_id": None, "title": title,
                "size": float(size), "signals": list(signals),
                "first_signal_at": edited + timedelta(minutes=30) if signals else None,
                "last_edit_at": edited, "created_at": edited - timedelta(hours=1),
                "adjusted_min": adjusted,
                "adjusted_by_user_id": ids["admin"] if adjusted is not None else None,
                "adjusted_at": edited + timedelta(hours=1) if adjusted is not None else None,
            },
        })
    return rows


async def seed(ids: dict[str, str]) -> dict[str, object]:
    """Write the lines stamped with their agents' home fleets; answer their ids by label and the stamped count."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.roi import RoiChatRecord
    from app.services.fleet_runs import home_fleet_defaults_of

    def _uuid(value: object) -> object:
        return uuid.UUID(value) if isinstance(value, str) else value

    keyed = {"user_id", "agent_id", "fleet_id", "adjusted_by_user_id"}
    written: dict[str, str] = {}
    async with async_session() as db:
        homes = await home_fleet_defaults_of(db, ids.values())
        fleets = {str(agent): str(defaults.fleet_id) for agent, defaults in homes.items()}
        rows = []
        planned = plan(datetime.now(UTC), ids, fleets)
        for row in planned:
            record = RoiChatRecord(**{k: _uuid(v) if k in keyed else v for k, v in row["record"].items()})
            db.add(record)
            rows.append((row["label"], record))
        await db.commit()
        written = {label: str(record.id) for label, record in rows}
    await engine.dispose()
    return {"records": written, "fleet_stamped": sum(1 for row in planned if row["record"]["fleet_id"])}


def main() -> None:
    ids_file, out = sys.argv[1:3]
    ids = json.loads(Path(ids_file).read_text())
    missing = [role for role in ROLES if not ids.get(role)]
    if missing:
        raise SystemExit(f"missing ids: {', '.join(missing)}")
    Path(out).write_text(json.dumps(asyncio.run(seed(ids))))


if __name__ == "__main__":
    main()
