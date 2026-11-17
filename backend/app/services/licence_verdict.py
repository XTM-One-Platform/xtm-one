# SPDX-License-Identifier: Apache-2.0

"""The licensed verdict the web process shares with the worker.

``ENTERPRISE_LICENSE`` is read by every process from its own environment. A
deployment that gave it to the platform container only (the web process, where
the UI reads it) and not to the worker ran every background job anyway before
the background gate: nothing on the worker asked the licence. The gate
asks it now, so such a worker resolves Community Edition on its own and would
hold the assignment runs, delegated tasks, evaluations and document indexing of
a licensed deployment.

So the web process, whose licence answer is yes while it holds
``ENTERPRISE_LICENSE``, publishes a short-lived verdict in Redis, and the
worker, whose own answer is no, accepts it until it expires and says so once in
its log. The verdict is "licensed until <time>", signed with a key derived from
``SECRET_KEY``: every process of a deployment holds that secret (the database
secrets are encrypted with it), so a Redis write alone cannot forge a verdict,
and no license material leaves the process that holds it. A deployment whose
worker resolves the license itself never reads one.

Only the web process publishes (``share_licensed_verdicts``, from its lifespan)
and only the worker accepts (``accept_licensed_verdicts``, from its startup),
so every other caller, the test suite included, never touches Redis here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time

from app.core.config import settings
from app.core.security import timing_safe_str_eq

logger = logging.getLogger(__name__)

VERDICT_KEY = "xtm:licence:peer_verdict"
# How long a published verdict holds. The web process republishes it every
# ``PUBLISH_INTERVAL_SECONDS`` while it stays licensed, so a worker keeps it
# through a web restart, and a license removed from the web stops counting on
# the worker within this delay.
VERDICT_TTL_SECONDS = 15 * 60
PUBLISH_INTERVAL_SECONDS = 60
# The Redis client retries a refused connection for seconds; the licence read
# must not wait that long when Redis is down.
REDIS_TIMEOUT_SECONDS = 1.0

_SIGNING_CONTEXT = b"xtm-one/licence-peer-verdict/v1"

_publishing = False
_accepting = False
_last_publish_attempt: float | None = None
_warned = False


def share_licensed_verdicts() -> None:
    """Publish this process's licensed answer (the web process, at startup)."""
    global _publishing
    _publishing = True


def accept_licensed_verdicts() -> None:
    """Accept the web process's licensed answer (the worker, at startup)."""
    global _accepting
    _accepting = True


def _signature(until: int) -> str:
    key = hmac.new(settings.secret_key.encode(), _SIGNING_CONTEXT, hashlib.sha256).digest()
    return hmac.new(key, str(until).encode(), hashlib.sha256).hexdigest()


async def publish_licensed_verdict() -> None:
    """Share that this process is licensed, when its licence is ``ENTERPRISE_LICENSE``.

    Called after a licence read answered yes. A license installed in Settings
    or an Enterprise Edition platform is read by every process from the
    database, so only the environment variable needs sharing. Best effort and
    at most once per ``PUBLISH_INTERVAL_SECONDS``: a verdict outlives a missed
    publication by far.
    """
    global _last_publish_attempt
    if not _publishing or not (settings.enterprise_license or "").strip():
        return
    now = time.monotonic()
    if _last_publish_attempt is not None and now - _last_publish_attempt < PUBLISH_INTERVAL_SECONDS:
        return
    _last_publish_attempt = now
    until = int(time.time()) + VERDICT_TTL_SECONDS
    value = json.dumps({"until": until, "sig": _signature(until)})
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await asyncio.wait_for(client.set(VERDICT_KEY, value, ex=VERDICT_TTL_SECONDS), REDIS_TIMEOUT_SECONDS)
    except Exception:
        logger.debug("Could not publish the licensed verdict for the worker", exc_info=True)


async def peer_licensed_verdict() -> bool:
    """Whether the web process published a licensed verdict that still holds.

    Only a worker asks, after its own licence read answered no. Never raises:
    an unreadable, expired or unsigned verdict is no verdict.
    """
    global _warned
    if not _accepting:
        return False
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        raw = await asyncio.wait_for(client.get(VERDICT_KEY), REDIS_TIMEOUT_SECONDS)
    except Exception:
        logger.debug("Could not read the licensed verdict of the web process", exc_info=True)
        return False
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return False
    if not isinstance(data, dict):
        return False
    until = data.get("until")
    signature = data.get("sig")
    # Only the shape ``publish_licensed_verdict`` writes: an integer and a
    # string. A float (``1e1000`` reads as infinity) has no place in a timestamp.
    if type(until) is not int or not isinstance(signature, str):
        return False
    if until <= time.time() or not timing_safe_str_eq(signature, _signature(until)):
        return False
    if not _warned:
        _warned = True
        logger.warning(
            "This worker resolves no valid Filigran license of its own (ENTERPRISE_LICENSE is not set "
            "here or does not validate, and none is installed in Settings), but the platform "
            "process holds one in ENTERPRISE_LICENSE: background AI runs on its verdict. Set "
            "ENTERPRISE_LICENSE on the worker as well, or install the license in Settings > "
            "Platform."
        )
    return True
