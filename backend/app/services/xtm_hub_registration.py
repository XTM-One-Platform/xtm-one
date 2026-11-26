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

Once the Hub has enrolled this instance's public key (``key_enrolled_at``,
set by the enrollment), every call is signed instead: a fresh, single-use
assertion in ``XTM-Hub-Platform-Assertion`` (``core/xtm_hub_key.py``) and no
token. ``PlatformCredential`` makes that choice for every platform call, and
``enroll_platform_key()`` is the one call that carries both, to prove the
instance holds the key it enrolls. The Hub's refusals of a platform credential
are read in one place (``PLATFORM_REFUSALS``).

The Hub's answers go through ``xtm_hub_graphql()`` (bounded, redirect-free,
fixed error sentences); nothing here ever logs, audits or serves the token.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import async_session
from app.core.log_once import fingerprint, log_once
from app.core.redis import redis_client
from app.core.security import decrypt_value, encrypt_value
from app.core.utils import format_exception_message
from app.core.xtm_hub_key import (
    PLATFORM_IDENTIFIER,
    XtmHubKey,
    XtmHubKeyUnavailableError,
    derive_key,
    key_version,
)
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

ENROLL_MUTATION = """
mutation XtmOneEnrollPlatformKey($input: EnrollPlatformKeyInput!) {
  enrollPlatformKey(input: $input) { status }
}
"""

CheckResult = Literal[
    "active", "inactive", "not_found", "unreachable", "refused", "key_mismatch", "token_path_closed"
]
Contract = Literal["EE", "CE"]

#: What the Hub answers an enrollment. ``enrolled``: the key is stored (again,
#: for a retry), the token can go. ``conflict``: another key is enrolled for
#: this registration. ``not_found``: no registration matches the token.
#: ``key_mismatch``: the assertion's key is not the one the Hub holds.
#: ``not_available``: the Hub does not enroll keys yet. ``unknown_operation``:
#: an older Hub without ``enrollPlatformKey``. ``unreachable``: a network
#: failure, a timeout or a 5xx. ``refused``: any other refusal or answer.
#: ``not_available``, ``unknown_operation`` and ``unreachable`` keep the token
#: for a later try.
EnrollmentOutcome = Literal[
    "enrolled",
    "conflict",
    "not_found",
    "key_mismatch",
    "not_available",
    "unknown_operation",
    "unreachable",
    "refused",
]
#: The answers of ``enrollPlatformKey`` that are outcomes as they are.
_ENROLLMENT_STATUSES: dict[str, EnrollmentOutcome] = {
    "enrolled": "enrolled",
    "conflict": "conflict",
    "not_found": "not_found",
    "key_mismatch": "key_mismatch",
    "not_available": "not_available",
}

PlatformRefusal = Literal["assertion_invalid", "key_mismatch", "token_refused", "token_path_closed"]

#: The Hub's refusals of a platform credential, by error code: the one place
#: they are read. ``assertion_invalid``: a signature, ``typ``, ``aud``,
#: expiry or replayed ``jti`` the Hub refused. ``key_mismatch``: the key that
#: signed is not the one enrolled (``SECRET_KEY`` or the key version
#: changed). ``token_refused``: the Hub knows no registration under that
#: token. ``token_path_closed``: the Hub no longer takes platform tokens.
PLATFORM_REFUSALS: dict[str, PlatformRefusal] = {
    "PLATFORM_ASSERTION_INVALID": "assertion_invalid",
    "PLATFORM_KEY_MISMATCH": "key_mismatch",
    "PLATFORM_TOKEN_REFUSED": "token_refused",
    "PLATFORM_TOKEN_PATH_CLOSED": "token_path_closed",
}

#: The GraphQL code of a document the Hub's schema does not validate: an
#: operation, argument or input type an older Hub does not have.
_UNKNOWN_OPERATION_CODE = "GRAPHQL_VALIDATION_FAILED"

_running: set[asyncio.Task] = set()


class RegistrationRefusedError(Exception):
    """A registration that was not stored; ``str()`` is safe to show an admin."""


@dataclass(frozen=True)
class PlatformCredential:
    """What one platform call to the Hub proves itself with.

    *key* set (the instance is enrolled): a fresh assertion signed with it,
    never the token. Otherwise the *token*, as before enrollment.
    """

    platform_id: uuid.UUID
    token: str | None = None
    key: XtmHubKey | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (self.key is None) == (self.token is None):
            raise ValueError("A platform credential is the token or the key, exactly one of them")

    @property
    def signed(self) -> bool:
        return self.key is not None

    def assertion(self, *, htu: str | None = None) -> str:
        """A fresh assertion for one call: a new ``jti`` every time."""
        if self.key is None:
            raise ValueError("No key: this platform is not enrolled")
        return self.key.sign_assertion(self.platform_id, htu=htu)

    def headers(self) -> dict[str, Any]:
        """The ``xtm_hub_graphql()`` credential of one call: the assertion, or the token headers."""
        if self.key is not None:
            return {"platform_assertion": self.assertion()}
        if self.token is None:  # pragma: no cover - __post_init__ refuses it
            raise ValueError("A platform credential is the token or the key")
        return {"platform_token": (str(self.platform_id), self.token)}


def platform_refusal(error: str | None) -> PlatformRefusal | None:
    """The Hub's refusal of a platform credential in an ``xtm_hub_graphql()`` error, if it is one."""
    for code in hub.refusal_codes(error):
        if code in PLATFORM_REFUSALS:
            return PLATFORM_REFUSALS[code]
    return None


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


