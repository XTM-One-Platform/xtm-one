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

"""Runner transport – event-driven command dispatch + durable backstop.

Performance model (the user's hard requirement — real-time, not polling):

* **Fast path (WebSocket).** The runner holds an outbound WebSocket. When an
  agent dispatches a command, the replica holding that socket claims the row
  and pushes it **instantly** — no poll interval, sub-second end to end.
* **Cross-replica wakeup (Redis pub/sub).** The dispatching replica may not be
  the one holding the socket. It publishes to ``runner:wake:{runner_id}``; the
  holding replica's listener claims + pushes on receipt. Same instant latency.
* **Result wakeup (Redis pub/sub).** The runner uploads the result over HTTP
  (any replica). That replica publishes ``runner:result:{command_id}`` and the
  awaiting agent tool — possibly on a third replica — returns immediately.
* **Durable backstop (Postgres).** Every command is a row. If the socket is
  down the runner's long-poll drains the same queue, and a redelivery after a
  crash is safe because claiming is atomic (``FOR UPDATE SKIP LOCKED``).

Large binary results (screenshots, file reads) travel over HTTP multipart, not
the WebSocket, so control frames stay tiny and reuse the existing
``FileAttachment`` persistence path.

Session discipline: the agent-tool-facing calls (:func:`dispatch_command`,
:func:`await_result`) open their OWN short-lived sessions — they run mid
agentic-loop, where pinning the caller's pooled connection for the minutes of a
capture would exhaust the web pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, or_, select, update

from app.core.config import settings
from app.core.database import async_session
from app.core.redaction import (
    known_secrets_cover,
    known_secrets_scope,
    redact_known_secrets,
    redact_known_secrets_for_display,
    restore_known_secrets,
    restored_secret_fingerprints,
)
from app.core.redis import close_pubsub, open_pubsub, redis_client, redis_pubsub_client
from app.models.runner import COMMAND_TERMINAL_STATUSES, Runner, RunnerCommand

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)


# Payload keys whose values may be large or sensitive (typed text, file
# contents). Audit keeps command *provenance*, never a verbatim copy of a big
# or private blob — the value is length-summarized instead.
_AUDIT_REDACT_KEYS: frozenset[str] = frozenset({"content", "text"})
_AUDIT_VALUE_LIMIT = 200


def _summarize_payload(payload: dict | None) -> dict:
    """Compact, audit-safe view of a command payload.

    Long strings are truncated and the two content-bearing keys are reduced to
    a ``<N chars>`` marker, so the audit log stays a lean provenance trail
    rather than a second copy of every file written or string typed.
    """
    out: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        if isinstance(value, str):
            if key in _AUDIT_REDACT_KEYS:
                out[key] = f"<{len(value)} chars>"
            elif len(value) > _AUDIT_VALUE_LIMIT:
                out[key] = value[:_AUDIT_VALUE_LIMIT] + "..."
            else:
                out[key] = value
        else:
            out[key] = value
    return out


async def _load_dispatcher_secrets(session: AsyncSession, user_id: Any) -> None:
    """Remember, in the open scope, the secret variables a command's dispatcher can read now.

    In a SAVEPOINT: the caller's transaction holds the command's row lock and
    its own write, which a failed read must not abort.
    """
    from app.api.v1.helpers import get_user_group_ids
    from app.services.variable_resolver import load_variables

    async with session.begin_nested():
        group_ids = await get_user_group_ids(session, user_id) if user_id is not None else []
        await load_variables(session, user_id, group_ids)


async def restored_command_payload(session: AsyncSession, cmd: RunnerCommand) -> tuple[dict, set[str]]:
    """The command's payload as the runner executes it, and a fingerprint of each secret value put into it.

    The row holds markers bound to the command itself (:func:`dispatch_command`),
    restored with the values its dispatcher can read now: a share revoked
    meanwhile leaves the marker. This in-memory copy is what the runner's
    policy is checked against (at approval and at delivery), so a check never
    passes on a marker while the runner gets the value. The values stay in a
    scope of their own, since a drain run inside one user's turn delivers
    other users' commands too.
    """
    from app.services.runners import requested_payload

    payload = requested_payload(cmd.payload)
    if "[[SECRET_VARIABLE:" not in json.dumps(payload, default=str):
        return payload, set()
    try:
        with known_secrets_scope(cmd.id, inherit=False):
            await _load_dispatcher_secrets(session, cmd.user_id)
            return restore_known_secrets(payload), restored_secret_fingerprints(payload)
    except Exception:
        logger.warning("Secret variables not restored in runner command %s", cmd.id, exc_info=True)
        return payload, set()


def _record_delivered_secrets(cmd: RunnerCommand, fingerprints: set[str]) -> None:
    """Add what this delivery carried to what every delivery of the command carried (a redelivery may carry a rotated value)."""
    from app.services.runners import DELIVERED_SECRETS_KEY

    if not fingerprints:
        return
    stored = cmd.payload or {}
    recorded = {str(item) for item in stored.get(DELIVERED_SECRETS_KEY) or []}
    if fingerprints <= recorded:
        return
    cmd.payload = {**stored, DELIVERED_SECRETS_KEY: sorted(recorded | fingerprints)}


#: What a command's result says when it could not be stored without a secret its runner was given.
_RESULT_WITHHELD = (
    "The command ran, but its output was not kept: a secret variable it was "
    "given has changed or can no longer be read, so its value could not be "
    "kept out of what is stored."
)


async def _without_secrets(
    session: AsyncSession, cmd: RunnerCommand, status: str, result: dict | None, error: str | None
) -> tuple[str, dict | None, str | None]:
    """What a runner sent back, each secret value its dispatcher can read as ``[secret]``.

    The upload comes from the machine, outside the run that dispatched the
    command, so the values are loaded here. Display text rather than a marker:
    the agent tool and the flow node read the result as it is stored. Every
    value a delivery of the command carried must still be one of them: one
    rotated, deleted, unshared or unreadable since could come back
    unrecognised, so then nothing the machine sent is kept and the command
    is recorded as failed.
    """
    from app.services.runners import DELIVERED_SECRETS_KEY

    if not (result or error):
        return status, result, error
    delivered = (cmd.payload or {}).get(DELIVERED_SECRETS_KEY) or []
    with known_secrets_scope(cmd.id, inherit=False):
        await _load_dispatcher_secrets(session, cmd.user_id)
        if not known_secrets_cover(delivered):
            logger.warning("Result of runner command %s not kept: a secret variable it was given changed", cmd.id)
            return "failed", None, _RESULT_WITHHELD
        return status, redact_known_secrets_for_display(result), redact_known_secrets_for_display(error)


def _wake_channel(runner_id: Any) -> str:
    return f"runner:wake:{runner_id}"


def _control_channel(runner_id: Any) -> str:
    return f"runner:control:{runner_id}"


def _result_channel(command_id: Any) -> str:
    return f"runner:result:{command_id}"


#: Prefix of the control signal that stops one running command; every
#: other control signal is a revoke.
STOP_SIGNAL_PREFIX = "stop:"


def stop_signal(command_id: Any) -> str:
    """The control signal asking the runner to stop *command_id*."""
    return f"{STOP_SIGNAL_PREFIX}{command_id}"


def stop_frame(signal: Any) -> dict | None:
    """The ``stop`` frame a control *signal* carries, ``None`` for any other signal."""
    if isinstance(signal, bytes):
        signal = signal.decode("utf-8", "replace")
    if not isinstance(signal, str) or not signal.startswith(STOP_SIGNAL_PREFIX):
        return None
    command_id = signal[len(STOP_SIGNAL_PREFIX) :]
    return {"type": "stop", "command_id": command_id} if command_id else None


async def publish_runner_control(runner_id: Any, signal: str) -> None:
    """Fan a control signal (e.g. ``"revoke"``) to the replica holding the WS.

    Revocation must drop the live socket *immediately*, not wait out the
    heartbeat window. The credential is already invalid (``is_active`` is
    cleared before this is called), so this only closes a socket that can no
    longer do anything — the claim hands a revoked runner nothing — but it
    makes revocation feel instant and frees the connection. Best-effort: the
    socket's next heartbeat reads the revocation from the database and closes
    it anyway (:func:`serve_runner_websocket`).
    """
    try:
        redis = await redis_client()
        await redis.publish(_control_channel(runner_id), signal)
    except Exception:
        logger.debug(
            "Runner control publish (%s) failed for %s", signal, runner_id,
            exc_info=True,
        )


# ── In-process WebSocket registry (per replica) ─────────────────────────────


class RunnerConnection:
    """A live WebSocket to one runner, held by THIS replica.

    Sends are serialized by a lock (a runner is single-consumer). The registry
    is best-effort and per-replica: a runner reachable here pushes instantly;
    one reachable elsewhere is woken through Redis; one reachable nowhere falls
    back to the durable queue via long-poll.
    """

    def __init__(self, runner_id: str, websocket: WebSocket) -> None:
        self.runner_id = runner_id
        self.websocket = websocket
        self._lock = asyncio.Lock()
        self.alive = True

    async def send(self, message: dict) -> bool:
        if not self.alive:
            return False
        async with self._lock:
            try:
                await self.websocket.send_json(message)
                return True
            except Exception:
                self.alive = False
                return False


_connections: dict[str, RunnerConnection] = {}


def register_connection(runner_id: Any, websocket: WebSocket) -> RunnerConnection:
    """Register (replacing any stale) the live WS for a runner on this replica."""
    key = str(runner_id)
    existing = _connections.get(key)
    if existing is not None:
        existing.alive = False
    conn = RunnerConnection(key, websocket)
    _connections[key] = conn
    return conn


def unregister_connection(runner_id: Any, conn: RunnerConnection) -> None:
    key = str(runner_id)
    if _connections.get(key) is conn:
        del _connections[key]
    conn.alive = False


def has_local_connection(runner_id: Any) -> bool:
    conn = _connections.get(str(runner_id))
    return conn is not None and conn.alive


# ── Liveness bookkeeping ────────────────────────────────────────────────────


async def touch_runner(
    runner_id: Any,
    *,
    transport: str,
    status: str = "online",
    host_facts: dict | None = None,
    agent_version: str | None = None,
    db: AsyncSession | None = None,
) -> bool:
    """Refresh ``last_seen_at`` (+ optional host_facts / version) for a runner.

    ``last_seen_at`` recency is the authoritative online signal, so this is
    called on connect and on every heartbeat. Returns ``False`` when the runner
    is gone or revoked: the WebSocket, authenticated only once, learns it here
    at its next heartbeat whatever became of the Redis revoke signal.

    Presence events are the transitions of the cached ``status`` column: this
    touch moves an active runner ``offline`` -> ``online`` (firing
    ``Runner online``) and :func:`sweep_stale_runners` moves it back once the
    heartbeat went stale (firing ``Runner offline``). Enrollment and the revoke
    route fire their own event when they change it, and a revoked row is
    final. A socket closing moves nothing: every deploy closes every runner
    socket held by the old pod, and a runner that reconnects inside the offline
    window never went offline. So the two events always alternate.
    """
    values: dict[str, Any] = {
        "last_seen_at": datetime.now(UTC),
        "transport": transport,
        "status": status,
    }
    if host_facts is not None:
        values["host_facts"] = host_facts
    if agent_version is not None:
        values["agent_version"] = agent_version

    async def _run(session: AsyncSession) -> bool:
        # Presence transition, decided under the row lock: the sweep's guarded
        # UPDATE waits for this transaction and then no longer matches (the
        # row was just seen), and a concurrent heartbeat reads the status this
        # one wrote - so "Runner online" fires once, and only for a runner
        # that was not already online.
        row = (
            await session.execute(
                select(Runner.status, Runner.is_active)
                .where(Runner.id == runner_id)
                .with_for_update()
            )
        ).one_or_none()
        if row is None or not row.is_active:
            # Deleted, or revoked: a heartbeat that authenticated just before
            # the revoke committed reads the revoked row here, and must not
            # bring it back online.
            await session.commit()
            return False
        came_online = status == "online" and row.status != "online"
        await session.execute(
            update(Runner).where(Runner.id == runner_id).values(**values)
        )
        if came_online:
            try:
                from app.services.runner_flow_events import emit_runner_presence

                # SAVEPOINT: a flush failure inside the emission must roll
                # back to here, not poison the transaction that owns the
                # liveness write (fail-open means the commit below survives).
                async with session.begin_nested():
                    await emit_runner_presence(session, runner_id, online=True)
            except Exception:
                # The flow bus must never break liveness bookkeeping.
                logger.debug(
                    "Runner online flow-event emission failed for %s",
                    runner_id,
                    exc_info=True,
                )
        await session.commit()
        if came_online:
            await _notify_flow_engine()
        return True

    if db is not None:
        return await _run(db)
    async with async_session() as own:
        return await _run(own)


#: Runners flipped offline per sweep, so a fleet-wide outage is spread over a
#: few ticks rather than written in one transaction.
_PRESENCE_SWEEP_BATCH = 500


async def sweep_stale_runners(now: datetime | None = None) -> int:
    """Flip runners whose heartbeat went stale to ``offline``; return how many.

    Each flip fires ``Runner offline`` once: the guarded UPDATE only matches an
    active runner cached ``online`` and seen longer ago than
    ``runner_offline_after_seconds``, so a second sweep (another replica, the
    next tick) finds nothing to do. Runs on the scheduler tick; see
    :func:`touch_runner` for why this, and not a socket closing, is what
    declares a runner offline.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=settings.runner_offline_after_seconds)
    # Only an active runner: a revoked row is final (the revoke route fired its
    # own offline), whatever its cached status - a heartbeat racing a revoke
    # used to rewrite it to ``online``.
    stale = and_(
        Runner.is_active.is_(True),
        Runner.status == "online",
        or_(Runner.last_seen_at.is_(None), Runner.last_seen_at < cutoff),
    )
    flipped = 0
    async with async_session() as db:
        # In id order, so two overlapping sweeps (a tick outliving its window
        # lock) take the row locks in one order instead of deadlocking.
        candidates = (
            await db.execute(
                select(Runner.id)
                .where(stale)
                .order_by(Runner.id)
                .limit(_PRESENCE_SWEEP_BATCH)
            )
        ).scalars().all()
        for runner_id in candidates:
            result = await db.execute(
                update(Runner).where(Runner.id == runner_id, stale).values(status="offline")
            )
            if not result.rowcount:
                continue
            flipped += 1
            try:
                from app.services.runner_flow_events import emit_runner_presence

                async with db.begin_nested():
                    await emit_runner_presence(db, runner_id, online=False)
            except Exception:
                # The flow bus must never break liveness bookkeeping.
                logger.debug(
                    "Runner offline flow-event emission failed for %s",
                    runner_id,
                    exc_info=True,
                )
        await db.commit()
    if flipped:
        await _notify_flow_engine()
    return flipped


