# SPDX-License-Identifier: Apache-2.0

"""Google SecOps (Chronicle) integration – Agentic SOC for the Google SIEM.

Full-fledged Google Security Operations (formerly Chronicle) integration on top
of the Chronicle REST API, covering UDM threat hunting, entity lookup, IoC
matches, SOAR case management, alerts with their UDM events and triage
feedback, detection rules, retrohunts, and reference lists — designed as a
best-in-class building block for an agentic security team.

Tool categories (30 tools):
  - Hunting: UDM search (events), natural-language → UDM translation, UDM
    query validation, entity lookup (free-text IP / hostname / hash / user →
    asset summary), and enterprise-wide IoC matches
  - Cases (SOAR): list, get, update (priority/stage/assignee), bulk change
    priority, bulk assign, bulk close, bulk reopen, and the case → alerts →
    events triage path
  - Alerts: list SIEM/SOAR alerts, list rule-engine alerts, get alert (with
    its UDM events and observables), update alert feedback
    (severity/priority/status/verdict/comment)
  - Detection rules: list, get, create, update, enable/disable (deployment),
    list detections for a rule
  - Retrohunts: create (run a rule over historical data), list
  - Reference lists: list, get, create, add entries (threat-intel / IOC
    enforcement primitive)

Authentication is a **Google service account** (JSON key). A short-lived OAuth2
access token (scope ``cloud-platform``) is minted by signing a JWT with the
service account's private key and redeeming it at the token endpoint. The
integration stores the service account JSON plus the region, GCP project ID,
and SecOps customer/instance ID; the compound
``region||project||customer||{service_account_json}`` token is assembled by
``integration_tools._get_token``. The API base URL is resolved from ``region``
via :func:`resolve_base_url` (a short name such as ``us``/``europe`` →
``https://{region}-chronicle.googleapis.com/{version}``, or a full base URL
with the version segment normalized), and the resource path is
``projects/{project}/locations/{location}/instances/{customer}`` where
``{location}`` is :func:`resolve_location` — the region normalized to lowercase
(derived from the ``{location}-chronicle`` host label when a full base URL is
supplied).

**API versions are per-endpoint, not global.** Every request names the version
it needs (``_api(..., version=...)``) instead of inheriting one base URL. The
``legacy:`` RPCs and the hunting/entity endpoints are ``v1alpha``-only; the case
collection (``cases.list/get/patch/executeBulk*``, ``cases.caseAlerts.list``) is
served identically on ``v1``, ``v1beta`` and ``v1alpha``, and is pinned here to
``v1beta`` to match Google's own ``secops-wrapper`` SDK.

**Case vs alert identifiers**, the thing this integration got wrong before.
Three ids are in play and they are not interchangeable:
``cases/{case}`` — the SIEM case id, the trailing segment of the case resource
name; ``CaseAlert.caseId`` / ``CaseAlert.identifier`` — the SOAR-side ids shown
in the SOAR UI; and ``CaseAlert.siemAlertId`` — the id the ``legacy:legacyGetAlert``
and ``legacy:legacyUpdateAlert`` RPCs take. The triage path is therefore
case → ``caseAlerts.list`` → ``siemAlertId`` → get/update the alert, and the
tools print every id they return so an agent never has to guess.

**Priority enums differ between cases and alerts**: a case takes ``CasePriority``
(``HIGH``), alert feedback takes the shared ``Priority`` (``PRIORITY_HIGH``).
Both spellings are accepted and normalized per target.

**A write is confirmed, never assumed**. ``legacy:legacyUpdateAlert``
answers 200 for a feedback write, and a 200 is not evidence that the platform
stored what was asked: a customer's agent reported an alert as re-scored while
the SOAR still showed the original severity, and the tool had nothing to
contradict it with because it discarded the response and printed
``"Alert … updated"``.  ``update_secops_alert`` therefore re-reads the alert
after the mutation and reports the platform's own ``feedbackSummary``: fields
it echoes back unchanged are reported as CONFIRMED, fields it echoes back
differently make the call an error, and fields it does not echo at all are
named as unverified rather than counted as success.  ``update_secops_case``
does the same for a case: an independent ``GET cases/{id}`` after the
PATCH, each written field classified, a contradicted priority or stage returned
as an error.

**``severity`` and ``severityDisplay`` are different things.**  The
``LegacyFeedback`` write side takes ``severity`` as a numeric 0-100 *score*;
the ``FeedbackSummary`` read side reports ``severityDisplay`` / ``priorityDisplay``
— the labels an analyst reads in the SOAR alert view — and does not echo the
numeric score at all.  Writing a score therefore cannot be confirmed against a
label, and may not be what moves the display an analyst is looking at, which is
why ``severity_display`` is exposed as its own field and why the read-back
prints the observed labels instead of asserting a mapping between the two.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json as _json
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.http import (
    format_entity_list,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

logger = logging.getLogger(__name__)

_PROVIDER = "Google SecOps"
_DEFAULT_TIMEOUT = 60
_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_TOKEN_SAFETY_WINDOW = 60

# API version segments Chronicle serves, longest-first so that stripping a
# version suffix off a user-supplied base URL never leaves a stray ``alpha``.
_API_VERSIONS = ("v1alpha", "v1beta", "v1")
_DEFAULT_API_VERSION = "v1alpha"

# ``legacy:legacyFetchAlertsView`` is a *streaming* endpoint: it answers with a
# sequence of progressively-filled snapshots and only sets ``complete: true`` on
# the last one. Poll a bounded number of times rather than trusting the first
# (partial) snapshot, and give up with what we have instead of hanging a tool
# call for the full request timeout.
_ALERT_VIEW_MAX_POLLS = 6
_ALERT_VIEW_POLL_INTERVAL = 1.0

_AUTH_ERROR = (
    "Error: Failed to authenticate with Google SecOps. Check that the service "
    "account JSON key is valid and complete, that the region, project ID, and "
    "customer (instance) ID are correct, and that the service account has the "
    "Chronicle API Editor (or Viewer) role."
)

# Reference list syntax types accepted by the API.
_REFLIST_SYNTAX: dict[str, str] = {
    "plain_text": "REFERENCE_LIST_SYNTAX_TYPE_PLAIN_TEXT_STRING",
    "regex": "REFERENCE_LIST_SYNTAX_TYPE_REGEX",
    "cidr": "REFERENCE_LIST_SYNTAX_TYPE_CIDR",
}

# --- Enum vocabularies -----------------------------------------------------
# Cases and alert feedback use *different* priority enums, which is easy to get
# wrong: a case takes CasePriority (bare names) while alert feedback takes the
# shared Priority (PRIORITY_-prefixed). Sending PRIORITY_HIGH to a case is
# rejected. Every tool also accepts the other spelling and normalizes it, so an
# agent that guesses either way still succeeds.
#   https://docs.cloud.google.com/chronicle/docs/reference/rest/v1alpha/CasePriority
#   https://docs.cloud.google.com/chronicle/docs/reference/rest/Shared.Types/Priority
_CASE_PRIORITIES = ("UNCHANGED", "INFORMATIVE", "LOW", "MEDIUM", "HIGH", "CRITICAL")
_ALERT_PRIORITIES = (
    "PRIORITY_UNSPECIFIED",
    "PRIORITY_INFO",
    "PRIORITY_LOW",
    "PRIORITY_MEDIUM",
    "PRIORITY_HIGH",
    "PRIORITY_CRITICAL",
)
_ALERT_STATUSES = ("STATUS_UNSPECIFIED", "NEW", "REVIEWED", "CLOSED", "OPEN")
_ALERT_VERDICTS = ("VERDICT_UNSPECIFIED", "TRUE_POSITIVE", "FALSE_POSITIVE")
_ALERT_REASONS = (
    "REASON_UNSPECIFIED",
    "REASON_NOT_MALICIOUS",
    "REASON_MALICIOUS",
    "REASON_MAINTENANCE",
)
# CloseReasonEnum — note there is no CLOSE_REASON_UNSPECIFIED member here; the
# similarly-named inline enum on CaseClosureDetails is a different type.
_CASE_CLOSE_REASONS = (
    "MALICIOUS",
    "NOT_MALICIOUS",
    "MAINTENANCE",
    "INCONCLUSIVE",
    "UNKNOWN",
)

# --- Filter / query syntax help -------------------------------------------
# Surfaced both in the tool descriptions and appended to an API error, because
# a rejected filter otherwise comes back as an opaque 400 with no hint at the
# syntax the endpoint actually wants. Field lists are the ones
# cases.list / caseAlerts.list document as filterable — notably `status` is NOT
# a case filter field (it is `case_data_state`), though it *is* one on alerts.
_CASE_FILTER_HELP = (
    "Case filter syntax: `field='VALUE'`, combined with AND / OR. Filterable "
    "fields: displayName, assignee, assignedUser, creatorUser, "
    "lastModifyingUser, stage, priority, important, type, environment, "
    "case_data_state, score, tags, products, sla, alertsSla, closureDetails, "
    "tasks. Examples: `priority='HIGH'`, `stage='Triage' AND important=true`. "
    "Order with order_by, e.g. `priority desc, displayName`."
)
_CASE_ALERT_FILTER_HELP = (
    "Case-alert filter syntax: `field='VALUE'`. Filterable fields: displayName, "
    "caseId, identifier, sourceGroupingIdentifier, product, vendor, environment, "
    "ticketId, sourceSystemName, creation_method, priority, status, startTime, "
    "endTime. Example: `priority='HIGH'`."
)
_ALERT_SNAPSHOT_HELP = (
    "Alert snapshot query syntax (UDM-like): "
    '`feedback_summary.status != "CLOSED"`, `feedback_summary.priority = '
    '"PRIORITY_HIGH"`, `detection.rule_name = "My Rule"`, `case_name = "CASE"`. '
    "Combine with AND / OR."
)

# Entity-value shapes recognised by lookup_secops_entity, mirroring the
# detection order of Google's own SDK: the first pattern that matches wins, so
# the more specific shapes (hash, email, MAC, domain) are tried before the
# catch-all hostname / user-id ones. IPs are detected separately with
# ``ipaddress`` — an IPv6 regex would also swallow every MAC address.
_ENTITY_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"^[a-fA-F0-9]{32}$|^[a-fA-F0-9]{40}$|^[a-fA-F0-9]{64}$", "hash", "FILE"),
    (r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$", "email", "USER"),
    (r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$", "mac", "ASSET"),
    (
        r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
        r"(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)+$",
        "domain",
        "DOMAIN_NAME",
    ),
    (r"^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$", "hostname", "ASSET"),
    (r"^[a-zA-Z0-9_.-]+$", "user.userid", "USER"),
)


# ---------------------------------------------------------------------------
# Cloud + auth helpers
# ---------------------------------------------------------------------------


def resolve_base_url(region: str, version: str = _DEFAULT_API_VERSION) -> str:
    """Resolve a SecOps region (or full URL) to an API base URL for *version*.

    ``version`` is one of ``v1alpha`` / ``v1beta`` / ``v1``. A full base URL
    supplied as the region keeps its host but has its version segment
    *replaced*, so the same configured URL can serve the v1alpha hunting
    endpoints and the v1beta case collection.
    """
    raw = (region or "us").strip()
    if raw.lower().startswith("http"):
        # Only the scheme test and the version-suffix match are
        # case-insensitive: a full base URL may point at a reverse proxy whose
        # path segments are case-sensitive, so its casing is preserved rather
        # than lowercased along with the region short names below.
        base = raw.rstrip("/")
        for known in _API_VERSIONS:
            if base.lower().endswith(f"/{known}"):
                base = base[: -(len(known) + 1)]
                break
        return f"{base}/{version}"
    return f"https://{raw.lower()}-chronicle.googleapis.com/{version}"


def resolve_location(region: str) -> str:
    """Resolve the SecOps location id used in the instance resource path.

    Normalized **separately** from :func:`resolve_base_url` so the resource path
    (``locations/{location}``) never carries a wrong-case region (e.g. the user
    typed ``EUROPE``) or a full base URL. Accepts a region short name (``us``,
    ``europe``, ``europe-west2``) or a full base URL — for a URL the location is
    derived from the ``{location}-chronicle`` host label. Always lowercase;
    falls back to ``us``.
    """
    r = (region or "us").strip().lower()
    if r.startswith("http"):
        host = urlparse(r).hostname or ""
        label = host.split(".", 1)[0]
        if label.endswith("-chronicle"):
            label = label[: -len("-chronicle")]
        return label or "us"
    return r or "us"


async def _request_access_token(service_account_json: str) -> tuple[str | None, int]:
    """Sign a JWT with the service account key and redeem it for an access token.

    Returns ``(access_token, expires_in_seconds)`` or ``(None, 0)`` on failure.
    """
    try:
        sa = _json.loads(service_account_json)
    except (ValueError, TypeError) as exc:
        logger.warning("Google SecOps: invalid service account JSON: %s", format_exception_message(exc))
        return None, 0
    client_email = sa.get("client_email")
    private_key = sa.get("private_key")
    if not client_email or not private_key:
        logger.warning(
            "Google SecOps: service account JSON missing client_email/private_key."
        )
        return None, 0
    token_uri = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
    now = int(time.time())
    headers = {"kid": sa["private_key_id"]} if sa.get("private_key_id") else None
    try:
        assertion = jwt.encode(
            {
                "iss": client_email,
                "scope": _SCOPE,
                "aud": token_uri,
                "iat": now,
                "exp": now + 3600,
            },
            private_key,
            algorithm="RS256",
            headers=headers,
        )
    except Exception as exc:
        logger.warning("Google SecOps: failed to sign JWT assertion: %s", format_exception_message(exc))
        return None, 0
    try:
        async with egress_client(timeout=20) as client:
            resp = await client.post(
                token_uri,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            if resp.is_success:
                data = resp.json()
                return data.get("access_token"), int(data.get("expires_in") or 3600)
            logger.warning(
                "Google SecOps token request failed (%s): %s",
                resp.status_code,
                resp.text[:300],
            )
    except httpx.HTTPError as exc:
        logger.warning("Google SecOps token request error: %s", format_exception_message(exc))
    return None, 0


async def validate_connection(
    region: str, project: str, customer: str, service_account_json: str
) -> tuple[bool, str]:
    """Verify SecOps credentials by minting an OAuth2 access token.

    Returns ``(ok, message)``. Used by the integration connectivity test.
    """
    if not all([region, project, customer, service_account_json]):
        return False, (
            "Missing region, project ID, customer ID, or service account JSON."
        )
    token, _ = await _request_access_token(service_account_json)
    if token:
        return True, "Google SecOps is connected and authenticated."
    return False, (
        "Authentication failed — check the service account JSON key and that the "
        "Chronicle API is enabled with the correct role."
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("…" if len(text) > max_len else "")


def _dt(value: Any) -> str:
    """Render a Chronicle timestamp.

    Most of the API uses RFC 3339 strings, but the ``Case`` resource types
    ``createTime`` / ``updateTime`` as an **int64 of milliseconds** — printing
    that raw gives the reader ``1714557600000``, so epoch values are converted
    rather than truncated.
    """
    if value is None or value == "":
        return ""
    text = str(value)
    if text.isdigit():
        try:
            seconds = int(text) / (1000 if len(text) > 11 else 1)
            return _rfc3339(datetime.fromtimestamp(seconds, UTC))
        except (ValueError, OverflowError, OSError):
            return text
    return text[:19]


def _rfc3339(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_range(hours: int) -> tuple[str, str]:
    now = datetime.now(UTC)
    return _rfc3339(now - timedelta(hours=max(hours, 1))), _rfc3339(now)


def _short_name(resource_name: str) -> str:
    """Return the trailing id segment of a full resource name."""
    return str(resource_name or "").rstrip("/").rsplit("/", 1)[-1]


def _resource_id(value: Any) -> str:
    """Normalize an identifier that may arrive as a full resource name.

    Chronicle hands out case and alert identifiers in two shapes: a bare id
    (``12345``, or a UUID) and a full resource name
    (``projects/…/instances/…/cases/12345``). Agents copy whichever one they
    last saw, so every tool that takes an id runs it through here — the two are
    interchangeable at the call site instead of silently 404-ing.
    """
    return str(value or "").strip().rstrip("/").rsplit("/", 1)[-1]


def _quote(value: str) -> str:
    """Escape a value for embedding in a double-quoted Chronicle query literal."""
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _resolve_enum(value: str, allowed: tuple[str, ...], prefix: str = "") -> str | None:
    """Normalize *value* to a member of *allowed*, or ``None`` if unknown.

    Accepts the bare spelling when *prefix* is given (``high`` →
    ``PRIORITY_HIGH``) as well as the fully-qualified one, and tolerates
    hyphens/spaces in place of underscores.
    """
    v = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not v:
        return None
    if v in allowed:
        return v
    if prefix and f"{prefix}{v}" in allowed:
        return f"{prefix}{v}"
    return None


def _resolve_case_priority(value: str) -> str | None:
    """Normalize to ``CasePriority``, the bare-name enum a *case* takes.

    Accepts the alert-side ``PRIORITY_HIGH`` spelling too and strips the prefix:
    the two enums look interchangeable and are not, so guessing either way still
    lands on a value the API accepts.
    """
    v = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    v = v.removeprefix("PRIORITY_")
    if v == "INFO":
        v = "INFORMATIVE"
    return v if v in _CASE_PRIORITIES else None


def _enum_error(label: str, allowed: tuple[str, ...]) -> str:
    return f"Error: unknown {label}. Valid values: {', '.join(allowed)}."


# Feedback fields the alert read-back can confirm, mapped to the enum prefix
# each one travels with.  A field absent from this map is NOT comparable
# against ``feedbackSummary`` — ``severity`` and ``confidenceScore`` are
# write-side numeric scores the summary never echoes — and is reported as
# unverified rather than silently counted as applied.
_FEEDBACK_ECHO_PREFIXES: dict[str, str] = {
    "priority": "PRIORITY_",
    # ``_ALERT_STATUSES`` carries ``STATUS_UNSPECIFIED``, which is this file's
    # own evidence that the family prefix is ``STATUS_``.  Without it a platform
    # answering ``STATUS_REVIEWED`` to a written ``REVIEWED`` reads as a refusal.
    # It cannot cause a false match: no other member starts with ``STATUS_``.
    "status": "STATUS_",
    "verdict": "VERDICT_",
    "reason": "REASON_",
    "riskScore": "",
    "comment": "",
    "rootCause": "",
    "severityDisplay": "",
}


# Of the fields the summary echoes, only these may be reported as a REFUSAL.
# Each is a closed enum this module resolves before sending, so the platform has
# nothing to normalise and a difference is a real disagreement.
#
# The others — ``riskScore``, ``comment``, ``rootCause``, ``severityDisplay`` —
# echo values the platform is free to reshape: trimmed, re-wrapped, prefixed or
# timestamped free text, a clamped or rounded score, a relabelled display
# string.  An equal echo still CONFIRMS them; a different one is reported as an
# observation and never as an error, because an ``Error:``-prefixed result feeds
# the per-tool error counters and the consecutive-error brake, and
# a playbook writing a comment on every alert would brake itself out of the tool
# over a server-side trim.  Claiming a failure this comparison cannot support is
# the same unearned claim this read-back exists to remove, with the sign flipped.
#
# ``severityDisplay`` is deliberately NOT contradictable even though it is the
# field one would most expect here: it is brand-new and opt-in, so no tenant has
# ever echoed it back to us.  It is the last field that should be able to emit a
# hard error, and it joins this set once someone validates its round-trip on a
# real instance.
_CONTRADICTABLE_FEEDBACK_KEYS = frozenset({"priority", "status", "verdict", "reason"})

# How deep the post-write SOAR lookup scans a case's alerts for the row an
# analyst reads.  ``caseAlerts.list`` cannot be filtered on ``siemAlertId``
# (``_CASE_ALERT_FILTER_HELP`` lists what is filterable, and it is not there),
# so the row is found by scanning — bounded, because this runs on every
# confirmed write and each entry carries its expanded relations.
_SOAR_ROW_SCAN_LIMIT = 200


# Case fields a read-back may report as a REFUSAL: ``priority`` is a closed enum
# this module resolves before sending, and ``stage`` is sent verbatim from the
# instance's own stage list, so a different value is a real disagreement.
# ``assignee`` / ``displayName`` may be normalized by the platform (a user id for
# a username, trimmed text), so a difference there is only an observation.
_CONTRADICTABLE_CASE_KEYS = frozenset({"priority", "stage"})


def _observed_case_priority(value: Any) -> str:
    """A case priority as read back, in ``CasePriority`` spelling.

    The platform answers ``PriorityHigh``, ``PRIORITY_HIGH`` or ``HIGH`` for a
    written ``HIGH``; all three must compare equal.
    """
    v = str(value or "").strip().upper().replace("-", "_").replace(" ", "_")
    v = v.removeprefix("PRIORITY_").removeprefix("PRIORITY")
    return "INFORMATIVE" if v == "INFO" else v


def _case_value_matches(key: str, expected: Any, observed: Any) -> bool:
    if key == "priority":
        return _observed_case_priority(observed) == str(expected).upper()
    return str(observed).strip().casefold() == str(expected).strip().casefold()


def _feedback_matches(expected: Any, observed: Any, prefix: str = "") -> bool:
    """True when the platform's echoed feedback value means what was written.

    Enum values travel with or without their family prefix (``PRIORITY_LOW``
    vs ``LOW``), so *prefix* — the family this field belongs to, never a
    guess — is stripped from both sides before comparing.  Stripping a generic
    trailing segment instead would collapse ``TRUE_POSITIVE`` and
    ``FALSE_POSITIVE`` onto the same value and report the platform's verdict as
    confirming the opposite one.

    A numeric expectation is compared numerically: the API is free to answer a
    score as ``5`` or ``"5"``, and a string comparison would report a correctly
    stored value as a contradiction — the one failure mode this whole check
    exists to avoid manufacturing.
    """
    if isinstance(expected, int) and not isinstance(expected, bool):
        try:
            # Through ``float`` first: protobuf-JSON is free to render an
            # integer field as ``5.0``, and ``int("5.0")`` raises — a crash path
            # that would surface as a false "not applied".
            return int(float(str(observed).strip())) == expected
        except (TypeError, ValueError):
            return False

    def _norm(value: Any) -> str:
        text = str(value if value is not None else "").strip()
        if prefix and text.upper().startswith(prefix):
            text = text[len(prefix) :]
        return text.casefold()

    return _norm(expected) == _norm(observed)


def _id_list(raw: str) -> list[str]:
    """Split a comma/newline-separated list of ids, normalizing resource names."""
    parts = [p.strip() for p in str(raw or "").replace(",", "\n").splitlines()]
    return [_resource_id(p) for p in parts if p.strip()]


def _numeric_ids(ids: list[str]) -> list[Any]:
    """Coerce all-digit ids to ``int`` — the bulk case RPCs type them as int64."""
    return [int(i) if i.isdigit() else i for i in ids]


def _parse_stream_payload(text: str) -> dict[str, Any]:
    """Merge a Chronicle streaming response body into a single object.

    ``legacyFetchAlertsView`` does not answer with one JSON document: the body
    is either a top-level array of snapshots or several concatenated objects
    separated by a newline. Chunks are merged the way Google's own SDK merges
    them — nested dicts updated, lists extended, scalars overwritten by the
    later chunk. Callers dedupe list entries afterwards, since a server that
    resends a full snapshot per chunk would otherwise repeat them.
    """
    raw = (text or "").strip()
    if not raw:
        return {}
    try:
        parsed: Any = _json.loads(raw)
    except ValueError:
        try:
            parsed = _json.loads("[" + raw.replace("}\n{", "},\n{") + "]")
        except ValueError:
            logger.warning(
                "Google SecOps: could not parse a streaming response body (%d chars).",
                len(raw),
            )
            return {}
    chunks = parsed if isinstance(parsed, list) else [parsed]
    merged: dict[str, Any] = {}
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        for key, value in chunk.items():
            current = merged.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                current.update(value)
            elif isinstance(current, list) and isinstance(value, list):
                current.extend(value)
            else:
                merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# UDM event extraction — the triage payload the SIEM hangs off an alert
# ---------------------------------------------------------------------------


def _collect_udm_events(node: Any) -> list[dict[str, Any]]:
    """Collect the UDM events attached to an alert / detection payload.

    Chronicle hangs the matched events off two different shapes depending on
    the endpoint: ``collectionElements[].references[].event`` (alerts and
    detections) and ``resultEvents[<match variable>].eventSamples[].event``
    (rule alerts). Both are walked, at the top level and inside a nested
    ``detection`` list, so callers hand over the whole alert object and get the
    IP / hostname / OS / user context an analyst actually needs — which
    ``get_secops_alert`` used to drop on the floor.
    """
    events: list[dict[str, Any]] = []
    if not isinstance(node, dict):
        return events

    scopes: list[dict[str, Any]] = [node]
    detection = node.get("detection")
    if isinstance(detection, list):
        scopes.extend(d for d in detection if isinstance(d, dict))

    for scope in scopes:
        # The Collection reference renders its fields as proto snake_case while
        # a real protobuf-JSON response emits lowerCamelCase, so accept both
        # rather than betting on which one the wire actually carries.
        elements = scope.get("collectionElements") or scope.get("collection_elements")
        for element in elements or []:
            if not isinstance(element, dict):
                continue
            for ref in element.get("references") or []:
                event = ref.get("event") if isinstance(ref, dict) else None
                if isinstance(event, dict):
                    events.append(event)
        result_events = scope.get("resultEvents") or scope.get("result_events")
        if isinstance(result_events, dict):
            for group in result_events.values():
                if not isinstance(group, dict):
                    continue
                for sample in group.get("eventSamples") or []:
                    event = sample.get("event") if isinstance(sample, dict) else None
                    if isinstance(event, dict):
                        events.append(event)
    return events


def _platform(noun: dict[str, Any]) -> str:
    """Operating system of a UDM noun, from either the noun or its asset."""
    software = (noun.get("asset") or {}).get("platformSoftware") or {}
    platform = noun.get("platform") or software.get("platform") or ""
    version = noun.get("platformVersion") or software.get("platformVersion") or ""
    return f"{platform} {version}".strip()


def _first_ip(noun: dict[str, Any]) -> str:
    ips = noun.get("ip") or (noun.get("asset") or {}).get("ip") or []
    if isinstance(ips, str):
        return ips
    return str(ips[0]) if ips else ""


def _noun_summary(noun: Any) -> str:
    """One-line summary of a UDM noun (principal / target / src)."""
    if not isinstance(noun, dict) or not noun:
        return ""
    asset = noun.get("asset") or {}
    bits = [
        noun.get("hostname") or asset.get("hostname") or "",
        _first_ip(noun),
        noun.get("url") or "",
        _platform(noun),
    ]
    user = (noun.get("user") or {}).get("userid") or ""
    if user:
        bits.append(f"user: {user}")
    process = noun.get("process") or {}
    command = process.get("commandLine") or (process.get("file") or {}).get("fullPath")
    if command:
        bits.append(f"process: {_trunc(command, 120)}")
    file_hash = (noun.get("file") or {}).get("sha256") or ""
    if file_hash:
        bits.append(f"sha256: {file_hash}")
    return " | ".join(b for b in bits if b)


def _format_udm_event(event: dict[str, Any]) -> str:
    """Render a UDM event as the two-to-three lines an analyst reads first."""
    meta = event.get("metadata") or {}
    lines = [
        f"  [{_dt(meta.get('eventTimestamp'))}] {meta.get('eventType', 'EVENT')}"
        + (f" — {meta.get('productName')}" if meta.get("productName") else "")
    ]
    for label in ("principal", "src", "target"):
        summary = _noun_summary(event.get(label))
        if summary:
            lines.append(f"    {label}: {summary}")
    results = event.get("securityResult") or []
    first = results[0] if isinstance(results, list) and results else {}
    if isinstance(first, dict) and (first.get("summary") or first.get("severity")):
        actions = first.get("action") or []
        action = ", ".join(actions) if isinstance(actions, list) else str(actions)
        lines.append(
            f"    security_result: {first.get('summary') or first.get('ruleName') or 'N/A'}"
            + (f" [{first.get('severity')}]" if first.get("severity") else "")
            + (f" action={action}" if action else "")
        )
    return "\n".join(lines)


_INCOMPLETE_VIEW_NOTE = (
    "\n\n[Partial — the alerts view was still filling in when the poll budget ran "
    "out. Narrow the time range or re-run for the complete set.]"
)


def _format_view_alert(alert: dict[str, Any]) -> str:
    """One-line summary of an alert returned by ``legacyFetchAlertsView``."""
    detections = alert.get("detection") or []
    first = detections[0] if isinstance(detections, list) and detections else {}
    feedback = alert.get("feedbackSummary") or {}
    rule = (
        first.get("ruleName")
        or first.get("threatName")
        or alert.get("ruleName")
        or alert.get("type")
        or "Detection"
    )
    severity = feedback.get("severityDisplay") or first.get("severity") or "N/A"
    case_name = _short_name(alert.get("caseName")) if alert.get("caseName") else ""
    event_count = len(_collect_udm_events(alert))
    return (
        f"  {rule}\n"
        f"    alert_id: {alert.get('id', '?')} | "
        f"Status: {feedback.get('status', 'N/A')} | "
        f"Priority: {feedback.get('priority', 'N/A')} | "
        f"Severity: {severity}\n"
        f"    Created: {_dt(alert.get('createdTime'))}"
        + (f" | Case: {case_name}" if case_name else "")
        + (f" | Verdict: {feedback['verdict']}" if feedback.get("verdict") else "")
        + (f" | Events: {event_count}" if event_count else "")
    )


def _relation_entities(relations: Any) -> list[str]:
    """Deduplicated entity identifiers from a CaseAlert's ``involvedRelations``.

    Each relation links a ``from`` EntityKey to a ``to`` EntityKey; the pair is
    what the SOAR UI shows as the alert's involved entities.
    """
    out: list[str] = []
    for relation in relations or []:
        if not isinstance(relation, dict):
            continue
        for side in ("from", "to"):
            key = relation.get(side)
            if not isinstance(key, dict):
                continue
            name = key.get("identifier") or key.get("name") or ""
            kind = key.get("type") or key.get("entityType") or ""
            if name:
                out.append(f"{name} [{kind}]" if kind else str(name))
    return list(dict.fromkeys(out))


def _observables(events: list[dict[str, Any]]) -> str:
    """Deduplicated indicators across *events*, ready to pivot on.

    This is the digest an agent feeds to OpenAEV / GTI: the distinct hostnames,
    IPs, users, domains/URLs and hashes seen anywhere in the alert's events,
    rather than making it re-read every event to reconstruct them.
    """
    buckets: dict[str, list[str]] = {
        "Hostnames": [],
        "IPs": [],
        "Users": [],
        "Domains/URLs": [],
        "Hashes": [],
        "Platforms": [],
    }
    for event in events:
        for label in ("principal", "src", "target", "observer", "intermediary"):
            noun = event.get(label)
            if not isinstance(noun, dict):
                continue
            asset = noun.get("asset") or {}
            buckets["Hostnames"] += [
                h for h in (noun.get("hostname"), asset.get("hostname")) if h
            ]
            for source in (noun.get("ip"), asset.get("ip")):
                if isinstance(source, str):
                    buckets["IPs"].append(source)
                elif isinstance(source, list):
                    buckets["IPs"] += [str(i) for i in source if i]
            user = (noun.get("user") or {}).get("userid")
            if user:
                buckets["Users"].append(user)
            domain = noun.get("domain") if isinstance(noun.get("domain"), dict) else {}
            buckets["Domains/URLs"] += [
                d for d in (noun.get("url"), domain.get("name")) if d
            ]
            file_info = noun.get("file") or {}
            buckets["Hashes"] += [
                h
                for h in (
                    file_info.get("sha256"),
                    file_info.get("sha1"),
                    file_info.get("md5"),
                )
                if h
            ]
            platform = _platform(noun)
            if platform:
                buckets["Platforms"].append(platform)

    lines = []
    for label, values in buckets.items():
        unique = list(dict.fromkeys(values))
        if unique:
            shown = ", ".join(unique[:20])
            more = f" (+{len(unique) - 20} more)" if len(unique) > 20 else ""
            lines.append(f"  {label}: {shown}{more}")
    return "\n".join(lines)


def _format_entity(entity: dict[str, Any]) -> list[str]:
    """Render a Chronicle entity (asset / file / user) as indented lines."""
    metadata = entity.get("metadata") or {}
    metric = entity.get("metric") or {}
    noun = entity.get("entity") or {}
    asset = noun.get("asset") or {}
    file_info = noun.get("file") or {}
    user = noun.get("user") or {}

    identity = (
        asset.get("hostname")
        or _first_ip(noun)
        or file_info.get("sha256")
        or file_info.get("md5")
        or user.get("userid")
        or (noun.get("domain") or {}).get("name")
        or _short_name(entity.get("name"))
        or "N/A"
    )
    lines = [f"  {identity} [{metadata.get('entityType', 'UNKNOWN')}]"]
    detail = " | ".join(
        part
        for part in (
            f"IP: {_first_ip(noun)}" if _first_ip(noun) else "",
            f"OS: {_platform(noun)}" if _platform(noun) else "",
            f"MAC: {(asset.get('mac') or [''])[0]}" if asset.get("mac") else "",
            f"User: {user.get('userid')}" if user.get("userid") else "",
        )
        if part
    )
    if detail:
        lines.append(f"    {detail}")
    if metric.get("firstSeen") or metric.get("lastSeen"):
        lines.append(
            f"    First seen: {_dt(metric.get('firstSeen'))} | "
            f"Last seen: {_dt(metric.get('lastSeen'))}"
        )
    return lines


def _detect_entity_query(value: str) -> tuple[str, str]:
    """Map a free-text entity value to a Chronicle entity query + preferred type.

    ``lookup_secops_entity`` exists because a bare hostname or IP is *not* a
    valid UDM query — feeding one to ``run_secops_udm_search`` returns nothing
    even for an asset that plainly exists, which is what the free-text search
    complaint was.
    """
    v = str(value or "").strip()
    try:
        ipaddress.ip_address(v)
    except ValueError:
        pass
    else:
        return f'ip = "{_quote(v)}"', "ASSET"
    for pattern, field, entity_type in _ENTITY_PATTERNS:
        if re.match(pattern, v):
            return f'{field} = "{_quote(v)}"', entity_type
    return f'string_value = "{_quote(v)}"', "ASSET"


def _truncation_note(data: Any, flag: str, hint: str) -> str:
    """Return a one-line truncation note when *flag* is truthy in *data*.

    These tools query the Chronicle search endpoints WITHOUT following
    pagination: each caps the endpoint's own limit parameter (``limit`` for UDM
    search, ``maxMatchesToReturn`` for IoC matches, ``maxNumAlertsToReturn`` for
    rule alerts, ``pageSize`` for legacy detections) and leaves any page cursor
    unfollowed. A capped result is surfaced from whichever truncation signal the
    endpoint returns: a boolean (``moreDataAvailable`` for UDM search / IoC
    matches, ``tooManyAlerts`` for rule alerts) or a ``nextPageToken`` pagination
    cursor (legacy detections) that we treat as a "there is more" flag rather
    than chasing it. Surfacing the cap tells the agent to narrow the time range /
    add filters instead of silently trusting a partial result.
    """
    if isinstance(data, dict) and data.get(flag):
        return f"\n\n[Truncated — {hint}.]"
    return ""


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Google SecOps tools.

    ``token`` is the compound ``region||project||customer||service_account_json``
    assembled by ``integration_tools._get_token``. The service account JSON is
    the final segment (split with maxsplit so embedded delimiters are safe).
    """
    parts = token.split("||", 3)
    region = parts[0] if len(parts) > 0 else ""
    project = parts[1] if len(parts) > 1 else ""
    customer = parts[2] if len(parts) > 2 else ""
    sa_json = parts[3] if len(parts) > 3 else ""

    # One base URL per API version: hunting/detection/legacy live on v1alpha,
    # the case collection only on v1beta (see the module docstring).
    base_urls = {v: resolve_base_url(region, v) for v in _API_VERSIONS}
    location = resolve_location(region)
    instance = f"projects/{project}/locations/{location}/instances/{customer}"
    instance_path = f"/{instance}"

    _token_state: dict[str, Any] = {"token": None, "exp": 0.0}

    async def _access_token(*, force: bool = False) -> str | None:
        now = time.monotonic()
        cached = _token_state["token"]
        if not force and cached and _token_state["exp"] > now:
            return cached
        access, ttl = await _request_access_token(sa_json)
        if access:
            _token_state["token"] = access
            _token_state["exp"] = now + max(ttl - _TOKEN_SAFETY_WINDOW, 30)
        return access

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        timeout: int = _DEFAULT_TIMEOUT,
        version: str = _DEFAULT_API_VERSION,
        stream: bool = False,
    ) -> tuple[dict | None, str | None]:
        """Authenticated Chronicle request returning ``(json, error)``.

        ``path`` is appended to the base URL for *version* (it should start with
        the instance path, e.g. ``/projects/.../instances/...:udmSearch``).
        Set ``stream=True`` for the endpoints that answer with several
        concatenated JSON snapshots instead of one document.
        """
        if not region or not project or not customer or not sa_json:
            return None, _AUTH_ERROR
        access_token = await _access_token()
        if not access_token:
            return None, _AUTH_ERROR
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                method,
                f"{base_urls[version]}{path}",
                _PROVIDER,
                action,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                params=params,
                json=json,
            )
            if err:
                return None, err
            if stream:
                return _parse_stream_payload(getattr(resp, "text", "") or ""), None
            try:
                return resp.json(), None
            except Exception:
                return {}, None

    async def _alerts_view(
        snapshot_query: str, hours: int, count: int, action: str
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """Fetch SIEM/SOAR alerts via ``legacyFetchAlertsView``.

        Returns ``(alerts, error, complete)``. The endpoint streams
        progressively-filled snapshots, so it is polled until it reports
        ``complete`` or the poll budget runs out — a caller that ignored this
        would show a half-filled alert list as if it were the whole answer.
        Alerts are deduplicated by id because a snapshot may repeat entries
        already seen in an earlier chunk.
        """
        start, end = _time_range(hours)
        params: dict[str, Any] = {
            "timeRange.startTime": start,
            "timeRange.endTime": end,
            "snapshotQuery": snapshot_query,
            "alertListOptions.maxReturnedAlerts": min(max(count, 1), 1000),
        }
        data: dict[str, Any] = {}
        complete = False
        for attempt in range(_ALERT_VIEW_MAX_POLLS):
            if attempt:
                await asyncio.sleep(_ALERT_VIEW_POLL_INTERVAL)
            data, err = await _api(
                "get",
                f"{instance_path}/legacy:legacyFetchAlertsView",
                action,
                params=params,
                timeout=120,
                stream=True,
            )
            if err:
                return [], err, False
            data = data or {}
            # An invalid snapshot query is not an HTTP error: the endpoint
            # answers 200 with validSnapshotQuery=false and an empty alert
            # list, which would otherwise read as "nothing matched".
            if data.get("validSnapshotQuery") is False:
                problems = "; ".join(
                    str(e.get("message") or e)
                    for e in (data.get("queryValidationErrors") or [])
                    if e
                )
                return (
                    [],
                    "Error: the snapshot query was rejected by Google SecOps"
                    + (f" — {problems}" if problems else "")
                    + f".\n\n{_ALERT_SNAPSHOT_HELP}",
                    False,
                )
            if data.get("complete"):
                complete = True
                break

        raw = (data.get("alerts") or {}).get("alerts") or []
        seen: set[str] = set()
        alerts: list[dict[str, Any]] = []
        for alert in raw:
            if not isinstance(alert, dict):
                continue
            key = str(alert.get("id") or alert.get("name") or len(alerts))
            if key in seen:
                continue
            seen.add(key)
            alerts.append(alert)
        return alerts[:count], None, complete

    # ══════════════════════════════════════════════════════════════════════
    # 0. HUNTING — UDM search + IoC matches
    # ══════════════════════════════════════════════════════════════════════

    async def run_secops_udm_search(
        query: str, hours: int = 24, count: int = 50
    ) -> str:
        """Run a UDM search for events matching a query over the last N hours.

        ``query`` is a Google SecOps UDM query, e.g.
        ``principal.hostname = "host1" AND metadata.event_type = "PROCESS_LAUNCH"``.
        """
        if not query or not query.strip():
            return "Error: a UDM query string is required."
        start, end = _time_range(hours)
        data, err = await _api(
            "get",
            f"{instance_path}:udmSearch",
            "run UDM search",
            params={
                "query": query,
                "timeRange.startTime": start,
                "timeRange.endTime": end,
                "limit": min(count, 1000),
            },
            timeout=120,
        )
        if err:
            return err
        events = (data or {}).get("events", []) if isinstance(data, dict) else []
        if not events:
            return "UDM search returned no events."

        lines = [f"UDM search results ({len(events)} events):"]
        for ev in events[:count]:
            udm = ev.get("udm", ev) if isinstance(ev, dict) else {}
            meta = udm.get("metadata", {}) or {}
            principal = udm.get("principal", {}) or {}
            target = udm.get("target", {}) or {}
            lines.append(
                f"  [{_dt(meta.get('eventTimestamp'))}] "
                f"{meta.get('eventType', 'EVENT')} — "
                f"principal: {principal.get('hostname') or principal.get('ip') or 'N/A'} "
                f"→ target: {target.get('hostname') or target.get('ip') or target.get('url') or 'N/A'}"
            )
        return "\n".join(lines) + _truncation_note(
            data, "moreDataAvailable", "narrow the time range or refine the query"
        )

    async def translate_secops_udm_query(text: str) -> str:
        """Translate a natural-language description into a UDM search query."""
        if not text or not text.strip():
            return "Error: a natural-language description is required."
        data, err = await _api(
            "post",
            f"{instance_path}:translateUdmQuery",
            "translate UDM query",
            json={"text": text},
        )
        if err:
            return err
        if not isinstance(data, dict):
            return "Translation failed (no response)."
        query = data.get("query")
        if not query:
            message = data.get("message") or "the request could not be translated."
            return f"No UDM query was produced — {message}"
        lines = [f"UDM query:\n{query}"]
        tr = data.get("timeRange") or {}
        if tr.get("startTime") or tr.get("endTime"):
            lines.append(
                f"\nSuggested time range: {_dt(tr.get('startTime'))} → "
                f"{_dt(tr.get('endTime'))}"
            )
        if data.get("message"):
            lines.append(f"\nNote: {data.get('message')}")
        return "\n".join(lines)

    async def validate_secops_udm_query(query: str) -> str:
        """Validate (compile) a UDM search query without running it."""
        if not query or not query.strip():
            return "Error: a UDM query string is required."
        data, err = await _api(
            "get",
            f"{instance_path}:validateQuery",
            "validate UDM query",
            params={"rawQuery": query},
        )
        if err:
            return err
        if not isinstance(data, dict) or not data.get("errorText"):
            return "Valid: the UDM query compiled successfully."
        pos = data.get("errorPosition") or {}
        where = ""
        if pos.get("startLine"):
            where = (
                f" (line {pos.get('startLine')}, "
                f"column {pos.get('startColumn', '?')})"
            )
        return (
            f"Invalid UDM query{where}: {data.get('errorText')}"
            + (f" [{data.get('errorType')}]" if data.get("errorType") else "")
        )

    async def list_secops_iocs(hours: int = 24, count: int = 25) -> str:
        """List enterprise-wide IoC matches against ingested events."""
        start, end = _time_range(hours)
        data, err = await _api(
            "get",
            f"{instance_path}/legacy:legacySearchEnterpriseWideIoCs",
            "list IoC matches",
            params={
                # This legacy endpoint binds the time window to ``timestampRange``
                # (not ``timeRange``) and requires both ``maxMatchesToReturn`` and
                # ``addMandiantAttributes``; omitting either is rejected as an
                # unknown / missing query parameter.
                "timestampRange.startTime": start,
                "timestampRange.endTime": end,
                "maxMatchesToReturn": min(count, 100),
                "addMandiantAttributes": False,
            },
        )
        if err:
            return err
        iocs = (data or {}).get("matches", []) if isinstance(data, dict) else []

        def _fmt(i: dict) -> str:
            artifact = i.get("artifactIndicator") or {}
            indicator = (
                i.get("id")
                or artifact.get("domainName")
                or artifact.get("destinationIpAddress")
                or artifact.get("ipAddress")
                or "N/A"
            )
            categories = i.get("categories")
            category = (
                str(categories[0])
                if isinstance(categories, list) and categories
                else ""
            )
            extra = " | ".join(
                p
                for p in (
                    i.get("feedName") or "",
                    category,
                    i.get("confidenceBucket") or "",
                )
                if p
            )
            return (
                f"  {indicator} — "
                f"First seen: {_dt(i.get('firstSeenTimestamp'))} | "
                f"Last seen: {_dt(i.get('lastSeenTimestamp'))}"
                + (f" | {extra}" if extra else "")
            )

        return format_entity_list(iocs, _fmt, noun="IoC match") + _truncation_note(
            data, "moreDataAvailable", "narrow the time range to see all IoC matches"
        )

    async def lookup_secops_entity(value: str, hours: int = 168) -> str:
        """Look up an IP, hostname, domain, hash, or user and summarize the asset.

        This is the free-text entry point: a bare value is turned into the
        right Chronicle entity query, so a hostname that a UDM search would
        reject still resolves.
        """
        term = (value or "").strip()
        if not term:
            return "Error: an entity value (IP, hostname, domain, hash, or user) is required."
        query, preferred_type = _detect_entity_query(term)
        start, end = _time_range(hours)
        data, err = await _api(
            "get",
            f"{instance_path}:summarizeEntitiesFromQuery",
            f"look up entity {term}",
            params={
                "query": query,
                "timeRange.startTime": start,
                "timeRange.endTime": end,
            },
            timeout=120,
        )
        if err:
            return err

        entities: list[dict[str, Any]] = []
        for summary in (data or {}).get("entitySummaries", []) or []:
            for entity in (summary or {}).get("entity", []) or []:
                if isinstance(entity, dict):
                    entities.append(entity)
        if not entities:
            return (
                f"No entity found for '{term}' in the last {hours}h "
                f"(query: {query}). Widen hours, or check the value's spelling."
            )

        primary = next(
            (
                e
                for e in entities
                if (e.get("metadata") or {}).get("entityType") == preferred_type
            ),
            entities[0],
        )
        lines = [f"Entity summary for '{term}' (query: {query}):", "", "Primary entity:"]
        lines.extend(_format_entity(primary))

        # Details (first/last seen, alert counts) are only available by entity id.
        entity_id = _short_name(primary.get("name"))
        if entity_id:
            details, derr = await _api(
                "get",
                f"{instance_path}:summarizeEntity",
                f"summarize entity {entity_id}",
                params={
                    "entityId": entity_id,
                    "timeRange.startTime": start,
                    "timeRange.endTime": end,
                    "returnAlerts": True,
                    "returnPrevalence": True,
                    "includeAllUdmEventTypesForFirstLastSeen": True,
                },
                timeout=120,
            )
            if not derr and isinstance(details, dict):
                alert_counts = details.get("alertCounts") or []
                if alert_counts:
                    lines.append("\nAssociated alerts:")
                    for ac in alert_counts[:15]:
                        lines.append(
                            f"  {ac.get('rule', 'Unknown rule')} — {ac.get('count', 0)}"
                        )
                    if details.get("hasMoreAlerts"):
                        lines.append("  … more alerts available")
                buckets = (details.get("timeline") or {}).get("buckets") or []
                if buckets:
                    lines.append(
                        f"\nActivity: {sum(int(b.get('eventCount', 0)) for b in buckets)} events, "
                        f"{sum(int(b.get('alertCount', 0)) for b in buckets)} alerts "
                        f"over the last {hours}h."
                    )

        related = [e for e in entities if e is not primary]
        if related:
            lines.append(f"\nRelated entities ({len(related)}):")
            for entity in related[:10]:
                lines.extend(f"  {line.strip()}" for line in _format_entity(entity))
            if len(related) > 10:
                lines.append(f"  … [{len(related) - 10} more]")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 1. CASES (SOAR)
    # ══════════════════════════════════════════════════════════════════════

    def _case_filter_error(err: str, filter_query: str) -> str:
        """Append the filter grammar to a rejected-filter error."""
        return f"{err}\n\n{_CASE_FILTER_HELP}" if filter_query else err

    async def list_secops_cases(
        filter_query: str = "", order_by: str = "", count: int = 25
    ) -> str:
        """List SecOps cases (v1beta case collection).

        ``filter_query`` follows the case filter grammar, e.g.
        ``priority = "PRIORITY_HIGH"``; ``order_by`` e.g. ``createTime desc``.
        """
        params: dict[str, Any] = {
            "pageSize": min(max(count, 1), 1000),
            "expand": "tags,products",
        }
        if filter_query:
            params["filter"] = filter_query
        if order_by:
            params["orderBy"] = order_by
        data, err = await _api(
            "get",
            f"{instance_path}/cases",
            "list cases",
            params=params,
            version="v1beta",
        )
        if err:
            return _case_filter_error(err, filter_query)
        cases = (data or {}).get("cases", []) if isinstance(data, dict) else []

        def _fmt(c: dict) -> str:
            return (
                f"  [{c.get('priority', 'N/A')}] {c.get('stage') or c.get('status') or '?'} — "
                f"{c.get('displayName', 'Untitled')}\n"
                f"    case_id: {_short_name(c.get('name'))} | "
                f"Assignee: {c.get('assignee') or 'Unassigned'} | "
                f"Alerts: {c.get('alertCount', 'N/A')} | "
                f"Created: {_dt(c.get('createTime'))}"
                + (f" | Env: {c.get('environment')}" if c.get("environment") else "")
            )

        total = (data or {}).get("totalSize") if isinstance(data, dict) else None
        listing = format_entity_list(
            cases, _fmt, noun="case", total=int(total) if total else None
        )
        return listing + (
            "\n\n[case_id is the value every other case tool takes. For the "
            "alerts in a case use list_secops_case_alerts, which also returns "
            "the siem_alert_id needed by get_secops_alert / update_secops_alert.]"
            if cases
            else ""
        )

    async def get_secops_case(case_id: str) -> str:
        """Get full details of a SecOps case by ID (v1beta)."""
        cid = _resource_id(case_id)
        data, err = await _api(
            "get",
            f"{instance_path}/cases/{cid}",
            f"get case {cid}",
            params={"expand": "tags,products"},
            version="v1beta",
        )
        if err:
            return err
        if not isinstance(data, dict) or not data.get("name"):
            return f"Case {cid} not found."
        lines = [
            f"Case: {data.get('displayName', 'Untitled')}",
            f"case_id: {_short_name(data.get('name'))}"
            + (f" | Environment: {data.get('environment')}" if data.get("environment") else "")
            + (f" | Score: {data.get('score')}" if data.get("score") is not None else ""),
            f"Priority: {data.get('priority', 'N/A')} | "
            f"Stage: {data.get('stage', 'N/A')} | "
            f"Status: {data.get('status', 'N/A')}",
            f"Assignee: {data.get('assignee') or 'Unassigned'}",
            f"Alert count: {data.get('alertCount', 'N/A')} | "
            f"SOAR severity: {data.get('soarSeverity', 'N/A')}",
            f"Created: {_dt(data.get('createTime'))} | "
            f"Updated: {_dt(data.get('updateTime'))}",
        ]
        tags = data.get("tags") or []
        if tags:
            names = [t.get("displayName") or t.get("name") if isinstance(t, dict) else t for t in tags]
            lines.append(f"Tags: {', '.join(str(n) for n in names if n)}")
        if data.get("description"):
            lines.append(f"\nDescription:\n{_trunc(data.get('description'), 600)}")
        lines.append(
            f"\nNext: list_secops_case_alerts(case_id='{_short_name(data.get('name'))}') "
            "for its alerts, list_secops_case_events for the underlying UDM events."
        )
        return "\n".join(lines)

    async def update_secops_case(
        case_id: str,
        *,
        priority: str = "",
        stage: str = "",
        assignee: str = "",
        display_name: str = "",
    ) -> str:
        """Update a case's priority, stage, assignee, or display name."""
        cid = _resource_id(case_id)
        body: dict[str, Any] = {}
        if priority:
            resolved = _resolve_case_priority(priority)
            if not resolved:
                return _enum_error("case priority", _CASE_PRIORITIES)
            body["priority"] = resolved
        if stage:
            body["stage"] = stage
        if assignee:
            body["assignee"] = assignee
        if display_name:
            body["displayName"] = display_name
        if not body:
            return (
                "Error: nothing to update — pass at least one of priority, stage, "
                "assignee, or display_name."
            )
        _, err = await _api(
            "patch",
            f"{instance_path}/cases/{cid}",
            f"update case {cid}",
            params={"updateMask": ",".join(body)},
            json=body,
            version="v1beta",
        )
        if err:
            return err

        # Confirmed by an independent read, never by the mutation's own answer:
        # a response carrying what the
        # request sent proves the request parsed, not that the case changed.
        changes = ", ".join(f"{k}={v}" for k, v in body.items())
        case, read_err = await _read_case(cid)
        if case is None:
            return (
                f"Case {cid}: the update was accepted ({changes}), but reading "
                f"the case back failed, so persistence is NOT confirmed "
                f"({read_err}). Re-read it with get_secops_case(case_id='{cid}') "
                "before reporting the change as applied."
            )

        confirmed: list[str] = []
        contradicted: list[str] = []
        differing: list[str] = []
        unverified: list[str] = []
        for key, value in body.items():
            observed = case.get(key)
            if observed in (None, ""):
                unverified.append(f"{key}={value}")
            elif _case_value_matches(key, value, observed):
                confirmed.append(f"{key}={value}")
            elif key in _CONTRADICTABLE_CASE_KEYS:
                contradicted.append(
                    f"{key}: sent {value!r}, platform now reports {observed!r}"
                )
            else:
                differing.append(f"{key}: sent {value!r}, platform reports {observed!r}")

        lines: list[str] = []
        if contradicted:
            # Error-shaped on purpose, like the alert side: "updated" would send
            # an agent off to report a change the analyst will not find.
            lines.append(
                f"Error: case {cid} did not take the update as requested - "
                + "; ".join(contradicted)
                + ". Either the write was refused, or the case was changed again "
                "between the write and this read - re-read it with "
                f"get_secops_case(case_id='{cid}') before concluding."
            )
            if confirmed:
                lines.append(f"Confirmed: {', '.join(confirmed)}.")
        elif confirmed:
            lines.append(
                f"Case {cid} updated and confirmed on the platform "
                f"({', '.join(confirmed)})."
            )
        else:
            lines.append(
                f"Case {cid}: the update was accepted ({changes}), but nothing in "
                "the read-back confirms it."
            )
        if differing:
            lines.append(
                f"Reported differently: {'; '.join(differing)}. The platform may "
                "normalize these values, so this is an observation, not evidence "
                "the write was refused."
            )
        if unverified:
            lines.append(
                f"Not verifiable: {', '.join(unverified)} - the case read back "
                "does not report these, so they are neither confirmed nor refuted."
            )
        state = [
            f"{key}={case[key]}"
            for key in ("priority", "stage", "status", "assignee")
            if case.get(key) not in (None, "")
        ]
        if state:
            lines.append(f"Now: {', '.join(state)}.")
        return "\n".join(lines)

    async def _read_case(cid: str) -> tuple[dict[str, Any] | None, str | None]:
        """Re-read a case for confirmation: ``(case, error)``."""
        data, err = await _api(
            "get",
            f"{instance_path}/cases/{cid}",
            f"confirm case {cid}",
            version="v1beta",
        )
        if err:
            return None, err
        if not isinstance(data, dict) or not data.get("name"):
            return None, f"case {cid} was not found when reading it back"
        return data, None

    async def bulk_change_secops_case_priority(case_ids: str, priority: str) -> str:
        """Change the priority of several cases at once."""
        ids = _id_list(case_ids)
        if not ids:
            return "Error: at least one case ID is required."
        resolved = _resolve_case_priority(priority)
        if not resolved:
            return _enum_error("case priority", _CASE_PRIORITIES)
        _, err = await _api(
            "post",
            f"{instance_path}/cases:executeBulkChangePriority",
            "bulk change case priority",
            json={"casesIds": _numeric_ids(ids), "priority": resolved},
            version="v1beta",
        )
        if err:
            return err
        return f"Set priority {resolved} on {len(ids)} case(s): {', '.join(ids)}."

    async def bulk_assign_secops_cases(case_ids: str, username: str) -> str:
        """Assign several cases to a user (or a group, e.g. ``@SecurityTeam``)."""
        ids = _id_list(case_ids)
        if not ids:
            return "Error: at least one case ID is required."
        if not username.strip():
            return "Error: a username (or @group) is required."
        _, err = await _api(
            "post",
            f"{instance_path}/cases:executeBulkAssign",
            "bulk assign cases",
            json={"casesIds": _numeric_ids(ids), "userName": username.strip()},
            version="v1beta",
        )
        if err:
            return err
        return f"Assigned {len(ids)} case(s) to {username.strip()}: {', '.join(ids)}."

    async def close_secops_cases(
        case_ids: str,
        close_reason: str,
        *,
        root_cause: str = "",
        close_comment: str = "",
    ) -> str:
        """Close several cases with a reason (and optional root cause / comment)."""
        ids = _id_list(case_ids)
        if not ids:
            return "Error: at least one case ID is required."
        resolved = _resolve_enum(
            str(close_reason).strip().upper().removeprefix("CLOSE_REASON_"),
            _CASE_CLOSE_REASONS,
        )
        if not resolved:
            return _enum_error("close_reason", _CASE_CLOSE_REASONS)
        body: dict[str, Any] = {
            "casesIds": _numeric_ids(ids),
            "closeReason": resolved,
        }
        if root_cause:
            body["rootCause"] = root_cause
        if close_comment:
            body["closeComment"] = close_comment
        _, err = await _api(
            "post",
            f"{instance_path}/cases:executeBulkClose",
            "bulk close cases",
            json=body,
            version="v1beta",
        )
        if err:
            return err
        return f"Closed {len(ids)} case(s) as {resolved}: {', '.join(ids)}."

    async def reopen_secops_cases(case_ids: str, reopen_comment: str) -> str:
        """Reopen several closed cases, recording why."""
        ids = _id_list(case_ids)
        if not ids:
            return "Error: at least one case ID is required."
        if not reopen_comment.strip():
            return "Error: a reopen comment is required."
        _, err = await _api(
            "post",
            f"{instance_path}/cases:executeBulkReopen",
            "bulk reopen cases",
            json={
                "casesIds": _numeric_ids(ids),
                "reopenComment": reopen_comment.strip(),
            },
            version="v1beta",
        )
        if err:
            return err
        return f"Reopened {len(ids)} case(s): {', '.join(ids)}."

    async def add_secops_case_comment(case_id: str, comment: str) -> str:
        """Post an investigative note to a case's comment wall."""
        cid = _resource_id(case_id)
        if not cid:
            return "Error: a case ID is required."
        if not comment.strip():
            return "Error: a comment body is required."
        data, err = await _api(
            "post",
            f"{instance_path}/cases/{cid}/caseComments",
            f"comment on case {cid}",
            json={"comment": comment.strip()},
            version="v1beta",
        )
        if err:
            return err
        author = (data or {}).get("userOwnerFullName") or (data or {}).get("user") or ""
        return (
            f"Comment added to case {cid}"
            + (f" as {author}" if author else "")
            + f": {_trunc(comment.strip(), 200)}"
        )

    async def list_secops_case_comments(case_id: str, count: int = 25) -> str:
        """Read a case's comment wall — prior analyst notes and findings."""
        cid = _resource_id(case_id)
        if not cid:
            return "Error: a case ID is required."
        data, err = await _api(
            "get",
            f"{instance_path}/cases/{cid}/caseComments",
            f"list comments on case {cid}",
            params={"pageSize": min(max(count, 1), 1000)},
            version="v1beta",
        )
        if err:
            return err
        comments = (data or {}).get("caseComments", []) if isinstance(data, dict) else []
        comments = [c for c in comments if isinstance(c, dict) and not c.get("isDeleted")]

        def _fmt(c: dict) -> str:
            who = c.get("userOwnerFullName") or c.get("user") or "Unknown"
            return (
                f"  [{_dt(c.get('createTime'))}] {who}"
                + (f" (alert {c.get('alertIdentifier')})" if c.get("alertIdentifier") else "")
                + f"\n    {_trunc(c.get('comment'), 600)}"
            )

        return format_entity_list(
            comments,
            _fmt,
            noun="comment",
            empty_msg=f"Case {cid} has no comments.",
        )

    async def _case_alerts(
        cid: str, filter_query: str, count: int
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Fetch a case's alerts from the first-class ``caseAlerts`` collection."""
        params: dict[str, Any] = {
            "pageSize": min(max(count, 1), 1000),
            "expand": "involvedRelations",
        }
        if filter_query:
            params["filter"] = filter_query
        data, err = await _api(
            "get",
            f"{instance_path}/cases/{cid}/caseAlerts",
            f"list alerts for case {cid}",
            params=params,
            version="v1beta",
        )
        if err:
            return [], (
                f"{err}\n\n{_CASE_ALERT_FILTER_HELP}" if filter_query else err
            )
        alerts = (data or {}).get("caseAlerts", []) if isinstance(data, dict) else []
        return [a for a in alerts if isinstance(a, dict)], None

    def _format_case_alert(alert: dict[str, Any]) -> str:
        """One-line summary of a CaseAlert, naming every id it carries.

        ``siem_alert_id`` is called out because it — not the SOAR identifier —
        is what get_secops_alert / update_secops_alert take.
        """
        siem_id = alert.get("siemAlertId") or ""
        line = (
            f"  [{alert.get('priority', 'N/A')}] {alert.get('status', 'N/A')} — "
            f"{alert.get('displayName', 'Untitled')}\n"
            f"    siem_alert_id: {siem_id or '(none — SOAR-only alert)'} | "
            f"soar identifier: {alert.get('identifier', 'N/A')} | "
            f"case_id: {alert.get('caseId', 'N/A')}\n"
            f"    Product: {alert.get('product') or alert.get('vendor') or 'N/A'} | "
            f"Events: {alert.get('eventCount', 'N/A')} | "
            f"Rule: {alert.get('ruleGenerator') or 'N/A'} | "
            f"Start: {_dt(alert.get('startTime'))}"
        )
        # involvedRelations is the alert's entity graph (host → IP, user → host…).
        # It is already fetched via expand, and it is the one source of entities
        # that a SOAR-only alert still has when there are no UDM events to read.
        entities = _relation_entities(alert.get("involvedRelations"))
        if entities:
            shown = ", ".join(entities[:12])
            more = f" (+{len(entities) - 12} more)" if len(entities) > 12 else ""
            line += f"\n    Entities: {shown}{more}"
        return line

    async def list_secops_case_alerts(
        case_id: str, filter_query: str = "", count: int = 25
    ) -> str:
        """List the alerts attached to a case, with the SIEM alert id for each."""
        cid = _resource_id(case_id)
        if not cid:
            return "Error: a case ID is required."
        alerts, err = await _case_alerts(cid, filter_query, count)
        if err:
            return err
        listing = format_entity_list(
            alerts,
            _format_case_alert,
            noun="alert",
            empty_msg=(
                f"Case {cid} has no alerts matching that filter. Check the case ID "
                "against list_secops_cases — it is the trailing segment of the case "
                "resource name, not the SOAR case id."
            ),
        )
        return listing + (
            "\n\n[Pass siem_alert_id to get_secops_alert for the UDM events, or to "
            "update_secops_alert to change severity/priority/status.]"
            if alerts
            else ""
        )

    async def list_secops_case_events(
        case_id: str, alert_id: str = "", count: int = 25, max_alerts: int = 10
    ) -> str:
        """List the UDM events behind a case's alerts (IP, hostname, OS, user…).

        Walks case → caseAlerts → siemAlertId → legacyGetAlert, which is the
        documented path from a case to the events underneath it. Optionally
        narrow to a single ``alert_id`` (either the SIEM alert id or the SOAR
        identifier).
        """
        cid = _resource_id(case_id)
        if not cid:
            return "Error: a case ID is required."
        alerts, err = await _case_alerts(cid, "", max(max_alerts, 1))
        if err:
            return err
        if not alerts:
            return f"Case {cid} has no alerts."

        wanted = _resource_id(alert_id) if alert_id else ""
        if wanted:
            alerts = [
                a
                for a in alerts
                if wanted in {str(a.get("siemAlertId") or ""), str(a.get("identifier") or "")}
            ]
            if not alerts:
                return (
                    f"Alert {wanted} was not found in case {cid}. Use "
                    f"list_secops_case_alerts(case_id='{cid}') to see the ids."
                )

        # Only SIEM-backed alerts have UDM events; SOAR-only alerts carry none.
        siem_ids = [
            str(a["siemAlertId"]) for a in alerts[:max_alerts] if a.get("siemAlertId")
        ]
        if not siem_ids:
            return (
                f"None of the {len(alerts)} alert(s) on case {cid} carry a "
                "siemAlertId, so there are no UDM events to read. These are "
                "SOAR-only alerts; use lookup_secops_entity or "
                "run_secops_udm_search to pull events from the SIEM directly."
            )

        events: list[dict[str, Any]] = []
        failures: list[str] = []
        for siem_id in siem_ids:
            data, gerr = await _api(
                "get",
                f"{instance_path}/legacy:legacyGetAlert",
                f"get alert {siem_id}",
                params={"alertId": siem_id, "includeDetections": True},
            )
            if gerr:
                failures.append(siem_id)
                continue
            alert = (data or {}).get("alert") if isinstance(data, dict) else None
            events.extend(_collect_udm_events(alert))

        if not events:
            note = f" ({len(failures)} alert fetch(es) failed)" if failures else ""
            return (
                f"No UDM events are attached to the {len(siem_ids)} SIEM alert(s) "
                f"of case {cid}{note}. Use run_secops_udm_search or "
                "lookup_secops_entity to pull events directly."
            )

        shown = events[: max(count, 1)]
        lines = [
            f"Found {len(events)} event(s) across {len(siem_ids)} alert(s) of case {cid}"
            + (f" (showing {len(shown)})" if len(shown) < len(events) else "")
            + ":",
            *(_format_udm_event(e) for e in shown),
        ]
        digest = _observables(events)
        if digest:
            lines.append(f"\nObservables:\n{digest}")
        if failures:
            lines.append(
                f"\n[{len(failures)} alert(s) could not be fetched: {', '.join(failures)}]"
            )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 2. ALERTS (rule-engine)
    # ══════════════════════════════════════════════════════════════════════

    async def list_secops_rule_alerts(hours: int = 24, count: int = 25) -> str:
        """List rule-engine generated alerts over the last N hours."""
        start, end = _time_range(hours)
        data, err = await _api(
            "get",
            f"{instance_path}/legacy:legacySearchRulesAlerts",
            "list rule alerts",
            params={
                "timeRange.startTime": start,
                "timeRange.endTime": end,
                # legacySearchRulesAlerts has no pagination but allows far more
                # than 100 (the API caps in the thousands). The result is
                # grouped one compact line per rule, so a low cap mostly just
                # hides rules behind tooManyAlerts rather than saving context.
                # Allow up to 1000 (the provider's "compact listing" ceiling,
                # matching udm_search / rules / reference_lists) so the
                # "increase count" truncation hint is actionable, while still
                # bounding what lands in the agent's context.
                "maxNumAlertsToReturn": min(count, 1000),
            },
        )
        if err:
            return err
        rule_alerts = (
            (data or {}).get("ruleAlerts", []) if isinstance(data, dict) else []
        )

        def _fmt(a: dict) -> str:
            # legacySearchRulesAlerts groups alerts per rule: each entry is a
            # RuleAlertsList = {ruleMetadata: Rule, alerts: [YaraL2Detection]}.
            rule_meta = a.get("ruleMetadata") or {}
            props = rule_meta.get("properties") or {}
            name = props.get("name") or a.get("ruleName") or "Unnamed rule"
            rule_id = rule_meta.get("ruleId") or a.get("ruleId") or "?"
            alerts = a.get("alerts")
            if isinstance(alerts, list):
                count_str = str(len(alerts))
                stamps = sorted(
                    d.get("detectionTimestamp")
                    for d in alerts
                    if isinstance(d, dict) and d.get("detectionTimestamp")
                )
                window = (
                    f" | Detections: {_dt(stamps[0])} → {_dt(stamps[-1])}"
                    if stamps
                    else ""
                )
            else:
                count_str = str(a.get("alertCount", "N/A"))
                window = ""
            status = props.get("liveRuleStatus") or ""
            return (
                f"  {name}\n"
                f"    rule_id: {rule_id} | Alerts: {count_str}"
                + (f" | {status}" if status else "")
                + window
            )

        return format_entity_list(
            rule_alerts, _fmt, noun="rule alert"
        ) + _truncation_note(
            data, "tooManyAlerts", "increase count or narrow the time range"
        )

    async def list_secops_alerts(
        snapshot_query: str = 'feedback_summary.status != "CLOSED"',
        hours: int = 24,
        count: int = 25,
    ) -> str:
        """List SIEM/SOAR alerts (the alerts pane) over the last N hours.

        Unlike ``list_secops_rule_alerts`` — which groups detections per rule —
        this returns individual alerts with their triage feedback (status,
        priority, verdict) and the case they belong to.
        """
        alerts, err, complete = await _alerts_view(
            snapshot_query or 'feedback_summary.status != "CLOSED"',
            hours,
            count,
            "list alerts",
        )
        if err:
            return f"{err}\n\n{_ALERT_SNAPSHOT_HELP}"
        listing = format_entity_list(
            alerts,
            _format_view_alert,
            noun="alert",
            empty_msg=(
                f"No alerts matched in the last {hours}h. "
                f"Widen hours or relax the snapshot query. {_ALERT_SNAPSHOT_HELP}"
            ),
        )
        return listing + ("" if complete else _INCOMPLETE_VIEW_NOTE)

    async def get_secops_alert(alert_id: str, event_count: int = 10) -> str:
        """Get an alert with its detection details, UDM events, and observables."""
        aid = _resource_id(alert_id)
        data, err = await _api(
            "get",
            f"{instance_path}/legacy:legacyGetAlert",
            f"get alert {aid}",
            params={"alertId": aid, "includeDetections": True},
        )
        if err:
            return err
        # legacyGetAlert wraps the alert in {"alert": Collection}; the rule
        # details live in the Collection's detection[] (SecurityResult) list.
        alert = data.get("alert") if isinstance(data, dict) else None
        if not isinstance(alert, dict) or not alert:
            return f"Alert {aid} not found."
        detections = alert.get("detection") or []
        sec = detections[0] if isinstance(detections, list) and detections else {}
        window = alert.get("timeWindow") or {}
        feedback = alert.get("feedbackSummary") or {}
        lines = [
            f"Alert: {sec.get('ruleName') or sec.get('threatName') or alert.get('type', 'Detection')}",
            f"alert_id: {alert.get('id') or aid}",
            f"Type: {alert.get('type', 'N/A')} | "
            f"Severity: {feedback.get('severityDisplay') or sec.get('severity', 'N/A')} | "
            f"State: {feedback.get('status') or sec.get('alertState', 'N/A')} | "
            f"Priority: {feedback.get('priority', 'N/A')}",
            f"Rule ID: {sec.get('ruleId', 'N/A')} | "
            f"Detection time: {_dt(alert.get('detectionTime'))}",
            f"Window: {_dt(window.get('startTime'))} → {_dt(window.get('endTime'))}",
        ]
        if alert.get("caseName"):
            lines.append(f"Case: {_short_name(alert.get('caseName'))}")
        if feedback.get("verdict") or feedback.get("comment"):
            lines.append(
                f"Verdict: {feedback.get('verdict', 'N/A')}"
                + (f" | Comment: {_trunc(feedback.get('comment'), 200)}" if feedback.get("comment") else "")
            )
        description = sec.get("description") or sec.get("summary")
        if description:
            lines.append(f"\nDescription:\n{_trunc(description, 600)}")

        # The UDM events are the reason to fetch an alert at all: they carry the
        # IP / hostname / OS / user context needed to pivot to other tools.
        events = _collect_udm_events(alert)
        if events:
            shown = events[: max(event_count, 1)]
            lines.append(f"\nEvents ({len(events)}, showing {len(shown)}):")
            lines.extend(_format_udm_event(e) for e in shown)
            digest = _observables(events)
            if digest:
                lines.append(f"\nObservables:\n{digest}")
        else:
            lines.append(
                "\nNo UDM events are attached to this alert. Use "
                "list_secops_case_events or run_secops_udm_search to pull the "
                "underlying events."
            )
        return "\n".join(lines)

    async def _read_alert_feedback(
        aid: str,
    ) -> tuple[dict[str, Any] | None, str | None, str]:
        """Re-read an alert for confirmation: ``(feedbackSummary, error, case_id)``.

        Deliberately a second, independent GET rather than a read of the
        mutation's own response: an endpoint echoing back the payload it was
        handed proves the request was parsed, not that anything was stored.
        Detections are not requested — this call exists to observe feedback, and
        pulling the UDM events of every confirmed write would multiply the cost
        of a triage decision for data nobody reads here.
        """
        data, err = await _api(
            "get",
            f"{instance_path}/legacy:legacyGetAlert",
            f"confirm alert {aid}",
            params={"alertId": aid},
        )
        if err:
            return None, err, ""
        alert = data.get("alert") if isinstance(data, dict) else None
        if not isinstance(alert, dict) or not alert:
            return None, f"alert {aid} was not found when reading it back", ""
        summary = alert.get("feedbackSummary")
        return (
            summary if isinstance(summary, dict) else {},
            None,
            _short_name(alert.get("caseName") or ""),
        )

    async def _soar_alert_line(case_id: str, siem_alert_id: str) -> str:
        """What the SOAR case view now shows for this alert, or ``""``.

        The SIEM alert's feedback and the SOAR case-alert row are two different
        objects, and the analyst who reports that nothing changed is looking at
        the second one. Printing it turns that disagreement into an observation
        instead of a dispute. Best-effort: a failure here says nothing about
        whether the write landed, so it is swallowed rather than surfaced.
        """
        alerts, err = await _case_alerts(case_id, "", _SOAR_ROW_SCAN_LIMIT)
        if err or not alerts:
            return ""
        for alert in alerts:
            if str(alert.get("siemAlertId") or "") != siem_alert_id:
                continue
            fields = [
                f"{key}={alert.get(key)}"
                for key in ("priority", "status", "severity")
                if alert.get(key) not in (None, "")
            ]
            return f"SOAR view (case {case_id}): {' | '.join(fields)}" if fields else ""
        if len(alerts) >= _SOAR_ROW_SCAN_LIMIT:
            # Say the row was not reached rather than returning "", which reads
            # identically to "this alert has no SOAR row" — and would go quiet
            # on exactly the noisy cases where the two views disagree most.
            return (
                f"SOAR view (case {case_id}): not found in the first "
                f"{_SOAR_ROW_SCAN_LIMIT} case alerts — check "
                f"list_secops_case_alerts(case_id='{case_id}')."
            )
        return ""

    async def update_secops_alert(
        alert_id: str,
        *,
        severity: int | None = None,
        priority: str = "",
        status: str = "",
        verdict: str = "",
        reason: str = "",
        risk_score: int | None = None,
        comment: str = "",
        root_cause: str = "",
        severity_display: str = "",
    ) -> str:
        """Update an alert's triage feedback, then confirm it on the platform.

        ``severity`` and ``risk_score`` are 0–100 scores; ``priority`` is the
        PRIORITY_* enum shown in the SecOps UI; ``severity_display`` is the
        severity *label* the SOAR alert view shows, which the numeric score does
        not necessarily drive.

        The alert is re-read after the write and the platform's own
        ``feedbackSummary`` decides what this returns.
        """
        aid = _resource_id(alert_id)
        if not aid:
            return "Error: an alert ID is required."

        # Field names are the canonical lowerCamelCase of the LegacyUpdateAlert
        # request and its LegacyFeedback body, per the REST reference.
        feedback: dict[str, Any] = {}
        for value, key in ((severity, "severity"), (risk_score, "riskScore")):
            if value is None or value == "":
                continue
            try:
                score = int(value)
            except (TypeError, ValueError):
                return f"Error: {key} must be a number between 0 and 100."
            if not 0 <= score <= 100:
                return f"Error: {key} must be between 0 and 100."
            feedback[key] = score
        for raw, allowed, prefix, key, label in (
            (priority, _ALERT_PRIORITIES, "PRIORITY_", "priority", "priority"),
            (status, _ALERT_STATUSES, "", "status", "status"),
            (verdict, _ALERT_VERDICTS, "VERDICT_", "verdict", "verdict"),
            (reason, _ALERT_REASONS, "REASON_", "reason", "reason"),
        ):
            if not raw:
                continue
            resolved = _resolve_enum(raw, allowed, prefix)
            if not resolved:
                return _enum_error(label, allowed)
            feedback[key] = resolved
        if comment:
            feedback["comment"] = comment
        if root_cause:
            feedback["rootCause"] = root_cause
        # Trim BEFORE the guard: a whitespace-only value would otherwise pass it
        # and send an empty severityDisplay, which either clears the label the
        # analyst reads or fails validation — a write nobody asked for either way.
        severity_display = severity_display.strip()
        if severity_display:
            feedback["severityDisplay"] = severity_display
        if not feedback:
            return (
                "Error: nothing to update — pass at least one of severity, "
                "severity_display, priority, status, verdict, reason, risk_score, "
                "comment, or root_cause."
            )

        _, err = await _api(
            "post",
            f"{instance_path}/legacy:legacyUpdateAlert",
            f"update alert {aid}",
            json={"alertId": aid, "feedback": feedback},
        )
        if err:
            return err

        requested = ", ".join(f"{k}={v}" for k, v in feedback.items())
        summary, read_err, case_id = await _read_alert_feedback(aid)
        if summary is None:
            # The write was accepted and may well have landed — but nothing here
            # observed it, and saying "updated" on that basis is the defect this
            # read-back exists to remove.
            return (
                f"Alert {aid}: the update was accepted ({requested}), but reading "
                f"the alert back failed, so persistence is NOT confirmed "
                f"({read_err}). Re-read it with get_secops_alert before reporting "
                f"the change as applied."
            )

        confirmed: list[str] = []
        contradicted: list[str] = []
        differing: list[str] = []
        unverified: list[str] = []
        for key, value in feedback.items():
            if key not in _FEEDBACK_ECHO_PREFIXES or key not in summary:
                unverified.append(f"{key}={value}")
            elif _feedback_matches(value, summary[key], _FEEDBACK_ECHO_PREFIXES[key]):
                confirmed.append(f"{key}={value}")
            elif key in _CONTRADICTABLE_FEEDBACK_KEYS:
                contradicted.append(
                    f"{key}: sent {value!r}, platform now reports {summary[key]!r}"
                )
            else:
                differing.append(
                    f"{key}: sent {value!r}, platform reports {summary[key]!r}"
                )

        lines: list[str] = []
        if contradicted:
            # Error-shaped on purpose: an agent that reads "updated" writes an
            # audit note claiming a change the analyst will not find, and the
            # per-tool error counters never learn the platform refused it.
            lines.append(
                f"Error: alert {aid} did not take the update as requested — "
                + "; ".join(contradicted)
                + ". Either the write was refused, or the alert was changed "
                "again between the write and this read — re-read it with "
                "get_secops_alert before concluding."
            )
            if confirmed:
                lines.append(f"Confirmed: {', '.join(confirmed)}.")
        elif confirmed:
            lines.append(
                f"Alert {aid} updated and confirmed on the platform "
                f"({', '.join(confirmed)})."
            )
        else:
            # Nothing was contradicted and nothing was confirmed. Saying
            # "confirmed" here would be the same unearned claim as before, one
            # API call later.
            lines.append(
                f"Alert {aid}: the update was accepted ({requested}), but "
                "nothing in the read-back confirms it."
            )
        if differing:
            lines.append(
                f"Reported differently: {'; '.join(differing)}. These fields are "
                "free text or scores the platform may normalize, so this is an "
                "observation, not evidence the write was refused."
            )
        if unverified and (confirmed or contradicted):
            lines.append(
                f"Not verifiable: {', '.join(unverified)} — the alert's feedback "
                "does not report these back, so they are neither confirmed nor "
                "refuted."
            )
        if severity is not None and severity != "":
            # The one field the customer report turned on: a numeric score was
            # written, and the label an analyst reads is a different field.
            shown = summary.get("severityDisplay")
            lines.append(
                "severity is a 0–100 score and is not echoed back; the alert now "
                f"displays severityDisplay={shown!r}"
                + (
                    ". Pass severity_display to change the label an analyst reads."
                    if not severity_display
                    else "."
                )
            )
        if case_id:
            soar = await _soar_alert_line(case_id, aid)
            if soar:
                lines.append(soar)
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 3. DETECTION RULES
    # ══════════════════════════════════════════════════════════════════════

    async def list_secops_rules(count: int = 25) -> str:
        """List YARA-L detection rules in the SecOps instance."""
        data, err = await _api(
            "get",
            f"{instance_path}/rules",
            "list rules",
            params={"pageSize": min(count, 1000), "view": "BASIC"},
        )
        if err:
            return err
        rules = (data or {}).get("rules", []) if isinstance(data, dict) else []

        def _fmt(r: dict) -> str:
            # BASIC view populates displayName, author, severity and type at the
            # top level (the metadata map and createTime are FULL-view only).
            sev = r.get("severity")
            severity = (
                sev.get("displayName", "") if isinstance(sev, dict) else (sev or "")
            )
            return (
                f"  {r.get('displayName') or _short_name(r.get('name'))}"
                + (f" [{severity}]" if severity else "")
                + "\n"
                f"    rule_id: {_short_name(r.get('name'))} | "
                f"Type: {r.get('type', 'N/A')} | "
                f"Author: {r.get('author', 'N/A')}"
            )

        return format_entity_list(rules, _fmt, noun="rule")

    async def get_secops_rule(rule_id: str) -> str:
        """Get a detection rule (including its YARA-L text) by rule ID."""
        data, err = await _api(
            "get",
            f"{instance_path}/rules/{rule_id}",
            f"get rule {rule_id}",
            params={"view": "FULL"},
        )
        if err:
            return err
        if not isinstance(data, dict) or not data.get("name"):
            return f"Rule {rule_id} not found."
        sev = data.get("severity")
        severity = (
            sev.get("displayName", "") if isinstance(sev, dict) else (sev or "")
        )
        lines = [
            f"Rule: {data.get('displayName') or rule_id}",
            f"rule_id: {_short_name(data.get('name'))} | "
            f"revision: {data.get('revisionId', 'N/A')}",
            f"Type: {data.get('type', 'N/A')} | Author: {data.get('author', 'N/A')}"
            + (f" | Severity: {severity}" if severity else ""),
            f"Created: {_dt(data.get('createTime'))} | "
            f"Updated: {_dt(data.get('revisionCreateTime'))}",
        ]
        if data.get("text"):
            lines.append(f"\nYARA-L:\n{_trunc(data.get('text'), 1500)}")
        return "\n".join(lines)

    async def create_secops_rule(rule_text: str) -> str:
        """Create a new YARA-L detection rule from its rule text."""
        if not rule_text or not rule_text.strip():
            return "Error: rule_text (YARA-L) is required."
        data, err = await _api(
            "post",
            f"{instance_path}/rules",
            "create rule",
            json={"text": rule_text},
        )
        if err:
            return err
        if not isinstance(data, dict) or not data.get("name"):
            return "Rule creation failed (no rule returned)."
        return (
            f"Rule created: {_short_name(data.get('name'))} "
            f"(revision: {data.get('revisionId', 'N/A')})."
        )

    async def update_secops_rule(rule_id: str, rule_text: str) -> str:
        """Update a detection rule's YARA-L text (creates a new revision)."""
        if not rule_text or not rule_text.strip():
            return "Error: rule_text (YARA-L) is required."
        data, err = await _api(
            "patch",
            f"{instance_path}/rules/{rule_id}",
            f"update rule {rule_id}",
            params={"updateMask": "text"},
            json={"text": rule_text},
        )
        if err:
            return err
        return (
            f"Rule {rule_id} updated "
            f"(revision: {(data or {}).get('revisionId', 'N/A') if isinstance(data, dict) else 'N/A'})."
        )

    async def toggle_secops_rule(rule_id: str, enable: bool = True) -> str:
        """Enable or disable a detection rule's live deployment."""
        _, err = await _api(
            "patch",
            f"{instance_path}/rules/{rule_id}/deployment",
            f"{'enable' if enable else 'disable'} rule {rule_id}",
            params={"updateMask": "enabled"},
            json={"enabled": bool(enable)},
        )
        if err:
            return err
        return f"Rule {rule_id} live deployment {'enabled' if enable else 'disabled'}."

    async def list_secops_rule_detections(
        rule_id: str, hours: int = 168, count: int = 25
    ) -> str:
        """List detections generated by a rule over the last N hours (default 7d)."""
        start, end = _time_range(hours)
        data, err = await _api(
            "get",
            f"{instance_path}/legacy:legacySearchDetections",
            f"list detections for rule {rule_id}",
            params={
                # legacySearchDetections takes top-level startTime/endTime query
                # params (not a nested timeRange); listBasis selects which
                # timestamp the [startTime, endTime) window is applied to.
                "ruleId": rule_id,
                "startTime": start,
                "endTime": end,
                "listBasis": "DETECTION_TIME",
                "pageSize": min(count, 100),
            },
        )
        if err:
            return err
        detections = (data or {}).get("detections", []) if isinstance(data, dict) else []

        def _fmt(d: dict) -> str:
            detection = (d.get("detection") or [{}])
            first = detection[0] if isinstance(detection, list) and detection else {}
            return (
                f"  {first.get('ruleName') or d.get('id', '?')} "
                f"[{first.get('alertState', d.get('alertState', 'N/A'))}]\n"
                f"    id: {d.get('id', '?')} | "
                f"Detection time: {_dt(d.get('detectionTime'))} | "
                f"Window: {_dt((d.get('timeWindow') or {}).get('startTime'))}"
            )

        return format_entity_list(
            detections, _fmt, noun="detection"
        ) + _truncation_note(
            data,
            "nextPageToken",
            "more detections exist than returned — raise count (max 100) or "
            "narrow the time range / add filters; the nextPageToken pagination "
            "cursor is not followed",
        )

    # ══════════════════════════════════════════════════════════════════════
    # 4. RETROHUNTS
    # ══════════════════════════════════════════════════════════════════════

    async def create_secops_retrohunt(rule_id: str, hours: int = 168) -> str:
        """Run a retrohunt for a rule over the last N hours of historical data."""
        start, end = _time_range(hours)
        data, err = await _api(
            "post",
            f"{instance_path}/rules/{rule_id}/retrohunts",
            f"create retrohunt for rule {rule_id}",
            json={"processInterval": {"startTime": start, "endTime": end}},
        )
        if err:
            return err
        # The create call returns a long-running Operation (no retrohunt state
        # yet); progress/state is read back via list_secops_retrohunts.
        name = (data or {}).get("name") if isinstance(data, dict) else None
        return (
            f"Retrohunt started for rule {rule_id} "
            f"(operation: {_short_name(name) if name else 'N/A'}). "
            f"Use list_secops_retrohunts to track its progress and state."
        )

    async def list_secops_retrohunts(rule_id: str, count: int = 25) -> str:
        """List retrohunts previously run for a rule."""
        data, err = await _api(
            "get",
            f"{instance_path}/rules/{rule_id}/retrohunts",
            f"list retrohunts for rule {rule_id}",
            params={"pageSize": min(count, 100)},
        )
        if err:
            return err
        retrohunts = (data or {}).get("retrohunts", []) if isinstance(data, dict) else []

        def _fmt(r: dict) -> str:
            interval = r.get("processInterval", {}) or {}
            return (
                f"  {_short_name(r.get('name'))} — {r.get('state', 'N/A')} "
                f"({r.get('progressPercentage', 'N/A')}%)\n"
                f"    Window: {_dt(interval.get('startTime'))} → "
                f"{_dt(interval.get('endTime'))}"
            )

        return format_entity_list(retrohunts, _fmt, noun="retrohunt")

    # ══════════════════════════════════════════════════════════════════════
    # 5. REFERENCE LISTS (threat intel / IOC enforcement)
    # ══════════════════════════════════════════════════════════════════════

    async def list_secops_reference_lists(count: int = 25) -> str:
        """List reference lists (used by detection rules for matching)."""
        data, err = await _api(
            "get",
            f"{instance_path}/referenceLists",
            "list reference lists",
            params={"pageSize": min(count, 1000), "view": "REFERENCE_LIST_VIEW_BASIC"},
        )
        if err:
            return err
        lists = (data or {}).get("referenceLists", []) if isinstance(data, dict) else []

        def _fmt(rl: dict) -> str:
            return (
                f"  {rl.get('displayName') or _short_name(rl.get('name'))} "
                f"[{rl.get('syntaxType', 'N/A')}]\n"
                f"    {_trunc(rl.get('description'), 140) or 'No description'} | "
                f"Entries: {rl.get('entryCount', len(rl.get('entries', []) or []))}"
            )

        return format_entity_list(lists, _fmt, noun="reference list")

    async def get_secops_reference_list(name: str) -> str:
        """Get a reference list and its entries by name (display name)."""
        data, err = await _api(
            "get",
            f"{instance_path}/referenceLists/{name}",
            f"get reference list {name}",
            params={"view": "REFERENCE_LIST_VIEW_FULL"},
        )
        if err:
            return err
        if not isinstance(data, dict) or not data.get("name"):
            return f"Reference list {name} not found."
        entries = data.get("entries", []) or []
        lines = [
            f"Reference list: {data.get('displayName') or _short_name(data.get('name'))}",
            f"Syntax: {data.get('syntaxType', 'N/A')}",
            f"Description: {_trunc(data.get('description'), 200) or 'None'}",
            f"Entries ({len(entries)}):",
        ]
        for e in entries[:100]:
            lines.append(f"  - {e.get('value') if isinstance(e, dict) else e}")
        if len(entries) > 100:
            lines.append(f"  ... [{len(entries) - 100} more]")
        return "\n".join(lines)

    async def create_secops_reference_list(
        name: str,
        entries: str,
        *,
        description: str = "",
        syntax_type: str = "plain_text",
    ) -> str:
        """Create a reference list. ``entries`` is a comma/newline-separated list.

        ``syntax_type``: plain_text, regex, or cidr.
        """
        values = [
            v.strip()
            for v in entries.replace(",", "\n").splitlines()
            if v.strip()
        ]
        if not values:
            return "Error: at least one entry value is required."
        body = {
            "description": description,
            "entries": [{"value": v} for v in values],
            "syntaxType": _REFLIST_SYNTAX.get(
                syntax_type.lower(), _REFLIST_SYNTAX["plain_text"]
            ),
        }
        data, err = await _api(
            "post",
            f"{instance_path}/referenceLists",
            f"create reference list {name}",
            params={"referenceListId": name},
            json=body,
        )
        if err:
            return err
        if not isinstance(data, dict) or not data.get("name"):
            return "Reference list creation failed."
        return f"Reference list '{name}' created with {len(values)} entries."

    async def add_secops_reference_list_entries(name: str, entries: str) -> str:
        """Add entries to an existing reference list (merges with current entries)."""
        new_values = [
            v.strip()
            for v in entries.replace(",", "\n").splitlines()
            if v.strip()
        ]
        if not new_values:
            return "Error: at least one entry value is required."
        # Fetch existing entries so the PATCH merges instead of replacing.
        current, gerr = await _api(
            "get",
            f"{instance_path}/referenceLists/{name}",
            f"load reference list {name}",
            params={"view": "REFERENCE_LIST_VIEW_FULL"},
        )
        if gerr:
            return gerr
        existing = []
        if isinstance(current, dict):
            existing = [
                e.get("value")
                for e in (current.get("entries") or [])
                if isinstance(e, dict) and e.get("value")
            ]
        merged = list(dict.fromkeys([*existing, *new_values]))
        _, err = await _api(
            "patch",
            f"{instance_path}/referenceLists/{name}",
            f"add entries to reference list {name}",
            params={"updateMask": "entries"},
            json={"entries": [{"value": v} for v in merged]},
        )
        if err:
            return err
        return (
            f"Reference list '{name}' now has {len(merged)} entries "
            f"(added {len(merged) - len(existing)})."
        )

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("run_secops_udm_search", run_secops_udm_search)
    executor.register("translate_secops_udm_query", translate_secops_udm_query)
    executor.register("validate_secops_udm_query", validate_secops_udm_query)
    executor.register("list_secops_iocs", list_secops_iocs)
    executor.register("lookup_secops_entity", lookup_secops_entity)
    executor.register("list_secops_cases", list_secops_cases)
    executor.register("get_secops_case", get_secops_case)
    executor.register("update_secops_case", update_secops_case)
    executor.register(
        "bulk_change_secops_case_priority", bulk_change_secops_case_priority
    )
    executor.register("bulk_assign_secops_cases", bulk_assign_secops_cases)
    executor.register("close_secops_cases", close_secops_cases)
    executor.register("reopen_secops_cases", reopen_secops_cases)
    executor.register("add_secops_case_comment", add_secops_case_comment)
    executor.register("list_secops_case_comments", list_secops_case_comments)
    executor.register("list_secops_case_alerts", list_secops_case_alerts)
    executor.register("list_secops_case_events", list_secops_case_events)
    executor.register("list_secops_alerts", list_secops_alerts)
    executor.register("list_secops_rule_alerts", list_secops_rule_alerts)
    executor.register("get_secops_alert", get_secops_alert)
    executor.register("update_secops_alert", update_secops_alert)
    executor.register("list_secops_rules", list_secops_rules)
    executor.register("get_secops_rule", get_secops_rule)
    executor.register("create_secops_rule", create_secops_rule)
    executor.register("update_secops_rule", update_secops_rule)
    executor.register("toggle_secops_rule", toggle_secops_rule)
    executor.register("list_secops_rule_detections", list_secops_rule_detections)
    executor.register("create_secops_retrohunt", create_secops_retrohunt)
    executor.register("list_secops_retrohunts", list_secops_retrohunts)
    executor.register("list_secops_reference_lists", list_secops_reference_lists)
    executor.register("get_secops_reference_list", get_secops_reference_list)
    executor.register("create_secops_reference_list", create_secops_reference_list)
    executor.register(
        "add_secops_reference_list_entries", add_secops_reference_list_entries
    )

    src = "integration:google_secops"
    return [
        # ── 0. Hunting ────────────────────────────────────────────────────
        ToolDefinition(
            name="run_secops_udm_search",
            description=(
                "Run a Google SecOps UDM search for events over the last N hours. "
                "query is UDM syntax, e.g. "
                "principal.hostname = \"host1\" AND metadata.event_type = "
                "\"PROCESS_LAUNCH\"."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "hours": {"type": "integer", "default": 24},
                    "count": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="translate_secops_udm_query",
            description=(
                "Translate a natural-language description into a Google SecOps "
                "UDM search query (e.g. 'powershell launches on WIN-01 in the "
                "last day'). Pair with run_secops_udm_search to execute it."
            ),
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="validate_secops_udm_query",
            description=(
                "Validate (compile) a Google SecOps UDM search query without "
                "running it, returning the syntax error and its position if the "
                "query is invalid. Use before run_secops_udm_search or when "
                "authoring detection logic."
            ),
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="list_secops_iocs",
            description=(
                "List enterprise-wide IoC matches against ingested events over "
                "the last N hours (domains/IPs seen in your environment that "
                "match threat intelligence)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "default": 24},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="lookup_secops_entity",
            description=(
                "Look up a single entity by value — IP, hostname, domain, file "
                "hash, email, MAC, or username — and return the asset summary "
                "(type, IP, OS/platform, first/last seen, related entities, "
                "associated alerts). Use this for free-text lookups: a bare "
                "hostname or IP is not valid UDM syntax, so run_secops_udm_search "
                "would return nothing for it."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "value": {
                        "type": "string",
                        "description": "IP, hostname, domain, hash, email, MAC, or username.",
                    },
                    "hours": {"type": "integer", "default": 168},
                },
                "required": ["value"],
            },
            source=src,
            priority=1,
        ),
        # ── 1. Cases (SOAR) ───────────────────────────────────────────────
        ToolDefinition(
            name="list_secops_cases",
            description=(
                "List Google SecOps cases with their priority, stage, assignee "
                "and alert count. Returns the SIEM case_id used by the other "
                "case tools. "
                + _CASE_FILTER_HELP
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter_query": {
                        "type": "string",
                        "description": _CASE_FILTER_HELP,
                    },
                    "order_by": {
                        "type": "string",
                        "description": 'Sort order, e.g. "createTime desc".',
                    },
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_secops_case",
            description=(
                "Get full details of a SecOps case. case_id is the SIEM case id "
                "from list_secops_cases (a bare id, a UUID, or a full resource "
                "name all work) — not the SOAR case id."
            ),
            input_schema={
                "type": "object",
                "properties": {"case_id": {"type": "string"}},
                "required": ["case_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="update_secops_case",
            description=(
                "Update a SecOps case: priority (INFORMATIVE/LOW/MEDIUM/HIGH/"
                "CRITICAL — note a case uses the bare names, unlike an alert's "
                "PRIORITY_* values), stage, assignee, or display name. Only the "
                "fields you pass are changed. The case is re-read afterwards: the "
                "reply states which fields the platform confirmed, which it "
                "contradicts (an error) and which it does not report."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "priority": {"type": "string"},
                    "stage": {"type": "string"},
                    "assignee": {"type": "string"},
                    "display_name": {"type": "string"},
                },
                "required": ["case_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="bulk_change_secops_case_priority",
            description=(
                "Change the priority of several SecOps cases at once. priority is "
                "INFORMATIVE/LOW/MEDIUM/HIGH/CRITICAL; case_ids is a "
                "comma-separated list of case IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_ids": {"type": "string"},
                    "priority": {"type": "string"},
                },
                "required": ["case_ids", "priority"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="bulk_assign_secops_cases",
            description=(
                "Assign several SecOps cases to a user or group (e.g. "
                "\"@SecurityTeam\"). case_ids is a comma-separated list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_ids": {"type": "string"},
                    "username": {"type": "string"},
                },
                "required": ["case_ids", "username"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="close_secops_cases",
            description=(
                "Close SecOps cases with a reason (MALICIOUS, NOT_MALICIOUS, "
                "MAINTENANCE, INCONCLUSIVE, UNKNOWN) and an optional root cause "
                "and comment. case_ids is a comma-separated list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_ids": {"type": "string"},
                    "close_reason": {"type": "string"},
                    "root_cause": {"type": "string"},
                    "close_comment": {"type": "string"},
                },
                "required": ["case_ids", "close_reason"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="reopen_secops_cases",
            description=(
                "Reopen closed SecOps cases, recording why. case_ids is a "
                "comma-separated list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_ids": {"type": "string"},
                    "reopen_comment": {"type": "string"},
                },
                "required": ["case_ids", "reopen_comment"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="add_secops_case_comment",
            description=(
                "Post an investigative note to a SecOps case's comment wall — use "
                "this to write triage findings and conclusions back to the case so "
                "the next analyst sees them."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "comment": {"type": "string"},
                },
                "required": ["case_id", "comment"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_secops_case_comments",
            description=(
                "Read a SecOps case's comment wall — prior analyst notes and "
                "findings. Check this before re-investigating a case."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": ["case_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_secops_case_alerts",
            description=(
                "List the alerts attached to a SecOps case with their priority, "
                "status, product and event count. Returns the siem_alert_id for "
                "each alert — that is the id get_secops_alert and "
                "update_secops_alert take, as opposed to the SOAR identifier. "
                + _CASE_ALERT_FILTER_HELP
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "filter_query": {
                        "type": "string",
                        "description": _CASE_ALERT_FILTER_HELP,
                    },
                    "count": {"type": "integer", "default": 25},
                },
                "required": ["case_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="list_secops_case_events",
            description=(
                "List the UDM events behind a case's alerts — principal/target "
                "hostname, IP, OS, user and process — plus a deduplicated "
                "observable digest to pivot on (OpenAEV, GTI, …). Optionally "
                "narrow to one alert_id within the case."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "alert_id": {
                        "type": "string",
                        "description": "Optional: narrow to one alert (SIEM alert id or SOAR identifier).",
                    },
                    "count": {"type": "integer", "default": 25},
                    "max_alerts": {"type": "integer", "default": 10},
                },
                "required": ["case_id"],
            },
            source=src,
            priority=1,
        ),
        # ── 2. Alerts ─────────────────────────────────────────────────────
        ToolDefinition(
            name="list_secops_alerts",
            description=(
                "List individual SIEM/SOAR alerts over the last N hours with "
                "their triage feedback (status, priority, verdict) and the case "
                "they belong to. " + _ALERT_SNAPSHOT_HELP
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "snapshot_query": {
                        "type": "string",
                        "description": _ALERT_SNAPSHOT_HELP,
                        "default": 'feedback_summary.status != "CLOSED"',
                    },
                    "hours": {"type": "integer", "default": 24},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="list_secops_rule_alerts",
            description=(
                "List rule-engine generated alerts grouped per rule over the last "
                "N hours. For individual alerts with triage feedback and case "
                "linkage, use list_secops_alerts instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "default": 24},
                    "count": {"type": "integer", "default": 25},
                },
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_secops_alert",
            description=(
                "Get an alert by ID with its detection details, the UDM events "
                "that triggered it (IP, hostname, OS, user, process) and a "
                "deduplicated observable digest."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "event_count": {"type": "integer", "default": 10},
                },
                "required": ["alert_id"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_secops_alert",
            description=(
                "Update a SIEM alert's triage feedback: severity (0-100 score), "
                "severity_display (the severity LABEL the SOAR alert view "
                "shows), priority (PRIORITY_INFO/LOW/MEDIUM/HIGH/CRITICAL), "
                "status (NEW/REVIEWED/OPEN/CLOSED), verdict (TRUE_POSITIVE/"
                "FALSE_POSITIVE), reason, risk_score (0-100), comment and "
                "root_cause. Only the fields you pass are changed. This is the "
                "tool for raising or lowering an alert's severity from a "
                "playbook; case-level priority is update_secops_case instead. "
                "The alert is re-read afterwards (and the case's SOAR row when "
                "it has one, so one update costs up to three API calls) and the "
                "reply states which fields the platform confirmed, which it "
                "refused (an error), which it reports differently, and which it "
                "does not echo back at all — do not report a change as applied "
                "beyond what it says."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "severity": {
                        "type": "integer",
                        "description": "Severity score 0-100.",
                    },
                    "priority": {"type": "string"},
                    "status": {"type": "string"},
                    "verdict": {"type": "string"},
                    "reason": {
                        "type": "string",
                        "description": (
                            "One of REASON_UNSPECIFIED, REASON_NOT_MALICIOUS, "
                            "REASON_MALICIOUS, REASON_MAINTENANCE."
                        ),
                    },
                    "risk_score": {
                        "type": "integer",
                        "description": "Risk score 0-100.",
                    },
                    "comment": {"type": "string"},
                    "root_cause": {"type": "string"},
                    "severity_display": {
                        "type": "string",
                        "description": (
                            "Severity label shown in the alert view (e.g. LOW, "
                            "MEDIUM, HIGH). Separate from the numeric severity "
                            "score, which the platform does not report back."
                        ),
                    },
                },
                "required": ["alert_id"],
            },
            source=src,
            priority=1,
        ),
        # ── 3. Detection rules ────────────────────────────────────────────
        ToolDefinition(
            name="list_secops_rules",
            description="List YARA-L detection rules in the SecOps instance.",
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="get_secops_rule",
            description="Get a detection rule (including its YARA-L text) by rule ID.",
            input_schema={
                "type": "object",
                "properties": {"rule_id": {"type": "string"}},
                "required": ["rule_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="create_secops_rule",
            description=(
                "Create a new YARA-L detection rule from its rule text (for "
                "authoring new detections / intelligence)."
            ),
            input_schema={
                "type": "object",
                "properties": {"rule_text": {"type": "string"}},
                "required": ["rule_text"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="update_secops_rule",
            description="Update a detection rule's YARA-L text (creates a new revision).",
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                    "rule_text": {"type": "string"},
                },
                "required": ["rule_id", "rule_text"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="toggle_secops_rule",
            description="Enable or disable a detection rule's live deployment.",
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                    "enable": {"type": "boolean", "default": True},
                },
                "required": ["rule_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_secops_rule_detections",
            description=(
                "List detections generated by a rule over the last N hours "
                "(default 7 days)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                    "hours": {"type": "integer", "default": 168},
                    "count": {"type": "integer", "default": 25},
                },
                "required": ["rule_id"],
            },
            source=src,
            priority=2,
        ),
        # ── 4. Retrohunts ─────────────────────────────────────────────────
        ToolDefinition(
            name="create_secops_retrohunt",
            description=(
                "Run a retrohunt for a rule over the last N hours of historical "
                "data (default 7 days) to find past matches."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                    "hours": {"type": "integer", "default": 168},
                },
                "required": ["rule_id"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="list_secops_retrohunts",
            description="List retrohunts previously run for a rule, with progress/state.",
            input_schema={
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                    "count": {"type": "integer", "default": 25},
                },
                "required": ["rule_id"],
            },
            source=src,
            priority=3,
        ),
        # ── 5. Reference lists ────────────────────────────────────────────
        ToolDefinition(
            name="list_secops_reference_lists",
            description=(
                "List reference lists (collections of values used by detection "
                "rules for matching)."
            ),
            input_schema={
                "type": "object",
                "properties": {"count": {"type": "integer", "default": 25}},
                "required": [],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="get_secops_reference_list",
            description="Get a reference list and its entries by name.",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
            source=src,
            priority=3,
        ),
        ToolDefinition(
            name="create_secops_reference_list",
            description=(
                "Create a reference list for IOC enforcement / threat intel. "
                "entries is a comma/newline-separated list of values. "
                "syntax_type: plain_text, regex, or cidr."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "entries": {"type": "string"},
                    "description": {"type": "string"},
                    "syntax_type": {"type": "string", "default": "plain_text"},
                },
                "required": ["name", "entries"],
            },
            source=src,
            priority=2,
        ),
        ToolDefinition(
            name="add_secops_reference_list_entries",
            description=(
                "Add entries to an existing reference list (merges with the "
                "current entries). entries is comma/newline-separated."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "entries": {"type": "string"},
                },
                "required": ["name", "entries"],
            },
            source=src,
            priority=2,
        ),
    ]
