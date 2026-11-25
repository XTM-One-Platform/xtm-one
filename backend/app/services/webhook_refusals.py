# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Refused webhook deliveries of an assignment, kept so its owner can read why (#2180).

A delivery the assignment's secret refuses (401) used to leave nothing on the
platform: the owner could not tell a wrong secret from no delivery at all.
The last :data:`MAX_REFUSALS` refusals of each assignment are kept in Redis for
:data:`REFUSALS_TTL_SECONDS`: the time, the reason the endpoint answered and
the NAMES of the authentication headers the request carried, never a header
value nor the body. ``get_assignment`` shows them. Best effort: Redis down
records and reads nothing, and the delivery is refused exactly as before.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

KEY_PREFIX = "webhook_refusals:"
MAX_REFUSALS = 10
REFUSALS_TTL_SECONDS = 7 * 86_400
MAX_REASON_CHARS = 200
_REDIS_TIMEOUT_SECONDS = 2.0

#: The headers a delivery authenticates with, named (never valued) in a refusal.
AUTH_HEADERS: tuple[str, ...] = (
    "x-webhook-token",
    "x-webhook-secret",
    "x-opencti-token",
    "x-hub-signature-256",
    "x-slack-signature",
    "x-signature",
    "authorization",
)


def auth_header_names(headers: Iterable[str]) -> list[str]:
    """The authentication headers among *headers*, lower-case, sorted: names only."""
    present = {str(name).lower() for name in headers}
    return sorted(name for name in AUTH_HEADERS if name in present)


def _reason(detail: Any) -> str:
    text = " ".join(str(detail or "refused").split())
    return text if len(text) <= MAX_REASON_CHARS else text[: MAX_REASON_CHARS - 3] + "..."


async def record_refusal(assignment_id: str, detail: Any, headers: Iterable[str]) -> None:
    """Keep one refused delivery of *assignment_id* (best effort, bounded)."""
    if not assignment_id:
        return
    from app.core.redis import redis_client

    entry = json.dumps(
        {
            "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reason": _reason(detail),
            "auth_headers": auth_header_names(headers),
        }
    )
    key = f"{KEY_PREFIX}{assignment_id}"
    try:
        async with asyncio.timeout(_REDIS_TIMEOUT_SECONDS):
            redis: Any = await redis_client()
            await redis.lpush(key, entry)
            await redis.ltrim(key, 0, MAX_REFUSALS - 1)
            await redis.expire(key, REFUSALS_TTL_SECONDS)
    except Exception as exc:
        logger.debug("Could not record a refused delivery of assignment %s: %r", assignment_id, exc)


async def refusals(assignment_id: Any) -> list[dict[str, Any]]:
    """The assignment's refused deliveries, newest first (empty when none or unreadable)."""
    from app.core.redis import redis_client

    try:
        async with asyncio.timeout(_REDIS_TIMEOUT_SECONDS):
            client: Any = await redis_client()
            raw = await client.lrange(f"{KEY_PREFIX}{assignment_id}", 0, MAX_REFUSALS - 1)
    except Exception as exc:
        logger.debug("Could not read the refused deliveries of assignment %s: %r", assignment_id, exc)
        return []
    found: list[dict[str, Any]] = []
    for item in raw or []:
        try:
            value = json.loads(item)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            found.append(value)
    return found
