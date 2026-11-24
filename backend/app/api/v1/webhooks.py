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

"""Webhook receiver – accepts external HTTP POST payloads for assignment triggers.

Four endpoint patterns:

  **Direct assignment** (generic webhook triggers):
    POST /api/hooks/assignments/{assignment_id}
    Targets a *single* assignment directly by its ID.  This is the standard
    URL for ``trigger_type="webhook"`` assignments.  No custom path needed —
    the assignment ID is the identifier.
    Authenticate with the assignment's ``webhook_secret``, either as a static
    token (``X-Webhook-Token`` — ``X-Webhook-Secret`` is an accepted alias —
    or ``Authorization: Bearer``) or as a body signature (``X-Signature``).
    Senders that cannot compute an HMAC — most SIEM/SOAR no-code webhook
    actions — should use the static token.

  **Integration-level** (preferred for GitHub, Slack, HubSpot, etc.):
    POST /api/hooks/integrations/{integration_id}
    One URL per integration.  Events are fanned out to *all* agents that have
    this integration bound, matching ``integration_event`` assignments by provider
    and event type.  This mirrors how third-party apps really work: one OAuth
    app / one webhook URL, many consumers.

  **Per-assignment via integration** (for providers that support multiple webhooks):
    POST /api/hooks/integrations/{integration_id}/assignments/{assignment_id}
    Targets a *single* assignment directly.  Useful for providers like OpenCTI,
    GitHub, HubSpot, and Perk where you can configure as many webhooks as
    you want, each pointing at a different assignment's dedicated URL.

  **Agent-level** (legacy, deprecated):
    POST /api/hooks/{agent_id}/{path}
    Legacy per-agent endpoint.  Matches assignments with trigger_type="webhook"
    whose trigger_config.path matches ``path``.

Signature validation (both endpoints):
  - GitHub: X-Hub-Signature-256 header (HMAC-SHA256)
  - Slack: X-Slack-Signature header (HMAC-SHA256 with v0: prefix)
  - LinkedIn: X-LI-Signature header (hex HMAC-SHA256 of "hmacsha256=" + body)
  - HubSpot: X-HubSpot-Signature-v3 / v2 / v1 headers
  - OpenCTI: token-based (X-OpenCTI-Token header)
  - Perk: Tk-Signature header (HMAC-SHA256)
  - Aikido Security: X-Aikido-Webhook-Signature header (HMAC-SHA256, 30 s replay window)
  - Airwallex: x-signature over x-timestamp + body (HMAC-SHA256, 5 min replay window)
  - Rydoo: unsigned expense events, refused unless the URL carries the
    integration's webhook_token as ?token= (no token configured: refused)
  - Spendesk: x-spendesk-webhook-signature header (HMAC-SHA256)
  - Custom: X-Signature header (HMAC-SHA256)
  - Static token (direct-assignment endpoint): X-Webhook-Token (or its
    X-Webhook-Secret alias), or Authorization: Bearer — the secret verbatim,
    for senders that cannot compute a body HMAC

Idempotency
-----------
Every receiver below funnels each delivery through
``_claim_webhook_dedup_slot`` so the same logical webhook fire creates
exactly one ``AssignmentRun`` even when the underlying POST is
delivered more than once (proxy retry on slow upstream, ALB target-
group retry, sender at-least-once delivery, etc.).  The dedup key is
derived from explicit idempotency headers first
(``Idempotency-Key``, ``X-GitHub-Delivery``, ``Webhook-Id``/Svix-Id),
then a provider-specific stable identifier in the JSON payload
(OpenCTI ``notification_id``, Slack ``event_id``, HubSpot
``eventId``, generic ``type``+``id``), and finally a SHA-256 hash of
the URL scope + raw body.  Retries within the TTL receive the
original response with ``deduplicated: True`` rather than triggering
again.  See ``_claim_webhook_dedup_slot`` for the full rationale.

Delivery queue (direct assignment endpoint)
-------------------------------------------
An assignment runs one execution at a time — parallel runs of the same
assignment race the same tools and DB writes.  On
``POST /api/hooks/assignments/{assignment_id}`` a delivery arriving while a
run is active is therefore QUEUED (``trigger_or_queue_assignment``), not
discarded: it becomes a ``queued`` ``AssignmentRun`` that the reconciler
promotes FIFO once the assignment goes idle, so an accepted delivery always
produces exactly one run.  The queue is bounded per assignment; at the cap the
default policy answers 429 + ``Retry-After`` (real backpressure — senders retry
a 429 and never a 200).  Configured via ``ASSIGNMENT_QUEUE_*``.

The other receivers below still use ``trigger_assignment(skip_if_active=True)``
and DROP an overlapping trigger: they fan one delivery out to many assignments,
so there is no single accepted-delivery-to-run correspondence to preserve.

OpenCTI Integration
-------------------
OpenCTI can push notifications via:
  1. **Live Streams** – real-time SSE streams (consumed by connectors).
  2. **Webhook triggers** (platform notifiers) – POST JSON payloads to an endpoint.

For webhook triggers, OpenCTI sends a JSON payload like:
  { "type": "create", "data": { STIX bundle or entity/relationship }, "message": "...", ... }

OpenCTI supports per-assignment webhooks (``supports_assignment_webhooks: True``).
Each assignment gets a **dedicated URL** displayed in the UI:
  - Per-assignment:   POST {base_url}/api/hooks/integrations/{integration_id}/assignments/{assignment_id}
  - Integration:    POST {base_url}/api/hooks/integrations/{integration_id}
  - Agent-level:    POST {base_url}/api/hooks/{agent_id}/opencti  (legacy)

To configure in OpenCTI:
  - Go to Settings > Customization > Notifiers.
  - Create a Webhook notifier pointing to the per-assignment URL (recommended)
    or the integration-level URL for fan-out to all matching assignments.
  - Set the trigger filters (entity types, event types) in OpenCTI.
  - Optionally add a secret token in the notifier and match it in the
    integration's extra_config (``webhook_secret``).

The webhook receiver will:
  - Parse the JSON payload.
  - Normalize OpenCTI event metadata into ``_opencti_event`` for the prompt
    (including entity_type, entity_name, relationship_type, message).
  - Match against assignment trigger_config.events (if set) using the event type
    (``create``, ``update``, ``delete``).

HubSpot Integration
-------------------
HubSpot sends CRM event notifications as **batches** (JSON arrays) via
webhooks configured in the Private App's Webhooks tab.

Each element in the array looks like:
  { "subscriptionType": "contact.creation", "objectId": 12345,
    "propertyName": "email", "propertyValue": "...", ... }

The ``subscriptionType`` is ``{objectType}.{action}`` where action is one of:
  - ``creation``      → mapped to event key ``entity_created``
  - ``propertyChange`` → mapped to event key ``entity_updated``
  - ``deletion``       → mapped to event key ``entity_deleted``

Signature verification uses the Client Secret from the HubSpot Private App
(stored in ``extra_config_enc`` as ``client_secret``).  Supports v3 (preferred),
v2, and v1 signature versions.

The webhook receiver will:
  - Verify the HubSpot signature (v3/v2/v1).
  - Split the batch into individual events.
  - Normalize each event's metadata into ``_hubspot_event`` for the prompt
    (including object_type, object_id, event_type, property_name, property_value).
  - Trigger matching assignments for each distinct event type in the batch.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import reprlib
import time
import uuid as _uuid_mod
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, NoReturn

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import String, cast, literal, select, union_all
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.core.config import settings
from app.core.database import async_session
from app.core.feature_flags import is_feature_enabled
from app.core.json_sql import json_unparseable, jsonb_field, jsonb_has_key, when_json_parseable
from app.core.log_once import log_once_soon
from app.core.rate_limit import INCR_EXPIRE_LUA, enforce_rate_limit
from app.core.security import (
    UnusableSecretError,
    compute_linkedin_challenge,
    configured_secret,
    decrypt_value,
    timing_safe_header_eq,
    timing_safe_str_eq,
    verify_github_signature,
    verify_linkedin_signature,
    verify_slack_signature,
)
from app.core.utils import (
    format_exception_message,
    is_valid_uuid,
    redact_secret_text,
    request_client_ip,
    safe_uuid,
)
from app.integrations.providers.aikido.webhooks import SIGNATURE_HEADER as AIKIDO_SIGNATURE_HEADER
from app.integrations.providers.aikido.webhooks import trigger_event as aikido_trigger_event
from app.integrations.providers.aikido.webhooks import verify_signature as verify_aikido_signature
from app.integrations.providers.airwallex.webhooks import TIMESTAMP_HEADER as AIRWALLEX_TIMESTAMP_HEADER
from app.integrations.providers.airwallex.webhooks import is_delivery as is_airwallex_delivery
from app.integrations.providers.airwallex.webhooks import trigger_event as airwallex_trigger_event
from app.integrations.providers.airwallex.webhooks import verify_signature as verify_airwallex_signature
from app.integrations.providers.rydoo.webhooks import TOKEN_FIELD as RYDOO_TOKEN_FIELD
from app.integrations.providers.rydoo.webhooks import TOKEN_PARAM as RYDOO_TOKEN_PARAM
from app.integrations.providers.rydoo.webhooks import is_delivery as is_rydoo_delivery
from app.integrations.providers.rydoo.webhooks import token_matches as rydoo_token_matches
from app.integrations.providers.rydoo.webhooks import trigger_event as rydoo_trigger_event
from app.integrations.providers.spendesk.webhooks import SIGNATURE_HEADER as SPENDESK_SIGNATURE_HEADER
from app.integrations.providers.spendesk.webhooks import event_key as spendesk_event_key
from app.integrations.providers.spendesk.webhooks import verify_signature as verify_spendesk_signature
from app.models.agent import Agent
from app.models.assignment import (
    Assignment,
    read_trigger_config,
    uses_exact_event_matching,
)
from app.models.integration import Integration
from app.models.oauth_default import OAuthDefault
from app.services import ctem_analyst_mark, ctem_openaev_events, ctem_signal_listeners, github_token_installations
from app.services import event_enrichment_retry as enrichment_retry
from app.services.assignment_engine import (
    QUOTA_DROP_REASON,
    queue_busy_pushed_event,
    trigger_assignment,
    trigger_or_queue_assignment,
)
from app.services.assignment_pause import (
    AssignmentOwnerDeactivatedError,
    FlowOwnerDeactivatedError,
    is_paused_by_deactivated_owner,
    owner_not_deactivated,
)
from app.services.event_enrichment import (
    enrichment_for,
    enrichment_retryable,
    may_still_match,
    watch_subject,
    with_enrichment,
)
from app.services.integration_event_filters import (
    event_filter_reason,
    needs_enrichment,
    plain_filter_reason,
    reason_label,
    record_filter_reasons,
    record_filtered_events,
    trigger_filter,
)
from app.services.integration_notification_auth import (
    NO_CLIENT_SECRET,
    NO_OAUTH_DEFAULT,
    NO_WEBHOOK_SECRET,
    admin_app_scoping_required,
    log_rate_limited,
    warn_unscoped_admin_app_delivery,
    warn_unverified_provider_delivery,
)
from app.services.integration_state import uses_admin_oauth_app
from app.services.vault import SecretOwner, SecretUnavailableError, reveal_ciphertext

if TYPE_CHECKING:
    from app.integrations.pull_request_events import PullRequestEvent

logger = logging.getLogger(__name__)
router = APIRouter()

#: The ``ignored`` reason of a delivery to an assignment (not company-managed) whose owner's
#: account is deactivated. Answered 200: the sender has nothing to retry,
#: and it is not a routing miss, so it does not feed the drift alerts.
_OWNER_DEACTIVATED_REASON = "assignment paused: its owner's account is deactivated"
#: The same for a delivery to a flow's webhook trigger.
_FLOW_OWNER_DEACTIVATED_REASON = "flow paused: its owner's account is deactivated"


async def _owner_paused_answer(
    dedup_key: str | None, reason: str = _OWNER_DEACTIVATED_REASON
) -> dict:
    """The ``ignored`` answer for a paused assignment or flow, never cached.

    The claimed dedup slot is released, not finalized: a replayed ``ignored``
    would swallow the same delivery retried once the owner is back.
    """
    await _release_webhook_dedup_slot(dedup_key)
    return {"status": "ignored", "reason": reason}

# Per-target ceiling on assignment webhook deliveries (keyed by assignment ID,
# or agent ID + path for the legacy agent-level endpoint). A misbehaving or
# malicious sender hammering one URL gets 429s instead of fanning out runs.
#
# Configurable via ``WEBHOOK_REQUESTS_PER_MINUTE``: an alert storm is
# exactly when deliveries matter most, and a hard-coded 60/min made a
# legitimate high-cadence SIEM feed unusable with no way to raise it. The
# per-assignment delivery queue still bounds how many accepted deliveries turn
# into runs, so raising this trades 429s for visible queue depth, not for a
# stampede. Read through a helper (not bound at import) so a settings override
# — env var or test monkeypatch — takes effect without a module reload.
def _webhook_requests_per_minute() -> int:
    """Effective per-target webhook throttle (clamped so 0 can't disable it)."""
    return max(1, int(settings.webhook_requests_per_minute))

# Repeated direct webhook misses for the same assignment ID usually indicate an
# external sender still pointing at a deleted/disabled/non-webhook assignment.
# Track misses in Redis over a fixed window (TTL anchored at the first miss)
# to suppress per-request warning noise and emit a distinct actionable alert
# once the pattern is persistent.
_DIRECT_WEBHOOK_MISS_KEY_PREFIX = "webhook:direct_assignment_miss:"
_DIRECT_WEBHOOK_MISS_WINDOW_SECONDS = 3600
_DIRECT_WEBHOOK_MISS_ALERT_THRESHOLD = 10
_DIRECT_WEBHOOK_MISS_REALERT_EVERY = 25
# After the first "drift detected" ERROR fires for an assignment, set a
# longer-lived dedup key so subsequent repetitions of the same persistent
# drift condition are downgraded from ERROR → WARNING.  This stops the
# indefinite hourly error-budget drain for known-unaddressed conditions
# while retaining visibility: every alert-eligible crossing (the threshold,
# then every ``_DIRECT_WEBHOOK_MISS_REALERT_EVERY`` misses — see
# ``_should_alert_direct_webhook_miss``) still logs a WARNING, so heavy spam
# can emit several WARNINGs within one counter window.  The key expires
# after 24 h so operators get one fresh ERROR reminder per day, and is
# deleted as soon as a delivery resolves the assignment again so a new,
# unrelated drift re-alerts at ERROR immediately.
_DIRECT_WEBHOOK_DRIFT_ALERTED_PREFIX = "webhook:direct_assignment_drift_alerted:"
_DIRECT_WEBHOOK_DRIFT_ALERTED_SECONDS = 86400  # 24 hours


# ---------------------------------------------------------------------------
# Provider webhook routing cache
# ---------------------------------------------------------------------------
# ``/api/hooks/providers/{provider}`` receives delivery STORMS (e.g. dozens of
# GitHub repositories all pointing their webhooks at the platform). The
# routing data needed to decide "does this delivery match anything?" — the
# provider's OAuthDefault signature secrets plus the bound agents and their
# webhook/integration_event assignments — changes rarely, yet the handler
# used to re-load all of it (2 sessions, 3+N queries) on EVERY delivery.
# Under a storm ~40 concurrent handlers saturated the 40-connection pool and
# the uvicorn concurrency backstop shed deliveries with 429 (GitHub does not
# retry failed deliveries, so shed events were lost).
#
# The cache keeps a short-TTL, per-process snapshot per provider so the
# common no-match delivery does signature verification + Redis dedup + pure
# CPU matching with ZERO DB sessions. Matching deliveries re-load the
# matched agent/assignment rows FRESH in one session (so rows disabled or
# re-configured since the snapshot are skipped and ``trigger_assignment``
# sees live ORM objects).
#
# Trade-off: up to ``_PROVIDER_ROUTING_TTL_SECONDS`` of staleness between
# editing an assignment / agent binding / OAuthDefault secret and provider
# webhooks observing it: a trigger edited to select an event the snapshot
# does not match waits for the next load. The defense-in-depth signature
# re-check, the assignment's own filters and the admin-app scoping on
# the match path always use the fresh DB row: an integration moved off the
# admin app, a share withdrawn or no longer visible, a binding or pin removed
# stops the delivery at once; only a NEW reachable integration waits for the
# next snapshot.

_PROVIDER_ROUTING_TTL_SECONDS = 30.0
# ``provider`` is an attacker-controlled path segment — bound the cache so
# random paths can't grow it without limit. On overflow the OLDEST entry is
# evicted (see ``_store_provider_routing``) so a churn of bogus paths can't
# blow away hot providers' snapshots.
_PROVIDER_ROUTING_MAX_ENTRIES = 256

# Small backpressure gate for webhook DB work (cache loads + match-path
# triggering): a cache-cold burst queues briefly instead of stampeding the
# pool alongside the rest of the web process.
_WEBHOOK_DB_SEMAPHORE = asyncio.Semaphore(10)


@dataclass(frozen=True)
class _AssignmentMatcher:
    """Immutable snapshot of one assignment's webhook-matching config."""

    agent_id: str
    assignment_id: str
    trigger_type: str  # "integration_event" | "webhook"
    provider: str | None
    event: str
    path: str
    # A legacy allow-list stored as a string keeps its string (see
    # ``_legacy_allow_list``).
    events_filter: tuple[str, ...] | str
    # ``uses_exact_event_matching``: False is the prefix match a trigger saved
    # before exact matching existed keeps.
    exact: bool = False
    # The integrations through which this assignment may receive the admin
    # app's deliveries: each is ``(integration id, recorded GitHub App
    # installation ids or None when it records none)``. Empty = unreachable
    # once INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION applies the rule.
    integrations: tuple[tuple[str, frozenset[str] | None], ...] = ()
    # An ``integration_event`` trigger's ``filter`` (``trigger_filter``): the
    # cached routing drops what it filters out before any DB work. Left out of
    # the hash: a dict is not hashable, and the id fields identify the entry.
    payload_filter: dict | None = field(default=None, hash=False)


@dataclass(frozen=True)
class _ProviderRouting:
    """Immutable per-provider routing snapshot for webhook fan-out."""

    oauth_default_exists: bool
    extra_config_enc: str | None
    client_secret_enc: str | None
    has_agents: bool
    matchers: tuple[_AssignmentMatcher, ...]
    # The OAuth default stores a client id that cannot be decrypted, so no
    # integration can be matched to the admin app: under
    # INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION deliveries are refused with
    # a 503 instead of being answered "nothing matched".
    admin_app_unreadable: bool = False
    # The OAuth default the secrets belong to: a vault read failure is recorded on it.
    oauth_default_id: str | None = None


_provider_routing_cache: dict[str, tuple[float, _ProviderRouting]] = {}
_provider_routing_lock = asyncio.Lock()


def _webhook_request_source(request: Request) -> tuple[str, str]:
    """Return best-effort source IP and user-agent for operator diagnostics.

    ``X-Forwarded-For`` is honoured only when the ``trust_proxy_headers``
    security option is on — the same policy as the rate limiter and the audit trail
    (``app.core.utils.request_client_ip``).  This
    endpoint is unauthenticated, so trusting the header unconditionally would
    let any caller spoof the ``source_ip`` in the miss warnings and drift
    escalation errors and misdirect the operator investigation those logs
    exist to serve.
    """
    source_ip = request_client_ip(request)
    user_agent = request.headers.get("User-Agent", "")
    return source_ip or "<unknown>", user_agent or "<unknown>"


def _should_alert_direct_webhook_miss(hit_count: int) -> bool:
    """Return True when a miss count should trigger an actionable alert."""
    if hit_count < _DIRECT_WEBHOOK_MISS_ALERT_THRESHOLD:
        return False
    excess = hit_count - _DIRECT_WEBHOOK_MISS_ALERT_THRESHOLD
    return excess % _DIRECT_WEBHOOK_MISS_REALERT_EVERY == 0


async def _record_direct_webhook_miss(assignment_id: str) -> int:
    """Record a direct-webhook miss count in Redis and return the new count.

    ``assignment_id`` must be the CANONICAL UUID string (``str(uuid.UUID(...))``)
    — like the rate-limit bucket, the counter key would otherwise fragment
    across the many formatting variants (uppercase, braces, no-hyphen hex)
    that all resolve to the same assignment downstream, letting a sender
    dodge the escalation by rotating the formatting of the same ID.

    Returns ``0`` when Redis is unavailable (fail-open); callers should fall
    back to one immediate warning in that case.
    """
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        redis_key = f"{_DIRECT_WEBHOOK_MISS_KEY_PREFIX}{assignment_id}"
        count = await redis.eval(
            INCR_EXPIRE_LUA,
            1,
            redis_key,
            _DIRECT_WEBHOOK_MISS_WINDOW_SECONDS,
        )
        return int(count)
    except Exception as exc:
        logger.debug(
            "Failed to record direct webhook miss count for assignment %s: %s",
            assignment_id,
            format_exception_message(exc),
        )
        return 0


async def _check_and_set_drift_alert_dedup(assignment_id: str) -> bool:
    """Claim the cross-window drift-alert dedup slot and return True on first claim.

    Uses ``SET NX EX`` to atomically set a 24-hour dedup key for the given
    assignment on the first call, returning ``True``.  Subsequent calls within
    the same 24-hour window find the key already set and return ``False``.

    When ``False`` is returned the caller should downgrade the log from ERROR
    to WARNING to avoid indefinite hourly error-budget drain for a known-
    unaddressed drift condition.

    Returns ``True`` on any Redis failure (fail-open: the first-alert ERROR
    path is always safe to take when the dedup state is unknown).
    """
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        key = f"{_DIRECT_WEBHOOK_DRIFT_ALERTED_PREFIX}{assignment_id}"
        result = await redis.set(key, "1", ex=_DIRECT_WEBHOOK_DRIFT_ALERTED_SECONDS, nx=True)
        return bool(result)
    except Exception as exc:
        logger.debug(
            "Failed to check drift alert dedup for assignment %s: %s",
            assignment_id,
            format_exception_message(exc),
        )
        return True


async def _clear_direct_webhook_drift_state(assignment_id: str) -> None:
    """Reset the drift escalation state once the assignment resolves again.

    Deletes both the hourly miss counter and the 24-hour alert dedup key.
    Called on every delivery whose pre-claim lookup successfully resolves the
    assignment: at that point the drift condition (deleted / disabled /
    non-webhook target) is provably gone, so any leftover state belongs to a
    REMEDIATED incident.  Without this reset, a NEW genuine drift starting
    within the residual 24-hour dedup window would have its first alert
    wrongly downgraded to WARNING instead of firing a fresh ERROR.

    Best-effort: a Redis failure only means stale state expires via TTL
    (the pre-fix behaviour), so errors are swallowed at DEBUG.
    """
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        await redis.delete(
            f"{_DIRECT_WEBHOOK_MISS_KEY_PREFIX}{assignment_id}",
            f"{_DIRECT_WEBHOOK_DRIFT_ALERTED_PREFIX}{assignment_id}",
        )
    except Exception as exc:
        logger.debug(
            "Failed to clear drift state for assignment %s: %s",
            assignment_id,
            format_exception_message(exc),
        )


async def _handle_direct_webhook_miss(
    canonical_assignment_id: str, assignment_id: str, request: Request
) -> None:
    """Record one direct-webhook miss and emit the escalation-aware log.

    First miss in the window: one warning with source diagnostics (also the
    fail-open path — a Redis outage makes the counter return 0, so every
    request degrades to this immediate warning and no visibility is lost).
    Misses 2..threshold-1: suppressed.  At the threshold (and every
    ``_DIRECT_WEBHOOK_MISS_REALERT_EVERY`` misses after it) a distinct
    actionable error fires so operators can hunt down the stale sender.

    Cross-window deduplication: the first drift ERROR for a given assignment
    also sets a 24-hour dedup key.  If the drift condition persists into the
    next hourly counter window (key still exists), subsequent alerts are
    downgraded from ERROR → WARNING so the same persistent condition does not
    drain the error budget on every fresh 1-hour window.  The dedup key
    expires after 24 hours, allowing one fresh ERROR reminder per day, and is
    cleared early by ``_clear_direct_webhook_drift_state`` as soon as a
    delivery resolves the assignment again (condition remediated).
    """
    source_ip, user_agent = _webhook_request_source(request)
    miss_count = await _record_direct_webhook_miss(canonical_assignment_id)
    if miss_count <= 1:
        logger.warning(
            "Direct webhook: assignment %s not found, not a webhook trigger, "
            "or disabled (source_ip=%s user_agent=%s)",
            assignment_id,
            source_ip,
            user_agent,
        )
    elif _should_alert_direct_webhook_miss(miss_count):
        is_first_alert = await _check_and_set_drift_alert_dedup(canonical_assignment_id)
        if is_first_alert:
            logger.error(
                "Direct webhook drift detected: assignment %s has %s invalid "
                "deliveries in the last %s seconds "
                "(source_ip=%s user_agent=%s). Verify the external sender and "
                "remove/update the stale webhook target.",
                assignment_id,
                miss_count,
                _DIRECT_WEBHOOK_MISS_WINDOW_SECONDS,
                source_ip,
                user_agent,
            )
        else:
            logger.warning(
                "Direct webhook drift recurring: assignment %s still receiving "
                "invalid deliveries (count=%s in last %s seconds, "
                "source_ip=%s user_agent=%s). Stale webhook target has not been "
                "remediated.",
                assignment_id,
                miss_count,
                _DIRECT_WEBHOOK_MISS_WINDOW_SECONDS,
                source_ip,
                user_agent,
            )


@dataclass(frozen=True)
class _IntegrationRow:
    """The columns that decide whether an integration hears the admin app."""

    id: object
    provider: str
    user_id: object
    auth_type: str
    oauth_share_acknowledged: bool | None
    client_id_enc: str | None
    extra_config_enc: str | None
    uses_admin_oauth_credentials: bool | None = None


def _integration_row_columns() -> tuple:
    return (
        Integration.id,
        Integration.provider,
        Integration.user_id,
        Integration.auth_type,
        Integration.oauth_share_acknowledged,
        Integration.client_id_enc,
        Integration.extra_config_enc,
        Integration.uses_admin_oauth_credentials,
    )


def _decrypt_client_id(value_enc: str | None) -> tuple[str | None, bool]:
    """``(client id, unreadable)``: ``(None, False)`` when none is stored,
    ``(None, True)`` when one is stored but cannot be decrypted."""
    if not value_enc:
        return None, False
    try:
        return decrypt_value(value_enc) or None, False
    except Exception:
        return None, True


# An answer to a delivery the admin app cannot be told apart for: the OAuth
# default's client id is stored but unreadable (a changed SECRET_KEY). 503,
# not "nothing matched": the operator must fix it, and a 5xx is shown as a
# failed delivery the provider retries or lets the admin redeliver.
_ADMIN_APP_UNREADABLE_DETAIL = "Webhook routing cannot be resolved right now"
_ADMIN_APP_UNREADABLE_RETRY_AFTER_SECONDS = 300


def _admin_app_unreadable_error() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail=_ADMIN_APP_UNREADABLE_DETAIL,
        headers={"Retry-After": str(_ADMIN_APP_UNREADABLE_RETRY_AFTER_SECONDS)},
    )


def _log_admin_app_unreadable(provider: str, *, refused: bool = True) -> None:
    logger.error(
        "Provider webhook %s: the OAuth default's client id cannot be decrypted, "
        "so no integration can be matched to the admin app and deliveries are "
        "%s. Re-enter the OAuth default or restore the SECRET_KEY "
        "it was encrypted with.",
        provider,
        "refused with 503" if refused else "not limited to the admin app's integrations",
    )


