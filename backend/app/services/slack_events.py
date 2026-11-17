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

"""Scoped dispatch of real-time Slack message events to ``new_slack_message`` assignments.

``POST /api/v1/integrations/notifications/slack`` authenticates a delivery first
(``integration_notification_auth.check_slack_delivery``): the result is the set of
Slack integrations whose app signed it. This module decides which assignments the
message may reach and makes sure it runs each of them once:

1. **Workspace.** An integration receives the event only when it belongs to the
   installation Slack delivered it for: its workspace (``auth.test`` on its own
   token, cached per token) is one of the event's ``authorizations[].team_id`` - or
   the envelope ``team_id`` when Slack sent no authorizations - or its Enterprise Grid
   org when the authorization is an org-wide install.
2. **Conversation.** A public channel message is readable by every member of the
   workspace. Anything else (private channel, direct or group message) reaches only
   the integration of the message's author or of the user Slack authorized the event
   for: nobody else is known to be able to read that conversation.
3. **Assignment.** Only an enabled ``new_slack_message`` assignment whose enabled
   agent is connected to one of those integrations - a fixed ``slack`` binding (or
   the assignment's pinned ``integration_id``), or a ``slack`` template binding the
   run user's own or a company-wide integration resolves - under the event
   poller's rules: for a fixed binding, an OAuth token is the agent owner's or
   shared with acknowledgement (``oauth_credentials_usable_clause``); a run
   whose acting user cannot see the integration still receives the message, and
   it is logged. The channel filter is read as the poller reads it.
4. **Once.** The message is claimed in the poller's own per-assignment seen set
   (``event_seen:<assignment>``, member ``<channel>:<ts>``, see
   ``SEEN_SET_TTL_SECONDS`` for its window) before the run is
   created, so the poller - and a Slack retry - skip it. The claim is released when
   no run is created (AI unavailable, an active run, an error), so the poller can
   still deliver it. A Redis failure fails open: a message is never lost because
   deduplication was unavailable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session, release_db_connection_before_loop
from app.core.redis import redis_client
from app.core.security import decrypt_value
from app.core.utils import format_exception_message
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.integration import Integration
from app.services.agent_posts import is_agent_slack_post
from app.services.agentic_gate import refusal_code
from app.services.assignment_dispatch import CONVERSATION_KEY_FIELD
from app.services.assignment_engine import queue_busy_integration_event, trigger_assignment
from app.services.assignment_pause import (
    AssignmentOwnerDeactivatedError,
    owner_not_deactivated,
)
from app.services.integration_credentials import (
    agent_acting_user_id,
    oauth_credentials_usable_clause,
)
from app.services.slack_conversations import conversation_key as slack_conversation_key
from app.services.trigger_integration_access import (
    TriggerIntegrationVisibility,
    assignment_acting_user_id,
    log_unseen_trigger_integration,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The seen set shared with the event poller
# ---------------------------------------------------------------------------

SEEN_SET_TTL_SECONDS = 86400 * 7
"""How long a claim holds: a message claimed within this window is never run twice.

