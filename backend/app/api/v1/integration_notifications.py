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

"""Integration notification endpoints – Microsoft Graph, Slack & LinkedIn webhooks.

Handles real-time push notifications from external providers and triggers
matching assignments via the assignment engine. The sender of a Slack or Graph
delivery is authenticated before anything is read for it, see
``services/integration_notification_auth.py``. A LinkedIn delivery comes from
the admin's LinkedIn app and reaches only the integrations on that app, by the
provider webhook's rule.
"""

import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.api.v1.webhooks import (
    UnreadableWebhookConfigError,
    _admin_app_integrations_or_report,
    oauth_default_secret_owner,
    reveal_webhook_client_secret,
)
from app.core.database import async_session
from app.core.egress import egress_client, egress_route
from app.core.http_body import read_bounded_body
from app.core.redis import redis_client
from app.core.security import (
    compute_linkedin_challenge,
    decrypt_value,
    verify_linkedin_signature,
)
from app.core.utils import format_exception_message
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.integration import Integration
from app.models.oauth_default import OAuthDefault
from app.services.agentic_gate import refusal_code
from app.services.assignment_engine import queue_busy_pushed_event, trigger_assignment
from app.services.assignment_pause import (
    AssignmentOwnerDeactivatedError,
    owner_not_deactivated,
)
from app.services.event_poller import advance_event_cursor, mailbox_addresses
from app.services.graph_event_subscriptions import (
    SUBSCRIBABLE_EVENTS,
    handle_lifecycle_notification,
    note_notified,
    notified_event,
)
from app.services.integration_credentials import agent_acting_user_id
from app.services.integration_event_filters import (
    event_filter_reason,
    needs_mailbox,
    reason_label,
    record_filter_reasons,
    record_filtered_events,
    trigger_filter,
    without_filter_signals,
)
from app.services.integration_notification_auth import (
    admin_app_scoping_required,
    check_slack_delivery,
    graph_notification_assignments,
    log_across_processes,
    slack_signing_secrets,
    verification_required,
    warn_unscoped_admin_app_delivery,
)
from app.services.integration_user_credentials import reader_clause, resolve_token_holder
from app.services.relays import integration_egress_route
from app.services.slack_events import claim_seen, dispatch_slack_message, release_seen, slack_message_skipped
from app.services.trigger_integration_access import (
    TriggerIntegrationVisibility,
    assignment_acting_user_id,
    log_unseen_trigger_integration,
    trigger_integration_id,
)

logger = logging.getLogger(__name__)

notification_router = APIRouter()


# Slack caps a message at 40,000 characters and 50 blocks, and even fully
# \u-escaped an event_callback envelope stays near 1.3 MB; a Graph basic change
# notification is about 1 KB and a LinkedIn batch holds at most 10. 4 MiB is
# three times the largest legitimate delivery.
NOTIFICATION_MAX_BODY_BYTES = 4 * 1024 * 1024


async def _read_bounded_body(request: Request) -> bytes:
    """The request body, refused with a 413 past ``NOTIFICATION_MAX_BODY_BYTES``.

    Checked on the declared length before anything is read, then counted while
    streaming, so a chunked or understated body is cut off at the limit rather
    than buffered whole (``read_bounded_body``).
    """
    body = await read_bounded_body(request, NOTIFICATION_MAX_BODY_BYTES)
    if body is None:
        raise HTTPException(status_code=413, detail="Request body too large")
    # The stream is consumed: later reads (``request.json()``) take these bytes.
    request._body = body
    return body


