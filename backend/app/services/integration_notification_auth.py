# SPDX-License-Identifier: Apache-2.0

"""Sender authentication for the shared integration notification endpoints.

Two push receivers in ``api/v1/integration_notifications.py`` serve assignments of every
user, so the sender is proven before anything is read or run:

- **Slack** (``POST /api/v1/integrations/notifications/slack``): the ``X-Slack-Signature``
  HMAC over ``v0:<X-Slack-Request-Timestamp>:<body>``, keyed with the signing secret of
  the Slack app that sends the event. Every Slack integration is bound to the secret of
  ITS app: the one stored on the Slack OAuth default when the integration runs on
  the admin-provided app, else the one stored on the integration
  (``extra_config.webhook_secret``, the key the hook routes of ``webhooks.py`` verify
  Slack signatures with). A delivery is authentic only for the integrations whose app
  secret signed it - a secret held for one app says nothing about another app's
  integrations, so a user who
  knows their own app's secret cannot sign for a colleague's. A delivery no held secret
  proves never reaches an integration whose app secret is configured; it still reaches
  the integrations holding no usable secret, as before senders were checked, with a
  warning and a Security Audit finding, unless
  ``INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION`` refuses it (those integrations then
  receive their messages through the event poller). A Slack channel bot's secret proves
  nothing here: its app sends its events to ``/api/channels/slack/events``.
- **Microsoft Graph** (``POST /api/v1/integrations/notifications/microsoft``): the
  ``clientState`` Graph echoes on every change notification. The record of which
  assignment a subscription feeds holds it Fernet-encrypted; :func:`new_graph_client_state`
  and :func:`store_graph_subscription` are the only way to write one. A legacy record
  holding only the assignment id keeps serving it, with a warning, unless
  ``INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION`` is set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session
from app.core.log_once import fingerprint, log_once_soon
from app.core.security import (
    UnusableSecretError,
    configured_secret,
    decrypt_value,
    encrypt_value,
    timing_safe_str_eq,
    verify_slack_signature,
)
from app.core.security_options import security_option, where_to_set
from app.core.utils import format_exception_message, is_valid_uuid
from app.models.integration import Integration
from app.models.oauth_default import OAuthDefault
from app.services.integration_state import UNREADABLE_ADMIN_CLIENT_ID, uses_admin_oauth_app

logger = logging.getLogger(__name__)

SLACK_NOTIFICATION_PATH = "/api/v1/integrations/notifications/slack"
GRAPH_NOTIFICATION_PATH = "/api/v1/integrations/notifications/microsoft"

# One line per condition per process in this window: an endpoint hit on every Slack
# message must not drown the log, and one line is enough to surface the gap.
_LOG_INTERVAL_SECONDS = 900.0
_log_state: dict[str, tuple[float, int]] = {}


def _log_rate_limited(key: str, level: int, message: str, *args: object) -> None:
    """Log *message* at most once per window for *key*, counting what was held back."""
    now = time.monotonic()
    last = _log_state.get(key)
    if last is not None and now - last[0] < _LOG_INTERVAL_SECONDS:
        _log_state[key] = (last[0], last[1] + 1)
        return
    _log_state[key] = (now, 0)
    if last is not None and last[1]:
        message += " (%d more since the last one)"
        args = (*args, last[1])
    logger.log(level, message, *args)


log_rate_limited = _log_rate_limited


def log_across_processes(key: str, level: int, message: str, *args: object) -> None:
    """:func:`log_rate_limited` for the whole platform: one line per window across every process.

    For a delivery accepted as before senders were checked: it arrives with every
    legitimate event of a standing configuration, so each worker, recycle and
    replica would otherwise say it again. A *key* that ends with the
    configuration it describes is said again as soon as that changes.
    """
    log_once_soon(logger, level, f"integration_notification:{key}", message, *args, interval=_LOG_INTERVAL_SECONDS)


VERIFICATION_OPTION = "integration_notifications_require_verification"


def verification_required() -> bool:
    """Whether the notification endpoints refuse what they cannot verify.

    ``INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION`` when the variable is set,
    else the "Require verified integration notifications" option of Settings >
    Security, else ``False``: what an install received before senders were checked
    keeps arriving.
    """
    return bool(security_option(VERIFICATION_OPTION))


def admin_app_scoping_required() -> bool:
    """Whether admin-app deliveries are narrowed to the admin app's integrations.

    The provider webhook route and the LinkedIn notification endpoint belong to the
    admin's OAuth app; the verification option limits what they deliver to
    assignments reaching an integration on that app the agent owner may use
    #(4385). Off by default: a delivery reaches what it reached before.
    """
    return verification_required()


def warn_unscoped_admin_app_delivery(provider: str, count: int, reason: str) -> None:
    """Say (rate-limited, no payload) that a delivery reached assignments the strict rule would not."""
    if count <= 0:
        return
    log_across_processes(
        f"admin-app-unscoped:{provider}",
        logging.WARNING,
        "%s delivery reached %d assignment(s) %s, as before deliveries were limited to "
        "the admin app's integrations. Set INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION"
        "=true to limit them.",
        provider,
        count,
        reason,
    )


PROVIDER_WEBHOOK_PATH = "/api/hooks/providers/{provider}"

# Why the admin app's provider webhook accepts a delivery it cannot verify.
NO_OAUTH_DEFAULT = "no OAuth default"
NO_WEBHOOK_SECRET = "no webhook secret on its OAuth default"  # noqa: S105
NO_CLIENT_SECRET = "no client secret on its OAuth default"  # noqa: S105


def warn_unverified_provider_delivery(provider: str, reason: str, count: int) -> None:
    """Say (rate-limited, no payload) that the provider webhook ran assignments on a delivery it cannot verify."""
    if count <= 0:
        return
    hint = (
        "To keep such deliveries from reaching any assignment, "
        + where_to_set(VERIFICATION_OPTION, "true")
        + "."
        if reason == NO_OAUTH_DEFAULT
        else "The Security Audit (Settings > Access and security) says how to verify or block them."
    )
    log_across_processes(
        f"provider-unverified:{provider}:{fingerprint(reason)}",
        logging.WARNING,
        "Provider webhook %s: a delivery the platform cannot verify (%s) triggered %d assignment(s), "
        "as before signatures were checked; anyone who can reach %s can forge one. %s",
        provider,
        reason,
        count,
        PROVIDER_WEBHOOK_PATH.format(provider=provider),
        hint,
    )


# ---------------------------------------------------------------------------
# Slack signing secrets
# ---------------------------------------------------------------------------

SLACK_SIGNATURE_SETUP_HINT = (
    "Store the Slack app's Signing Secret (Slack app > Basic Information) in the "
    "'Signing secret' field of its Slack integration, or of the Slack OAuth default "
    "when an administrator provides the app."
)


OAUTH_DEFAULT_LABEL = "Slack OAuth default"


@dataclass(frozen=True)
class SlackAppSecret:
    """One Slack app's signing secret and the integrations that run on that app."""

    secret: str
    integration_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SlackSigningSecrets:
    """The Slack signing secrets the platform holds, bound to the integrations they sign for."""

    apps: tuple[SlackAppSecret, ...] = ()
    """One entry per distinct usable secret."""
    unknown: tuple[str, ...] = ()
    """Slack integrations whose app secret is not stored anywhere: no real-time delivery."""
    unreadable: tuple[str, ...] = ()
    """Configurations (integration ids, or ``Slack OAuth default``) that cannot be decrypted."""
    unusable: tuple[str, ...] = ()
    """Configurations whose secret is set but no sender can prove."""

    @property
    def secret_for(self) -> dict[str, str]:
        """Integration id -> the signing secret of its app."""
        return {iid: app.secret for app in self.apps for iid in app.integration_ids}

    @property
    def unverifiable(self) -> tuple[str, ...]:
        """The Slack integrations holding no usable signing secret, which no delivery can prove."""
        return tuple(
            iid for iid in (*self.unknown, *self.unusable, *self.unreadable) if iid != OAUTH_DEFAULT_LABEL
        )


