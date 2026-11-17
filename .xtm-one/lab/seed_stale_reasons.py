"""The lab's stale work items: one per ``stale_reason``, so the stale view renders every reason on real rows.

Started by ``lab.py`` (``seed_stale_reasons``) with the backend's interpreter, from ``backend/`` and
with the backend's environment, once the backend answers (#9905). Through the public API a lab can
only produce ``past_due``: ``wait_never_releases`` needs a deployment reference on a repository the
platform does not observe (``refuse_unobserved_deployment_refs`` refuses it at write time), and
``ref_wait_quiet`` and ``quiet`` need a ``last_activity_at`` older than the configured windows. This
writes, through the ``WorkItem`` model, four items of the administrator with no owner agent (so no
inbox dispatches them), idempotent by correlation key:

* ``past_due``: open, its due date :data:`MARGIN` ago;
* ``wait_never_releases``: blocked on the deployment of :data:`UNOBSERVED_REPO`, which
  ``work_item_deployment_repositories`` never names;
* ``ref_wait_quiet``: blocked on a pull request and quiet past ``work_item_stale_ref_wait_days``
  (and ``work_item_stale_after_hours``, the longer of the two) by :data:`MARGIN`;
* ``quiet``: blocked with a reason only, quiet past ``work_item_stale_after_hours`` by :data:`MARGIN`.

The ages are read from the live windows (``work_item._stale_after``, ``work_item._stale_ref_wait``),
so a lab that tunes them still gets every reason. Arguments: the administrator's user id and the
JSON file the ids are written to, ``{reason: id}``.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: The reasons ``work_item.work_item_stale_reason`` answers, in the order the seed writes them.
REASONS = ("past_due", "wait_never_releases", "ref_wait_quiet", "quiet")
#: The correlation key prefix of the rows this seed writes.
KEY_PREFIX = "lab-stale-reasons"
#: How far past its window each aged row is.
MARGIN = timedelta(days=1)
#: A repository no lab build observes deployments of (``observed_deployment_repositories``).
UNOBSERVED_REPO = "lab-sample/unobserved-repository"
#: The pull request the quiet reference wait names (a repository no code host delivers events for).
WATCHED_REPO = "lab-sample/watched-repository"


def plan(
    now: datetime,
    stale_after: timedelta | None,
    ref_wait: timedelta | None,
    observed: frozenset[str],
) -> dict[str, dict]:
    """The fields of the item of each reason, as of *now*.

    *stale_after* and *ref_wait* are the live windows (``None`` when disabled); *observed* the
    repositories whose deployments the platform observes. A disabled inactivity window leaves
    ``ref_wait_quiet`` and ``quiet`` unreachable, and an observed :data:`UNOBSERVED_REPO` leaves
    ``wait_never_releases`` unreachable: both raise, the lab names the cause.
    """
    if stale_after is None:
        raise RuntimeError("work_item_stale_after_hours <= 0: no item can be stale on inactivity")
    if UNOBSERVED_REPO.lower() in observed:
        raise RuntimeError(f"{UNOBSERVED_REPO} is in work_item_deployment_repositories")
    quiet_since = now - stale_after - MARGIN
    ref_since = now - max(stale_after, ref_wait or stale_after) - MARGIN
    common = {"status": "blocked", "due_at": None, "not_before": None, "blocked_on": None}
    return {
        "past_due": {
            **common,
            "status": "open",
            "title": "Seeded by the lab: past due",
            "due_at": now - MARGIN,
            "blocked_on_refs": None,
            "blocked_reason": None,
            "last_activity_at": now,
        },
        "wait_never_releases": {
            **common,
            "title": "Seeded by the lab: waits on a deployment nobody observes",
            "blocked_on_refs": [{"repo": UNOBSERVED_REPO, "kind": "deployment", "number": 1, "release_on": "deployed"}],
            "blocked_reason": "Seeded by the lab: the deployment of a repository this platform does not observe.",
            "last_activity_at": now,
        },
        "ref_wait_quiet": {
            **common,
            "title": "Seeded by the lab: waits on a pull request, quiet for a week",
            "blocked_on_refs": [
                {"repo": WATCHED_REPO, "kind": "pull_request", "number": 1, "release_on": "merged_or_closed"}
            ],
            "blocked_reason": "Seeded by the lab: a pull request nobody merged.",
            "last_activity_at": ref_since,
        },
        "quiet": {
            **common,
            "title": "Seeded by the lab: blocked and quiet for two days",
            "blocked_on_refs": None,
            "blocked_reason": "Seeded by the lab: waits on a person, nobody moved it.",
            "last_activity_at": quiet_since,
        },
    }


async def seed(admin_id: str) -> dict[str, str]:
    """Write the four items; answer ``{reason: id}``."""
    from sqlalchemy import select

    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from app.core.database import async_session, engine
    from app.models.work_item import WorkItem
    from app.services.work_item import _stale_after, _stale_ref_wait
    from app.services.work_item_blocked_refs import observed_deployment_repositories

    admin = uuid.UUID(admin_id)
    now = datetime.now(UTC)
    rows = plan(now, _stale_after(), _stale_ref_wait(), observed_deployment_repositories())
    answer: dict[str, str] = {}
    async with async_session() as db:
        items = {}
        for reason in REASONS:
            key = f"{KEY_PREFIX}:{reason}"
            item = (await db.execute(select(WorkItem).where(WorkItem.correlation_key == key))).scalars().first()
            if item is None:
                item = WorkItem(
                    user_id=admin,
                    correlation_key=key,
                    title=rows[reason]["title"],
                    description=f"The {reason} reason of the stale work items view.",
                    source="agent",
                )
                db.add(item)
            item.owner_agent_id = None
            item.claimed_by_agent_id = None
            item.claimed_by_run_id = None
            item.claimed_at = None
            for field, value in rows[reason].items():
                setattr(item, field, value)
            item.created_at = min(rows[reason]["last_activity_at"], now - MARGIN)
            items[reason] = item
        await db.commit()
        answer = {reason: str(item.id) for reason, item in items.items()}
    await engine.dispose()
    return answer


def main() -> None:
    admin_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(admin_id))))


if __name__ == "__main__":
    main()
