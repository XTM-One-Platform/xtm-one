# SPDX-License-Identifier: Apache-2.0

"""Web Push to linked phones: the bell's notifications, delivered to the pocket.

What is pushed is exactly what the bell shows (``services/notification_feed``
presents it, route included), so a phone and the web app never say two
different things. Delivery runs in the worker (``send_notification_push_job``),
queued only once the notification is committed (``queue_push``), and never
blocks the run or the request that raised it.

The subscription a phone hands us is hostile input: its endpoint is a URL the
browser chose, so it is held to the browser push services' hosts
(:data:`PUSH_SERVICE_HOST_SUFFIXES`), HTTPS only, and sent through the
platform's one outbound policy (``web_fetch.validate_outbound_url_async`` +
``guarded_client_options``: public addresses only, the connection pinned to
the address checked, no redirect followed, a timeout, a bounded read).

The payload is encrypted end to end to the phone (RFC 8291, ``http_ece``): the
push service carries it without reading it. A deployment that wants nothing of
a notification on a lock screen sets the ``phone_notification_content``
security option to ``minimal``: the push then says only that something is new.

The platform authenticates to the push services with its VAPID key pair
(RFC 8292), generated on first use and kept encrypted in ``web_push_keys``.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from sqlalchemy import event, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message
from app.services.notification_feed import DECISION_KINDS, kind_names

logger = logging.getLogger(__name__)

#: The browser push services a subscription endpoint may name: FCM (Chrome,
#: Edge, Samsung Internet, Opera on Android), Apple (Safari), Mozilla (Firefox)
#: and Windows (WNS). Anything else is refused at link time and at send time.
PUSH_SERVICE_HOST_SUFFIXES: tuple[str, ...] = (
    "fcm.googleapis.com",
    "push.apple.com",
    "push.services.mozilla.com",
    "notify.windows.com",
)

#: How long a push service keeps a push for an offline phone.
PUSH_TTL_SECONDS = 24 * 60 * 60
PUSH_TIMEOUT_SECONDS = 10
#: A push service answers a few bytes; anything past this is not read.
MAX_RESPONSE_BYTES = 4096
#: Bounds of what a notification shows.
MAX_TITLE_CHARS = 80
MAX_BODY_CHARS = 240
#: The VAPID token's lifetime (the push services accept up to 24 h).
VAPID_TOKEN_SECONDS = 12 * 60 * 60

#: What a push says in ``minimal`` mode.
MINIMAL_TITLE = "New notification"
MINIMAL_BODY = "Open the app to see it."


class PushEndpointError(ValueError):
    """A subscription endpoint that is not a browser push service."""


def push_endpoint_problem(endpoint: object) -> str | None:
    """Why ``endpoint`` cannot be pushed to, or ``None``."""
    if not isinstance(endpoint, str) or len(endpoint) > 2048:
        return "not a push endpoint"
    try:
        # Both raise ValueError: ``urlsplit`` on a malformed IPv6 host, ``port``
        # on one that is not a number or past 65535.
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return "not a push endpoint"
    if parts.scheme != "https":
        return "not https"
    if parts.username or parts.password or port not in (None, 443):
        return "credentials or a port in the endpoint"
    host = (parts.hostname or "").lower().rstrip(".")
    if not any(host == suffix or host.endswith(f".{suffix}") for suffix in PUSH_SERVICE_HOST_SUFFIXES):
        return "not a browser push service"
    return None


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass(frozen=True, slots=True)
class Subscription:
    endpoint: str
    p256dh: bytes
    auth: bytes

    @classmethod
    def parse(cls, raw: object) -> Subscription:
        """A browser ``PushSubscription.toJSON()``, checked (``PushEndpointError`` otherwise)."""
        if not isinstance(raw, dict):
            raise PushEndpointError("not a push subscription")
        endpoint = raw.get("endpoint")
        problem = push_endpoint_problem(endpoint)
        if problem:
            raise PushEndpointError(problem)
        keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
        try:
            p256dh = _b64url_decode(str(keys.get("p256dh") or ""))
            auth = _b64url_decode(str(keys.get("auth") or ""))
        except (ValueError, TypeError) as exc:
            raise PushEndpointError("unreadable subscription keys") from exc
        # An uncompressed P-256 point and a 16-byte secret (RFC 8291).
        if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
            raise PushEndpointError("unexpected subscription keys")
        return cls(endpoint=str(endpoint), p256dh=p256dh, auth=auth)

    def to_json(self) -> str:
        return json.dumps(
            {"endpoint": self.endpoint, "keys": {"p256dh": _b64url(self.p256dh), "auth": _b64url(self.auth)}}
        )

    @property
    def endpoint_hash(self) -> str:
        return sha256(self.endpoint.encode()).hexdigest()


# ── VAPID ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class VapidKeys:
    public_key: str  # base64url, uncompressed point: the browser's applicationServerKey
    private_pem: str


def _generate_vapid_keys() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    pem = private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return _b64url(public), pem


async def get_vapid_keys(db: AsyncSession) -> VapidKeys:
    """The platform's VAPID key pair, created on first use (once, whatever the race)."""
    from app.core.security import decrypt_stored_value, encrypt_value
    from app.models.mobile_device import WebPushKeys

    row = (await db.execute(select(WebPushKeys).where(WebPushKeys.id == 1))).scalar_one_or_none()
    if row is None:
        public, pem = _generate_vapid_keys()
        await db.execute(
            pg_insert(WebPushKeys)
            .values(id=1, public_key=public, private_key=encrypt_value(pem), created_at=datetime.now(UTC))
            .on_conflict_do_nothing(index_elements=[WebPushKeys.id])
        )
        await db.commit()
        row = (await db.execute(select(WebPushKeys).where(WebPushKeys.id == 1))).scalar_one()
    return VapidKeys(public_key=row.public_key, private_pem=decrypt_stored_value(row.private_key))


