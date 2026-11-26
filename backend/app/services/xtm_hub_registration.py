# SPDX-License-Identifier: Apache-2.0

"""XTM One's registration with the XTM Hub.

An administrator registers the platform from Settings: the XTM Hub page
``/redirect/register-xtmone`` (the Hub's own login and organization choice)
creates the registration and posts the platform token back to the window
that opened it, which sends it here. The token is stored only once the Hub
confirms it (``refreshPlatformRegistrationConnectivityStatus`` answers
``active``), encrypted, bound to that Hub and to this platform's id.

Every hour the heartbeat asks the Hub again, declaring the platform's
edition as its contract (``EE`` or ``CE``, as the manual registration does),
so the Hub follows a licence installed or removed after the registration.
``active`` keeps it registered, ``inactive`` or an unreachable Hub marks it
``lost_connectivity`` (kept, retried), ``not_found`` (the registration was
removed on the Hub) forgets it.
A platform id that changed since the registration (``PLATFORM_ID`` set later,
a restored or cloned database) marks it ``needs_reregistration`` and sends
nothing; the heartbeat and the manual check both look again, and once the id
matches again the Hub's answer decides. Every write is a compare-and-set on ``generation``, so a heartbeat
that started before a new registration never overwrites or deletes it.

A platform the XTM Platform deployed in a Hub trial is registered without an
administrator: the deployer hands ``auto_register()`` the platform token the
Hub issued for it, and XTM One calls the Hub's ``autoRegisterPlatform`` with
it (``XTM-Hub-Platform-Id`` / ``XTM-Hub-Platform-Token``) at
``XTM_HUB_DEFAULT_URL``, like OpenCTI's ``autoRegisterOpenCTI`` and OpenAEV's
``/api/xtmhub/auto-register``. The Hub knows the platform by the id the
deployer reported, so ``PLATFORM_ID`` must be that id.

The Hub's answers go through ``xtm_hub_graphql()`` (bounded, redirect-free,
fixed error sentences); nothing here ever logs, audits or serves the token.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session
from app.core.log_once import fingerprint, log_once
from app.core.redis import redis_client
from app.core.security import decrypt_value, encrypt_value
from app.core.utils import format_exception_message
from app.core.xtm_hub_key import PLATFORM_IDENTIFIER
from app.integrations.providers import xtm_hub as hub
from app.models.user import User
from app.models.xtm_hub_registration import (
    SINGLETON_ID,
    STATUS_LOST_CONNECTIVITY,
    STATUS_NEEDS_REREGISTRATION,
    STATUS_REGISTERED,
    XtmHubRegistration,
)

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 3600
HEARTBEAT_LEASE_KEY = "xtm_hub:registration_heartbeat"
_SYSTEM_ACTOR = "xtm_hub_heartbeat"
_VERSION_CORE_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

REFRESH_MUTATION = """
mutation XtmOneRegistrationStatus($input: RefreshPlatformRegistrationConnectivityStatusInput!) {
  refreshPlatformRegistrationConnectivityStatus(input: $input) { status }
}
"""

AUTO_REGISTER_MUTATION = """
mutation XtmOneAutoRegister($input: AutoRegisterPlatformInput) {
  autoRegisterPlatform(input: $input) { success }
}
"""

CheckResult = Literal["active", "inactive", "not_found", "unreachable", "refused"]
Contract = Literal["EE", "CE"]

_running: set[asyncio.Task] = set()


class RegistrationRefusedError(Exception):
    """A registration that was not stored; ``str()`` is safe to show an admin."""


@dataclass(frozen=True)
class RegistrationView:
    """What the API serves about the registration: never the token."""

    status: str
    hub_url: str | None
    registered_at: datetime | None
    last_check_at: datetime | None
    last_check_result: str | None


UNREGISTERED = RegistrationView("unregistered", None, None, None, None)


def registration_platform_version() -> str:
    """The ``x.y.z`` core of the application version, as the Hub requires.

    The Hub refuses anything but ``x.y.z`` (or ``x.y.z-ltsN``); a development
    build (``0.0.0-dev``, ``1.260513.0-dev.abc1234``) is sent as its core.
    """
    match = _VERSION_CORE_RE.match((settings.app_version or "").strip())
    if not match:
        return "0.0.0"
    return ".".join(str(int(part)) for part in match.groups())


def registration_hub_url(raw: object) -> str:
    """The Hub URL a registration may be bound to, ``""`` when refused.

    Absolute http(s) without credentials, query or fragment
    (``xtm_hub_base_url``), and HTTPS unless the Hub runs on this machine:
    the token travels to that address on every heartbeat.
    """
    return hub.xtm_hub_token_url(raw)


async def registration_contract(db: AsyncSession) -> Contract:
    """The contract this platform declares to the Hub: ``EE`` in Enterprise Edition, else ``CE``.

    The edition the platform reports everywhere (``get_deployment_edition``),
    as the Settings card sends it to the Hub's register page.
    """
    from app.api.v1.platform import EDITION_ENTERPRISE, get_deployment_edition

    return "EE" if await get_deployment_edition(db) == EDITION_ENTERPRISE else "CE"


async def check_registration(
    hub_url: str, platform_id: uuid.UUID, token: str, contract: Contract
) -> CheckResult:
    """What the Hub says about this platform's registration under *token*.

    *contract* is declared with it: the Hub records a move between ``CE`` and
    ``EE`` and keeps the contract of a trial as it is.
    """
    data, err = await hub.xtm_hub_graphql(
        hub_url,
        REFRESH_MUTATION,
        {
            "input": {
                "platformId": str(platform_id),
                "token": token,
                "platformVersion": registration_platform_version(),
                "platformIdentifier": PLATFORM_IDENTIFIER,
                "platformContract": contract,
            }
        },
        "check the platform registration",
    )
    if hub.is_transient_error(err):
        return "unreachable"
    if err:
        return "refused"
    answer = (data or {}).get("refreshPlatformRegistrationConnectivityStatus")
    status = answer.get("status") if isinstance(answer, dict) else None
    if status in ("active", "inactive", "not_found"):
        return status
    logger.warning("XTM Hub registration check: unexpected status %r", str(status)[:40])
    return "refused"


async def current_platform_id(db: AsyncSession) -> uuid.UUID:
    """This platform's id: the ``PlatformSettings`` row's (``PLATFORM_ID`` when set)."""
    from app.api.v1.platform_settings_helpers import get_platform_settings

    return (await get_platform_settings(db)).id


async def load_registration(db: AsyncSession) -> XtmHubRegistration | None:
    return await db.get(XtmHubRegistration, SINGLETON_ID)


def view_of(registration: XtmHubRegistration | None) -> RegistrationView:
    if registration is None:
        return UNREGISTERED
    return RegistrationView(
        status=registration.status,
        hub_url=registration.hub_url,
        registered_at=registration.registered_at,
        last_check_at=registration.last_check_at,
        last_check_result=registration.last_check_result,
    )


async def register(db: AsyncSession, *, hub_url: str, token: str) -> XtmHubRegistration:
    """Verify *token* with the Hub, then store it; the caller audits and commits.

    Raises ``RegistrationRefusedError`` (a sentence safe to show) when the Hub URL is
    refused or the Hub does not confirm an active registration; nothing is
    written then.
    """
    bound_url = registration_hub_url(hub_url)
    if not bound_url:
        raise RegistrationRefusedError(
            "The XTM Hub URL must be an https URL (http only for a Hub on this machine)."
        )
    platform_id = await current_platform_id(db)
    result = await check_registration(bound_url, platform_id, token, await registration_contract(db))
    if result == "unreachable":
        raise RegistrationRefusedError("The XTM Hub could not be reached. Retry shortly.")
    if result != "active":
        raise RegistrationRefusedError(
            "The XTM Hub did not confirm this registration. Register again from the XTM Hub page."
        )
    return await _store_registration(db, hub_url=bound_url, platform_id=platform_id, token=token)


async def existing_users_count(db: AsyncSession) -> int:
    """The accounts the Hub counts for a new registration: every user, active or not, but the bootstrap admin.

    ``ADMIN_EMAIL`` is the account the deployer creates, not a person of the
    organization. The one place to leave out other non-human accounts.
    """
    count = await db.scalar(
        select(func.count()).select_from(User).where(func.lower(User.email) != settings.admin_email)
    )
    return int(count or 0)


async def auto_register_contract(db: AsyncSession) -> str:
    """The contract an automatic registration declares: the Filigran licence's type, sent as is.

    As OpenCTI's ``autoRegisterOpenCTI``. Only the deployer of a Hub trial
    calls it, whose licence is a ``trial`` one; the Hub refuses a type it does
    not know (``standard``, ``lts``...). Without a valid licence, ``CE``.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.services.xtm_license import resolve_xtm_license

    info = resolve_xtm_license(await get_platform_settings(db))
    return info.license_type if info.license_validated else "CE"


