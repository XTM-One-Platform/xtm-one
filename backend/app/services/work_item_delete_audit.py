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

"""What a work item delete leaves behind: its open decision settled, one audit row (#2201).

A delete removes the row outright. Before #2201 an item waiting on a person
(``needs_human``) vanished with its decision still ``open`` (the bulk delete is a
core ``DELETE`` the ORM ``before_flush`` hook never sees, so the gate was never
closed and its sent cards never turned decided), and nothing was written to the
audit log, so an open human gate disappeared without a trace.

:func:`settle_before_delete` runs in the deleting session, before the delete:

- every open decision of the targets is cancelled with the reason
  ``work item deleted`` (a ``cancelled`` event per decision), so the decision
  queue, the sent cards and the CTEM case audit that reads gate events see
  the gate end;
- one audit row per item (``entity_type`` work_item, ``action`` delete), its
  details saying whether it waited on a person and which decisions it cancelled.

Never fails the delete: a database without the decisions table (a scratch test
schema) or a missing user leaves the delete as it was, logged.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

logger = logging.getLogger(__name__)

#: The reason a decision cancelled by its item's delete carries.
DELETED_REASON = "work item deleted"


async def _cancel_open_decisions(db: Any, item_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[str]]:
    """Cancel the open decisions of *item_ids*; ``{item id: [decision ids]}`` of those cancelled."""
    from app.models.decision import Decision, DecisionEvent

    if not item_ids:
        return {}
    now = datetime.now(UTC)
    try:
        async with db.begin_nested():
            rows = (
                await db.execute(
                    update(Decision)
                    .where(
                        Decision.source_type == "work_item",
                        Decision.source_id.in_(list(item_ids)),
                        Decision.status == "open",
                    )
                    .values(status="cancelled", updated_at=now)
                    .returning(Decision.id, Decision.source_id)
                )
            ).all()
            for decision_id, _source in rows:
                db.add(DecisionEvent(decision_id=decision_id, event="cancelled", detail={"reason": DELETED_REASON}))
    except Exception as exc:
        logger.warning("Could not cancel the open decisions of %d deleted work item(s): %s", len(item_ids), exc)
        return {}
    out: dict[uuid.UUID, list[str]] = {}
    for decision_id, source in rows:
        out.setdefault(source, []).append(str(decision_id))
    return out


def audit_details(item: Any, cancelled: list[str]) -> dict[str, Any]:
    """The audit details of one deleted item. Pure."""
    return {
        "status": getattr(item, "status", None),
        "needs_human": bool(getattr(item, "needs_human", False)),
        "correlation_key": getattr(item, "correlation_key", None),
        "owner_agent_id": str(item.owner_agent_id) if getattr(item, "owner_agent_id", None) else None,
        "cancelled_decisions": cancelled,
    }


def audit_summary(item: Any, cancelled: list[str]) -> str:
    """The one-line audit summary of one deleted item. Pure."""
    title = (getattr(item, "title", None) or "")[:200]
    if getattr(item, "needs_human", False) or cancelled:
        return f"Deleted work item '{title}' while it waited on a person's decision (decision cancelled)"
    return f"Deleted work item '{title}' ({getattr(item, 'status', None)})"


async def settle_before_delete(db: Any, user_uuid: uuid.UUID | None, items: Sequence[Any]) -> list[str]:
    """Cancel the open decisions of *items* and write one audit row each, in *db*, before their delete.

    Returns the cancelled decision ids: the caller passes them to
    :func:`refresh_cancelled_cards` once its commit landed.
    """
    from app.models.user import User
    from app.services.audit import log_audit

    if not items:
        return []
    cancelled = await _cancel_open_decisions(db, [item.id for item in items])
    decision_ids = [d for ids in cancelled.values() for d in ids]
    user = None
    if user_uuid is not None:
        user = (await db.execute(select(User).where(User.id == user_uuid))).scalar_one_or_none()
    if user is None:
        logger.warning("Work item delete by an unknown user: %d item(s) deleted without an audit row", len(items))
        return decision_ids
    for item in items:
        mine = cancelled.get(item.id, [])
        try:
            await log_audit(
                db,
                user=user,
                action="delete",
                entity_type="work_item",
                entity_id=item.id,
                entity_name=(item.title or "")[:255],
                summary=audit_summary(item, mine)[:1000],
                details=audit_details(item, mine),
            )
        except Exception as exc:
            logger.warning("Could not audit the delete of work item %s: %s", item.id, exc)
    return decision_ids


def refresh_cancelled_cards(decision_ids: Sequence[str]) -> None:
    """Turn the sent cards of *decision_ids* into the decided card, in the background, after the commit."""
    if not decision_ids:
        return
    from app.services.decision_router import schedule_card_refresh

    for decision_id in decision_ids:
        try:
            schedule_card_refresh(uuid.UUID(decision_id))
        except RuntimeError:  # no running loop: the cards show the decided state at their next read
            return


__all__ = ["DELETED_REASON", "audit_details", "audit_summary", "refresh_cancelled_cards", "settle_before_delete"]
