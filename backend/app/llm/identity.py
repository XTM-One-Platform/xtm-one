# SPDX-License-Identifier: Apache-2.0

"""Caller identity for outbound LLM calls.

The Filigran AI Gateway authenticates with per-request XTM Suite JWTs whose
``sub``/``email`` claims identify the acting user. Providers are shared,
user-agnostic instances (see the router's provider cache), so the identity
travels out-of-band through a :class:`~contextvars.ContextVar`: the router
sets it where the user context is known, and the gateway provider's auth
hook reads it at request time. asyncio context is per-task and inherited by
child tasks, so the value set during request handling is visible to every
LLM call made on behalf of that request (including sub-agent tasks).

A job run outside any request binds the person it acts for
(:func:`bind_llm_caller`). Background/system work with nobody to act for
(company-managed KB ingestion, title generation, scheduled runs without a
user) falls back to the **service identity** — the platform admin account,
set by the platform's bootstrap and looked up when any other process starts
(:func:`load_service_identity`), which the worker keeps current
(:func:`keep_service_identity_current`). The auth hook only reads it: no
gateway request waits on the database.
Gateway-side quotas and usage attribute those calls to that account.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMCallerIdentity:
    """The user an outbound LLM call is made on behalf of."""

    user_id: uuid.UUID
    email: str
    source: str = "xtm-one"


_llm_caller: ContextVar[LLMCallerIdentity | None] = ContextVar(
    "llm_caller", default=None
)
_service_identity: LLMCallerIdentity | None = None

# The worker looks the service identity up again this often: a re-created
# admin or a changed ``ADMIN_EMAIL`` is picked up without a restart.
SERVICE_IDENTITY_REFRESH_SECONDS = 600.0
# And this often while it has none (the platform has not created the admin
# yet): a job right after the admin appears is refused for at most this long.
SERVICE_IDENTITY_RETRY_SECONDS = 10.0


def set_llm_caller(user_id: uuid.UUID | str, email: str) -> None:
    """Bind the acting user to the current async context.

    Silently ignores unparseable ids (e.g. ``"anonymous"``, ``"system"``) —
    those calls fall back to the service identity.
    """
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(user_id)
    except (ValueError, TypeError):
        return
    if not email:
        return
    _llm_caller.set(LLMCallerIdentity(user_id=uid, email=email))


@contextmanager
def llm_caller_scope() -> Iterator[None]:
    """Unbind, on exit, whoever this block bound.

    A job that binds a person for one step (a document's embed) would
    otherwise sign its next steps in the same task as that person.
    """
    token = _llm_caller.set(_llm_caller.get())
    try:
        yield
    finally:
        _llm_caller.reset(token)


def set_service_identity(user_id: uuid.UUID, email: str) -> None:
    """Register the platform service account (called once at startup)."""
    global _service_identity
    _service_identity = LLMCallerIdentity(user_id=user_id, email=email)
    logger.info("LLM service identity set: %s", email)


def resolve_llm_caller() -> LLMCallerIdentity:
    """The identity to sign outbound gateway JWTs with.

    Context-bound user first, then the service identity.

    Raises:
        RuntimeError: when neither is available (startup not finished).
    """
    caller = _llm_caller.get()
    if caller is not None:
        return caller
    if _service_identity is not None:
        return _service_identity
    raise RuntimeError(
        "No LLM caller identity available: no user bound to this context and "
        "the service identity has not been initialized yet."
    )


async def service_account_id(db) -> uuid.UUID | None:
    """The id of the admin account (``ADMIN_EMAIL``) the service identity names.

    ``None`` until the platform's bootstrap has created it.
    """
    from sqlalchemy import func, select

    from app.core.config import settings
    from app.models.user import User

    return (
        await db.execute(select(User.id).where(func.lower(User.email) == settings.admin_email))
    ).scalar_one_or_none()


async def load_service_identity() -> bool:
    """Look the service identity up, in a process the bootstrap did not run in.

    Only the platform's startup calls :func:`set_service_identity`, so the SAQ
    worker had none: every gateway call it made without a bound user (KB
    ingestion, re-embedding) failed in the auth hook before it was sent, and
    the OpenAI SDK reported it as "Connection error.". Never raises; a failed
    lookup keeps the identity already loaded.
    """
    from app.core.config import settings
    from app.core.database import async_session
    from app.core.utils import format_exception_message

    try:
        async with async_session() as db:
            admin_id = await service_account_id(db)
    except Exception as exc:
        logger.warning("Could not load the LLM service identity: %s", format_exception_message(exc))
        return False
    if admin_id is None:
        logger.warning("LLM service identity not set: no admin user %s yet", settings.admin_email)
        return False
    if _service_identity != LLMCallerIdentity(user_id=admin_id, email=settings.admin_email):
        set_service_identity(admin_id, settings.admin_email)
    return True


async def keep_service_identity_current() -> None:
    """The worker's loop: retry soon while no identity is loaded, refresh rarely once one is."""
    while True:
        await asyncio.sleep(
            SERVICE_IDENTITY_RETRY_SECONDS if _service_identity is None else SERVICE_IDENTITY_REFRESH_SECONDS
        )
        await load_service_identity()


async def bind_llm_caller(db, user_id: object) -> bool:
    """Bind the person a job run outside any request acts for, by id.

    A job has no request context, so nothing bound a caller: the gateway
    would sign its calls as the service identity, and charge its quotas to
    the admin. Binds nothing (``False``) for no id, an id that is not a UUID,
    or one that names no user or a deactivated one: those calls sign as the
    service identity, and a deactivated person's quota is not charged.
    """
    from app.models.user import User

    if not user_id:
        return False
    try:
        uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    except ValueError:
        return False
    # ``get``: free when the job already loaded the user.
    user = await db.get(User, uid)
    if user is None or not user.is_active:
        return False
    set_llm_caller(user.id, user.email)
    return True
