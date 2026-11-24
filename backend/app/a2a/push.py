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

"""Push notifications: telling a partner its task moved, without being polled.

A task can now stay open for as long as the work it delegated runs, which is
exactly the case the protocol defines push notifications for. The partner
registers a callback on a task; this module validates it, stores it, and POSTs a
signal when the task's state changes.

Three properties are the whole design:

* **the notification carries no content.** Task identity and state, and the
  partner reads the result through ``tasks/get`` with its own credential. That
  is what keeps a task's answer off an endpoint we can validate but not
  authenticate;
* **the platform holds no key material of its own.** The partner decides how
  its own endpoint is protected and registers what to present — a ``token``
  echoed back, or a scheme and credentials. Both are the partner's secrets, so
  both are Fernet-encrypted at rest like every other stored credential
  (Constitution III) and decrypted only to be presented;
* **delivery cannot touch the task.** A dead callback, a slow one, or an
  exhausted retry budget changes nothing about the outcome the partner will
  read. The task is the source of truth; this is an accelerator.

Egress reuses the client direction's destination guard rather than adding a
second path: a partner-supplied URL we POST to is the same threat as a partner
card URL we fetch, and ``client.destinations`` already refuses one that resolves
inward. The client direction's circuit breaker does NOT transfer — it accounts
failures on a target row (``circuit_open_until``), and a callback has no such
row. The per-config attempt bound is what stops a dead endpoint being retried
forever instead.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a import metrics as a2a_metrics
from app.a2a import protocol as proto
from app.a2a.client.destinations import DestinationRefusedError, assert_allowed_async
from app.core.config import settings
from app.core.security import decrypt_value, encrypt_value
from app.models.a2a import A2APushConfig, A2ATask

logger = logging.getLogger(__name__)

#: How long one delivery attempt may take. Short: the partner learns the state
#: by polling anyway, and a slow callback must not tie up a worker.
_DELIVERY_TIMEOUT_SECONDS = 10.0

#: What a delivery can actually present. ``_deliver`` puts the registered
#: credential verbatim after the scheme name, so a scheme needing anything
#: computed would be sent as a header the partner rejects — refused on
#: registration rather than at delivery (contracts/a2a-push-notifications.md).
_PRESENTABLE_AUTH_SCHEMES = ("Bearer", "Basic")


class PushConfigRefusedError(ValueError):
    """A registration the surface will not accept. Carries the partner's reason."""


async def register(
    db: AsyncSession,
    *,
    task: A2ATask,
    client_id: str,
    url: str,
    token: str | None = None,
    auth_scheme: str | None = None,
    auth_credentials: str | None = None,
) -> A2APushConfig:
    """Store a validated callback for *task*. Raises :class:`PushConfigRefusedError`.

    The address arrives already checked — ``validate_declared`` is the one
    entry point, and it has to answer before a task exists — and is checked
    again before every send: DNS moves, and a host that resolved outward at
    registration can resolve inward later.
    """
    existing = (
        await db.execute(
            select(A2APushConfig).where(A2APushConfig.task_id == task.id)
        )
    ).scalars().all()
    if len(existing) >= settings.a2a_push_configs_per_task:
        # Refused rather than evicting: a partner that registered a callback is
        # relying on it, and silently dropping the oldest would leave it
        # waiting for a notification that will never come.
        raise PushConfigRefusedError(
            f"At most {settings.a2a_push_configs_per_task} push notification "
            "configurations per task"
        )

    config = A2APushConfig(
        task_id=task.id,
        client_id=client_id,
        url=url,
        # Encrypted on the way in, so a database dump carries no partner
        # credential. Decrypted only at the moment of presenting it.
        token=encrypt_value(token) if token else None,
        auth_scheme=auth_scheme,
        auth_credentials=(
            encrypt_value(auth_credentials) if auth_credentials else None
        ),
    )
    db.add(config)
    await db.flush()
    return config


async def validate_declared(declared: Any) -> dict[str, Any]:
    """Check a partner's declared configuration, writing nothing.

    Separate from registering it because of WHEN the submission path needs the
    answer. A callback declared on ``message/send`` cannot be registered until
    the task exists, but refusing it after the task is created leaves a row
    nothing will ever execute — and the message id is claimed by then, so the
    partner cannot even retry with a corrected address. Everything except the
    per-task count can be decided before any of that, and the count cannot fail
    on a task that has just been created.

    Every field is partner-supplied and unparsed until here, so the shape is
    checked rather than assumed: a scheme list that is not one, or a scheme
    this surface cannot present, is the partner's error and gets told so —
    not a 500 from a column bound or a subscript.
    """
    if not isinstance(declared, dict):
        raise PushConfigRefusedError(
            "pushNotificationConfig must be an object"
        )
    auth = declared.get("authentication")
    auth = auth if isinstance(auth, dict) else {}
    schemes = auth.get("schemes")
    schemes = schemes if isinstance(schemes, list) else []
    scheme = None
    if schemes:
        scheme = next(
            (
                str(s)
                for s in schemes
                if str(s).lower()
                in {p.lower() for p in _PRESENTABLE_AUTH_SCHEMES}
            ),
            None,
        )
        if scheme is None:
            raise PushConfigRefusedError(
                "Unsupported authentication scheme; this surface presents "
                + " or ".join(_PRESENTABLE_AUTH_SCHEMES)
            )
    credentials = auth.get("credentials")
    url = str(declared.get("url") or "")
    try:
        await assert_allowed_async(url)
    except DestinationRefusedError as exc:
        raise PushConfigRefusedError(str(exc)) from exc
    return {
        "url": url,
        "token": declared.get("token"),
        "auth_scheme": scheme,
        "auth_credentials": str(credentials) if credentials else None,
    }