_UNREADABLE_INTEGRATION_EFFECT = {
    "client_id": (
        "its client id cannot be decrypted, so it receives none of the admin "
        "app's deliveries"
    ),
    "config": (
        "its configuration cannot be decrypted or parsed, so no delivery naming "
        "a GitHub App installation reaches it"
    ),
}
_unreadable_integrations_warned: set[tuple[str, str]] = set()


def _warn_unreadable_integration(integration_id: object, what: str) -> None:
    """Say once per process that an integration is left out, and why.

    Re-evaluated on every routing load and matching delivery, so a line each
    time would flood the log; one names the integration to repair. Never the
    stored value.
    """
    key = (str(integration_id), what)
    if key in _unreadable_integrations_warned:
        return
    _unreadable_integrations_warned.add(key)
    logger.warning(
        "Provider webhook: integration %s is left out: %s. Re-enter its "
        "credentials or restore the SECRET_KEY they were encrypted with.",
        integration_id,
        _UNREADABLE_INTEGRATION_EFFECT[what],
    )


def _bound_integration_ids(bindings: object, provider: str) -> tuple[str, ...]:
    """The integration ids an agent binds for *provider* (fixed bindings)."""
    if not isinstance(bindings, dict):
        return ()
    ids = bindings.get(provider) or []
    if isinstance(ids, str):
        ids = [ids]
    return tuple(str(i) for i in ids if i)


def _assignment_candidate_ids(
    trigger_config: object, bindings: object, provider: str
) -> tuple[str, ...]:
    """The integrations an assignment receives this provider's events through.

    Its pinned ``trigger_config.integration_id`` when set, else its agent's
    bindings for the provider - the poller's choice.
    """
    pinned = trigger_config.get("integration_id") if isinstance(trigger_config, dict) else None
    if pinned:
        return (str(pinned),)
    return _bound_integration_ids(bindings, provider)


def _recorded_installations(provider: str, extra_config_enc: str | None) -> frozenset[str] | None:
    """GitHub App installations an integration records.

    ``None`` = it provably records none (no config, or a readable one without
    installations). An unreadable config is NOT that: it may hold
    installations this delivery is not one of, so it reads as an empty set -
    no named installation matches it (fail closed), while a delivery naming
    none still passes.
    """
    if provider != "github" or not extra_config_enc:
        return None
    try:
        extra = json.loads(decrypt_value(extra_config_enc))
    except Exception:
        return frozenset()
    if extra is None:
        return None
    if not isinstance(extra, dict):
        return frozenset()
    entries = extra.get("installations")
    if entries is not None and not isinstance(entries, list):
        return frozenset()
    ids = {
        str(entry.get("id"))
        for entry in entries or []
        if isinstance(entry, dict) and entry.get("id")
    }
    if extra.get("installation_id"):
        ids.add(str(extra["installation_id"]))
    return frozenset(ids) or None


def _event_installation_id(provider: str, payload: object) -> str | None:
    """The GitHub App installation a delivery belongs to, when it names one."""
    if provider != "github" or not isinstance(payload, dict):
        return None
    installation = payload.get("installation")
    if isinstance(installation, dict) and installation.get("id") is not None:
        return str(installation["id"])
    return None


# Owners per visibility statement: each adds a select with a few bound
# parameters to the UNION ALL, and one statement carries at most 32767.
_VISIBILITY_OWNERS_PER_QUERY = 200


async def _foreign_visible_by_owner(db, candidates_by_owner: dict, rows_by_id: dict) -> dict:
    """For each owner, which of their candidates they do NOT own are visible to them now.

    ``user_visible_filter`` for each owner (their groups, never ``is_admin``),
    the first half of ``shadow_invisible_bindings``: an acknowledged share
    stops counting once the owner no longer sees the integration. Only a
    candidate that could be usable (not OAuth, or share-acknowledged) is asked
    about. Two queries per ``_VISIBILITY_OWNERS_PER_QUERY`` owners holding one
    - their group memberships, then one ``UNION ALL`` of a filtered select per
    owner - and none when no owner does: never a query per owner, which the
    snapshot load and every matching delivery would pay.
    """
    foreign: dict = {}
    for owner_id, candidate_ids in candidates_by_owner.items():
        if owner_id is None:
            continue
        for candidate in candidate_ids:
            uid = safe_uuid(candidate)
            row = rows_by_id.get(uid)
            if (
                row is not None
                and row.user_id != owner_id
                and (row.auth_type != "oauth" or row.oauth_share_acknowledged is True)
            ):
                foreign.setdefault(owner_id, set()).add(uid)
    if not foreign:
        return {}
    from app.api.v1.helpers import user_visible_filter
    from app.models.group import GroupMember

    owners = list(foreign)
    visible: dict = {}
    for start in range(0, len(owners), _VISIBILITY_OWNERS_PER_QUERY):
        batch = owners[start : start + _VISIBILITY_OWNERS_PER_QUERY]
        memberships = await db.execute(
            select(GroupMember.user_id, cast(GroupMember.group_id, String)).where(
                GroupMember.user_id.in_(batch)
            )
        )
        group_ids: dict = {}
        for user_id, group_id in memberships.all():
            group_ids.setdefault(user_id, []).append(group_id)
        selects = [
            select(Integration.id, literal(str(owner)).label("owner")).where(
                Integration.id.in_(list(foreign[owner])),
                user_visible_filter(Integration, owner, group_ids.get(owner)),
            )
            for owner in batch
        ]
        owner_by_key = {str(owner): owner for owner in batch}
        result = await db.execute(selects[0] if len(selects) == 1 else union_all(*selects))
        for integration_id, owner_key in result.all():
            visible.setdefault(owner_by_key[owner_key], set()).add(integration_id)
    return visible


def _admin_app_integrations(
    candidate_ids: tuple[str, ...],
    owner_id: object,
    rows_by_id: dict,
    provider: str,
    admin_client_id: str | None,
    visible_foreign: set | frozenset = frozenset(),
) -> tuple[tuple[str, frozenset[str] | None], ...]:
    """Which of an assignment's integrations may receive the admin app's events.

    The provider-level route is the admin app's webhook, so an integration
    hears it only when:

    - it is on that app: ``integration_state.uses_admin_oauth_app``, the rule
      the integration API answers with - the recorded
      ``uses_admin_oauth_credentials``, else (a row created before it was
      recorded) its stored client id is the OAuth default's;
    - the agent's owner may use it, the ``shadow_invisible_bindings`` rule:
      their own, or - visible to them now (*visible_foreign*, from
      ``_foreign_visible_by_owner``) - not OAuth or share-acknowledged.
      An agent with no owner keeps the poller's rule (not OAuth or
      acknowledged): there is no user to check visibility against.
    """
    if not admin_client_id:
        return ()
    eligible: list[tuple[str, frozenset[str] | None]] = []
    for candidate in candidate_ids:
        row = rows_by_id.get(safe_uuid(candidate))
        if row is None or row.provider != provider:
            continue
        on_admin_app = uses_admin_oauth_app(
            row.uses_admin_oauth_credentials, row.client_id_enc, admin_client_id
        )
        if on_admin_app is None:
            _warn_unreadable_integration(row.id, "client_id")
        if not on_admin_app:
            continue
        owned = owner_id is not None and row.user_id == owner_id
        shareable = row.auth_type != "oauth" or row.oauth_share_acknowledged is True
        usable = owned or (
            shareable and (owner_id is None or row.id in visible_foreign)
        )
        if not usable:
            continue
        recorded = _recorded_installations(provider, row.extra_config_enc)
        if recorded is not None and not recorded:
            # The empty set is how an unreadable configuration reads.
            _warn_unreadable_integration(row.id, "config")
        eligible.append((str(row.id), recorded))
    return tuple(eligible)


def _reaches_installation(
    integrations: tuple[tuple[str, frozenset[str] | None], ...],
    installation_id: str | None,
    token_reach: dict[str, bool] | None = None,
) -> bool:
    """Whether one of *integrations* may see a delivery of *installation_id*.

    An integration recording installations sees only those. One recording none
    is not narrowed here, except by *token_reach*: what the match path read of
    the installations its own token sees (``_token_installation_reach``).
    """
    reach = token_reach or {}
    return any(
        installation_id is None
        or (reach.get(integration_id, True) if recorded is None else installation_id in recorded)
        for integration_id, recorded in integrations
    )


def _token_row_columns() -> tuple:
    """What the token check reads of an integration: its token and what fingerprints it."""
    return (
        Integration.id,
        Integration.name,
        Integration.auth_type,
        Integration.client_id_enc,
        Integration.access_token_enc,
        Integration.api_key_enc,
        Integration.extra_config_enc,
    )


async def _token_installation_reach(
    db,
    integrations_by_assignment: dict[str, tuple[tuple[str, frozenset[str] | None], ...]],
    installation_id: str,
) -> dict[str, bool]:
    """Whether each admin-app integration recording no installation may hear *installation_id*.

    Their credentials in one query, then the answers cached for them: GitHub is
    never asked here (``github_token_installations`` learns what is missing in
    the background, and an integration it knows nothing of yet is reached as
    before).
    """
    needed = {
        integration_id
        for integrations in integrations_by_assignment.values()
        for integration_id, recorded in integrations
        if recorded is None
    }
    uuids = [u for u in (safe_uuid(i) for i in needed) if u]
    if not uuids:
        return {}
    rows = await db.execute(select(*_token_row_columns()).where(Integration.id.in_(uuids)))
    candidates = [github_token_installations.token_candidate(*row) for row in rows.all()]
    return await github_token_installations.installation_reach(candidates, installation_id)


async def _admin_app_integrations_by_assignment(
    db,
    provider: str,
    oauth_default: OAuthDefault | None,
    pairs: list[tuple[Assignment, Agent]],
) -> dict[str, tuple[tuple[str, frozenset[str] | None], ...]]:
    """``_admin_app_integrations`` of each ``(assignment, agent)`` of *pairs*, on these rows.

    Every URL the admin app delivers to hands a delivery over by this one rule:
    the provider route's match path and the LinkedIn notification route.
    *oauth_default* is the app the delivery comes from (``None``:
    no app, nobody is reached). The candidates are read off the rows given, so
    a pin or binding removed since a snapshot no longer counts; the
    integrations take one ``IN`` query, and the owners' visibility of the ones
    they do not own the batched queries of ``_foreign_visible_by_owner``.
    Keyed by ``str(assignment.id)``: an empty tuple is an assignment the
    delivery does not reach.

    Raises ``_admin_app_unreadable_error`` when the OAuth default's client id is
    stored but cannot be decrypted: the delivery cannot be routed.
    """
    admin_client_id, unreadable = (
        _decrypt_client_id(oauth_default.client_id_enc) if oauth_default else (None, False)
    )
    if unreadable:
        _log_admin_app_unreadable(provider, refused=admin_app_scoping_required())
        raise _admin_app_unreadable_error()
    candidates: dict[str, tuple[str, ...]] = {}
    owner_candidates: dict = {}
    for wf, agent in pairs:
        ids = _assignment_candidate_ids(wf.trigger_config, agent.integration_bindings, provider)
        candidates[str(wf.id)] = ids
        owner_candidates.setdefault(agent.user_id, set()).update(ids)
    candidate_uuids = {
        u for ids in candidates.values() for u in (safe_uuid(i) for i in ids) if u
    }
    rows_by_id: dict = {}
    if admin_client_id and candidate_uuids:
        rows = await db.execute(
            select(*_integration_row_columns()).where(Integration.id.in_(candidate_uuids))
        )
        rows_by_id = {row[0]: _IntegrationRow(*row) for row in rows.all()}
    visible_by_owner = await _foreign_visible_by_owner(db, owner_candidates, rows_by_id)
    return {
        str(wf.id): _admin_app_integrations(
            candidates[str(wf.id)],
            agent.user_id,
            rows_by_id,
            provider,
            admin_client_id,
            visible_by_owner.get(agent.user_id, frozenset()),
        )
        for wf, agent in pairs
    }


async def _admin_app_integrations_or_report(
    db,
    provider: str,
    oauth_default: OAuthDefault | None,
    pairs: list[tuple[Assignment, Agent]],
) -> dict[str, tuple[tuple[str, frozenset[str] | None], ...]]:
    """``_admin_app_integrations_by_assignment``, which only strict mode applies.

    By default the admin-app rule is reported, not applied, so
    working it out never costs the delivery it reports on: it runs in a savepoint
    of the delivery's session, and whatever fails - an admin client id that cannot
    be decrypted included - leaves every assignment unscoped, as before the rule.
    """
    if admin_app_scoping_required():
        return await _admin_app_integrations_by_assignment(db, provider, oauth_default, pairs)
    try:
        async with db.begin_nested():
            return await _admin_app_integrations_by_assignment(db, provider, oauth_default, pairs)
    except HTTPException:
        return {}
    except Exception as exc:
        logger.warning(
            "%s delivery: could not tell which assignments the admin-app rule would "
            "leave out (%s); it is handed out as before",
            provider,
            format_exception_message(exc),
        )
        return {}


async def _scope_matchers(
    db,
    provider: str,
    admin_client_id: str | None,
    pending: list[tuple[_AssignmentMatcher, tuple[str, ...], object]],
) -> list[_AssignmentMatcher]:
    """Every pending matcher, with the admin-app integrations it may be reached through."""
    candidate_uuids = {
        u for _m, ids, _o in pending for u in (safe_uuid(i) for i in ids) if u
    }
    rows_by_id: dict = {}
    if admin_client_id and candidate_uuids:
        integration_rows = await db.execute(
            select(*_integration_row_columns()).where(
                Integration.id.in_(candidate_uuids)
            )
        )
        rows_by_id = {
            row[0]: _IntegrationRow(*row) for row in integration_rows.all()
        }
    # Integrations an owner does not own count only while visible to them.
    owner_candidates: dict = {}
    for _m, ids, owner in pending:
        owner_candidates.setdefault(owner, set()).update(ids)
    visible_by_owner = await _foreign_visible_by_owner(db, owner_candidates, rows_by_id)
    return [
        replace(
            matcher,
            integrations=_admin_app_integrations(
                candidates,
                owner_id,
                rows_by_id,
                provider,
                admin_client_id,
                visible_by_owner.get(owner_id, set()),
            ),
        )
        for matcher, candidates, owner_id in pending
    ]


def _invalidate_provider_routing_cache(provider: str | None = None) -> None:
    """Drop cached routing (all providers when *provider* is None). Test seam."""
    if provider is None:
        _provider_routing_cache.clear()
    else:
        _provider_routing_cache.pop(provider, None)


#: The most agent ids one warning about unreadable bindings names.
_UNREADABLE_BINDINGS_NAMED = 5


def _bound_or_unreadable(bound):
    """*bound* on the agents whose bindings PostgreSQL can read, and every other agent.

    One agent's bindings holding a ``\\u0000`` or lone surrogate escape made the
    ``::jsonb`` cast abort the whole query, for every provider. Those rows are
    returned instead, flagged by ``json_unparseable``, and never delivered to.
    """
    return when_json_parseable(Agent.integration_bindings, bound, otherwise=True)


def _warn_unreadable_bindings(route: str, agent_ids: list) -> None:
    """Say (rate-limited, ids only, never the content) which bound agents a delivery skipped."""
    if not agent_ids:
        return
    named = sorted(str(agent_id) for agent_id in agent_ids)
    shown = ", ".join(named[:_UNREADABLE_BINDINGS_NAMED])
    if len(named) > _UNREADABLE_BINDINGS_NAMED:
        shown += f" and {len(named) - _UNREADABLE_BINDINGS_NAMED} more"
    log_rate_limited(
        f"unreadable-bindings:{route}",
        logging.WARNING,
        "%s: skipped %d bound agent(s) (%s): their integration bindings hold a \\u0000 or lone "
        "surrogate escape PostgreSQL cannot read. An edit of the agent's integrations stores "
        "them clean.",
        route,
        len(named),
        shown,
    )


async def _agents_bound_to_provider(db, provider: str) -> dict:
    """``{agent id: (owner id, bindings)}`` of the enabled agents whose bindings name *provider*.

    An agent whose bindings PostgreSQL cannot read is skipped, and named in a
    warning when its bindings name *provider*.
    """
    rows = await db.execute(
        select(
            Agent.id,
            Agent.user_id,
            Agent.integration_bindings,
            json_unparseable(Agent.integration_bindings).label("unreadable"),
        ).where(
            _bound_or_unreadable(cast(Agent.integration_bindings, JSONB).has_key(provider)),
            Agent.enabled.is_(True),
        )
    )
    agents: dict = {}
    skipped: list = []
    for agent_id, owner_id, bindings, unreadable in rows.all():
        if not unreadable:
            agents[agent_id] = (owner_id, bindings)
        elif jsonb_has_key(bindings, provider):
            skipped.append(agent_id)
    _warn_unreadable_bindings(f"Provider webhook {provider}", skipped)
    return agents


async def _agents_bound_to_integration(
    db, provider: str, integration_id: str, *, route: str
) -> list[Agent]:
    """The enabled agents whose ``integration_bindings[provider]`` holds *integration_id*.

    ``integration_bindings`` is JSON like ``{"github": ["<uuid>", ...], ...}``.
    An agent whose bindings PostgreSQL cannot read is skipped, and named in a
    warning when its bindings name this integration.
    """
    rows = await db.execute(
        select(Agent, json_unparseable(Agent.integration_bindings).label("unreadable")).where(
            _bound_or_unreadable(cast(Agent.integration_bindings, JSONB)[provider].has_key(integration_id)),
            Agent.enabled.is_(True),
        )
    )
    agents: list[Agent] = []
    skipped: list = []
    for agent, unreadable in rows.all():
        if not unreadable:
            agents.append(agent)
        elif jsonb_has_key(jsonb_field(agent.integration_bindings, provider), integration_id):
            skipped.append(agent.id)
    _warn_unreadable_bindings(route, skipped)
    return agents


async def _load_provider_routing(provider: str) -> _ProviderRouting:
    """Load the routing snapshot for *provider* in one short session.

    Replaces the per-delivery lookups: one OAuthDefault fetch, one enabled-
    agents id query, and ONE ``IN`` query for all their assignments (the old
    code issued one assignment query per bound agent). Only the four columns
    the matchers need are selected (no full ORM rows), and matchers that can
    NEVER match this provider — an ``integration_event`` bound to a different
    ``trigger_config.provider``, a ``webhook`` with a different
    ``trigger_config.path``, or a selection that names no event (see
    ``_assignment_matcher``) — are dropped at load time: the snapshot is
    keyed by provider, so the per-delivery scan on the storm path only walks
    matchers whose provider/path equality already holds (the event/events
    filters stay runtime — they depend on the delivery's event key).
    """
    async with async_session() as db:
        od = (
            await db.execute(
                select(OAuthDefault).where(OAuthDefault.provider == provider)
            )
        ).scalar_one_or_none()

        agents = await _agents_bound_to_provider(db, provider)
        agent_ids = list(agents)

        # (matcher, candidate integration ids, agent owner) - scoped below.
        pending: list[tuple[_AssignmentMatcher, tuple[str, ...], object]] = []
        if agent_ids:
            wf_result = await db.execute(
                select(
                    Assignment.id,
                    Assignment.agent_id,
                    Assignment.trigger_type,
                    Assignment.trigger_config,
                ).where(
                    Assignment.agent_id.in_(agent_ids),
                    Assignment.trigger_type.in_(["integration_event", "webhook"]),
                    Assignment.enabled.is_(True),
                )
            )
            for wf_id, wf_agent_id, trigger_type, trigger_config in (
                wf_result.all()
            ):
                # Load-time filter (the same one the match path applies to
                # the fresh row) — never-matching assignments don't inflate
                # the snapshot or the storm-path scan.
                matcher = _assignment_matcher(
                    provider, wf_id, wf_agent_id, trigger_type, trigger_config
                )
                if matcher is None:
                    continue
                owner_id, bindings = agents.get(wf_agent_id, (None, None))
                candidates = _assignment_candidate_ids(
                    trigger_config or {}, bindings, provider
                )
                pending.append((matcher, candidates, owner_id))

        # Only the integrations on the app this route belongs to:
        # one IN query over every candidate, none without an OAuth default -
        # an unconfigured admin app has no integrations, whoever posts here.
        # The snapshot is the same in both modes: every matcher is kept with the
        # integrations the rule allows it, and ``_match_provider_assignments``
        # applies them only under INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION, an
        # option an administrator may switch at any time. By default the narrowing
        # is only reported, so working it out never costs a delivery.
        admin_client_id: str | None = None
        admin_app_unreadable = False
        if od is not None and pending:
            admin_client_id, admin_app_unreadable = _decrypt_client_id(od.client_id_enc)
            if admin_app_unreadable:
                _log_admin_app_unreadable(provider, refused=admin_app_scoping_required())
        try:
            matchers = await _scope_matchers(db, provider, admin_client_id, pending)
        except Exception as exc:
            if admin_app_scoping_required():
                raise
            logger.warning(
                "Provider webhook %s: could not tell which assignments the admin-app "
                "rule would leave out (%s); deliveries are handed out as before",
                provider,
                format_exception_message(exc),
            )
            matchers = [matcher for matcher, _candidates, _owner in pending]

        return _ProviderRouting(
            oauth_default_exists=od is not None,
            extra_config_enc=od.extra_config_enc if od else None,
            client_secret_enc=od.client_secret_enc if od else None,
            has_agents=bool(agent_ids),
            matchers=tuple(matchers),
            admin_app_unreadable=admin_app_unreadable,
            oauth_default_id=str(od.id) if od is not None else None,
        )


def _store_provider_routing(provider: str, routing: _ProviderRouting) -> None:
    """Insert a routing snapshot, evicting the OLDEST entry when full.

    Must be called while holding ``_provider_routing_lock``. Evicting a
    single stale entry (instead of clearing the whole dict) keeps the bound
    while preserving hot providers' snapshots: the provider path segment is
    attacker-controlled, and a wholesale clear would let a churn of bogus
    paths repeatedly blow away the hot ``github`` entry and force DB reloads
    for legitimate traffic.
    """
    if (
        provider not in _provider_routing_cache
        and len(_provider_routing_cache) >= _PROVIDER_ROUTING_MAX_ENTRIES
    ):
        oldest = min(_provider_routing_cache, key=lambda k: _provider_routing_cache[k][0])
        _provider_routing_cache.pop(oldest, None)
    _provider_routing_cache[provider] = (time.monotonic(), routing)


async def _get_provider_routing(provider: str) -> _ProviderRouting:
    """Return the cached routing snapshot for *provider*, loading on miss.

    Loads are single-flight (one lock) so a cache-cold storm performs one DB
    load instead of N identical ones.
    """
    cached = _provider_routing_cache.get(provider)
    if cached and time.monotonic() - cached[0] < _PROVIDER_ROUTING_TTL_SECONDS:
        return cached[1]
    async with _provider_routing_lock:
        cached = _provider_routing_cache.get(provider)
        if cached and time.monotonic() - cached[0] < _PROVIDER_ROUTING_TTL_SECONDS:
            return cached[1]
        async with _WEBHOOK_DB_SEMAPHORE:
            routing = await _load_provider_routing(provider)
        _store_provider_routing(provider, routing)
        return routing


# Minimum age of a cached routing snapshot before a signature-verification
# failure may force-refresh it (see ``_refresh_provider_routing_after_auth_
# failure``). Bounds attacker-forced reloads at ~1/second/provider while
# letting a rotated secret take effect within ~1s instead of a full TTL.
_PROVIDER_ROUTING_SIG_RETRY_MIN_AGE_SECONDS = 1.0


async def _refresh_provider_routing_after_auth_failure(
    provider: str,
) -> _ProviderRouting | None:
    """Force-refresh the routing snapshot after a signature failure.

    A delivery signed with a freshly ROTATED secret would otherwise be
    rejected with 401 until the TTL expires — up to
    ``_PROVIDER_ROUTING_TTL_SECONDS`` of dropped deliveries (GitHub does not
    retry failed deliveries). Rate-limited: the refresh only happens when the
    cached entry is at least
    ``_PROVIDER_ROUTING_SIG_RETRY_MIN_AGE_SECONDS`` old, so a storm of
    invalid-signature requests cannot turn the cache back into a
    per-delivery DB load. Returns ``None`` when the entry is too fresh (the
    caller re-raises the original 401).
    """
    async with _provider_routing_lock:
        cached = _provider_routing_cache.get(provider)
        if (
            cached
            and time.monotonic() - cached[0]
            < _PROVIDER_ROUTING_SIG_RETRY_MIN_AGE_SECONDS
        ):
            return None
        async with _WEBHOOK_DB_SEMAPHORE:
            routing = await _load_provider_routing(provider)
        _store_provider_routing(provider, routing)
        return routing


async def _verify_provider_signatures(
    body: bytes,
    provider: str,
    extra_config_enc: str | None,
    client_secret_enc: str | None,
    request: Request,
    oauth_default_id: object | None = None,
) -> None:
    """Run BOTH provider signature gates against one set of secrets.

    Shared by the pre-claim gate (cached snapshot + rotation retry) and the
    match-path defense-in-depth re-check (fresh DB row) so the two layers
    can never drift apart: the generic ``webhook_secret`` HMAC gate plus the
    fail-closed LinkedIn ``X-LI-Signature`` gate.
    """
    _try_verify_webhook_signature(
        body, extra_config_enc, request, context=f"provider={provider}"
    )
    await _verify_linkedin_provider_signature(
        body, provider, client_secret_enc, request, oauth_default_id
    )


def provider_webhook_unverified_reason(provider: str, oauth_default: object | None) -> str | None:
    """Why ``/api/hooks/providers/<provider>`` accepts deliveries it cannot verify, ``None`` when it accepts none.

    The reading of ``_verify_provider_signatures``: the ``webhook_secret`` of the
    provider's OAuth default, and for LinkedIn its client secret, with which that
    gate requires a signature. A configuration or a secret that cannot be used
    refuses every delivery (``_read_webhook_config``, ``_usable_secret``): no
    delivery is accepted unverified then.
    """
    if oauth_default is None:
        return NO_OAUTH_DEFAULT
    if provider == "linkedin" and getattr(oauth_default, "client_secret_enc", None):
        return None
    try:
        config = decrypt_webhook_config(getattr(oauth_default, "extra_config_enc", None))
        secret = configured_secret(config.get("webhook_secret"))
    except (UnreadableWebhookConfigError, UnusableSecretError):
        return None
    if secret:
        return None
    return NO_CLIENT_SECRET if provider == "linkedin" else NO_WEBHOOK_SECRET


def _event_name(selected: object) -> str:
    """*selected* as an event name, or ``""`` when it names none.

    ``trigger_config`` is free JSON: a value that is not a string names no
    event, and a trailing ``.`` or surrounding blanks typed into a config are
    tolerated.
    """
    return selected.strip().rstrip(".") if isinstance(selected, str) else ""


