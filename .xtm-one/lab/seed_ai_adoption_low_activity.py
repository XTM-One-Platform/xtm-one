"""The lab's AI Adoption > Gaps > Lowest Activity: four users active below the threshold.

Started by ``lab.py`` (``seed_ai_adoption_low_activity``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once the four users exist (created through the
API). Without it ``GET /api/v1/ai-adoption/gaps`` answers ``low_activity_count`` 0: every lab
user is either inactive (score 0) or active, so the Lowest Activity rows (``low_activity``)
rendered only through a ``window.fetch`` override. This writes, in the collector's shape
(``app/services/ai_metrics_collector.py``), for each user one platform ``usage`` row
(``copilot_platform``, ``source`` ``platform_daily``) with 1, 3, 5 or 7 ``messages``, under
``ACTIVITY_THRESHOLD`` (10).

The rows are dated two days back, inside the page's 30-day default. Argument: the JSON file the
count is written to. Idempotent: a restarted lab finds every row by day, provider, type and user
and writes nothing twice.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

#: The low-activity users ``lab.py`` creates, in this order: (email, full name); one long of each.
LOW_ACTIVITY_USERS: tuple[tuple[str, str], ...] = (
    ("lab-low-activity-01@filigran.io", "Lab low-activity user 01"),
    ("lab-low-activity-02@filigran.io", "Lab low-activity user 02"),
    ("lab-low-activity-03@filigran.io", "Lab low-activity user 03"),
    (
        "lab.low.activity.long.name@ai-adoption-lowest-activity-truncation-check.lab.filigran.io",
        "Lab low-activity user with a deliberately long display name to check row truncation in Gaps",
    ),
)

#: Days back the rows sit.
DAYS_BACK = 2


def platform_usage(n: int) -> dict[str, object]:
    """A platform ``platform_daily`` row whose activity score is ``2 * n + 1``."""
    return {"source": "platform_daily", "messages": 2 * n + 1}


async def seed() -> dict[str, int]:
    """Write every planned row (each once); answer the users and rows counts."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from sqlalchemy import func, select

    from app.core.database import async_session, engine
    from app.models.ai_adoption_metric import AiAdoptionMetric
    from app.models.user import User

    day = date.today() - timedelta(days=DAYS_BACK)
    rows = 0
    async with async_session() as db:
        for n, (email, _name) in enumerate(LOW_ACTIVITY_USERS):
            user_id = (
                await db.execute(select(User.id).where(func.lower(User.email) == email))
            ).scalar_one_or_none()
            if user_id is None:
                raise RuntimeError(f"no lab user {email}: lab.py did not create it")
            for provider, data in (("copilot_platform", platform_usage(n)),):
                found = (
                    await db.execute(
                        select(AiAdoptionMetric.id).where(
                            AiAdoptionMetric.date == day,
                            AiAdoptionMetric.provider == provider,
                            AiAdoptionMetric.metric_type == "usage",
                            AiAdoptionMetric.user_id == user_id,
                        )
                    )
                ).scalars().first()
                if found is None:
                    db.add(
                        AiAdoptionMetric(
                            date=day,
                            provider=provider,
                            user_id=user_id,
                            user_email=None,
                            metric_type="usage",
                            data=data,
                            created_at=datetime.now(UTC),
                        )
                    )
                rows += 1
        await db.commit()
    await engine.dispose()
    return {"users": len(LOW_ACTIVITY_USERS), "rows": rows}


def main() -> None:
    (out,) = sys.argv[1:2]
    Path(out).write_text(json.dumps(asyncio.run(seed())))


if __name__ == "__main__":
    main()