Past Slack's retries (minutes) and the poller's 5-minute cadence by orders of
magnitude. The poller never re-reads a message at or before its cursor (kept 30
days after the newest message), so a message only reaches it again once that
cursor is gone - which the unbounded set did not survive either: it expired 7 days
after its last addition.
"""


def seen_set_key(assignment_id: object) -> str:
    """The sorted set (member -> claim time) of the messages an assignment was triggered for."""
    return f"event_seen:{assignment_id}"


def legacy_seen_set_key(assignment_id: object) -> str:
    """The plain set written before claims were bounded; read until it expires."""
    return f"event_seen_ids:{assignment_id}"


async def claim_seen(redis, assignment_id: object, member: str, *, now: float | None = None) -> bool:
    """Claim *member* for an assignment: ``True`` on the first claim inside the window.

    Claims older than the window are trimmed on every claim, so the set is bounded
    by what an assignment receives in ``SEEN_SET_TTL_SECONDS``. Redis errors on the
    claim itself propagate; a failed TTL refresh is only logged, since the member
    is recorded by then.
    """
    key = seen_set_key(assignment_id)
    if await redis.sismember(legacy_seen_set_key(assignment_id), member):
        return False
    now = time.time() if now is None else now
    await redis.zremrangebyscore(key, "-inf", now - SEEN_SET_TTL_SECONDS)
    added = await redis.zadd(key, {member: now}, nx=True)
    try:
        await redis.expire(key, SEEN_SET_TTL_SECONDS)
    except Exception as exc:
        logger.warning(
            "Could not refresh the event dedup TTL of assignment %s: %s",
            assignment_id,
            format_exception_message(exc),
        )
    return bool(added)


async def release_seen(redis, assignment_id: object, member: str) -> None:
    """Give a claimed message back (no run was created for it)."""
    await redis.zrem(seen_set_key(assignment_id), member)


def slack_message_key(channel_id: str, ts: str) -> str:
    """How a Slack message is named in the seen set, by the poller and the events endpoint."""
    return f"{channel_id}:{ts}"


def slack_message_skipped(message: dict) -> bool:
    """Whether a Slack message is not a new message to trigger on.

    The one rule of the poller and of the events endpoint: any ``subtype`` (a bot
    message, an edit, a join, ...) is skipped. A message the integration's own user
    wrote has no subtype and is NOT skipped, on either path.
    """
    return bool(message.get("subtype"))


def assignment_slack_channels(trigger_config: dict | None) -> list[str]:
    """The channels a ``new_slack_message`` assignment watches (empty: all)."""
    filter_config = (trigger_config or {}).get("filter") or {}
    if not isinstance(filter_config, dict):
        return []
    scope = filter_config.get("scope") or {}
    channels = (scope.get("slack_channel") if isinstance(scope, dict) else None) or filter_config.get(
        "channels"
    ) or []
    if isinstance(channels, str):
        channels = [channels]
    return [c for c in channels if isinstance(c, str) and c]


# ---------------------------------------------------------------------------
# Who an integration is in Slack
# ---------------------------------------------------------------------------

SLACK_AUTH_TEST_URL = "https://slack.com/api/auth.test"
_IDENTITY_TTL_SECONDS = 86400 * 7
_IDENTITY_MISS_TTL_SECONDS = 600
_IDENTITY_LOOKUP_TIMEOUT_SECONDS = 5.0
_IDENTITY_LOOKUP_CONCURRENCY = 8
_MISS = "-"


@dataclass(frozen=True)
class SlackIdentity:
    """The Slack workspace, org and user an integration's token acts as."""

    team_id: str
    user_id: str = ""
    enterprise_id: str = ""


def _identity_key(integration_id: str, token: str) -> str:
    # Keyed on the token too: a reconnect to another workspace is a new identity.
    digest = hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()[:24]
    return f"slack:identity:{integration_id}:{digest}"


async def fetch_slack_identity(token: str, http: httpx.AsyncClient) -> SlackIdentity | None:
    """Ask Slack who *token* is (``auth.test``); ``None`` when it cannot say."""
    try:
        resp = await http.post(
            SLACK_AUTH_TEST_URL,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_IDENTITY_LOOKUP_TIMEOUT_SECONDS,
        )
        data = resp.json() if resp.status_code == 200 else {}
    except Exception as exc:
        logger.warning("Slack auth.test failed: %s", format_exception_message(exc))
        return None
    if not isinstance(data, dict) or not data.get("ok") or not isinstance(data.get("team_id"), str):
        return None
    return SlackIdentity(
        team_id=data["team_id"],
        user_id=data.get("user_id") if isinstance(data.get("user_id"), str) else "",
        enterprise_id=data.get("enterprise_id") if isinstance(data.get("enterprise_id"), str) else "",
    )