def _event_key_matches(event_key: object, selected: object) -> bool:
    """Whether a delivery's *event_key* is the *selected* event.

    Keys are dot-separated, most general part first: ``{event}.{action}`` for
    GitHub (see ``_extract_event_key``), a generic sender's own ``type`` or
    ``action`` elsewhere. A selection matches the key itself and every key
    below it across a ``.``: ``issues`` matches ``issues.opened``, and
    ``repository_dispatch.deploy`` matches ``repository_dispatch.deploy.staging``
    (a dispatch action is the caller's own ``event_type`` and may carry dots).
    A bare prefix never matches: it let ``pull_request`` fire on
    ``pull_request_review_comment.created`` and ``repository`` on
    ``repository_advisory.published``.

    Both sides are free JSON (the stored ``trigger_config``, the request
    body), so a non-string key, or a selection that names no event (see
    ``_event_name``), matches nothing.
    """
    selected = _event_name(selected)
    if not selected or not isinstance(event_key, str):
        return False
    return event_key == selected or event_key.startswith(f"{selected}.")


# The live receivers meet a row on every delivery and the routing snapshot is
# rebuilt every TTL: one line per assignment and reason a day, for the whole
# platform, is enough to find it.
_SELECTION_REPORT_INTERVAL_SECONDS = 24 * 60 * 60

# ``reprlib.repr`` cuts a string past 30 characters in its middle, and an event
# name such as ``pull_request_review.submitted`` must read whole; the bound
# still keeps a name taken from a request header from filling the line.
_selection_repr = reprlib.Repr(maxstring=120, maxother=120).repr


def _report_once(assignment_id: object, reason: str, message: str, *args: object) -> None:
    if assignment_id is None:
        return
    log_once_soon(
        logger,
        logging.WARNING,
        f"webhook_selection:{assignment_id}:{reason}",
        message,
        assignment_id,
        *args,
        interval=_SELECTION_REPORT_INTERVAL_SECONDS,
    )


def _report_unusable_selection(assignment_id: object, field: str, value: object) -> None:
    """Log, once a day per assignment for the whole platform, a selection no delivery can match."""
    _report_once(
        assignment_id,
        f"unusable:{field}",
        "Assignment %s: trigger_config.%s names no event (%s), so no webhook "
        "delivery runs it until the trigger is fixed",
        field,
        _selection_repr(value),
    )


def _report_legacy_widening(
    assignment_id: object, field: str, value: object, event_key: object
) -> None:
    """Log, once a day per assignment for the whole platform, a delivery only legacy matching took."""
    _report_once(
        assignment_id,
        "legacy",
        "Assignment %s: legacy event matching ran it on %s, which "
        "trigger_config.%s (%s) does not select exactly. It was saved before "
        "exact matching and keeps its prefix match until its events "
        "are chosen again or Use exact matching is chosen on its trigger card; "
        "WEBHOOK_EXACT_EVENT_MATCHING=true (Settings > Access and security) makes every "
        "trigger exact",
        _selection_repr(event_key),
        field,
        _selection_repr(value),
    )


def _usable_event_selection(selected: object, *, exact: bool) -> bool:
    """Whether an ``event`` selection that is set can admit a delivery.

    Exact matching needs an event name. Legacy matching keeps what ``main``
    did: a falsy value selected nothing, a string was a prefix, anything else
    raised (and now matches nothing).
    """
    if exact:
        return bool(_event_name(selected))
    return not selected or isinstance(selected, str)


def _event_selected(
    event_key: object,
    selected: object,
    *,
    exact: bool,
    assignment_id: object = None,
) -> bool:
    """Whether a delivery keyed *event_key* passes an ``event`` selection.

    No selection (absent, ``null``, ``""``) admits every event of the
    provider. Exact matching (``uses_exact_event_matching``) crosses the dot
    boundary (``_event_key_matches``); a selection that names no event - a
    list, a number, an empty list, a blank string - admits none. Legacy
    matching is the prefix match a trigger saved before exact matching existed always had, and
    a delivery it admits only by prefix is reported. A selection that names
    no event is reported too.
    """
    if selected is None or selected == "":
        return True
    if not _usable_event_selection(selected, exact=exact):
        _report_unusable_selection(assignment_id, "event", selected)
        return False
    if exact:
        return _event_key_matches(event_key, selected)
    if not selected:
        return True
    if not (isinstance(event_key, str) and event_key.startswith(selected)):
        return False
    if not _event_key_matches(event_key, selected):
        _report_legacy_widening(assignment_id, "event", selected, event_key)
    return True


def _events_allow_list(events_filter: object) -> tuple[str, ...] | None:
    """The entries of an exact ``events`` allow-list, ``None`` if it does not filter.

    ``trigger_config`` is free JSON. Absent, ``null``, ``""`` and an empty
    list do not filter. A string is read as a comma-separated list rather
    than walked letter by letter, and an empty entry is dropped: a list of
    nothing but empty entries does not filter either (the old bare prefix let
    every key through ``[""]``). An entry that is not a string, or a value
    that is not a list (a number, a boolean, an object), names no event and
    admits none: skipping it must never widen the list to every event.
    """
    if events_filter is None or (
        isinstance(events_filter, str | list | tuple) and not events_filter
    ):
        return None
    entries: Sequence[object]
    if isinstance(events_filter, str):
        entries = events_filter.split(",")
    elif isinstance(events_filter, list | tuple):
        entries = events_filter
    else:
        return ()
    named = tuple(e for e in entries if isinstance(e, str) and e)
    if named or any(not isinstance(e, str) for e in entries):
        return named
    return None


def _legacy_allow_list(events_filter: object) -> tuple[str, ...] | str | None:
    """A legacy ``events`` allow-list as ``main`` read it, ``None`` if it does not filter.

    A falsy value did not filter. A string was matched as ``main`` matched it
    (``key in value`` plus a prefix per character), so it is kept as is. An
    entry that is not a string, or a value that holds no entries, raised on
    ``main``: it matches nothing.
    """
    if not events_filter:
        return None
    if isinstance(events_filter, str):
        return events_filter
    if isinstance(events_filter, Iterable):
        return tuple(e for e in events_filter if isinstance(e, str))
    return ()


def _allow_list_admits_some(allowed: tuple[str, ...] | str, *, exact: bool) -> bool:
    if exact:
        return any(_event_name(selected) for selected in allowed)
    return bool(allowed)


def _event_key_allowed(
    event_key: object,
    events_filter: object,
    *,
    exact: bool,
    assignment_id: object = None,
) -> bool:
    """Whether *event_key* passes an ``events`` allow-list.

    Exact matching reads the list through ``_events_allow_list`` and crosses
    the dot boundary; legacy matching is ``main``'s ``key in list or
    key.startswith(entry)`` (``_legacy_allow_list``), and a delivery it admits
    that exact matching would not is reported. An allow-list that names no
    event admits nothing and is reported, like an ``event`` selection.
    """
    allowed = (
        _events_allow_list(events_filter) if exact else _legacy_allow_list(events_filter)
    )
    if allowed is None:
        return True
    if not _allow_list_admits_some(allowed, exact=exact):
        _report_unusable_selection(assignment_id, "events", events_filter)
        return False
    if exact:
        return any(_event_key_matches(event_key, selected) for selected in allowed)
    if not isinstance(event_key, str):
        return False
    if event_key not in allowed and not any(
        event_key.startswith(selected) for selected in allowed
    ):
        return False
    if not _event_key_allowed(event_key, events_filter, exact=True):
        _report_legacy_widening(assignment_id, "events", events_filter, event_key)
    return True


def _assignment_matcher(
    provider: str,
    assignment_id: object,
    agent_id: object,
    trigger_type: str,
    trigger_config: object,
) -> _AssignmentMatcher | None:
    """The routing entry of one assignment, ``None`` when it takes no *provider* delivery.

    Shared by the snapshot loader, which selects columns, and the match path,
    which re-reads the fresh ORM row: both read ``trigger_config`` the way
    every ORM reader does (``read_trigger_config``, the model's load-time
    reading: the legacy ``event_type`` of a trigger marked exact),
    take its matching mode from ``uses_exact_event_matching``, and leave out
    what no live receiver runs: another provider or path, a config that is
    not an object, a selection that names no event (``_event_selected``,
    ``_event_key_allowed``).
    """
    config = read_trigger_config(trigger_type, trigger_config or {})
    if not isinstance(config, dict):
        return None
    exact = uses_exact_event_matching(config)
    cfg_provider = config.get("provider")
    path = config.get("path")
    selected = config.get("event")
    raw_events = config.get("events")
    allowed = _events_allow_list(raw_events) if exact else _legacy_allow_list(raw_events)
    if trigger_type == "integration_event":
        if cfg_provider != provider:
            return None
        if selected not in (None, "") and not _usable_event_selection(selected, exact=exact):
            _report_unusable_selection(assignment_id, "event", selected)
            return None
    elif trigger_type == "webhook":
        if path != provider:
            return None
        if allowed is not None and not _allow_list_admits_some(allowed, exact=exact):
            _report_unusable_selection(assignment_id, "events", raw_events)
            return None
    else:
        return None
    return _AssignmentMatcher(
        agent_id=str(agent_id),
        assignment_id=str(assignment_id),
        trigger_type=trigger_type,
        provider=cfg_provider if isinstance(cfg_provider, str) else None,
        event=selected if isinstance(selected, str) else "",
        path=path if isinstance(path, str) else "",
        events_filter=allowed or (),
        exact=exact,
        payload_filter=(trigger_filter(config) or None) if trigger_type == "integration_event" else None,
    )


def _trigger_filter_reason(event: object, payload: object, filter_config: object) -> str | None:
    """Why an ``integration_event`` trigger's ``filter`` drops a webhook delivery, ``None`` when it runs.

    The same filter the poller and the Graph notifications apply
    (``event_filter_reason``), evaluated on the delivered body: a dropped
    delivery creates no run and no gate call, and is counted on the assignment.
    """
    if not filter_config or not isinstance(filter_config, dict) or not isinstance(payload, dict):
        return None
    return event_filter_reason(event if isinstance(event, str) else "", payload, filter_config)


def _filtered_out(m: _AssignmentMatcher, payload: object) -> str | None:
    """Why the routing entry *m*'s trigger filter drops *payload* (``_trigger_filter_reason``).

    A filter naming the ``enriched.`` namespace is left to the match path,
    which reads the facts with the assignment's credential first.
    """
    if m.trigger_type != "integration_event" or needs_enrichment(m.payload_filter):
        return None
    return _trigger_filter_reason(m.event, payload, m.payload_filter)


def _filter_reads_as_agent(trigger_config: object) -> bool:
    """Whether an assignment's filter reads a platform as its agent's owner.

    A filter naming the ``enriched.`` facts does, and so does the CTEM PIR
    score rule, whose live read on a registered (platform-managed) OpenCTI
    mints its token for that user: without the agent it has no identity and
    drops every delivery, the ones above the threshold included.
    """
    from app.services.ctem_trigger_rules import pir_rule_of

    return needs_enrichment(trigger_filter(trigger_config)) or pir_rule_of(trigger_config)


async def _assignment_filter_reason(
    db,
    provider: str,
    event_key: str,
    payload: object,
    *,
    event: object,
    trigger_config: object,
    agent: object,
    receiver_integration_id: str | None = None,
    memo: dict | None = None,
    assignment_id: object = None,
    retry_context: dict[str, object] | None = None,
) -> str | None:
    """Why an ``integration_event`` assignment's filter drops a delivery, the ``enriched`` facts read first.

    With *assignment_id*, a delivery dropped because its facts could not be
    read and the provider marks it retryable (a CI completion) is deferred
    for a further read (``services/event_enrichment_retry.py``); the reason
    is then :data:`DEFERRED_REASON`, which the caller skips without counting.

    The facts (``services/event_enrichment.py``) are read only when the filter
    names them, with the credential of the integration the delivery was
    received on (*receiver_integration_id*), else of the assignment's own;
    the delivery handed to the run is left as it came.
    """
    if provider == "opencti":
        # CTEM-08: "a vulnerability on software we run" and the policy's PIR score
        # thresholds (read live), before any filter, gate or run.
        from app.services import ctem_trigger_rules

        rule = await ctem_trigger_rules.filter_reason(
            db, trigger_config, payload, agent=agent, receiver_integration_id=receiver_integration_id
        )
        if rule is not None:
            return rule
    filter_config = trigger_filter(trigger_config)
    if not filter_config or not isinstance(payload, dict):
        return None
    # The entries reading the delivery alone first: a delivery they drop
    # (a requested run, a success the filter does not want) costs no read of
    # the facts and is never counted as an enrichment outage.
    plain = plain_filter_reason(event if isinstance(event, str) else "", payload, filter_config)
    if plain is not None:
        return plain
    facts = await enrichment_for(
        db,
        provider,
        event_key,
        payload,
        trigger_config=trigger_config,
        agent=agent,
        receiver_integration_id=receiver_integration_id,
        memo=memo,
    )
    reason = _trigger_filter_reason(event, with_enrichment(payload, facts), filter_config)
    if reason is not None and enrichment_retryable(facts) and isinstance(assignment_id, str | _uuid_mod.UUID):
        # A CI completion whose facts could not be read: the last check of a
        # head is delivered once, so it is read again later rather than lost.
        deferred = await enrichment_retry.defer(
            assignment_id=assignment_id,
            provider=provider,
            event_key=event_key,
            payload=payload,
            reason=reason,
            receiver_integration_id=receiver_integration_id,
            trigger_context=retry_context,
        )
        if deferred:
            # The two further reads may all time out while the provider is
            # slow: the head is watched as well, so its green is never lost.
            await _watch_or_admit(
                provider,
                event_key,
                payload,
                facts,
                reason=reason,
                event=event,
                filter_config=filter_config,
                assignment_id=assignment_id,
                receiver_integration_id=receiver_integration_id,
                retry_context=retry_context,
            )
            return DEFERRED_REASON
    if isinstance(assignment_id, str | _uuid_mod.UUID):
        await _watch_or_admit(
            provider,
            event_key,
            payload,
            facts,
            reason=reason,
            event=event,
            filter_config=filter_config,
            assignment_id=assignment_id,
            receiver_integration_id=receiver_integration_id,
            retry_context=retry_context,
            trigger_config=trigger_config,
        )
    return reason


async def _watch_or_admit(
    provider: str,
    event_key: str,
    payload: dict,
    facts: object,
    *,
    reason: str | None,
    event: object,
    filter_config: object,
    assignment_id: object,
    receiver_integration_id: str | None,
    retry_context: dict[str, object] | None,
    trigger_config: object = None,
) -> None:
    """Keep a CI head's last completion from being lost to a rollout (``event_enrichment_retry.watch``).

    An admitted delivery marks its head admitted for the assignment; a
    dropped one whose head may still turn the filter true watches the head,
    which the scheduler tick reads again until it settles. A delivery whose
    facts could not be read (a timeout, a provider error) watches its head
    too: nothing says it was not the head's last completion, and the deferred
    reads alone are spent within minutes of a provider slowdown. Never raises.
    """
    try:
        subject = watch_subject(provider, event_key, payload)
        if subject is None:
            return
        if reason is None:
            await enrichment_retry.mark_admitted(assignment_id, subject)
            # The run it starts may stand down on a window (a review, a bot
            # thread): the head is delivered once more at its end (#11988).
            delay = enrichment_retry.redeliver_after(trigger_config)
            if delay is not None:
                await enrichment_retry.schedule_redelivery(
                    assignment_id=assignment_id,
                    provider=provider,
                    event_key=event_key,
                    payload=payload,
                    subject=subject,
                    delay_seconds=delay,
                    trigger_context=retry_context,
                )
            return
        if isinstance(facts, dict) and (
            enrichment_retryable(facts)
            or may_still_match(provider, event if isinstance(event, str) else "", payload, facts, filter_config)
        ):
            await enrichment_retry.watch(
                assignment_id=assignment_id,
                provider=provider,
                event_key=event_key,
                payload=payload,
                subject=subject,
                receiver_integration_id=receiver_integration_id,
                trigger_context=retry_context,
            )
    except Exception as exc:
        logger.debug("Head watch not recorded: %s", format_exception_message(exc))


#: What :func:`_assignment_filter_reason` answers for a delivery it deferred for a
#: further read: not a run now, and not counted as filtered (the retry counts it).
DEFERRED_REASON = "deferred: the enriched facts are read again shortly"


def _note_filtered(
    filtered: dict[str, int],
    reasons: dict[str, dict[str, int]] | None,
    assignment_id: str,
    reason: str,
) -> None:
    """Count one delivery *assignment_id*'s filter dropped, and why."""
    filtered[assignment_id] = filtered.get(assignment_id, 0) + 1
    if reasons is not None:
        per = reasons.setdefault(assignment_id, {})
        label = reason_label(reason)
        per[label] = per.get(label, 0) + 1


async def _count_filtered(filtered: dict[str, int], reasons: dict[str, dict[str, int]] | None = None) -> None:
    """Add each assignment's dropped deliveries to its ``events_filtered_count``, and their reasons."""
    for assignment_id, count in filtered.items():
        await record_filtered_events(safe_uuid(assignment_id) or assignment_id, count)
    for assignment_id, per in (reasons or {}).items():
        await record_filter_reasons(assignment_id, per)


def _matcher_admits(m: _AssignmentMatcher, provider: str, event_key: str) -> bool:
    """Whether the routing entry *m* runs on a *provider* delivery keyed *event_key*.

    The filters the live receivers apply to an ORM row: an
    ``integration_event`` assignment on its provider and ``event`` selection,
    a ``webhook`` one on ``path`` equal to the provider and its ``events``
    allow-list, each in the assignment's own matching mode.
    """
    if m.trigger_type == "integration_event":
        return m.provider == provider and _event_selected(
            event_key, m.event, exact=m.exact, assignment_id=m.assignment_id
        )
    if m.trigger_type == "webhook":
        return m.path == provider and _event_key_allowed(
            event_key, m.events_filter, exact=m.exact, assignment_id=m.assignment_id
        )
    return False  # pragma: no cover - the snapshot only holds the two types


def _match_provider_assignments(
    routing: _ProviderRouting,
    provider: str,
    event_key: str,
    installation_id: str | None = None,
    *,
    payload: object = None,
    filtered: dict[str, int] | None = None,
    reasons: dict[str, dict[str, int]] | None = None,
) -> list[_AssignmentMatcher]:
    """Pure-CPU assignment matching against the cached routing snapshot.

    Mirrors the filter semantics the live receivers apply to ORM rows
    (``_matcher_admits``): ``integration_event`` assignments match on
    provider + event; ``webhook`` assignments match on ``trigger_config.path``
    equal to the provider and the optional events allow-list. An assignment
    is reached only through one of its admin-app integrations, and a delivery
    naming a GitHub App installation only through one that records it or
    records none - for the latter the match path then reads whether its
    own token sees the installation.

    With *payload*, an ``integration_event`` entry whose trigger ``filter``
    drops it is left out too, and counted in *filtered* (assignment id to
    count) for ``events_filtered_count``, its reason in *reasons*.
    """
    matched: list[_AssignmentMatcher] = []
    strict = admin_app_scoping_required()
    for m in routing.matchers:
        if strict and not _reaches_installation(m.integrations, installation_id):
            continue
        if not _matcher_admits(m, provider, event_key):
            continue
        if payload is not None and (reason := _filtered_out(m, payload)) is not None:
            logger.debug(
                "Provider webhook %s: event %s filtered out for assignment %s (%s)",
                provider,
                event_key,
                m.assignment_id,
                reason,
            )
            if filtered is not None:
                _note_filtered(filtered, reasons, m.assignment_id, reason)
            continue
        matched.append(m)
    return matched


# ---------------------------------------------------------------------------
# HubSpot helpers
# ---------------------------------------------------------------------------

# Map HubSpot subscriptionType suffixes → normalised event keys
_HUBSPOT_EVENT_MAP: dict[str, str] = {
    "creation": "entity_created",
    "propertyChange": "entity_updated",
    "deletion": "entity_deleted",
}


def _normalize_hubspot_event(subscription_type: str) -> str:
    """Map a HubSpot ``subscriptionType`` (e.g. ``contact.creation``) to a
    normalized event key (e.g. ``entity_created``)."""
    parts = subscription_type.rsplit(".", 1)
    if len(parts) == 2:
        return _HUBSPOT_EVENT_MAP.get(parts[1], subscription_type)
    return subscription_type


def _hubspot_object_type(subscription_type: str) -> str:
    """Extract the object type from a subscriptionType (e.g. ``contact``)."""
    parts = subscription_type.rsplit(".", 1)
    return parts[0] if len(parts) == 2 else "unknown"


def _signed_url(request: Request) -> str | None:
    """``str(request.url)`` for a signature, or ``None`` when it cannot be spelled.

    Starlette decodes the query string there as strict UTF-8. uvicorn's parsers
    refuse raw non-ASCII bytes in a URL before the app runs, another ASGI server
    might not - and a URL that cannot be spelled is not one HubSpot signed.
    """
    try:
        return str(request.url)
    except UnicodeDecodeError:
        return None


#: Answer to a delivery whose secret is configured but cannot be read. 503, not
#: 401: the sender did nothing wrong and cannot fix it, the operator must (a
#: changed SECRET_KEY), and providers retry a 5xx once the key is restored.
_UNVERIFIABLE_STATUS = 503
_UNVERIFIABLE_DETAIL = "Webhook signature cannot be verified right now"
_UNVERIFIABLE_RETRY_AFTER_SECONDS = 300


def _refuse_unverifiable_delivery(context: str, exc: BaseException | None = None) -> NoReturn:
    """Fail closed on an unreadable stored secret: ERROR log, then a 503."""
    logger.error(
        "Webhook delivery refused (%s): the stored configuration holding its "
        "secret cannot be decrypted or parsed, so the delivery cannot be "
        "verified. Re-enter the secret or restore the SECRET_KEY it was "
        "encrypted with.%s",
        context,
        f" {format_exception_message(exc)}" if exc is not None else "",
    )
    raise HTTPException(
        status_code=_UNVERIFIABLE_STATUS,
        detail=_UNVERIFIABLE_DETAIL,
        headers={"Retry-After": str(_UNVERIFIABLE_RETRY_AFTER_SECONDS)},
    )


class UnreadableWebhookConfigError(ValueError):
    """A stored secret or configuration the webhook gates cannot read."""


def decrypt_webhook_config(extra_config_enc: str | None) -> dict:
    """An integration's (or OAuth default's) decrypted ``extra_config``, ``{}`` when there is none.

    Raises :class:`UnreadableWebhookConfigError` when it is undecryptable, not
    JSON or not an object. The one reading of a stored webhook config: the
    gates refuse the delivery on it (:func:`_read_webhook_config`) and the
    security audit lists the configuration, so the two cannot disagree.
    """
    if not extra_config_enc:
        return {}
    try:
        extra = json.loads(decrypt_value(extra_config_enc))
    except Exception as exc:
        raise UnreadableWebhookConfigError("the stored configuration cannot be decrypted or parsed") from exc
    if not isinstance(extra, dict):
        raise UnreadableWebhookConfigError(f"the stored configuration is a JSON {type(extra).__name__}, not an object")
    return extra


def oauth_default_secret_owner(oauth_default_id: object | None) -> SecretOwner | None:
    """Who an OAuth default's client secret belongs to, for the vault failure record."""
    if not oauth_default_id:
        return None
    return SecretOwner(kind="oauth_default", id=str(oauth_default_id), field="client_secret")


async def reveal_webhook_client_secret(client_secret_enc: str, owner: SecretOwner | None = None) -> str:
    """An OAuth default's client secret, which LinkedIn signs its deliveries with, a vault reference resolved.

    Raises :class:`UnreadableWebhookConfigError` when it cannot be decrypted or
    its vault cannot be read (the failure is recorded against *owner*): the
    delivery is refused, never accepted unverified.
    """
    try:
        return await reveal_ciphertext(client_secret_enc, owner) or ""
    except SecretUnavailableError as exc:
        raise UnreadableWebhookConfigError("the client secret cannot be read from its vault") from exc
    except Exception as exc:
        raise UnreadableWebhookConfigError("the stored client secret cannot be decrypted") from exc


def _read_webhook_config(extra_config_enc: str | None, *, context: str) -> dict:
    """:func:`decrypt_webhook_config` for a delivery: an unreadable config refuses it.

    Never read as "no secret": the config may hold one, and it cannot be told
    apart from a config that does not.
    """
    try:
        return decrypt_webhook_config(extra_config_enc)
    except UnreadableWebhookConfigError as exc:
        _refuse_unverifiable_delivery(context, exc.__cause__)


def hubspot_webhook_secret(extra: dict) -> str | None:
    """The secret a HubSpot delivery is verified with, read out of its config.

    HubSpot signs with the app's ``client_secret``; ``webhook_secret`` is the
    fallback. A configured value no sender can prove gives way to the other
    one. ``None`` when neither is configured; :class:`UnusableSecretError`
    when one is and neither can be used, which refuses every delivery.
    """
    unusable: UnusableSecretError | None = None
    for candidate in (extra.get("client_secret"), extra.get("webhook_secret")):
        try:
            secret = configured_secret(candidate)
        except UnusableSecretError as exc:
            unusable = unusable or exc
            continue
        if secret:
            return secret
    if unusable is not None:
        raise unusable
    return None


def _hubspot_secret(extra_config_enc: str | None, *, context: str) -> str | None:
    """The secret a HubSpot delivery is verified with, or ``None`` for no check.

    :func:`hubspot_webhook_secret` of the integration's config: a delivery is
    refused when no configured secret can be used. An unreadable
    config is refused by ``_read_webhook_config``.
    """
    if not extra_config_enc:
        return None
    extra = _read_webhook_config(extra_config_enc, context=context or "hubspot")
    try:
        return hubspot_webhook_secret(extra)
    except UnusableSecretError as exc:
        logger.warning("HubSpot delivery refused%s: %s", f" ({context})" if context else "", exc)
        raise HTTPException(status_code=401, detail="Invalid HubSpot signature") from None


