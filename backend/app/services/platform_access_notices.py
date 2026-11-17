# SPDX-License-Identifier: Apache-2.0

"""Access notices: a registered platform tells XTM One it gave someone access.

XTM One then emails that person: an access email when they have an account, an
invitation to create one when they do not and the acting user may invite. The
platform names the recipient (their XTM One id, the ``sub`` of its sign-in, or
their email) and the place it opened (a tenant name); the email comes from a
fixed template and links only to XTM One's registration page or the platform's
own public URL, so this cannot relay arbitrary mail or send someone to another
site.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.platform_identity import RegisteredPlatform, platform_for_issuer
from app.models.platform_registration import PlatformRegistration
from app.models.user import User

#: A path on the platform: absolute, never ``//`` (a protocol-relative URL to
#: another host), and only characters a link carries as written.
NOTICE_PATH = re.compile(r"/(?!/)[A-Za-z0-9\-._~%!$&*+,;=:@/?#]*")


@dataclass(frozen=True)
class NoticePlatform:
    """The registered platform sending a notice: its registration identifier, and how its recipient is shown it."""

    identifier: str
    title: str
    public_url: str


async def acting_platform(db: AsyncSession, user: User) -> NoticePlatform | None:
    """The active registration that signed *user*'s token, ``None`` when no registered platform did.

    Only a cross-platform JWT carries a verified issuer: a session, an API key
    or a platform whose registration is gone or names no public URL is refused.
    """
    if getattr(user, "auth_credential_kind", None) != "platform_jwt":
        return None
    issuer = getattr(user, "platform_issuer", None)
    if not issuer:
        return None
    platform = await platform_for_issuer(db, issuer)
    if platform is None or platform.public_url is None:
        return None
    title = (
        await db.execute(
            select(PlatformRegistration.platform_title).where(
                PlatformRegistration.platform_identifier == platform.platform_identifier
            )
        )
    ).scalar_one_or_none()
    return NoticePlatform(
        identifier=platform.platform_identifier,
        title=title or platform.platform_identifier,
        public_url=platform.public_url,
    )


def notice_link(public_url: str, path: str) -> str:
    """*path* (matched by :data:`NOTICE_PATH`) on the platform's public URL."""
    return f"{public_url.rstrip('/')}{path}"


def may_invite(user: User) -> bool:
    """Whether *user*, acting through a registered platform, may invite someone with no account.

    The one place this is decided: an administrator today.
    """
    return bool(user.is_admin)


def invitation_destination(platform: NoticePlatform, path: str) -> dict:
    """What an invitation stores of where its person goes once registered (:func:`registered_destination`)."""
    return {"platform": platform.identifier, "path": path}


def notice_made(invitation: dict) -> bool:
    """Whether a pending *invitation* is one a notice made, which another notice may send again.

    Role ``user`` with a destination; an invitation an administrator made on the
    Invitations page is never reissued, so its role and link stay as made.
    """
    return invitation.get("role") == "user" and isinstance(invitation.get("destination"), dict)


async def registered_destination(db: AsyncSession, destination: object) -> str | None:
    """Where a person who registered from an invitation goes next.

    The path the invitation stored on its platform's public URL, read from the
    registration as it stands now: ``None`` for an invitation no platform made,
    or whose platform is no longer registered or names no public URL.
    """
    if not isinstance(destination, dict):
        return None
    identifier, path = destination.get("platform"), destination.get("path")
    if not isinstance(identifier, str) or not isinstance(path, str) or not NOTICE_PATH.fullmatch(path):
        return None
    platform_url = (
        await db.execute(
            select(PlatformRegistration.platform_url).where(
                PlatformRegistration.platform_identifier == identifier,
                PlatformRegistration.status == "active",
            )
        )
    ).scalar_one_or_none()
    if platform_url is None:
        return None
    public_url = RegisteredPlatform(platform_identifier=identifier, platform_url=platform_url, api_url=None).public_url
    return notice_link(public_url, path) if public_url else None