def _unverified_slack_body(body: bytes) -> dict | None:
    """An unverified Slack body as a JSON object, ``None`` when it is not one."""
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_json_body(body: bytes) -> dict:
    """The request body as a JSON object: a malformed body is a 400, never a 500."""
    try:
        parsed = json.loads(body)
    except (ValueError, RecursionError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    return parsed


# ---------------------------------------------------------------------------
# Microsoft Graph change-notification endpoint (real-time push)
# ---------------------------------------------------------------------------


@notification_router.post("/notifications/microsoft")
async def microsoft_graph_notification(request: Request):
    """Receive Microsoft Graph change notifications (webhooks).

    Microsoft Graph sends a validation request (with ``validationToken``)
    during subscription creation, and POST notifications afterwards. Each
    notification of a batch must echo the ``clientState`` its subscription was
    created with; one that does not is discarded before its assignment, its
    integration or the owner's token is read, with the same answer whatever
    is wrong. A legacy record holding only the assignment id still
    serves it, with a warning, unless
    ``INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION`` is set.
    """
    from fastapi.responses import PlainTextResponse

    # Subscription validation handshake
    validation = request.query_params.get("validationToken")
    if validation:
        return PlainTextResponse(content=validation, media_type="text/plain")

    # Process actual notifications
    body = _parse_json_body(await _read_bounded_body(request))
    notifications = body.get("value", [])
    if not isinstance(notifications, list) or not all(
        isinstance(notif, dict) for notif in notifications
    ):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    if not notifications:
        return {"status": "ok", "processed": 0}

    redis = await redis_client()
    authenticated = [
        (notif, assignment_id)
        for notif, assignment_id in zip(
            notifications,
            await graph_notification_assignments(
                redis,
                notifications,
                require_verification=verification_required(),
            ),
            strict=True,
        )
        if assignment_id is not None
    ]
    if not authenticated:
        return {"status": "ok", "processed": 0}

    triggered = 0
    filtered: dict[str, int] = {}
    filtered_reasons: dict[str, dict[str, int]] = {}
    visibility = TriggerIntegrationVisibility()
    async with async_session() as db, egress_client() as http:
        for notif, assignment_id in authenticated:
            # Lifecycle notifications (reauthorization, removal, missed) carry
            # the same clientState and are acted on without a run.
            if notif.get("lifecycleEvent"):
                outcome = await handle_lifecycle_notification(redis, assignment_id, notif)
                logger.info(
                    "Graph lifecycle notification %s for assignment %s: %s",
                    notif.get("lifecycleEvent"),
                    assignment_id,
                    outcome,
                )
                continue
            resource = notif.get("resource", "")
            subscription_id = notif.get("subscriptionId", "")
            change_type = notif.get("changeType", "")
            if not isinstance(resource, str):
                continue

            # An assignment of a deactivated owner (unless company-managed) is skipped like a
            # disabled one, before its owner's token enriches anything.
            wf_result = await db.execute(
                select(Assignment).where(
                    Assignment.id == assignment_id,
                    Assignment.enabled.is_(True),
                    owner_not_deactivated(),
                )
            )
            wf = wf_result.scalar_one_or_none()
            if not wf:
                continue

            agent_result = await db.execute(
                select(Agent).where(Agent.id == wf.agent_id)
            )
            agent = agent_result.scalar_one_or_none()
            if not agent or not agent.enabled:
                continue

            # The event is the one the assignment's trigger names; a record
            # written before triggers named theirs reads it off the resource.
            trigger_config = getattr(wf, "trigger_config", None) or {}
            event_type = trigger_config.get("event")
            if event_type not in SUBSCRIBABLE_EVENTS:
                event_type = (
                    "new_email"
                    if "messages" in resource
                    else "new_teams_message"
                    if "chatMessages" in resource
                    else None
                )
            if not event_type:
                continue
            # Stamped on receipt, whatever the filter decides next: a
            # subscription whose every event is filtered still reads as
            # notified, never as one that hears nothing.
            await note_notified(redis, wf.id)

            payload: dict = {
                "change_type": change_type,
                "resource": resource,
                "subscription_id": subscription_id,
                "trigger_type": "integration_event",
                "event": event_type,
            }
            cursor_instant = ""
            # The integration the trigger reads, as the poller picks it (a
            # Graph notification comes from a Microsoft integration).
            integration_id = trigger_integration_id({**trigger_config, "provider": "microsoft"}, agent)
            access = None
            if integration_id:
                # The event comes from this integration and goes to a run
                # acting as this user: delivered even when they cannot see it,
                # and said so, whether or not its token may be used to
                # read the resource below.
                acting_user_id = assignment_acting_user_id(wf, agent)
                if acting_user_id is not None and await visibility.hides(
                    db, acting_user_id, integration_id
                ):
                    log_unseen_trigger_integration(
                        "assignment",
                        wf.id,
                        integration_id=integration_id,
                        acting_user_id=acting_user_id,
                    )
                # OAuth tokens are personal to whoever connected them:
                # this webhook runs on behalf of the agent, so the
                # agent's owner is the acting identity for the ownership check.
                int_result = await db.execute(
                    select(Integration).where(
                        Integration.id == integration_id,
                        reader_clause(agent_acting_user_id(agent)),
                    )
                )
                integration = int_result.scalar_one_or_none()
                # The owner's own account on a per_user integration.
                holder = (
                    await resolve_token_holder(db, integration, agent_acting_user_id(agent))
                    if integration is not None
                    else None
                )
                if integration and holder and holder.access_token_enc:
                    access = (
                        decrypt_value(holder.access_token_enc),
                        await integration_egress_route(db, integration),
                    )
            if access is not None:
                token, route = access
                with egress_route(route):
                    try:
                        event_payload, cursor_instant = await notified_event(
                            event_type,
                            notif,
                            wf=wf,
                            token=token,
                            redis=redis,
                            http=http,
                            integration_id=integration_id,
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to enrich Graph notification: %s", format_exception_message(e)
                        )
                        event_payload = {}
                if event_payload is None:
                    # Not an event of this trigger (a reply to a root-message
                    # trigger, an update that left the flag alone, its own post).
                    continue
                payload.update(event_payload)

            # The seen set the poller shares: each event runs once, whichever
            # path - notification or a poll after a lapse - reads it first.
            msg_id = payload.get("message_id") or ""
            if msg_id and not await claim_seen(redis, wf.id, msg_id):
                continue
            filter_config = trigger_filter(trigger_config)
            if filter_config:
                mailbox: set[str] = set()
                if access is not None and needs_mailbox(filter_config):
                    with egress_route(access[1]):
                        mailbox = await mailbox_addresses(
                            redis, http, "microsoft", access[0], str(integration_id)
                        )
                reason = event_filter_reason(event_type, payload, filter_config, mailbox=mailbox)
                if reason:
                    filtered[str(wf.id)] = filtered.get(str(wf.id), 0) + 1
                    per = filtered_reasons.setdefault(str(wf.id), {})
                    label = reason_label(reason)
                    per[label] = per.get(label, 0) + 1
                    continue
            without_filter_signals(payload)

            graph_trigger_context = {
                "type": "integration_event",
                "provider": "microsoft",
                "event": event_type,
                "change_type": change_type,
                "resource": resource,
                "subscription_id": subscription_id,
                "message_id": msg_id or None,
                "triggered_at": datetime.now(UTC).isoformat(),
            }
            # A provider push receiver must answer 200. ``trigger_assignment``
            # raises with AI off, which would 403 the delivery — and Microsoft
            # Graph and Slack *remove* a subscription after repeated failures,
            # so a day with AI off would silently lose them, and re-enabling AI
            # would not bring them back. The raise also aborted the batch before
            # ``db.commit()``. Skip this one and carry on (/code-review,
            # 2026-08-28).
            try:
                run_id = await trigger_assignment(
                    assignment=wf,
                    agent=agent,
                    payload=payload,
                    db=db,
                    trigger_context=graph_trigger_context,
                    skip_if_active=True,
                )
                if run_id is None:
                    # Busy: with INTEGRATION_EVENT_QUEUE_ENABLED on a pushed
                    # event has no poller to fetch it again, so it is queued, past
                    # the cap if need be; off, the skip stands.
                    run_id = await queue_busy_pushed_event(
                        wf, agent, payload, db=db, trigger_context=graph_trigger_context
                    )
            except HTTPException as exc:
                # AI switched off, or not licensed (Community Edition).
                refusal = refusal_code(exc)
                if refusal is not None:
                    logger.info(
                        "Agentic AI unavailable (%s): not dispatching "
                        "assignment '%s' for this delivery",
                        refusal,
                        wf.name,
                    )
                    if msg_id:
                        await release_seen(redis, wf.id, msg_id)
                    continue
                if isinstance(exc, AssignmentOwnerDeactivatedError):
                    # Its owner was deactivated since the query picked it:
                    # skipped like a paused one, still answered 200.
                    if msg_id:
                        await release_seen(redis, wf.id, msg_id)
                    continue
                raise
            await advance_event_cursor(redis, wf.id, cursor_instant)
            if run_id is None:
                # Burst guard: an active run already exists for this
                # assignment (skip logged inside trigger_assignment).
                continue
            triggered += 1

        await db.commit()

    for filtered_id, count in filtered.items():
        await record_filtered_events(filtered_id, count)
        # The reasons ``get_assignment`` lists, as the poller records them.
        await record_filter_reasons(filtered_id, filtered_reasons.get(filtered_id, {}))
    return {"status": "ok", "processed": triggered}


# ---------------------------------------------------------------------------
# Slack Events API webhook (real-time integration events)
# ---------------------------------------------------------------------------


@notification_router.post("/notifications/slack")
async def slack_events_notification(request: Request):
    """Receive Slack Events API notifications for integration-level events.

    Handles:
      - URL verification challenge (Slack sends this during setup)
      - message events (message.channels, message.groups, message.im, message.mpim)

    The ``X-Slack-Signature`` is checked before the body is parsed. A
    verified message reaches only the integrations whose app signed it, of the
    workspace it was sent for, and the assignments connected to them - see
    ``services/slack_events.py``, which also runs each assignment once per message,
    whatever the poller or a Slack retry does. A delivery no held secret proves
    never reaches an integration whose app secret is configured; by default it is still
    handed out as before signatures were checked to every other assignment, and the URL
    verification handshake is still answered.
    ``INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION`` refuses it before its body is read.
    """
    body_bytes = await _read_bounded_body(request)
    strict = verification_required()
    secrets_state = await slack_signing_secrets()
    verdict = check_slack_delivery(
        secrets_state,
        body_bytes,
        request.headers.get("X-Slack-Signature", ""),
        request.headers.get("X-Slack-Request-Timestamp", ""),
        require_verification=strict,
    )
    if not verdict.verified and strict:
        raise HTTPException(status_code=401, detail="Invalid Slack signature")
    if not verdict.accepted:
        body = _unverified_slack_body(body_bytes)
        if body is None or body.get("type") != "url_verification":
            raise HTTPException(status_code=401, detail="Invalid Slack signature")
    else:
        body = _parse_json_body(body_bytes)

    # URL verification handshake
    if body.get("type") == "url_verification":
        return {"challenge": body.get("challenge", "")}

    # Process actual events
    event = body.get("event", {})
    if not isinstance(event, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    event_type = event.get("type", "")

    if event_type != "message" or slack_message_skipped(event):
        # Only new messages, by the poller's rule (not bot messages, edits, etc.)
        return {"status": "ignored"}

    triggered = await dispatch_slack_message(
        body,
        event,
        verdict.integration_ids,
        compatible=verdict.compatible,
        protected_ids=frozenset(secrets_state.secret_for),
    )
    return {"status": "ok", "triggered": triggered}


# ---------------------------------------------------------------------------
# LinkedIn Organization Social Action Notifications (webhook push events)
# ---------------------------------------------------------------------------

# Map LinkedIn notification action → catalog event key
_LINKEDIN_ACTION_EVENT_MAP: dict[str, str] = {
    "LIKE": "linkedin_like",
    "COMMENT": "linkedin_comment",
    "ADMIN_COMMENT": "linkedin_comment",
    "COMMENT_EDIT": "linkedin_comment",
    "COMMENT_DELETE": "linkedin_comment",
    "SHARE": "linkedin_share",
    "SHARE_MENTION": "linkedin_mention",
}


def _log_linkedin_unverified() -> None:
    log_across_processes(
        "linkedin-unverified",
        logging.WARNING,
        "LinkedIn notification accepted without a verified signature: no LinkedIn "
        "OAuth default client secret is configured, as before signatures were "
        "required. Anyone who can reach this URL can post notifications. Store the "
        "LinkedIn app on Settings > Integrations > OAuth Defaults, or set "
        "INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true to drop them.",
    )


def _json_object(value: object) -> dict:
    """*value* when it is a JSON object, else ``{}``: a malformed field reads as absent."""
    return value if isinstance(value, dict) else {}


@notification_router.get("/notifications/linkedin")
async def linkedin_webhook_validation(request: Request):
    """Handle LinkedIn webhook challenge-response validation.

    LinkedIn sends a GET request with a ``challengeCode`` query parameter
    during initial registration and every 2 hours for re-validation.
    The response must include the ``challengeCode`` and an HMACSHA256
    ``challengeResponse`` computed using the app's ``clientSecret``.
    """
    challenge_code = request.query_params.get("challengeCode", "")
    if not challenge_code:
        return {"status": "ok", "message": "No challenge code provided"}

    async with async_session() as db:
        od_result = await db.execute(
            select(OAuthDefault).where(OAuthDefault.provider == "linkedin")
        )
        oauth_default = od_result.scalar_one_or_none()

        if not oauth_default or not oauth_default.client_secret_enc:
            raise HTTPException(
                status_code=400,
                detail="No LinkedIn OAuth configuration found",
            )

        try:
            client_secret = await reveal_webhook_client_secret(
                oauth_default.client_secret_enc,
                oauth_default_secret_owner(oauth_default.id),
            )
        except UnreadableWebhookConfigError as exc:
            # The POST's refusal: the challenge cannot be answered, and
            # LinkedIn re-validates on its own schedule once the key is back.
            logger.error(
                "LinkedIn challenge refused: the LinkedIn client secret "
                "(OAuth default %s) cannot be read: %s",
                oauth_default.id,
                format_exception_message(exc.__cause__ or exc),
            )
            raise HTTPException(
                status_code=503,
                detail="LinkedIn challenge cannot be answered right now",
            ) from exc
        challenge_response = compute_linkedin_challenge(challenge_code, client_secret)

        logger.info(
            "LinkedIn webhook challenge validated (integration notification endpoint)"
        )
        return {
            "challengeCode": challenge_code,
            "challengeResponse": challenge_response,
        }


@notification_router.post("/notifications/linkedin")
async def linkedin_social_action_notification(request: Request):
    """Receive LinkedIn Organization Social Action webhook notifications.

    Handles:
      - Push events for organization social actions (LIKE, COMMENT, SHARE,
        SHARE_MENTION, ADMIN_COMMENT, COMMENT_EDIT, COMMENT_DELETE)
      - Signature verification via X-LI-Signature header
      - Delivery to the matching integration_event assignments reachable
        through an integration on the app that signed it

    LinkedIn batches up to 10 notifications per payload.  Each notification
    includes a ``notificationId`` for deduplication and an ``action`` field
    that maps to the catalog event types.

    Like ``/api/hooks/providers/linkedin``, this URL belongs to the admin's
    LinkedIn OAuth app: only the OAuth default's client secret answers the
    challenge and verifies a delivery. Under
    ``INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION`` a delivery reaches an
    assignment only through an integration on that app that its agent's owner may
    use, the provider route's rule, and an unverified one reaches
    nobody; by default every LinkedIn assignment receives it, as before, logged.
    """
    body_bytes = await _read_bounded_body(request)

    # ── Signature verification (X-LI-Signature with clientSecret) ────
    # Required whenever a client secret is configured: a missing signature is
    # refused like a wrong one, and the body is not parsed before the check.
    async with async_session() as db:
        od_result = await db.execute(
            select(OAuthDefault).where(OAuthDefault.provider == "linkedin")
        )
        oauth_default = od_result.scalar_one_or_none()
    client_secret_enc = oauth_default.client_secret_enc if oauth_default else None
    if client_secret_enc:
        try:
            client_secret = await reveal_webhook_client_secret(
                client_secret_enc, oauth_default_secret_owner(oauth_default.id)
            )
        except UnreadableWebhookConfigError as exc:
            # A secret is configured but unreadable (SECRET_KEY changed, its vault
            # unreachable): the delivery cannot be verified, so it is refused.
            logger.error(
                "LinkedIn notification refused: the LinkedIn client secret "
                "(OAuth default %s) cannot be read: %s",
                oauth_default.id,
                format_exception_message(exc.__cause__ or exc),
            )
            raise HTTPException(
                status_code=503,
                detail="LinkedIn signature cannot be verified right now",
            ) from exc
        linkedin_sig = request.headers.get("X-LI-Signature", "")
        if not linkedin_sig or not verify_linkedin_signature(
            body_bytes, client_secret, linkedin_sig
        ):
            raise HTTPException(status_code=401, detail="Invalid LinkedIn signature")

    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # ── Process notifications ────────────────────────────────────────
    event_type_header = body.get("type", "")
    notifications = body.get("notifications")
    if notifications is None:
        notifications = []
    if not isinstance(notifications, list) or not all(
        isinstance(notif, dict) for notif in notifications
    ):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    if not notifications:
        return {"status": "ok", "triggered": 0}
    strict = admin_app_scoping_required()
    if not client_secret_enc:
        if strict:
            # No secret verified the sender, so no app did: there is no
            # integration to hand the delivery to.
            return {"status": "ok", "triggered": 0}
        _log_linkedin_unverified()

    triggered = 0
    async with async_session() as db:
        # Find integration_event assignments for LinkedIn (not those
        # paused while their owner is deactivated)
        wf_result = await db.execute(
            select(Assignment).where(
                Assignment.trigger_type == "integration_event",
                Assignment.enabled.is_(True),
                owner_not_deactivated(),
            )
        )
        linkedin_assignments = [
            wf
            for wf in wf_result.scalars().all()
            if (wf.trigger_config or {}).get("provider") == "linkedin"
        ]
        agent_ids = {wf.agent_id for wf in linkedin_assignments if wf.agent_id}
        agents = {}
        if agent_ids:
            agent_result = await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))
            agents = {agent.id: agent for agent in agent_result.scalars().all()}
        candidates = [
            (wf, agents[wf.agent_id])
            for wf in linkedin_assignments
            if wf.agent_id in agents and agents[wf.agent_id].enabled
        ]
        # Through an integration on the app that signed the delivery, one its
        # agent's owner may use - the provider route's rule. Applied
        # under INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION; by default every
        # LinkedIn assignment keeps receiving it, as before, and the ones the
        # rule would leave out are reported.
        reachable: dict = {}
        if candidates and client_secret_enc:
            reachable = await _admin_app_integrations_or_report(
                db, "linkedin", oauth_default, candidates
            )
        if strict:
            receivers = [(wf, agent) for wf, agent in candidates if reachable.get(str(wf.id))]
        else:
            receivers = candidates
            warn_unscoped_admin_app_delivery(
                "LinkedIn notification",
                sum(1 for wf, _agent in candidates if not reachable.get(str(wf.id))),
                "whose agent has no integration on the admin LinkedIn app its owner may use",
            )

        for notif in notifications:
            action = notif.get("action", "")
            mapped_event = (
                _LINKEDIN_ACTION_EVENT_MAP.get(action, "linkedin_social_action")
                if isinstance(action, str)
                else "linkedin_social_action"
            )
            organization_entity = notif.get("organizationalEntity", "")
            notification_id = notif.get("notificationId")
            source_post = notif.get("sourcePost", "")

            for wf, agent in receivers:
                config = wf.trigger_config or {}

                # Match against specific event or the catch-all social_action
                wf_event = config.get("event", "")
                if (
                    wf_event
                    and wf_event != mapped_event
                    and wf_event != "linkedin_social_action"
                ):
                    continue

                # Build a rich payload with notification data
                decorated_source = _json_object(notif.get("decoratedSourcePost"))
                decorated_generated = _json_object(notif.get("decoratedGeneratedActivity"))

                payload = {
                    "trigger_type": "integration_event",
                    "event": mapped_event,
                    "provider": "linkedin",
                    "action": action,
                    "notification_id": notification_id,
                    "organization_entity": organization_entity,
                    "source_post": source_post,
                    "generated_activity": notif.get("generatedActivity", ""),
                    "last_modified_at": notif.get("lastModifiedAt"),
                    "subscriber": notif.get("subscriber", ""),
                    "event_type": event_type_header,
                }

                # Include decorated data when available
                if decorated_source:
                    share_data = _json_object(decorated_source.get("share"))
                    payload["source_post_text"] = share_data.get("text", "")
                    payload["source_post_owner"] = share_data.get("owner", "")
                    payload["source_post_url"] = share_data.get("landingPageUrl", "")
                    payload["source_post_media"] = share_data.get("mediaCategory", "")

                if decorated_generated:
                    comment_data = _json_object(decorated_generated.get("comment"))
                    share_data = _json_object(decorated_generated.get("share"))
                    if comment_data:
                        payload["comment_text"] = comment_data.get("text", "")
                        payload["comment_owner"] = comment_data.get("owner", "")
                    elif share_data:
                        payload["share_text"] = share_data.get("text", "")
                        payload["share_owner"] = share_data.get("owner", "")
                        payload["share_url"] = share_data.get("landingPageUrl", "")

                linkedin_trigger_context = {
                    "type": "integration_event",
                    "provider": "linkedin",
                    "event": mapped_event,
                    "action": action,
                    "organization_entity": organization_entity,
                    "notification_id": notification_id,
                    "triggered_at": datetime.now(UTC).isoformat(),
                }
                # A provider push receiver must answer 200. ``trigger_assignment``
                # raises with AI off, which would 403 the delivery — and Microsoft
                # Graph and Slack *remove* a subscription after repeated failures,
                # so a day with AI off would silently lose them, and re-enabling AI
                # would not bring them back. The raise also aborted the batch before
                # ``db.commit()``. Skip this one and carry on (/code-review,
                # 2026-08-28).
                try:
                    run_id = await trigger_assignment(
                        assignment=wf,
                        agent=agent,
                        payload=payload,
                        db=db,
                        trigger_context=linkedin_trigger_context,
                        skip_if_active=True,
                    )
                    if run_id is None:
                        # Busy: with INTEGRATION_EVENT_QUEUE_ENABLED on a pushed
                        # event has no poller to fetch it again, so it is queued, past
                        # the cap if need be; off, the skip stands.
                        run_id = await queue_busy_pushed_event(
                            wf, agent, payload, db=db, trigger_context=linkedin_trigger_context
                        )
                except HTTPException as exc:
                    # AI switched off, or not licensed (Community Edition).
                    refusal = refusal_code(exc)
                    if refusal is not None:
                        logger.info(
                            "Agentic AI unavailable (%s): not dispatching "
                            "assignment '%s' for this delivery",
                            refusal,
                            wf.name,
                        )
                        continue
                    if isinstance(exc, AssignmentOwnerDeactivatedError):
                        # Its owner was deactivated since the query picked it:
                        # skipped like a paused one, still answered 200.
                        continue
                    raise
                if run_id is None:
                    # Burst guard: an active run already exists for this
                    # assignment (skip logged inside trigger_assignment).
                    continue
                triggered += 1

        if triggered:
            await db.commit()

    return {"status": "ok", "triggered": triggered}