def _verify_hubspot_signature(
    body: bytes,
    client_secret: str,
    request: Request,
) -> None:
    """Verify a HubSpot webhook signature (v3 preferred, v2/v1 fallback).

    Callers only invoke this once a ``client_secret`` is configured, so the
    request *must* carry a usable HubSpot signature header.  A missing
    signature (no ``X-HubSpot-Signature*`` header at all, or a v3 header
    without the required timestamp) is rejected with ``HTTPException(401)``
    rather than silently passing — otherwise an unsigned replay could
    authenticate against a configured secret.
    """
    # --- v3 (HMAC-SHA256, base64) ---
    sig_v3 = request.headers.get("X-HubSpot-Signature-v3", "")
    timestamp = request.headers.get("X-HubSpot-Request-Timestamp", "")
    if sig_v3:
        if not timestamp:
            raise HTTPException(
                status_code=401, detail="Missing HubSpot v3 timestamp"
            )
        try:
            ts_ms = int(timestamp)
            if abs(time.time() * 1000 - ts_ms) > 300_000:  # 5 min
                raise HTTPException(
                    status_code=401, detail="HubSpot webhook timestamp expired"
                )
        # OverflowError: a digit string too long for a float (``1`` + 400 zeros)
        # parses as an int and then cannot be subtracted from one.
        except (ValueError, OverflowError) as exc:
            raise HTTPException(
                status_code=401, detail="Invalid HubSpot timestamp"
            ) from exc
        url = _signed_url(request)
        message = f"{request.method}{url}{body.decode('utf-8', errors='replace')}{timestamp}"
        expected = base64.b64encode(
            hmac.new(client_secret.encode(), message.encode(), hashlib.sha256).digest(),
        ).decode()
        if url is None or not timing_safe_str_eq(sig_v3, expected):
            raise HTTPException(status_code=401, detail="Invalid HubSpot v3 signature")
        return

    # --- v2 (SHA-256 hex) ---
    sig_v2 = request.headers.get("X-HubSpot-Signature-v2", "")
    if sig_v2:
        url = _signed_url(request)
        message = f"{client_secret}{request.method}{url}{body.decode('utf-8', errors='replace')}"
        expected = hashlib.sha256(message.encode()).hexdigest()
        if url is None or not timing_safe_str_eq(sig_v2, expected):
            raise HTTPException(status_code=401, detail="Invalid HubSpot v2 signature")
        return

    # --- v1 (SHA-256 of secret + body, hex) ---
    sig_v1 = request.headers.get("X-HubSpot-Signature", "")
    if sig_v1:
        expected = hashlib.sha256(
            (client_secret + body.decode("utf-8", errors="replace")).encode(),
        ).hexdigest()
        if not timing_safe_str_eq(sig_v1, expected):
            raise HTTPException(status_code=401, detail="Invalid HubSpot v1 signature")
        return

    # No usable HubSpot signature header present, but a secret is configured.
    raise HTTPException(status_code=401, detail="Missing HubSpot signature")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _extract_event_key(request: Request, payload: dict) -> str:
    """Derive a normalised event key from the inbound webhook.

    Returns a string like ``issues.closed`` (GitHub), ``message``
    (Slack), or falls back to the JSON ``type`` / ``action`` fields.
    """
    # GitHub: X-GitHub-Event header + optional action
    gh_event = request.headers.get("X-GitHub-Event", "")
    if gh_event:
        action = payload.get("action", "")
        return f"{gh_event}.{action}" if action else gh_event

    # Slack: event_callback → event.type
    if payload.get("type") == "event_callback":
        return payload.get("event", {}).get("type", "message")

    # LinkedIn: ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS → individual action types
    if payload.get("type") == "ORGANIZATION_SOCIAL_ACTION_NOTIFICATIONS":
        notifications = payload.get("notifications", [])
        if notifications:
            action = notifications[0].get("action", "").lower()
            return f"linkedin.{action}" if action else "linkedin.social_action"
        return "linkedin.social_action"

    # Aikido Security: the event type is ``event_type``, signed per delivery;
    # the trigger key is its flat form (issue.open.created -> issue_open_created),
    # so a selection never runs on a nested sibling such as issue.open.created.sast.
    aikido_event = payload.get("event_type")
    if request.headers.get(AIKIDO_SIGNATURE_HEADER) and isinstance(aikido_event, str):
        return aikido_trigger_event(aikido_event)

    # Airwallex: an Event object (``name``, ``account_id``, ``data``) stamped
    # with x-timestamp; the trigger key is airwallex_ and its flat name.
    if is_airwallex_delivery(request.headers.get, payload):
        return airwallex_trigger_event(payload["name"])

    # Rydoo: a flat expense event (eventType, expenseId, companyId); the
    # trigger key is rydoo_ and its snake form (ExpenseApproved ->
    # rydoo_expense_approved).
    if is_rydoo_delivery(payload):
        return rydoo_trigger_event(payload["eventType"])

    # Spendesk: ``event.entity`` and ``event.eventType`` name the event
    # (payables + created -> payables_created), signed per delivery.
    if request.headers.get(SPENDESK_SIGNATURE_HEADER):
        spendesk_key = spendesk_event_key(payload)
        if spendesk_key:
            return spendesk_key

    # Generic / OpenCTI
    return payload.get("type", payload.get("action", "unknown"))


def _opencti_event_type(payload: object) -> object:
    """The ``_opencti_event.event_type`` of a delivery, ``None`` when it carries none.

    ``_normalize_opencti_payload`` writes a mapping, but a sender may post the
    key itself with any JSON value: that names no OpenCTI event. Read on every
    delivery (the ``events`` check always runs), so it must never
    fail one that runs.
    """
    meta = payload.get("_opencti_event") if isinstance(payload, dict) else None
    if not isinstance(meta, dict) or not meta:
        return None
    return meta.get("event_type", "")


def _generic_webhook_event_key(payload: object, request: Request) -> object:
    """The key the direct and agent-level URLs match an ``events`` allow-list against."""
    opencti_event_type = _opencti_event_type(payload)
    if opencti_event_type is not None:
        return opencti_event_type
    if (
        isinstance(payload, dict)
        and request.headers.get(AIKIDO_SIGNATURE_HEADER)
        and isinstance(payload.get("event_type"), str)
    ):
        return aikido_trigger_event(payload["event_type"])
    if isinstance(payload, dict) and is_airwallex_delivery(request.headers.get, payload):
        return airwallex_trigger_event(payload["name"])
    if is_rydoo_delivery(payload):
        return rydoo_trigger_event(payload["eventType"])
    if request.headers.get(SPENDESK_SIGNATURE_HEADER) and spendesk_event_key(payload):
        return spendesk_event_key(payload)
    gh_action = payload.get("action", "") if isinstance(payload, dict) else ""
    gh_event = request.headers.get("X-GitHub-Event", "")
    return f"{gh_event}.{gh_action}" if gh_event else gh_action


# ---------------------------------------------------------------------------
# Webhook idempotency / deduplication
# ---------------------------------------------------------------------------
# Production observation: every webhook handler in this module
# creates a fresh ``AssignmentRun`` for each request that reaches it.
# There is no idempotency layer, so any duplicate delivery — common in
# real HTTP traffic — runs the assignment twice.  Examples that produce
# duplicate deliveries even when the *sender* reports firing only once:
#
#  - Reverse proxy retries.  Nginx (``proxy_next_upstream``), AWS ALB
#    target-group retries, Cloud Run, Cloudflare and friends silently
#    replay POST bodies to a fresh upstream when the first attempt is
#    slow, times out, or returns 5xx.  With ``uvicorn --workers N`` a
#    retry typically lands on a *different* worker than the first
#    attempt, so the in-process backpressure semaphore can't deduplicate.
#  - HTTP client retry policies (``urllib3.Retry``, GitHub Actions, the
#    OpenCTI notifier) that resend on transient errors.
#  - Sender-side at-least-once delivery (GitHub, Slack, HubSpot all
#    document that they may redeliver events).
#  - Browser double-submit on a manual test POST.
#
# Without dedup, each duplicate creates an extra ``AssignmentRun`` and
# runs the agentic loop a second time — doubling cost, side-effects
# (sub-agent tasks, KB writes, external API calls, notifications), and
# UI confusion (two near-identical runs at the same timestamp).
#
# The dedup layer below:
#  1. Computes an idempotency key — preferring an explicit header
#     (``Idempotency-Key``, ``X-GitHub-Delivery``, ``Webhook-Id``/Svix),
#     then a provider-specific stable id parsed from the JSON payload
#     (OpenCTI / HubSpot / Slack / GitHub), and finally a SHA-256 hash
#     of the URL path + raw request body.
#  2. ``SET … NX EX`` in Redis — atomic across uvicorn workers and pods.
#  3. The first arrival caches the original response under the key
#     before returning; any retry within the TTL receives the *same*
#     ``run_id`` so the caller cannot accidentally trigger a fan-out.
#
# Fails open: a Redis outage logs a WARNING and lets the request through.
# Better to occasionally double-process than to 5xx the webhook (which
# would itself cause the sender to retry harder and amplify the storm).
#
# LIMIT worth knowing: the layer is correct for retries but cannot
# distinguish a retry from a genuine RECURRENCE when the sender gives it
# nothing to key on. A sender that emits no idempotency header and no stable
# payload id falls through to the body hash, so two genuinely distinct events
# carrying byte-identical bodies inside the window (a repeated detection with
# no timestamp, a counter-less state notification) collapse into a single run
# and the second delivery replays the first's response with
# ``deduplicated: true``. The fix is on the sender: an ``Idempotency-Key``
# header makes every delivery distinct. Failing that, the window itself is
# configurable (``WEBHOOK_DEDUP_TTL_SECONDS``) so it can be shortened below
# the sender's event cadence — or lengthened past a slow sender's retry
# backoff. Read through a helper (not bound at import) so an override takes
# effect without a module reload.
DEFAULT_WEBHOOK_DEDUP_TTL_SECONDS = 300
_WEBHOOK_DEDUP_PENDING_MARKER = "__pending__"


def _webhook_dedup_ttl_seconds() -> int:
    """Effective idempotency window (clamped so 0 can't disable dedup)."""
    return max(1, int(settings.webhook_dedup_ttl_seconds))

# Headers that callers may set to opt into explicit idempotency.  Order
# is significant — earlier entries win on conflict.  Vendor-specific
# delivery IDs (``X-GitHub-Delivery``, ``Svix-Id``) are documented as
# globally unique by the provider, so they are strictly better than the
# body-hash fallback even when the body bytes happen to match a previous
# delivery (e.g. GitHub redelivers from the UI use the same payload but
# a fresh delivery UUID — we *do* want the redelivery to fire).
_DEDUP_HEADER_NAMES: tuple[str, ...] = (
    "X-Idempotency-Key",
    "Idempotency-Key",
    "X-GitHub-Delivery",
    "Webhook-Id",
    "Svix-Id",
    "X-Hook-Signature-Id",
)


def _extract_payload_dedup_id(payload: object) -> str | None:
    """Return a stable provider-specific identifier from a JSON payload.

    Handles the common shapes the receivers in this module deal with —
    OpenCTI notifier ``notification_id``, HubSpot ``eventId``, Slack
    ``event_id`` (top-level on ``event_callback``), and the generic
    ``type`` + ``id`` pair found in many JSON-API payloads.  Returns
    ``None`` when no stable identifier can be derived; callers then
    fall back to the body hash.
    """
    if not isinstance(payload, dict):
        return None

    # OpenCTI notifier — emits a unique ``notification_id`` per delivery.
    nid = payload.get("notification_id") or payload.get("notificationId")
    if isinstance(nid, str) and nid:
        return f"opencti:{nid}"

    # Slack Events API — top-level ``event_id`` is globally unique per delivery.
    eid = payload.get("event_id")
    if isinstance(eid, str) and eid:
        return f"slack:{eid}"

    # HubSpot single-event payloads — ``eventId`` is unique per CRM event.
    hs_eid = payload.get("eventId")
    if isinstance(hs_eid, (str, int)) and hs_eid:
        return f"hubspot:{hs_eid}"

    # Generic JSON-API ``type`` + ``id`` pair.  Restricting to (type, id)
    # together avoids accidentally matching JSON whose top-level ``id`` is
    # merely an entity reference reused across many distinct events.
    pid = payload.get("id")
    ptype = payload.get("type")
    if isinstance(pid, str) and pid and isinstance(ptype, str) and ptype:
        return f"json:{ptype}:{pid}"

    return None


def _compute_webhook_dedup_key(
    request: Request, body: bytes, scope: str
) -> str:
    """Compute a stable idempotency key suffix for the inbound webhook.

    Priority order (most precise → most generic):
      1. Standard idempotency headers (``Idempotency-Key`` etc.)
      2. Vendor delivery IDs (``X-GitHub-Delivery``, ``Svix-Id``)
      3. Provider-specific stable id parsed from the JSON payload
      4. SHA-256(``scope`` + ``body``) as a last resort
    """
    for header_name in _DEDUP_HEADER_NAMES:
        val = request.headers.get(header_name)
        if val:
            return f"hdr:{header_name.lower()}:{val}"

    # Best-effort payload parse — never raises on malformed bodies because
    # the dedup layer must not break the webhook for legitimate non-JSON
    # senders (e.g. ``application/x-www-form-urlencoded``).
    try:
        parsed = json.loads(body) if body else None
    except Exception:
        parsed = None
    payload_id = _extract_payload_dedup_id(parsed)
    if payload_id:
        return f"pl:{payload_id}"

    digest = hashlib.sha256()
    digest.update(scope.encode("utf-8"))
    digest.update(b"|")
    digest.update(body)
    return f"sha256:{digest.hexdigest()}"


async def _claim_webhook_dedup_slot(
    request: Request, body: bytes, scope: str
) -> tuple[bool, dict | None, str | None]:
    """Atomically claim an idempotency slot for the current delivery.

    Returns ``(is_duplicate, cached_response, redis_key)``:

    - ``is_duplicate == False``: this is the first delivery for the key.
      The caller proceeds and SHOULD call
      ``_persist_webhook_dedup_response`` with its final response so
      retries arriving within the TTL receive the same payload.
    - ``is_duplicate == True``: a previous delivery already claimed the
      slot.  The caller MUST return ``cached_response`` (or a generic
      ``{"status": "duplicate", "deduplicated": True}`` payload when
      the original is still in-flight) and trigger nothing.

    ``redis_key`` may be ``None`` if Redis is unreachable — the layer
    fails open so a Redis outage never blocks legitimate webhooks.
    """
    suffix = _compute_webhook_dedup_key(request, body, scope)
    redis_key = f"webhook:dedup:{scope}:{suffix}"

    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        was_set = await redis.set(
            redis_key,
            _WEBHOOK_DEDUP_PENDING_MARKER,
            nx=True,
            ex=_webhook_dedup_ttl_seconds(),
        )
        if was_set:
            return False, None, redis_key

        cached_raw = await redis.get(redis_key)
        if cached_raw and cached_raw != _WEBHOOK_DEDUP_PENDING_MARKER:
            try:
                cached = json.loads(cached_raw)
                if isinstance(cached, dict):
                    # Tag the replay so operators can spot it in logs / Langfuse
                    return True, {**cached, "deduplicated": True}, redis_key
            except Exception:
                # Corrupted cache entry — fall through to the generic answer.
                logger.debug(
                    "Webhook dedup cache entry for %s is unparseable; "
                    "returning generic duplicate response",
                    redis_key,
                )
        # Original delivery is still in flight (or its response was not
        # persisted yet).  Returning a generic answer is safe because the
        # important guarantee — "do not trigger again" — is upheld.
        return True, {"status": "duplicate", "deduplicated": True}, redis_key
    except Exception as exc:
        # Fail open: a transient Redis failure must never 5xx the webhook
        # because the sender would simply retry, amplifying load.
        logger.warning(
            "Webhook idempotency check failed for scope=%s (failing open): %s",
            scope,
            format_exception_message(exc),
        )
        return False, None, None


async def _persist_webhook_dedup_response(
    redis_key: str | None, response: dict
) -> None:
    """Cache the original response so retries replay it identically.

    A no-op when ``redis_key`` is ``None`` (Redis was unavailable when the
    slot was claimed) or when serialisation fails.  The response has
    already been delivered to the caller by this point — failing here
    only means a retry arriving inside the TTL will receive the generic
    in-flight ``{"status": "duplicate"}`` answer instead of the exact
    original payload, which is still correct from a dedup standpoint.
    """
    if not redis_key:
        return
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        await redis.set(
            redis_key,
            json.dumps(response, default=str),
            ex=_webhook_dedup_ttl_seconds(),
        )
    except Exception as exc:
        logger.debug(
            "Failed to persist webhook idempotency response for %s: %s",
            redis_key,
            format_exception_message(exc),
        )


async def _release_webhook_dedup_slot(redis_key: str | None) -> None:
    """Delete a previously-claimed dedup slot so legitimate retries proceed.

    Called on exception paths (signature mismatch, DB blip, transient
    crash before persist).  If the original delivery raised before
    persisting a response, leaving the ``__pending__`` marker would
    silently swallow every retry within the TTL with a generic
    ``{"status": "duplicate"}`` reply — masking the underlying error
    and preventing the caller from succeeding even after the issue is
    fixed.  Releasing the slot restores the natural retry behaviour:
    the next delivery is treated as the first one for that key.

    Like the other dedup helpers this fails open — a Redis hiccup
    while releasing simply degrades to the legacy behaviour (the slot
    expires naturally after the TTL) rather than 5xx-ing the webhook.
    """
    if not redis_key:
        return
    try:
        from app.core.redis import redis_client

        redis = await redis_client()
        await redis.delete(redis_key)
    except Exception as exc:
        logger.debug(
            "Failed to release webhook dedup slot %s: %s", redis_key, format_exception_message(exc)
        )


async def _finalize_dedup(redis_key: str | None, response: dict) -> dict:
    """Persist a final response into the dedup slot and return it.

    Used at every non-duplicate return point in a webhook handler —
    including early ``"ignored"`` returns and event-filter skips — so a
    retry arriving within the TTL replays the *actual* response
    instead of the generic ``{"status": "duplicate"}`` answer that
    would otherwise mask a real misconfiguration ("agent disabled",
    "integration not found", etc.).

    The persist step is best-effort and never raises (see
    ``_persist_webhook_dedup_response``); the caller still receives
    the response unchanged.
    """
    await _persist_webhook_dedup_response(redis_key, response)
    return response


# ---------------------------------------------------------------------------
# Provider-level webhook (admin pre-provisioned – one URL for ALL instances)
# ---------------------------------------------------------------------------
# When an admin pre-configures OAuth credentials (OAuthDefault), there is a
# single third-party app (e.g. one GitHub OAuth App) with a single webhook URL.
# This endpoint receives that app's events and hands them only to the
# assignments reachable through an integration on that app (see
# ``_admin_app_integrations``) - an integration on the user's own app gets
# its events through ``/api/hooks/integrations/{id}`` instead.


def _verify_signature(
    body: bytes,
    secret: str,
    request: Request,
) -> None:
    """Verify a webhook signature or raise 401.

    Supports GitHub, Slack, LinkedIn, HubSpot, Perk, OpenCTI token, and a
    generic ``X-Signature`` HMAC.  Callers only invoke this once a secret is
    configured (see ``_try_verify_webhook_signature``), so the request *must*
    carry a recognised signature/token header: when none is present the
    delivery is rejected with 401 instead of silently passing.  Without this
    fail-closed behaviour an unsigned replay could authenticate against a
    configured secret and reach the idempotency cache (read a cached response
    or plant a pending marker) without ever proving knowledge of the secret.
    """
    # A configured-but-empty secret cannot verify anything; treat as no-op so
    # the only callers (which already guard on a truthy secret) stay safe.
    if not secret:
        return

    gh_sig = request.headers.get("X-Hub-Signature-256", "")
    slack_sig = request.headers.get("X-Slack-Signature", "")
    slack_ts = request.headers.get("X-Slack-Request-Timestamp", "")
    linkedin_sig = request.headers.get("X-LI-Signature", "")
    perk_sig = request.headers.get("Tk-Signature", "")
    aikido_sig = request.headers.get(AIKIDO_SIGNATURE_HEADER, "")
    airwallex_ts = request.headers.get(AIRWALLEX_TIMESTAMP_HEADER, "")
    spendesk_sig = request.headers.get(SPENDESK_SIGNATURE_HEADER, "")
    opencti_token = request.headers.get("X-OpenCTI-Token", "")
    openaev_token = request.headers.get(_OPENAEV_HEADER, "")

    # HubSpot signatures (any version present → use the dedicated verifier)
    hs_sig = (
        request.headers.get("X-HubSpot-Signature-v3", "")
        or request.headers.get("X-HubSpot-Signature-v2", "")
        or request.headers.get("X-HubSpot-Signature", "")
    )

    if gh_sig:
        if not verify_github_signature(body, secret, gh_sig):
            raise HTTPException(status_code=401, detail="Invalid GitHub signature")
    elif slack_sig:
        if not verify_slack_signature(body, secret, slack_sig, slack_ts):
            raise HTTPException(status_code=401, detail="Invalid Slack signature")
    elif linkedin_sig:
        if not verify_linkedin_signature(body, secret, linkedin_sig):
            raise HTTPException(status_code=401, detail="Invalid LinkedIn signature")
    elif hs_sig:
        _verify_hubspot_signature(body, secret, request)
    elif perk_sig:
        # Perk sends HMAC-SHA256 signatures in the Tk-Signature header
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not timing_safe_str_eq(perk_sig, expected):
            raise HTTPException(
                status_code=401, detail="Invalid Perk webhook signature"
            )
    elif aikido_sig:
        # Aikido signs the JSON payload (hex HMAC-SHA256) and stamps it with
        # dispatched_at, refused past its 30-second replay window.
        ok, reason = verify_aikido_signature(body, secret, aikido_sig)
        if not ok:
            raise HTTPException(status_code=401, detail=f"Invalid Aikido webhook signature: {reason}")
    elif airwallex_ts and request.headers.get("X-Signature", ""):
        # Airwallex signs x-timestamp + the raw body (hex HMAC-SHA256) in
        # x-signature; checked before the generic X-Signature (body only).
        ok, reason = verify_airwallex_signature(body, secret, request.headers.get("X-Signature", ""), airwallex_ts)
        if not ok:
            raise HTTPException(status_code=401, detail=f"Invalid Airwallex webhook signature: {reason}")
    elif spendesk_sig:
        # Spendesk signs the payload (hex HMAC-SHA256) with the webhook secret.
        ok, reason = verify_spendesk_signature(body, secret, spendesk_sig)
        if not ok:
            raise HTTPException(status_code=401, detail=f"Invalid Spendesk webhook signature: {reason}")
    elif opencti_token:
        # OpenCTI sends a static bearer token in X-OpenCTI-Token.
        if not timing_safe_header_eq(opencti_token, secret):
            raise HTTPException(
                status_code=401, detail="Invalid OpenCTI webhook token"
            )
    elif openaev_token:
        # A provisioned OpenAEV webhook notifier sends the integration's
        # webhook_secret as a static header (services/openaev_trigger_provisioning.py).
        if not timing_safe_header_eq(openaev_token, secret):
            raise HTTPException(
                status_code=401, detail="Invalid OpenAEV webhook token"
            )
    else:
        generic_sig = request.headers.get("X-Signature", "")
        if generic_sig:
            expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            if not timing_safe_str_eq(generic_sig, expected):
                raise HTTPException(status_code=401, detail="Invalid webhook signature")
        else:
            # A secret is configured but the request carries no recognised
            # signature/token header → reject the unsigned delivery.
            raise HTTPException(status_code=401, detail="Missing webhook signature")


async def _verify_linkedin_provider_signature(
    body: bytes,
    provider: str,
    client_secret_enc: str | None,
    request: Request,
    oauth_default_id: object | None = None,
) -> None:
    """Fail-closed LinkedIn ``X-LI-Signature`` gate for the LinkedIn provider.

    LinkedIn signs deliveries with the OAuth ``clientSecret`` (NOT the generic
    ``webhook_secret`` covered by :func:`_verify_signature` /
    :func:`_try_verify_webhook_signature`), via the ``X-LI-Signature`` header,
    so it needs this provider-aware gate.

    When the provider IS LinkedIn and a client secret is configured, the
    signature header is **required**: a missing OR invalid signature is a 401.
    The prior ``if linkedin_sig and ...`` shape only verified a *present*
    header, so an unsigned replay (no header) fell through to the dedup cache
    / generic fan-out unauthenticated. For any other provider that nonetheless
    sends an ``X-LI-Signature`` while a client secret is configured, a present
    signature is still verified as defense in depth, but its absence is not
    treated as authentication.

    The client secret may be a vault reference: it is read through the
    resolver, and a vault that cannot be read refuses the delivery like an
    undecryptable secret.
    """
    if not client_secret_enc:
        return
    linkedin_sig = request.headers.get("X-LI-Signature", "")
    if provider != "linkedin" and not linkedin_sig:
        return
    try:
        client_secret = await reveal_webhook_client_secret(
            client_secret_enc, oauth_default_secret_owner(oauth_default_id)
        )
    except UnreadableWebhookConfigError as exc:
        _refuse_unverifiable_delivery(f"provider={provider} client secret", exc.__cause__)
    if not linkedin_sig or not verify_linkedin_signature(
        body, client_secret, linkedin_sig
    ):
        raise HTTPException(
            status_code=401, detail="Missing or invalid LinkedIn signature"
        )


def _try_verify_webhook_signature(
    body: bytes,
    extra_config_enc: str | None,
    request: Request,
    *,
    context: str = "",
) -> None:
    """Webhook signature verification from encrypted extra_config.

    No config, or no ``webhook_secret`` in it: nothing to verify. A config
    that cannot be read is refused (``_read_webhook_config``). An error
    raised while checking a sender's signature propagates, never reads as a
    delivery that needs no check, and a secret that is configured but unusable
    - a non-string one, falsy or not - refuses every delivery.
    """
    if not extra_config_enc:
        return
    ws = _read_webhook_config(extra_config_enc, context=context or "webhook").get("webhook_secret")
    secret = _usable_secret(ws, refusal="Invalid webhook signature", context=context)
    if secret:
        _verify_signature(body, secret, request)


def _signed_with_webhook_secret(extra_config_enc: str | None) -> bool:
    """Whether a delivery that passed :func:`_try_verify_webhook_signature` was signed.

    True when the config holds a usable ``webhook_secret``: the gate then
    refused every delivery whose signature did not match. A delivery accepted
    without a secret is not trusted with a work item's state.
    """
    try:
        return bool(configured_secret(decrypt_webhook_config(extra_config_enc).get("webhook_secret")))
    except (UnreadableWebhookConfigError, UnusableSecretError):
        return False


async def _route_pull_request_event(
    provider: str, event_key: str, payload: dict, extra_config_enc: str | None
) -> None:
    """Hand a signed pull request delivery to the work items naming it.

    The provider's parser decides whether the delivery is one
    (``integrations/pull_request_events.py``); the work-item service moves
    the items that name the pull request. Best-effort: never fails the
    delivery. An unsigned one is not trusted with a work item's state: a
    closing it announces is read back from the code host before it releases
    a blocked item, and every other kind is ignored.

    A pull request closing also resolves the active shared-state entries that
    track that pull request (``services/shared_state_pull_requests.py``), read
    back first when the delivery is unsigned.
    """
    from app.integrations.pull_request_events import parse_pull_request_event
    from app.services import shared_state_pull_requests

    event = parse_pull_request_event(provider, event_key, payload)
    if event is None:
        return
    if event.kind == "ci":
        # A successful workflow run may meet a blocked item's workflow_run reference:
        # read back from the code host in the background, signed or not.
        from app.services.work_item_blocked_refs import schedule_workflow_run_release

        schedule_workflow_run_release(event)
    if not _signed_with_webhook_secret(extra_config_enc):
        if event.kind not in ("closed", "issue_closed"):
            return
        from app.services.work_item_blocked_refs import release_from_unverified_delivery

        try:
            await release_from_unverified_delivery(event)
        except Exception as exc:
            logger.warning(
                "Unsigned pull request event %s (%s) not read back: %s",
                event_key,
                provider,
                format_exception_message(exc),
            )
        await _resolve_pull_request_states(
            shared_state_pull_requests.resolve_from_unverified_delivery, event, event_key, provider
        )
        return
    from app.services.full_ci_parking import on_base_green
    from app.services.work_item_pull_requests import handle_pull_request_event

    # A gated workflow green on a base branch runs the parked pull requests' full CI again.
    on_base_green(provider, event)
    try:
        await handle_pull_request_event(event)
    except Exception as exc:
        logger.warning(
            "Pull request event %s (%s) not applied to work items: %s",
            event_key,
            provider,
            format_exception_message(exc),
        )
    await _resolve_pull_request_states(
        shared_state_pull_requests.resolve_states_of_closed_pull_request, event, event_key, provider
    )


async def _resolve_pull_request_states(
    resolver: Callable[["PullRequestEvent"], Awaitable[int]], event: "PullRequestEvent", event_key: str, provider: str
) -> None:
    """Run *resolver* on a pull request closing; never fails the delivery."""
    if event.kind != "closed":
        return
    try:
        await resolver(event)
    except Exception as exc:
        logger.warning(
            "Pull request event %s (%s) not applied to shared state: %s",
            event_key,
            provider,
            format_exception_message(exc),
        )


