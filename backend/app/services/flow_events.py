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

"""Typed flow events — declaration lookup, coercion, emission, dispatch.

The UE-Blueprint event pipeline, server side:

* :func:`coerce_event_fields` — validate + type-coerce the emit node's rendered
  mappings against the declaration's signature (missing required field is an
  error, wrong type coerces when unambiguous and errors otherwise).
* :func:`emit_flow_event` — persist the :class:`FlowEvent` row, then dispatch
  **synchronously**: one new run per bound ``trigger.event`` node within the
  emission's scope.  Parked ``util.wait_event`` nodes are NOT woken here —
  they poll the events table on their own resume tick, which keeps emit free
  of cross-run state surgery.

Scope decides the reach, chosen at EMIT time only (listeners just bind to a
declaration, like UE):

* ``run``    — heard only by ``util.wait_event`` inside the emitting run.
* ``flow``   — any run of the same flow: waiters, plus new runs for the
  flow's own ``trigger.event`` nodes.
* ``global`` — every ACTIVE flow of the same owner carrying a bound
  ``trigger.event``.

The runaway guard is ``chain_depth``: a run started by an event inherits the
event's depth, and an emit whose depth exceeds
:data:`EVENT_CHAIN_MAX_DEPTH` still persists (waiters, audit) but starts no
further runs — UE's recursion guard, adapted to fire-and-forget.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Text, cast, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.flow import FLOW_STATUS_ACTIVE, Flow
from app.models.flow_event import (
    EVENT_CHAIN_MAX_DEPTH,
    EVENT_SCOPE_FLOW,
    EVENT_SCOPE_GLOBAL,
    EVENT_SCOPE_RUN,
    FlowEvent,
    FlowEventType,
)
from app.services.sub_agent_result_text import clipped

logger = logging.getLogger(__name__)


def coerce_event_fields(
    declared: list[dict], values: dict[str, str]
) -> tuple[dict, list[str]]:
    """Coerce rendered (string) mappings to the declaration's types.

    Returns ``(payload, errors)`` — a non-empty errors list means the emit
    must fail through the node's error path rather than fire a malformed
    event at every listener.
    """
    payload: dict = {}
    errors: list[str] = []
    for f in declared or []:
        name = (f.get("name") or "").strip()
        if not name:
            continue
        ftype = f.get("type") or "string"
        raw = values.get(name)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if f.get("required"):
                errors.append(f"Required field `{name}` has no value.")
            continue
        text = raw if isinstance(raw, str) else json.dumps(raw)
        try:
            if ftype == "number":
                payload[name] = float(text) if "." in text else int(text)
            elif ftype == "boolean":
                lowered = text.strip().lower()
                if lowered in ("true", "1", "yes", "on"):
                    payload[name] = True
                elif lowered in ("false", "0", "no", "off"):
                    payload[name] = False
                else:
                    raise ValueError(text)
            elif ftype == "json":
                payload[name] = json.loads(text)
            else:
                payload[name] = text
        except (ValueError, json.JSONDecodeError):
            errors.append(
                f"Field `{name}` expects {ftype}, got {text[:80]!r}."
            )
    return payload, errors


async def get_event_type(
    db: AsyncSession, event_type_id: str | uuid.UUID | None
) -> FlowEventType | None:
    if not event_type_id:
        return None
    try:
        type_uuid = (
            event_type_id
            if isinstance(event_type_id, uuid.UUID)
            else uuid.UUID(str(event_type_id))
        )
    except ValueError:
        return None
    return (
        await db.execute(
            select(FlowEventType).where(FlowEventType.id == type_uuid)
        )
    ).scalar_one_or_none()


def _debounce_seconds(config: dict) -> int:
    """The node's debounce window in seconds — 0 when unset or unusable."""
    raw = config.get("debounce_seconds")
    if raw in (None, ""):
        return 0
    try:
        return max(0, int(float(str(raw).strip())))
    except (TypeError, ValueError):
        return 0