class _UnreadableConfigError(Exception):
    """A stored configuration that cannot be decrypted or is not a JSON object."""


def _read_config(config_enc: str | None) -> dict:
    """A decrypted JSON-object ``extra_config`` (``{}`` when none is stored)."""
    if not config_enc:
        return {}
    try:
        config = json.loads(decrypt_value(config_enc))
    except Exception:
        raise _UnreadableConfigError from None
    if not isinstance(config, dict):
        raise _UnreadableConfigError
    return config


def _decrypt_or_none(value_enc: str | None) -> str | None:
    if not value_enc:
        return None
    try:
        return decrypt_value(value_enc)
    except Exception:
        return None


async def load_slack_signing_secrets(db: AsyncSession) -> SlackSigningSecrets:
    """Bind every Slack integration to the signing secret of the app it runs on."""
    unreadable: list[str] = []
    unusable: list[str] = []

    default_secret: str | None = None
    default_client_id: object = None
    default_row = (
        await db.execute(
            select(OAuthDefault.client_id_enc, OAuthDefault.extra_config_enc).where(
                OAuthDefault.provider == "slack"
            )
        )
    ).first()
    if default_row is not None:
        default_client_id = _decrypt_or_none(default_row[0]) or UNREADABLE_ADMIN_CLIENT_ID
        try:
            default_secret = configured_secret(_read_config(default_row[1]).get("webhook_secret"))
        except _UnreadableConfigError:
            unreadable.append(OAUTH_DEFAULT_LABEL)
        except UnusableSecretError:
            unusable.append(OAUTH_DEFAULT_LABEL)

    bound: dict[str, list[str]] = {}
    unknown: list[str] = []
    rows = (
        await db.execute(
            select(
                Integration.id,
                Integration.client_id_enc,
                Integration.extra_config_enc,
                Integration.uses_admin_oauth_credentials,
            ).where(Integration.provider == "slack")
        )
    ).all()
    for row_id, client_id_enc, config_enc, recorded in rows:
        integration_id = str(row_id)
        # Creating an integration on the admin-provided app copies the default's
        # client id into it - and its extra config, signing secret included, a copy
        # that goes stale when the administrator rotates or clears the secret, and
        # that the edit form never shows. The app's current secret is the default's,
        # and only the default's: with none usable there, the integration has none.
        # Which app it runs on is the integration API's answer: the recorded
        # choice, else its client id against the default's.
        if default_row is not None and uses_admin_oauth_app(recorded, client_id_enc, default_client_id):
            if default_secret:
                bound.setdefault(default_secret, []).append(integration_id)
            else:
                unknown.append(integration_id)
            continue
        try:
            own = configured_secret(_read_config(config_enc).get("webhook_secret"))
        except _UnreadableConfigError:
            unreadable.append(integration_id)
            continue
        except UnusableSecretError:
            unusable.append(integration_id)
            continue
        if own is None:
            unknown.append(integration_id)
            continue
        bound.setdefault(own, []).append(integration_id)

    if default_secret and default_secret not in bound:
        # The admin app with no integration on it yet: it still proves its own URL
        # verification request, it serves no assignment.
        bound[default_secret] = []
    return SlackSigningSecrets(
        apps=tuple(SlackAppSecret(secret, tuple(ids)) for secret, ids in bound.items()),
        unknown=tuple(unknown),
        unreadable=tuple(unreadable),
        unusable=tuple(unusable),
    )


