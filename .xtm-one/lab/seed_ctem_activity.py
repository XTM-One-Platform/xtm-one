"""The lab's platform timeline of ``LAB_CTEM_ACTIVITY=on``: registrations backdated, one heartbeat stopped.

Started by ``lab.py`` (``seed_ctem_activity``) with the backend's interpreter, from ``backend/`` and with
the backend's environment. ``POST /platform/register`` always stamps ``registered_at`` and the heartbeat now
and no API writes an older one, so this writes both through the platform's model, from
``ctem_activity_timeline.json`` (the one timeline ``lab_asgi`` and the feed's vitest test read too):
``registered_at`` of each registration in ``registered``, and ``last_heartbeat_at`` of the silent platform
(``heartbeat``), past ``HEARTBEAT_HEALTHY_WINDOW`` (15 min) so ``PlatformRegistration.is_healthy`` answers false
and ``GET /platform/registrations`` lists it with ``healthy`` false ("stopped responding"). With the three
registrations stamped at the lab start, they filled the newest rows of the Live activity card and pushed the
validation and stopped-responding rows out (#9627).
Arguments: the ``platform_identifier`` left silent, and the JSON file the written ids are written to.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: Minutes before now of each source of the feed: ``product_sync``, ``heartbeat`` and ``registered``.
TIMELINE: dict[str, dict[str, int]] = json.loads((Path(__file__).resolve().parent / "ctem_activity_timeline.json").read_text())


def heartbeat(now: datetime, identifier: str = "opencrq") -> datetime:
    """The backdated heartbeat of the silent platform *identifier* for *now*."""
    return now - timedelta(minutes=TIMELINE["heartbeat"][identifier])


def registered(now: datetime, identifier: str) -> datetime | None:
    """The backdated ``registered_at`` of *identifier* for *now*; ``None`` when the timeline names none."""
    minutes = TIMELINE["registered"].get(identifier)
    return None if minutes is None else now - timedelta(minutes=minutes)


async def seed(silent: str) -> list[str]:
    """Backdate every registration of the timeline and the heartbeat of *silent*; answer the ids written."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from sqlalchemy import select

    from app.core.database import async_session, engine
    from app.models.platform_registration import PlatformRegistration

    now = datetime.now(UTC)
    written: list[str] = []
    async with async_session() as db:
        rows = (await db.execute(select(PlatformRegistration))).scalars().all()
        by_identifier = {r.platform_identifier: r for r in rows}
        if silent not in by_identifier:
            raise RuntimeError(f"no registration of {silent}")
        for identifier, reg in by_identifier.items():
            at = registered(now, identifier)
            if at is not None:
                reg.registered_at = at
                written.append(str(reg.id))
        by_identifier[silent].last_heartbeat_at = heartbeat(now, silent)
        if str(by_identifier[silent].id) not in written:
            written.append(str(by_identifier[silent].id))
        await db.commit()
    await engine.dispose()
    return written


def main() -> None:
    identifier, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(identifier))))


if __name__ == "__main__":
    main()