async def _debounce_allows(
    flow_id: uuid.UUID, node_id: object, config: dict
) -> bool:
    """Whether this trigger node may start a run, or is inside its window.

    A per-node rate limit, not a payload-aware suppressor: ``debounce_seconds``
    means "do not start me more than once every N seconds", which is what a
    listener needs when the producer writes in a loop (fact reconciliation
    persists once per reconciled fact). The event row is always written
    regardless — the audit trail and parked waiters must not lose emissions
    because one listener is busy.

    Fail-OPEN: Redis being unavailable must degrade to the pre-debounce
    behaviour (the flow still reacts), never to a silently deaf trigger.
    """
    window = _debounce_seconds(config)
    if window <= 0:
        return True
    key = f"flow_event_debounce:{flow_id}:{node_id or '-'}"
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        # NX+EX is the whole mechanism: the winner starts the run and owns the
        # window; everyone else in it gets None back and stands down.
        acquired = await redis.set(key, "1", nx=True, ex=window)
    except Exception:
        logger.debug(
            "Flow event debounce unavailable for node %s — allowing the run",
            node_id,
            exc_info=True,
        )
        return True
    if not acquired:
        logger.debug(
            "Flow event debounced for flow %s node %s (%ds window)",
            flow_id,
            node_id,
            window,
        )
        return False
    return True


async def _release_debounce(flow_id: uuid.UUID, node_id: object, config: dict) -> None:
    """Give back a window :func:`_debounce_allows` granted to a run that did
    not start, so the next emission is heard. Fail-open, like it."""
    if _debounce_seconds(config) <= 0:
        return
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        await redis.delete(f"flow_event_debounce:{flow_id}:{node_id or '-'}")
    except Exception:
        logger.debug(
            "Flow event debounce release unavailable for node %s", node_id, exc_info=True
        )


async def purge_old_flow_events(
    *, older_than: timedelta | None = None, limit: int | None = None
) -> int:
    """Delete emissions past the retention window. Returns rows removed.

    ``flow_events`` is append-only and, since durable memory writes started
    emitting, its biggest producer is a table nobody prunes by hand.
    An emission has already done its dispatch work synchronously at emit time,
    so an old row is audit history — valuable for a while, unbounded forever.

    **The window has a floor at ``flow_run_timeout_seconds``.** A parked
    ``util.wait_event`` hears an emission by polling for the first row created
    after it parked, and it never marks one consumed, so deleting a row a
    waiter has not reached yet leaves that run parked until its own timeout.
    A run cannot outlive that timeout, so a window at least that long provably
    cannot strand one — and clamping here means a misconfigured
    ``flow_event_retention_days`` degrades to "keeps more than asked" rather
    than to silently hung flows.

    Opens its own session and is capped per call, oldest first, like the other
    autonomous retention sweeps.
    """
    from app.core.config import settings
    from app.core.database import async_session

    if older_than is None:
        days = settings.flow_event_retention_days
        if not days or days <= 0:
            return 0
        older_than = timedelta(days=days)
    floor = timedelta(seconds=max(0, settings.flow_run_timeout_seconds))
    window = max(older_than, floor)
    cutoff = datetime.now(UTC) - window
    batch = limit if limit is not None else settings.flow_event_sweep_batch

    async with async_session() as db:
        target_ids = (
            (
                await db.execute(
                    select(FlowEvent.id)
                    .where(FlowEvent.created_at < cutoff)
                    .order_by(FlowEvent.created_at.asc())
                    .limit(min(max(batch, 1), 20000))
                )
            )
            .scalars()
            .all()
        )
        if not target_ids:
            return 0
        # The cutoff is re-applied to the DELETE itself, not just to the SELECT
        # that chose the ids — the same guard the shared-state sweep uses, so
        # the statement that actually removes rows is the one enforcing the
        # retention predicate.
        result = await db.execute(
            delete(FlowEvent).where(
                FlowEvent.id.in_(target_ids), FlowEvent.created_at < cutoff
            )
        )
        await db.commit()
    return result.rowcount or 0


@dataclass
class EmitResult:
    event: FlowEvent
    started_run_ids: list[str] = field(default_factory=list)
    # True when the depth cap suppressed run dispatch.
    chain_capped: bool = False