async def slack_identity(
    integration_id: str, token: str, *, redis, http: httpx.AsyncClient
) -> SlackIdentity | None:
    """The cached Slack identity of an integration's token, resolved once per token."""
    key = _identity_key(integration_id, token)
    if redis is not None:
        try:
            cached = await redis.get(key)
        except Exception:
            cached = None
        if cached is not None:
            if isinstance(cached, bytes):
                cached = cached.decode("utf-8", "replace")
            if cached == _MISS:
                return None
            try:
                data = json.loads(cached)
                return SlackIdentity(**{k: str(data.get(k) or "") for k in ("team_id", "user_id", "enterprise_id")})
            except (ValueError, TypeError):
                pass
    identity = await fetch_slack_identity(token, http)
    if redis is not None:
        try:
            if identity is None:
                await redis.set(key, _MISS, ex=_IDENTITY_MISS_TTL_SECONDS)
            else:
                await redis.set(key, json.dumps(identity.__dict__), ex=_IDENTITY_TTL_SECONDS)
        except Exception as exc:
            logger.debug("Could not cache the Slack identity of %s: %s", integration_id, format_exception_message(exc))
    return identity


# ---------------------------------------------------------------------------
# Who a delivery is for
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlackDeliveryScope:
    """The installations Slack delivered one event for."""

    teams: frozenset[str]
    enterprises: frozenset[str] = frozenset()
    users: frozenset[str] = frozenset()


def delivery_scope(envelope: dict) -> SlackDeliveryScope:
    """Read the installations of an ``event_callback`` envelope."""
    teams: set[str] = set()
    enterprises: set[str] = set()
    users: set[str] = set()
    authorizations = envelope.get("authorizations")
    if isinstance(authorizations, list):
        for auth in authorizations:
            if not isinstance(auth, dict):
                continue
            team = auth.get("team_id")
            if isinstance(team, str) and team:
                teams.add(team)
            enterprise = auth.get("enterprise_id")
            if auth.get("is_enterprise_install") is True and isinstance(enterprise, str) and enterprise:
                enterprises.add(enterprise)
            user = auth.get("user_id")
            if auth.get("is_bot") is not True and isinstance(user, str) and user:
                users.add(user)
    if not teams and not enterprises:
        team = envelope.get("team_id")
        if isinstance(team, str) and team:
            teams.add(team)
    return SlackDeliveryScope(frozenset(teams), frozenset(enterprises), frozenset(users))


def integration_may_see(identity: SlackIdentity, scope: SlackDeliveryScope, event: dict) -> bool:
    """Whether the user behind *identity* can read the message of *event*."""
    in_workspace = identity.team_id in scope.teams or (
        bool(identity.enterprise_id) and identity.enterprise_id in scope.enterprises
    )
    if not in_workspace:
        return False
    if event.get("channel_type") == "channel":
        return True
    if not identity.user_id:
        return False
    return identity.user_id == event.get("user") or identity.user_id in scope.users


# ---------------------------------------------------------------------------
# Which assignments are connected to an integration
# ---------------------------------------------------------------------------


def _as_uuid(value: object) -> uuid.UUID | None:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _slack_binding_ids(bindings: object) -> list[str]:
    ids = (bindings or {}).get("slack") if isinstance(bindings, dict) else None
    if isinstance(ids, str):
        ids = [ids]
    return [str(i) for i in ids or [] if i]


def _canonical_ids(ids: Iterable[object]) -> list[str]:
    """The ids that parse as a UUID, in canonical form: bindings may hold another spelling."""
    return [str(u) for u in (_as_uuid(i) for i in ids) if u is not None]