# Short-lived and single-flight, like the provider routing of ``webhooks.py``: the
# endpoint is unauthenticated, and one load decrypts every Slack configuration, so a
# flood of forged deliveries costs one load per window rather than one per request.
# A secret saved in the UI therefore applies within this delay (Slack retries a
# refused delivery after a minute).
_SLACK_SECRETS_TTL_SECONDS = 30.0
_slack_secrets_cache: tuple[float, SlackSigningSecrets] | None = None
_slack_secrets_lock = asyncio.Lock()


def invalidate_slack_signing_secrets() -> None:
    """Drop the cached secrets so the next delivery reloads them."""
    global _slack_secrets_cache
    _slack_secrets_cache = None


async def slack_signing_secrets() -> SlackSigningSecrets:
    """The Slack signing secrets, cached for ``_SLACK_SECRETS_TTL_SECONDS``."""
    global _slack_secrets_cache
    cached = _slack_secrets_cache
    if cached is not None and time.monotonic() - cached[0] < _SLACK_SECRETS_TTL_SECONDS:
        return cached[1]
    async with _slack_secrets_lock:
        cached = _slack_secrets_cache
        if cached is not None and time.monotonic() - cached[0] < _SLACK_SECRETS_TTL_SECONDS:
            return cached[1]
        async with async_session() as db:
            loaded = await load_slack_signing_secrets(db)
        _slack_secrets_cache = (time.monotonic(), loaded)
        return loaded