async def auto_register(db: AsyncSession, *, token: str) -> XtmHubRegistration:
    """Register this platform with ``XTM_HUB_DEFAULT_URL`` under the Hub's *token*; the caller audits and commits.

    Raises ``RegistrationRefusedError`` (a sentence safe to show) when the
    default Hub URL cannot carry a token, the Hub cannot be reached or does
    not accept the registration; nothing is written then. Once stored, the
    hourly heartbeat checks it like a manual registration.
    """
    from app.services.branding import get_cached_branding

    bound_url = registration_hub_url(settings.xtm_hub_default_url)
    if not bound_url:
        raise RegistrationRefusedError(
            "XTM_HUB_DEFAULT_URL must be an https URL (http only for an XTM Hub on this machine)."
        )
    platform_id = await current_platform_id(db)
    variables = {
        "input": {
            "platform": {
                "id": str(platform_id),
                "url": settings.base_url,
                "title": get_cached_branding().name,
                "contract": await auto_register_contract(db),
                "version": registration_platform_version(),
            },
            "existing_users_count": await existing_users_count(db),
        }
    }
    data, err = await hub.xtm_hub_graphql(
        bound_url,
        AUTO_REGISTER_MUTATION,
        variables,
        "register the platform automatically",
        platform_token=(str(platform_id), token),
    )
    if hub.is_transient_error(err):
        raise RegistrationRefusedError("The XTM Hub could not be reached. Retry shortly.")
    answer = (data or {}).get("autoRegisterPlatform")
    if err or not isinstance(answer, dict) or answer.get("success") is not True:
        raise RegistrationRefusedError(
            "The XTM Hub did not accept this registration: check the platform token and PLATFORM_ID."
        )
    return await _store_registration(db, hub_url=bound_url, platform_id=platform_id, token=token)


