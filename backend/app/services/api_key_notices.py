# SPDX-License-Identifier: Apache-2.0

"""Tell the owner of a personal API key what happens to it.

Two notices, both in the notification bell (the Activity row + live push of
``notify_web_user``), opening My Profile > API Keys:

* **before it expires** - :data:`api_key_access.EXPIRY_NOTICE_DAYS` (7 days,
  then 1 day) before. The scheduler asks :func:`notify_expiring_api_keys_if_due`
  on every tick and a Redis lease runs the scan at most once per
  :data:`NOTICE_INTERVAL_SECONDS` install-wide. What was sent is durable: the
  key's ``expiry_notified_days`` is written in the transaction that commits the
  notification, so each notice goes out once per key, and losing Redis repeats
  nothing. A notice the key was already inside when it was created or re-dated
  counts as sent (its owner just chose that date). A disabled account is told
  nothing and nothing is recorded, so it still hears of a key that has not
  expired once it is enabled again.
* **when an administrator revokes it** - :func:`notify_key_revoked`, from the
  revocation route, in its transaction.

In the bell only, like the platform's other per-user notices (a paused
assignment, a Security Audit finding): no email.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, or_, select

from app.core.database import async_session
from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.models.api_key import UserApiKey
from app.models.user import User
from app.services import api_key_access as access
from app.services.notify import notify_web_user

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

NOTICE_INTERVAL_SECONDS = 3600
LEASE_KEY = "api_keys:expiry_notice_lease"
NOTIFICATION_SOURCE = "api_key"
SOURCE_LABEL = "API keys"
PROFILE_PATH = "/profile/api-keys"
#: Keys handled per scan: the next one picks up the rest.
_SCAN_LIMIT = 500


def _days(count: int) -> str:
    return f"{count} day" if count == 1 else f"{count} days"


async def notify_key_revoked(db: AsyncSession, key: UserApiKey) -> None:
    """Tell *key*'s owner an administrator revoked it; flushed with the caller's transaction."""
    await notify_web_user(
        db,
        str(key.user_id),
        title=f"An administrator revoked your API key '{key.name}'",
        summary=(
            "It no longer authenticates. Create a new key in My Profile > API Keys if you still need one."
        ),
        source_label=SOURCE_LABEL,
        extra_data={"notification_source": NOTIFICATION_SOURCE, "api_key_id": str(key.id), "path": PROFILE_PATH},
    )


async def notify_expiring_api_keys_if_due() -> int:
    """Run :func:`notify_expiring_api_keys` when this process takes the lease; the notices sent."""
    try:
        redis = await redis_client()
        if not await redis.set(LEASE_KEY, "1", nx=True, ex=NOTICE_INTERVAL_SECONDS):
            return 0
    except Exception as exc:
        logger.debug("API key expiry notice lease not taken: %s", format_exception_message(exc))
        return 0
    async with async_session() as db:
        return await notify_expiring_api_keys(db)


async def notify_expiring_api_keys(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Send every expiry notice that is due, once per key and notice; commits. Returns how many went out."""
    now = now or datetime.now(UTC)
    # Only keys with a notice still to send, so keys already told never fill the
    # batch: inside a notice window it has not had yet (``due_expiry_notice`` in SQL).
    unsent = or_(
        *(
            and_(
                UserApiKey.expires_at <= now + timedelta(days=days),
                or_(UserApiKey.expiry_notified_days.is_(None), UserApiKey.expiry_notified_days > days),
            )
            for days in access.EXPIRY_NOTICE_DAYS
        )
    )
    rows = (
        await db.execute(
            select(UserApiKey, User)
            .join(User, User.id == UserApiKey.user_id)
            .where(
                UserApiKey.is_active.is_(True),
                User.is_active.is_(True),
                UserApiKey.expires_at.is_not(None),
                UserApiKey.expires_at > now,
                unsent,
            )
            .order_by(UserApiKey.expires_at)
            .limit(_SCAN_LIMIT)
            .with_for_update(of=UserApiKey, skip_locked=True)
        )
    ).all()
    sent = 0
    for key, owner in rows:
        due = access.due_expiry_notice(key.expires_at, now)
        if due is None or (key.expiry_notified_days is not None and key.expiry_notified_days <= due):
            continue
        key.expiry_notified_days = due
        expires_on = key.expires_at.astimezone(UTC).date().isoformat()
        await notify_web_user(
            db,
            str(owner.id),
            title=f"Your API key '{key.name}' expires within {_days(due)}",
            summary=(
                f"It stops working on {expires_on} (UTC). Give it a later expiry or create a new key "
                "in My Profile > API Keys."
            ),
            source_label=SOURCE_LABEL,
            extra_data={
                "notification_source": NOTIFICATION_SOURCE,
                "api_key_id": str(key.id),
                "path": PROFILE_PATH,
                "expires_at": key.expires_at.isoformat(),
            },
        )
        sent += 1
    await db.commit()
    if sent:
        logger.info("API keys: %d expiry notice(s) sent", sent)
    return sent
