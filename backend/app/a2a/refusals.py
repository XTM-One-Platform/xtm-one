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

"""The last token request an inbound ``oauth_a2a`` credential refused (#105).

The token endpoint answers every refusal with the same ``invalid_client``: a
caller that could tell "no such client" from "wrong secret" could enumerate
registrations. The OPERATOR of the integration, however, must be able to tell
them apart, or a partner holding a stale client id and one holding a mistyped
secret look the same and both get told to "check the credentials".

So the reason is recorded where only the integration's managers read it: on
its A2A settings row, shown by ``get_integration``. Never a secret: the time,
the client id the caller presented (an identifier it sends in clear on every
exchange) and a reason code.

A refusal is attributed to an integration only when the client id names one:
its current client id (wrong secret, or no agent left to reach), or one of the
ids an explicit rotation retired. A client id nobody issued names no
integration and is not recorded anywhere a partner-facing operator reads.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Final

logger = logging.getLogger(__name__)

#: The client id is known and current, the secret did not match its hash.
REASON_SECRET_MISMATCH: Final = "secret_mismatch"  # noqa: S105 - a reason code, not a secret
#: The client id is not this integration's current one: an explicit rotation
#: retired it. (An id nobody issued names no integration and is not recorded.)
REASON_UNKNOWN_CLIENT: Final = "unknown_client"
#: The credential is valid but no agent bound to the integration answers.
REASON_NO_REACHABLE_AGENT: Final = "no_reachable_agent"

#: What an operator should do, per reason. Shown next to the record.
WHAT_TO_DO: Final[dict[str, str]] = {
    REASON_SECRET_MISMATCH: (
        "The partner presented the current client id with a secret that does not "
        "match: they must paste the latest secret."
    ),
    REASON_UNKNOWN_CLIENT: (
        "The partner presented a client id that was rotated away: they must paste the current client id and secret."
    ),
    REASON_NO_REACHABLE_AGENT: ("The credential is valid, but no agent bound to this integration is exposed."),
}

_CLIENT_ID_MAX_CHARS = 255


def _session_factory():
    from app.core.database import async_session

    return async_session


async def attribute(db: Any, client_id: str) -> tuple[Any, str] | None:
    """``(integration_id, reason)`` for a refused *client_id*, or ``None``.

    Only a client id that names an inbound integration is attributed: the
    current one of its credential (the secret did not match) or one its
    rotation retired.
    """
    from sqlalchemy import select

    from app.models.a2a import DIRECTION_INBOUND, A2AIntegrationSettings
    from app.models.oauth_client import OAuthClient

    client = (await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))).scalar_one_or_none()
    integration_id = getattr(client, "a2a_integration_id", None) if client is not None else None
    if integration_id is not None:
        return integration_id, REASON_SECRET_MISMATCH
    if client is not None:
        return None
    rows = (
        (
            await db.execute(
                select(A2AIntegrationSettings).where(
                    A2AIntegrationSettings.direction == DIRECTION_INBOUND,
                    A2AIntegrationSettings.retired_client_ids.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if client_id in (row.retired_client_ids or []):
            return row.integration_id, REASON_UNKNOWN_CLIENT
    return None


async def record_refusal(
    integration_id: Any,
    client_id: str,
    reason: str,
    *,
    session_factory: Any = None,
) -> bool:
    """Stamp the refusal on the inbound settings row; whether it was written.

    Its own session and commit: the token request that was refused writes
    nothing else, and a failure here must never change its answer.
    """
    from sqlalchemy import update

    from app.models.a2a import DIRECTION_INBOUND, A2AIntegrationSettings

    factory = session_factory or _session_factory()
    try:
        async with factory() as db:
            stamped = await db.execute(
                update(A2AIntegrationSettings)
                .where(
                    A2AIntegrationSettings.integration_id == integration_id,
                    A2AIntegrationSettings.direction == DIRECTION_INBOUND,
                )
                .values(
                    last_refused_token_at=datetime.now(UTC),
                    last_refused_client_id=(client_id or "")[:_CLIENT_ID_MAX_CHARS],
                    last_refused_reason=reason,
                )
            )
            await db.commit()
            return bool(stamped.rowcount)
    except Exception:
        logger.warning("a2a: could not record a refused token request on %s", integration_id, exc_info=True)
        return False


async def note_refusal(db: Any, client_id: str, *, reason: str | None = None, integration_id: Any = None) -> None:
    """Attribute and record one refused token request. Never raises."""
    try:
        if integration_id is None or reason is None:
            found = await attribute(db, client_id)
            if found is None:
                return
            integration_id, reason = found
        await record_refusal(integration_id, client_id, reason)
    except Exception:
        logger.warning("a2a: could not attribute a refused token request", exc_info=True)


def describe_refusal(settings: Any) -> dict[str, Any] | None:
    """The last refusal ``get_integration`` exposes to managers; None when none."""
    at = getattr(settings, "last_refused_token_at", None)
    if settings is None or at is None:
        return None
    reason = getattr(settings, "last_refused_reason", None)
    return {
        "at": at.isoformat(),
        "client_id": getattr(settings, "last_refused_client_id", None),
        "reason": reason,
        "what_to_do": WHAT_TO_DO.get(reason or ""),
    }
