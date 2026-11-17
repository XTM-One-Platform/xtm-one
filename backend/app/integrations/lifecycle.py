# SPDX-License-Identifier: Apache-2.0

"""Provider lifecycle hooks (integration seam).

A provider can register optional ``on_connect`` / ``on_disconnect`` coroutines
that run when its integration becomes connected or is removed — the seam that
lets an integration set itself up (e.g. create a rule-sync source, register a
flow node) without editing the central create/delete endpoints. Providers
without a hook are a no-op, so this is invisible to every existing integration.

``on_update`` runs after every edit of the integration (``PUT
/integrations/{id}``), once the edit is flushed and before it is committed, so
a provider can carry the edit over to what it set up — OpenRouter copies a
replaced API key into the platform LLM provider it feeds. Unlike the other
two it is NOT best-effort: it keeps state in step with the integration, so a
hook that fails fails the edit (409, nothing committed) rather than leave the
two apart with no trace. It is handed an :class:`IntegrationUpdate`: who made
the edit (for the audit of what the hook changes) and the route's
``AfterCommit`` for what must follow the commit (a cache drop).

Hooks are best-effort: a failing ``on_connect`` is logged, not raised, so a
transient side-effect problem (an unreachable repo) never turns a successful
connect into a 500. Side-effects a hook creates should therefore be idempotent
(safe to re-run on the next connect or a manual retry).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.api.deps import AfterCommit
    from app.models.integration import Integration
    from app.models.user import User

logger = logging.getLogger(__name__)



@dataclass(frozen=True)
class IntegrationUpdate:
    """The edit an ``on_update`` hook is told about."""

    actor: User
    request: Request | None
    after_commit: AfterCommit


LifecycleHook = Callable[["AsyncSession", "Integration"], Awaitable[None]]
UpdateHook = Callable[["AsyncSession", "Integration", IntegrationUpdate], Awaitable[None]]

_ON_CONNECT: dict[str, LifecycleHook] = {}
_ON_DISCONNECT: dict[str, LifecycleHook] = {}
_ON_UPDATE: dict[str, UpdateHook] = {}


def register_lifecycle(
    provider: str,
    *,
    on_connect: LifecycleHook | None = None,
    on_disconnect: LifecycleHook | None = None,
    on_update: UpdateHook | None = None,
) -> None:
    """Register lifecycle hooks for *provider*. Idempotent (last wins)."""
    if on_connect is not None:
        _ON_CONNECT[provider] = on_connect
    if on_disconnect is not None:
        _ON_DISCONNECT[provider] = on_disconnect
    if on_update is not None:
        _ON_UPDATE[provider] = on_update


async def dispatch_on_connect(db: AsyncSession, integration: Integration) -> None:
    """Run the provider's ``on_connect`` hook, if any. Never raises."""
    hook = _ON_CONNECT.get(integration.provider)
    if hook is None:
        return
    try:
        await hook(db, integration)
    except Exception:
        logger.exception(
            "on_connect hook failed for provider %s", integration.provider
        )


async def dispatch_on_disconnect(db: AsyncSession, integration: Integration) -> None:
    """Run the provider's ``on_disconnect`` hook, if any. Never raises."""
    hook = _ON_DISCONNECT.get(integration.provider)
    if hook is None:
        return
    try:
        await hook(db, integration)
    except Exception:
        logger.exception(
            "on_disconnect hook failed for provider %s", integration.provider
        )


async def dispatch_on_update(
    db: AsyncSession, integration: Integration, update: IntegrationUpdate
) -> None:
    """Run the provider's ``on_update`` hook, if any.

    A failure fails the edit: the hook keeps what the provider set up in step
    with the integration (OpenRouter: the key of the LLM provider it feeds),
    and an edit saved while that copy stayed behind would leave the two apart
    silently. The caller's transaction is rolled back with it; the client gets
    a 409 whose message says nothing was saved, the reason goes to the log (a
    503 would read as the backend being down to the web client).
    """
    hook = _ON_UPDATE.get(integration.provider)
    if hook is None:
        return
    try:
        await hook(db, integration, update)
    except Exception as exc:
        logger.exception(
            "on_update hook failed for provider %s", integration.provider
        )
        from fastapi import HTTPException

        raise HTTPException(
            status_code=409,
            detail=(
                "The edit could not be carried over to what this integration "
                "feeds, so nothing was saved. Try again."
            ),
        ) from exc