async def eligible_integration_ids(
    db: AsyncSession,
    acting_user_id: uuid.UUID | None,
    candidate_ids: Iterable[str],
    *,
    template: bool,
    company_automation: bool = False,
) -> set[str]:
    """The candidate integrations whose events an assignment run may receive.

    ``template``: the per-user resolution rule the run itself applies
    (``integration_tools.template_candidate_filter``: the acting user's own
    integrations, plus company-wide ones they may see whose OAuth token is shared;
    *company_automation* for a company-managed agent's run, as the assignment
    engine passes it). Otherwise a fixed binding, under the event poller's gate for
    the agent's owner (``integration_credentials.oauth_credentials_usable_clause``):
    an OAuth token is theirs or shared with acknowledgement. Whether the
    run's acting user can see the integration is not a condition: its
    events are delivered, and the dispatch says so.
    """
    ids = [u for u in (_as_uuid(c) for c in candidate_ids) if u is not None]
    if not ids:
        return set()
    from app.services.integration_tools import template_candidate_filter

    clause = (
        await template_candidate_filter(db, acting_user_id, company_automation=company_automation)
        if template
        else oauth_credentials_usable_clause(acting_user_id)
    )
    rows = await db.execute(select(Integration.id).where(Integration.id.in_(ids), clause))
    return {str(row[0]) for row in rows.all()}


# ---------------------------------------------------------------------------
# Claim a message once per assignment
# ---------------------------------------------------------------------------


async def claim_message(redis, assignment_id: object, member: str) -> bool | None:
    """Claim *member* for an assignment: ``True`` first claim, ``False`` seen, ``None`` Redis down."""
    if redis is None:
        return None
    try:
        return await claim_seen(redis, assignment_id, member)
    except Exception as exc:
        logger.warning(
            "Slack event dedup unavailable for assignment %s (failing open): %s",
            assignment_id,
            format_exception_message(exc),
        )
        return None


async def release_message(redis, assignment_id: object, member: str) -> None:
    """Give a claimed message back so the poller can still deliver it."""
    if redis is None:
        return
    try:
        await release_seen(redis, assignment_id, member)
    except Exception:
        logger.debug("Could not release Slack message %s for assignment %s", member, assignment_id)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def _receiving_integrations(
    db: AsyncSession,
    integration_ids: frozenset[str],
    scope: SlackDeliveryScope,
    event: dict,
    redis,
) -> dict[str, Integration]:
    """The verified integrations whose user belongs to this delivery and can read it."""
    ids = [u for u in (_as_uuid(i) for i in integration_ids) if u is not None]
    if not ids:
        return {}
    rows = (
        await db.execute(
            select(Integration).where(
                Integration.id.in_(ids),
                Integration.provider == "slack",
                Integration.status == "connected",
            )
        )
    ).scalars().all()
    candidates = [r for r in rows if r.access_token_enc]
    if not candidates:
        return {}
    # auth.test can take seconds per token: no pooled connection is held across it.
    await release_db_connection_before_loop(db)
    semaphore = asyncio.Semaphore(_IDENTITY_LOOKUP_CONCURRENCY)

    async with httpx.AsyncClient() as http:

        async def _resolve(integ: Integration) -> tuple[Integration, SlackIdentity | None]:
            try:
                token = decrypt_value(integ.access_token_enc)
            except Exception:
                return integ, None
            async with semaphore:
                return integ, await slack_identity(str(integ.id), token, redis=redis, http=http)

        resolved = await asyncio.gather(*(_resolve(i) for i in candidates))
    return {
        str(integ.id): integ
        for integ, identity in resolved
        if identity is not None and integration_may_see(identity, scope, event)
    }


async def _slack_integration_ids(db: AsyncSession, cache: dict[str, list[str]]) -> list[str]:
    """Every Slack integration id, read once per delivery."""
    if "ids" not in cache:
        rows = await db.execute(select(Integration.id).where(Integration.provider == "slack"))
        cache["ids"] = [str(row[0]) for row in rows.all()]
    return cache["ids"]