def vapid_subject() -> str:
    """Who the push services may contact: the platform's https address, else a mailto."""
    from app.core.config import settings

    base = (settings.base_url or "").strip()
    if base.startswith("https://"):
        return base.rstrip("/")
    host = urlsplit(base).hostname or "localhost"
    return f"mailto:push@{host}"


def vapid_authorization(endpoint: str, keys: VapidKeys, *, now: float | None = None) -> str:
    """The ``Authorization`` header of a push (RFC 8292): a signed ES256 JWT + the public key."""
    import jwt

    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int((now or time.time()) + VAPID_TOKEN_SECONDS),
        "sub": vapid_subject(),
    }
    token = jwt.encode(claims, keys.private_pem, algorithm="ES256")
    return f"vapid t={token}, k={keys.public_key}"


def encrypt_payload(subscription: Subscription, payload: bytes) -> bytes:
    """``payload`` encrypted to the phone (RFC 8291, ``aes128gcm``)."""
    import http_ece
    from cryptography.hazmat.primitives.asymmetric import ec

    server_key = ec.generate_private_key(ec.SECP256R1())
    return http_ece.encrypt(
        payload,
        salt=os.urandom(16),
        private_key=server_key,
        dh=subscription.p256dh,
        auth_secret=subscription.auth,
        version="aes128gcm",
    )


# ── What a push says ─────────────────────────────────────────────────────────


def _truncate(text: str | None, limit: int) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else f"{clean[: limit - 1]}…"


def notification_icon() -> str:
    """The app icon a notification shows: the platform mode's (``build_web_manifest``'s PNG)."""
    from app.core.config import settings

    return "/xtm-one-icon-192.png" if settings.is_xtm_one else "/copilot-icon-192.png"


def push_payload(item: Any, *, minimal: bool, actions: list[dict] | None = None) -> dict[str, Any]:
    """The JSON a phone's service worker shows (``public/sw.js``).

    ``url`` is the item's route, a path of the web app (the feed builds it
    with ``check_internal_path``); the service worker opens it on its own
    origin only. ``minimal`` drops everything a lock screen could reveal.
    """
    if minimal:
        return {
            "title": MINIMAL_TITLE,
            "body": MINIMAL_BODY,
            "url": "/",
            "tag": item.id,
            "kind": "minimal",
            "icon": notification_icon(),
        }
    body = item.summary or item.subtitle or ""
    payload: dict[str, Any] = {
        "title": _truncate(item.title, MAX_TITLE_CHARS),
        "body": _truncate(body, MAX_BODY_CHARS),
        "url": item.route,
        "tag": item.id,
        "kind": item.kind,
        # Every decision is urgent, whichever source it waits on (#842, S4).
        "urgent": item.kind in DECISION_KINDS or bool(getattr(item, "critical", False)),
        "icon": notification_icon(),
    }
    if actions:
        payload["actions"] = actions
    return payload


def in_quiet_hours(quiet_hours: object, now: datetime) -> bool:
    """Whether ``now`` falls in a phone's quiet hours ``{start, end, timezone}`` (may cross midnight)."""
    if not isinstance(quiet_hours, dict):
        return False
    try:
        start = datetime.strptime(str(quiet_hours["start"]), "%H:%M").time()
        end = datetime.strptime(str(quiet_hours["end"]), "%H:%M").time()
        local = now.astimezone(ZoneInfo(str(quiet_hours.get("timezone") or "UTC"))).time()
    except (KeyError, ValueError, LookupError):
        return False
    if start == end:
        return False
    return start <= local < end if start < end else local >= start or local < end