async def _notify_flow_engine() -> None:
    """Nudge the orchestration reconciler after a committed flow-event emission
    (started listener runs / parked waiters). Best-effort, like every wake."""
    try:
        from app.services.orchestration_engine import notify_orchestration_wake

        await notify_orchestration_wake()
    except Exception:
        logger.debug("Orchestration wake after runner event failed", exc_info=True)


# ── Command lifecycle ───────────────────────────────────────────────────────


# The runner runs each command under an outer ceiling of ``budget + 30s`` (see
# ``apps/runner/src/executor.rs``) and only then uploads the result, retrying a
# transient upload failure with bounded backoff for up to
# ``UPLOAD_RETRY_BUDGET_SECS`` (``apps/runner/src/api.rs``; both kept in step
# with the graces below by ``tests/test_runners_transport.py``).
# These are the server side of that shared contract: the reclaim lease must
# EXCEED the runner's full execute-and-upload window, or a valid long-running
# command would be reclaimed and redelivered while its first execution is still
# active - a concurrent duplicate of a mutating action. Keep the 30s execution
# grace in lock-step with the runner's ceiling. The agent's result wait
# (:func:`default_result_wait_seconds`) sits between the two: past the runner's
# window, before the lease.
_RUNNER_EXECUTION_GRACE_SECONDS = 30
_RUNNER_UPLOAD_GRACE_SECONDS = 60
#: The runner's ``UPLOAD_RETRY_BUDGET_SECS``: how long after a command ends its
#: result may still be arriving, retried through a platform restart. Strictly
#: inside the upload grace, so an upload never races a redelivery.
RUNNER_UPLOAD_RETRY_BUDGET_SECONDS = 50
#: Half of what the upload grace keeps past the retry budget: how long the
#: result wait outlasts the runner's last upload attempt (the command's trip to
#: the machine, an attempt the platform is still committing, the 1 s re-read).
#: The other half is left to record the give-up before the lease runs out.
_RESULT_WAIT_MARGIN_SECONDS = (
    _RUNNER_UPLOAD_GRACE_SECONDS - RUNNER_UPLOAD_RETRY_BUDGET_SECONDS
) // 2
#: The runner's ``MAX_RESULT_META_BYTES`` (``apps/runner/src/protocol.rs``):
#: the largest ``meta`` part - the result's JSON - a result upload may carry.
#: The form parser holds that part in memory, so it stays bounded, above every
#: result a capability produces within its own caps: two 1 MiB shell streams
#: written at the worst JSON escaping (six bytes a byte) are 12 MiB. The binary
#: ``file`` part has its own bound, ``RUNNER_MAX_RESULT_BYTES``.
RUNNER_MAX_RESULT_META_BYTES = 16 * 1024 * 1024