async def dispatch_slack_message(
    envelope: dict,
    event: dict,
    integration_ids: frozenset[str],
    *,
    compatible: bool = False,
    protected_ids: frozenset[str] = frozenset(),
) -> int:
    """Trigger the assignments one Slack message may reach; return how many ran.

    A verified delivery (*integration_ids* = the integrations whose app signed it)
    follows the workspace, conversation and connection rules above. An assignment
    connected to no Slack integration at all has no app or workspace to be scoped by,
    so it receives an accepted delivery as it always did. A *compatible* delivery -
    unverified, accepted because verification is not required - is handed out
    exactly as before signatures were checked, to every assignment its channel filter
    admits, except the assignments connected only to integrations whose app secret is
    configured (*protected_ids*): those are refused what their secret does not prove.
    """
    channel_id = event.get("channel") if isinstance(event.get("channel"), str) else ""
    ts = event.get("ts") if isinstance(event.get("ts"), str) else ""
    if not channel_id or not ts:
        return 0
    try:
        redis = await redis_client()
    except Exception as exc:
        logger.warning("Slack event dedup unavailable (failing open): %s", format_exception_message(exc))
        redis = None
    member = slack_message_key(channel_id, ts)
    scope = delivery_scope(envelope)

    triggered = 0
    async with async_session() as db:
        receiving: dict[str, Integration] = {}
        if not compatible and integration_ids:
            receiving = await _receiving_integrations(db, integration_ids, scope, event, redis)

        # Not the assignments paused while their owner is deactivated.
        assignments = [
            wf
            for wf in (
                await db.execute(
                    select(Assignment).where(
                        Assignment.trigger_type == "integration_event",
                        Assignment.enabled.is_(True),
                        owner_not_deactivated(),
                    )
                )
            ).scalars().all()
            if (wf.trigger_config or {}).get("provider") == "slack"
            and (wf.trigger_config or {}).get("event") == "new_slack_message"
        ]
        agent_ids = {wf.agent_id for wf in assignments if wf.agent_id}
        agents = {}
        if agent_ids:
            agents = {
                a.id: a
                for a in (
                    await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
                ).scalars().all()
            }

        eligible_cache: dict[tuple, set[str]] = {}
        cache: dict[str, list[str]] = {}
        visibility = TriggerIntegrationVisibility()

        async def _eligible(
            acting: uuid.UUID | None, ids: list[str], template: bool, company_automation: bool = False
        ) -> set[str]:
            key = (acting, template, company_automation, tuple(sorted(ids)))
            if key not in eligible_cache:
                eligible_cache[key] = await eligible_integration_ids(
                    db, acting, ids, template=template, company_automation=company_automation
                )
            return eligible_cache[key]

        for wf in assignments:
            agent = agents.get(wf.agent_id)
            if agent is None or not agent.enabled:
                continue
            config = wf.trigger_config or {}
            channels = assignment_slack_channels(config)
            if channels and channel_id not in channels:
                continue

            pinned = config.get("integration_id")
            fixed = _canonical_ids([str(pinned)] if pinned else _slack_binding_ids(agent.integration_bindings))
            templated = "slack" in (getattr(agent, "integration_template_bindings", None) or [])
            run_user = assignment_acting_user_id(wf, agent)
            company_run = bool(getattr(agent, "is_company_managed", False))
            integration_id: str | None = None
            if not fixed and not templated:
                # Connected to no Slack integration: no app, workspace or user to
                # scope by - what every assignment received.
                pass
            elif compatible:
                if fixed:
                    connected = set(fixed)
                elif protected_ids:
                    connected = await _eligible(
                        run_user, await _slack_integration_ids(db, cache), True, company_run
                    )
                else:
                    # No secret is stored at all: nothing to hold back, so no lookup.
                    connected = set()
                if connected and connected <= protected_ids:
                    # Every integration it runs on has a stored app secret, which
                    # this delivery did not prove.
                    continue
                integration_id = next((i for i in sorted(connected) if i not in protected_ids), None)
            else:
                if fixed:
                    candidates = [i for i in fixed if i in receiving]
                    acting = agent_acting_user_id(agent)
                    usable = await _eligible(acting, candidates, False) if candidates else set()
                else:
                    candidates = list(receiving)
                    usable = await _eligible(run_user, candidates, True, company_run) if candidates else set()
                integration_id = next((i for i in candidates if i in usable), None)
                if integration_id is None:
                    continue
                if fixed and run_user is not None and not await visibility.sees(db, run_user, integration_id):
                    # Delivered all the same, as the poller does.
                    log_unseen_trigger_integration(
                        "assignment", wf.id, integration_id=integration_id, acting_user_id=run_user
                    )

            if await is_agent_slack_post(redis, channel_id, ts, wf.id):
                # This assignment's own run posted it: the poller skips it too.
                continue

            claim = await claim_message(redis, wf.id, member)
            if claim is False:
                # Already triggered for this message: by the poller, an earlier
                # delivery, a Slack retry or another integration of a shared channel.
                continue

            payload = {
                "text": event.get("text", ""),
                "from_id": event.get("user", ""),
                "channel_id": channel_id,
                "message_id": ts,
                "trigger_type": "integration_event",
                "event": "new_slack_message",
                "provider": "slack",
                # One turn per conversation: a DM, a channel or a thread.
                CONVERSATION_KEY_FIELD: slack_conversation_key(channel_id, ts, event.get("thread_ts")),
            }
            trigger_context = {
                "type": "integration_event",
                "provider": "slack",
                "event": "new_slack_message",
                "integration_id": integration_id,
                "channel_id": channel_id,
                "message_id": ts,
                "triggered_at": datetime.now(UTC).isoformat(),
            }
            # A provider push receiver must answer 200. ``trigger_assignment``
            # raises with AI off, which would 403 the delivery - and Slack
            # disables an event subscription after repeated failures. Skip this
            # one and carry on; the released claim lets the poller retry it.
            queued = None
            try:
                run_id = await trigger_assignment(
                    assignment=wf,
                    agent=agent,
                    payload=payload,
                    db=db,
                    trigger_context=trigger_context,
                    skip_if_active=True,
                )
                if run_id is None:
                    # Busy: started at once in a free slot; with every
                    # slot taken and INTEGRATION_EVENT_QUEUE_ENABLED on, queued,
                    # past the cap if need be, rather than handed to a
                    # poller whose cursor may already be past it (or that does
                    # not serve this assignment at all). Off, it goes back to the
                    # poller as before.
                    queued = await queue_busy_integration_event(
                        wf, agent, payload, db=db, trigger_context=trigger_context, on_full="persist"
                    )
            except HTTPException as exc:
                if claim:
                    await release_message(redis, wf.id, member)
                refusal = refusal_code(exc)
                if refusal is not None:
                    logger.info(
                        "Agentic AI unavailable (%s): not dispatching assignment '%s' for this delivery",
                        refusal,
                        wf.name,
                    )
                    continue
                if isinstance(exc, AssignmentOwnerDeactivatedError):
                    # Its owner was deactivated since the query picked it:
                    # skipped like a paused one, still answered 200.
                    continue
                raise
            except asyncio.CancelledError:
                if claim:
                    await release_message(redis, wf.id, member)
                raise
            except Exception:
                if claim:
                    await release_message(redis, wf.id, member)
                raise
            if run_id is None and queued is not None and queued.run_id is not None:
                # Started in a free slot or queued; the claim stays, so the
                # poller never runs it a second time.
                triggered += 1
                continue
            if run_id is None:
                # Nothing was created for it (the integration event queue is
                # off). Give the message back to the poller.
                if claim:
                    await release_message(redis, wf.id, member)
                continue
            triggered += 1

        if triggered:
            await db.commit()
    return triggered
