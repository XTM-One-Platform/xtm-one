"""The lab's documented ``manual`` About prerequisites, in each confirmation state (#10509).

Started by ``lab.py`` (``seed_fleet_manual_prerequisites``) with the backend's interpreter, from
``backend/`` and with the backend's environment. No lab fleet documents a ``manual`` line that no
configuration subject covers, so the To confirm, confirmed (Undo) and expired states of About >
Prerequisites (#10469) never rendered on real rows. ``live_prerequisites`` keeps at most
``MANUAL_LINES_KEPT`` manual lines per fleet, so the states are spread over two fleets:

* the first: one line confirmed a few days ago by the lab administrator, one never confirmed;
* the second: one line confirmed more than ``CONFIRMATION_DAYS`` ago (expired), one never confirmed.

Written through the product's own writers: ``fleet_docs.save_documentation`` (version, history,
fingerprint) and ``fleet_about.set_prerequisite_confirmation`` with a past ``now``. Arguments: the
author (the lab administrator), the two fleet ids, and the JSON file the answer
(``{fleet id: {"version": N, "confirmed": [checks]}}``) is written to. No model call.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: Days before the run of the current confirmation, and of the expired one (past CONFIRMATION_DAYS).
CONFIRMED_DAYS_AGO = 3
EXPIRED_DAYS_AGO = 120


def manual_lines(fleet_index: int) -> list[dict[str, str]]:
    """The two documented manual lines of fleet *fleet_index* (0 or 1), naming nothing a system reads."""
    if fleet_index == 0:
        return [
            {"check": "manual", "label": "The security team signed off the escalation runbook"},
            {"check": "manual", "label": "Legal approved sharing the weekly summary outside the company"},
        ]
    return [
        {"check": "manual", "label": "The on-call rota names a reviewer for every weekend"},
        {"check": "manual", "label": "Customers were told the replies are drafted with AI"},
    ]


#: Per fleet: how many days ago its first manual line was confirmed (the second stays unconfirmed).
CONFIRMATIONS = (CONFIRMED_DAYS_AGO, EXPIRED_DAYS_AGO)

SECTIONS = {
    "purpose": "Seeded by the lab: a fleet whose About page lists manual prerequisites a fleet manager confirms.",
}


async def seed(author: str, fleet_ids: list[str]) -> dict:
    """Write the documentation and the confirmations on each fleet; answer what was stored."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.fleet import Fleet
    from app.models.user import User
    from app.services.fleet_about import MANUAL_PREFIX, manual_key, set_prerequisite_confirmation
    from app.services.fleet_docs import save_documentation

    answer: dict[str, dict] = {}
    now = datetime.now(UTC)
    async with async_session() as db:
        user = await db.get(User, uuid.UUID(author))
        if user is None:
            raise RuntimeError(f"no user {author}")
        for index, fleet_id in enumerate(fleet_ids):
            fleet = await db.get(Fleet, uuid.UUID(fleet_id))
            if fleet is None:
                raise RuntimeError(f"no fleet {fleet_id}")
            lines = manual_lines(index)
            await save_documentation(
                db, fleet, dict(SECTIONS), author=user.id, base_version=None, structured={"prerequisites": lines}
            )
            await db.flush()
            check = f"{MANUAL_PREFIX}{manual_key(lines[0]['label'])}"
            when = now - timedelta(days=CONFIRMATIONS[index])
            await set_prerequisite_confirmation(db, user, fleet, check, confirmed=True, now=when)
            answer[fleet_id] = {"version": int(fleet.documentation["version"]), "confirmed": [check]}
        await db.commit()
    await engine.dispose()
    return answer


def main() -> None:
    author, first, second, out = sys.argv[1:5]
    Path(out).write_text(json.dumps(asyncio.run(seed(author, [first, second]))))


if __name__ == "__main__":
    main()