async def emit_flow_event(
    db: AsyncSession,
    *,
    event_type: FlowEventType,
    user_id: uuid.UUID | None,
    flow_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
    scope: str,
    payload: dict,
    chain_depth: int,
    exclude_flow_id: uuid.UUID | None = None,
) -> EmitResult:
    """Persist the emission and start bound ``trigger.event`` runs.

    The caller owns the transaction (commit + orchestration wake), exactly
    like :func:`app.services.flow_runner.create_flow_run`.

    *exclude_flow_id* names a flow that must NOT be started by this emission
    even though it binds the type — the emission is a side effect of that
    flow's own run, so triggering it would be a guaranteed self-loop rather
    than a reaction. It is deliberately narrower than ``flow_id``,
    which is provenance and also drives ``scope="flow"`` matching: a flow
    emitting to its own scope on purpose (``util.emit_event``) still wants to
    be heard, so only callers that emit *implicitly* on a flow's behalf pass
    this.
    """
    event = FlowEvent(
        event_type_id=event_type.id,
        user_id=user_id,
        flow_id=flow_id,
        run_id=run_id,
        scope=scope,
        payload=payload,
        chain_depth=chain_depth,
    )
    db.add(event)
    await db.flush()
    result = EmitResult(event=event)

    if scope == EVENT_SCOPE_RUN:
        return result  # waiters in this run poll the row; nothing to start
    if chain_depth > EVENT_CHAIN_MAX_DEPTH:
        result.chain_capped = True
        logger.warning(
            "Flow event %s (type '%s') hit the chain-depth cap (%d) — "
            "no listener runs started.",
            event.id,
            event_type.name,
            EVENT_CHAIN_MAX_DEPTH,
        )
        return result

    # Imported here: flow_runner imports this module from an executor, and
    # flow_triggers imports flow_runner — a module-level import would cycle.
    from app.services.assignment_pause import (
        FlowOwnerDeactivatedError,
        owner_not_deactivated,
    )
    from app.services.flow_runner import (
        create_flow_run,
        find_trigger_nodes,
        matches_event_filter,
    )

    # A deactivated owner's listener is not started unless company-managed; the
    # emission itself is still recorded, like one nobody listens to.
    if scope == EVENT_SCOPE_FLOW:
        flows = (
            (
                await db.execute(
                    select(Flow).where(
                        Flow.id == flow_id,
                        Flow.status == FLOW_STATUS_ACTIVE,
                        owner_not_deactivated(Flow),
                    )
                )
            )
            .scalars()
            .all()
        )
    elif scope == EVENT_SCOPE_GLOBAL:
        # The emitter's own flows AND company-managed ones: a company
        # listener (user_id NULL) is platform-wide by definition — without
        # the second arm it would never hear anything.
        #
        # The graph pre-filter is what keeps this affordable now that durable
        # memory writes emit: without it every active flow of the owner
        # plus every company-managed flow is materialised — full `graph` JSON
        # and all — and walked in Python on EVERY memory write, and fact
        # extraction, reconciliation and consolidation each persist in a loop.
        # A bound `trigger.event` node stores the type id verbatim in its
        # config, so a graph that does not contain that string cannot match;
        # the LIKE therefore has no false negatives, and the false positives it
        # does let through are re-checked by ``find_trigger_nodes`` below. This
        # is a narrowing, not a substitute for the exact match.
        #
        # Not the real fix: that is a binding side table keyed on
        # (event_type_id, flow_id, node_id) maintained on flow save, which
        # turns this into an indexed lookup. Deliberately out of scope here —
        # it changes dispatch for every event type, not just memory's.
        flows = (
            (
                await db.execute(
                    select(Flow).where(
                        or_(
                            Flow.user_id == user_id,
                            Flow.is_company_managed.is_(True),
                        ),
                        Flow.status == FLOW_STATUS_ACTIVE,
                        owner_not_deactivated(Flow),
                        cast(Flow.graph, Text).like(f"%{event_type.id}%"),
                    )
                )
            )
            .scalars()
            .all()
        )
    else:
        flows = []

    wanted_type = str(event_type.id)
    for flow in flows:
        if exclude_flow_id is not None and flow.id == exclude_flow_id:
            # This emission is a side effect of THIS flow's own run — see the
            # parameter's docstring. Skipped silently: it is the normal case
            # for every memory a listener flow writes, not an anomaly.
            continue
        for node in find_trigger_nodes(flow.graph or {}, "trigger.event"):
            config = node.get("config") or {}
            bound = (config.get("event_type_id") or "").strip()
            if bound != wanted_type:
                continue
            # Payload predicate, same JSON-subset semantics (and the same
            # helper) as `trigger.integration_event`'s filter.
            if not matches_event_filter(payload, config.get("filter")):
                continue
            # Attribution: the EMITTER first, the flow's own owner only as a
            # fallback for an ownerless platform emission. Not the other way
            # round — a company-managed flow does NOT reliably have
            # ``user_id IS NULL``: ``toggle_company_managed_list_field``
            # deliberately preserves the creator when a private flow is turned
            # company-managed. Preferring ``flow.user_id`` therefore let user A
            # emit a global event and have the listener run execute under the
            # creator B's identity — B's quotas, B's integration bindings, B's
            # visibility. That is the cross-user privilege bug; the emitter is
            # also the attribution the declared-event contract documents.
            run_user_id = user_id or flow.user_id
            if run_user_id is None:
                # No attributable owner (ownerless platform event heard by a
                # company-managed flow): the orchestration adapter refuses
                # user-less runs, so starting one would only litter run
                # history with an instant failure. Mirrors the
                # ``_skip_ownerless`` contract in flow_triggers.
                logger.warning(
                    "Skipping event listener run for flow %s: neither the "
                    "flow nor the emission has an owning user.",
                    flow.id,
                )
                continue
            # Same gate as the other automatic triggers (FR-016): the
            # skip is recorded so the flow's silence is visible in Operations.
            from app.services.flow_triggers import hold_back_if_ai_required

            if await hold_back_if_ai_required(
                db,
                flow,
                trigger_type="event",
                trigger_context={
                    "type": "event",
                    "event_name": event_type.name,
                },
            ):
                continue
            # Debounce LAST, so the window is claimed by a run that actually
            # starts. Checked earlier, an event this node filtered out — or
            # one held back by the AI gate, or dropped for want of an owner —
            # would consume the window and silence the next real trigger.
            if not await _debounce_allows(flow.id, node.get("id"), config):
                continue
            try:
                run = await create_flow_run(
                    db,
                    flow,
                    trigger_type="event",
                    # Shaped as the trigger's DECLARED outputs, so
                    # `{{t.fields.<name>}}` / `{{t.event.name}}` resolve.
                    trigger_payload={
                        "fields": payload,
                        "event": {
                            "id": str(event.id),
                            "name": event_type.name,
                            "scope": scope,
                            "chain_depth": chain_depth,
                        },
                    },
                    trigger_context={
                        "type": "event",
                        "trigger_node_id": node.get("id"),
                        "event_id": str(event.id),
                        "event_type_id": wanted_type,
                        "chain_depth": chain_depth,
                    },
                    # See ``run_user_id`` above: the emitting user, so the
                    # run's quotas and integration scopes are their own.
                    user_id=run_user_id,
                )
            except FlowOwnerDeactivatedError:
                # Owner deactivated since the query picked it: the
                # window this listener just took goes back.
                await _release_debounce(flow.id, node.get("id"), config)
                continue
            result.started_run_ids.append(str(run.id))
    return result