def runner_execution_ceiling_seconds() -> int:
    """How long a runner may take to run a command, from its delivery.

    The budget every command is handed (:func:`claim_next_command`) plus the
    execution grace (``executor.rs``); the wait for the machine's turn - one
    command at a time - counts against it. A ``run_shell`` ``timeout``
    argument only bounds the shell inside it: the ceiling still ends a longer
    one, so no command outlasts it.
    """
    return (
        max(1, settings.runner_command_default_timeout_seconds)
        + _RUNNER_EXECUTION_GRACE_SECONDS
    )


def runner_report_window_seconds() -> int:
    """How long after its delivery a command's result may still validly land:
    the execution ceiling, then the upload retries."""
    return runner_execution_ceiling_seconds() + RUNNER_UPLOAD_RETRY_BUDGET_SECONDS


def default_result_wait_seconds() -> int:
    """How long after its delivery the default result wait covers a command.

    The report window plus the margin (205 s with the default budget), which
    ends before :func:`_command_lease_seconds`: the give-up recorded when it
    ends lands while the command still cannot be delivered again.
    """
    return runner_report_window_seconds() + _RESULT_WAIT_MARGIN_SECONDS


def _command_lease_seconds() -> int:
    """How long a ``dispatched`` command is considered still in-flight.

    A command handed to a transport but not completed within this window is
    presumed lost (the runner crashed / dropped after claiming, or its result
    upload failed) and becomes reclaimable by the next drain.

    The window is the per-command execution budget the runner is given PLUS the
    runner's own overrun grace and result-upload budget, so the lease provably
    exceeds the runner's complete execute-and-upload window. Without that slack
    a valid long-running command (a big file read, a slow shell) would be
    reclaimed and redelivered WHILE its first execution is still active,
    producing a concurrent duplicate of a mutating command. Reusing the
    per-command timeout as the base keeps the budget a single knob; the graces
    mirror the runner contract (``executor.rs`` ``budget + 30`` ceiling +
    ``api.rs`` upload retry).
    """
    return runner_execution_ceiling_seconds() + _RUNNER_UPLOAD_GRACE_SECONDS


