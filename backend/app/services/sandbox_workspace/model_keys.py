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

"""A live model for a lab: a short-lived key of the platform's own LLM proxy, minted per lab.

A lab runs the stub model of its checkout unless one of its services opts in with
``LAB_MODEL = "live"`` in its declared ``env``. At the start of such a lab,
:func:`mint` creates one personal API key (``fcp-...``) of the account
``AGENTIC_LABS_MODEL_KEY_ACCOUNT``, scoped to the LLM proxy surface only and
expiring after ``AGENTIC_LABS_MODEL_KEY_HOURS``, and hands every container of the
pod three variables: ``LAB_MODEL_API_KEY`` (the key), ``LAB_MODEL_BASE_URL``
(``AGENTIC_LABS_MODEL_BASE_URL``, the proxy's OpenAI-compatible ``/v1``) and
``LAB_MODEL_NAMES`` (``AGENTIC_LABS_MODEL_NAMES``). No provider credential ever
reaches a pod: the model is reached through the proxy, which meters the key like
any other. The key is revoked when the lab stops, is replaced or is reaped; its
expiry is the backstop. Off while the account or the URL is unset: the lab then
starts with its stub model, and the start answer says why.

What is recorded (the lab's ``kernel_state``, the pod annotation, the answer) is the
key's id and display prefix, never the key. ``dev-docs/reference/agentic-labs.md``,
"A live model (option A)".
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services.sandbox_workspace.services import ResolvedService

logger = logging.getLogger(__name__)

#: The declared ``env`` switch a service opts in with.
OPT_IN_VAR = "LAB_MODEL"
OPT_IN_VALUE = "live"
#: The variables every container of an opted-in lab gets.
KEY_VAR = "LAB_MODEL_API_KEY"
URL_VAR = "LAB_MODEL_BASE_URL"
NAMES_VAR = "LAB_MODEL_NAMES"
#: Bounds on the key's life, in hours.
MAX_HOURS = 72
#: The name every minted key carries, followed by the pod name.
KEY_NAME_PREFIX = "agentic lab "


@dataclass
class Minted:
    """What :func:`mint` gave a lab: the variables for its containers, the key's id, or why not."""

    env: dict[str, str] = field(default_factory=dict)
    key_id: str = ""
    prefix: str = ""
    expires_at: str = ""
    why_not: str = ""

    def summary(self) -> str:
        """One line for the start answer, never the key."""
        if self.key_id:
            return (
                f"Live model: {KEY_VAR} ({self.prefix}, LLM proxy only, expires {self.expires_at}), "
                f"{URL_VAR}, {NAMES_VAR}={self.env.get(NAMES_VAR, '')}"
            )
        return f"Live model: not set ({self.why_not}); the lab runs its stub model." if self.why_not else ""


def opted_in(resolved: ResolvedService) -> bool:
    """Whether a service of *resolved*'s chain declares ``LAB_MODEL = "live"``."""
    return any(dict(spec.env).get(OPT_IN_VAR, "").strip().lower() == OPT_IN_VALUE for spec in resolved.chain)


def configured() -> bool:
    from app.core.config import settings

    return bool((settings.agentic_labs_model_key_account or "").strip()) and bool(
        (settings.agentic_labs_model_base_url or "").strip()
    )


def _hours() -> int:
    from app.core.config import settings

    return max(1, min(int(settings.agentic_labs_model_key_hours or 12), MAX_HOURS))


async def _create(db: Any, *, account: str, name: str, now: datetime) -> tuple[Any, str] | str:
    """The new key row and the raw key, or why none was created. Commits nothing."""
    from sqlalchemy import func, select

    from app.core.security import bcrypt_hashpw_async
    from app.models.api_key import UserApiKey
    from app.models.user import User
    from app.services import api_key_access as access

    user = (
        await db.execute(select(User).where(func.lower(User.email) == account.strip().lower()))
    ).scalar_one_or_none()
    if user is None:
        return (
            f"no user of this platform signs in as {account} (AGENTIC_LABS_MODEL_KEY_ACCOUNT names a platform "
            "user's email, not a mailbox or an integration account)"
        )
    if not user.is_active:
        return f"the platform user {account} is disabled"
    raw = access.generate_api_key()
    row = UserApiKey(
        user_id=user.id,
        name=name[:255],
        key_prefix=access.display_prefix(raw),
        key_hash=(await bcrypt_hashpw_async(raw.encode("utf-8"))).decode("utf-8"),
        surfaces=[access.ApiKeySurface.LLM_PROXY.value],
        read_only=False,
        expires_at=now + timedelta(hours=_hours()),
        # Inside the shortest notice: no expiry mail for a key nobody keeps.
        expiry_notified_days=1,
        created_at=now,
    )
    db.add(row)
    await db.flush()
    return row, raw


async def mint(resolved: ResolvedService, *, pod: str, session_factory: Any = None) -> Minted:
    """A key for *pod* when *resolved* opts in and the platform is configured; never raises."""
    from app.core.config import settings

    if not opted_in(resolved):
        return Minted()
    if not configured():
        return Minted(why_not="AGENTIC_LABS_MODEL_KEY_ACCOUNT or AGENTIC_LABS_MODEL_BASE_URL is not set")
    if session_factory is None:
        from app.core.database import async_session as session_factory
    now = datetime.now(UTC)
    try:
        async with session_factory() as db:
            created = await _create(
                db, account=settings.agentic_labs_model_key_account, name=KEY_NAME_PREFIX + pod, now=now
            )
            if isinstance(created, str):
                return Minted(why_not=created)
            row, raw = created
            await db.commit()
    except Exception as exc:  # a lab never fails for its model key
        logger.warning("Agentic labs: no model key for %s: %s", pod, type(exc).__name__)
        return Minted(why_not=f"the key could not be created ({type(exc).__name__})")
    env = {
        KEY_VAR: raw,
        URL_VAR: settings.agentic_labs_model_base_url.strip().rstrip("/"),
        NAMES_VAR: (settings.agentic_labs_model_names or "").strip(),
    }
    return Minted(
        env=env, key_id=str(row.id), prefix=row.key_prefix, expires_at=row.expires_at.isoformat(timespec="minutes")
    )


async def revoke(key_id: str, *, session_factory: Any = None) -> bool:
    """Revoke the lab key *key_id* (only a key this module minted); whether one was revoked. Never raises."""
    if not key_id:
        return False
    try:
        wanted = uuid.UUID(str(key_id))
    except ValueError:
        return False
    if session_factory is None:
        from app.core.database import async_session as session_factory
    try:
        from sqlalchemy import select

        from app.models.api_key import UserApiKey

        async with session_factory() as db:
            row = (await db.execute(select(UserApiKey).where(UserApiKey.id == wanted))).scalar_one_or_none()
            if row is None or not row.is_active or not str(row.name).startswith(KEY_NAME_PREFIX):
                return False
            row.is_active = False
            row.revoked_at = datetime.now(UTC)
            await db.commit()
            return True
    except Exception as exc:
        logger.warning("Agentic labs: model key %s not revoked: %s", key_id, type(exc).__name__)
        return False