async def find_matching_event(
    db: AsyncSession,
    *,
    event_type_id: str,
    user_id: uuid.UUID | None,
    flow_id: uuid.UUID | None,
    run_id: uuid.UUID | None,
    after_iso: str | None,
) -> FlowEvent | None:
    """Oldest event a parked ``util.wait_event`` can hear, or None.

    A waiter hears: same-run emissions (scope ``run``), same-flow emissions
    (scope ``flow``), and global emissions from the same owner OR from the
    platform itself (ownerless — e.g. a company-managed runner's lifecycle) —
    always only those emitted AFTER it parked, so a run never consumes its
    own past.
    """
    from datetime import datetime

    try:
        type_uuid = uuid.UUID(str(event_type_id))
    except ValueError:
        return None
    query = select(FlowEvent).where(FlowEvent.event_type_id == type_uuid)
    if after_iso:
        query = query.where(
            FlowEvent.created_at > datetime.fromisoformat(after_iso)
        )
    rows = (
        (await db.execute(query.order_by(FlowEvent.created_at))).scalars().all()
    )
    for event in rows:
        if event.scope == EVENT_SCOPE_RUN and run_id and event.run_id == run_id:
            return event
        if (
            event.scope == EVENT_SCOPE_FLOW
            and flow_id
            and event.flow_id == flow_id
        ):
            return event
        if event.scope == EVENT_SCOPE_GLOBAL and (
            # Same-owner emissions — a user's global events stay theirs.
            (user_id and event.user_id == user_id)
            # Ownerless platform emissions (e.g. a company-managed runner's
            # lifecycle): audible to every waiter, mirroring the dispatch
            # side's company-managed arm.
            or event.user_id is None
        ):
            return event
    return None