def _usable_secret(value: object, *, refusal: str, context: str = "") -> str | None:
    """:func:`configured_secret` for a delivery: an unusable value is a 401.

    *refusal* is the route's answer to a wrong credential, so a misconfigured
    secret refuses every delivery - never read as "no secret" when it is falsy,
    never a 500 when it is not a string.
    """
    try:
        return configured_secret(value)
    except UnusableSecretError as exc:
        logger.warning(
            "Webhook delivery refused%s: %s", f" ({context})" if context else "", exc
        )
        raise HTTPException(status_code=401, detail=refusal) from None


# The static-token spellings, in precedence order: the header carries the
# assignment's secret verbatim.  ONE definition, because it feeds both the
# accept path in ``_verify_assignment_webhook_secret`` and the scrubbing
# denylist below — a future alias added to the first and forgotten in the
# second would persist the raw secret into every run's stored trigger payload,
# turning a usability fix into a credential disclosure.  ``X-OpenCTI-Token`` is
# the same family but keeps its own branch (and its own 401 message) for
# backward compatibility.
_STATIC_TOKEN_HEADERS: tuple[str, ...] = ("X-Webhook-Token", "X-Webhook-Secret")
_OPENCTI_HEADER = "X-OpenCTI-Token"
_OPENAEV_HEADER = "X-OpenAEV-Token"

# Request headers that carry credentials or replayable authentication
# material and therefore must never be persisted in a run's trigger payload:
# ``X-Webhook-Token`` / ``X-Webhook-Secret`` / ``X-OpenCTI-Token`` are the raw
# webhook secret itself, ``Authorization`` / ``Cookie`` / API-key headers are
# bearer credentials (``Authorization: Bearer <secret>`` is an accepted
# static-token scheme, so it carries the live secret too), and HMAC signature
# headers would let anyone who can read the stored run replay the same body
# to the live endpoint after the dedup TTL expires.  The set must cover every
# signature scheme this module verifies (``_verify_signature`` /
# ``_verify_hubspot_signature`` / ``_verify_linkedin_provider_signature``):
# LinkedIn, Perk, and HubSpot v1/v2 signatures are body-bound HMACs with no
# timestamp component, so a persisted value stays replayable indefinitely.
_SENSITIVE_WEBHOOK_HEADERS: frozenset[str] = frozenset(
    {header.lower() for header in (*_STATIC_TOKEN_HEADERS, _OPENCTI_HEADER, _OPENAEV_HEADER)}
    | {
        "authorization",
        "proxy-authorization",
        "cookie",
        "x-api-key",
        "x-hub-signature",
        "x-hub-signature-256",
        "x-slack-signature",
        "x-signature",
        "x-li-signature",
        "tk-signature",
        "x-hubspot-signature",
        "x-hubspot-signature-v2",
        "x-hubspot-signature-v3",
    }
)


def _safe_webhook_headers(request: Request) -> dict[str, str]:
    """Return request headers safe to persist in ``_webhook_meta``.

    The trigger payload (including ``_webhook_meta``) is stored on the
    ``AssignmentRun`` row and surfaced through the run APIs to every user who
    can view the run — a strictly wider audience than the assignment's
    managers, who are the only ones allowed to see the webhook secret
    (``_visible_trigger_config``).  Stripping credential-bearing headers here
    keeps the secret-visibility contract intact end to end.
    """
    # Compare on the lowercased name: HTTP header names are case-insensitive
    # and HTTP/1.1 servers surface them as sent (``X-Forwarded-For``), so a
    # case-sensitive prefix check would let proxy headers slip through.
    return {
        k: v
        for k, v in request.headers.items()
        if not k.lower().startswith("x-forwarded")
        and k.lower() not in _SENSITIVE_WEBHOOK_HEADERS
    }


# Assignments without a webhook secret that have already been warned about in
# this process — bounds log spam to one WARNING per assignment per process.
_secretless_assignments_warned: set[str] = set()


def _bearer_credentials(authorization: str) -> str:
    """Extract the credentials from an ``Authorization: Bearer <token>`` header.

    Returns an empty string when the header is absent or uses a different
    scheme, so a Basic/Digest/AWS-signed request falls through to the
    "unsigned delivery" rejection instead of being compared — and failing —
    against the webhook secret.  The scheme name is matched case-insensitively
    per RFC 7235 §2.1.  Only HTTP's optional whitespace (space, tab) is
    stripped: the value is latin-1-decoded wire bytes, and ``str.strip()``
    would also eat the 0x85 / 0xA0 that end the UTF-8 of a non-ASCII secret.
    """
    scheme, _, credentials = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return credentials.strip(" \t")


def _verify_assignment_webhook_secret(
    body: bytes,
    request: Request,
    webhook_secret: object,
    *,
    assignment_id: str = "",
) -> None:
    """Verify a per-assignment webhook secret or raise 401.

    Mirrors the inline signature/token validation used by the assignment
    webhook handlers (OpenCTI ``X-OpenCTI-Token`` → GitHub → Slack → generic
    ``X-Signature`` HMAC).  Used to authenticate a delivery *before* the
    idempotency cache is consulted so a replay with an invalid/absent
    signature can never read a cached response.

    When NO secret is configured the behaviour depends on the
    ``WEBHOOK_REQUIRE_SECRET`` platform setting:

    - ``False`` (default, backward compatible): the delivery is allowed but a
      WARNING is logged once per assignment per process, since the endpoint
      is then authenticated by URL path (assignment UUID) only.
    - ``True`` (strict enforcement): the delivery is rejected with 401 so a
      secretless assignment can never be triggered by an unauthenticated
      request.

    When a secret *is* configured, the request must carry a recognised
    signature/token header: an unsigned delivery (no ``X-Webhook-Token`` /
    ``X-Webhook-Secret`` / ``X-OpenCTI-Token`` / ``X-Hub-Signature-256`` /
    ``X-Slack-Signature`` / ``X-Signature`` / ``Authorization: Bearer``) is
    rejected with 401 rather than silently passing, so an unsigned replay
    cannot authenticate against the configured secret.

    Two families of scheme are accepted:

    - **Static token** — the header carries the secret verbatim and is
      compared with :func:`timing_safe_header_eq`, not :func:`hmac.compare_digest`:
      the value is attacker-controlled and Starlette decodes headers as
      latin-1, so a crafted byte >= 0x80 must yield a mismatch rather than the
      ``TypeError`` (an unauthenticated 500) ``compare_digest`` raises on a
      non-ASCII ``str`` - while a non-ASCII secret still matches whether the
      sender wrote it in latin-1 or in UTF-8.  ``X-Webhook-Token`` is the
      product-neutral spelling and ``X-Webhook-Secret`` an accepted alias of
      it; ``X-OpenCTI-Token`` is the original name kept for backward
      compatibility; ``Authorization: Bearer <secret>`` is the fallback for
      senders that cannot set a custom header.  This is what SIEM/SOAR
      platforms can actually emit — they generally cannot compute an HMAC
      over the request body from a no-code webhook action.
    - **Signature** — ``X-Hub-Signature-256`` (GitHub), ``X-Slack-Signature``
      (+ timestamp), or the generic ``X-Signature`` (hex HMAC-SHA256 of the
      raw body).  Stronger, since the secret never travels on the wire.

    Precedence is deliberate and append-only: the explicit token headers, then
    each provider signature, then the generic signature, and finally
    ``Authorization``.  ``Authorization`` is consulted last so a request that
    already carries a recognised signature *plus* an unrelated bearer token
    (an API gateway injecting its own credential, say) keeps authenticating on
    the signature exactly as it did before this scheme existed.

    ``trigger_config`` is JSON: only an absent, ``null`` or empty secret means
    none is configured, and any other value that is not a string refuses the
    delivery (:func:`configured_secret`).
    """
    secret = _usable_secret(
        webhook_secret,
        refusal="Invalid webhook signature",
        context=f"assignment={assignment_id}" if assignment_id else "",
    )
    if not secret:
        from app.core.security_options import security_option, where_to_set

        if security_option("webhook_require_secret"):
            raise HTTPException(
                status_code=401,
                detail=(
                    "This assignment webhook has no secret configured and the "
                    "platform requires webhook secrets (Settings > Access and security, "
                    "or WEBHOOK_REQUIRE_SECRET=true). Configure a webhook_secret "
                    "in the assignment trigger settings and sign requests "
                    "with it."
                ),
            )
        if assignment_id not in _secretless_assignments_warned:
            # Only cache real assignment ids: caching the empty-string
            # default would let one id-less call permanently suppress the
            # warning for every other secretless assignment in this process.
            if assignment_id:
                _secretless_assignments_warned.add(assignment_id)
            logger.warning(
                "Webhook assignment %s has no webhook_secret configured - the "
                "endpoint is authenticated by URL path only. Configure a "
                "secret in the assignment trigger settings, or %s to reject "
                "secretless deliveries.",
                assignment_id or "<unknown>",
                where_to_set("webhook_require_secret", "true"),
            )
        return
    # ``X-Webhook-Secret`` is the same static token under the name the Trigger
    # card's "Webhook secret" label leads integrators to reach for — a customer
    # wiring a SOAR playbook sent exactly that and read the resulting 401 as a
    # wrong secret rather than a wrong header.  It is an alias, not a
    # second scheme: same comparison, same precedence, and a canonical header
    # that carries a value wins over the alias.  Falling back on truthiness
    # (not on presence) is deliberate: an ``X-Webhook-Token`` a sender emits
    # empty — a SOAR template with an unset field, a gateway stripping the
    # value — must not shadow a filled ``X-Webhook-Secret`` and 401 a delivery
    # that carries the right secret.  An empty header proves nothing, so there
    # is nothing to shadow it with.
    webhook_token = ""
    for header in _STATIC_TOKEN_HEADERS:
        webhook_token = request.headers.get(header, "")
        if webhook_token:
            break
    opencti_token = request.headers.get(_OPENCTI_HEADER, "")
    gh_sig = request.headers.get("X-Hub-Signature-256", "")
    slack_sig = request.headers.get("X-Slack-Signature", "")
    slack_ts = request.headers.get("X-Slack-Request-Timestamp", "")
    if webhook_token:
        # timing_safe_header_eq, not hmac.compare_digest: the value comes
        # straight from a request header and Starlette decodes headers as
        # latin-1, so crafted bytes >= 0x80 yield a non-ASCII str that
        # compare_digest rejects with TypeError — an unauthenticated 500
        # instead of a 401.
        if not timing_safe_header_eq(webhook_token, secret):
            raise HTTPException(status_code=401, detail="Invalid webhook token")
    elif opencti_token:
        if not timing_safe_header_eq(opencti_token, secret):
            raise HTTPException(
                status_code=401, detail="Invalid OpenCTI webhook token"
            )
    elif gh_sig:
        if not verify_github_signature(body, secret, gh_sig):
            raise HTTPException(status_code=401, detail="Invalid GitHub signature")
    elif slack_sig:
        if not verify_slack_signature(body, secret, slack_sig, slack_ts):
            raise HTTPException(status_code=401, detail="Invalid Slack signature")
    else:
        generic_sig = request.headers.get("X-Signature", "")
        bearer = _bearer_credentials(request.headers.get("Authorization", ""))
        if generic_sig:
            expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
            # Header-derived value on the right — same latin-1 TypeError risk
            # as the token comparisons above.
            if not timing_safe_str_eq(generic_sig, expected):
                raise HTTPException(
                    status_code=401, detail="Invalid webhook signature"
                )
        elif bearer:
            if not timing_safe_header_eq(bearer, secret):
                raise HTTPException(
                    status_code=401, detail="Invalid webhook bearer token"
                )
        else:
            # A secret is configured but the request carries no recognised
            # signature/token header → reject the unsigned delivery.
            raise HTTPException(
                status_code=401,
                detail=(
                    "Missing webhook signature: send the assignment's secret "
                    "as X-Webhook-Token (X-Webhook-Secret is accepted too), or "
                    "as Authorization: Bearer, or sign the raw body with "
                    "X-Signature (hex HMAC-SHA256)."
                ),
            )


def _verify_opencti_token_header(
    extra_config_enc: str | None,
    request: Request,
    *,
    context: str = "",
) -> None:
    """Hold an ``X-OpenCTI-Token`` a delivery carries to the integration secret.

    ``_verify_signature`` reads the header only when no provider signature is
    present; this runs whenever the header is. Fail closed: only an
    absent or empty secret means none is configured, and a secret that is not
    a string - falsy ones included - refuses the token exactly like a wrong
    one, never accepts it unchecked. An unreadable config is refused by
    ``_read_webhook_config``, as on every webhook gate.
    """
    token = request.headers.get(_OPENCTI_HEADER, "")
    if not token or not extra_config_enc:
        return
    configured = _read_webhook_config(extra_config_enc, context=context or "opencti").get("webhook_secret")
    secret = _usable_secret(
        configured, refusal="Invalid OpenCTI webhook token", context=context
    )
    if secret and not timing_safe_header_eq(token, secret):
        raise HTTPException(status_code=401, detail="Invalid OpenCTI webhook token")


def _normalize_opencti_payload(payload: dict) -> None:
    """Inject ``_opencti_event`` metadata into an OpenCTI webhook payload (in-place)."""
    opencti_event_type = payload.get("type", payload.get("action", "unknown"))
    opencti_data = payload.get("data", {})
    entity_type = ""
    entity_name = ""
    relationship_type = ""
    if isinstance(opencti_data, dict):
        entity_type = opencti_data.get("type", opencti_data.get("entity_type", ""))
        entity_name = (
            opencti_data.get("name")
            or opencti_data.get("value")
            or opencti_data.get("observable_value")
            or opencti_data.get("id", "")
        )
        relationship_type = opencti_data.get("relationship_type", "")
    payload["_opencti_event"] = {
        "event_type": opencti_event_type,
        "entity_type": entity_type,
        "entity_name": entity_name,
        "relationship_type": relationship_type,
        "message": payload.get("message", ""),
        **_opencti_entity_facts(opencti_data),
    }


def _normalize_openaev_payload(payload: dict) -> str | None:
    """Inject ``_openaev_event`` into an OpenAEV notification delivery (in-place).

    OpenAEV's default webhook body (no template) is ``notification_name``,
    ``notification_type``, ``platform_url`` and ``content``: groups of events,
    each ``operation``, ``message``, ``resource_type`` and ``resource_id``. A
    provisioned trigger is named ``... - <event>``; that event is returned and
    recorded, ``None`` for a delivery from a trigger XTM One did not provision.
    """
    from app.services.openaev_trigger_provisioning import event_from_notification_name

    event = event_from_notification_name(payload.get("notification_name"))
    first: dict = {}
    groups = payload.get("content")
    if isinstance(groups, list):
        for group in groups:
            events = group.get("events") if isinstance(group, dict) else None
            if isinstance(events, list) and events and isinstance(events[0], dict):
                first = events[0]
                break
    platform_url = payload.get("platform_url")
    resource_id = first.get("resource_id")
    meta = {
        "event_type": event or str(first.get("operation") or "unknown").lower(),
        "operation": first.get("operation"),
        "resource_type": first.get("resource_type"),
        "resource_id": resource_id,
        "message": first.get("message", ""),
        "trigger_name": payload.get("notification_name"),
    }
    if isinstance(platform_url, str) and platform_url and isinstance(resource_id, str) and resource_id:
        meta["platform_url"] = platform_url.rstrip("/")
    payload["_openaev_event"] = {k: v for k, v in meta.items() if v not in (None, "")}
    return event


def _opencti_entity_facts(data: object) -> dict:
    """The entity a delivery is about, beyond its id (STIX id, score, labels, link).

    A provisioned notifier posts the whole STIX instance as ``data``
    (``services/opencti_trigger_provisioning.py``): the run gets the entity,
    not an id to look up. Absent fields are left out.
    """
    if not isinstance(data, dict):
        return {}
    ext = data.get("extensions")
    octi = {}
    if isinstance(ext, dict):
        octi = next(
            (v for k, v in ext.items() if isinstance(v, dict) and "opencti" in str(k)),
            {},
        )
    facts: dict = {}
    stix_id = data.get("id")
    if isinstance(stix_id, str) and stix_id:
        facts["stix_id"] = stix_id
    internal_id = octi.get("id")
    if isinstance(internal_id, str) and internal_id:
        facts["entity_id"] = internal_id
    score = data.get("x_opencti_score", octi.get("score"))
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        facts["score"] = score
    labels = data.get("labels")
    if isinstance(labels, list):
        facts["labels"] = [str(label) for label in labels if isinstance(label, (str, int))][:50]
    for key in ("created", "modified", "description"):
        value = data.get(key)
        if isinstance(value, str) and value:
            facts[key] = value[:2000] if key == "description" else value
    return facts


def _extract_platform_url(extra_config_enc: str | None, *, context: str = "") -> str:
    """Extract ``base_url`` from an integration's encrypted extra_config.

    Returns an empty string on failure (non-critical metadata).
    """
    if not extra_config_enc:
        return ""
    try:
        extra = json.loads(decrypt_value(extra_config_enc))
        return extra.get("base_url", "").rstrip("/")
    except Exception as exc:
        logger.debug(
            "Could not extract platform_url%s: %s",
            f" ({context})" if context else "",
            format_exception_message(exc),
        )
        return ""


async def _shown_platform_url(db: AsyncSession, integration: Integration, *, context: str = "") -> str:
    """The ``trigger.platform_url`` of an event from *integration*: its
    ``base_url``, on the platform's registered URL when XTM One reaches the
    platform on an internal one - the prompt builds links from it."""
    platform_url = _extract_platform_url(integration.extra_config_enc, context=context)
    if not platform_url or getattr(integration, "auth_type", None) != "platform_managed":
        return platform_url
    from app.core.platform_identity import platforms_reached_internally

    platform = (await platforms_reached_internally(db, [integration.id])).get(str(integration.id))
    return platform.shown(platform_url) if platform else platform_url


@router.get("/providers/{provider}")
async def validate_provider_webhook(provider: str, request: Request) -> dict:
    """Handle GET-based webhook validation challenges (e.g. LinkedIn).

    LinkedIn validates webhook ownership by sending a GET request with a
    ``challengeCode`` query parameter.  The response must include the
    ``challengeCode`` and an HMACSHA256 ``challengeResponse`` computed
    using the app's ``clientSecret``.  This is called during initial
    registration and every 2 hours for re-validation.
    """
    challenge_code = request.query_params.get("challengeCode", "")
    if not challenge_code:
        return {"status": "ok", "message": "No challenge code provided"}

    # Look up the client secret for this provider from OAuthDefault
    async with async_session() as db:
        od_result = await db.execute(
            select(OAuthDefault).where(OAuthDefault.provider == provider)
        )
        oauth_default = od_result.scalar_one_or_none()

        if not oauth_default or not oauth_default.client_secret_enc:
            logger.warning(
                "LinkedIn challenge for provider=%s but no OAuthDefault found",
                provider,
            )
            raise HTTPException(
                status_code=400,
                detail="No OAuth configuration found for this provider",
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
                "LinkedIn challenge refused for provider=%s: the client secret "
                "(OAuth default %s) cannot be read: %s",
                provider,
                oauth_default.id,
                format_exception_message(exc.__cause__ or exc),
            )
            raise HTTPException(
                status_code=503,
                detail="LinkedIn challenge cannot be answered right now",
            ) from exc
        challenge_response = compute_linkedin_challenge(challenge_code, client_secret)

        logger.info(
            "LinkedIn webhook challenge validated for provider=%s",
            provider,
        )
        return {
            "challengeCode": challenge_code,
            "challengeResponse": challenge_response,
        }


@router.post("/providers/{provider}")
async def receive_provider_webhook(provider: str, request: Request) -> dict:
    """Receive a webhook for an admin-provisioned provider (one URL for all users).

    Use this when the admin has pre-configured OAuth credentials for a provider
    (e.g. a single GitHub OAuth App shared by all users).  The webhook URL is
    ``/api/hooks/providers/{provider}``. Under
    ``INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION`` it reaches the assignments
    whose agent is bound to an integration running on that admin app and usable by
    the agent's owner - never one on another app - and a GitHub App
    delivery only integrations recording its installation or, recording none,
    whose own token sees it. By default it reaches every assignment it
    reached before, and the ones the app / owner / recorded-installation rule
    would leave out are logged. A delivery it cannot verify - no OAuth default, or
    one holding no webhook secret - is handed out all the same, and logged once it
    triggered assignments.
    """
    body = await request.body()
    try:
        payload = json.loads(body)
    except ValueError:
        payload = {"raw_body": body.decode("utf-8", errors="replace")}
    if not isinstance(payload, dict):
        # A JSON array or scalar gets the integration route's shape, rather
        # than an unauthenticated 500 on the first ``payload.get``.
        payload = {"raw_body": str(payload)}

    # ── Slack URL verification challenge ─────────────────────────────
    # When configuring Event Subscriptions, Slack sends a POST with
    # {"type": "url_verification", "challenge": "..."} and expects the
    # challenge value echoed back immediately.  Must run BEFORE the
    # dedup layer — Slack uses the response to validate the endpoint
    # and a cached ``__pending__`` marker would break validation.
    if payload.get("type") == "url_verification" and "challenge" in payload:
        return {"challenge": payload["challenge"]}

    # ── Authenticate BEFORE consulting the idempotency cache ──────────────
    # The dedup lookup must be bound to an authenticated request: otherwise an
    # unauthenticated replay of a captured body could read the cached response
    # (or plant a ``__pending__`` marker that suppresses a valid concurrent
    # delivery) before the signature is ever checked.  Verification is
    # re-run below (against the FRESH DB row) as defense in depth on the
    # match path.
    #
    # Routing comes from the short-TTL per-process cache: repository
    # webhook storms deliver hundreds of events that mostly match nothing, and
    # re-loading OAuthDefault + agents + assignments per delivery exhausted
    # the DB pool and shed deliveries with 429.
    routing = await _get_provider_routing(provider)
    if routing.oauth_default_exists:
        # Generic webhook_secret HMAC gate + LinkedIn fail-closed gate (a
        # configured client secret REQUIRES a valid X-LI-Signature for the
        # LinkedIn provider, so an unsigned replay can't reach the dedup
        # cache — see ``_verify_linkedin_provider_signature``).
        try:
            await _verify_provider_signatures(
                body,
                provider,
                routing.extra_config_enc,
                routing.client_secret_enc,
                request,
                routing.oauth_default_id,
            )
        except HTTPException as sig_err:
            if sig_err.status_code != 401:
                raise
            # The delivery may be signed with a freshly ROTATED secret the
            # cached snapshot doesn't know yet — force-refresh (rate-limited
            # to ~1/second/provider so invalid-signature storms can't turn
            # the cache back into a per-delivery DB load) and retry once
            # against the fresh secrets before rejecting.
            refreshed = await _refresh_provider_routing_after_auth_failure(
                provider
            )
            if refreshed is None:
                raise
            routing = refreshed
            if routing.oauth_default_exists:
                await _verify_provider_signatures(
                    body,
                    provider,
                    routing.extra_config_enc,
                    routing.client_secret_enc,
                    request,
                    routing.oauth_default_id,
                )

    # Before the dedup slot: a delivery that cannot be routed must be retried
    # or redelivered once the operator fixes it, not remembered as handled. By
    # default it is handed out as before the admin-app rule.
    if routing.admin_app_unreadable and admin_app_scoping_required():
        raise _admin_app_unreadable_error()

    is_duplicate, cached_response, dedup_key = await _claim_webhook_dedup_slot(
        request, body, scope=f"providers:{provider}"
    )
    if is_duplicate:
        logger.info(
            "Provider webhook deduplicated for provider=%s "
            "(returning cached response from prior delivery within TTL)",
            provider,
        )
        return cached_response or {
            "status": "duplicate",
            "deduplicated": True,
            "triggered": [],
            "count": 0,
        }

    # Anything past this point that returns or raises must either persist
    # the final response (success / "ignored" / event-filter skip) or
    # release the slot (exception) so a transient first-attempt failure
    # does not poison the cache and silently swallow legitimate retries
    # for the full TTL window.  See ``_finalize_dedup`` /
    # ``_release_webhook_dedup_slot``.
    try:
        event_key = _extract_event_key(request, payload)

        # ── Inject metadata ─────────────────────────────────────────────
        payload["_webhook_meta"] = {
            "provider": provider,
            "event_key": event_key,
            "scope": "provider",
            "headers": _safe_webhook_headers(request),
            "method": request.method,
        }
        # A change in exposure the CTEM loop listens to, whether or not the
        # event reaches an assignment (#826). Only a delivery verified against
        # a configured webhook secret records one: this route accepts an
        # unsigned delivery when no OAuth default or no secret is set.
        await ctem_signal_listeners.note_event(
            provider,
            event_key,
            payload,
            verified=routing.oauth_default_exists and _signed_with_webhook_secret(routing.extra_config_enc),
        )

        installation_id = _event_installation_id(provider, payload)
        if (
            installation_id is not None
            and admin_app_scoping_required()
            and github_token_installations.changes_installation_visibility(event_key)
        ):
            # Whoever sees this installation may have changed, whether or not the
            # event reaches an assignment: answers read before now no longer
            # decide for it.
            await github_token_installations.note_installation_change(installation_id)

        # The work items waiting on this pull request, whether or not the
        # event reaches an assignment.
        if routing.oauth_default_exists:
            await _route_pull_request_event(provider, event_key, payload, routing.extra_config_enc)

        # ── Fast no-match paths: ZERO DB sessions ────────────────
        if not routing.has_agents:
            logger.info(
                "Provider webhook %s (event=%s): no agents bound",
                provider,
                event_key,
            )
            return await _finalize_dedup(
                dedup_key, {"status": "ok", "triggered": [], "count": 0}
            )

        filtered: dict[str, int] = {}
        reasons: dict[str, dict[str, int]] = {}
        matched = _match_provider_assignments(
            routing, provider, event_key, installation_id, payload=payload, filtered=filtered, reasons=reasons
        )
        await _count_filtered(filtered, reasons)
        filtered = {}
        reasons = {}
        # The enrichment read once per integration and head for this delivery.
        enrichment_memo: dict = {}
        if not matched:
            logger.info(
                "Provider webhook %s (event=%s): no matching assignments",
                provider,
                event_key,
            )
            return await _finalize_dedup(
                dedup_key, {"status": "ok", "triggered": [], "count": 0}
            )

        # ── Match path: one bounded session, fresh rows ──────────────────
        triggered: list[dict] = []
        async with _WEBHOOK_DB_SEMAPHORE, async_session() as db:
            # Defense-in-depth signature re-check against the FRESH DB row
            # (the pre-claim gate above used the cached snapshot, with a
            # rate-limited refresh-and-retry for rotated secrets) — a
            # delivery still signed with the OLD secret is rejected here
            # before it can trigger work, no matter how fresh the cache is.
            od_result = await db.execute(
                select(OAuthDefault).where(OAuthDefault.provider == provider)
            )
            oauth_default = od_result.scalar_one_or_none()
            if oauth_default:
                await _verify_provider_signatures(
                    body,
                    provider,
                    oauth_default.extra_config_enc,
                    oauth_default.client_secret_enc,
                    request,
                    oauth_default.id,
                )
            unverified = provider_webhook_unverified_reason(provider, oauth_default)

            # Re-load the matched rows fresh: the cache may be up to
            # TTL seconds stale, so rows disabled, deleted or re-configured
            # since the snapshot are silently skipped and
            # ``trigger_assignment`` receives live ORM objects.
            matched_agent_ids = {safe_uuid(m.agent_id) for m in matched}
            matched_wf_ids = {safe_uuid(m.assignment_id) for m in matched}
            agent_result = await db.execute(
                select(Agent).where(
                    Agent.id.in_([a for a in matched_agent_ids if a]),
                    Agent.enabled.is_(True),
                )
            )
            agents_by_id = {str(a.id): a for a in agent_result.scalars().all()}
            wf_result = await db.execute(
                select(Assignment).where(
                    Assignment.id.in_([w for w in matched_wf_ids if w]),
                    Assignment.enabled.is_(True),
                    owner_not_deactivated(),
                )
            )
            assignments_by_id = {
                str(w.id): w for w in wf_result.scalars().all()
            }

            # The same app / owner / installation scoping, on fresh rows: a
            # client id replaced or a share withdrawn since the snapshot must
            # stop the delivery now, not a TTL later.
            # The candidates too come from the fresh rows: a pin or binding
            # removed since the snapshot must not keep its old integration.
            # By default the rule is reported, not applied: what the route reached
            # without the scoping keeps receiving the delivery, with a warning.
            strict = admin_app_scoping_required()
            fresh_integrations = await _admin_app_integrations_or_report(
                db,
                provider,
                oauth_default,
                [
                    (assignments_by_id[m.assignment_id], agents_by_id[m.agent_id])
                    for m in matched
                    if m.assignment_id in assignments_by_id
                    and m.agent_id in agents_by_id
                ],
            )
            # A GitHub App event reaches an integration recording no installation
            # only when its own token sees the installation.
            token_reach = (
                await _token_installation_reach(db, fresh_integrations, installation_id)
                if strict and installation_id is not None
                else None
            )
            unscoped = 0

            for m in matched:
                agent = agents_by_id.get(m.agent_id)
                wf = assignments_by_id.get(m.assignment_id)
                if agent is None or wf is None:
                    continue  # disabled/deleted since the cache snapshot
                # Nothing refreshes the snapshot when a trigger is edited:
                # the fresh row must still take this delivery.
                current = _assignment_matcher(
                    provider, wf.id, wf.agent_id, wf.trigger_type, wf.trigger_config
                )
                if current is None or not _matcher_admits(current, provider, event_key):
                    continue
                # The fresh row's filter too: one edited since the snapshot, and
                # one naming the ``enriched.`` facts, read now.
                if current.trigger_type == "integration_event":
                    reason = await _assignment_filter_reason(
                        db,
                        provider,
                        event_key,
                        payload,
                        event=current.event,
                        trigger_config=wf.trigger_config,
                        agent=agent,
                        memo=enrichment_memo,
                        assignment_id=m.assignment_id,
                        retry_context={"type": "webhook", "scope": "provider", "provider": provider},
                    )
                    if reason == DEFERRED_REASON:
                        continue
                    if reason is not None:
                        _note_filtered(filtered, reasons, m.assignment_id, reason)
                        continue
                if not _reaches_installation(
                    fresh_integrations.get(m.assignment_id, ()), installation_id, token_reach
                ):
                    if strict:
                        continue  # no longer an admin-app integration it may use
                    unscoped += 1

                prov_trigger_context = {
                    "type": "webhook",
                    "scope": "provider",
                    "url": str(request.url),
                    "method": request.method,
                    "provider": provider,
                    "event_key": event_key,
                    "headers": _safe_webhook_headers(request),
                    "triggered_at": datetime.now(UTC).isoformat(),
                    "body": payload,
                }
                try:
                    run_id = await trigger_assignment(
                        assignment=wf,
                        agent=agent,
                        payload=payload,
                        db=db,
                        trigger_context=prov_trigger_context,
                        skip_if_active=True,
                    )
                    if run_id is None:
                        # Busy: with INTEGRATION_EVENT_QUEUE_ENABLED on a pushed
                        # event has no poller to fetch it again, so it is queued, past
                        # the cap if need be; off, the skip stands.
                        run_id = await queue_busy_pushed_event(
                            wf, agent, payload, db=db, trigger_context=prov_trigger_context
                        )
                except AssignmentOwnerDeactivatedError:
                    # Owner deactivated since the query picked it.
                    continue
                if run_id is None:
                    # Burst guard: an active run already exists for this
                    # assignment (skip logged inside trigger_assignment).
                    continue
                triggered.append(
                    {
                        "agent": agent.name,
                        "assignment": wf.name,
                        "run_id": run_id,
                    }
                )
                logger.info(
                    "Provider webhook triggered '%s' on agent '%s': run %s",
                    wf.name,
                    agent.name,
                    run_id,
                )

            warn_unscoped_admin_app_delivery(
                f"Provider webhook {provider}",
                unscoped,
                "whose agent has no integration on the admin app its owner may use "
                "(or none recording the delivery's installation)",
            )
            if unverified:
                warn_unverified_provider_delivery(provider, unverified, len(triggered))
            if not triggered:
                logger.info(
                    "Provider webhook %s (event=%s): matched %d assignment(s) "
                    "but none triggered (rows changed since cache snapshot or "
                    "active-run burst guard)",
                    provider,
                    event_key,
                    len(matched),
                )

            await db.commit()

        await _count_filtered(filtered, reasons)
        response = {"status": "ok", "triggered": triggered, "count": len(triggered)}
        return await _finalize_dedup(dedup_key, response)
    except Exception:
        await _release_webhook_dedup_slot(dedup_key)
        raise