@dataclass(frozen=True)
class SlackVerdict:
    """What one delivery to the Slack endpoint proved, decided before its body is read."""

    verified: bool
    """Signed with a secret the platform holds."""
    integration_ids: frozenset[str] = frozenset()
    """The only integrations it may reach: those whose app secret signed it, or, for an
    unverified delivery accepted for compatibility, those holding no usable secret."""
    compatible: bool = False
    """Unverified, and accepted as before signatures were checked."""

    @property
    def accepted(self) -> bool:
        return self.verified or self.compatible


def check_slack_delivery(
    state: SlackSigningSecrets,
    body: bytes,
    signature: str,
    timestamp: str,
    *,
    require_verification: bool | None = None,
) -> SlackVerdict:
    """The verdict on one delivery to the Slack endpoint, from its raw body and headers.

    Every held secret is tried - no early exit, so the time taken does not say which
    one matched - through ``verify_slack_signature`` (constant-time, 5-minute
    freshness window, never raises on crafted headers). Missing, wrong, stale and
    malformed signatures are the same refusal. Logs are rate-limited and never carry
    the payload.

    A delivery no held secret proves never reaches an integration whose app secret is
    configured. Unless ``INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION`` (or
    *require_verification*) says otherwise, it is accepted as before signatures were
    checked (``compatible``): ``slack_events.dispatch_slack_message`` then fans it out
    as it always did to every assignment not connected to an integration whose app
    secret is configured.
    """
    if require_verification is None:
        require_verification = verification_required()
    verified = False
    reached: set[str] = set()
    if signature:
        for app in state.apps:
            if verify_slack_signature(body, app.secret, signature, timestamp):
                verified = True
                reached.update(app.integration_ids)
    if verified:
        return SlackVerdict(True, frozenset(reached))
    if not require_verification:
        if state.unreadable:
            _log_rate_limited(
                "slack-unreadable",
                logging.ERROR,
                "Slack deliveries on %s cannot be verified for %s: the configuration "
                "that may hold the signing secret cannot be decrypted or parsed. "
                "Re-enter the signing secret or restore the SECRET_KEY it was "
                "encrypted with.",
                SLACK_NOTIFICATION_PATH,
                ", ".join(state.unreadable),
            )
        log_across_processes(
            f"slack-unverified:{fingerprint(sorted(state.unverifiable))}",
            logging.WARNING,
            "Slack delivery on %s accepted without a verified signature, as before "
            "signatures were checked: it reaches every new_slack_message assignment not "
            "connected to an integration whose Slack app signing secret is stored (%d "
            "Slack integration(s) hold no usable secret). Anyone who can reach this URL "
            "can post events for them. %s Set "
            "INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true to refuse every "
            "unverified delivery.",
            SLACK_NOTIFICATION_PATH,
            len(state.unverifiable),
            SLACK_SIGNATURE_SETUP_HINT,
        )
        return SlackVerdict(False, frozenset(state.unverifiable), compatible=True)
    if state.unreadable:
        _log_rate_limited(
            "slack-unreadable",
            logging.ERROR,
            "Slack delivery refused on %s: the Slack configuration that may hold its "
            "signing secret cannot be decrypted or parsed (%s). Re-enter the signing "
            "secret or restore the SECRET_KEY it was encrypted with; until then its "
            "messages are polled every 5 minutes.",
            SLACK_NOTIFICATION_PATH,
            ", ".join(state.unreadable),
        )
    _log_rate_limited(
        "slack-refused",
        logging.WARNING,
        "Slack delivery refused on %s: it is not signed with the signing secret of any "
        "Slack app configured on the platform (%d Slack integration(s) have none). "
        "Real-time events stay off for them and their new messages are polled every 5 "
        "minutes. %s",
        SLACK_NOTIFICATION_PATH,
        len(state.unknown),
        SLACK_SIGNATURE_SETUP_HINT,
    )
    return SlackVerdict(False)


