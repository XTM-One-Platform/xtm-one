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

"""Automatic flow triggers — schedule, webhook resolution, integration events.

Three of the four trigger kinds live here (the fourth,
``trigger.manual``, is just ``POST /flows/{id}/run``):

* :func:`process_scheduled_flows` — swept by the same 60-second scheduler tick
  as assignments (``autonomous._run_autonomous_tick_inner``), NOT by the
  worker's ``FlowAdapter.periodic``: the web tick runs under the Redis window
  lock, and firing crons from both processes would double-trigger.
* :func:`resolve_webhook_trigger` — matches an inbound delivery to the
  ``trigger.webhook`` node whose ``path`` it names (route in
  ``api/v1/webhooks.py``).
* :func:`poll_flow_integration_events` — rides the existing per-provider
  pollers in ``event_poller.py`` through a duck-typed shim (they only read
  ``.id`` for Redis cursor keys and ``.trigger_config`` for filters).

Automatic triggers run **as the flow's owner** — every node executor resolves
agents/integrations through that user's visibility.  A company-managed flow
has no owner (``user_id`` NULL), so its automatic triggers are skipped with a
once-per-process warning; it can still be run manually (the caller is the
run user).  Lifting this needs a per-user fan-out decision like assignments
made — deliberately out of Phase 2.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Text, cast, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session
from app.core.egress import egress_client, egress_route
from app.models.flow import FLOW_STATUS_ACTIVE, Flow
from app.models.flow_run import FLOW_RUN_STATUS_SKIPPED, FlowRun
from app.services.assignment_pause import (
    FlowOwnerDeactivatedError,
    owner_not_deactivated,
)
from app.services.flow_catalog import (
    MAX_WEBHOOK_CONCURRENT_RUNS,
    concurrent_runs_setting,
)
from app.services.flow_runner import (
    active_run_count,
    create_flow_run,
    find_trigger_nodes,
    has_active_run,
    matches_event_filter,
)
from app.services.relays import integration_egress_route

if TYPE_CHECKING:
    from app.services.work_item import WorkItemInbox

logger = logging.getLogger(__name__)

# Once-per-process log guards (same spirit as autonomous._should_warn_cron).
_warned_cron: set[str] = set()
_warned_ownerless: set[str] = set()

# How deep a ``flow.call`` chain is followed when answering "does this flow need
# AI". Generous: the walk is over cached graphs, not runs, and a chain deeper
# than this is pathological. Hitting the cap answers "yes" — see the resolver.
_MAX_CALL_DEPTH = 20

# The reason written onto the held-back run. One sentence, because it is read in
# Operations beside the run, not in a log.
_AI_HELD_BACK_REASON = (
    "Held back: this flow contains a step that needs AI, and AI is disabled on "
    "this platform. No step was executed. Enable AI in Settings > Platform "
    "and the flow runs on its next trigger."
)
# Its licence counterpart, completed by the licence refusal's own
# sentence - the remedy it names depends on the platform mode.
_LICENCE_HELD_BACK_PREFIX = (
    "Held back: this flow contains a step that needs AI, and this deployment is "
    "not licensed for AI. No step was executed, and the flow runs on its next "
    "trigger once it is. "
)


def _held_back_reason(refusal: str) -> str:
    from app.services.agentic_gate import AI_DISABLED_CODE

    if refusal == AI_DISABLED_CODE:
        return _AI_HELD_BACK_REASON
    from app.api.deps import licence_required_detail

    return _LICENCE_HELD_BACK_PREFIX + licence_required_detail()


async def flow_requires_ai(db: AsyncSession, flow: Flow) -> bool:
    """Whether triggering *flow* would need a model or embedding provider.

    Computed from the flow's **current** graph at trigger time and never
    stored: a cached verdict is wrong exactly when someone has just fixed the
    flow, and it would keep holding back a graph whose AI step has been
    removed (FR-014).

    Follows ``flow.call`` into the flows it hands off to, because a graph of
    entirely deterministic nodes that calls an AI flow does need AI, and the
    callee is knowable — the node names it in ``config.flow_id``. Without this,
    ``flow.call`` would have to be classified blind: mark it AI and every
    deterministic composition stops, mark it safe and AI leaks through it.

    Fails closed on everything unclear: an unresolvable callee, a cycle beyond
    the depth cap, or a node kind this build does not know.
    """
    from app.services.flow_catalog import graph_ai_dependency

    needs_ai, called = graph_ai_dependency(flow.graph or {})
    if needs_ai:
        return True

    def _parse(raw: object) -> uuid.UUID | None:
        try:
            return uuid.UUID(str(raw))
        except (ValueError, AttributeError, TypeError):
            return None

    # Keyed on the parsed UUID, never the raw string: two config entries naming
    # the same flow in different case are one flow, and comparing string sets
    # would report a valid composition as unresolvable.
    seen: set[uuid.UUID] = {flow.id}
    frontier: set[uuid.UUID] = set()
    for raw in called:
        parsed = _parse(raw)
        if parsed is None:
            logger.warning(
                "Flow %s calls an unparseable flow id %r — treating the flow "
                "as requiring AI",
                flow.id,
                raw,
            )
            return True
        frontier.add(parsed)
    frontier -= seen

    depth = 0
    while frontier:
        if depth >= _MAX_CALL_DEPTH:
            logger.warning(
                "Flow %s has a flow.call chain deeper than %d — treating the "
                "flow as requiring AI rather than walking further",
                flow.id,
                _MAX_CALL_DEPTH,
            )
            return True
        depth += 1
        ids = sorted(frontier, key=str)
        seen |= frontier

        rows = (
            await db.execute(select(Flow.id, Flow.graph).where(Flow.id.in_(ids)))
        ).all()
        if len(rows) != len(ids):
            # A missing callee will fail the run anyway; refusing to clear it
            # keeps this from being the thing that lets AI through.
            logger.warning(
                "Flow %s calls %d flow(s) that do not exist — treating the "
                "flow as requiring AI",
                flow.id,
                len(ids) - len(rows),
            )
            return True

        nxt: set[uuid.UUID] = set()
        for _cid, graph in rows:
            child_needs_ai, child_called = graph_ai_dependency(graph or {})
            if child_needs_ai:
                return True
            for raw in child_called:
                parsed = _parse(raw)
                if parsed is None:
                    logger.warning(
                        "Flow %s reaches an unparseable flow id %r — treating "
                        "the flow as requiring AI",
                        flow.id,
                        raw,
                    )
                    return True
                nxt.add(parsed)
        frontier = nxt - seen

    # Walked the whole reachable set without finding an AI node. Reached here
    # only with an empty frontier, so the chain was fully resolved — the loop
    # used to fall through to True for a chain exactly _MAX_CALL_DEPTH deep.
    return False


async def hold_back_if_ai_required(
    db: AsyncSession,
    flow: Flow,
    *,
    trigger_type: str,
    trigger_context: dict | None = None,
) -> bool:
    """``True`` when the flow needs AI and AI is off: do not dispatch it.

    "AI is off" is the background gate's answer (``agentic_gate``): switched
    off by an administrator, or not licensed - Community Edition. A
    deterministic flow runs either way (FR-014).

    Records rather than drops (FR-016): a run row with status ``skipped`` and
    the reason on it, so the flow's absence is visible in Operations instead of
    looking like a scheduler that stopped working. Deliberately creates **no
    orchestration execution**, which is what guarantees zero steps run — the
    walker never sees this run at all.

    A state that could not be read (``agentic_verdict``'s ``transient``) is not
    a refusal: nothing is recorded - the reason would claim AI is off or not
    licensed on a deployment where it may well be allowed - and ``True`` is
    still returned, since no model may run on a state nobody could read.

    Callers must treat ``True`` as "do not dispatch", and must not consume the
    trigger behind it either: a schedule keeps its window, an integration event
    goes back unseen, a webhook sender is answered 429, a work item stays
    unclaimed - so each is picked up once agentic AI is available again. An
    emitted flow event (``flow_events``) is the exception: it is delivered
    once, so its listener is skipped.
    """
    from app.services.agentic_gate import agentic_verdict, group_verdict

    verdict = await agentic_verdict(db)
    if verdict.refusal is None:
        # For the flow's owner, the person it runs for: a group of theirs may
        # switch AI off (``group_ai_access``).
        verdict = await group_verdict(db, getattr(flow, "user_id", None))
    if verdict.refusal is None:
        return False
    if not await flow_requires_ai(db, flow):
        return False
    if verdict.transient:
        logger.info(
            "Flow %s not triggered (%s): whether agentic AI may run could not "
            "be read, so no skip is recorded",
            flow.id,
            trigger_type,
        )
        return True
    refusal = verdict.refusal
    reason = _held_back_reason(refusal)

    # One record per held-back stretch, not one per tick. The work-item sweep
    # reaches here on every autonomous tick whether or not there is work, and
    # the integration poller once per matching event, so an unconditional
    # insert grew ``flow_runs`` without bound and buried the very signal the
    # skipped status exists to give. A later real run (or a cancel) makes the
    # newest run something other than ``skipped``, so re-disabling AI records
    # again.
    # Also compared by trigger kind. Deduping on "newest run of any kind"
    # meant a scheduled skip swallowed a later *webhook* delivery: the caller
    # got held-back=True with nothing recorded, so the partner's POST vanished
    # with no trace in Operations — the "silently succeeds while doing nothing"
    # case the assignment gate raises to avoid (/code-review, 2026-08-28). A
    # discrete external delivery earns its own record; a tick that repeats does
    # not.
    # Newest run *of this trigger type*, plus "nothing real has happened since".
    #
    # Two failed attempts are worth recording. Deduping on the newest run of any
    # type let a scheduled skip swallow a later webhook delivery, so a partner's
    # POST vanished with no trace. Comparing that newest run's type to this one
    # then leapfrogged: a flow with both a work-item tick and a webhook
    # alternated types, each seeing a mismatch, and inserted a row per tick *and*
    # per delivery — the unbounded growth the debounce exists to prevent
    # (/code-review, 2026-08-28).
    latest_same_kind = (
        await db.execute(
            select(FlowRun.status, FlowRun.result_summary)
            .where(
                FlowRun.flow_id == flow.id,
                FlowRun.trigger_type == trigger_type,
            )
            .order_by(FlowRun.created_at.desc())
            .limit(1)
        )
    ).first()
    real_run_since = (
        await db.execute(
            select(FlowRun.id)
            .where(
                FlowRun.flow_id == flow.id,
                FlowRun.status != FLOW_RUN_STATUS_SKIPPED,
                FlowRun.created_at
                > (
                    select(FlowRun.created_at)
                    .where(
                        FlowRun.flow_id == flow.id,
                        FlowRun.trigger_type == trigger_type,
                        FlowRun.status == FLOW_RUN_STATUS_SKIPPED,
                    )
                    .order_by(FlowRun.created_at.desc())
                    .limit(1)
                    .scalar_subquery()
                ),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        latest_same_kind is not None
        and latest_same_kind.status == FLOW_RUN_STATUS_SKIPPED
        # For the same reason: a stretch recorded as "AI is disabled" that goes
        # on because the licence went (or the reverse) says something else now.
        and latest_same_kind.result_summary == reason
        and real_run_since is None
    ):
        logger.debug(
            "Flow %s still held back (needs AI, agentic AI unavailable: %s) — "
            "already recorded for trigger %s",
            flow.id,
            refusal,
            trigger_type,
        )
        return True

    run = FlowRun(
        flow_id=flow.id,
        flow_version_number=flow.version_number,
        # The graph as it was when held back, same as a real run: the reason
        # only makes sense against the graph it was judged on.
        graph=flow.graph or {},
        user_id=flow.user_id,
        status=FLOW_RUN_STATUS_SKIPPED,
        trigger_type=trigger_type,
        trigger_context=trigger_context,
        result_summary=reason,
        # EVERY sharing tier, mirroring ``create_flow_run``. ``get_visible_run``
        # re-implements the flow's visibility filter over these columns, so a
        # tier missing here is a tier the run journal fails to enforce (see the
        # comment on ``FlowRun``): an ``access_restricted`` flow would
        # produce a skipped run readable by every authenticated user, frozen
        # graph included, and a group-shared flow one its own viewers 404 on.
        is_company_managed=flow.is_company_managed,
        shared_group_ids=list(flow.shared_group_ids or []) or None,
        shared_user_ids=list(flow.shared_user_ids or []) or None,
        access_restricted=bool(flow.access_restricted),
        completed_at=datetime.now(UTC),
    )
    # Written on its own session, never the caller's. ``emit_flow_event`` calls
    # this with the walker's session mid-node-execution, so committing here
    # would publish the walker's in-flight burst early and take away its
    # ability to roll the step back if it later errors.
    async with async_session() as own:
        own.add(run)
        await own.commit()
    logger.info(
        "Flow %s ('%s') held back: needs AI and agentic AI is unavailable "
        "(%s, run %s)",
        flow.id,
        flow.name,
        refusal,
        run.id,
    )
    return True


async def _active_flows_with_trigger(
    db: AsyncSession, trigger_kind: str
) -> list[tuple[Flow, list[dict]]]:
    """Active flows carrying at least one trigger node of *trigger_kind*.

    The graph is JSON, so the trigger scan happens in Python; active flows
    are few (activation is an explicit, validated act), so a full scan of
    ``status='active'`` per tick is cheap.

    A deactivated owner's flow is not selected unless company-managed: no run, no
    skipped record per tick, its work items stay queued and its poll cursor
    stays where it was, so it resumes as it was once the account is back.

    The graph text is narrowed to the ones naming *trigger_kind* first (the
    ``emit_flow_event`` pre-filter): the platform-event dispatch runs this on
    every event raised. A node stores its kind verbatim, so the LIKE has no
    false negatives; ``find_trigger_nodes`` re-checks what it lets through.
    """
    rows = (
        (
            await db.execute(
                select(Flow).where(
                    Flow.status == FLOW_STATUS_ACTIVE,
                    owner_not_deactivated(Flow),
                    cast(Flow.graph, Text).like(f"%{trigger_kind}%"),
                )
            )
        )
        .scalars()
        .all()
    )
    out: list[tuple[Flow, list[dict]]] = []
    for flow in rows:
        nodes = find_trigger_nodes(flow.graph or {}, trigger_kind)
        if nodes:
            out.append((flow, nodes))
    return out


def _skip_ownerless(flow: Flow, kind: str) -> bool:
    if flow.user_id is not None:
        return False
    key = f"{kind}:{flow.id}"
    if key not in _warned_ownerless:
        _warned_ownerless.add(key)
        logger.warning(
            "Flow %s ('%s') is company-managed (no owner) — its %s trigger "
            "is skipped; run it manually or assign an owner.",
            flow.id,
            flow.name,
            kind,
        )
    return True


# ─── Schedule ────────────────────────────────────────────────────────────────


def _cron_due(
    cron_expr: str, timezone: str | None, last: datetime | None, now: datetime
) -> bool:
    """Is the cron due, evaluated in the node's timezone?

    First-ever run fires when the previous occurrence is already in the past
    (the assignment sweep's rule); afterwards the next occurrence computed
    from ``last`` is the single source of truth.
    """
    from croniter import CroniterBadCronError, croniter

    tz = UTC
    if timezone:
        try:
            tz = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            # A bad timezone must not silently shift every fire time to UTC
            # without a trace — raise into the caller's misconfig handler.
            raise CroniterBadCronError(f"unknown timezone {timezone!r}") from None

    now_local = now.astimezone(tz)
    if last is not None:
        next_run = croniter(cron_expr.strip(), last.astimezone(tz)).get_next(
            datetime
        )
        return now_local >= next_run
    prev_run = croniter(cron_expr.strip(), now_local).get_prev(datetime)
    return now_local >= prev_run


async def process_scheduled_flows(db: AsyncSession) -> int:
    """Fire due `trigger.schedule` nodes on active flows.  Returns count."""
    from croniter import CroniterBadCronError

    from app.services import orchestration_engine as engine

    now = datetime.now(UTC)
    count = 0
    for flow, nodes in await _active_flows_with_trigger(db, "trigger.schedule"):
        if _skip_ownerless(flow, "schedule"):
            continue
        try:
            due_node: dict | None = None
            for node in nodes:
                config = node.get("config") or {}
                cron_expr = config.get("cron")
                if not isinstance(cron_expr, str) or not cron_expr.strip():
                    raise CroniterBadCronError("cron expression missing")
                if _cron_due(
                    cron_expr,
                    (config.get("timezone") or "").strip() or None,
                    flow.last_triggered_at,
                    now,
                ):
                    due_node = node
                    break
            if due_node is None:
                continue
            if await has_active_run(db, flow.id):
                logger.debug(
                    "Flow %s cron due but a run is still active — skipped",
                    flow.id,
                )
                continue
            if await hold_back_if_ai_required(
                db,
                flow,
                trigger_type="schedule",
                trigger_context={
                    "type": "schedule",
                    "trigger_node_id": due_node.get("id"),
                },
            ):
                # Not stamped: the window stays open, so the flow runs once
                # agentic AI is available again - one catch-up run, not one
                # per missed occurrence, as a scheduled assignment catches up.
                # The due cron asks again every tick, but the skip is recorded
                # once per held-back stretch.
                continue
            # Stamp BEFORE firing (assignment sweep order): a crash between
            # the stamp and the run creation loses one occurrence, the
            # opposite order would double-fire — losing one is the safe side.
            previous_triggered_at = flow.last_triggered_at
            flow.last_triggered_at = now
            try:
                await create_flow_run(
                    db,
                    flow,
                    trigger_type="schedule",
                    trigger_payload={"fired_at": now.isoformat()},
                    trigger_context={
                        "type": "schedule",
                        "trigger_node_id": due_node.get("id"),
                        "cron": (due_node.get("config") or {}).get("cron"),
                    },
                    user_id=flow.user_id,
                )
            except FlowOwnerDeactivatedError:
                # Owner deactivated since the query picked it: the
                # window stays open, as for a flow the query had skipped.
                flow.last_triggered_at = previous_triggered_at
                continue
            await db.commit()
            await engine.notify_orchestration_wake()
            count += 1
        except CroniterBadCronError as cron_err:
            await db.rollback()
            key = str(flow.id)
            if key not in _warned_cron:
                _warned_cron.add(key)
                logger.error(
                    "Flow %s ('%s') has an invalid schedule (%s) — skipping "
                    "every tick until it is fixed",
                    flow.id,
                    flow.name,
                    cron_err,
                )
        except Exception:
            await db.rollback()
            logger.exception("Scheduled flow %s failed to trigger", flow.id)
    return count


# ─── Webhook ─────────────────────────────────────────────────────────────────


def resolve_webhook_trigger(graph: dict, path: str) -> dict | None:
    """The `trigger.webhook` node whose configured path matches *path*."""
    normalized = path.strip().strip("/")
    for node in find_trigger_nodes(graph or {}, "trigger.webhook"):
        node_path = str((node.get("config") or {}).get("path") or "").strip().strip("/")
        if node_path and node_path == normalized:
            return node
    return None


# Namespace of the per-flow run-admission advisory lock (the first key of
# ``pg_advisory_xact_lock(int4, int4)``), so the flow hash can't collide with
# advisory locks taken elsewhere (the assignment trigger guard, the sub-agent
# fan-out cap, the platform registration lock).
_FLOW_RUN_ADMISSION_LOCK_NS = 0x46524144  # "FRAD" - Flow Run ADmission


def webhook_concurrency_limit(node: dict) -> int:
    """How many runs of its flow a Webhook trigger node lets be active at once.

    ``max_concurrent_runs`` from the node's config, clamped to
    ``[1, MAX_WEBHOOK_CONCURRENT_RUNS]``. Anything unreadable - absent, not a
    whole number, a boolean - is 1, one delivery at a time: the safe default,
    never more concurrency than the author asked for.
    """
    value = concurrent_runs_setting((node.get("config") or {}).get("max_concurrent_runs"))
    if value is None:
        return 1
    return min(max(value, 1), MAX_WEBHOOK_CONCURRENT_RUNS)


async def _lock_flow_run_admission(db: AsyncSession, flow_id: uuid.UUID) -> None:
    """Serialize the run admission of one flow's webhook deliveries.

    Counting the active runs and creating one is not atomic on its own: two
    deliveries landing together would both count below the limit and both
    start a run. A transaction-scoped advisory lock (the assignment trigger
    guard's pattern) makes the second wait until the first committed its run,
    then count it. Released by the caller's commit (a run created) or by the
    session's rollback/close (a delivery refused).
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(CAST(:ns AS integer), hashtext(:scope))"),
        {"ns": _FLOW_RUN_ADMISSION_LOCK_NS, "scope": f"flow_run_admission:{flow_id}"},
    )


async def webhook_run_admitted(db: AsyncSession, flow_id: uuid.UUID, limit: int) -> bool:
    """Whether a webhook delivery may start a run: fewer than *limit* are active.

    Takes the flow's admission lock first, so concurrent deliveries are
    counted one after the other and never exceed *limit* together. Every
    active run of the flow counts, whatever trigger started it.
    """
    await _lock_flow_run_admission(db, flow_id)
    return await active_run_count(db, flow_id) < limit


#: Why a webhook delivery started no run (``WebhookTriggerOutcome.skipped``).
WEBHOOK_SKIP_OWNERLESS = "ownerless"
WEBHOOK_SKIP_ACTIVE_RUN = "active_run"
WEBHOOK_SKIP_AI_UNAVAILABLE = "ai_unavailable"


@dataclass(frozen=True, slots=True)
class WebhookTriggerOutcome:
    """A webhook delivery's outcome: the run started, or why none was."""

    run_id: str | None = None
    skipped: str | None = None


async def trigger_flow_from_webhook(
    db: AsyncSession,
    flow: Flow,
    node: dict,
    *,
    payload: dict,
    headers: dict[str, str],
) -> str | None:
    """Create the run for an authenticated webhook delivery.

    Returns the run id, or ``None`` when skipped; see
    :func:`trigger_flow_from_webhook_outcome` for why.
    """
    outcome = await trigger_flow_from_webhook_outcome(
        db, flow, node, payload=payload, headers=headers
    )
    return outcome.run_id


async def trigger_flow_from_webhook_outcome(
    db: AsyncSession,
    flow: Flow,
    node: dict,
    *,
    payload: dict,
    headers: dict[str, str],
) -> WebhookTriggerOutcome:
    """:func:`trigger_flow_from_webhook`, saying why no run was started.

    Skipped for an ownerless flow, a flow that needs agentic AI while it is
    unavailable (held back), or a flow already running as many runs as
    the trigger's ``max_concurrent_runs`` allows (1 by default: the same
    overlap guard as cron; a run parked on a human answer counts). The route answers every skip with a 429 so the sender
    retries, worded after the reason.
    """
    from app.services import orchestration_engine as engine

    if _skip_ownerless(flow, "webhook"):
        return WebhookTriggerOutcome(skipped=WEBHOOK_SKIP_OWNERLESS)
    # The AI gate first: it reads the AI switch and the licence, which must not
    # run inside the admission lock every concurrent delivery waits on.
    if await hold_back_if_ai_required(
        db,
        flow,
        trigger_type="webhook",
        trigger_context={
            "type": "webhook",
            "trigger_node_id": node.get("id"),
            "path": (node.get("config") or {}).get("path"),
        },
    ):
        return WebhookTriggerOutcome(skipped=WEBHOOK_SKIP_AI_UNAVAILABLE)
    if not await webhook_run_admitted(db, flow.id, webhook_concurrency_limit(node)):
        # Ends the transaction, and with it the admission lock, here rather
        # than whenever the caller's session closes (the assignment trigger
        # guard does the same on its skip path).
        await db.commit()
        return WebhookTriggerOutcome(skipped=WEBHOOK_SKIP_ACTIVE_RUN)
    run = await create_flow_run(
        db,
        flow,
        trigger_type="webhook",
        trigger_payload={"payload": payload, "headers": headers},
        trigger_context={
            "type": "webhook",
            "trigger_node_id": node.get("id"),
            "path": (node.get("config") or {}).get("path"),
        },
        user_id=flow.user_id,
    )
    await db.commit()
    await engine.notify_orchestration_wake()
    return WebhookTriggerOutcome(run_id=str(run.id))


# ─── Integration events ──────────────────────────────────────────────────────


@dataclass
class _FlowPollTarget:
    """Duck-typed stand-in for the Assignment the event pollers expect.

    ``event_poller._poll_provider`` and friends read exactly two attributes:
    ``.id`` (Redis cursor/seen keys — namespaced here so a flow and an
    assignment watching the same mailbox keep independent cursors) and
    ``.trigger_config`` (channel filters).
    """

    id: str
    trigger_config: dict


async def poll_flow_integration_events(db: AsyncSession) -> int:
    """Poll providers for `trigger.integration_event` nodes on active flows.

    Mirrors ``event_poller.poll_integration_events`` shape: per-target poll
    interval and seen-set in Redis, OAuth ownership gate, the owner's
    visibility of the integration. One run at a time per flow: an
    event polled while a run is active (or held back because agentic AI is
    unavailable) is given back unconsumed and fires on a later poll, once;
    the seen set keeps a dispatched event from firing twice.
    """

    from app.core.redis import redis_client
    from app.core.security import decrypt_value
    from app.models.integration import Integration
    from app.services import orchestration_engine as engine
    from app.services.event_poller import (
        POLL_INTERVALS,
        _poll_provider,
        give_back_polled_event,
    )
    from app.services.integration_user_credentials import (
        connected_for_clause,
        reader_clause,
        resolve_token_holder,
    )
    from app.services.trigger_integration_access import (
        TriggerIntegrationVisibility,
        log_unseen_trigger_integration,
    )

    targets = await _active_flows_with_trigger(db, "trigger.integration_event")
    if not targets:
        return 0
    redis = await redis_client()
    triggered = 0
    visibility = TriggerIntegrationVisibility()

    async with egress_client() as http:
        for flow, nodes in targets:
            if _skip_ownerless(flow, "integration_event"):
                continue
            for node in nodes:
                config = node.get("config") or {}
                integration_id = config.get("integration_id")
                event_type = str(config.get("event_type") or "").strip()
                if not integration_id or not event_type:
                    continue
                target_key = f"flow:{flow.id}:{node.get('id')}"
                try:
                    poll_key = f"event_poll:{target_key}"
                    last_poll = await redis.get(poll_key)
                    interval = POLL_INTERVALS.get(event_type, 300)
                    if last_poll:
                        elapsed = (
                            datetime.now(UTC).timestamp() - float(last_poll)
                        )
                        if elapsed < interval:
                            continue

                    # Same OAuth-share gate as the assignment poller: the
                    # poll acts as the flow owner.
                    integration = (
                        await db.execute(
                            select(Integration).where(
                                Integration.id == integration_id,
                                connected_for_clause(flow.user_id),
                                reader_clause(flow.user_id),
                            )
                        )
                    ).scalar_one_or_none()
                    # The owner's own account on a per_user integration.
                    holder = (
                        await resolve_token_holder(db, integration, flow.user_id)
                        if integration is not None
                        else None
                    )
                    if holder is None or not holder.access_token_enc:
                        continue
                    # The run acts as the flow owner: delivered even when they
                    # cannot see the integration the events come from, and
                    # said so.
                    if not await visibility.sees(db, flow.user_id, integration.id):
                        log_unseen_trigger_integration(
                            "flow",
                            f"{flow.id} (node {node.get('id')})",
                            integration_id=integration.id,
                            acting_user_id=flow.user_id,
                        )
                    access_token = decrypt_value(holder.access_token_enc)
                    # The poll follows the integration's relay route.
                    route = await integration_egress_route(db, integration)

                    from app.core.database import release_db_connection_before_loop

                    await release_db_connection_before_loop(db)

                    shim = _FlowPollTarget(
                        id=target_key,
                        trigger_config={"filter": config.get("filter") or {}},
                    )
                    seen_set_key = f"event_seen_ids:{target_key}"
                    # Every provider poller advances this cursor before
                    # returning; kept so a given-back event can put it back.
                    cursor_key = f"event_cursor:{target_key}"
                    cursor_before = await redis.get(cursor_key)
                    with egress_route(route):
                        events = await _poll_provider(
                            integration.provider,
                            event_type,
                            access_token,
                            shim,  # type: ignore[arg-type] — duck-typed on purpose
                            redis,
                            seen_set_key,
                            http,
                        )
                    await redis.set(
                        poll_key, str(datetime.now(UTC).timestamp()), ex=86400
                    )

                    predicate = config.get("filter") or {}
                    owner_paused = False
                    for event_payload in events:
                        msg_id = event_payload.get("message_id", "")
                        if msg_id:
                            added = await redis.sadd(seen_set_key, msg_id)
                            if not added:
                                continue
                            await redis.expire(seen_set_key, 86400 * 7)
                        if not matches_event_filter(event_payload, predicate):
                            continue
                        if await has_active_run(db, flow.id):
                            # One run at a time: this event and the ones after
                            # it wait for the active run to end, unconsumed.
                            await give_back_polled_event(
                                redis,
                                seen_set_key=seen_set_key,
                                msg_id=msg_id,
                                cursor_key=cursor_key,
                                cursor_before=cursor_before,
                            )
                            break
                        if await hold_back_if_ai_required(
                            db,
                            flow,
                            trigger_type="integration_event",
                            trigger_context={
                                "type": "integration_event",
                                "trigger_node_id": node.get("id"),
                                "provider": integration.provider,
                            },
                        ):
                            # Held back: nothing is consumed, and it
                            # runs once agentic AI is available.
                            await give_back_polled_event(
                                redis,
                                seen_set_key=seen_set_key,
                                msg_id=msg_id,
                                cursor_key=cursor_key,
                                cursor_before=cursor_before,
                            )
                            break
                        try:
                            await create_flow_run(
                                db,
                                flow,
                                trigger_type="integration_event",
                                trigger_payload={
                                    "payload": event_payload,
                                    "event_type": event_type,
                                },
                                trigger_context={
                                    "type": "integration_event",
                                    "trigger_node_id": node.get("id"),
                                    "provider": integration.provider,
                                },
                                user_id=flow.user_id,
                            )
                        except FlowOwnerDeactivatedError:
                            # Owner deactivated since the query picked it:
                            # give the event, the cursor and the poll
                            # stamp back, so this poll is fetched again once
                            # the owner is back; the events already dispatched
                            # stay seen and are not dispatched twice.
                            await give_back_polled_event(
                                redis,
                                seen_set_key=seen_set_key,
                                msg_id=msg_id,
                                cursor_key=cursor_key,
                                cursor_before=cursor_before,
                            )
                            if last_poll is None:
                                await redis.delete(poll_key)
                            else:
                                await redis.set(poll_key, last_poll, ex=86400)
                            owner_paused = True
                            break
                        await db.commit()
                        await engine.notify_orchestration_wake()
                        triggered += 1
                    if owner_paused:
                        break
                except Exception:
                    await db.rollback()
                    logger.exception(
                        "Integration-event poll failed for flow %s node %s",
                        flow.id,
                        node.get("id"),
                    )
    return triggered


# ─── Work items ──────────────────────────────────────────────────────────────


def flow_work_item_inbox(flow: Flow, node: dict) -> WorkItemInbox | None:
    """The inbox a ``trigger.work_item`` *node* of an owned *flow* is, or ``None``.

    ``None`` without an owner agent to drain (the validator warns). The flow
    sees its owner's items and takes the item over whichever agent it names
    as the owner: a personal flow never claims a company-managed item.
    """
    from app.services.company_managed_writes import flow_writer
    from app.services.work_item import WorkItemInbox

    config = node.get("config") or {}
    owner_agent_id = (config.get("owner_agent_id") or "").strip()
    if not owner_agent_id or flow.user_id is None:
        return None
    return WorkItemInbox(
        kind="flow",
        id=flow.id,
        name=flow.name,
        owner_agent_id=owner_agent_id,
        user_id=str(flow.user_id),
        writer=flow_writer(is_company_managed=flow.is_company_managed),
        correlation_prefix=(config.get("correlation_prefix") or "").strip() or None,
        priority_min=(config.get("priority_min") or "").strip() or None,
        source=(flow, node),
    )


async def load_flow_work_item_inboxes(db: AsyncSession) -> list[WorkItemInbox]:
    """Every flow inbox :func:`process_work_item_flows` dispatches to.

    Active flows of an active owner (an ownerless, company-managed flow skips
    its automatic triggers), one inbox per ``trigger.work_item`` node naming
    an owner agent.
    """
    inboxes: list[WorkItemInbox] = []
    for flow, nodes in await _active_flows_with_trigger(db, "trigger.work_item"):
        for node in nodes:
            inbox = flow_work_item_inbox(flow, node)
            if inbox is not None:
                inboxes.append(inbox)
    return inboxes


async def process_work_item_flows(db: AsyncSession) -> int:
    """Fire `trigger.work_item` nodes on active flows.  Returns run count.

    The flow-side twin of the assignment inbox consumer
    (``autonomous._process_work_item_assignments``): per trigger node, list
    the owner agent's dispatchable backlog (open, due, prefix/priority
    filtered), atomically claim the first item won, and start ONE run per
    node per tick with the item as the trigger payload.  The claim is the
    concurrency gate — two ticks (or a flow and an assignment inbox watching
    the same queue) can never double-run one item.  The run's end settles the
    item: completed → ``done`` with the summary, failed → claim released
    (``FlowWalker._settle_trigger_work_item``).
    """
    from app.services import orchestration_engine as engine
    from app.services.work_item import claim_work_item, list_dispatchable_work_items

    count = 0
    for flow, nodes in await _active_flows_with_trigger(db, "trigger.work_item"):
        if _skip_ownerless(flow, "work_item"):
            continue
        for node in nodes:
            inbox = flow_work_item_inbox(flow, node)
            if inbox is None:
                continue  # validator warns; nothing to drain without an owner
            owner_agent_id = inbox.owner_agent_id
            writer = inbox.writer
            try:
                # Before the claim, not after: claiming an item this flow
                # cannot process would take it off every other worker's queue
                # and leave it stuck.
                if await hold_back_if_ai_required(
                    db,
                    flow,
                    trigger_type="work_item",
                    trigger_context={
                        "type": "work_item",
                        "trigger_node_id": node.get("id"),
                    },
                ):
                    continue
                candidates = await list_dispatchable_work_items(
                    owner_agent_id=owner_agent_id,
                    user_id=inbox.user_id,
                    correlation_prefix=inbox.correlation_prefix,
                    priority_min=inbox.priority_min,
                    writer=writer,
                )
                claimed: dict | None = None
                for candidate in candidates:
                    outcome = await claim_work_item(
                        str(candidate.get("id")),
                        agent_id=owner_agent_id,
                        user_id=str(flow.user_id),
                        writer=writer,
                    )
                    if outcome.get("claimed"):
                        claimed = outcome.get("work_item") or candidate
                        break
                if claimed is None:
                    continue
                try:
                    await create_flow_run(
                        db,
                        flow,
                        trigger_type="work_item",
                        # Shaped as the trigger's DECLARED outputs, so
                        # `{{t.title}}` / `{{t.payload.priority}}` resolve.
                        trigger_payload={
                            "payload": claimed,
                            "work_item_id": str(claimed.get("id")),
                            "title": claimed.get("title") or "",
                            "description": claimed.get("description") or "",
                        },
                        trigger_context={
                            "type": "work_item",
                            "trigger_node_id": node.get("id"),
                            "work_item_id": str(claimed.get("id")),
                            "correlation_key": claimed.get("correlation_key"),
                        },
                        user_id=flow.user_id,
                    )
                except FlowOwnerDeactivatedError:
                    # Owner deactivated since the query picked it:
                    # the item goes back to the queue, no attempt counted.
                    from app.services.work_item import release_work_item_claim

                    await release_work_item_claim(
                        work_item_id=str(claimed.get("id")),
                        user_id=str(flow.user_id),
                        writer=writer,
                    )
                    break
                await db.commit()
                await engine.notify_orchestration_wake()
                count += 1
            except Exception:
                await db.rollback()
                logger.exception(
                    "Work-item trigger failed for flow %s node %s",
                    flow.id,
                    node.get("id"),
                )
    return count


# ─── Platform events ─────────────────────────────────────────────────────────

#: The ``trigger_type`` of a run a platform event started.
PLATFORM_EVENT_TRIGGER_TYPE = "platform_event"


def platform_event_in_flow_scope(
    *,
    owner_id: uuid.UUID,
    agent_ids: set[str],
    payload: dict,
    scope_all: bool,
) -> bool:
    """Whether a flow of *owner_id* running *agent_ids* may hear an event.

    The assignment rule (``platform_events.in_scope``) with the flow in the
    assignment's place: a flow has no agent of its own, so "its" agents are
    the agent nodes on its canvas that its owner may manage (the caller
    narrows them, ``_manageable_agent_ids``). A personal object (a video job, a personal
    work item) reaches only its owner's flows, which hear it without an agent
    of theirs taking part; any other object reaches a flow one of whose agents
    takes part in it. ``scope_all`` is the administrator's every-object scope.
    """
    from app.services.platform_events import is_platform_wide, is_viewer_checked, personal_owners

    # A viewer-checked event (a CTEM case) is then decided by the object's own view (#933).
    if scope_all or is_platform_wide(payload) or is_viewer_checked(payload):
        return True
    owners = personal_owners(payload)
    if owners:
        return owners == {str(owner_id)}
    return bool(agent_ids & {str(a) for a in payload.get("participant_agent_ids") or ()})


def _graph_config_ids(graph: dict, kind: str, field: str) -> set[str]:
    """The *field* values of the *kind* nodes of *graph*."""
    return {
        str((node.get("config") or {}).get(field))
        for node in graph.get("nodes") or []
        if isinstance(node, dict)
        and node.get("kind") == kind
        and (node.get("config") or {}).get(field)
    }


async def _owner_role(db: AsyncSession, owner_id: uuid.UUID) -> str:
    """A flow owner's role ("" when the account is gone)."""
    from app.models.user import User

    return (await db.execute(select(User.role).where(User.id == owner_id))).scalar_one_or_none() or ""


async def _manageable_agent_ids(
    db: AsyncSession, owner_id: uuid.UUID, is_admin: bool, agent_ids: set[str]
) -> set[str]:
    """The agents of *agent_ids* the flow's owner may manage, read from their rows.

    An agent node takes part in a flow's platform-event scope only for an
    owner who could give that agent an assignment (``create_assignment``'s
    rule, ``helpers.can_manage``): placing an agent on a canvas needs only to
    SEE it, and hearing its events - a background task's result, someone
    else's chat behind it - is reserved to whoever manages it. Read on every
    delivery, so a share revoked or narrowed since the save applies at once.
    """
    from app.api.v1.helpers import can_manage_as, get_user_group_ids
    from app.core.utils import safe_uuid
    from app.models.agent import Agent

    ids = [uid for uid in (safe_uuid(a) for a in agent_ids) if uid is not None]
    if not ids:
        return set()
    groups = await get_user_group_ids(db, owner_id)
    agents = (await db.execute(select(Agent).where(Agent.id.in_(ids)))).scalars().all()
    return {str(a.id) for a in agents if can_manage_as(owner_id, is_admin, a, groups)}


async def _platform_event_seen(
    db: AsyncSession, flow_id: uuid.UUID, node_id: str, event_id: str
) -> bool:
    """Whether this trigger node already started a run for this event."""
    from datetime import timedelta

    from app.services.platform_events import DEDUP_WINDOW_SECONDS

    found = (
        await db.execute(
            select(FlowRun.id)
            .where(
                FlowRun.flow_id == flow_id,
                FlowRun.trigger_type == PLATFORM_EVENT_TRIGGER_TYPE,
                FlowRun.created_at >= datetime.now(UTC) - timedelta(seconds=DEDUP_WINDOW_SECONDS),
                FlowRun.trigger_context["event_id"].as_string() == event_id,
                FlowRun.trigger_context["trigger_node_id"].as_string() == node_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return found is not None


def _flow_skips_platform_event(flow: Flow, name: str, payload: Mapping[str, Any]) -> bool:
    """Whether *flow* never hears event *name*: ownerless, or the event is about its own writes."""
    if _skip_ownerless(flow, "platform_event"):
        return True
    if payload.get("actor_flow_id") == str(flow.id):
        return True
    return name.startswith("run.") and str(payload.get("assignment_id")) in _graph_config_ids(
        flow.graph or {}, "action.trigger_assignment", "assignment_id"
    )


async def _flow_node_takes(
    db: AsyncSession,
    flow: Flow,
    node: dict,
    name: str,
    payload: Mapping[str, Any],
    reads: dict[str, Any],
) -> bool:
    """Whether trigger *node* of *flow* takes event *name*: events, filter, scope and workspaces.

    *reads* caches the owner's role, manageable agents and readable workspaces
    across the nodes of one flow (pass a fresh dict per flow).
    """
    from app.services.flow_catalog import PLATFORM_EVENT_SCOPE_ALL
    from app.services.integration_event_filters import payload_filter_reason
    from app.services.platform_events import is_viewer_checked, personal_owners, touched_workspaces, user_reads_object
    from app.services.workspaces import workspace_scope_tiers

    config = node.get("config") or {}
    events = config.get("events")
    events = [events] if isinstance(events, str) else events
    if not isinstance(events, list) or name not in events:
        return False
    flt = config.get("filter") if isinstance(config.get("filter"), dict) else {}
    if payload_filter_reason(payload, flt) is not None:
        return False

    async def _role() -> str:
        if "role" not in reads:
            reads["role"] = await _owner_role(db, flow.user_id)
        return reads["role"]

    scope_all = config.get("scope") == PLATFORM_EVENT_SCOPE_ALL and await _role() == "admin"
    if "agent_ids" not in reads:
        # Only the agents the event names are worth a read.
        participants = {str(a) for a in payload.get("participant_agent_ids") or ()}
        named = _graph_config_ids(flow.graph or {}, "agent", "agent_id") & participants
        if named and not personal_owners(payload):
            reads["agent_ids"] = await _manageable_agent_ids(db, flow.user_id, await _role() == "admin", named)
        else:
            reads["agent_ids"] = set()
    if not platform_event_in_flow_scope(
        owner_id=flow.user_id, agent_ids=reads["agent_ids"], payload=dict(payload), scope_all=scope_all
    ):
        return False
    wanted = touched_workspaces(payload)
    if wanted and not scope_all:
        if "readable" not in reads:
            ids, _writable = await workspace_scope_tiers(db, str(flow.user_id))
            reads["readable"] = {str(w) for w in ids}
        if not wanted <= reads["readable"]:
            return False
    # A flow runs as its owner: a CTEM case reaches it only when the owner sees
    # the case, unless the owner is an administrator with scope all (#933).
    if is_viewer_checked(payload) and not scope_all:
        if "object" not in reads:
            reads["object"] = await user_reads_object(str(flow.user_id), payload)
        if not reads["object"]:
            return False
    return True


async def flows_taking_platform_event(name: str, payload: Mapping[str, Any]) -> list[str]:
    """The ids of the active flows a trigger node of which takes event *name*, as the dispatch tests it.

    Read-only: no run, no dedupe read. For a publisher that must know whether
    anything hears an event before it raises it (``platform_timers``).
    """
    from app.services.flow_catalog import PLATFORM_EVENT_TRIGGER_KIND

    taking: list[str] = []
    async with async_session() as db:
        for flow, nodes in await _active_flows_with_trigger(db, PLATFORM_EVENT_TRIGGER_KIND):
            if _flow_skips_platform_event(flow, name, payload):
                continue
            reads: dict[str, Any] = {}
            for node in nodes:
                if await _flow_node_takes(db, flow, node, name, payload, reads):
                    taking.append(str(flow.id))
                    break
    return taking


async def dispatch_flow_platform_event(name: str, payload: dict) -> list[str]:
    """Start the active flows whose ``trigger.platform_event`` takes event *name*.

    One run per (trigger node, event id): a publisher reporting the same end
    twice starts one run. Every path the other automatic triggers take
    applies: the flow selection leaves deactivated owners out, an ownerless
    flow is skipped, a flow needing unavailable AI is held back, and the
    owner must read every workspace the event touches. A flow never starts on
    its own writes: a ``run.*`` event about an assignment the flow triggers (the
    assignments' own self-trigger guard), or an event its own nodes raised
    (``actor_flow_id``, a work item a node noted or completed). Never raises;
    one outcome string per run started or refused, for the dispatcher's log.
    """
    from app.services import orchestration_engine as engine
    from app.services.flow_catalog import PLATFORM_EVENT_TRIGGER_KIND

    outcomes: list[str] = []
    event_id = str(payload.get("event_id") or "")
    if not event_id:
        return outcomes
    try:
        async with async_session() as db:
            targets = await _active_flows_with_trigger(db, PLATFORM_EVENT_TRIGGER_KIND)
            for flow, nodes in targets:
                if _flow_skips_platform_event(flow, name, payload):
                    continue
                # Read once per flow, and only when a node needs them.
                reads: dict[str, Any] = {}
                for node in nodes:
                    if not await _flow_node_takes(db, flow, node, name, payload, reads):
                        continue
                    node_id = str(node.get("id") or "")
                    context = {
                        "type": PLATFORM_EVENT_TRIGGER_TYPE,
                        "trigger_node_id": node_id,
                        "event": name,
                        "event_id": event_id,
                        "object_type": payload.get("object_type"),
                        "object_id": payload.get("object_id"),
                    }
                    # Serialised per flow: two deliveries of one event read the
                    # dedupe one after the other. Every outcome commits, which
                    # releases the lock at once; never a rollback, which would
                    # expire the flows still to be read.
                    await _lock_flow_run_admission(db, flow.id)
                    if await _platform_event_seen(db, flow.id, node_id, event_id):
                        await db.commit()
                        continue
                    if await hold_back_if_ai_required(
                        db, flow, trigger_type=PLATFORM_EVENT_TRIGGER_TYPE, trigger_context=context
                    ):
                        # Recorded in its own session; this one wrote nothing.
                        await db.commit()
                        outcomes.append(f"flow {flow.id} held back")
                        continue
                    try:
                        run = await create_flow_run(
                            db,
                            flow,
                            trigger_type=PLATFORM_EVENT_TRIGGER_TYPE,
                            trigger_payload={"payload": dict(payload), "event": name, "event_id": event_id},
                            trigger_context=context,
                            user_id=flow.user_id,
                        )
                    except FlowOwnerDeactivatedError:
                        # Deactivated since the selection: nothing was added.
                        await db.commit()
                        continue
                    await db.commit()
                    await engine.notify_orchestration_wake()
                    outcomes.append(f"flow {flow.id} run {run.id}")
    except Exception:
        logger.exception("Platform event %s not delivered to flows", name)
    return outcomes