async def register_declared(
    db: AsyncSession, *, task: A2ATask, client_id: str, declared: dict[str, Any]
) -> A2APushConfig:
    """Register the config object as a partner spelled it.

    One reader for the two places a partner supplies one: the ``set`` method,
    and the callback declared on a submission itself.
    """
    return await register(
        db, task=task, client_id=client_id, **await validate_declared(declared)
    )


async def get(
    db: AsyncSession, *, config_id: str, client_id: str
) -> A2APushConfig | None:
    """One configuration, scoped to its owning registration.

    ``None`` for a malformed id, one that does not exist, and one belonging to
    another client alike — the same uniform answer the rest of this surface
    gives, so a partner cannot enumerate another's callbacks.
    """
    try:
        parsed = uuid.UUID(str(config_id))
    except (ValueError, AttributeError, TypeError):
        return None
    return (
        await db.execute(
            select(A2APushConfig).where(
                A2APushConfig.id == parsed,
                A2APushConfig.client_id == client_id,
            )
        )
    ).scalar_one_or_none()


async def list_for_task(
    db: AsyncSession, *, task: A2ATask, client_id: str
) -> list[A2APushConfig]:
    return list(
        (
            await db.execute(
                select(A2APushConfig)
                .where(
                    A2APushConfig.task_id == task.id,
                    A2APushConfig.client_id == client_id,
                )
                .order_by(A2APushConfig.created_at)
            )
        )
        .scalars()
        .all()
    )


async def delete(db: AsyncSession, *, config: A2APushConfig) -> None:
    await db.delete(config)
    await db.flush()


def render(config: A2APushConfig, version: str) -> dict[str, Any]:
    """Project a configuration for the wire, in *version*'s own shape.

    The credential is never echoed in a read: a partner that registered a token
    knows it, and returning it would put a secret in a response body for the
    benefit of nobody. ``tenant`` is absent for the same reason it is absent
    from the card — this platform has no tenant entity.
    """
    body: dict[str, Any] = {"id": str(config.id), "url": config.url}
    if config.auth_scheme:
        body["authentication"] = {"schemes": [config.auth_scheme]}
    if proto.is_legacy(version):
        return {"taskId": str(config.task_id), "pushNotificationConfig": body}
    # v1.0 folded the two levels into one: ``TaskPushNotificationConfig``
    # carries ``taskId`` alongside ``url`` and the rest, rather than wrapping a
    # nested ``PushNotificationConfig``. Same fields, one object — a v1.0
    # parser reads the nested shape as a config with no url at all.
    return {"taskId": str(config.task_id), **body}


async def notify(db: AsyncSession, task: A2ATask, state: str) -> bool:
    """Tell every callback on *task* that it reached *state*.

    Returns whether the announcement is SETTLED: every callback either took it
    or has spent its attempts. ``False`` means at least one address failed with
    attempts left, and the caller must leave the announcement re-announceable —
    otherwise one bad ten seconds costs the partner the notification
    permanently, and the attempt budget and its abandonment audit are never
    reached.

    Never raises. A failure here is a partner that has to poll, which is what
    every partner does today — whereas an exception escaping into the executor
    would cost the task its terminal write.
    """
    try:
        configs = (
            await db.execute(
                select(A2APushConfig).where(
                    A2APushConfig.task_id == task.id,
                    A2APushConfig.attempts < settings.a2a_push_max_attempts,
                )
            )
        ).scalars().all()
    except Exception:
        logger.warning("Push notification lookup failed", exc_info=True)
        return False

    settled = True
    for config in configs:
        await _deliver(config, task, state)
        if (
            config.delivery_state == "failed"
            and config.attempts >= settings.a2a_push_max_attempts
        ):
            # The platform has stopped trying. Recorded durably as well as
            # counted, because "why was this partner never told" is asked about
            # one task and one address, which a counter cannot answer.
            a2a_metrics.record_push_abandoned()
            await _audit_abandoned(db, task, config)
        elif config.delivery_state != "delivered":
            settled = False

    # The attempt counter and delivery state are this module's own bookkeeping,
    # and nothing else commits them: the caller committed the task's terminal
    # write BEFORE calling here, and its session closes after. Without this the
    # counter never advances — so the retry bound never binds, the delivery
    # metrics record nothing, and the abandonment audit can never fire.
    try:
        await db.commit()
    except Exception:
        logger.warning("Push notification bookkeeping not persisted", exc_info=True)
    return settled


