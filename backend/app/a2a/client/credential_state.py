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

"""The credential state of an OUTBOUND A2A integration, on the integration itself.

A partner credential that cannot mint a token used to show only on the
per-card target rows (``usability`` / ``unusable_reason``): the integration
kept ``status: connected``, nobody was told, and ``get_integration`` gave no
hint (#12346). This module is the one writer that makes the integration's own
status follow its credential:

* :func:`record_mint_failure`: the stored credential could not mint (the
  token endpoint answered 400/401, a secret is missing, a vault reference is
  unreadable). The integration moves to ``error`` with the reason, the detail
  and the time on its A2A settings row; the owner and the administrators are
  notified ONCE per transition (the UPDATE that flips the status is the
  transition, so a second failed send changes nothing and notifies nobody).
* :func:`record_mint_success`: a token was minted. ``last_token_minted_at`` is
  stamped (at most every :data:`MINT_STAMP_INTERVAL`), and an integration in
  ``error`` FOR ITS CREDENTIAL returns to ``connected``.
* :func:`clear_on_credential_edit`: an operator typed a new credential; the
  error is cleared and the integration is ``connected`` again, the next mint
  being the test.

Every write runs in its own session and commits: the integration a delegation
holds may come from another session (the tool row cache), and the state must
not depend on whether the caller commits. Never raises: the delegation outcome
the calling agent reads matters more than this bookkeeping.

Outbound only. An inbound A2A integration, and every other provider, is never
touched: the settings row's direction is checked in the same UPDATE.

No secret is ever written here: the detail is the ``CredentialError`` text,
which names an HTTP status or a missing field, never a value.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final

logger = logging.getLogger(__name__)

#: The integration status an outbound credential failure sets: the value the
#: status badge, the security audit and the integration list already know.
STATUS_ERROR: Final = "error"
STATUS_CONNECTED: Final = "connected"
#: ``Integration.connection_test_status`` values (``api.v1.integrations``).
CONNECTION_TEST_UNTESTED: Final = "untested"
CONNECTION_TEST_OK: Final = "ok"

#: ``credential_error_code`` of a credential that cannot mint a token. The
#: refusal outcome the delegation answers carries the same family.
CODE_CREDENTIAL_REJECTED: Final = "credential_rejected"

#: How often a successful mint is stamped: a token is cached for its own
#: lifetime, so this bounds the writes of a busy partner, not the mints.
MINT_STAMP_INTERVAL: Final = timedelta(minutes=5)

#: Notification source of the transition notice.
NOTIFICATION_SOURCE: Final = "integration_a2a_credential"
SOURCE_LABEL: Final = "Integrations"

_DETAIL_MAX_CHARS: Final = 500

#: Integration id -> when this process last stamped a mint: bounds the writes
#: of a busy partner when the caller holds no settings row.
_LAST_STAMP: dict[str, datetime] = {}


def repair_hint(integration: Any) -> str:
    """The sentence a refusal ends with: what an administrator must do."""
    name = getattr(integration, "name", None) or str(getattr(integration, "id", ""))
    return (
        "The partner credential was rejected; an administrator must enter a valid "
        f"client id and secret on integration '{name}'."
    )


def _is_row(integration: Any) -> bool:
    """Whether *integration* is a persisted integration row (not a stand-in)."""
    return getattr(integration, "__tablename__", None) == "integrations"


def _session_factory():
    from app.core.database import async_session

    return async_session


async def _admin_ids(db: Any) -> list[str]:
    from sqlalchemy import select

    from app.models.user import User

    rows = (await db.execute(select(User.id).where(User.role == "admin", User.is_active.is_(True)))).scalars().all()
    return [str(user_id) for user_id in rows]


async def _notify_transition(db: Any, integration: Any, detail: str) -> int:
    """Tell the owner and the administrators once; how many were told."""
    from app.services.notify import notify_web_user

    integration_id = getattr(integration, "id", None)
    name = getattr(integration, "name", None) or str(integration_id)
    to: list[str] = []
    owner = getattr(integration, "user_id", None)
    if owner is not None:
        to.append(str(owner))
    for admin in await _admin_ids(db):
        if admin not in to:
            to.append(admin)
    for user_id in to:
        await notify_web_user(
            db,
            user_id,
            title=f"Integration '{name}': the partner rejected its credential",
            summary=(
                f"Delegations through the outbound A2A integration '{name}' fail: {detail} "
                f"{repair_hint(integration)} This notice is sent once each time the "
                "integration enters this error."
            ),
            source_label=SOURCE_LABEL,
            extra_data={
                "notification_source": NOTIFICATION_SOURCE,
                "integration_id": str(integration_id),
                "path": f"/integrations/{integration_id}",
            },
        )
    return len(to)


async def record_mint_failure(
    integration: Any,
    detail: str,
    *,
    code: str = CODE_CREDENTIAL_REJECTED,
    session_factory: Any = None,
) -> bool:
    """Move an outbound integration to ``error``; whether this call made the transition."""
    from sqlalchemy import update

    from app.models.a2a import DIRECTION_OUTBOUND, A2AIntegrationSettings
    from app.models.integration import Integration

    integration_id = getattr(integration, "id", None)
    if integration_id is None or not _is_row(integration):
        return False
    detail = (detail or "")[:_DETAIL_MAX_CHARS]
    now = datetime.now(UTC)
    factory = session_factory or _session_factory()
    try:
        async with factory() as db:
            stamped = await db.execute(
                update(A2AIntegrationSettings)
                .where(
                    A2AIntegrationSettings.integration_id == integration_id,
                    A2AIntegrationSettings.direction == DIRECTION_OUTBOUND,
                )
                .values(
                    credential_error_code=code,
                    credential_error_detail=detail,
                    credential_error_at=now,
                )
            )
            if not stamped.rowcount:
                # Not an outbound A2A row: nothing of this module applies.
                await db.rollback()
                return False
            _LAST_STAMP.pop(str(integration_id), None)
            flipped = await db.execute(
                update(Integration)
                .where(Integration.id == integration_id, Integration.status != STATUS_ERROR)
                .values(status=STATUS_ERROR)
            )
            transitioned = bool(flipped.rowcount)
            if transitioned:
                told = await _notify_transition(db, integration, detail)
                logger.warning(
                    "a2a.client: integration %s moved to error (%s: %s); notified %d user(s)",
                    integration_id,
                    code,
                    detail,
                    told,
                )
            await db.commit()
        return transitioned
    except Exception:
        logger.warning("a2a.client: could not record the credential failure of %s", integration_id, exc_info=True)
        return False


async def record_mint_success(
    integration: Any,
    *,
    last_minted_at: datetime | None = None,
    session_factory: Any = None,
    force: bool = False,
) -> bool:
    """Stamp a minted token and restore ``connected``; whether anything was written.

    *last_minted_at* is the stamp the caller already read (the settings row it
    holds), so a busy partner writes at most every :data:`MINT_STAMP_INTERVAL`
    unless the integration is in error. ``force`` (an operator's Verify, #105)
    always writes: the stamp is the answer the operator is waiting for.
    """
    from sqlalchemy import update

    from app.models.a2a import DIRECTION_OUTBOUND, A2AIntegrationSettings
    from app.models.integration import Integration

    integration_id = getattr(integration, "id", None)
    if integration_id is None or not _is_row(integration):
        return False
    now = datetime.now(UTC)
    in_error = getattr(integration, "status", None) == STATUS_ERROR
    last = last_minted_at or _LAST_STAMP.get(str(integration_id))
    if not force and not in_error and last is not None and now - last < MINT_STAMP_INTERVAL:
        return False
    _LAST_STAMP[str(integration_id)] = now
    factory = session_factory or _session_factory()
    try:
        async with factory() as db:
            stamped = await db.execute(
                update(A2AIntegrationSettings)
                .where(
                    A2AIntegrationSettings.integration_id == integration_id,
                    A2AIntegrationSettings.direction == DIRECTION_OUTBOUND,
                )
                .values(
                    last_token_minted_at=now,
                    credential_error_code=None,
                    credential_error_detail=None,
                    credential_error_at=None,
                )
            )
            if not stamped.rowcount:
                await db.rollback()
                return False
            # Only an error this module set: the credential just minted, so
            # the reason it was in error no longer holds.
            restored = await db.execute(
                update(Integration)
                .where(Integration.id == integration_id, Integration.status == STATUS_ERROR)
                .values(status=STATUS_CONNECTED)
            )
            if restored.rowcount:
                logger.info("a2a.client: integration %s minted a token again, back to connected", integration_id)
            # A credential saved since and not yet tested is tested now (#105).
            await db.execute(
                update(Integration)
                .where(
                    Integration.id == integration_id,
                    Integration.connection_test_status == CONNECTION_TEST_UNTESTED,
                )
                .values(connection_test_status=CONNECTION_TEST_OK)
            )
            await db.commit()
        return True
    except Exception:
        logger.warning("a2a.client: could not record the minted token of %s", integration_id, exc_info=True)
        return False


async def clear_on_credential_edit(db: Any, integration: Any) -> bool:
    """An operator typed a new credential: clear the error in the caller's transaction.

    Returns whether the integration was in a credential error. The caller
    commits with the edit itself, so a refused edit leaves the error in place.

    The saved credential is NOT reported as working (#105): the integration
    reads "Not tested" (``connection_test_status`` untested) until a mint
    succeeds, from Verify, Test Connection or the next send. It used to read
    connected, green on a secret nobody had tried.
    """
    from sqlalchemy import update

    from app.models.a2a import DIRECTION_OUTBOUND, A2AIntegrationSettings

    integration_id = getattr(integration, "id", None)
    if integration_id is None:
        return False
    _LAST_STAMP.pop(str(integration_id), None)
    if hasattr(integration, "connection_test_status"):
        integration.connection_test_status = CONNECTION_TEST_UNTESTED
    # The targets' verdicts were judged on the old credential: mark them due,
    # so the next delegation re-verifies (and mints) before trusting a stale
    # unusable or usable, and the sweep takes them first (#105).
    from app.models.a2a import A2AClientTarget

    await db.execute(
        update(A2AClientTarget).where(A2AClientTarget.integration_id == integration_id).values(last_verified_at=None)
    )
    cleared = await db.execute(
        update(A2AIntegrationSettings)
        .where(
            A2AIntegrationSettings.integration_id == integration_id,
            A2AIntegrationSettings.direction == DIRECTION_OUTBOUND,
            A2AIntegrationSettings.credential_error_code.is_not(None),
        )
        .values(credential_error_code=None, credential_error_detail=None, credential_error_at=None)
    )
    if not cleared.rowcount:
        return False
    if getattr(integration, "status", None) == STATUS_ERROR:
        integration.status = STATUS_CONNECTED
    return True


def describe(settings: Any, integration: Any) -> dict[str, Any] | None:
    """The registration state ``get_integration`` exposes; None for any other row.

    Never a secret: a code, the error text the mint raised and timestamps.
    """
    if settings is None or not getattr(settings, "is_outbound", False):
        return None

    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    code = getattr(settings, "credential_error_code", None)
    return {
        "usable": code is None and getattr(integration, "status", None) != STATUS_ERROR,
        "last_token_minted_at": _iso(getattr(settings, "last_token_minted_at", None)),
        "last_error": (
            {
                "code": code,
                "detail": getattr(settings, "credential_error_detail", None),
                "at": _iso(getattr(settings, "credential_error_at", None)),
            }
            if code
            else None
        ),
    }