# ── Delegation completion emissions ─────────────────────────────────

_DELEGATION_TEMPLATE = re.compile(r"\{\{\s*delegation\.([a-z_]+)\s*\}\}")


async def emit_delegation_completion_events(
    db: AsyncSession, task: Any, status: str
) -> int:
    """Fire the completion-mode "On delegate" events stamped on *task*.

    Called by the sub-agent completion path when a delegated background task
    reaches a terminal state. Each spec was frozen at spawn time by the flow
    walker (`_register_completion_emissions`): the mapping renders here with
    ONLY the ``{{delegation.*}}`` pseudo-fields — now including ``result``
    and ``status`` — because the origin run's context no longer exists.
    Failures are logged and swallowed: the delegated work itself already
    reached its terminal state, a signalling error must not undo that.
    """
    specs = list((task.metadata_json or {}).get("flow_completion_emissions") or [])
    if not specs:
        return 0
    delegation = {
        "task": (task.task_description or "")[:500],
        "task_id": str(task.id),
        "target_agent_id": str(task.target_agent_id),
        # A cut result says so and names the call that reads it whole (#1394).
        "result": clipped(
            task.result or task.error or "",
            4000,
            read_call=f'get_full_task_result(task_id="{task.id}")',
        ),
        "status": status,
    }

    def _render(template: Any) -> str:
        if not isinstance(template, str):
            return "" if template is None else str(template)
        return _DELEGATION_TEMPLATE.sub(
            lambda m: str(delegation.get(m.group(1), "")), template
        )

    emitted = 0
    for spec in specs:
        try:
            event_type = await get_event_type(db, spec.get("event_type_id"))
            if event_type is None:
                continue
            mappings = spec.get("mappings") or {}
            rendered = {
                (f.get("name") or "").strip(): _render(
                    mappings.get((f.get("name") or "").strip())
                )
                for f in event_type.fields or []
                if (f.get("name") or "").strip()
                and mappings.get((f.get("name") or "").strip()) is not None
            }
            payload, errors = coerce_event_fields(event_type.fields or [], rendered)
            if errors:
                logger.warning(
                    "Delegation completion event payload mismatch: %s",
                    " ".join(errors),
                )
                continue
            await emit_flow_event(
                db,
                event_type=event_type,
                user_id=uuid.UUID(spec["user_id"]) if spec.get("user_id") else None,
                flow_id=uuid.UUID(spec["flow_id"]) if spec.get("flow_id") else None,
                run_id=uuid.UUID(spec["run_id"]) if spec.get("run_id") else None,
                scope=(spec.get("scope") or "global").strip() or "global",
                payload=payload,
                chain_depth=int(spec.get("chain_depth") or 1),
            )
            emitted += 1
        except Exception as exc:
            logger.warning(
                "Delegation completion event emission failed: %s", exc
            )
    return emitted