# ---------------------------------------------------------------------------
# Integration-level webhook (one URL per integration instance, fan-out)
# ---------------------------------------------------------------------------


def _hubspot_filter_reason(event_key: str, payload: dict, config: object) -> str | None:
    """Why a HubSpot assignment's trigger ``filter`` drops one batch event, ``None`` when it runs.

    ``scope.crm_object_type`` is applied by the caller; every other key is the
    payload filter on the event (``propertyName``, ``propertyValue``,
    ``_hubspot_event.property_name``, operators and combinators), as the other
    webhook providers are matched. A dropped event is counted on the assignment.
    """
    return _trigger_filter_reason(event_key, payload, trigger_filter(config))


async def _handle_hubspot_webhook(
    integration_id: str,
    integration: Integration,
    body: bytes,
    events: list[dict],
    request: Request,
) -> dict:
    """Handle a HubSpot webhook batch.

    HubSpot sends an **array** of event objects.  Each has a
    ``subscriptionType`` like ``contact.creation``, ``deal.propertyChange``,
    or ``company.deletion``.

    We:
      1. Verify the HubSpot signature using the integration's ``client_secret``.
      2. Find all agents bound to the integration.
      3. For each event in the batch, normalise the event key and trigger all
         matching ``integration_event`` assignments.
    """
    provider = integration.provider

    # ── 1. Signature validation (uses client_secret, not webhook_secret) ──
    # An unreadable config refuses the delivery; an error raised while
    # verifying propagates instead of skipping the check.
    secret = _hubspot_secret(
        integration.extra_config_enc, context=f"integration={integration.id}"
    )
    if secret:
        _verify_hubspot_signature(body, secret, request)

    async with async_session() as db:
        # ── 2. Find agents bound to this integration ─────────────────────
        agents = await _agents_bound_to_integration(
            db, provider, str(integration.id), route=f"HubSpot webhook {integration_id}"
        )
        if not agents:
            logger.info("HubSpot webhook %s: no agents bound", integration_id)
            return {"status": "ok", "triggered": [], "count": 0}

        # Collect all assignments once per agent
        agent_assignments: list[tuple] = []
        for agent in agents:
            wf_result = await db.execute(
                select(Assignment).where(
                    Assignment.agent_id == agent.id,
                    Assignment.trigger_type == "integration_event",
                    Assignment.enabled.is_(True),
                    owner_not_deactivated(),
                )
            )
            for wf in wf_result.scalars().all():
                config = wf.trigger_config or {}
                if config.get("provider") == provider:
                    agent_assignments.append((agent, wf, config))

        if not agent_assignments:
            logger.info("HubSpot webhook %s: no matching assignments", integration_id)
            return {"status": "ok", "triggered": [], "count": 0}

        # ── 3. Process each event in the batch ───────────────────────────
        triggered: list[dict] = []
        filtered: dict[str, int] = {}
        reasons: dict[str, dict[str, int]] = {}
        now_iso = datetime.now(UTC).isoformat()

        for evt in events:
            subscription_type = evt.get("subscriptionType", "")
            event_key = _normalize_hubspot_event(subscription_type)
            object_type = _hubspot_object_type(subscription_type)
            object_id = str(evt.get("objectId", ""))

            # Build normalised payload for this event
            payload: dict = {
                "_hubspot_event": {
                    "event_type": event_key,
                    "subscription_type": subscription_type,
                    "object_type": object_type,
                    "object_id": object_id,
                    "property_name": evt.get("propertyName", ""),
                    "property_value": evt.get("propertyValue", ""),
                    "change_source": evt.get("changeSource", ""),
                    "portal_id": str(evt.get("portalId", "")),
                    "occurred_at": evt.get("occurredAt"),
                },
                "_webhook_meta": {
                    "integration_id": integration_id,
                    "provider": provider,
                    "event_key": event_key,
                    "headers": _safe_webhook_headers(request),
                    "method": request.method,
                },
                # Keep the raw event for full context
                **evt,
            }

            for agent, wf, config in agent_assignments:
                exact = uses_exact_event_matching(config)
                if not _event_selected(
                    event_key, config.get("event"), exact=exact, assignment_id=wf.id
                ):
                    continue

                # Check events filter list too (if set)
                if not _event_key_allowed(
                    event_key, config.get("events"), exact=exact, assignment_id=wf.id
                ):
                    continue

                # Entity-type (CRM object) filter — scope the trigger to specific
                # HubSpot object types when configured (empty = every type).
                allowed_object_types = (
                    (config.get("filter") or {}).get("scope") or {}
                ).get("crm_object_type") or []
                if allowed_object_types and object_type not in allowed_object_types:
                    continue
                # The rest of the filter is matched on the event itself
                # (``propertyName``, ``_hubspot_event.property_value``, operators).
                reason = _hubspot_filter_reason(event_key, payload, config)
                if reason is not None:
                    _note_filtered(filtered, reasons, str(wf.id), reason)
                    continue

                trigger_context = {
                    "type": "webhook",
                    "scope": "integration",
                    "url": str(request.url),
                    "method": request.method,
                    "provider": provider,
                    "event_key": event_key,
                    "integration_id": integration_id,
                    "headers": _safe_webhook_headers(request),
                    "triggered_at": now_iso,
                    "body": payload,
                }
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
                        # Busy: with INTEGRATION_EVENT_QUEUE_ENABLED on a pushed
                        # event has no poller to fetch it again, so it is queued, past
                        # the cap if need be; off, the skip stands.
                        run_id = await queue_busy_pushed_event(
                            wf, agent, payload, db=db, trigger_context=trigger_context
                        )
                except AssignmentOwnerDeactivatedError:
                    # Owner deactivated since the query picked it.
                    continue
                if run_id is None:
                    # Burst guard: an active run already exists for this
                    # assignment (skip logged inside trigger_assignment).
                    continue
                triggered.append(
                    {
                        "agent": agent.name,
                        "assignment": wf.name,
                        "run_id": run_id,
                        "event_key": event_key,
                        "object_type": object_type,
                        "object_id": object_id,
                    }
                )
                logger.info(
                    "HubSpot webhook triggered '%s' on agent '%s' (event=%s, object=%s/%s): run %s",
                    wf.name,
                    agent.name,
                    event_key,
                    object_type,
                    object_id,
                    run_id,
                )

        if not triggered:
            logger.info(
                "HubSpot webhook %s: no events matched any assignment", integration_id
            )

        await db.commit()
        await _count_filtered(filtered, reasons)

    return {"status": "ok", "triggered": triggered, "count": len(triggered)}


async def _handle_jira_webhook(integration_id: str, payload: object, request: Request) -> dict:
    """A Jira dynamic webhook delivery on the integration's webhook URL.

    Refused (401) unless ``Authorization: Bearer <JWT>`` is signed (HS256) with
    the integration's OAuth client secret: no secret refuses every delivery.
    Then ``jira_webhooks.dispatch_delivery``.
    """
    from app.services import jira_webhooks
    from app.services.vault import reveal_field

    async with async_session() as db:
        integration = await db.get(Integration, _uuid_mod.UUID(str(integration_id)))
        try:
            secret = (await reveal_field(integration, "client_secret_enc")) if integration is not None else ""
        except Exception:
            secret = ""
    if not jira_webhooks.verify_delivery(request.headers.get("authorization"), secret or ""):
        raise HTTPException(status_code=401, detail="Missing or invalid Jira webhook token")
    return await jira_webhooks.dispatch_delivery(integration_id, payload)


async def _handle_docusign_connect(
    integration_id: str,
    extra_config_enc: str | None,
    body: bytes,
    payload: object,
    request: Request,
) -> dict:
    """A DocuSign Connect delivery on the integration's webhook URL.

    Refused (401) unless one ``X-DocuSign-Signature-<n>`` signs the raw body
    under one of the integration's Connect HMAC keys: no key configured refuses
    every delivery. Then ``docusign_connect.dispatch_connect_delivery``.
    """
    from app.services import docusign_connect

    context = f"integration={integration_id}"
    extra = _read_webhook_config(extra_config_enc, context=context)
    keys = docusign_connect.connect_hmac_keys(extra)
    if not keys:
        logger.warning("DocuSign Connect delivery refused (%s): no Connect HMAC key configured", context)
        raise HTTPException(status_code=401, detail="Missing or invalid DocuSign Connect signature")
    if not docusign_connect.verify_connect_signature(body, keys, request.headers):
        raise HTTPException(status_code=401, detail="Missing or invalid DocuSign Connect signature")
    return await docusign_connect.dispatch_connect_delivery(integration_id, extra, payload)


def _verify_rydoo_webhook_token(extra_config_enc: str | None, request: Request, *, context: str) -> None:
    """Refuse (401) a Rydoo delivery that does not carry the integration's webhook token.

    Rydoo neither signs its expense webhook nor sets a custom header, so the
    URL given to Rydoo carries the integration's ``webhook_token`` as
    ``?token=`` (``X-Webhook-Token`` is accepted too). No token configured
    refuses every delivery: the integration URL is never left open.
    """
    extra = _read_webhook_config(extra_config_enc, context=context) if extra_config_enc else {}
    configured = _usable_secret(
        extra.get(RYDOO_TOKEN_FIELD), refusal="Missing or invalid Rydoo webhook token", context=context
    )
    if not configured:
        logger.warning("Rydoo webhook delivery refused (%s): no webhook token configured", context)
        raise HTTPException(status_code=401, detail="Missing or invalid Rydoo webhook token")
    presented = str(request.query_params.get(RYDOO_TOKEN_PARAM) or "") or request.headers.get(
        _STATIC_TOKEN_HEADERS[0], ""
    )
    if not rydoo_token_matches(presented, configured):
        raise HTTPException(status_code=401, detail="Missing or invalid Rydoo webhook token")


async def _handle_dealhub_webhook(
    integration_id: str,
    extra_config_enc: str | None,
    payload: object,
    request: Request,
) -> dict[str, object]:
    """A DealHub quote webhook delivery on the integration's webhook URL.

    Refused (401) unless ``Authorization: Bearer <token>`` names one of the
    integration's webhook tokens (DealHub's Token authentication type): no
    token configured refuses every delivery. Then
    ``dealhub_events.dispatch_delivery``.
    """
    from app.services import dealhub_events

    context = f"integration={integration_id}"
    extra = _read_webhook_config(extra_config_enc, context=context)
    tokens = dealhub_events.webhook_tokens(extra)
    if not tokens:
        logger.warning("DealHub webhook delivery refused (%s): no webhook token configured", context)
        raise HTTPException(status_code=401, detail="Missing or invalid DealHub webhook token")
    if not dealhub_events.verify_bearer(request.headers.get("authorization"), tokens):
        raise HTTPException(status_code=401, detail="Missing or invalid DealHub webhook token")
    return await dealhub_events.dispatch_delivery(integration_id, payload)


@router.post("/integrations/{integration_id}")
async def receive_integration_webhook(integration_id: str, request: Request) -> dict:
    """Receive a webhook for an *integration* and fan out to all bound agents.

    Flow:
      1. Look up the integration → get ``provider``.
      2. Parse and normalise the event key from headers / payload.
      3. Optional: verify signature using the integration's webhook secret
         (stored in ``extra_config_enc`` as ``webhook_secret``).
      4. Find all **enabled** agents whose ``integration_bindings`` JSON
         contains this integration (matched by provider → integration-id).
      5. For each agent, find ``integration_event`` assignments whose
         ``trigger_config.provider`` matches and (optionally) whose
         ``trigger_config.event`` matches the event key.
      6. Trigger every matching assignment.

    **HubSpot special handling**: HubSpot sends event batches (JSON arrays).
    When detected, the batch is split into individual events and each is
    dispatched separately with normalised ``_hubspot_event`` metadata.
    """
    body = await request.body()
    try:
        payload_raw = json.loads(body)
    except ValueError:
        payload_raw = {"raw_body": body.decode("utf-8", errors="replace")}

    # ── Slack URL verification challenge ─────────────────────────────
    # Handled BEFORE the dedup layer so retried challenges always echo a
    # fresh ``{"challenge": ...}`` answer.  Slack uses the response to
    # *validate* the endpoint and we don't want a cached ``__pending__``
    # marker to ever block validation.
    if (
        isinstance(payload_raw, dict)
        and payload_raw.get("type") == "url_verification"
        and "challenge" in payload_raw
    ):
        return {"challenge": payload_raw["challenge"]}

    if not is_valid_uuid(integration_id):
        return {"status": "ignored", "reason": "invalid integration id"}

    # ── Authenticate BEFORE consulting the idempotency cache (see provider
    # handler for the rationale).  Verification is re-run below as defense
    # in depth.
    async with async_session() as db:
        integ_pre = (
            await db.execute(
                select(Integration)
                .options(
                    load_only(Integration.provider, Integration.extra_config_enc)
                )
                .where(Integration.id == integration_id)
            )
        ).scalar_one_or_none()
    if integ_pre and integ_pre.provider == "docusign":
        # DocuSign Connect: verified with the Connect HMAC keys, then
        # deduplicated per envelope event in the seen set the poll shares.
        return await _handle_docusign_connect(integration_id, integ_pre.extra_config_enc, body, payload_raw, request)
    if integ_pre and integ_pre.provider == "dealhub":
        # DealHub quote webhook: its Token authentication (a bearer token stored
        # on the integration), then deduplicated per quote change in the seen set
        # the poll shares.
        return await _handle_dealhub_webhook(integration_id, integ_pre.extra_config_enc, payload_raw, request)
    if integ_pre and integ_pre.provider == "jira":
        # Jira dynamic webhook: a bearer JWT signed with the OAuth app's client
        # secret, then deduplicated per change in the seen set the poll shares.
        return await _handle_jira_webhook(integration_id, payload_raw, request)
    if integ_pre and integ_pre.provider == "wordsmith":
        # Wordsmith webhook endpoint: Wordsmith-Signature with the secret of the
        # endpoint Wordsmith-Webhook-Id names, then deduplicated per event id;
        # a routed question's callback: the secret kept under its token.
        from app.services import wordsmith_webhooks

        return await wordsmith_webhooks.dispatch_delivery(
            integration_id,
            request.headers,
            body,
            payload_raw,
            callback_token=str(request.query_params.get(wordsmith_webhooks.CALLBACK_PARAM) or ""),
        )
    if integ_pre and integ_pre.provider == "rydoo":
        # Rydoo expense webhook: unsigned, so the integration's webhook token
        # in the URL gates it before the dedup cache; refused without one.
        _verify_rydoo_webhook_token(integ_pre.extra_config_enc, request, context=f"integration={integration_id}")
    if integ_pre and integ_pre.extra_config_enc:
        # HubSpot signs every delivery with the app client secret, so the
        # signature must be verified for the configured provider regardless of
        # payload shape. Gating on ``isinstance(payload, list)`` let a non-list
        # (malformed / crafted) HubSpot body skip the HubSpot check and fall
        # through to the ``webhook_secret``-only path, reaching the dedup cache
        # unauthenticated even with a signing secret configured.
        if integ_pre.provider == "hubspot":
            hs_secret = _hubspot_secret(
                integ_pre.extra_config_enc, context=f"integration={integration_id}"
            )
            if hs_secret:
                _verify_hubspot_signature(body, hs_secret, request)
        else:
            _try_verify_webhook_signature(
                body,
                integ_pre.extra_config_enc,
                request,
                context=f"integration={integration_id}",
            )

    is_duplicate, cached_response, dedup_key = await _claim_webhook_dedup_slot(
        request, body, scope=f"integrations:{integration_id}"
    )
    if is_duplicate:
        logger.info(
            "Integration webhook deduplicated for integration=%s "
            "(returning cached response from prior delivery within TTL)",
            integration_id,
        )
        return cached_response or {
            "status": "duplicate",
            "deduplicated": True,
            "triggered": [],
            "count": 0,
        }

    # All return / raise paths below must either persist the final response
    # (``_finalize_dedup``) or release the slot (``_release_webhook_dedup_slot``)
    # so a transient first-attempt failure cannot poison the cache for the
    # full TTL window and silently swallow legitimate retries.
    try:
        async with async_session() as db:
            # ── 1. Resolve integration ──────────────────────────────────────
            result = await db.execute(
                select(Integration)
                .options(
                    load_only(
                        Integration.id,
                        Integration.provider,
                        Integration.extra_config_enc,
                        # _shown_platform_url reads it once this session is closed.
                        Integration.auth_type,
                    )
                )
                .where(Integration.id == integration_id)
            )
            integration = result.scalar_one_or_none()
            if not integration:
                logger.warning(
                    "Integration webhook for unknown integration: %s", integration_id
                )
                return await _finalize_dedup(
                    dedup_key,
                    {"status": "ignored", "reason": "integration not found"},
                )

            provider = integration.provider

        # ── HubSpot batch handling ───────────────────────────────────────
        # HubSpot sends an array of events.  Delegate to the dedicated handler.
        if provider == "hubspot" and isinstance(payload_raw, list):
            hs_response = await _handle_hubspot_webhook(
                integration_id,
                integration,
                body,
                payload_raw,
                request,
            )
            return await _finalize_dedup(dedup_key, hs_response)

        # ── Generic single-payload flow ──────────────────────────────────
        payload = (
            payload_raw
            if isinstance(payload_raw, dict)
            else {"raw_body": str(payload_raw)}
        )

        async with async_session() as db:
            # ── 2. Event key ────────────────────────────────────────────────
            event_key = _extract_event_key(request, payload)

            # ── 3. Signature validation (optional) ──────────────────────────
            _try_verify_webhook_signature(
                body,
                integration.extra_config_enc,
                request,
                context=f"integration={integration_id}",
            )

            # The work items waiting on this pull request, whether or not the
            # event reaches an assignment.
            await _route_pull_request_event(
                provider, event_key, payload, integration.extra_config_enc
            )

            # ── 4. Inject metadata into payload ─────────────────────────────
            payload["_webhook_meta"] = {
                "integration_id": integration_id,
                "provider": provider,
                "event_key": event_key,
                "headers": _safe_webhook_headers(request),
                "method": request.method,
            }
            # A change in exposure the CTEM loop listens to (#826), from a
            # delivery verified against a configured webhook secret only.
            await ctem_signal_listeners.note_event(
                provider,
                event_key,
                payload,
                verified=_signed_with_webhook_secret(integration.extra_config_enc),
            )

            # ── 5. Find agents bound to this integration ───────────────────
            agents = await _agents_bound_to_integration(
                db, provider, str(integration.id), route=f"Integration webhook {integration_id}"
            )

            if not agents:
                logger.info(
                    "Integration webhook %s (%s): no agents bound",
                    integration_id,
                    provider,
                )
                return await _finalize_dedup(
                    dedup_key, {"status": "ok", "triggered": [], "count": 0}
                )

            # ── 6. Fan out to matching assignments ────────────────────────────
            triggered: list[dict] = []

            # Loop-invariant GitHub self-echo lookup (loop protection): the event
            # target comes from this single payload and the integration id is
            # fixed for the request, so resolve the target once and memoize the
            # Redis check. A fan-out across many assignments bound to the same
            # integration then issues at most one ``was_self_action`` query
            # instead of one per assignment. See services/github_echo.py.
            _gh_target: tuple[str, int] | None = None
            _gh_self_echo_memo: list[bool] = []
            if provider == "github":
                from app.services.github_echo import extract_github_target

                _gh_target = extract_github_target(payload)

            async def _github_event_is_self_echo() -> bool:
                if not _gh_target:
                    return False
                if not _gh_self_echo_memo:
                    from app.services.github_echo import was_self_action

                    _gh_self_echo_memo.append(
                        await was_self_action(
                            integration_id, _gh_target[0], _gh_target[1]
                        )
                    )
                return _gh_self_echo_memo[0]

            # Deliveries each assignment's trigger filter dropped, counted at the end.
            filtered: dict[str, int] = {}
            reasons: dict[str, dict[str, int]] = {}
            enrichment_memo: dict = {}

            # Loop-invariant too: resolved once, on the first assignment the
            # event triggers, then reused by every trigger context.
            _platform_url_memo: list[str] = []

            async def _delivery_platform_url() -> str:
                if not _platform_url_memo:
                    _platform_url_memo.append(
                        await _shown_platform_url(
                            db,
                            integration,
                            context=f"integration={integration_id}",
                        )
                    )
                return _platform_url_memo[0]

            for agent in agents:
                wf_result = await db.execute(
                    select(Assignment).where(
                        Assignment.agent_id == agent.id,
                        Assignment.trigger_type.in_(["integration_event", "webhook"]),
                        Assignment.enabled.is_(True),
                        owner_not_deactivated(),
                    )
                )
                assignments = wf_result.scalars().all()

                for wf in assignments:
                    config = wf.trigger_config or {}

                    if wf.trigger_type == "integration_event":
                        # Match by provider
                        if config.get("provider") != provider:
                            continue
                        # Match by event (if specified)
                        if not _event_selected(
                            event_key,
                            config.get("event"),
                            exact=uses_exact_event_matching(config),
                            assignment_id=wf.id,
                        ):
                            continue
                        # The trigger's filter: no run, no gate, counted.
                        reason = await _assignment_filter_reason(
                            db,
                            provider,
                            event_key,
                            payload,
                            event=config.get("event"),
                            trigger_config=config,
                            agent=agent,
                            receiver_integration_id=str(integration_id),
                            memo=enrichment_memo,
                            assignment_id=str(wf.id),
                            retry_context={
                                "type": "webhook",
                                "scope": "integration",
                                "provider": provider,
                                "integration_id": str(integration_id),
                            },
                        )
                        if reason == DEFERRED_REASON:
                            continue
                        if reason is not None:
                            logger.debug(
                                "Integration webhook %s: event %s filtered out for assignment %s (%s)",
                                integration_id,
                                event_key,
                                wf.id,
                                reason,
                            )
                            _note_filtered(filtered, reasons, str(wf.id), reason)
                            continue

                    elif wf.trigger_type == "webhook":
                        # Legacy: match by path == provider name
                        wf_path = config.get("path", "")
                        if wf_path != provider:
                            continue
                        # Event filter
                        if not _event_key_allowed(
                            event_key,
                            config.get("events"),
                            exact=uses_exact_event_matching(config),
                            assignment_id=wf.id,
                        ):
                            continue

                    # Self-triggered event suppression (loop protection): drop
                    # GitHub events the agent itself caused when the assignment
                    # opted in. See the per-assignment handler for rationale.
                    # The target + Redis lookup are memoized above, so this is at
                    # most one ``was_self_action`` query for the whole fan-out.
                    if (
                        provider == "github"
                        and getattr(wf, "ignore_self_triggered_events", False)
                        and await _github_event_is_self_echo()
                    ):
                        logger.info(
                            "Integration webhook: event %s on %s#%s is the "
                            "agent's own echo — skipped for assignment '%s' "
                            "(ignore_self_triggered_events)",
                            event_key,
                            _gh_target[0],
                            _gh_target[1],
                            wf.name,
                        )
                        continue

                    platform_url = await _delivery_platform_url()

                    wh_trigger_context = {
                        "type": "webhook",
                        "scope": "integration",
                        # A token the URL carries (Rydoo's ?token=) never reaches the run.
                        "url": redact_secret_text(str(request.url)),
                        "method": request.method,
                        "provider": provider,
                        "event_key": event_key,
                        "integration_id": integration_id,
                        "platform_url": platform_url,
                        "headers": _safe_webhook_headers(request),
                        "triggered_at": datetime.now(UTC).isoformat(),
                        "body": payload,
                    }
                    try:
                        run_id = await trigger_assignment(
                            assignment=wf,
                            agent=agent,
                            payload=payload,
                            db=db,
                            trigger_context=wh_trigger_context,
                            skip_if_active=True,
                        )
                        if run_id is None:
                            # Busy: with INTEGRATION_EVENT_QUEUE_ENABLED on a pushed
                            # event has no poller to fetch it again, so it is queued, past
                            # the cap if need be; off, the skip stands.
                            run_id = await queue_busy_pushed_event(
                                wf, agent, payload, db=db, trigger_context=wh_trigger_context
                            )
                    except AssignmentOwnerDeactivatedError:
                        # Owner deactivated since the query picked it.
                        continue
                    if run_id is None:
                        # Burst guard: an active run already exists for this
                        # assignment (skip logged inside trigger_assignment).
                        continue
                    triggered.append(
                        {
                            "agent": agent.name,
                            "assignment": wf.name,
                            "run_id": run_id,
                        }
                    )
                    logger.info(
                        "Integration webhook triggered '%s' on agent '%s': run %s",
                        wf.name,
                        agent.name,
                        run_id,
                    )

            if not triggered:
                logger.info(
                    "Integration webhook %s (%s, event=%s): no matching assignments",
                    integration_id,
                    provider,
                    event_key,
                )

            await db.commit()

        await _count_filtered(filtered, reasons)
        response = {"status": "ok", "triggered": triggered, "count": len(triggered)}
        return await _finalize_dedup(dedup_key, response)
    except Exception:
        await _release_webhook_dedup_slot(dedup_key)
        raise


