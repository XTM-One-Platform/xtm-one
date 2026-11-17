# SPDX-License-Identifier: Apache-2.0

"""Park a pull request whose full CI is red on its base, run it again at the base's green.

The merge gate (GitHub: ``full_ci_gate.gate``) parks a pull request whose
required full run failed on the jobs the base branch's own latest run fails on
(:func:`park`). A signed CI delivery of that workflow completing green on the
base branch (:func:`on_base_green`, from the webhook receiver) hands every pull
request parked on it to the provider's redispatcher
(``integrations/full_ci_redispatch.py``), with the token of the person who
parked it, so the run starts again within the delivery's seconds and nobody
has to notice main turned green.

The list lives in Redis, one hash per ``(provider, repo, base, workflow)``
(field: the pull request number), for :data:`PARK_TTL_SECONDS`: a pull request
nobody merged in that time is not waited for any more. Best effort throughout:
a Redis or provider failure is logged, never raised into a tool or a delivery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: How long a parked pull request is waited for.
PARK_TTL_SECONDS = 2 * 24 * 3600

#: Pull requests re-dispatched per base green, the newest parked first.
MAX_REDISPATCH = 20

_PREFIX = "full_ci:parked"

#: The re-dispatches in flight, held so the loop does not collect them.
_TASKS: set[asyncio.Task[None]] = set()


def _key(provider: str, repo: str, base: str, workflow: str) -> str:
    return f"{_PREFIX}:{provider}:{repo.lower()}:{base}:{workflow}"


async def _redis() -> Any | None:
    try:
        from app.core.redis import redis_client

        return await redis_client()
    except Exception as exc:
        logger.warning("Full CI parking unavailable (Redis): %s", format_exception_message(exc))
        return None


async def park(provider: str, repo: str, base: str, workflow: str, number: int, head_sha: str, user_id: str) -> bool:
    """Park PR *number* of *repo* on *head_sha* until *workflow* is green on *base*; whether it was recorded."""
    if not (provider and repo and base and workflow and number and head_sha and user_id):
        return False
    redis = await _redis()
    if redis is None:
        return False
    key = _key(provider, repo, base, workflow)
    entry = json.dumps({"head_sha": head_sha, "user_id": str(user_id), "parked_at": int(time.time())})
    try:
        await redis.hset(key, str(number), entry)
        await redis.expire(key, PARK_TTL_SECONDS)
    except Exception as exc:
        logger.warning("Could not park %s#%s: %s", repo, number, format_exception_message(exc))
        return False
    return True


async def take_parked(provider: str, repo: str, base: str, workflow: str) -> list[tuple[int, dict[str, Any]]]:
    """Remove and answer the pull requests parked on ``(repo, base, workflow)``, the newest parked first."""
    redis = await _redis()
    if redis is None:
        return []
    key = _key(provider, repo, base, workflow)
    try:
        raw = await redis.hgetall(key)
        if raw:
            await redis.delete(key)
    except Exception as exc:
        logger.warning("Could not read the pull requests parked on %s: %s", key, format_exception_message(exc))
        return []
    out: list[tuple[int, dict[str, Any]]] = []
    for field, value in (raw or {}).items():
        try:
            number = int(field.decode() if isinstance(field, bytes) else field)
            entry = json.loads(value.decode() if isinstance(value, bytes) else value)
        except (ValueError, TypeError, AttributeError):
            continue
        if isinstance(entry, dict) and entry.get("head_sha") and entry.get("user_id"):
            out.append((number, entry))
    out.sort(key=lambda item: int(item[1].get("parked_at") or 0), reverse=True)
    return out[:MAX_REDISPATCH]


def base_green(event: Any) -> tuple[str, str] | None:
    """``(base branch, workflow file)`` when *event* is a gated workflow's green run on a base branch. Pure."""
    extra = getattr(event, "extra", None) or {}
    if (
        getattr(event, "kind", None) != "ci"
        or getattr(event, "status", None) != "completed"
        or getattr(event, "conclusion", None) != "success"
        or getattr(event, "pr_numbers", ())
        or not extra.get("base_run")
    ):
        return None
    branch = getattr(event, "head_branch", None)
    workflow = extra.get("workflow_file")
    if not branch or not isinstance(workflow, str) or not workflow:
        return None
    return branch, workflow


async def redispatch_parked(provider: str, repo: str, base: str, workflow: str) -> list[str]:
    """Run *workflow* again on every pull request parked on it; one line each."""
    from app.core.database import async_session
    from app.integrations.full_ci_redispatch import full_ci_redispatcher
    from app.services.work_item_blocked_refs import reference_token

    redispatcher = full_ci_redispatcher(provider)
    if redispatcher is None:
        return []
    parked = await take_parked(provider, repo, base, workflow)
    lines: list[str] = []
    tokens: dict[str, str | None] = {}
    for number, entry in parked:
        user_id = str(entry["user_id"])
        if user_id not in tokens:
            try:
                async with async_session() as db:
                    tokens[user_id] = await reference_token(db, provider, user_id)
            except Exception as exc:
                logger.warning("No %s token for a parked pull request: %s", provider, format_exception_message(exc))
                tokens[user_id] = None
        token = tokens[user_id]
        if not token:
            lines.append(f"{repo}#{number}: no {provider} credential of the person who parked it")
            continue
        try:
            lines.append(await redispatcher(repo, number, str(entry["head_sha"]), workflow, token))
        except Exception as exc:
            lines.append(f"{repo}#{number}: not re-dispatched ({format_exception_message(exc)})")
    for line in lines:
        logger.info("Full CI re-dispatch on %s green of %s/%s: %s", base, repo, workflow, line)
    return lines


def on_base_green(provider: str, event: Any) -> asyncio.Task[None] | None:
    """Start the re-dispatch of the pull requests parked on the base green *event* names, in the background.

    Never awaited by the delivery (the redispatcher waits for ``update-branch``);
    answers the task, ``None`` when *event* is not a base green.
    """
    target = base_green(event)
    if target is None:
        return None
    base, workflow = target

    async def _run() -> None:
        try:
            await redispatch_parked(provider, str(event.repo), base, workflow)
        except Exception as exc:
            logger.warning("Full CI re-dispatch failed: %s", format_exception_message(exc))

    task = asyncio.create_task(_run())
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return task
