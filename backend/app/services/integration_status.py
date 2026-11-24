# SPDX-License-Identifier: Apache-2.0

"""Why and since when an integration is not usable (#1665).

``Integration.status`` says whether the tools can run; ``status_reason`` and
``status_reason_at`` say why it last left ``connected`` and when. The refresh
sweep writes both where it decides ``error`` or ``reauthorize``; they are read
only while the status is one of those (:func:`current_reason`), so a reconnect
needs no write of its own. The readers here name an
integration in trouble wherever a person or an agent meets it: the
integration read, ``list_available_resources`` and a gate check whose tool
belongs to it.
"""

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: The statuses whose tools cannot run as connected.
UNUSABLE_STATUSES = frozenset({"error", "reauthorize"})
#: The stored reason's length (the column's).
REASON_MAX = 300


def record_status_reason(holder: object, status: str, reason: str) -> None:
    """Set *holder*'s status with the reason it is set for.

    *holder* is an ``Integration`` or a person's credential row; only the
    integration keeps the reason (a credential row reads the integration's
    columns through read-only properties and keeps its status alone, as before).
    """
    from app.models.integration import Integration

    holder.status = status  # type: ignore[attr-defined]
    if isinstance(holder, Integration):
        holder.status_reason = (reason or "").strip()[:REASON_MAX] or None
        holder.status_reason_at = datetime.now(UTC)


def current_reason(holder: object) -> tuple[str | None, datetime | None]:
    """``(reason, recorded at)`` while the *holder* integration is not usable, ``(None, None)`` otherwise."""
    from app.models.integration import Integration

    if not isinstance(holder, Integration) or holder.status not in UNUSABLE_STATUSES:
        return None, None
    return getattr(holder, "status_reason", None), getattr(holder, "status_reason_at", None)


def describe_unusable(status: str, reason: str | None, changed_at: datetime | None) -> str:
    """One line naming an unusable status, its reason and its time."""
    since = f" since {changed_at.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')}" if changed_at else ""
    because = f": {reason}" if reason else ": no reason recorded"
    return f"status {status}{since}{because}"


async def unusable_integrations(db: AsyncSession, integration_ids: Iterable[str]) -> dict[str, str]:
    """``{integration id: description}`` for those of *integration_ids* that are not usable.

    An id that is not a UUID, or names no row, is left out. A read that fails
    answers ``{}``: the callers only add a hint.
    """
    from app.models.integration import Integration

    ids: list[uuid.UUID] = []
    for raw in integration_ids:
        try:
            ids.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            continue
    if not ids:
        return {}
    try:
        rows = (
            await db.execute(
                select(
                    Integration.id, Integration.status, Integration.status_reason, Integration.status_reason_at
                ).where(Integration.id.in_(ids), Integration.status.in_(UNUSABLE_STATUSES))
            )
        ).all()
    except Exception:
        return {}
    return {str(row.id): describe_unusable(row.status, row.status_reason, row.status_reason_at) for row in rows}


def check_integration_ids(check: dict[str, Any] | None) -> list[str]:
    """The integration ids a stored gate check (and its ``also`` / ``or`` checks) names."""
    if not isinstance(check, dict):
        return []
    found: list[str] = []
    stack = [check]
    while stack:
        item = stack.pop()
        if not isinstance(item, dict):
            continue
        integration_id = item.get("integration_id")
        if integration_id and str(integration_id) not in found:
            found.append(str(integration_id))
        for key in ("also", "or"):
            nested = item.get(key)
            if isinstance(nested, list):
                stack.extend(nested)
            elif isinstance(nested, dict):
                stack.append(nested)
    return found


async def gate_check_integration_warning(db: AsyncSession, check: dict[str, Any] | None) -> str | None:
    """A sentence naming the integrations a gate check depends on that are not usable, else ``None``."""
    found = await unusable_integrations(db, check_integration_ids(check))
    if not found:
        return None
    parts = "; ".join(f"integration {integration_id}: {text}" for integration_id, text in found.items())
    return f"Warning: the gate check calls a tool of an integration that is not usable ({parts}); it cannot pass until it is reconnected."