async def check_registration(hub_url: str, credential: PlatformCredential, contract: Contract) -> CheckResult:
    """What the Hub says about this platform's registration under *credential*.

    Before enrollment the token travels in the input, as it always has; once
    enrolled, the call is signed and the input carries no token.
    *contract* is declared with it: the Hub records a move between ``CE`` and
    ``EE`` and keeps the contract of a trial as it is. A key the Hub no longer
    matches, or a token it no longer takes, reads ``key_mismatch`` /
    ``token_path_closed``: only a new registration helps.
    """
    request: dict[str, Any] = {
        "platformId": str(credential.platform_id),
        "platformVersion": registration_platform_version(),
        "platformIdentifier": PLATFORM_IDENTIFIER,
        "platformContract": contract,
    }
    signed: dict[str, Any] = {}
    if credential.signed:
        signed = credential.headers()
    else:
        request["token"] = credential.token
    data, err = await hub.xtm_hub_graphql(
        hub_url,
        REFRESH_MUTATION,
        {"input": request},
        "check the platform registration",
        **signed,
    )
    if hub.is_transient_error(err):
        return "unreachable"
    if err:
        refusal = platform_refusal(err)
        if refusal == "key_mismatch":
            return "key_mismatch"
        if refusal == "token_path_closed":
            return "token_path_closed"
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
    result = await check_registration(
        bound_url, PlatformCredential(platform_id, token=token), await registration_contract(db)
    )
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
        **PlatformCredential(platform_id, token=token).headers(),
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
        # A new registration: its token is what the Hub knows, no key yet.
        "key_enrolled_at": None,
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
    if result in ("key_mismatch", "token_path_closed"):
        return STATUS_NEEDS_REREGISTRATION
    return STATUS_LOST_CONNECTIVITY


async def enroll_platform_key(
    hub_url: str, platform_id: uuid.UUID, token: str, key: XtmHubKey
) -> EnrollmentOutcome:
    """Ask the Hub to enroll *key* for the registration *token* belongs to.

    The one call that carries both credentials: the token headers name the
    registration, an assertion signed with *key* proves this instance holds
    it, and the input carries the public JWK. Only the Hub's answer is read:
    recording the enrollment and dropping the token are the caller's.
    """
    if not registration_hub_url(hub_url):
        return "refused"
    data, err = await hub.xtm_hub_graphql(
        hub_url,
        ENROLL_MUTATION,
        {"input": {"publicKey": key.public_jwk()}},
        "enroll the platform key",
        platform_token=(str(platform_id), token),
        platform_assertion=key.sign_assertion(platform_id),
    )
    if hub.is_transient_error(err):
        return "unreachable"
    if err:
        return _enrollment_refusal(err)
    answer = (data or {}).get("enrollPlatformKey")
    status = answer.get("status") if isinstance(answer, dict) else None
    if isinstance(status, str) and status in _ENROLLMENT_STATUSES:
        return _ENROLLMENT_STATUSES[status]
    logger.warning("XTM Hub key enrollment: unexpected status %r", str(status)[:40])
    return "refused"


def _enrollment_refusal(error: str) -> EnrollmentOutcome:
    """The outcome a refused enrollment reads as."""
    if _UNKNOWN_OPERATION_CODE in hub.refusal_codes(error):
        return "unknown_operation"
    refusal = platform_refusal(error)
    if refusal == "key_mismatch":
        return "key_mismatch"
    if refusal in ("token_refused", "token_path_closed"):
        # No registration the token still opens: the same as a token the Hub
        # does not know.
        return "not_found"
    return "refused"


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
    version = await key_version(db)
    await db.commit()
    outcome = await _check_snapshot(snapshot, platform_id, contract, version)
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
    key_enrolled_at: datetime | None = None

    @classmethod
    def of(cls, registration: XtmHubRegistration) -> _Snapshot:
        return cls(
            registration.generation,
            registration.hub_url,
            registration.platform_id,
            registration.token_enc,
            registration.status,
            registration.key_enrolled_at,
        )


def snapshot_credential(snapshot: _Snapshot, version: int) -> PlatformCredential | None:
    """The credential the registration's calls carry: the key at *version* once enrolled, else the token.

    ``None`` (logged) when it cannot be built: an unreadable token, or no key
    while ``SECRET_KEY`` is guessable. An enrolled registration never falls
    back to its token.
    """
    if snapshot.key_enrolled_at is not None:
        try:
            return PlatformCredential(snapshot.platform_id, key=derive_key(version))
        except (XtmHubKeyUnavailableError, ValueError) as exc:
            logger.warning("XTM Hub signing key unavailable: %s", format_exception_message(exc))
            return None
    try:
        return PlatformCredential(snapshot.platform_id, token=decrypt_value(snapshot.token_enc))
    except Exception as exc:  # pragma: no cover - a corrupted ciphertext
        logger.warning("XTM Hub registration token unreadable: %s", format_exception_message(exc))
        return None


async def _check_snapshot(
    snapshot: _Snapshot, platform_id: uuid.UUID, contract: Contract, version: int
) -> CheckResult | Literal["platform_changed"]:
    """The Hub's answer for *snapshot*, read with no database access.

    *platform_id* is this platform's current id: a registration made for
    another one sends nothing. *contract* is the one it declares, *version*
    the key version an enrolled registration signs with.
    """
    if snapshot.platform_id != platform_id:
        return "platform_changed"
    if not registration_hub_url(snapshot.hub_url):
        return "refused"
    credential = snapshot_credential(snapshot, version)
    if credential is None:
        return "refused"
    return await check_registration(snapshot.hub_url, credential, contract)


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
        version = await key_version(db)
        # Everything the check needs is read: the session holds no
        # transaction across the network call.
        await db.rollback()
        outcome = await _check_snapshot(snapshot, platform_id, contract, version)
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