def device_wants(device: Any, kind: str, now: datetime, *, source: str | None = None) -> bool:
    """Whether a phone gets a push of ``kind`` now (a decision muted under its former kind stays muted)."""
    if not device.push_enabled:
        return False
    if kind_names(kind, source) & set(device.muted_kinds or []):
        return False
    return not in_quiet_hours(device.quiet_hours, now)


# ── Sending ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PushResult:
    #: sent, gone (the subscription no longer exists), refused (endpoint
    #: policy), failed (the service or the network).
    outcome: str
    status_code: int | None = None


async def send_push(
    subscription: Subscription, payload: dict[str, Any], keys: VapidKeys, *, urgent: bool
) -> PushResult:
    """POST one encrypted push to the subscription's push service."""
    import httpx

    from app.services.web_fetch import guarded_client_options, validate_outbound_url_async

    if push_endpoint_problem(subscription.endpoint):
        return PushResult("refused")
    try:
        await validate_outbound_url_async(subscription.endpoint)
    except Exception as exc:
        logger.warning("Push endpoint refused by the outbound policy: %s", format_exception_message(exc))
        return PushResult("refused")
    body = encrypt_payload(subscription, json.dumps(payload, ensure_ascii=False).encode())
    headers = {
        "Authorization": vapid_authorization(subscription.endpoint, keys),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(PUSH_TTL_SECONDS),
        "Urgency": "high" if urgent else "normal",
        "Accept-Encoding": "identity",
    }
    try:
        async with (
            httpx.AsyncClient(
                timeout=PUSH_TIMEOUT_SECONDS,
                follow_redirects=False,
                trust_env=False,
                **guarded_client_options(allow_private=False),
            ) as client,
            client.stream("POST", subscription.endpoint, content=body, headers=headers) as response,
        ):
            received = 0
            async for chunk in response.aiter_raw():
                received += len(chunk)
                if received > MAX_RESPONSE_BYTES:
                    break
            status_code = response.status_code
    except Exception as exc:
        logger.warning("Push delivery failed: %s", format_exception_message(exc))
        return PushResult("failed")
    if status_code in (404, 410):
        return PushResult("gone", status_code)
    if 200 <= status_code < 300:
        return PushResult("sent", status_code)
    logger.warning("Push service answered %s", status_code)
    return PushResult("failed", status_code)


def phone_notifications_minimal() -> bool:
    from app.core.security_options import security_option

    return security_option("phone_notification_content") == "minimal"


async def push_item_to_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    item: Any,
    *,
    actions_for: Any = None,
    now: datetime | None = None,
    only_device: uuid.UUID | None = None,
) -> dict[str, int]:
    """Push one feed item to every phone of ``user_id`` that wants it.

    ``actions_for(device)`` returns the notification's action buttons for one
    phone (its own one-time tokens), or ``None``.
    """
    from app.core.security import decrypt_stored_value
    from app.models.mobile_device import MobileDevice

    counts = {"sent": 0, "skipped": 0, "gone": 0, "refused": 0, "failed": 0}
    if not companion_enabled():
        return counts
    moment = now or datetime.now(UTC)
    query = select(MobileDevice).where(MobileDevice.user_id == user_id)
    if only_device is not None:
        query = query.where(MobileDevice.id == only_device)
    devices = list((await db.execute(query)).scalars().all())
    wanting = [d for d in devices if device_wants(d, item.kind, moment, source=getattr(item, "source", None))]
    counts["skipped"] = len(devices) - len(wanting)
    if not wanting:
        return counts
    keys = await get_vapid_keys(db)
    # A PIN-locked companion shows nothing and decides nothing until unlocked:
    # its lock screen neither (``services/app_lock.py``).
    from app.services.app_lock import pin_applies

    minimal = phone_notifications_minimal() or await pin_applies(db, user_id)
    for device in wanting:
        try:
            subscription = Subscription.parse(json.loads(decrypt_stored_value(device.push_subscription)))
        except Exception:
            counts["refused"] += 1
            device.push_enabled = False
            continue
        actions = None if minimal or actions_for is None else await actions_for(device)
        payload = push_payload(item, minimal=minimal, actions=actions)
        result = await send_push(subscription, payload, keys, urgent=bool(payload.get("urgent")))
        counts[result.outcome] += 1
        if result.outcome == "sent":
            device.last_push_at = moment
        elif result.outcome in ("gone", "refused"):
            # The phone dropped the subscription (or it was never valid):
            # stop pushing until the person turns notifications on again.
            device.push_enabled = False
    await db.commit()
    return counts


# ── Queuing after commit ─────────────────────────────────────────────────────

_PENDING_KEY = "mobile_push_pending"


def companion_enabled() -> bool:
    """Whether the mobile companion is on ("MOBILE_COMPANION"): off, no phone is ever pushed."""
    from app.core.feature_flags import is_feature_enabled

    return is_feature_enabled("MOBILE_COMPANION")