async def _store_registration(
    db: AsyncSession, *, hub_url: str, platform_id: uuid.UUID, token: str
) -> XtmHubRegistration:
    """Store a registration the Hub just confirmed, replacing any other."""
    now = datetime.now(UTC)
    values = {
        "hub_url": hub_url,
        "platform_id": platform_id,
        "token_enc": encrypt_value(token),
        "status": STATUS_REGISTERED,
        "registered_at": now,
        "last_check_at": now,
        "last_check_result": "active",
    }
    # One atomic upsert: two registrations at once each get their own
    # generation (the row lock orders them), so a heartbeat that snapshotted
    # one can never match, and overwrite or delete, the other.
    statement = pg_insert(XtmHubRegistration).values(id=SINGLETON_ID, generation=1, **values)
    statement = statement.on_conflict_do_update(
        index_elements=[XtmHubRegistration.id],
        set_={
            **{key: statement.excluded[key] for key in values},
            "generation": XtmHubRegistration.generation + 1,
        },
    )
    await db.execute(statement)
    registration = await db.get(XtmHubRegistration, SINGLETON_ID, populate_existing=True)
    if registration is None:  # pragma: no cover - the upsert just wrote it
        raise RuntimeError("the XTM Hub registration was not stored")
    return registration


async def forget(db: AsyncSession) -> bool:
    """Drop the local registration; the caller audits and commits. Whether there was one."""
    result = await db.execute(delete(XtmHubRegistration).where(XtmHubRegistration.id == SINGLETON_ID))
    return bool(result.rowcount)


def status_after(result: CheckResult) -> str | None:
    """The status a check result leads to; ``None`` = forget the registration."""
    if result == "active":
        return STATUS_REGISTERED
    if result == "not_found":
        return None
    return STATUS_LOST_CONNECTIVITY


async def check_now(db: AsyncSession) -> RegistrationView:
    """Ask the Hub now and record its answer (compare-and-set); the caller commits.

    Like the heartbeat, no transaction is open while the Hub answers: once
    the registration and the platform id are read, the read transaction is
    committed (the caller holds no pending write; the session does not
    expire on commit, so what it loaded stays readable) and the answer is
    recorded in a new one.
    """
    registration = await load_registration(db)
    if registration is None:
        return UNREGISTERED
    snapshot = _Snapshot.of(registration)
    platform_id = await current_platform_id(db)
    contract = await registration_contract(db)
    await db.commit()
    outcome = await _check_snapshot(snapshot, platform_id, contract)
    # A miss means a newer registration replaced this one: show it as it is.
    await _apply(db, snapshot, outcome)
    return view_of(await db.get(XtmHubRegistration, SINGLETON_ID, populate_existing=True))


@dataclass(frozen=True)
class _Snapshot:
    generation: int
    hub_url: str
    platform_id: uuid.UUID
    token_enc: str
    status: str

    @classmethod
    def of(cls, registration: XtmHubRegistration) -> _Snapshot:
        return cls(
            registration.generation,
            registration.hub_url,
            registration.platform_id,
            registration.token_enc,
            registration.status,
        )