async def claim_next_command(session: AsyncSession, runner_id: Any) -> dict | None:
    """Atomically claim the next deliverable command for a runner.

    Deliverable = a ``pending`` row, OR a ``dispatched`` row whose lease has
    expired (a redelivery after a crashed / dropped runner — the durable
    backstop the module promises). ``FOR UPDATE SKIP LOCKED`` guarantees exactly
    one transport (WS on some replica, or a long-poll) claims a given row even
    under concurrent drains.

    Redelivery is at-least-once: a command the runner executed but whose result
    upload was lost can be redelivered. Read-only kinds are idempotent; the
    mutating kinds are exactly the ones an operator gates behind the approval /
    ``full_control`` opt-in, and a duplicate terminal result is dropped by
    :func:`submit_result` (first result wins).

    Never for a revoked runner - read in the same statement, so a socket whose
    revoke signal was lost is handed nothing once the revoke commits -
    and never a command its waiter gave up on.

    Re-authorized against the runner's policy as it is at delivery, as the
    approval route does: a command the policy no longer allows (a capability
    removed, a shell or path list tightened while it was queued) is cancelled
    with the refusal and the next one is claimed. What is checked is
    the payload the runner gets, its secret variables restored.
    """
    from app.services.runners import authorize_command, delivery_payload

    lease_cutoff = datetime.now(UTC) - timedelta(seconds=_command_lease_seconds())
    stmt = (
        select(RunnerCommand)
        .where(
            RunnerCommand.runner_id == runner_id,
            select(Runner.id)
            .where(Runner.id == runner_id, Runner.is_active.is_(True))
            .exists(),
            RunnerCommand.given_up_at.is_(None),
            or_(
                RunnerCommand.status == "pending",
                and_(
                    RunnerCommand.status == "dispatched",
                    RunnerCommand.dispatched_at.is_not(None),
                    RunnerCommand.dispatched_at < lease_cutoff,
                ),
            ),
        )
        .order_by(RunnerCommand.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    while True:
        cmd = (await session.execute(stmt)).scalar_one_or_none()
        if cmd is None:
            await session.rollback()
            return None
        runner = await session.get(Runner, cmd.runner_id, populate_existing=True)
        if runner is None:
            refusal = "This runner has been revoked."
        else:
            payload, delivered = await restored_command_payload(session, cmd)
            refusal = authorize_command(runner, cmd.kind, payload)
        if refusal is None:
            break
        _finish(cmd, "cancelled", f"Blocked by current runner policy at delivery: {refusal}")
        await _emit_command_terminal(session, cmd)
        await session.commit()
        await _announce_command_terminal(cmd.id, cmd.status)
    cmd.status = "dispatched"
    cmd.dispatched_at = datetime.now(UTC)
    _record_delivered_secrets(cmd, delivered)
    # The runner's allowed folders and the read cap are added at delivery,
    # from the runner as it is now; the stored payload stays what was
    # asked.
    payload = delivery_payload(runner, cmd.kind, payload)
    await session.commit()
    return {
        "id": str(cmd.id),
        "kind": cmd.kind,
        "payload": payload,
        "timeout": settings.runner_command_default_timeout_seconds,
    }


#: What a command nobody waits for says when the transport finishes it instead
#: of delivering it (a failed send, a lease that ran out).
_GIVEN_UP_UNDELIVERED = (
    "Not delivered: whoever dispatched it had stopped waiting for its result."
)


async def _requeue_command(command_id: Any) -> None:
    """Return an undelivered, just-claimed command to ``pending``.

    A WS send that fails after the row was marked ``dispatched`` means the
    command never reached the runner. Without this the row would sit
    ``dispatched`` until its lease expired — delayed redelivery and, worse, a
    slot consumed against the in-flight cap in the meantime. Only reverts a row
    still in ``dispatched`` so it can never clobber a result that raced in, and
    finishes it instead when its waiter gave up in the meantime: read
    under the row lock, so a give-up landing mid-send is always seen.
    """
    try:
        async with async_session() as db:
            cmd = await db.scalar(
                select(RunnerCommand)
                .where(
                    RunnerCommand.id == command_id,
                    RunnerCommand.status == "dispatched",
                )
                .with_for_update()
            )
            if cmd is None:
                await db.rollback()
                return
            if cmd.given_up_at is None:
                cmd.status = "pending"
                cmd.dispatched_at = None
                await db.commit()
                return
            _finish(cmd, "timeout", _GIVEN_UP_UNDELIVERED)
            await _emit_command_terminal(db, cmd)
            await db.commit()
        await _announce_command_terminal(command_id, "timeout")
    except Exception:
        logger.debug("Failed to requeue runner command %s", command_id, exc_info=True)


async def _drain_and_push(conn: RunnerConnection, runner_id: Any) -> int:
    """Claim + push every currently-deliverable command to a live local WS."""
    pushed = 0
    while conn.alive:
        async with async_session() as db:
            cmd = await claim_next_command(db, runner_id)
        if cmd is None:
            break
        if not await conn.send({"type": "command", "command": cmd}):
            # Send failed — the command never reached the runner. Return it to
            # ``pending`` so a reconnect / long-poll redelivers it immediately
            # instead of waiting out the lease, and stop pushing on a dead
            # socket.
            await _requeue_command(cmd["id"])
            break
        pushed += 1
    return pushed


async def dispatch_command(
    *,
    runner_id: Any,
    kind: str,
    payload: dict | None,
    user_id: Any | None,
    agent_id: Any | None = None,
    conversation_id: Any | None = None,
    assignment_run_id: Any | None = None,
    flow_run_id: Any | None = None,
    requires_approval: bool = False,
) -> tuple[str, str]:
    """Enqueue a command and (unless it needs approval) wake the runner now.

    Returns ``(command_id, status)`` where status is ``"pending"`` (dispatched
    / awaiting the runner), ``"pending_approval"`` (held for a human),
    ``"busy"`` (the runner is at its concurrent-command cap) or ``"revoked"``
    (the runner was revoked since the caller loaded it) - nothing is enqueued
    and ``command_id`` is empty for the last two.
    """
    from app.services.runners import requested_payload

    initial_status = "pending_approval" if requires_approval else "pending"
    command_uuid = uuid.uuid4()
    # Stored with markers bound to the command, not to the run, turn or flow
    # that dispatched it: delivery restores them by the command alone. Only a
    # delivery writes the delivered-secrets record, never what was asked.
    with known_secrets_scope(command_uuid):
        stored_payload = redact_known_secrets(requested_payload(payload))
    command_id = ""
    try:
        async with async_session() as db:
            # Under the runner's row lock (the command_count update below
            # takes it anyway): a revoke either ends this command with the
            # others or has committed first and is seen here - never a command
            # left behind that nothing will deliver or end.
            active = await db.scalar(
                select(Runner.is_active).where(Runner.id == runner_id).with_for_update()
            )
            if not active:
                return "", "revoked"
            # Bounded concurrency: refuse to pile past the per-runner in-flight
            # cap so a burst of parallel tool calls can't swamp one machine.
            cap = settings.runner_max_inflight_commands
            if cap:
                # A ``dispatched`` row past its lease is reclaimable (the
                # runner is gone), so it must NOT keep consuming a slot forever
                # — exclude it, or a crashed runner would permanently wedge the
                # in-flight cap.
                lease_cutoff = datetime.now(UTC) - timedelta(
                    seconds=_command_lease_seconds()
                )
                inflight = await db.scalar(
                    select(func.count())
                    .select_from(RunnerCommand)
                    .where(
                        RunnerCommand.runner_id == runner_id,
                        RunnerCommand.status.not_in(tuple(COMMAND_TERMINAL_STATUSES)),
                        or_(
                            RunnerCommand.status != "dispatched",
                            RunnerCommand.dispatched_at.is_(None),
                            RunnerCommand.dispatched_at >= lease_cutoff,
                        ),
                    )
                )
                if inflight is not None and inflight >= cap:
                    return "", "busy"
            cmd = RunnerCommand(
                id=command_uuid,
                runner_id=runner_id,
                user_id=user_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
                flow_run_id=flow_run_id,
                kind=kind,
                payload=stored_payload,
                status=initial_status,
            )
            db.add(cmd)
            await db.flush()
            command_id = str(cmd.id)
            await db.execute(
                update(Runner)
                .where(Runner.id == runner_id)
                .values(command_count=Runner.command_count + 1)
            )
            # Immutable per-command audit entry (plan Component 6: "every
            # dispatched command is written via log_audit" — the trail is
            # itself evidence). Company-managed runs have no owner user; the
            # durable runner_commands row remains the trail in that case
            # (mirrors log_security_event's graceful skip for
            # non-identifiable users).
            if user_id is not None:
                from app.models.user import User
                from app.services.audit import log_audit

                owner = await db.get(User, user_id)
                if owner is not None:
                    verb = "await approval" if requires_approval else "dispatch"
                    await log_audit(
                        db,
                        user=owner,
                        action="runner_command",
                        entity_type="runner",
                        entity_id=runner_id,
                        entity_name=kind,
                        summary=f"Runner command '{kind}' ({verb})",
                        details={
                            "command_id": command_id,
                            "kind": kind,
                            "runner_id": str(runner_id),
                            "agent_id": str(agent_id) if agent_id else None,
                            "conversation_id": (
                                str(conversation_id) if conversation_id else None
                            ),
                            "assignment_run_id": (
                                str(assignment_run_id) if assignment_run_id else None
                            ),
                            "requires_approval": requires_approval,
                            # Summarized once redacted: a cut never leaves part of a value.
                            "params": _summarize_payload(stored_payload),
                        },
                    )
            await db.commit()

        if requires_approval:
            return command_id, "pending_approval"

        await _notify_new_command(runner_id)
        return command_id, "pending"
    except asyncio.CancelledError:
        if command_id:
            # The caller never learns this command's id, so nobody will wait
            # for it: give it up (nothing to do if the insert never committed).
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    give_up_on_command(
                        command_id,
                        "Whoever dispatched it was cancelled before it got the command.",
                        undelivered_status="cancelled",
                    )
                )
        raise


async def _notify_new_command(runner_id: Any) -> None:
    """Instant dispatch: push locally if we hold the WS, else wake via Redis."""
    conn = _connections.get(str(runner_id))
    if conn is not None and conn.alive:
        await _drain_and_push(conn, runner_id)
        return
    try:
        redis = await redis_client()
        await redis.publish(_wake_channel(runner_id), "1")
    except Exception:
        # The durable row + long-poll are the backstop; a failed wake only
        # costs latency, never delivery.
        logger.debug("Runner wake publish failed for %s", runner_id, exc_info=True)


async def submit_result(
    command_id: Any,
    *,
    status: str,
    result: dict | None = None,
    error: str | None = None,
    result_file_id: Any | None = None,
    db: AsyncSession | None = None,
) -> bool:
    """Record a terminal result and wake the awaiting agent tool via Redis.

    Idempotent: the first terminal result wins. At-least-once redelivery (a
    lease-expired command reclaimed after the runner already ran it) can produce
    a second upload; dropping it keeps the recorded evidence — the hash-verified
    capture the agent already surfaced — from being clobbered by the duplicate.

    When the caller already holds the command's row lock (the result-upload
    endpoint claims it with ``SELECT ... FOR UPDATE`` so artifact persistence
    and this terminal transition are one atomic unit), it MUST pass its session
    via ``db`` — opening a second session here would deadlock against that
    lock. The commit below is what releases it.
    """

    async def _apply(session: AsyncSession) -> str:
        cmd = await session.get(RunnerCommand, command_id)
        if cmd is None:
            return "missing"
        if cmd.status in COMMAND_TERMINAL_STATUSES:
            return "duplicate"
        recorded, cmd.result, cmd.error = await _without_secrets(session, cmd, status, result, error)
        cmd.result = _with_stopped_by(cmd, cmd.result)
        cmd.status = recorded
        cmd.result_file_id = result_file_id
        cmd.completed_at = datetime.now(UTC)
        await _emit_command_terminal(session, cmd)
        await session.commit()
        return recorded

    if db is not None:
        outcome = await _apply(db)
    else:
        async with async_session() as own:
            outcome = await _apply(own)
    if outcome == "missing":
        return False
    if outcome != "duplicate":
        await _announce_command_terminal(command_id, outcome)
    return True


def _with_stopped_by(cmd: RunnerCommand, result: dict | None) -> dict | None:
    """A stopped command's result with who stopped it from its card.

    The runner only says it was stopped (``stopped: "user"``); who asked is
    on the row since the stop route recorded it. A runner never chooses it:
    a ``stopped_by`` it sent is replaced, or dropped when nobody asked here.
    """
    from app.services.runners import STOPPED_BY_KEY

    if not isinstance(result, dict) or result.get("stopped") != "user":
        return result
    out = {k: v for k, v in result.items() if k != "stopped_by"}
    asked = (cmd.payload or {}).get(STOPPED_BY_KEY)
    if isinstance(asked, dict) and isinstance(asked.get("user_id"), str):
        name = asked.get("name")
        out["stopped_by"] = {"user_id": asked["user_id"], "name": name if isinstance(name, str) else None}
    return out


async def _emit_command_terminal(session: AsyncSession, cmd: RunnerCommand) -> None:
    """Terminal chokepoint -> flow event bus, inside the terminal write's
    transaction: listener flows react to the result, and a parked
    `Runner command` node's poll gets pre-empted by the wake that follows the
    commit. Fail-open - recording the result must never depend on the bus."""
    try:
        from app.services.runner_flow_events import (
            emit_runner_command_completed,
        )

        runner = await session.get(Runner, cmd.runner_id)
        # SAVEPOINT: recording the result must never depend on the bus -
        # a failed emission rolls back to here, the terminal write and
        # the commit survive.
        async with session.begin_nested():
            await emit_runner_command_completed(session, cmd, runner)
    except Exception:
        logger.debug(
            "Runner command flow-event emission failed for %s",
            cmd.id,
            exc_info=True,
        )


async def _announce_command_terminal(command_id: Any, status: str) -> None:
    """After a committed terminal write: wake the awaiting agent tool, and the
    parked `Runner command` flow nodes (they re-check their row on the next
    reconciler cycle - waking it makes wait-mode resumes sub-second)."""
    try:
        redis = await redis_client()
        await redis.publish(_result_channel(command_id), status)
    except Exception:
        logger.debug(
            "Result wake publish failed for %s", command_id, exc_info=True
        )
    await _notify_flow_engine()


def _snapshot(cmd: RunnerCommand) -> dict:
    return {
        "id": str(cmd.id),
        "status": cmd.status,
        "result": cmd.result,
        "error": cmd.error,
        "result_file_id": str(cmd.result_file_id) if cmd.result_file_id else None,
        "kind": cmd.kind,
        "dispatched_at": cmd.dispatched_at,
    }


async def load_command(command_id: Any) -> dict | None:
    """One command row as the agent tool reads it, or ``None`` when it is gone."""
    async with async_session() as db:
        cmd = await db.get(RunnerCommand, command_id)
        return _snapshot(cmd) if cmd is not None else None


async def load_command_status(command_id: Any) -> str | None:
    """The current status of one command row, or ``None`` when it is gone.

    A cheap re-read for callers that wait on a NON-terminal transition - the
    inline approval wait of an XTM One Desktop chat watches
    ``pending_approval`` leave, which the approve / reject routes flip
    directly on the row without publishing on the result channel.
    """
    async with async_session() as db:
        status = await db.scalar(
            select(RunnerCommand.status).where(RunnerCommand.id == command_id)
        )
    return status


#: A command no runner holds yet: giving it up finishes it on the spot.
_UNDELIVERED_STATUSES: tuple[str, ...] = ("pending", "pending_approval")

#: What a given-up command says once its lease ran out with no result.
_GIVEN_UP_LEASE_EXPIRED = (
    "No result within the runner's lease, and whoever dispatched it had "
    "stopped waiting: it is not sent again."
)


def _finish(cmd: RunnerCommand, status: str, error: str) -> None:
    cmd.status = status
    cmd.error = error
    cmd.completed_at = datetime.now(UTC)


async def give_up_on_command(
    command_id: Any,
    reason: str,
    *,
    undelivered_status: str = "timeout",
    db: AsyncSession | None = None,
) -> dict | None:
    """Record that whoever dispatched a command stopped waiting for it.

    Returns the command as it now stands (``None`` when it is gone), so the
    waiter reports what the row says, not a snapshot read before the lock.
    Recorded on the row - never in Redis or in a process - so it survives a
    restart, another replica and a Redis flush. Idempotent.

    * Still queued (``pending``, or ``pending_approval`` for a flow run that
      ended): it was never delivered, so it is finished now with
      ``undelivered_status`` and ``reason``; a late approval finds it gone.
    * ``dispatched``: a runner may be running it and may still upload the real
      result, which is recorded as usual. ``given_up_at`` is stamped so it is
      never delivered again: a failed WebSocket send finishes it
      (:func:`_requeue_command`), the claim never takes it back once its lease
      runs out, and :func:`expire_given_up_commands` then finishes it.
    * Finished: left as it is.

    The row is locked, so a claim, a requeue or a result upload in flight is
    either seen whole or sees this. With ``db`` the caller's transaction is
    used and the caller commits; the result-channel wake is skipped then (the
    waiter giving up is the caller).
    """

    async def _apply(session: AsyncSession) -> tuple[dict | None, bool]:
        cmd = await session.scalar(
            select(RunnerCommand)
            .where(RunnerCommand.id == command_id)
            .with_for_update()
        )
        if cmd is None:
            return None, False
        finished = False
        if cmd.status not in COMMAND_TERMINAL_STATUSES:
            cmd.given_up_at = cmd.given_up_at or datetime.now(UTC)
            if cmd.status in _UNDELIVERED_STATUSES:
                _finish(cmd, undelivered_status, reason)
                await _emit_command_terminal(session, cmd)
                finished = True
        return _snapshot(cmd), finished

    if db is not None:
        snapshot, _ = await _apply(db)
        return snapshot
    async with async_session() as own:
        snapshot, finished = await _apply(own)
        await own.commit()
    if finished and snapshot is not None:
        await _announce_command_terminal(command_id, snapshot["status"])
    return snapshot


#: A command not finished yet (the complement of ``COMMAND_TERMINAL_STATUSES``).
_OPEN_STATUSES: tuple[str, ...] = ("pending", "pending_approval", "dispatched")


async def give_up_flow_run_commands(db: AsyncSession, flow_run_id: Any, reason: str) -> None:
    """Give up every open command a flow run dispatched, in the caller's transaction.

    For a run that is cancelled or dead-lettered: nobody in it will read those
    results. Found by ``flow_run_id``, not by the run's parked state, because
    the command is committed before the walker persists the park - a run ended
    in between has no parked state naming it.
    """
    command_ids = (
        await db.execute(
            select(RunnerCommand.id).where(
                RunnerCommand.flow_run_id == flow_run_id,
                RunnerCommand.status.in_(_OPEN_STATUSES),
            )
        )
    ).scalars().all()
    for command_id in command_ids:
        await give_up_on_command(command_id, reason, undelivered_status="cancelled", db=db)


async def expire_given_up_commands(now: datetime | None = None) -> int:
    """Finish the given-up commands whose lease ran out; return how many.

    A command whose waiter gave up while a runner held it is never delivered
    again, so once its lease - the runner's whole execute-and-upload window -
    runs out without a result, none is coming: it is finished as a timeout.
    Runs on the scheduler tick next to the presence sweep; rows another
    replica or a result upload holds are skipped (``SKIP LOCKED``).
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(seconds=_command_lease_seconds())
    finished: list[Any] = []
    async with async_session() as db:
        rows = (
            await db.execute(
                select(RunnerCommand)
                .where(
                    RunnerCommand.status == "dispatched",
                    RunnerCommand.given_up_at.is_not(None),
                    or_(
                        RunnerCommand.dispatched_at.is_(None),
                        RunnerCommand.dispatched_at < cutoff,
                    ),
                )
                .order_by(RunnerCommand.id)
                .limit(_PRESENCE_SWEEP_BATCH)
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        for cmd in rows:
            _finish(cmd, "timeout", _GIVEN_UP_LEASE_EXPIRED)
            await _emit_command_terminal(db, cmd)
            finished.append(cmd.id)
        await db.commit()
    await announce_finished_commands(finished, "timeout")
    return len(finished)


async def end_revoked_runner_commands(
    db: AsyncSession, runner_id: Any, reason: str
) -> list[Any]:
    """Finish every outstanding command of a runner being revoked.

    Nothing reaches a revoked machine any more - the claim refuses it and its
    result uploads fail authentication - so a queued, held or in-flight
    command would only leave its waiter hanging (a parked flow waits with no
    end). Runs in the revoke's transaction; the caller commits, then passes the
    returned ids to :func:`announce_finished_commands`.
    """
    rows = (
        await db.execute(
            select(RunnerCommand)
            .where(
                RunnerCommand.runner_id == runner_id,
                RunnerCommand.status.not_in(tuple(COMMAND_TERMINAL_STATUSES)),
            )
            .order_by(RunnerCommand.id)
            .with_for_update()
        )
    ).scalars().all()
    for cmd in rows:
        _finish(cmd, "cancelled", reason)
        await _emit_command_terminal(db, cmd)
    return [cmd.id for cmd in rows]


async def announce_finished_commands(command_ids: list[Any], status: str) -> None:
    """Wake the waiters of commands a committed transaction just finished."""
    for command_id in command_ids:
        await _announce_command_terminal(command_id, status)


async def cancel_held_command(command_id: Any, reason: str) -> bool:
    """Cancel a command still waiting for approval; ``True`` when this call did.

    One guarded ``UPDATE ... WHERE status = 'pending_approval'``, so a decision
    that landed a moment earlier (approve flipped it to ``pending``, reject to
    ``cancelled``) is never overwritten. Used when the chat turn that offered a
    command inline ends before anyone decided: a command offered with
    its chat context must not survive that context and run later, from the
    Desktop page, with none of it.
    """
    async with async_session() as db:
        result = await db.execute(
            update(RunnerCommand)
            .where(
                RunnerCommand.id == command_id,
                RunnerCommand.status == "pending_approval",
            )
            .values(
                status="cancelled",
                error=reason,
                completed_at=datetime.now(UTC),
            )
        )
        await db.commit()
    return bool(result.rowcount)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _result_wait_ends(
    *,
    started: float,
    fixed: float | None,
    delivery: tuple[datetime, float] | None,
    not_after: float | None,
) -> float:
    """The ``time.monotonic()`` instant a wait for a command's result ends.

    * ``fixed`` (an explicit wait, or ``RUNNER_RESULT_WAIT_TIMEOUT_SECONDS``
      set by an operator): that many seconds from ``started``, the start of
      the wait.
    * Otherwise :func:`default_result_wait_seconds` from the command's delivery
      once a machine took it - ``delivery`` is its ``dispatched_at`` and when
      this wait first saw it, and the earlier reading wins, so neither a clock
      behind the claiming replica's nor a delivery noticed late carries the
      wait past the lease - and from ``started`` while none has.
    * Never past ``not_after``, the caller's own deadline.
    """
    if fixed is not None:
        ends = started + fixed
    elif delivery is None:
        ends = started + default_result_wait_seconds()
    else:
        dispatched_at, seen = delivery
        window = default_result_wait_seconds()
        since_delivery = (_utcnow() - dispatched_at).total_seconds()
        ends = min(time.monotonic() + window - since_delivery, seen + window)
    return ends if not_after is None else min(ends, not_after)


async def await_result(
    command_id: Any,
    timeout: float | None = None,
    *,
    not_after: float | None = None,
) -> dict | None:
    """Block until a command reaches a terminal state (or the wait ends).

    Event-driven: subscribes to the command's Redis result channel and returns
    the instant the runner uploads its result, with a 1 s DB re-poll as the
    backstop so a missed pub/sub message can never wedge the tool.

    ``timeout`` seconds from the start of the wait, or
    ``RUNNER_RESULT_WAIT_TIMEOUT_SECONDS`` when an operator set it; unset, as
    long as the machine may take to report, from the command's delivery
    (:func:`_result_wait_ends`). Never past ``not_after``, a
    ``time.monotonic()`` instant. A command still open when the wait ends is
    returned as it stands.
    """
    fixed = timeout or settings.runner_result_wait_timeout_seconds
    started = time.monotonic()
    delivery: tuple[datetime, float] | None = None

    def _ends(command: dict | None) -> float:
        nonlocal delivery
        dispatched_at = command.get("dispatched_at") if command else None
        if not isinstance(dispatched_at, datetime):
            delivery = None
        elif delivery is None or delivery[0] != dispatched_at:
            delivery = (dispatched_at, time.monotonic())
        return _result_wait_ends(
            started=started, fixed=fixed, delivery=delivery, not_after=not_after
        )

    # Check before subscribing — the result may already be in.
    current = await load_command(command_id)
    if current and current["status"] in COMMAND_TERMINAL_STATUSES:
        return current

    pubsub_client = await redis_pubsub_client()
    pubsub = await open_pubsub(pubsub_client, _result_channel(command_id))
    try:
        while True:
            left = _ends(current) - time.monotonic()
            if left <= 0:
                return current  # non-terminal → caller treats as timeout
            with contextlib.suppress(Exception):
                await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=min(1.0, left)
                )
            current = await load_command(command_id)
            if current and current["status"] in COMMAND_TERMINAL_STATUSES:
                return current
    finally:
        await close_pubsub(pubsub, pubsub_client)


# ── WebSocket serve loop (called by the router) ─────────────────────────────


async def _wake_listener(conn: RunnerConnection, runner_id: Any) -> None:
    """React to cross-replica signals for this runner.

    * ``runner:wake:{id}`` — a command was enqueued elsewhere; drain + push.
    * ``runner:control:{id}`` — a control signal: ``stop:<command id>`` is
      forwarded to the runner as a ``stop`` frame; any other signal
      (``"revoke"``) closes the socket immediately so a revoked credential's
      connection dies at once rather than waiting out the heartbeat window.
    """
    wake = _wake_channel(runner_id)
    control = _control_channel(runner_id)
    pubsub_client = await redis_pubsub_client()
    pubsub = await open_pubsub(pubsub_client, wake, control)
    try:
        while conn.alive:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=5.0)
            if msg is None or not conn.alive:
                continue
            if msg.get("channel") == control:
                frame = stop_frame(msg.get("data"))
                if frame is not None:
                    # Stop one running command: the runner ends its
                    # tree and reports it; the socket stays open.
                    await conn.send(frame)
                    continue
                conn.alive = False
                with contextlib.suppress(Exception):
                    await conn.websocket.close(code=4401)
                break
            await _drain_and_push(conn, runner_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.debug("Wake listener ended for %s", runner_id, exc_info=True)
    finally:
        await close_pubsub(pubsub, pubsub_client)


async def serve_runner_websocket(websocket: WebSocket, runner: Runner) -> None:
    """Serve a runner's WebSocket: push commands, absorb heartbeats.

    Control frames only. The runner executes each pushed command and uploads
    its result over HTTP; heartbeats keep ``last_seen_at`` fresh so the
    online signal stays accurate without a dedicated ping/pong protocol.
    """
    runner_id = runner.id
    conn = register_connection(runner_id, websocket)
    if not await touch_runner(runner_id, transport="ws"):
        # Revoked between the credential check and here.
        unregister_connection(runner_id, conn)
        with contextlib.suppress(Exception):
            await websocket.close(code=4401)
        return
    listener = asyncio.create_task(_wake_listener(conn, runner_id))
    try:
        # Drain anything queued while the runner was away.
        await _drain_and_push(conn, runner_id)
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=settings.runner_offline_after_seconds,
                )
            except TimeoutError:
                # No heartbeat within the offline window → treat as gone.
                break
            msg_type = message.get("type") if isinstance(message, dict) else None
            if msg_type in ("heartbeat", "hello"):
                if not await touch_runner(
                    runner_id,
                    transport="ws",
                    host_facts=message.get("host_facts"),
                    agent_version=message.get("agent_version"),
                ):
                    # Revoked: the database says so whatever became of the
                    # Redis revoke signal, which is only the fast path.
                    conn.alive = False
                    with contextlib.suppress(Exception):
                        await websocket.close(code=4401)
                    break
                # A hello may report new capabilities becoming ready; re-drain.
                if msg_type == "hello":
                    await _drain_and_push(conn, runner_id)
            # "ack" and unknown frames are intentionally ignored.
    except Exception:
        logger.debug("Runner WS loop ended for %s", runner_id, exc_info=True)
    finally:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await listener
        unregister_connection(runner_id, conn)
        # Deliberately no presence write: a closed socket is also every
        # deploy, and the runner reconnects to the next pod within seconds.
        # sweep_stale_runners declares it offline if its heartbeat stops.
