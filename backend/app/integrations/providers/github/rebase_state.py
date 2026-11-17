"""What one ``rebase_github_branch`` rebase remembers between its calls.

A rebase spans several calls: one per conflicting commit, one per
``max_commits`` budget, one after a worker restart. Each call used to start
from its own arguments only, so a caller had to resend every resolution of
every earlier commit, a call naming ``onto: main`` after main moved began a new
work branch from zero, and a commit whose resolution left nothing to write was
looked at again on the next call (#11802). The state of one rebase, keyed by
the repository, the branch and ``expected_head`` (the head the rebase moves
from), holds:

- ``onto_ref`` and ``onto_commit``: the ref the first call named and the commit
  it pointed at; a later call naming the same ref replays onto that commit, so
  the work branch and its replayed commits are kept;
- ``resolutions``: every resolution passed so far, a later one for the same
  commit and path replacing the earlier;
- ``settled``: the commits that wrote nothing once applied (their change is
  already on the work tip), never applied again.

It lives in Redis for :data:`TTL_SECONDS` and is dropped once the branch is
moved. Redis unreachable, the call goes on with its own arguments, as before.
"""

from __future__ import annotations

import json
import logging
from typing import Any, NamedTuple

logger = logging.getLogger(__name__)

#: How long an unfinished rebase is remembered.
TTL_SECONDS = 7 * 24 * 3600
_KEY = "github:rebase:{repo}:{branch}:{head}"


class RebaseState(NamedTuple):
    onto_ref: str
    onto_commit: str
    #: ``[[commit prefix, path, keep, content], ...]``
    resolutions: list[list[Any]]
    settled: list[str]


def state_key(repo: str, branch: str, expected_head: str) -> str:
    return _KEY.format(repo=repo.lower(), branch=branch, head=expected_head)


def _decode(raw: object) -> RebaseState | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(doc, dict):
        return None
    resolutions = [r for r in doc.get("resolutions") or [] if isinstance(r, list) and len(r) == 4]
    settled = [s for s in doc.get("settled") or [] if isinstance(s, str)]
    return RebaseState(str(doc.get("onto_ref") or ""), str(doc.get("onto_commit") or ""), resolutions, settled)


async def load(key: str) -> RebaseState | None:
    """The stored state at *key*, ``None`` when there is none or Redis cannot be read."""
    from app.core.redis import redis_client

    try:
        client = await redis_client()
        return _decode(await client.get(key))
    except Exception:
        logger.warning("rebase state %s could not be read; this call uses its own arguments", key, exc_info=True)
        return None


async def save(key: str, state: RebaseState) -> bool:
    """Store *state* at *key*; ``False`` when Redis refused it."""
    from app.core.redis import redis_client

    payload = json.dumps(state._asdict())
    try:
        client = await redis_client()
        await client.set(key, payload, ex=TTL_SECONDS)
        return True
    except Exception:
        logger.warning("rebase state %s could not be written", key, exc_info=True)
        return False


async def drop(key: str) -> None:
    from app.core.redis import redis_client

    try:
        client = await redis_client()
        await client.delete(key)
    except Exception:
        logger.warning("rebase state %s could not be dropped", key, exc_info=True)


def merge_resolutions(stored: list[list[Any]], new: dict[tuple[str, str], Any]) -> list[list[Any]]:
    """*stored* with the resolutions of this call (``{(commit, path): Resolution}``) replacing any for the same pair.

    A stored pair whose commit prefix and path match a new one by prefix (``abc1234`` and ``abc1234def``) is the
    same resolution: the new one wins.
    """
    kept = [
        entry
        for entry in stored
        if not any(
            entry[1] == path and (str(entry[0]).startswith(commit) or commit.startswith(str(entry[0])))
            for commit, path in new
        )
    ]
    kept.extend([commit, path, resolution.keep, resolution.content] for (commit, path), resolution in new.items())
    return kept