# ---------------------------------------------------------------------------
# Microsoft Graph subscription records
# ---------------------------------------------------------------------------

GRAPH_SUBSCRIPTION_KEY_PREFIX = "graph_sub:"
# Graph refuses a clientState longer than 128 characters.
GRAPH_CLIENT_STATE_MAX_LENGTH = 128


def graph_subscription_key(subscription_id: str) -> str:
    """The Redis key of the record for a Graph subscription."""
    return f"{GRAPH_SUBSCRIPTION_KEY_PREFIX}{subscription_id}"


def new_graph_client_state() -> str:
    """A fresh ``clientState`` to create a Graph subscription with (256 random bits)."""
    return secrets.token_urlsafe(32)


async def store_graph_subscription(
    redis: Redis,
    subscription_id: str,
    assignment_id: str,
    client_state: str,
    *,
    ttl_seconds: int,
) -> None:
    """Record the assignment a Graph subscription feeds, with the clientState it was created with.

    A caller creating a subscription whose ``notificationUrl`` is the Graph endpoint
    sends ``new_graph_client_state()`` as its ``clientState``, then stores it here with
    the id Graph returns; the notification endpoint refuses every notification that
    does not echo it. A renewal (``PATCH`` of ``expirationDateTime``) cannot change a
    clientState - Graph lets a PATCH set only ``expirationDateTime`` and
    ``notificationUrl`` - so it only extends the record's TTL, and a subscription
    created without one can only gain one by being recreated (create and store the
    new subscription before deleting the old one, so no notification is lost).
    """
    if not subscription_id:
        raise ValueError("a Graph subscription record needs the subscription id")
    if not client_state or len(client_state) > GRAPH_CLIENT_STATE_MAX_LENGTH:
        raise ValueError(f"a Graph clientState is 1 to {GRAPH_CLIENT_STATE_MAX_LENGTH} characters")
    if not is_valid_uuid(str(assignment_id)):
        raise ValueError("a Graph subscription record names an assignment by its id")
    record = {"assignment_id": str(assignment_id), "client_state": encrypt_value(client_state)}
    await redis.set(graph_subscription_key(subscription_id), json.dumps(record), ex=ttl_seconds)