def queue_push(db: AsyncSession, *, user_id: object, ref_kind: str, ref_id: object) -> None:
    """Push a notification once the transaction that wrote it commits.

    ``ref_kind`` is ``activity`` (an ``Activity`` row) or ``run`` (an
    assignment run the bell shows). Nothing is pushed on a rollback.
    """
    if user_id is None or ref_id is None or not companion_enabled():
        return
    # A side channel: whatever goes wrong here never costs the caller the
    # notification it is writing (the bell keeps it, the phone misses it).
    try:
        sync_session = db.sync_session
        pending = sync_session.info.setdefault(_PENDING_KEY, [])
        pending.append({"user_id": str(user_id), "ref_kind": ref_kind, "ref_id": str(ref_id)})
        if not sync_session.info.get("mobile_push_listening"):
            sync_session.info["mobile_push_listening"] = True
            event.listen(sync_session, "after_commit", _after_commit)
            event.listen(sync_session, "after_rollback", _after_rollback)
    except Exception:
        logger.warning("Could not queue the phone push of %s %s", ref_kind, ref_id, exc_info=True)


def _after_rollback(session) -> None:
    session.info.pop(_PENDING_KEY, None)


def _after_commit(session) -> None:
    import asyncio

    pending = session.info.pop(_PENDING_KEY, None)
    if not pending:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for job in pending:
        loop.create_task(_enqueue(job))


async def _enqueue(job: dict[str, str]) -> None:
    try:
        from saq import Job

        from app.worker import queue

        await queue.enqueue(
            Job(
                function="send_notification_push_job",
                kwargs=job,
                key=f"push-{job['ref_kind']}-{job['ref_id']}-{job['user_id']}",
                timeout=60,
                retries=2,
                retry_delay=5.0,
            )
        )
    except Exception as exc:
        logger.warning("Could not queue a phone notification: %s", format_exception_message(exc))


async def deliver_queued_push(db: AsyncSession, *, user_id: str, ref_kind: str, ref_id: str) -> dict[str, int] | None:
    """The worker side: present the notification as the bell does and push it."""
    from app.core.feature_flags import is_feature_enabled
    from app.models.activity import Activity
    from app.services.notification_feed import present_activity, present_run

    # Queued before the flag went off: dropped like every later one.
    if not companion_enabled():
        return None
    owner = uuid.UUID(user_id)
    if not await _has_phone(db, owner):
        return None
    item = None
    actions_for = None
    if ref_kind == "activity":
        row = await db.get(Activity, uuid.UUID(ref_id))
        if row is None or row.user_id != owner:
            return None
        item = present_activity(row, flows_enabled=is_feature_enabled("FLOW_BUILDER"))
    elif ref_kind == "run":
        view = await _run_view(db, uuid.UUID(ref_id))
        if view is None:
            return None
        item = present_run(view)
        # A run decision on tool approvals: Approve / Reject on the push.
        if item is not None and item.source == "run" and view.pending_approvals and not view.interactive:
            from app.services.push_action_tokens import approval_actions

            async def actions_for(device, _view=view):
                return await approval_actions(
                    run_id=_view.id, user_id=owner, device_id=device.id, pending=_view.pending_approvals
                )

    if item is None:
        return None
    return await push_item_to_user(db, owner, item, actions_for=actions_for)


async def _has_phone(db: AsyncSession, user_id: uuid.UUID) -> bool:
    from app.models.mobile_device import MobileDevice

    found = await db.execute(
        select(MobileDevice.id).where(MobileDevice.user_id == user_id, MobileDevice.push_enabled.is_(True)).limit(1)
    )
    return found.scalar_one_or_none() is not None


@dataclass(frozen=True, slots=True)
class _RunView:
    id: uuid.UUID
    status: str
    created_at: datetime
    assignment_name: str | None
    agent_name: str | None
    human_input_question: str | None
    result_summary: str | None
    pending_approvals: list
    interactive: bool


async def _run_view(db: AsyncSession, run_id: uuid.UUID) -> _RunView | None:
    from app.models.agent import Agent
    from app.models.assignment import Assignment, AssignmentRun
    from app.services.assignment_helpers import is_interactive_run

    run = await db.get(AssignmentRun, run_id)
    if run is None:
        return None
    assignment = await db.get(Assignment, run.assignment_id) if run.assignment_id else None
    agent = await db.get(Agent, run.agent_id) if run.agent_id else None
    return _RunView(
        id=run.id,
        status=run.status,
        created_at=run.created_at,
        assignment_name=assignment.name if assignment else None,
        agent_name=agent.name if agent else None,
        human_input_question=run.human_input_question,
        result_summary=run.result_summary,
        pending_approvals=list(run.pending_approvals or []),
        interactive=is_interactive_run(run),
    )