# ---------------------------------------------------------------------------
# Per-assignment webhook (for providers that support dedicated assignment URLs)
# ---------------------------------------------------------------------------
# Providers like OpenCTI, GitHub, HubSpot, and Perk allow configuring as many
# webhooks as needed.  This endpoint targets a single assignment directly,
# instead of fanning out.
# URL pattern: POST /api/hooks/integrations/{integration_id}/assignments/{assignment_id}


async def _handle_hubspot_assignment_webhook(
    integration_id: str,
    integration: Integration,
    body: bytes,
    events: list[dict],
    assignment_id: str,
    request: Request,
) -> dict:
    """Handle a HubSpot webhook batch targeting a *specific* assignment.

    Same normalisation as ``_handle_hubspot_webhook`` but triggers only the
    specified assignment instead of fanning out to all matching assignments.
    """
    provider = integration.provider

    # ── 1. Signature validation ──────────────────────────────────────
    # Same gate as ``_handle_hubspot_webhook``.
    secret = _hubspot_secret(
        integration.extra_config_enc, context=f"integration={integration.id} assignment={assignment_id}"
    )
    if secret:
        _verify_hubspot_signature(body, secret, request)

    async with async_session() as db:
        # ── 2. Resolve the specific assignment ─────────────────────────────
        wf_result = await db.execute(
            select(Assignment).where(
                Assignment.id == assignment_id,
                Assignment.enabled.is_(True),
            )
        )
        wf = wf_result.scalar_one_or_none()
        if not wf:
            logger.warning(
                "HubSpot per-assignment webhook: assignment %s not found or disabled",
                assignment_id,
            )
            return {"status": "ignored", "reason": "assignment not found or disabled"}
        if await is_paused_by_deactivated_owner(db, wf):
            logger.info(
                "HubSpot per-assignment webhook: assignment %s ignored, its "
                "owner is deactivated",
                assignment_id,
            )
            return {"status": "ignored", "reason": _OWNER_DEACTIVATED_REASON}

        config = wf.trigger_config or {}
        if wf.trigger_type == "integration_event":
            if config.get("provider") != provider:
                return {"status": "ignored", "reason": "provider mismatch"}
        elif wf.trigger_type != "webhook":
            return {"status": "ignored", "reason": "trigger type mismatch"}

        # ── 3. Resolve agent ─────────────────────────────────────────────
        agent_result = await db.execute(select(Agent).where(Agent.id == wf.agent_id))
        agent = agent_result.scalar_one_or_none()
        if not agent or not agent.enabled:
            return {"status": "ignored", "reason": "agent not found or disabled"}

        # ── 4. Process each event in the batch ───────────────────────────
        triggered: list[dict] = []
        now_iso = datetime.now(UTC).isoformat()
        wf_event = config.get("event")
        events_filter = config.get("events")
        exact = uses_exact_event_matching(config)
        # Entity-type (CRM object) filter — scope the trigger to specific HubSpot
        # object types when configured (empty = every type).
        allowed_object_types = (
            (config.get("filter") or {}).get("scope") or {}
        ).get("crm_object_type") or []
        filtered: dict[str, int] = {}
        reasons: dict[str, dict[str, int]] = {}

        for evt in events:
            subscription_type = evt.get("subscriptionType", "")
            event_key = _normalize_hubspot_event(subscription_type)
            object_type = _hubspot_object_type(subscription_type)
            object_id = str(evt.get("objectId", ""))

            # Event filter
            if not _event_selected(
                event_key, wf_event, exact=exact, assignment_id=wf.id
            ):
                continue
            if not _event_key_allowed(
                event_key, events_filter, exact=exact, assignment_id=wf.id
            ):
                continue
            if allowed_object_types and object_type not in allowed_object_types:
                continue

            payload: dict = {
                "_hubspot_event": {
                    "event_type": event_key,
                    "subscription_type": subscription_type,
                    "object_type": object_type,
                    "object_id": object_id,
                    "property_name": evt.get("propertyName", ""),
                    "property_value": evt.get("propertyValue", ""),
                    "change_source": evt.get("changeSource", ""),
                    "portal_id": str(evt.get("portalId", "")),
                    "occurred_at": evt.get("occurredAt"),
                },
                "_webhook_meta": {
                    "integration_id": integration_id,
                    "assignment_id": assignment_id,
                    "provider": provider,
                    "event_key": event_key,
                    "scope": "assignment",
                    "headers": _safe_webhook_headers(request),
                    "method": request.method,
                },
                **evt,
            }
            reason = _hubspot_filter_reason(event_key, payload, config)
            if reason is not None:
                _note_filtered(filtered, reasons, str(wf.id), reason)
                continue

            trigger_context = {
                "type": "webhook",
                "scope": "assignment",
                "url": str(request.url),
                "method": request.method,
                "provider": provider,
                "event_key": event_key,
                "integration_id": integration_id,
                "assignment_id": assignment_id,
                "headers": _safe_webhook_headers(request),
                "triggered_at": now_iso,
                "body": payload,
            }
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
                    # Busy: with INTEGRATION_EVENT_QUEUE_ENABLED on a pushed
                    # event has no poller to fetch it again, so it is queued, past
                    # the cap if need be; off, the skip stands.
                    run_id = await queue_busy_pushed_event(
                        wf, agent, payload, db=db, trigger_context=trigger_context
                    )
            except AssignmentOwnerDeactivatedError:
                # Owner deactivated since the check above: the rest of
                # the batch is paused. With nothing started yet this is the
                # check's own uncached answer; otherwise the started runs are
                # reported.
                if not triggered:
                    return {"status": "ignored", "reason": _OWNER_DEACTIVATED_REASON}
                break
            if run_id is None:
                # Burst guard: an active run already exists for this
                # assignment (skip logged inside trigger_assignment).
                continue
            triggered.append(
                {
                    "agent": agent.name,
                    "assignment": wf.name,
                    "run_id": run_id,
                    "event_key": event_key,
                    "object_type": object_type,
                    "object_id": object_id,
                }
            )
            logger.info(
                "HubSpot per-assignment webhook triggered '%s' on agent '%s' (event=%s, object=%s/%s): run %s",
                wf.name,
                agent.name,
                event_key,
                object_type,
                object_id,
                run_id,
            )

        if not triggered:
            logger.info(
                "HubSpot per-assignment webhook %s/%s: no events matched",
                integration_id,
                assignment_id,
            )

        await db.commit()
        await _count_filtered(filtered, reasons)

    return {"status": "ok", "triggered": triggered, "count": len(triggered)}


@router.post("/integrations/{integration_id}/assignments/{assignment_id}")
async def receive_assignment_webhook(
    integration_id: str,
    assignment_id: str,
    request: Request,
) -> dict:
    """Receive a webhook that targets a *specific* assignment.

    Unlike the integration-level endpoint which fans out to all matching
    assignments, this endpoint triggers **only** the specified assignment.
    The integration is still validated (provider, signature, etc.) and the
    event key is extracted the same way.

    If the generic integration-level URL also receives the same event it
    will trigger matching assignments too — both paths are valid.

    **HubSpot special handling**: HubSpot sends event batches (JSON arrays).
    When detected, the batch is split into individual events and each is
    dispatched separately with normalised ``_hubspot_event`` metadata.
    """
    assignment_uuid = safe_uuid(assignment_id)
    if not is_valid_uuid(integration_id) or assignment_uuid is None:
        return {"status": "ignored", "reason": "invalid integration or assignment id"}

    # Per-assignment throttle — raises 429 when the sender floods this URL.
    # The bucket uses the canonical UUID form: ``uuid.UUID()`` accepts many
    # representations (uppercase, braces, no-hyphen hex) that all reach the
    # same assignment downstream, so the raw string would let a sender evade
    # the throttle by varying the formatting of the same ID.
    await enforce_rate_limit(
        request,
        "webhook-assignment",
        limit=_webhook_requests_per_minute(),
        window_seconds=60,
        bucket=str(assignment_uuid),
    )

    body = await request.body()

    # ── Authenticate BEFORE consulting the idempotency cache (see provider
    # handler for the rationale).  Verification is re-run below as defense
    # in depth.
    async with async_session() as db:
        integ_pre = (
            await db.execute(
                select(Integration)
                .options(
                    load_only(Integration.provider, Integration.extra_config_enc)
                )
                .where(Integration.id == integration_id)
            )
        ).scalar_one_or_none()
    if integ_pre and integ_pre.extra_config_enc:
        # HubSpot signs every delivery with the app client secret, so verify
        # for the configured provider regardless of payload shape (a non-list
        # crafted body must not skip the HubSpot check and fall through to the
        # ``webhook_secret``-only path). See the integration-level handler.
        if integ_pre.provider == "hubspot":
            hs_secret = _hubspot_secret(
                integ_pre.extra_config_enc, context=f"integration={integration_id} assignment={assignment_id}"
            )
            if hs_secret:
                _verify_hubspot_signature(body, hs_secret, request)
        else:
            _try_verify_webhook_signature(
                body,
                integ_pre.extra_config_enc,
                request,
                context=f"integration={integration_id} assignment={assignment_id}",
            )
            _verify_opencti_token_header(
                integ_pre.extra_config_enc,
                request,
                context=f"integration={integration_id} assignment={assignment_id}",
            )

    is_duplicate, cached_response, dedup_key = await _claim_webhook_dedup_slot(
        request,
        body,
        scope=f"integrations:{integration_id}:assignments:{assignment_id}",
    )
    if is_duplicate:
        logger.info(
            "Per-assignment webhook deduplicated for integration=%s assignment=%s "
            "(returning cached response from prior delivery within TTL)",
            integration_id,
            assignment_id,
        )
        return cached_response or {
            "status": "duplicate",
            "deduplicated": True,
            "triggered": [],
            "count": 0,
        }

    # All return / raise paths below must persist (`_finalize_dedup`) or
    # release (`_release_webhook_dedup_slot`) the dedup slot, otherwise a
    # transient first-attempt failure (signature mismatch, DB blip,
    # crash) would silently swallow legitimate retries within the TTL
    # with a generic "duplicate" reply.
    try:
        try:
            payload_raw = json.loads(body)
        except ValueError:
            payload_raw = {"raw_body": body.decode("utf-8", errors="replace")}

        async with async_session() as db:
            # ── 1. Resolve integration ──────────────────────────────────────
            result = await db.execute(
                select(Integration)
                .options(
                    load_only(
                        Integration.id,
                        Integration.provider,
                        Integration.extra_config_enc,
                        # _shown_platform_url reads it once this session is closed.
                        Integration.auth_type,
                    )
                )
                .where(Integration.id == integration_id)
            )
            integration = result.scalar_one_or_none()
            if not integration:
                logger.warning(
                    "Per-assignment webhook for unknown integration: %s",
                    integration_id,
                )
                return await _finalize_dedup(
                    dedup_key,
                    {"status": "ignored", "reason": "integration not found"},
                )

            provider = integration.provider

        # ── HubSpot batch handling ───────────────────────────────────────
        if provider == "hubspot" and isinstance(payload_raw, list):
            hs_response = await _handle_hubspot_assignment_webhook(
                integration_id,
                integration,
                body,
                payload_raw,
                assignment_id,
                request,
            )
            if hs_response.get("reason") == _OWNER_DEACTIVATED_REASON:
                return await _owner_paused_answer(dedup_key)
            return await _finalize_dedup(dedup_key, hs_response)

        # For non-batch payloads, continue with the single-event flow
        payload = (
            payload_raw
            if isinstance(payload_raw, dict)
            else {"raw_body": str(payload_raw)}
        )

        async with async_session() as db:
            # ── 2. Event key ────────────────────────────────────────────────
            event_key = _extract_event_key(request, payload)

            # ── 3. Signature / token validation (optional) ──────────────────
            _try_verify_webhook_signature(
                body,
                integration.extra_config_enc,
                request,
                context=f"integration={integration_id} assignment={assignment_id}",
            )

            # Also check OpenCTI token header against integration secret
            _verify_opencti_token_header(
                integration.extra_config_enc,
                request,
                context=f"integration={integration_id} assignment={assignment_id}",
            )
            opencti_token = request.headers.get("X-OpenCTI-Token", "")

            # ── 4. Normalize OpenCTI event metadata ─────────────────────────
            if provider == "opencti" or opencti_token:
                _normalize_opencti_payload(payload)
            elif provider == "openaev":
                # A provisioned trigger names its event; key the delivery on it.
                event_key = _normalize_openaev_payload(payload) or event_key

            # ── 5. Inject metadata ──────────────────────────────────────────
            payload["_webhook_meta"] = {
                "integration_id": integration_id,
                "assignment_id": assignment_id,
                "provider": provider,
                "event_key": event_key,
                "scope": "assignment",
                "headers": _safe_webhook_headers(request),
                "method": request.method,
            }
            if provider == "openaev":
                # CTEM-26: a failed reachability assertion is an outside-lens exposure (#1164).
                await ctem_openaev_events.stamp_reachability_assertion(
                    event_key,
                    payload,
                    integration_id=str(integration_id),
                    verified=_signed_with_webhook_secret(integration.extra_config_enc),
                )
            # A change in exposure the CTEM loop listens to (#826): a provisioned
            # OpenCTI trigger delivers here; the signal is recorded once per object,
            # from a delivery verified against a configured webhook secret only.
            await ctem_signal_listeners.note_event(
                provider,
                event_key,
                payload,
                verified=_signed_with_webhook_secret(integration.extra_config_enc),
            )
            if provider == "opencti":
                # The analyst's mark on a drafted Grouping answers the CTEM review
                # gate waiting on its coverage request (CTEM-16, #1089).
                await ctem_analyst_mark.note_event(
                    provider,
                    payload,
                    verified=_signed_with_webhook_secret(integration.extra_config_enc),
                )
            if provider == "openaev":
                # An OpenAEV result a CTEM case waits on moves it (CTEM-15, #918).
                await ctem_openaev_events.note_event(
                    event_key,
                    payload,
                    verified=_signed_with_webhook_secret(integration.extra_config_enc),
                )

            # ── 6. Resolve the specific assignment ────────────────────────────
            wf_result = await db.execute(
                select(Assignment).where(
                    Assignment.id == assignment_id,
                    Assignment.enabled.is_(True),
                )
            )
            wf = wf_result.scalar_one_or_none()
            if not wf:
                logger.warning(
                    "Per-assignment webhook: assignment %s not found or disabled",
                    assignment_id,
                )
                return await _finalize_dedup(
                    dedup_key,
                    {
                        "status": "ignored",
                        "reason": "assignment not found or disabled",
                    },
                )
            if await is_paused_by_deactivated_owner(db, wf):
                logger.info(
                    "Per-assignment webhook: assignment %s ignored, its owner "
                    "is deactivated",
                    assignment_id,
                )
                return await _owner_paused_answer(dedup_key)

            # Verify the assignment's trigger references this integration/provider
            config = wf.trigger_config or {}
            if provider == "opencti":
                # A trigger serving tracked organizations (#9157 M4): the run
                # learns which ones this event concerns.
                from app.services.organization_events import annotate_payload, named_in_payload

                annotate_payload(payload, config)
                # CTEM-29: a report or incident naming a tracked organization is a
                # third-party event (path 11), recorded once per organization.
                await ctem_signal_listeners.note_tracked_organizations(
                    payload,
                    named_in_payload(payload, config),
                    verified=_signed_with_webhook_secret(integration.extra_config_enc),
                )
            if wf.trigger_type == "integration_event":
                if config.get("provider") != provider:
                    logger.warning(
                        "Per-assignment webhook: assignment %s provider mismatch (%s != %s)",
                        assignment_id,
                        config.get("provider"),
                        provider,
                    )
                    return await _finalize_dedup(
                        dedup_key,
                        {"status": "ignored", "reason": "provider mismatch"},
                    )
                # The event the assignment was configured for. A
                # GitHub webhook pointed at this URL delivers every event its
                # subscription lists, and ``X-GitHub-Event`` makes the key
                # reliable, so the selection is enforced on such a delivery
                # like on the integration-level URL. A POST without the
                # header (a manual test, a relay that drops headers) is keyed
                # on the generic ``type`` / ``action`` fallback, which never
                # names a GitHub event, and keeps running as before. Other
                # providers keep relying on the sender's own event filter
                # plus ``events`` below: an OpenCTI notifier's ``type`` is not
                # always the catalog id. A trigger saved before exact matching
                # keeps running on every delivery here, as it always did, and
                # the first one it would now skip is reported.
                wf_event = config.get("event")
                github_delivery = provider == "github" and bool(
                    request.headers.get("X-GitHub-Event")
                )
                if not uses_exact_event_matching(config):
                    if github_delivery and not _event_selected(
                        event_key, wf_event, exact=True
                    ):
                        _report_legacy_widening(assignment_id, "event", wf_event, event_key)
                elif github_delivery and not _event_selected(
                    event_key, wf_event, exact=True, assignment_id=assignment_id
                ):
                    logger.info(
                        "Per-assignment webhook: GitHub event %s is not the "
                        "event %s selected for assignment %s",
                        event_key,
                        wf_event,
                        assignment_id,
                    )
                    return await _finalize_dedup(
                        dedup_key,
                        {
                            "status": "ok",
                            "triggered": [],
                            "count": 0,
                            "reason": "event not selected",
                        },
                    )
            elif wf.trigger_type != "webhook":
                logger.info(
                    "Per-assignment webhook: assignment %s trigger_type is %s, not integration_event/webhook",
                    assignment_id,
                    wf.trigger_type,
                )
                return await _finalize_dedup(
                    dedup_key,
                    {"status": "ignored", "reason": "trigger type mismatch"},
                )

            # ── 7. Event filter (optional) ──────────────────────────────────
            ek = _opencti_event_type(payload)
            if ek is None:
                ek = event_key
            if not _event_key_allowed(
                ek,
                config.get("events"),
                exact=uses_exact_event_matching(config),
                assignment_id=assignment_id,
            ):
                logger.info(
                    "Per-assignment webhook: event %s filtered out for assignment %s",
                    ek,
                    assignment_id,
                )
                return await _finalize_dedup(
                    dedup_key,
                    {"status": "ok", "triggered": [], "count": 0},
                )

            # ── 7a. The trigger's filter (integration events) ──────────────
            # Evaluated on the delivered body before any run or gate, as the
            # poller and the Graph notifications do; a dropped delivery is
            # counted on the assignment.
            if wf.trigger_type == "integration_event":
                # The agent is read here only when the filter acts as it
                # (``_filter_reads_as_agent``); a disabled one reads nothing:
                # the delivery is then refused below, unread.
                filter_agent = await db.get(Agent, wf.agent_id) if _filter_reads_as_agent(config) else None
                if filter_agent is not None and not filter_agent.enabled:
                    return await _finalize_dedup(
                        dedup_key,
                        {"status": "ignored", "reason": "agent not found or disabled"},
                    )
                reason = await _assignment_filter_reason(
                    db,
                    provider,
                    event_key,
                    payload,
                    event=config.get("event"),
                    trigger_config=config,
                    agent=filter_agent,
                    receiver_integration_id=str(integration_id),
                    assignment_id=str(wf.id),
                    retry_context={
                        "type": "webhook",
                        "scope": "assignment",
                        "provider": provider,
                        "integration_id": str(integration_id),
                        "assignment_id": str(assignment_id),
                    },
                )
                if reason == DEFERRED_REASON:
                    return await _finalize_dedup(
                        dedup_key,
                        {"status": "ok", "triggered": [], "count": 0, "reason": "event deferred"},
                    )
                if reason is not None:
                    logger.info(
                        "Per-assignment webhook: event %s filtered out for assignment %s (%s)",
                        event_key,
                        assignment_id,
                        reason,
                    )
                    await _count_filtered({str(wf.id): 1}, {str(wf.id): {reason_label(reason): 1}})
                    return await _finalize_dedup(
                        dedup_key,
                        {
                            "status": "ok",
                            "triggered": [],
                            "count": 0,
                            "reason": "event filtered out",
                        },
                    )

            # ── 7b. Self-triggered event suppression (loop protection) ──────
            # Drop GitHub events caused by the agent's OWN writes when the
            # assignment opted into it. Without this, an assignment that edits
            # an issue (title/labels/comment) re-triggers on its own webhook
            # echo and loops forever. Target-based (not actor-based) because the
            # agent usually acts through the owner's own credentials.
            if provider == "github" and getattr(
                wf, "ignore_self_triggered_events", False
            ):
                from app.services.github_echo import (
                    extract_github_target,
                    was_self_action,
                )

                target = extract_github_target(payload)
                if target and await was_self_action(
                    integration_id, target[0], target[1]
                ):
                    logger.info(
                        "Per-assignment webhook: event %s on %s#%s is the "
                        "agent's own echo — skipped for assignment %s "
                        "(ignore_self_triggered_events)",
                        event_key,
                        target[0],
                        target[1],
                        assignment_id,
                    )
                    return await _finalize_dedup(
                        dedup_key,
                        {
                            "status": "ok",
                            "triggered": [],
                            "count": 0,
                            "reason": "self-triggered event ignored",
                        },
                    )

            # ── 7c. The tracked organizations' daily run cap (#9157 M6) ─────
            # An event concerning tracked organizations that all used their
            # cap today is held and run tomorrow as one digest run.
            if provider == "opencti":
                from app.services.organization_run_cap import admit_delivery

                if not await admit_delivery(db, str(wf.id), payload):
                    return await _finalize_dedup(
                        dedup_key,
                        {
                            "status": "ok",
                            "triggered": [],
                            "count": 0,
                            "reason": "held for the organization's daily digest",
                        },
                    )

            # ── 8. Resolve agent and trigger ────────────────────────────────
            agent_result = await db.execute(
                select(Agent).where(Agent.id == wf.agent_id)
            )
            agent = agent_result.scalar_one_or_none()
            if not agent or not agent.enabled:
                logger.warning(
                    "Per-assignment webhook: agent %s not found or disabled",
                    wf.agent_id,
                )
                return await _finalize_dedup(
                    dedup_key,
                    {"status": "ignored", "reason": "agent not found or disabled"},
                )

            platform_url = await _shown_platform_url(
                db, integration, context=f"integration={integration_id} assignment={assignment_id}"
            )

            wf_trigger_context = {
                "type": "webhook",
                "scope": "assignment",
                "url": str(request.url),
                "method": request.method,
                "provider": provider,
                "event_key": event_key,
                "integration_id": integration_id,
                "assignment_id": assignment_id,
                "platform_url": platform_url,
                "headers": _safe_webhook_headers(request),
                "triggered_at": datetime.now(UTC).isoformat(),
                "body": payload,
            }
            try:
                run_id = await trigger_assignment(
                    assignment=wf,
                    agent=agent,
                    payload=payload,
                    db=db,
                    trigger_context=wf_trigger_context,
                    skip_if_active=True,
                )
                if run_id is None:
                    # Busy: with INTEGRATION_EVENT_QUEUE_ENABLED on a pushed
                    # event has no poller to fetch it again, so it is queued, past
                    # the cap if need be; off, the skip stands.
                    run_id = await queue_busy_pushed_event(
                        wf, agent, payload, db=db, trigger_context=wf_trigger_context
                    )
            except AssignmentOwnerDeactivatedError:
                # Owner deactivated since the check above.
                return await _owner_paused_answer(dedup_key)
            if run_id is None:
                # Burst guard: an active run already exists for this
                # assignment (skip logged inside trigger_assignment).
                return await _finalize_dedup(
                    dedup_key,
                    {
                        "status": "skipped",
                        "reason": "assignment already has an active run",
                    },
                )
            logger.info(
                "Per-assignment webhook triggered '%s' on agent '%s': run %s",
                wf.name,
                agent.name,
                run_id,
            )

            await db.commit()

        response = {
            "status": "ok",
            "triggered": [
                {"agent": agent.name, "assignment": wf.name, "run_id": run_id}
            ],
            "count": 1,
        }
        return await _finalize_dedup(dedup_key, response)
    except Exception:
        await _release_webhook_dedup_slot(dedup_key)
        raise


# ---------------------------------------------------------------------------
# Direct assignment webhook (by assignment ID)
# ---------------------------------------------------------------------------