async def _check_snapshot(
    snapshot: _Snapshot, platform_id: uuid.UUID, contract: Contract
) -> CheckResult | Literal["platform_changed"]:
    """The Hub's answer for *snapshot*, read with no database access.

    *platform_id* is this platform's current id: a registration made for
    another one sends nothing. *contract* is the one it declares.
    """
    if snapshot.platform_id != platform_id:
        return "platform_changed"
    if not registration_hub_url(snapshot.hub_url):
        return "refused"
    try:
        token = decrypt_value(snapshot.token_enc)
    except Exception as exc:  # pragma: no cover - a corrupted ciphertext
        logger.warning("XTM Hub registration token unreadable: %s", format_exception_message(exc))
        return "refused"
    return await check_registration(snapshot.hub_url, snapshot.platform_id, token, contract)


async def _apply(
    db: AsyncSession, snapshot: _Snapshot, outcome: CheckResult | Literal["platform_changed"]
) -> tuple[bool, str | None]:
    """Record *outcome* unless a newer registration replaced *snapshot*.

    ``(applied, new_status)``; ``new_status`` ``None`` = forgotten. When a newer
    registration replaced the snapshot nothing is written: ``applied`` is
    False and the status is not this check's to report.
    """
    current = XtmHubRegistration.generation == snapshot.generation
    now = datetime.now(UTC)
    if outcome == "platform_changed":
        new_status: str | None = STATUS_NEEDS_REREGISTRATION
    else:
        new_status = status_after(outcome)
    if new_status is None:
        result = await db.execute(delete(XtmHubRegistration).where(current))
    else:
        result = await db.execute(
            update(XtmHubRegistration)
            .where(current)
            .values(
                status=new_status,
                last_check_at=now,
                last_check_result=outcome if outcome != "platform_changed" else None,
            )
        )
    return bool(result.rowcount), new_status


async def heartbeat_if_due() -> bool:
    """Start the hourly heartbeat when this process takes the lease; whether it did.

    The check runs in the background: the scheduler tick must never wait on
    the Hub. A Redis failure skips this tick (the next one asks again).
    """
    try:
        redis = await redis_client()
        if not await redis.set(HEARTBEAT_LEASE_KEY, "1", nx=True, ex=HEARTBEAT_INTERVAL_SECONDS):
            return False
    except Exception as exc:
        logger.debug("XTM Hub heartbeat lease not taken: %s", format_exception_message(exc))
        return False
    task = asyncio.create_task(_heartbeat_logged(), name="xtm-hub-registration-heartbeat")
    _running.add(task)
    task.add_done_callback(_running.discard)
    return True


async def _heartbeat_logged() -> None:
    try:
        await heartbeat()
    except Exception as exc:
        logger.error("XTM Hub registration heartbeat error: %s", format_exception_message(exc), exc_info=True)


async def heartbeat() -> str | None:
    """One heartbeat: check the registration and record the answer; the status it left, if any."""
    async with async_session() as db:
        registration = await load_registration(db)
        if registration is None:
            return None
        snapshot = _Snapshot.of(registration)
        platform_id = await current_platform_id(db)
        contract = await registration_contract(db)
        # Everything the check needs is read: the session holds no
        # transaction across the network call.
        await db.rollback()
        outcome = await _check_snapshot(snapshot, platform_id, contract)
        if outcome == "unreachable":
            await log_once(
                logger,
                logging.WARNING,
                f"xtm_hub:heartbeat_unreachable:{fingerprint(snapshot.hub_url)}",
                "The XTM Hub at %s could not be reached to check this platform's registration.",
                snapshot.hub_url,
                interval=6 * HEARTBEAT_INTERVAL_SECONDS,
            )
        applied, new_status = await _apply(db, snapshot, outcome)
        if not applied:
            # A newer registration replaced the one checked: nothing was
            # written, so there is no transition to audit; report the row as
            # it now is.
            await db.commit()
            current = await load_registration(db)
            return current.status if current is not None else None
        if new_status != snapshot.status:
            await _audit_transition(db, snapshot, new_status, outcome)
        await db.commit()
        return new_status


async def _audit_transition(
    db: AsyncSession, snapshot: _Snapshot, new_status: str | None, outcome: str
) -> None:
    from app.services.audit import log_system_audit

    anchor = (
        await db.execute(select(User.id).where(User.role == "admin").order_by(User.created_at, User.id).limit(1))
    ).scalar_one_or_none()
    if anchor is None:
        return
    if new_status is None:
        summary = "The XTM Hub no longer knows this platform's registration: forgot it locally"
    else:
        summary = f"XTM Hub registration is now {new_status.replace('_', ' ')}"
    await log_system_audit(
        db,
        actor=_SYSTEM_ACTOR,
        anchor_user_id=anchor,
        action="update" if new_status else "delete",
        entity_type="xtm_hub_registration",
        entity_name="XTM Hub registration",
        summary=summary,
        details={"previous_status": snapshot.status, "status": new_status, "hub_answer": outcome},
    )
