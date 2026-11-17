"""The lab's AI Adoption at scale: figures of 1,000 and more, under ``LAB_AI_ADOPTION_SCALE=large``.

Started by ``lab.py`` (``seed_ai_adoption_large``) with the backend's interpreter, from ``backend/``
and with the backend's environment. Every other AI Adoption seed of the lab stays under 1,000 (a
few dozen users), so the K compaction of the KPI values, subtitles and the Adoption Breakdown
card, and the exact value on hover, rendered only through a ``window.fetch`` override. This
writes, through the backend's own models and in the collector's shapes
(``app/services/ai_metrics_collector.py``):

* ``POOL_USERS`` active users ``lab-scale-NNNNN@filigran.io`` with no password (nobody signs in as
  them): the registered users and the Gaps pool go over 5,000;
* one ``copilot_platform`` ``platform_daily`` usage row for each of the first ``AGENTIC_USERS``,
  activity score at or over ``ACTIVITY_THRESHOLD`` (10): active users over 1,000;
* one personal API key for each of the first ``API_KEY_USERS``, with a request count in the
  thousands, so the API Calls KPI (the top ten keys' requests) reads in K. A key stores a hash
  that is not a bcrypt hash and a ``fcp-lab-`` prefix no generated key has (hex only): no token
  ever authenticates as one of them.

Argument: the JSON file the counts are written to. Idempotent: a restarted lab finds the users by
email, the metric rows by day, provider, type and user, the keys by name, and writes nothing twice.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

#: The pool users' email pattern (``POOL_EMAIL.format(n)``) and their LIKE pattern.
POOL_EMAIL = "lab-scale-{:05d}@filigran.io"
POOL_LIKE = "lab-scale-%@filigran.io"
#: The pool size, the Agentic AI active users and the API key users.
POOL_USERS = 5200
AGENTIC_USERS = 1240
API_KEY_USERS = 1050
#: Days back the per-user rows sit, inside the page's 30-day default.
DAYS_BACK = 3
#: The API keys' name and display prefix; the hash is not a bcrypt hash, so no token matches it.
KEY_NAME = "Lab scale key"
KEY_PREFIX = "fcp-lab-..."
KEY_HASH = "lab-only-not-a-bcrypt-hash"


def pool_email(n: int) -> str:
    return POOL_EMAIL.format(n + 1)


def platform_usage(n: int) -> dict[str, object]:
    """A platform ``platform_daily`` row whose activity score is 12 to 40 (over the threshold)."""
    return {"source": "platform_daily", "conversations": 2 + n % 5, "messages": 10 + n % 24, "assignment_runs": n % 3}



def key_requests(n: int) -> int:
    """A key's request count: 1,200 to 9,700."""
    return 1200 + (n * 37) % 8500


async def seed() -> dict[str, int]:
    """Write every planned row (each once); answer the counts."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from sqlalchemy import func, insert, select

    from app.core.database import async_session, engine
    from app.models.ai_adoption_metric import AiAdoptionMetric
    from app.models.api_key import UserApiKey
    from app.models.user import User

    now = datetime.now(UTC)
    today = date.today()
    day = today - timedelta(days=DAYS_BACK)
    async with async_session() as db:
        existing = {
            email.lower()
            for (email,) in (await db.execute(select(User.email).where(User.email.like(POOL_LIKE)))).all()
        }
        missing = [
            {"email": pool_email(n), "full_name": f"Lab scale user {n + 1:05d}", "role": "user", "is_active": True}
            for n in range(POOL_USERS)
            if pool_email(n) not in existing
        ]
        if missing:
            await db.execute(insert(User), missing)
            await db.flush()
        ids = {
            email.lower(): uid
            for uid, email in (await db.execute(select(User.id, User.email).where(User.email.like(POOL_LIKE)))).all()
        }
        users = [ids[pool_email(n)] for n in range(POOL_USERS)]

        rows: list[dict[str, object]] = []
        for provider, count, usage in (("copilot_platform", AGENTIC_USERS, platform_usage),):
            have = set(
                (
                    await db.execute(
                        select(AiAdoptionMetric.user_id).where(
                            AiAdoptionMetric.date == day,
                            AiAdoptionMetric.provider == provider,
                            AiAdoptionMetric.metric_type == "usage",
                            AiAdoptionMetric.user_id.in_(users[:count]),
                        )
                    )
                ).scalars()
            )
            rows += [
                {"date": day, "provider": provider, "user_id": users[n], "user_email": None,
                 "metric_type": "usage", "data": usage(n), "created_at": now}
                for n in range(count)
                if users[n] not in have
            ]
        if rows:
            await db.execute(insert(AiAdoptionMetric), rows)

        keyed = set(
            (
                await db.execute(
                    select(UserApiKey.user_id).where(
                        UserApiKey.name == KEY_NAME, UserApiKey.user_id.in_(users[:API_KEY_USERS])
                    )
                )
            ).scalars()
        )
        keys = [
            {"user_id": users[n], "name": KEY_NAME, "key_prefix": KEY_PREFIX, "key_hash": KEY_HASH,
             "is_active": True, "request_count": key_requests(n), "last_used_at": now - timedelta(days=1),
             "created_at": now - timedelta(days=20)}
            for n in range(API_KEY_USERS)
            if users[n] not in keyed
        ]
        if keys:
            await db.execute(insert(UserApiKey), keys)
        await db.commit()
        pool = (await db.execute(select(func.count(User.id)).where(User.email.like(POOL_LIKE)))).scalar_one()
    await engine.dispose()
    return {
        "pool_users": pool,
        "agentic_users": AGENTIC_USERS,
        "api_key_users": API_KEY_USERS,
        "new_rows": len(missing) + len(rows) + len(keys),
    }


def main() -> None:
    (out,) = sys.argv[1:2]
    Path(out).write_text(json.dumps(asyncio.run(seed())))


if __name__ == "__main__":
    main()