@router.post("/assignments/{assignment_id}")
async def receive_direct_assignment_webhook(
    assignment_id: str, request: Request
) -> dict:
    """Receive an external webhook targeting a specific assignment by ID.

    URL pattern: ``POST /api/hooks/assignments/{assignment_id}``

    Looks up the assignment directly, verifies it is a webhook trigger and is
    enabled, then runs it — or queues the delivery when it is already busy.

    **Delivery guarantee.** An accepted delivery produces exactly one run,
    eventually. An assignment executes one run at a time (parallel runs of the
    same assignment would race the same tools and DB writes), so a delivery
    arriving while a run is active is placed on a per-assignment FIFO queue as
    a ``queued`` run and started as soon as the assignment goes idle. It is no
    longer discarded behind a 200.

    **Response status values**

    - ``ok`` — a run started; ``triggered[0].run_id`` is its id.
    - ``queued`` — accepted and waiting behind the active run; ``run_id`` is
      the queued run's id and ``queue_depth`` the current backlog.
    - ``duplicate`` (with ``deduplicated: true``) — a delivery with the same
      idempotency key already arrived within the window; the original
      response is replayed and nothing new was triggered.
    - ``dropped`` — the queue was full and the deployment is configured with
      ``ASSIGNMENT_QUEUE_OVERFLOW_POLICY=drop_newest``. **This delivery will
      never run.**
    - ``ignored`` — the target does not resolve (unknown/disabled assignment,
      not a webhook trigger, disabled agent) or an event filter excluded it.

    **Backpressure.** HTTP 429 with a ``Retry-After`` header means the
    delivery was NOT accepted and should be retried: either the per-assignment
    throttle (``WEBHOOK_REQUESTS_PER_MINUTE``, default 60/min) or a full
    delivery queue under the default ``reject`` overflow policy. Senders that
    honour ``Retry-After`` lose nothing during a burst.

    **Idempotency.** Duplicate deliveries (proxy / sender retries) are
    deduplicated so a retried POST returns the original ``run_id`` without
    re-firing the assignment. Send an ``Idempotency-Key`` header with a value
    unique per logical event to control this explicitly — it is the highest
    priority key and the only way to make two events that carry identical
    bodies fire twice. ``X-Idempotency-Key``, ``X-GitHub-Delivery``,
    ``Webhook-Id`` / ``Svix-Id`` and ``X-Hook-Signature-Id`` are honoured the
    same way. Without any of those, a provider id in the payload is used, and
    failing that a hash of the URL scope + raw body — see
    ``_claim_webhook_dedup_slot`` for the full key derivation and the
    recurrence caveat that fallback carries. The window is
    ``WEBHOOK_DEDUP_TTL_SECONDS`` (default 300).
    """
    assignment_uuid = safe_uuid(assignment_id)
    if assignment_uuid is None:
        return {"status": "ignored", "reason": "invalid assignment id"}

    # Per-assignment throttle — raises 429 when the sender floods this URL.
    # The bucket uses the canonical UUID form: ``uuid.UUID()`` accepts many
    # representations (uppercase, braces, no-hyphen hex) that all reach the
    # same assignment downstream, so the raw string would let a sender evade
    # the throttle by varying the formatting of the same ID.
    await enforce_rate_limit(
        request,
        "webhook-assignment",
        limit=_webhook_requests_per_minute(),
        window_seconds=60,
        bucket=str(assignment_uuid),
    )

    body = await request.body()

    # ── Authenticate BEFORE consulting the idempotency cache (see provider
    # handler for the rationale).  Verification is re-run below as defense
    # in depth.
    async with async_session() as db:
        wf_pre = (
            await db.execute(
                select(Assignment).where(
                    # Compare against the parsed UUID (not the raw path
                    # string) so the typed column never relies on implicit
                    # driver coercion and every formatting variant of the
                    # same ID resolves identically.
                    Assignment.id == assignment_uuid,
                    Assignment.trigger_type == "webhook",
                    Assignment.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        owner_paused = wf_pre is not None and await is_paused_by_deactivated_owner(
            db, wf_pre
        )
    if wf_pre is None:
        # Miss path — short-circuit BEFORE the idempotency claim so repeated
        # identical deliveries (same dedup key within the TTL) still count
        # toward the drift escalation.  Deduplicating a miss would let a
        # sender hammering the endpoint with a stable payload return early
        # as "duplicate" and never trip the drift alert.
        await _handle_direct_webhook_miss(
            str(assignment_uuid), assignment_id, request
        )
        return {
            "status": "ignored",
            "reason": "assignment not found, not a webhook trigger, or disabled",
        }

    # The assignment resolves again — the drift condition (deleted/disabled/
    # non-webhook target) is over, so drop any leftover miss counter + alert
    # dedup key.  A NEW drift starting later gets a fresh first-alert ERROR
    # instead of inheriting the remediated incident's 24-hour downgrade.
    await _clear_direct_webhook_drift_state(str(assignment_uuid))

    _verify_assignment_webhook_secret(
        body,
        request,
        (wf_pre.trigger_config or {}).get("webhook_secret"),
        assignment_id=assignment_id,
    )

    # Only an authenticated sender learns why nothing ran. Neither queued
    # nor deduplicated: there is nothing to replay once the owner is back.
    if owner_paused:
        logger.info(
            "Direct webhook delivery for assignment %s ignored: its owner is "
            "deactivated",
            assignment_id,
        )
        return {"status": "ignored", "reason": _OWNER_DEACTIVATED_REASON}

    is_duplicate, cached_response, dedup_key = await _claim_webhook_dedup_slot(
        request, body, scope=f"assignments:{assignment_id}"
    )
    if is_duplicate:
        logger.info(
            "Direct assignment webhook deduplicated for assignment %s "
            "(returning cached response from prior delivery within TTL)",
            assignment_id,
        )
        return cached_response or {
            "status": "duplicate",
            "deduplicated": True,
            "triggered": [],
            "count": 0,
        }

    # All return / raise paths below must persist (`_finalize_dedup`) or
    # release (`_release_webhook_dedup_slot`) the dedup slot, otherwise a
    # transient first-attempt failure (signature mismatch, DB blip,
    # crash) would silently swallow legitimate retries within the TTL
    # with a generic "duplicate" reply.
    try:
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {"raw_body": body.decode("utf-8", errors="replace")}
        if not isinstance(payload, dict):
            # A JSON array or scalar gets the provider route's shape,
            # rather than a 500 on the first key written into it.
            payload = {"raw_body": str(payload)}

        async with async_session() as db:
            wf_result = await db.execute(
                select(Assignment).where(
                    # Parsed UUID for the same reason as the pre-claim
                    # lookup above.
                    Assignment.id == assignment_uuid,
                    Assignment.trigger_type == "webhook",
                    Assignment.enabled.is_(True),
                )
            )
            wf = wf_result.scalar_one_or_none()
            if not wf:
                # Rare TOCTOU fallback: the assignment existed at the
                # pre-claim lookup above but was deleted/disabled since.
                # Count it like any other miss so the escalation stays
                # accurate.
                await _handle_direct_webhook_miss(
                    str(assignment_uuid), assignment_id, request
                )
                return await _finalize_dedup(
                    dedup_key,
                    {
                        "status": "ignored",
                        "reason": "assignment not found, not a webhook trigger, or disabled",
                    },
                )

            agent_result = await db.execute(
                select(Agent).where(Agent.id == wf.agent_id)
            )
            agent = agent_result.scalar_one_or_none()
            if not agent or not agent.enabled:
                logger.warning(
                    "Direct webhook: agent %s not found or disabled (assignment %s)",
                    wf.agent_id,
                    assignment_id,
                )
                return await _finalize_dedup(
                    dedup_key,
                    {"status": "ignored", "reason": "agent not found or disabled"},
                )

            # --- Normalize OpenCTI event metadata ---
            if request.headers.get("x-opencti-token"):
                _normalize_opencti_payload(payload)

            payload["_webhook_meta"] = {
                "assignment_id": assignment_id,
                "agent_id": str(wf.agent_id),
                "headers": _safe_webhook_headers(request),
                "method": request.method,
            }

            # --- Signature / token validation (optional per-assignment) ---
            # Defense in depth: the pre-claim gate above already rejected an
            # unsigned/invalid delivery, but re-verify against the shared
            # fail-closed helper so this path can never drift back open.
            webhook_secret = (wf.trigger_config or {}).get("webhook_secret")
            _verify_assignment_webhook_secret(
                body, request, webhook_secret, assignment_id=assignment_id
            )

            # --- Event filter (optional) ---
            wf_config = wf.trigger_config or {}
            event_key = _generic_webhook_event_key(payload, request)
            if not _event_key_allowed(
                event_key,
                wf_config.get("events"),
                exact=uses_exact_event_matching(wf_config),
                assignment_id=assignment_id,
            ):
                return await _finalize_dedup(
                    dedup_key,
                    {"status": "ok", "triggered": [], "count": 0},
                )

            trigger_context = {
                "type": "webhook",
                "scope": "assignment",
                "url": str(request.url),
                "method": request.method,
                "assignment_id": assignment_id,
                "agent_id": str(wf.agent_id),
                "headers": _safe_webhook_headers(request),
                "triggered_at": datetime.now(UTC).isoformat(),
                "body": payload,
            }
            if not settings.assignment_queue_enabled:
                # Legacy behaviour, opt-in only: a delivery arriving while a
                # run is active is DROPPED and still answered 200 — the sender
                # records a success for a run that never happened.
                try:
                    run_id = await trigger_assignment(
                        assignment=wf,
                        agent=agent,
                        payload=payload,
                        db=db,
                        trigger_context=trigger_context,
                        skip_if_active=True,
                    )
                except AssignmentOwnerDeactivatedError:
                    # Owner deactivated since the check above.
                    return await _owner_paused_answer(dedup_key)
                if run_id is None:
                    logger.warning(
                        "Direct webhook delivery for assignment '%s' (%s) was "
                        "DROPPED — a run is already active and the delivery "
                        "queue is disabled (ASSIGNMENT_QUEUE_ENABLED=false)",
                        wf.name,
                        assignment_id,
                    )
                    return await _finalize_dedup(
                        dedup_key,
                        {
                            "status": "skipped",
                            "reason": "assignment already has an active run",
                        },
                    )
                await db.commit()
                queue_result = None
            else:
                try:
                    queue_result = await trigger_or_queue_assignment(
                        assignment=wf,
                        agent=agent,
                        payload=payload,
                        db=db,
                        trigger_context=trigger_context,
                    )
                except AssignmentOwnerDeactivatedError:
                    # Owner deactivated since the check above.
                    return await _owner_paused_answer(dedup_key)
                if queue_result.status == "queue_full":
                    # Real backpressure: the delivery was NOT accepted, so the
                    # sender must retry. Raising (rather than returning through
                    # ``_finalize_dedup``) is deliberate — the generic handler
                    # below releases the dedup slot, so the sender's retry with
                    # the same idempotency key is treated as a first delivery
                    # instead of replaying the 429 body back as a 200.
                    raise HTTPException(
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        detail=(
                            "This assignment's delivery queue is full "
                            f"({queue_result.queue_depth} waiting). Retry "
                            "after the current backlog drains."
                        ),
                        headers={
                            "Retry-After": str(queue_result.retry_after_seconds)
                        },
                    )
                if queue_result.status == "dropped":
                    return await _finalize_dedup(
                        dedup_key,
                        {
                            "status": "dropped",
                            "reason": (
                                # The agentic quota is spent, no run created.
                                "the agent's agentic quota is exhausted - this "
                                "delivery will not run"
                                if queue_result.reason == QUOTA_DROP_REASON
                                else "assignment delivery queue is full and the "
                                "overflow policy is 'drop_newest' — this "
                                "delivery will not run"
                            ),
                            "queue_depth": queue_result.queue_depth,
                        },
                    )
                run_id = queue_result.run_id

            if queue_result is not None and queue_result.status == "queued":
                logger.info(
                    "Direct webhook queued '%s' on '%s': run %s (depth %d)",
                    wf.name,
                    agent.name,
                    run_id,
                    queue_result.queue_depth,
                )
                return await _finalize_dedup(
                    dedup_key,
                    {
                        "status": "queued",
                        "reason": (
                            "assignment already has an active run — this "
                            "delivery is queued and will run next"
                        ),
                        "run_id": run_id,
                        "queue_depth": queue_result.queue_depth,
                        "queued": [
                            {
                                "agent": agent.name,
                                "assignment": wf.name,
                                "run_id": run_id,
                            }
                        ],
                        "count": 0,
                    },
                )
            logger.info(
                "Direct webhook triggered '%s' on '%s': run %s",
                wf.name,
                agent.name,
                run_id,
            )

        response = {
            "status": "ok",
            "triggered": [
                {"agent": agent.name, "assignment": wf.name, "run_id": run_id}
            ],
            "count": 1,
        }
        return await _finalize_dedup(dedup_key, response)
    except Exception:
        await _release_webhook_dedup_slot(dedup_key)
        raise


# ---------------------------------------------------------------------------
# Agent-level webhook (generic / custom – OpenCTI, custom systems) [LEGACY]
# ---------------------------------------------------------------------------


# ─── Flow webhooks ───────────────────────────────────────────


#: The 429 wording of a flow webhook delivery that started no run, per reason
#: (``flow_triggers.WEBHOOK_SKIP_*``). Same status and ``Retry-After`` for all.
_FLOW_WEBHOOK_ACTIVE_RUN_DETAIL = "A run of this flow is already active — retry shortly."
_FLOW_WEBHOOK_SKIP_DETAILS = {
    "active_run": _FLOW_WEBHOOK_ACTIVE_RUN_DETAIL,
    "ai_unavailable": (
        "This flow has a step that needs AI, and agentic AI is not available "
        "on this platform right now (switched off, not licensed, or its state "
        "could not be read). The delivery was refused and nothing ran; retry "
        "later."
    ),
    "ownerless": (
        "This flow is company-managed and has no owner, so its webhook "
        "trigger does not run automatically. The delivery was refused and "
        "nothing ran; run the flow manually or give it an owner."
    ),
}


def flow_webhook_skip_detail(skipped: str | None) -> str:
    """The 429 ``detail`` for a flow webhook delivery skipped for *skipped*."""
    return _FLOW_WEBHOOK_SKIP_DETAILS.get(skipped or "", _FLOW_WEBHOOK_ACTIVE_RUN_DETAIL)


# Undocumented while "FLOW_BUILDER" is off, like the /api/v1/flows operations:
# this receiver is the one flow path on a router that must stay in the schema
# (the assignment receivers share it), so the gate is per-route here. The
# endpoint keeps working either way — the flag is UI-only, and a webhook a
# partner already configured must not start being ignored.
@router.post(
    "/flows/{flow_id}/{path:path}",
    include_in_schema=is_feature_enabled("FLOW_BUILDER"),
)
async def receive_flow_webhook(flow_id: str, path: str, request: Request) -> dict:
    """Receive an external webhook targeting a flow's `trigger.webhook` node.

    URL pattern: ``POST /api/hooks/flows/{flow_id}/{path}`` — the flow UUID
    plus the path configured on the trigger node.  The flow must be **active**
    and carry a webhook trigger whose ``path`` matches.

    Reuses the assignment webhook belt end to end: per-flow rate limit,
    secret verification *before* the idempotency cache (the secret is minted
    on activation and shown in the builder), dedup slot with cached-response
    replay, safe header capture.  Unlike assignments there is no delivery
    queue yet: a delivery that starts no run returns **429 with
    Retry-After** so a sender that honours it loses nothing - a run already
    active, a flow needing agentic AI while it is unavailable, or an ownerless
    flow, each with its own ``detail`` (``flow_webhook_skip_detail``).

    Response statuses: ``ok`` (run started), ``duplicate`` (idempotent
    replay), ``ignored`` (unknown/inactive flow, unmatched path, or a flow (not
    company-managed) whose owner's account is deactivated - never cached).
    """
    flow_uuid = safe_uuid(flow_id)
    if flow_uuid is None:
        return {"status": "ignored", "reason": "invalid flow id"}

    await enforce_rate_limit(
        request,
        "webhook-flow",
        limit=_webhook_requests_per_minute(),
        window_seconds=60,
        bucket=str(flow_uuid),
    )

    body = await request.body()

    from app.models.flow import FLOW_STATUS_ACTIVE, Flow
    from app.services.flow_triggers import (
        resolve_webhook_trigger,
        trigger_flow_from_webhook_outcome,
    )

    # ── Authenticate BEFORE consulting the idempotency cache, same order as
    # the assignment receiver: a replay with a bad signature must never read
    # a cached response.
    async with async_session() as db:
        flow_pre = (
            await db.execute(
                select(Flow).where(
                    Flow.id == flow_uuid, Flow.status == FLOW_STATUS_ACTIVE
                )
            )
        ).scalar_one_or_none()
        owner_paused = flow_pre is not None and await is_paused_by_deactivated_owner(
            db, flow_pre
        )
    if flow_pre is None or resolve_webhook_trigger(flow_pre.graph or {}, path) is None:
        return {
            "status": "ignored",
            "reason": "flow not found, not active, or no webhook trigger on this path",
        }

    _verify_assignment_webhook_secret(
        body,
        request,
        flow_pre.webhook_secret,
        assignment_id=f"flow:{flow_id}",
    )
    # After the secret check, before any dedup claim: nothing cached,
    # so the same delivery retried once the owner is back runs.
    if owner_paused:
        logger.info("Flow webhook for flow %s ignored: its owner is deactivated", flow_id)
        return {"status": "ignored", "reason": _FLOW_OWNER_DEACTIVATED_REASON}

    is_duplicate, cached_response, dedup_key = await _claim_webhook_dedup_slot(
        request, body, scope=f"flows:{flow_id}:{path}"
    )
    if is_duplicate:
        logger.info(
            "Flow webhook deduplicated for flow %s (cached response replayed)",
            flow_id,
        )
        return cached_response or {"status": "duplicate", "deduplicated": True}

    try:
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {"raw_body": body.decode("utf-8", errors="replace")}
        if not isinstance(payload, dict):
            payload = {"value": payload}

        async with async_session() as db:
            flow = (
                await db.execute(
                    select(Flow).where(
                        Flow.id == flow_uuid, Flow.status == FLOW_STATUS_ACTIVE
                    )
                )
            ).scalar_one_or_none()
            node = (
                resolve_webhook_trigger(flow.graph or {}, path) if flow else None
            )
            if flow is None or node is None:
                return await _finalize_dedup(
                    dedup_key,
                    {
                        "status": "ignored",
                        "reason": (
                            "flow not found, not active, or no webhook trigger "
                            "on this path"
                        ),
                    },
                )
            if await is_paused_by_deactivated_owner(db, flow):
                return await _owner_paused_answer(
                    dedup_key, _FLOW_OWNER_DEACTIVATED_REASON
                )

            try:
                outcome = await trigger_flow_from_webhook_outcome(
                    db,
                    flow,
                    node,
                    payload=payload,
                    headers=_safe_webhook_headers(request),
                )
            except FlowOwnerDeactivatedError:
                # Owner deactivated since the check above.
                return await _owner_paused_answer(
                    dedup_key, _FLOW_OWNER_DEACTIVATED_REASON
                )
        run_id = outcome.run_id

        if run_id is None:
            # Skipped: tell the sender to retry rather than silently dropping
            # the delivery, in words that say why. The dedup slot is released
            # so the retry is not swallowed as a duplicate.
            await _release_webhook_dedup_slot(dedup_key)
            raise HTTPException(
                status_code=429,
                detail=flow_webhook_skip_detail(outcome.skipped),
                headers={"Retry-After": "30"},
            )

        logger.info("Flow webhook triggered flow %s: run %s", flow_id, run_id)
        return await _finalize_dedup(
            dedup_key, {"status": "ok", "run_id": run_id}
        )
    except HTTPException:
        raise
    except Exception:
        await _release_webhook_dedup_slot(dedup_key)
        raise


@router.post("/{agent_id}/{path:path}")
async def receive_webhook(agent_id: str, path: str, request: Request) -> dict:
    """Receive an external webhook and trigger matching assignments.

    .. deprecated::
        Legacy endpoint. New webhook assignments use
        ``POST /api/hooks/assignments/{assignment_id}`` instead.

    Duplicate deliveries are deduplicated via the shared webhook
    idempotency layer (Redis SETNX + 5 min TTL) so retries don't fan
    out a second time.
    """
    agent_uuid = safe_uuid(agent_id)
    if agent_uuid is None:
        return {"status": "ignored", "reason": "invalid agent id"}

    # Per-target throttle for the legacy agent-level endpoint (bucket =
    # agent + path, the assignment-matching key) — raises 429 on floods.
    # ``path`` is a ``{path:path}`` catch-all and can be arbitrarily long /
    # attacker-controlled, so it is hashed to keep the Redis key bounded
    # (a multi-KB random path must not become a multi-KB Redis key). The
    # agent segment uses the canonical UUID form: ``uuid.UUID()`` accepts
    # many representations (uppercase, braces, no-hyphen hex) that all
    # reach the same agent downstream, so the raw string would let a
    # sender evade the throttle by varying the formatting of the same ID.
    path_digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    await enforce_rate_limit(
        request,
        "webhook-assignment",
        limit=_webhook_requests_per_minute(),
        window_seconds=60,
        bucket=f"{agent_uuid}:{path_digest}",
    )

    body = await request.body()

    # ── Authenticate BEFORE consulting the idempotency cache (see provider
    # handler for the rationale).  Any matching webhook assignment that
    # configures a secret must validate before the cache is consulted.
    # Verification is re-run per assignment below as defense in depth.
    async with async_session() as db:
        pre_assignments = (
            (
                await db.execute(
                    select(Assignment).where(
                        Assignment.agent_id == agent_id,
                        Assignment.trigger_type == "webhook",
                        Assignment.enabled.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
    for wf_pre in pre_assignments:
        cfg_pre = wf_pre.trigger_config or {}
        if cfg_pre.get("path", "") == path:
            _verify_assignment_webhook_secret(
                body,
                request,
                cfg_pre.get("webhook_secret"),
                assignment_id=str(wf_pre.id),
            )

    is_duplicate, cached_response, dedup_key = await _claim_webhook_dedup_slot(
        request, body, scope=f"agents:{agent_id}:{path}"
    )
    if is_duplicate:
        logger.info(
            "Agent webhook deduplicated for agent=%s path=%s "
            "(returning cached response from prior delivery within TTL)",
            agent_id,
            path,
        )
        return cached_response or {
            "status": "duplicate",
            "deduplicated": True,
            "triggered": [],
            "count": 0,
        }

    # All return / raise paths below must persist (`_finalize_dedup`) or
    # release (`_release_webhook_dedup_slot`) the dedup slot, otherwise a
    # transient first-attempt failure would silently swallow legitimate
    # retries within the TTL with a generic "duplicate" reply.
    try:
        try:
            payload = json.loads(body)
        except ValueError:
            payload = {"raw_body": body.decode("utf-8", errors="replace")}
        if not isinstance(payload, dict):
            # A JSON array or scalar gets the provider route's shape,
            # rather than a 500 on the first key written into it.
            payload = {"raw_body": str(payload)}

        payload["_webhook_meta"] = {
            "path": path,
            "agent_id": agent_id,
            "headers": _safe_webhook_headers(request),
            "method": request.method,
        }

        # --- Normalize OpenCTI event metadata ---
        if path == "opencti" or request.headers.get("x-opencti-token"):
            _normalize_opencti_payload(payload)

        async with async_session() as db:
            result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = result.scalar_one_or_none()
            if not agent or not agent.enabled:
                logger.warning("Webhook for unknown/disabled agent: %s", agent_id)
                return await _finalize_dedup(
                    dedup_key,
                    {"status": "ignored", "reason": "agent not found or disabled"},
                )

            wf_result = await db.execute(
                select(Assignment).where(
                    Assignment.agent_id == agent_id,
                    Assignment.trigger_type == "webhook",
                    Assignment.enabled.is_(True),
                    owner_not_deactivated(),
                )
            )
            assignments = wf_result.scalars().all()

            # Extract platform URL from agent's integration bindings (for link generation).
            # Try exact match on path first (legacy webhooks where path == provider),
            # then try prefix match (e.g. path "opencti-event" → binding "opencti"),
            # finally fall back to iterating all bindings for any with a base_url.
            platform_url = ""
            bindings = getattr(agent, "integration_bindings", None) or {}
            candidate_ids: list[str] = []
            _direct = bindings.get(path)
            if _direct:
                candidate_ids.extend(
                    _direct if isinstance(_direct, list) else [_direct]
                )
            else:
                for provider_key, integ_ids in bindings.items():
                    ids = (
                        integ_ids
                        if isinstance(integ_ids, list)
                        else [integ_ids] if integ_ids else []
                    )
                    if ids and path.startswith(provider_key):
                        candidate_ids.extend(ids)
                if not candidate_ids:
                    candidate_ids = [
                        iid
                        for ids in bindings.values()
                        for iid in (
                            ids if isinstance(ids, list) else [ids] if ids else []
                        )
                    ]
            for cid in candidate_ids:
                if platform_url:
                    break
                integ_result = await db.execute(
                    select(Integration)
                    .options(
                        load_only(Integration.extra_config_enc, Integration.auth_type)
                    )
                    .where(Integration.id == cid)
                )
                integ = integ_result.scalar_one_or_none()
                if integ:
                    platform_url = await _shown_platform_url(
                        db, integ, context=f"agent={agent_id}"
                    )

            triggered = []
            for wf in assignments:
                wf_path = (wf.trigger_config or {}).get("path", "")
                if wf_path != path:
                    continue

                # --- Signature / token validation (optional per-assignment) ---
                # Defense in depth: the pre-claim gate above already rejected
                # an unsigned/invalid delivery, but re-verify against the
                # shared fail-closed helper so this path can never drift open.
                webhook_secret = (wf.trigger_config or {}).get("webhook_secret")
                _verify_assignment_webhook_secret(
                    body, request, webhook_secret, assignment_id=str(wf.id)
                )

                # --- Event filter (optional) ---
                wf_config = wf.trigger_config or {}
                # OpenCTI event matching (create, update, delete), else GitHub / generic
                event_key = _generic_webhook_event_key(payload, request)
                if not _event_key_allowed(
                    event_key,
                    wf_config.get("events"),
                    exact=uses_exact_event_matching(wf_config),
                    assignment_id=wf.id,
                ):
                    continue

                agent_trigger_context = {
                    "type": "webhook",
                    "scope": "agent",
                    "url": str(request.url),
                    "method": request.method,
                    "path": path,
                    "agent_id": agent_id,
                    "platform_url": platform_url,
                    "headers": _safe_webhook_headers(request),
                    "triggered_at": datetime.now(UTC).isoformat(),
                    "body": payload,
                }
                try:
                    run_id = await trigger_assignment(
                        assignment=wf,
                        agent=agent,
                        payload=payload,
                        db=db,
                        trigger_context=agent_trigger_context,
                        skip_if_active=True,
                    )
                    if run_id is None:
                        # Busy: with INTEGRATION_EVENT_QUEUE_ENABLED on a pushed
                        # event has no poller to fetch it again, so it is queued, past
                        # the cap if need be; off, the skip stands.
                        run_id = await queue_busy_pushed_event(
                            wf, agent, payload, db=db, trigger_context=agent_trigger_context
                        )
                except AssignmentOwnerDeactivatedError:
                    # Owner deactivated since the query picked it.
                    continue
                if run_id is None:
                    # Burst guard: an active run already exists for this
                    # assignment (skip logged inside trigger_assignment).
                    continue
                triggered.append({"assignment": wf.name, "run_id": run_id})
                logger.info(
                    "Webhook triggered '%s' on '%s': run %s",
                    wf.name,
                    agent.name,
                    run_id,
                )

            if not triggered:
                logger.info("Webhook %s/%s: no matching assignment", agent_id, path)

            await db.commit()

        response = {"status": "ok", "triggered": triggered, "count": len(triggered)}
        return await _finalize_dedup(dedup_key, response)
    except Exception:
        await _release_webhook_dedup_slot(dedup_key)
        raise