def _parse_graph_record(raw: str | bytes) -> tuple[str, str] | None:
    """``(assignment_id, encrypted clientState)`` from a stored record, if it holds both."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        record = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(record, dict):
        return None
    assignment_id = record.get("assignment_id")
    client_state_enc = record.get("client_state")
    if not (
        isinstance(assignment_id, str)
        and is_valid_uuid(assignment_id)
        and isinstance(client_state_enc, str)
        and client_state_enc
    ):
        return None
    return assignment_id, client_state_enc


async def graph_notification_assignment(
    redis: Redis, notification: dict, *, require_verification: bool | None = None
) -> str | None:
    """The assignment a Graph change notification may trigger, or ``None`` to discard it.

    ``None`` whatever is wrong - no subscription id, no record, an unusable record, an
    unreadable clientState, or a notification that does not echo it - so the endpoint
    answers the same in every case and a caller cannot tell a known subscription id
    from an unknown one. A legacy record holding only the assignment id (the shape
    this endpoint read before notifications were authenticated; no platform code
    writes one) still serves its assignment unless
    ``INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION`` (or *require_verification*) is
    set, with a warning.
    """
    subscription_id = _subscription_id(notification)
    if subscription_id is None:
        return None
    raw = await redis.get(graph_subscription_key(subscription_id))
    return _matched_assignment(notification, _expected_client_state(raw, _strict(require_verification)))


def _strict(require_verification: bool | None) -> bool:
    if require_verification is None:
        return verification_required()
    return require_verification


# One MGET per this many distinct subscriptions of a batch.
GRAPH_RECORD_FETCH_CHUNK = 500


async def graph_notification_assignments(
    redis: Redis, notifications: list[dict], *, require_verification: bool | None = None
) -> list[str | None]:
    """``graph_notification_assignment`` for every notification of a batch, in order.

    Graph documents no batch size, so none is refused; what a batch costs is bounded
    instead: each distinct subscription id is read once, through chunked MGETs, and
    its clientState decrypted once, however many notifications repeat it.
    """
    strict = _strict(require_verification)
    distinct: list[str] = []
    distinct_set: set[str] = set()
    for notification in notifications:
        subscription_id = _subscription_id(notification)
        if subscription_id is not None and subscription_id not in distinct_set:
            distinct_set.add(subscription_id)
            distinct.append(subscription_id)
    expected: dict[str, tuple[str, str | None] | None] = {}
    for start in range(0, len(distinct), GRAPH_RECORD_FETCH_CHUNK):
        chunk = distinct[start : start + GRAPH_RECORD_FETCH_CHUNK]
        raws = await redis.mget([graph_subscription_key(s) for s in chunk])
        for subscription_id, raw in zip(chunk, raws, strict=True):
            expected[subscription_id] = _expected_client_state(raw, strict)
    return [
        _matched_assignment(notification, expected.get(_subscription_id(notification) or ""))
        for notification in notifications
    ]


def _subscription_id(notification: dict) -> str | None:
    subscription_id = notification.get("subscriptionId")
    return subscription_id if isinstance(subscription_id, str) and subscription_id else None


def _legacy_graph_assignment(raw: str | bytes) -> str | None:
    """The assignment id of a legacy record, which holds nothing else."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    raw = raw.strip()
    return raw if is_valid_uuid(raw) else None


def _expected_client_state(raw: object, strict: bool) -> tuple[str, str | None] | None:
    """``(assignment id, clientState)`` of a subscription record, ``None`` when unusable.

    The clientState is ``None`` for a legacy record accepted for compatibility.
    """
    if raw is None:
        return None
    record = _parse_graph_record(raw)
    if record is None:
        legacy = _legacy_graph_assignment(raw) if isinstance(raw, (str, bytes)) else None
        if legacy is not None and not strict:
            log_across_processes(
                "graph-legacy",
                logging.WARNING,
                "Graph change notification on %s accepted without a clientState: the "
                "record of its subscription holds only the assignment id, as records did "
                "before notifications were authenticated, so anyone who knows the "
                "subscription id can trigger it. Recreate the subscription through "
                "store_graph_subscription(), or set "
                "INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION=true to refuse it.",
                GRAPH_NOTIFICATION_PATH,
            )
            return legacy, None
        _log_rate_limited(
            "graph-record",
            logging.WARNING,
            "Graph change notification discarded on %s: the record of its subscription "
            "carries no clientState, so the notification cannot be authenticated. "
            "Recreate the subscription through store_graph_subscription().",
            GRAPH_NOTIFICATION_PATH,
        )
        return None
    assignment_id, client_state_enc = record
    try:
        expected = decrypt_value(client_state_enc)
    except Exception as exc:
        _log_rate_limited(
            "graph-unreadable",
            logging.ERROR,
            "Graph change notification discarded on %s: the clientState of its "
            "subscription cannot be decrypted (%s). Restore the SECRET_KEY it was "
            "encrypted with, or recreate the subscription.",
            GRAPH_NOTIFICATION_PATH,
            format_exception_message(exc),
        )
        return None
    return assignment_id, expected


def _matched_assignment(notification: dict, record: tuple[str, str | None] | None) -> str | None:
    """The record's assignment when *notification* echoes its clientState, else ``None``."""
    if record is None:
        return None
    assignment_id, expected = record
    if expected is None:
        return assignment_id
    provided = notification.get("clientState")
    if not isinstance(provided, str) or not timing_safe_str_eq(provided, expected):
        _log_rate_limited(
            "graph-mismatch",
            logging.WARNING,
            "Graph change notification discarded on %s: its clientState does not match "
            "the one its subscription was created with; it did not come from Microsoft "
            "Graph for that subscription.",
            GRAPH_NOTIFICATION_PATH,
        )
        return None
    return assignment_id