async def _deliver(config: A2APushConfig, task: A2ATask, state: str) -> None:
    """One attempt at one callback. Records the outcome, swallows the failure.

    Bounded by ``attempts`` on the row rather than by a circuit: a callback is
    per task, so there is no shared target for a breaker to hold state on, and
    exhausting the budget is what stops a dead endpoint being retried.
    """
    config.attempts += 1
    # Identity and state only. A forged notification therefore discloses
    # nothing; the partner reads the result through tasks/get.
    body: dict[str, Any] = {
        "taskId": str(task.id),
        "contextId": str(task.context_id),
        "state": state,
    }
    if not await _post(config, body):
        config.delivery_state = "failed"
        logger.info(
            "Push notification to task %s callback failed (attempt %s/%s)",
            task.id,
            config.attempts,
            settings.a2a_push_max_attempts,
        )
        return

    config.delivery_state = "delivered"


async def deliver_body(config: A2APushConfig, body: dict[str, Any], *, label: str) -> bool:
    """One best-effort send of *body* to *config*, outside the task's announcement.

    For a notification that is not the task's own state (``ticket_push``): it
    neither spends the callback's attempt budget nor changes its delivery
    state, which belong to the task's announcement. Never raises.
    """
    delivered = await _post(config, body)
    if not delivered:
        logger.info("Push notification about %s to a callback of task %s failed", label, config.task_id)
    return delivered


async def _post(config: A2APushConfig, body: dict[str, Any]) -> bool:
    """POST *body* to the callback with the partner's credential; whether it was taken.

    Records the delivery metric. Never raises.
    """
    headers = {"Content-Type": "application/json"}
    credentials = _reveal(config.auth_credentials)
    if config.auth_scheme and credentials:
        headers["Authorization"] = f"{config.auth_scheme} {credentials}"
    token = _reveal(config.token)
    if token:
        body = {**body, "token": token}
    try:
        # Re-checked, not trusted from registration: DNS moves, and a host that
        # resolved outward then can resolve inward now.
        await assert_allowed_async(config.url)
        async with httpx.AsyncClient(timeout=_DELIVERY_TIMEOUT_SECONDS) as client:
            response = await client.post(config.url, json=body, headers=headers)
        response.raise_for_status()
    except Exception:
        a2a_metrics.record_push_delivery("failed")
        return False
    a2a_metrics.record_push_delivery("delivered")
    return True


def _reveal(stored: str | None) -> str | None:
    """Decrypt a stored credential, or ``None`` if it cannot be read.

    A row written under a key that has since rotated cannot be presented, and
    guessing would send the ciphertext as though it were the credential — which
    the partner would reject while the platform reported a delivered call. The
    delivery fails instead, and the partner polls.
    """
    if not stored:
        return None
    try:
        return decrypt_value(stored)
    except Exception:
        logger.warning(
            "A push notification credential could not be decrypted; the "
            "callback cannot be authenticated and delivery will fail"
        )
        return None


async def _audit_abandoned(db: AsyncSession, task: A2ATask, config: A2APushConfig) -> None:
    """Record that this platform gave up reaching one callback.

    Anchored on the task's run-as user with the client id in the actor slot, the
    same shape the submission and quota-exhaustion entries use, so a partner's
    whole history reads as one series. Best-effort: a failed audit write must
    not become the reason a notification path raises.
    """
    from app.a2a.audit import audit_actor
    from app.models.a2a import A2AExposure
    from app.services.audit import log_audit

    try:
        exposure = (
            await db.execute(
                select(A2AExposure).where(A2AExposure.id == task.exposure_id)
            )
        ).scalar_one_or_none()
        if exposure is None:
            return
        await log_audit(
            db,
            user=audit_actor(exposure.run_as_user_id, config.client_id),
            action="reject",
            entity_type="a2a_task",
            entity_id=task.id,
            entity_name=config.url,
            summary=(
                f"Gave up notifying client {config.client_id} about task "
                f"{task.id} after {config.attempts} attempts; the partner must "
                "poll for this outcome"
            ),
            details={
                "client_id": config.client_id,
                "attempts": config.attempts,
                # The address, not the credential: an operator needs to know
                # which endpoint is unreachable.
                "url": config.url[:500],
            },
        )
    except Exception:
        logger.warning("Push abandonment audit failed", exc_info=True)
