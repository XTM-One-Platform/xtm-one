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

"""A decision gate never waits silently for an account nobody uses (#2390).

A ``needs_human`` work item is valid when its recipient is an active account,
whether or not a person ever signs in to it: a gate addressed to the seed
account, or to a person who left, waited for days with nobody told. Three
signals, none of which refuses a write:

* **At the write.** :func:`inactive_reason` reads the recipient's last
  sign-in (its newest ``login`` audit row): never, or not for
  ``work_item_recipient_inactive_days`` days, or the technical seed account
  (``settings.admin_email``), or a group none of whose members signed in in
  that window. The tool answer of create / update / get carries it as a
  warning line (:func:`app.services.work_item_recipient.recipient_line`).
* **Unseen.** :func:`raise_unseen_decisions`, on the scheduler tick: a gate
  waiting ``work_item_decision_unseen_hours`` hours that no person opened
  (``GET /work-items/{id}`` stamps :data:`OPENED_MARK_KEY`) notifies every
  active administrator in the bell and raises
  ``work_item.needs_human_unseen``, once per gate (:data:`UNSEEN_MARK_KEY`).
* **Fleet health.** ``decision_unseen`` and ``decision_recipient_inactive``
  findings (``services/fleet_health_signals.py``), read live.

The two marks are the platform's: kept across a writer's payload rewrite
(:func:`carry_decision_marks`) and cleared when a new gate starts
(:func:`clear_decision_marks`).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.utils import format_exception_message, safe_uuid

logger = logging.getLogger(__name__)

UNSEEN_EVENT = "work_item.needs_human_unseen"
#: Payload key: when a person first opened the item's current gate.
OPENED_MARK_KEY = "_decision_opened_at"
#: Payload key: when the administrators were told the current gate went unseen.
UNSEEN_MARK_KEY = "_decision_unseen_alerted_at"
DECISION_MARK_KEYS: tuple[str, ...] = (OPENED_MARK_KEY, UNSEEN_MARK_KEY)
#: Gates alerted per tick at most; the rest are taken at the next tick.
UNSEEN_SCAN_LIMIT = 100
#: The notification source of the administrators' bell entry (presented as an agent message).
UNSEEN_NOTIFICATION_SOURCE = "agent"


def inactive_days() -> int:
    from app.core.config import settings

    return max(int(getattr(settings, "work_item_recipient_inactive_days", 7) or 7), 1)


def unseen_hours() -> int:
    from app.core.config import settings

    return max(int(getattr(settings, "work_item_decision_unseen_hours", 24) or 24), 1)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ── Pure rules ───────────────────────────────────────────────────────────────


def signed_in_recently(last_login: datetime | None, *, now: datetime, days: int) -> bool:
    """Whether *last_login* falls within the last *days* days."""
    last = _aware(last_login)
    return last is not None and last >= now - timedelta(days=days)


def person_inactive_reason(
    *, email: str | None, last_login: datetime | None, now: datetime, days: int, seed_email: str | None
) -> str | None:
    """Why a person-recipient is unlikely to see a gate, or ``None``."""
    if seed_email and email and email.strip().lower() == seed_email.strip().lower():
        return "the recipient is the platform's technical seed account, which no person signs in to"
    if last_login is None:
        return "the recipient has never signed in"
    if not signed_in_recently(last_login, now=now, days=days):
        return f"the recipient last signed in on {_aware(last_login).date().isoformat()}, over {days} days ago"  # type: ignore[union-attr]
    return None


def group_inactive_reason(member_logins: Sequence[datetime | None], *, now: datetime, days: int) -> str | None:
    """Why a group-recipient is unlikely to see a gate (no member signed in recently), or ``None``."""
    if any(signed_in_recently(at, now=now, days=days) for at in member_logins):
        return None
    if not member_logins:
        return "its recipient groups have no member"
    return f"no member of its recipient groups signed in within {days} days"


def is_unseen(payload: Any, *, waiting_since: datetime | None, now: datetime, hours: int) -> bool:
    """Whether a gate waiting since *waiting_since* was never opened and is older than *hours*."""
    since = _aware(waiting_since)
    if since is None or since > now - timedelta(hours=hours):
        return False
    return not (isinstance(payload, Mapping) and payload.get(OPENED_MARK_KEY))


def carry_decision_marks(payload: Any, previous: Any) -> Any:
    """*payload* (a writer's new payload) with the stored decision marks kept: they are the platform's."""
    if not isinstance(payload, Mapping) or not isinstance(previous, Mapping):
        return payload
    kept = {k: previous[k] for k in DECISION_MARK_KEYS if previous.get(k) is not None and k not in payload}
    return {**payload, **kept} if kept else payload


def clear_decision_marks(item: Any) -> None:
    """Drop the marks of *item* whose new gate starts, so it is watched afresh."""
    payload = getattr(item, "payload", None)
    if isinstance(payload, Mapping) and any(k in payload for k in DECISION_MARK_KEYS):
        item.payload = {k: v for k, v in payload.items() if k not in DECISION_MARK_KEYS}


# ── Reads ────────────────────────────────────────────────────────────────────


async def last_logins(db: Any, user_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, datetime]:
    """The last sign-in of each account: its newest ``login`` audit row."""
    from sqlalchemy import func

    from app.models.audit_log import AuditLog

    ids = list({u for u in user_ids if u is not None})
    if not ids:
        return {}
    rows = await db.execute(
        select(AuditLog.user_id, func.max(AuditLog.created_at))
        .where(AuditLog.action == "login", AuditLog.user_id.in_(ids))
        .group_by(AuditLog.user_id)
    )
    return {uid: at for uid, at in rows.all()}


async def inactive_reasons(
    db: Any, recipients: Sequence[tuple[Any, Any]], *, now: datetime | None = None
) -> list[str | None]:
    """For each ``(person id, group ids)`` recipient, why it is unlikely to see a gate, or ``None``.

    ``(None, None)`` (the administrators) is never inactive. Three queries for the whole batch.
    """
    from app.core.config import settings
    from app.models.group import GroupMember
    from app.models.user import User

    now = now or datetime.now(UTC)
    days = inactive_days()
    people = {u for person, _ in recipients if (u := safe_uuid(str(person)) if person else None) is not None}
    groups = {g for _, gids in recipients for g in (safe_uuid(str(x)) for x in (gids or [])) if g is not None}
    members: dict[uuid.UUID, list[uuid.UUID]] = {}
    if groups:
        for gid, uid in (
            await db.execute(select(GroupMember.group_id, GroupMember.user_id).where(GroupMember.group_id.in_(groups)))
        ).all():
            members.setdefault(gid, []).append(uid)
    emails: dict[uuid.UUID, str | None] = {}
    if people:
        emails = dict((await db.execute(select(User.id, User.email).where(User.id.in_(people)))).all())
    logins = await last_logins(db, {*people, *(u for us in members.values() for u in us)})
    out: list[str | None] = []
    for person, gids in recipients:
        if gids:
            ids = [g for g in (safe_uuid(str(x)) for x in gids) if g is not None]
            out.append(
                group_inactive_reason([logins.get(u) for g in ids for u in members.get(g, [])], now=now, days=days)
            )
            continue
        uid = safe_uuid(str(person)) if person else None
        if uid is None:
            out.append(None)
            continue
        out.append(
            person_inactive_reason(
                email=emails.get(uid), last_login=logins.get(uid), now=now, days=days, seed_email=settings.admin_email
            )
        )
    return out


async def inactive_reason(db: Any, *, user_id: Any, group_ids: Any, now: datetime | None = None) -> str | None:
    """Why the recipient of one gate is unlikely to see it, or ``None``."""
    return (await inactive_reasons(db, [(user_id, group_ids)], now=now))[0]


def warning_line(reason: str) -> str:
    """The tool line a write answers with when its recipient is unlikely to see the gate."""
    return (
        f"  - warning: {reason}, so this decision may wait unseen; name an active person with "
        "recipient (a user id, an email, or group: followed by a group id)."
    )


# ── Opening ──────────────────────────────────────────────────────────────────


async def mark_opened(work_item_id: str, *, now: datetime | None = None) -> bool:
    """Stamp that a person opened the item's current gate; whether a mark was written. Never raises."""
    from app.core.database import async_session
    from app.models.work_item import WorkItem

    item_id = safe_uuid(work_item_id)
    if item_id is None:
        return False
    now = now or datetime.now(UTC)
    try:
        async with async_session() as db:
            item = (
                await db.execute(
                    select(WorkItem)
                    .where(WorkItem.id == item_id, WorkItem.needs_human.is_(True))
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            payload = dict(item.payload or {}) if item is not None else {}
            if item is None or payload.get(OPENED_MARK_KEY):
                await db.rollback()
                return False
            payload[OPENED_MARK_KEY] = now.isoformat()
            item.payload = payload
            await db.commit()
            return True
    except Exception as exc:
        logger.warning("Work item %s opening not stamped: %s", work_item_id, format_exception_message(exc))
        return False


# ── Unseen gates ─────────────────────────────────────────────────────────────


def unseen_event_id(item: Any) -> str:
    since = _aware(getattr(item, "last_activity_at", None))
    return f"{item.id}:needs_human_unseen:{since.isoformat() if since else ''}"


async def _notify_admins(db: Any, item: Any, *, hours: int) -> list[str]:
    from app.models.user import User
    from app.services.decision_links import work_item_link
    from app.services.notify import notify_web_user

    admin_ids = [
        str(a)
        for a in (await db.execute(select(User.id).where(User.role == "admin", User.is_active.is_(True))))
        .scalars()
        .all()
    ]
    for admin_id in admin_ids:
        await notify_web_user(
            db,
            admin_id,
            title=f"A decision has waited {hours} hours unseen: {str(item.title)[:300]}",
            summary="Nobody opened this decision. Open it, answer it, or name an active recipient.",
            source_label="Work items",
            extra_data={
                "notification_source": UNSEEN_NOTIFICATION_SOURCE,
                "path": work_item_link(item.id),
                "work_item_id": str(item.id),
            },
        )
    return admin_ids


async def raise_unseen_decisions(*, now: datetime | None = None, limit: int = UNSEEN_SCAN_LIMIT) -> int:
    """Tell the administrators once about each gate nobody opened in time; how many gates."""
    from app.core.database import async_session
    from app.models.work_item import WORK_ITEM_OPEN_STATUSES, WorkItem
    from app.services.platform_events import publish, work_item_data

    now = now or datetime.now(UTC)
    hours = unseen_hours()
    clause = (
        WorkItem.status.in_(WORK_ITEM_OPEN_STATUSES),
        WorkItem.needs_human.is_(True),
        WorkItem.last_activity_at <= now - timedelta(hours=hours),
        WorkItem.payload[OPENED_MARK_KEY].astext.is_(None),
        WorkItem.payload[UNSEEN_MARK_KEY].astext.is_(None),
    )
    raised = 0
    async with async_session() as db:
        ids = (
            (
                await db.execute(
                    select(WorkItem.id).where(*clause).order_by(WorkItem.last_activity_at.asc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        for item_id in ids:
            try:
                item = (
                    await db.execute(
                        select(WorkItem).where(WorkItem.id == item_id, *clause).with_for_update(skip_locked=True)
                    )
                ).scalar_one_or_none()
                if item is None:
                    await db.rollback()
                    continue
                payload = dict(item.payload or {})
                payload[UNSEEN_MARK_KEY] = now.isoformat()
                item.payload = payload
                admins = await _notify_admins(db, item, hours=hours)
                data = work_item_data(item, previous_status=item.status)
                since = _aware(item.last_activity_at)
                data["waiting_since"] = since.isoformat() if since else None
                data["unseen_hours"] = hours
                data["notified_admin_ids"] = admins
                data["waits_for_user_id"] = str(item.waits_for_user_id) if item.waits_for_user_id else None
                data["waits_for_group_ids"] = list(item.waits_for_group_ids or []) or None
                event_id = unseen_event_id(item)
                await db.commit()
            except Exception as exc:
                logger.warning("Unseen decision %s not raised: %s", item_id, format_exception_message(exc))
                await db.rollback()
                continue
            await publish(UNSEEN_EVENT, data, event_id=event_id)
            raised += 1
    return raised


__all__ = [
    "DECISION_MARK_KEYS",
    "OPENED_MARK_KEY",
    "UNSEEN_EVENT",
    "UNSEEN_MARK_KEY",
    "carry_decision_marks",
    "clear_decision_marks",
    "group_inactive_reason",
    "inactive_reason",
    "inactive_reasons",
    "is_unseen",
    "mark_opened",
    "person_inactive_reason",
    "raise_unseen_decisions",
    "signed_in_recently",
    "warning_line",
]
