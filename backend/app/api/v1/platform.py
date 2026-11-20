# SPDX-License-Identifier: Apache-2.0

"""Platform configuration, registration, and CTEM metrics API.

Public endpoints (no auth):
  GET  /config              — platform mode, branding, license status, features,
                              OpenCTI/OpenAEV link status + last verification

Registration endpoints (token auth via PLATFORM_REGISTRATION_TOKEN):
  POST /register            — register an OpenCTI/OpenAEV/OpenCRQ platform;
                              re-called periodically, it is also the heartbeat
  DELETE /register/{ident}  — unregister a platform

Admin endpoints (JWT auth):
  GET  /registrations       — list all registered platforms
  DELETE /registrations/{id}/purge — purge a registration and all its integrations
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import case, select, update
from sqlalchemy import text as sa_text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import Headers
from starlette.responses import Response as StarletteResponse

from app.api.deps import (
    get_current_user,
    get_db,
    require_admin,
    require_chat_enabled,
)
from app.api.v1.chat import (
    InterruptedTurnInfo,
    MessageFeedbackResponse,
    MessageFeedbackUpsert,
    last_message_previews,
)
from app.api.v1.platform_settings_helpers import (
    get_platform_settings as _load_ps,
)
from app.api.v1.schemas.settings import QuotaPeriodUsage
from app.core.config import settings
from app.core.http_body import read_bounded_body
from app.core.rate_limit import enforce_rate_limit, refund_rate_limit
from app.core.redaction import bind_known_secrets, known_secrets_scoped, redact_known_secrets_for_display
from app.core.registration_token import (
    PUBLISHED_DEFAULT_REFUSED_DETAIL,
    REGISTRATION_DISABLED_DETAIL,
    REGISTRATION_TOKEN_GENERATE_COMMAND,
    RegistrationTokenState,
    configured_registration_token,
    registration_token_matches,
    registration_token_state,
)
from app.core.security import decrypt_json, decrypt_value, encrypt_value
from app.core.security_options import security_option
from app.core.url_identity import url_identity
from app.core.utils import (
    format_exception_message,
    if_none_match_hits,
    is_graceful_shutdown_cancellation,
)
from app.core.utils import is_statement_timeout as _is_statement_timeout
from app.enterprise.licensing import (
    DEFAULT_AGENTIC_DAILY_QUOTA,
    DEFAULT_AGENTIC_MONTHLY_QUOTA,
    DEFAULT_AGENTIC_YEARLY_QUOTA,
    GLOBAL_LICENSE,
    LicenseInfo,
    XTMLicenseInfo,
)
from app.enterprise.platform_license_validator import validate_platform_license
from app.integrations.providers.opencrq.procedure_manifest import (
    MAX_MANIFEST_BYTES,
    parse_registered_manifest,
    valid_fingerprint,
)
from app.models.integration import Integration
from app.models.platform_procedure_manifest import PlatformProcedureManifest
from app.models.platform_registration import PlatformRegistration
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services import platform_sign_in_clients
from app.services.branding import (
    BrandingAsset,
    branding_asset_versions,
    build_web_manifest,
    get_branding,
    resolve_branding_asset,
    resolved_branding_payload,
)
from app.services.chat_turn_stream import AGENTIC_ACTION_STATUS
from app.services.context_usage import CONTEXT_USAGE_META_KEYS, ContextUsageTracker
from app.services.intent_aliases import canonical_intent_name, canonical_vertical
from app.services.platform_chat_extras import (
    list_platform_chat_prompts,
    load_user_feedback_by_message,
    resolve_platform_chat_quota,
)
from app.services.xtm_license import (
    ee_platform_status,
    resolve_xtm_license,
    xtm_sublicensed_ids,
)

if TYPE_CHECKING:
    from app.models.chat import Message

logger = logging.getLogger(__name__)

router = APIRouter()

# Interval between SSE keepalive comments on the platform chat stream. Must be
# comfortably below the idle/read timeout of any intermediary (k8s ingress,
# nginx — typically 60s) so a long silent window (e.g. the LLM generating a
# large tool-call argument) never lets the connection be dropped mid-stream.
_SSE_KEEPALIVE_INTERVAL_SECONDS = 15

# How long answer text or reasoning is held for more of the same before it is
# sent. The agent emits one piece per provider delta, and every event is
# a render in the embedded chat panel; the native web chat coalesces on the
# same window.
_SSE_TEXT_COALESCE_SECONDS = 0.05


class _SSETextCoalescer:
    """Joins consecutive answer-text or reasoning events of the platform stream.

    Two events are joined only when they differ in nothing but ``content``;
    any other event is sent as it came, after the text held so far, so the
    order the client sees is the order the agent emitted.
    """

    __slots__ = ("_held", "_parts", "deadline")

    def __init__(self) -> None:
        self._held: dict | None = None
        self._parts: list[str] = []
        self.deadline = 0.0

    @property
    def holding(self) -> bool:
        return self._held is not None

    @staticmethod
    def _joinable(item: dict) -> bool:
        if not isinstance(item.get("content"), str):
            return False
        return item.get("type") == "stream" or (
            item.get("type") == "status" and item.get("status") == "thinking_text"
        )

    def _joins_held(self, item: dict) -> bool:
        held = self._held
        if held is None or not self._joinable(item) or len(item) != len(held):
            return False
        return all(key == "content" or held.get(key) == value for key, value in item.items())

    def offer(self, item: dict, now: float) -> list[dict]:
        """Take *item*; return the events to send now, in order."""
        if self._joins_held(item):
            self._parts.append(item["content"])
            return []
        out: list[dict] = []
        held = self.flush()
        if held is not None:
            out.append(held)
        if self._joinable(item):
            self._held = item
            self._parts = [item["content"]]
            self.deadline = now + _SSE_TEXT_COALESCE_SECONDS
        else:
            out.append(item)
        return out

    def flush(self) -> dict | None:
        """The held text as one event, or ``None`` when nothing is held."""
        if self._held is None:
            return None
        event = {**self._held, "content": "".join(self._parts)}
        self._held = None
        self._parts = []
        return event

# Agent that leads ``GET /chat/agents`` — and therefore the first-run default in
# the embedded chat panel, which selects ``agents[0]`` until the user picks one.
# The generalist platform assistant, not a specialised one. Absent (e.g. Copilot
# deployments, or a scoped ``?tag=`` query) the ordering simply falls through to
# alphabetical.
PLATFORM_DEFAULT_CHAT_AGENT_SLUG = "xtm-one-assistant"

# Scope config applied to platform-managed integrations during registration.
# Enables disambiguation when multiple instances of the same provider exist.
_PLATFORM_SCOPE_CONFIG = {
    "match_key": "base_url",
    "resolve_from_header": "X-Platform-URL",
    "resolve_from_platform": "platform_url",
}

# Friendly display names for the embedding host application, keyed by the
# lowercase ``X-Platform-Product`` header value the host's chatbot proxy sends
# server-side (the "Ask Ariane" embedded chat): OpenCTI sends ``opencti``
# (httpChatbotProxy.ts), OpenAEV sends ``openaev`` (XtmOneClient.java),
# OpenCRQ sends ``opencrq`` (xtm-one feature chat proxy). Unknown products
# fall back to the raw header value so a new host still gets a sensible
# label.
_HOST_PRODUCT_DISPLAY_NAMES = {
    "opencti": "OpenCTI",
    "openaev": "OpenAEV",
    "opencrq": "OpenCRQ",
    # Legacy header value from before OpenCRQ was renamed from OpenGRC
    "opengrc": "OpenCRQ",
}

# Platform identifiers that were renamed after platforms had already
# registered under the old one. A product that has not yet shipped its own
# rename keeps sending the legacy value, so the identifier is normalised at
# the registration boundary and every persisted row, provider key and EE
# lookup downstream sees one spelling.
#
# ``openbas`` is deliberately absent: that rename predates this seam and is
# handled by tolerating the legacy identifier at lookup time instead (see
# ``_get_active_registration`` in ``mcp_platform.py``). Normalising it here
# would silently rewrite identifiers of platforms already in the field.
_LEGACY_PLATFORM_IDENTIFIERS = {"opengrc": "opencrq"}

# Every identifier an OpenCTI, OpenAEV or OpenCRQ platform registers or has
# registered under, legacy spellings included. The integration catalog in
# Settings lists the registrations of these products only.
PRODUCT_PLATFORM_IDENTIFIERS = frozenset(
    {"opencti", "openaev", "openbas", "opencrq", "opengrc"}
)


def _canonical_platform_identifier(platform_identifier: str) -> str:
    """Return the current spelling of a possibly-legacy platform identifier."""
    return _LEGACY_PLATFORM_IDENTIFIERS.get(
        platform_identifier, platform_identifier
    )


# The legacy ``{OLD}_API_URL`` names (``OPENGRC_API_URL`` since the OpenCRQ
# rename) stop being read after this date, which ``.env.sample``
# and the deployment docs (upgrade.md, xtm-suite-integration.md) announce.
LEGACY_API_URL_REMOVAL_DATE = "2026-12-31"

# Legacy variables already warned about in this process. Registration is also
# the platforms' heartbeat, so an unlatched warning would repeat on every beat.
_warned_legacy_api_url_vars: set[str] = set()


def _resolve_api_url_override(provider_key: str) -> tuple[str, str]:
    """Return ``(url, variable name)`` of the ``{PROVIDER}_API_URL`` override.

    ``("", "<current name>")`` when none is set. The variable is named after the
    provider key, so a renamed product also renames its override. An operator
    who set the old name keeps it working until
    ``LEGACY_API_URL_REMOVAL_DATE``: silently ignoring it would drop the
    platform back to its external URL, which in a split-horizon deployment is
    exactly the address the override existed to avoid. The current name always
    wins when both are set, and the old one is warned about once per process.
    """
    override_var = f"{provider_key.upper()}_API_URL"
    api_url_override = os.environ.get(override_var, "").strip()
    if api_url_override:
        return api_url_override, override_var
    for legacy, current in _LEGACY_PLATFORM_IDENTIFIERS.items():
        if current != provider_key:
            continue
        legacy_var = f"{legacy.upper()}_API_URL"
        legacy_value = os.environ.get(legacy_var, "").strip()
        if not legacy_value:
            continue
        if legacy_var not in _warned_legacy_api_url_vars:
            _warned_legacy_api_url_vars.add(legacy_var)
            logger.warning(
                "%s is deprecated, rename it to %s - the old name is no "
                "longer read after %s.",
                legacy_var,
                override_var,
                LEGACY_API_URL_REMOVAL_DATE,
            )
        return legacy_value, legacy_var
    return "", override_var


def _enrich_page_context(
    context: dict | None,
    headers: Headers,
) -> dict | None:
    """Add the embedding host application's NAME to the page context.

    The embedded chat (OpenCTI / OpenAEV "Ask Ariane") forwards the user's
    current relative URL as ``context`` (e.g. ``{"url": "/dashboard"}``) and
    identifies its host out-of-band via the ``X-Platform-Product`` header its
    chatbot proxy sets server-side. Without the product name the agent only
    sees a bare path and attributes it to the XTM One platform (its own
    branding) instead of the actual host (e.g. OpenCTI).

    Only the product *name* is injected — deliberately NOT a URL. The host's
    base URL is already owned by a single hardened mechanism: the bound
    integration surfaces it in the "Connected integrations" block
    (``get_integration_public_url``) and ``url_guard`` validates links against
    it. Injecting ``X-Platform-URL`` here too would put a second, possibly
    divergent base URL in the prompt (internal vs external host) and tempt the
    model to build links from "the current chat host" — exactly what the link
    policy in ``agent_chat`` forbids. So we add the name for locational
    awareness and leave every URL/link concern to the integration layer.

    The header value wins over any same-named key in the browser-supplied
    context — the proxy is the trusted source.
    """
    product = (headers.get("X-Platform-Product") or "").strip()
    if not product:
        return context

    enriched: dict = dict(context) if context else {}
    enriched["host_application"] = _HOST_PRODUCT_DISPLAY_NAMES.get(
        product.lower(), product
    )
    return enriched

# EE license types — a platform that self-reports one of these is treated as
# Enterprise Edition even without a server-validated enterprise license PEM.
# ``ci`` is included because a CI-licensed OpenCTI/OpenAEV instance is still
# an EE deployment (EE features are active; the license just expires quickly).
_EE_LICENSE_TYPES = frozenset(
    {"nfr", "enterprise", "ee", "standard", "lts", "trial", "ci"}
)


def _resolve_edition(
    *,
    license_validated: bool,
    license_type: str | None,
    xtm_sublicensed: bool = False,
) -> str:
    """Return ``"EE"`` / ``"CE"`` for a registering platform.

    EE is granted when the platform presents a server-validated enterprise
    license PEM, self-reports an EE ``license_type``, *or* is sub-licensed
    by the Filigran license (``xtm_sublicensed`` — resolved via
    :func:`_xtm_sublicense_grants_ee`). This is the single source of truth
    for both ``PlatformRegistration.contract`` (the home-page platform card)
    and the company-managed integration's edition tag (the integrations
    list), so the two surfaces can never diverge from each other or from the
    ``ee_enabled`` registration-response field (e.g. OpenCRQ — no product CA,
    no ``license_type`` — being granted EE via the XTM sub-license while
    every display surface kept showing a stale CE).
    """
    if license_validated or xtm_sublicensed:
        return "EE"
    if license_type and license_type in _EE_LICENSE_TYPES:
        return "EE"
    return "CE"


# ── Schemas ──────────────────────────────────────────────────────────────────


class AgenticQuotas(BaseModel):
    """Platform-wide agentic-action ceilings per period.

    ``None`` is uncapped. A number is the ceiling, and ``0`` means no
    allowance at all (Community Edition, an unlicensed Copilot). A license
    period is uncapped when the certificate carries ``0`` - build those
    through :meth:`from_license` so that ``0`` never reaches the wire as
    "uncapped".
    """

    daily: int | None = 0
    monthly: int | None = 0
    yearly: int | None = 0

    @classmethod
    def from_license(cls, info: Any) -> AgenticQuotas:
        from app.services.quota import ceiling_or_none

        return cls(
            daily=ceiling_or_none(info.daily_quota),
            monthly=ceiling_or_none(info.monthly_quota),
            yearly=ceiling_or_none(info.yearly_quota),
        )


ModuleCoverageScope = Literal["global", "platforms", "none"]


class ModuleCoverage(BaseModel):
    """Whether the Filigran license covers one module (OpenCTI, OpenAEV, OpenCRQ).

    Read from the module's IDs extension: ``global`` covers every instance
    of the module (the licensing service's default, the module's "Enterprise
    Edition" box checked), listed platform IDs cover those instances only
    (the "restrict to platform IDs" option), an empty list covers none (the
    box unchecked). A product reads ``covered`` and ``scope`` instead of
    re-deriving them from the raw IDs.
    """

    covered: bool = False
    scope: ModuleCoverageScope = "none"
    platform_ids: list[str] = []

    @classmethod
    def from_ids(cls, ids: list[str]) -> ModuleCoverage:
        if GLOBAL_LICENSE in ids:
            return cls(covered=True, scope="global", platform_ids=[])
        listed = [i for i in ids if i]
        if listed:
            return cls(covered=True, scope="platforms", platform_ids=listed)
        return cls()


class SubLicenses(BaseModel):
    """Sub-licensing platform IDs from the Filigran license.

    The ``*_ids`` lists are the raw extensions (kept for existing readers);
    ``modules`` says per module whether it is covered and how.
    """

    opencti_ids: list[str] = []
    openaev_ids: list[str] = []
    opencrq_ids: list[str] = []
    modules: dict[str, ModuleCoverage] = Field(
        default_factory=lambda: {
            "opencti": ModuleCoverage(),
            "openaev": ModuleCoverage(),
            "opencrq": ModuleCoverage(),
        }
    )

    @classmethod
    def from_license(cls, info: XTMLicenseInfo) -> SubLicenses:
        # The coverage reads the same ids as the registration grant
        # (xtm_sublicensed_ids): a model 2 licence covers every module.
        return cls(
            opencti_ids=info.opencti_ids,
            openaev_ids=info.openaev_ids,
            opencrq_ids=info.opencrq_ids,
            modules={
                "opencti": ModuleCoverage.from_ids(xtm_sublicensed_ids(info, "opencti")),
                "openaev": ModuleCoverage.from_ids(xtm_sublicensed_ids(info, "openaev")),
                "opencrq": ModuleCoverage.from_ids(xtm_sublicensed_ids(info, "opencrq")),
            },
        )


class XTMLicense(BaseModel):
    """Filigran license status exposed to the frontend.

    Carries what the license DECIDES — the acts quota and the module
    sub-licenses — plus its validity window. The legacy ``copilot_enabled`` /
    ``agentic_ai_enabled`` capability flags are deliberately not on the wire:
    nothing gates on them, and showing "Agentic AI: Disabled" next to a
    fully working platform only confuses (see ``XTMLicenseInfo``).
    """

    valid: bool = False
    customer: str | None = None
    license_type: str | None = None
    expiration: str | None = None
    expired: bool = False
    expiration_prevention: bool = False
    extra_expiration: bool = False
    extra_expiration_days: int = 0
    agentic_quotas: AgenticQuotas = AgenticQuotas()
    license_source: str = "none"
    sub_licenses: SubLicenses = SubLicenses()


class PlatformFeatures(BaseModel):
    ai_adoption: bool
    all_integrations: bool
    all_templates: bool
    has_ee_platform: bool
    # Number of active platform registrations (any contract). Lets the
    # frontend distinguish "no platforms registered yet" (welcome landing)
    # from "platforms registered but only CE" (CE-only lockdown splash).
    active_registration_count: int = 0


# ── OpenCTI / OpenAEV link status ───────────────────────────────────────────
# XTM One is what links OpenCTI and OpenAEV together: each product registers
# itself and keeps that registration alive by re-registering (``POST /register``
# refreshes ``last_heartbeat_at`` — there is no separate heartbeat route), so
# the last heartbeat IS the last time the link was verified.
#
# This ships in the public ``/config`` payload deliberately: it is the one
# question an operator (or an external probe / status page) needs answered
# before authenticating — "is the OpenCTI ↔ OpenAEV integration live, and how
# fresh is that answer?". Only the coarse state and the verification timestamp
# are exposed; platform URLs, titles, versions, and license details stay on the
# authenticated ``/platform/registrations`` endpoint.
PLATFORM_LINK_IDENTIFIERS = ("opencti", "openaev")

# Closed tri-state, declared as a Literal so the enum reaches the OpenAPI
# schema (this is a public contract — consumers generate clients off it) and
# an out-of-range value fails validation instead of shipping silently.
PlatformLinkState = Literal["connected", "disconnected", "not_registered"]

PLATFORM_LINK_CONNECTED: PlatformLinkState = "connected"
PLATFORM_LINK_DISCONNECTED: PlatformLinkState = "disconnected"
PLATFORM_LINK_NOT_REGISTERED: PlatformLinkState = "not_registered"


class PlatformLinkStatus(BaseModel):
    """Connectivity of one product (OpenCTI / OpenAEV) with XTM One.

    * ``connected``      — registered, active, and its last heartbeat is
      within ``HEARTBEAT_HEALTHY_WINDOW``.
    * ``disconnected``   — registered, but the registration was deactivated
      or its heartbeat went stale (the product stopped re-registering).
    * ``not_registered`` — the product has never registered with XTM One.
    """

    status: PlatformLinkState = PLATFORM_LINK_NOT_REGISTERED
    # Redundant with ``status`` but spares every consumer a string compare.
    connected: bool = False
    # When connectivity was last verified (ISO 8601, UTC) — the last
    # heartbeat, falling back to the registration time. ``None`` when the
    # product has never registered.
    last_checked_at: str | None = None


class PlatformIntegrationStatus(BaseModel):
    """OpenCTI ↔ OpenAEV integration state, as of the last verification."""

    opencti: PlatformLinkStatus = PlatformLinkStatus()
    openaev: PlatformLinkStatus = PlatformLinkStatus()
    # True only when BOTH products are currently connected — i.e. the
    # OpenCTI ↔ OpenAEV link through XTM One is live end to end.
    linked: bool = False
    # Most recent of the two verification timestamps (ISO 8601, UTC), so a
    # single read tells how fresh ``linked`` is. ``None`` when neither product
    # has ever registered.
    last_checked_at: str | None = None


# ── Editions ────────────────────────────────────────────────────────────────
# The XTM Platform model: ONE platform, TWO editions, and a license that only
# ever decides two things — which connected module is in Enterprise Edition,
# and the agentic capacity (the acts ceilings). It never decides which
# capability of the XTM One core is available: the core is fully activated in
# every Enterprise deployment, whatever the certificate carries.
#
# * ``enterprise`` — Enterprise Edition. Granted by EITHER a valid Filigran license
#   OR at least one connected platform whose own Enterprise license validated
#   server-side (OpenCTI / OpenAEV / OpenCRQ EE). Everything is on: chat,
#   agents, assignments, flows, knowledge bases, skills, prompts, MCP servers,
#   custom tools, integrations, variables, A2A, the MCP agent endpoints, the
#   full integration catalog, AI adoption, custom quotas. The only thing that
#   differs between the two grants is WHERE THE QUOTA COMES FROM
#   (``agentic_source``): the license ceilings when a license is installed,
#   the included allowance (``DEFAULT_AGENTIC_*``) otherwise.
# * ``community``  — Community Edition: no Filigran license and no EE platform. AI
#   is fully disabled — no agent runs, no chat, no AI feature in the modules.
#   The rest of the core keeps running (identity, settings, platform
#   registration, integrations, cross-module KPIs), so the deployment stays
#   explorable and the upgrade is one license key away, never a redeploy.
#
# There is deliberately no third state. The former ``ee_platform`` "Starter
# Pack" tier (chat on, creation flows gated behind an ``agentic_ai`` license
# flag) is retired: an Enterprise customer builds agents from day one.
#
# Copilot mode has no edition (``""``): it relies on ``license_required`` /
# ``license_valid`` instead.
EDITION_ENTERPRISE = "enterprise"
EDITION_COMMUNITY = "community"


def resolve_deployment_edition(
    is_xtm: bool,
    *,
    xtm_validated: bool,
    has_ee: bool,
) -> str:
    """Resolve the XTM One deployment edition — the ONE licence question.

    Pure so every caller (the config payload, the gates, telemetry) reads the
    same rule: Enterprise when a valid Filigran license is installed OR a connected
    platform is Enterprise Edition. Only meaningful for XTM One mode — Copilot
    mode returns the empty string.
    """
    if not is_xtm:
        return ""
    if xtm_validated or has_ee:
        return EDITION_ENTERPRISE
    return EDITION_COMMUNITY


class PlatformConfigResponse(BaseModel):
    instance_id: str
    platform_mode: str
    platform_name: str
    # Resolved branding logo — the binary asset endpoint path
    # (``/api/v1/platform/branding/asset/logo``) when an image has been
    # uploaded, a bundled static asset path, or the legacy
    # ``platform_logo_url`` string for pre-migration installs. Never a raw
    # base64 data URL — see ``platform_logo_hash``.
    platform_logo: str | None = None
    # Short content hash for the uploaded logo, or ``None`` when unset /
    # not a data-URL upload. The frontend appends this as a ``?v=`` query
    # param on ``platform_logo`` for cache-busting.
    platform_logo_hash: str | None = None
    # The bundled logo's light-theme variant (dark wordmark), for a client in
    # the light theme; ``None`` once an admin sets a logo of their own, which
    # is then drawn in both themes.
    platform_logo_light: str | None = None
    # The product mark alone, for the collapsed navigation rail: the bundled
    # emblem while the bundled logo is in use, the uploaded favicon (with its
    # ``?v=`` hash) beside an operator's own logo, ``None`` when they uploaded
    # no favicon. ``platform_emblem_light`` follows ``platform_logo_light``.
    platform_emblem: str | None = None
    platform_emblem_hash: str | None = None
    platform_emblem_light: str | None = None
    # These fields always carry a value: admin overrides take priority,
    # otherwise the mode-specific default is used.
    platform_description: str
    platform_theme_color: str
    # Binary asset endpoint path (``/api/v1/platform/branding/asset/favicon``)
    # when an admin has uploaded a favicon, else ``None``.
    platform_favicon: str | None = None
    platform_favicon_hash: str | None = None
    # Nullable admin overrides. When set, the frontend injects CSS variables
    # that intentionally beat the bundled mode/theme defaults.
    platform_accent_color: str | None = None
    platform_background_color: str | None = None
    # Effective fallback colors for unset override fields. The frontend may
    # show these in controls/previews, but keeps runtime styling on the bundled
    # CSS defaults unless the nullable override fields above are set.
    platform_default_accent_color: str
    platform_default_background_color: str
    # Binary asset endpoint path (``/api/v1/platform/branding/asset/og-image``)
    # when an admin has uploaded an OG image, else the absolute/legacy URL or
    # bundled static default.
    platform_og_image: str
    platform_og_image_hash: str | None = None
    # Markdown login-screen subtitle. Always carries a value: admin override
    # if set, otherwise the mode-specific default. Rendered as Markdown by the
    # (unauthenticated) login page, so it ships in this public config payload.
    platform_login_message: str
    # Platform-wide UI language ("auto" = browser detection). Ships in this
    # public payload so the login page is localized before authentication.
    platform_language: str = "auto"
    platform_version: str
    license_required: bool
    license_valid: bool
    license_customer: str | None = None
    license_type: str | None = None
    license_expiration: str | None = None
    features: PlatformFeatures
    # OpenCTI / OpenAEV connectivity + when it was last verified. Always
    # present (both products default to ``not_registered``) so consumers can
    # read it unconditionally, in both XTM One and Copilot modes.
    integration_status: PlatformIntegrationStatus = PlatformIntegrationStatus()
    xtm_license: XTMLicense | None = None
    # Whether AI is LICENSED on this deployment — the one licence answer every
    # client gates on (creation flows, chat composer, nav locks, upsell
    # dialogs). ``True`` on Enterprise Edition (XTM One: a valid Filigran license
    # OR a connected EE platform; Copilot: a valid license), ``False`` on
    # Community Edition. Independent of ``ai_enabled`` below, which is the
    # administrator's switch: a licensed platform can still be deliberately
    # AI-off, and neither implies the other.
    agentic_enabled: bool = False
    # The ceilings in force and where they come from: ``xtm_license`` (the
    # certificate's own figures), ``ee_platform`` (the included allowance —
    # Enterprise Edition granted by a connected EE platform, no Filigran license
    # installed yet), or ``none`` (Community Edition, nothing runs).
    agentic_quotas: AgenticQuotas = AgenticQuotas()
    agentic_source: str = "none"
    # XTM One only — ``enterprise`` / ``community`` (``EDITION_*``). Always set
    # for XTM One; left as ``""`` in Copilot mode (where the license-required
    # splash handles gating instead).
    edition: str = ""
    # Whether chat APIs and chat UI are available: ``agentic_enabled`` AND
    # ``ai_enabled``. In Copilot mode this mirrors ``license_valid``.
    chat_enabled: bool = True
    disable_code_interpreter: bool = True
    disable_custom_tools: bool = True
    disable_custom_variables: bool = True
    ai_catalog_enabled: bool = False
    # Whether AI is switched on here — the primary signal clients gate on
    # (FR-011), deliberately unauthenticated so the sign-in screen can state
    # the condition. Independent of ``agentic_enabled``, which answers
    # whether the *licence* permits AI: `ai_enabled: false` with
    # `agentic_enabled: true` is a licensed platform deliberately running
    # without AI, and neither implies the other. Defaults False to fail closed.
    ai_enabled: bool = False


class PlatformUserEntry(BaseModel):
    email: str
    display_name: str = ""
    api_key: str


class IntentDeclaration(BaseModel):
    name: str
    description: str = ""


def _seed_platform_policy() -> dict[str, str]:
    """Initial approval policy for a platform registering for the first time.

    Returns the seed as kwargs so the call site reads as what it is: a value
    applied at row creation, not a rule consulted later. An unrecognised
    setting falls back to gating rather than guessing — a typo in a deployment
    variable must not quietly disable an approval control.
    """
    from app.tools.approval import (
        POLICY_REQUIRE,
        POLICY_SKIP,
        SET_BY_DEFAULT,
        SET_BY_DEPLOYMENT,
    )

    configured = (settings.tool_approvals_platform_default or "").strip().lower()
    if configured == POLICY_SKIP:
        return {
            "approval_policy": POLICY_SKIP,
            "approval_policy_set_by": SET_BY_DEPLOYMENT,
        }
    if configured and configured != POLICY_REQUIRE:
        logger.warning(
            "Unrecognised TOOL_APPROVALS_PLATFORM_DEFAULT %r - gating this "
            "platform's tools instead",
            configured,
        )
    return {
        "approval_policy": POLICY_REQUIRE,
        "approval_policy_set_by": (
            SET_BY_DEPLOYMENT if configured == POLICY_REQUIRE else SET_BY_DEFAULT
        ),
    }


# Longest ``platform_url`` a registration may carry: an address, never a payload.
PLATFORM_URL_MAX_LENGTH = 2048
PLATFORM_URL_INVALID_DETAIL = (
    "must be an http(s) URL with a host, and no userinfo, query string, fragment, "
    "whitespace, control character or backslash"
)
# Characters ``urlsplit`` keeps (or silently drops) that no identity carries: a
# NUL would fail the row's write as a 500, a backslash is read by a browser as
# a path separator but by ``urlsplit`` as part of the host.
_PLATFORM_URL_UNSAFE_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")


class RegisterRequest(BaseModel):
    platform_identifier: str
    # The platform's identity (``iss`` of the tokens it signs, #4880) and the
    # address every surface links to: refused here, as a 422, unless it names
    # one - a ``javascript:``, ``data:`` or relative value never reaches a row.
    platform_url: str = Field(min_length=1, max_length=PLATFORM_URL_MAX_LENGTH)
    platform_title: str = "Platform"
    platform_version: str = ""
    platform_id: str | None = None
    contract: str = "CE"
    license_type: str | None = None
    enterprise_license_pem: str | None = None
    business_vertical: str | None = None  # e.g. "fimi", "cti", "fraud"
    # Can this platform's embedded assistant display a tool-approval prompt
    # and return a decision? A fact only the platform knows. ``None``
    # means it has not said - every build predating the capability - and is
    # NOT the same as False: an undeclared platform falls through to the
    # operator's policy, while one that declares False has actively told us
    # its assistant cannot ask.
    #
    # Deliberately NOT named ``supports_tool_approval``, which is the per-message
    # flag on ``PlatformChatRequest``. The two are related but answer different
    # questions at different scopes, and sharing a name would invite reading one
    # as the other: this one is about the platform ("can our assistant ask at
    # all?") and decides whether its tools gate; that one is about a single turn
    # ("will this client answer an approval_required event?") and decides whether
    # the turn pauses or ends with the plain message. A platform can honestly
    # declare this True while an unattended caller of its own sends nothing per
    # message.
    supports_approval_prompts: bool | None = None
    intents: list[IntentDeclaration] = []
    # What an OpenCRQ says of the procedures it serves. Typed ``Any`` on
    # purpose: they ride on every heartbeat, so a value that does not read is
    # ignored and logged (``_record_procedure_manifest``), never a 422 that would
    # cut the heartbeat. The fingerprint (sha256, lower-case hex) is sent on every
    # registration; the manifest, ``{formatVersion, jsonSchemaDialect, procedures}``,
    # only on the one that answers ``procedure_manifest_wanted``.
    procedure_manifest_fingerprint: Any = None
    procedure_manifest: Any = None
    # The platform's health snapshot (Platform Health, ``services/health/snapshots``):
    # versioned and bounded there, typed ``Any`` for the same reason as the
    # manifest: a value that does not read is ignored and logged, never a 422.
    health: Any = None
    # The platform's OpenID Connect callbacks, on its own origin. Sent:
    # XTM One keeps a sign-in client for it and answers its ``sign_in_client_id``.
    # ``[]``: the client is removed. Not sent: nothing changes. Unregistering or
    # purging the platform removes it too.
    sign_in_redirect_uris: list[Annotated[str, Field(max_length=2048)]] | None = Field(default=None, max_length=10)
    # Deprecated fields — kept for backward compat, ignored by registration logic
    admin_api_key: str = ""
    users: list[PlatformUserEntry] = []

    @field_validator("platform_url")
    @classmethod
    def _platform_url_names_an_identity(cls, value: str) -> str:
        # The value is stored as sent: only the check reads it canonically.
        if _PLATFORM_URL_UNSAFE_CHARS.search(value.strip()) or url_identity(value) is None:
            raise ValueError(PLATFORM_URL_INVALID_DETAIL)
        return value


# Which path granted a registration Enterprise Edition. A closed set,
# declared as a Literal so it reaches the OpenAPI schema.
RegistrationEESource = Literal["platform_license", "xtm_sublicense"]
EE_SOURCE_PLATFORM_LICENSE: RegistrationEESource = "platform_license"
EE_SOURCE_XTM_SUBLICENSE: RegistrationEESource = "xtm_sublicense"

# Why an answer whose ee_sources names the XTM sub-license carries no proof of
# it. A closed set, declared as a Literal so it reaches the OpenAPI schema.
LicenseWithheldReason = Literal["registration_token_published_default"]
LICENSE_WITHHELD_PUBLISHED_DEFAULT: LicenseWithheldReason = (
    "registration_token_published_default"
)


class RegistrationResponse(BaseModel):
    """Answer to ``POST /register``, repeated on every heartbeat.

    XTM One signs none of it, and the product decides which host it calls XTM
    One, so every value XTM One states here is only as trustworthy as that
    setting: ``ee_enabled`` is advisory, never an entitlement proof.
    The one field a product can verify is ``xtm_license_pem``: when the XTM
    license is what grants Enterprise Edition, it is that license certificate,
    signed by Filigran and verified by the product against the XTM trust
    anchor pinned in its own build (recipe in
    ``dev-docs/platform-registration-entitlement.md``) - unless
    ``xtm_license_withheld_reason`` says why XTM One kept it.
    """

    status: str
    platform_identifier: str
    ee_enabled: bool = Field(
        description=(
            "Advisory: XTM One's view of the platform's edition, true exactly "
            "when ee_sources is not empty. Not an entitlement proof - a "
            "product verifies xtm_license_pem, or its own license, instead."
        )
    )
    user_integrations: int = 0
    version: str = ""
    #: The platform's OpenID Connect client, when it sent
    #: ``sign_in_redirect_uris`` and the registration token is configured.
    sign_in_client_id: str | None = None
    ee_sources: list[RegistrationEESource] = Field(
        default_factory=list,
        description=(
            "Every path that granted ee_enabled, in this order: "
            "platform_license (the platform's own enterprise license, "
            "validated by XTM One; the platform verifies it itself) and "
            "xtm_sublicense (the Filigran license lists this platform_id, or "
            "global, for the platform type). Empty when ee_enabled is false."
        ),
    )
    xtm_license_pem: str | None = Field(
        default=None,
        description=(
            "The Filigran-signed Filigran license certificate proving the "
            "xtm_sublicense grant: present exactly when ee_sources contains "
            "xtm_sublicense and xtm_license_withheld_reason is null, null "
            "otherwise. One PEM CERTIFICATE block, no chain and nothing else."
        ),
    )
    xtm_license_withheld_reason: LicenseWithheldReason | None = Field(
        default=None,
        description=(
            "Why xtm_license_pem is null although ee_sources contains "
            "xtm_sublicense, null otherwise. "
            "registration_token_published_default: XTM One runs with the "
            "published default PLATFORM_REGISTRATION_TOKEN, which "
            "authenticates nobody, so the license certificate is withheld "
            "until a token of the operator's own is set on XTM One and on the "
            "platform. Read it as a missing proof; read an unknown value the "
            "same way."
        ),
    )
    procedure_manifest_wanted: bool = Field(
        default=False,
        description=(
            "True when the request reported a procedure_manifest_fingerprint "
            "XTM One holds no manifest for and carried no procedure_manifest: "
            "register again once with procedure_manifest in the body. Never "
            "true under the published default registration token, which "
            "stores no manifest."
        ),
    )


class PlatformApprovalUpdate(BaseModel):
    """An administrator's explicit choice for one platform's tools."""

    approval_policy: Literal["require", "skip"]


class PlatformRegistrationOut(BaseModel):
    id: str
    platform_identifier: str
    platform_id: str | None
    platform_url: str
    platform_title: str
    platform_version: str
    # Tool approval for this platform. ``supports_approval_prompts`` is
    # what the platform reports it can do; ``gates_tools`` is the decision
    # actually applied, which the UI should display rather than re-deriving.
    # ``capability_nudge`` flags the one case worth prompting about: something
    # can now ask, but the tools are still set to skip.
    # ``capability_nudge_reason`` says who that something is — ``declared`` for
    # the platform's own registration, ``observed`` for a client of it that was
    # seen accepting prompts. The UI must word the two differently: the second
    # is one caller speaking, not the platform.
    supports_approval_prompts: bool | None = None
    approval_policy: str = "require"
    approval_policy_set_by: str = "default"
    gates_tools: bool = True
    capability_nudge: bool = False
    capability_nudge_reason: str | None = None
    contract: str
    license_type: str | None
    license_validated: bool
    integration_id: str | None
    status: str
    registered_at: str
    last_heartbeat_at: str
    # Live connectivity health (status active + recent heartbeat), computed
    # server-side so every surface shares one staleness window. Never cached.
    healthy: bool


# ── Helpers ──────────────────────────────────────────────────────────────────


async def is_agentic_ai_enabled(db: AsyncSession) -> bool:
    """Whether agentic AI may run here: AI switched on AND licensed.

    The licence half is :func:`is_agentic_ai_licensed`. When AI is disabled for
    the deployment: always false, whatever the licence says. Answering
    here rather than only in ``require_agentic_ai`` covers the callers that ask
    this directly — ``ctem_metrics``, ``chat`` and ``auth`` — which would
    otherwise keep their write paths open.
    """
    from app.services.ai_state import is_ai_enabled

    if not await is_ai_enabled(db):
        return False
    return await is_agentic_ai_licensed(db)


async def is_agentic_ai_licensed(db: AsyncSession) -> bool:
    """Whether the *licence* permits AI, ignoring the AI toggle.

    THE licence predicate — every licence gate in the codebase resolves to
    this one answer (``is_chat_licensed`` and ``ee_features_enabled`` are its
    aliases, kept for their callers' vocabulary). Under the XTM Platform model
    there is nothing finer to ask: Enterprise Edition means everything, and a
    license carries a quota, not a feature switch.

    * XTM One mode: Enterprise Edition — a valid Filigran license OR at least one
      connected platform whose Enterprise license validated server-side.
    * Copilot mode: a valid Filigran license (the retired ``copilot`` capability
      flag is no longer consulted).

    Split out of :func:`is_agentic_ai_enabled` because two callers use that
    function as an **inclusion** test rather than a refusal — the Terms-of-Use
    resolvers in ``auth.py`` and ``chat.py`` treat "no agentic AI" as "no terms
    to accept". Folding the AI state into it therefore waived Terms of Use for
    everyone signing in while AI was off, and recorded ``accepted=True`` for
    users who were never shown them. Those callers want this function.
    """
    if settings.is_copilot_like:
        ps = await _load_ps(db)
        return resolve_xtm_license(ps).license_validated
    return (await get_deployment_edition(db)) == EDITION_ENTERPRISE


def _platform_language(ps: PlatformSettings | None) -> str:
    """Effective platform UI language ("auto" when unset or no settings row).

    Single source of truth for the two ``PlatformConfigResponse`` build
    sites so the fallback logic can never drift between XTM and Copilot
    modes.
    """
    if ps is None:
        return "auto"
    return getattr(ps, "platform_language", None) or "auto"


def _last_verified_at(reg: PlatformRegistration) -> datetime | None:
    """When this registration's connectivity was last verified (UTC).

    That is the last heartbeat (a re-registration), falling back to the
    registration time for a row that somehow has none. Mirrors the naive-value
    guard in ``is_healthy()`` so a malformed row can never break the public
    config payload, and converts an aware value to UTC so the serialized
    timestamp always carries the ``+00:00`` offset the contract advertises —
    whatever offset the driver/session hands back for the ``timestamptz``
    column.
    """
    last = reg.last_heartbeat_at or reg.registered_at
    if last is None:
        return None
    return last.replace(tzinfo=UTC) if last.tzinfo is None else last.astimezone(UTC)


def _link_status(
    reg: PlatformRegistration | None, now: datetime
) -> PlatformLinkStatus:
    """Map one registration (or its absence) to a public link status."""
    if reg is None:
        return PlatformLinkStatus()
    connected = reg.is_healthy(now)
    verified = _last_verified_at(reg)
    return PlatformLinkStatus(
        status=PLATFORM_LINK_CONNECTED if connected else PLATFORM_LINK_DISCONNECTED,
        connected=connected,
        last_checked_at=verified.isoformat() if verified else None,
    )


def _build_integration_status(
    regs: Sequence[PlatformRegistration],
    now: datetime | None = None,
) -> PlatformIntegrationStatus:
    """Build the OpenCTI/OpenAEV link status from registration rows.

    Pure function over the rows so the semantics can be unit-tested without a
    DB. Rows for other products (e.g. OpenCRQ) are ignored;
    ``platform_identifier`` is unique so there is at most one row per product.
    """
    now = now or datetime.now(UTC)
    by_identifier = {r.platform_identifier: r for r in regs}
    entries = {
        identifier: _link_status(by_identifier.get(identifier), now)
        for identifier in PLATFORM_LINK_IDENTIFIERS
    }
    verified = [
        v
        for identifier in PLATFORM_LINK_IDENTIFIERS
        if (reg := by_identifier.get(identifier)) is not None
        and (v := _last_verified_at(reg)) is not None
    ]
    return PlatformIntegrationStatus(
        **entries,
        linked=all(entry.connected for entry in entries.values()),
        # Compare datetimes, not ISO strings — a naive-then-normalized value
        # would sort wrong against an offset-suffixed one.
        last_checked_at=max(verified).isoformat() if verified else None,
    )


async def _load_integration_status(db: AsyncSession) -> PlatformIntegrationStatus:
    """Read the OpenCTI/OpenAEV link status live from the registrations table.

    Never cached: the whole point of ``last_checked_at`` is that the freshness
    it reports is real — the CTEM dashboard regression where a recovered
    platform stayed red behind a cached posture payload is exactly the failure
    mode to avoid (see ``test_platform_registration_health``).
    """
    result = await db.execute(
        select(PlatformRegistration).where(
            PlatformRegistration.platform_identifier.in_(PLATFORM_LINK_IDENTIFIERS)
        )
    )
    return _build_integration_status(result.scalars().all())


async def get_deployment_edition(db: AsyncSession) -> str:
    """Return the current XTM One edition (``EDITION_*``) — shared by the
    config endpoint, the gating dependencies and telemetry. ``""`` in Copilot
    mode."""
    if settings.is_copilot_like:
        return ""
    ps = await _load_ps(db)
    xtm_info = resolve_xtm_license(ps)
    has_ee, _ = await ee_platform_status(db, xtm_info)
    return resolve_deployment_edition(
        True, xtm_validated=xtm_info.license_validated, has_ee=has_ee
    )


async def ee_features_enabled(db: AsyncSession) -> bool:
    """Whether Enterprise Edition AI features (e.g. the exposure briefing, the
    autonomous attack path) are licensed — :func:`is_agentic_ai_licensed` under
    the name its callers use. CTEM metrics/dashboard are NOT gated (full CE).
    """
    return await is_agentic_ai_licensed(db)


async def is_chat_enabled(
    db: AsyncSession, *, licensed: bool | None = None
) -> bool:
    """Return whether chat APIs are available for any user.

    ``licensed`` lets a caller that has *already* resolved the licence answer
    pass it in. ``/platform/config`` is unauthenticated and hit on every page
    load, and it resolves the edition for its own payload — without this it
    paid for ``_load_ps`` + ``ee_platform_status`` twice per request.
    Re-deriving the chat rule at the call site instead would be the worse fix:
    two copies of it is what left the AI short-circuit out of the payload in
    the first place.

    * Licensed (Enterprise Edition, or a valid license in Copilot mode): on.
      The license carries the quotas — chat on an Enterprise Edition granted
      by a connected EE platform runs under the included 30/100/300 allowance.
    * Community Edition: off.
    * When AI is disabled: always false, whatever the licence says.

    The AI check lives here, not only in ``require_chat_enabled``: callers
    ask this directly — the public-chat widget, the A2A client — so gating
    only the dependency would leave them serving model traffic after AI was
    turned off.
    """
    from app.services.ai_state import is_ai_enabled

    if not await is_ai_enabled(db):
        return False
    return await is_chat_licensed(db, licensed=licensed)


async def is_chat_licensed(
    db: AsyncSession, *, licensed: bool | None = None
) -> bool:
    """The licence half of :func:`is_chat_enabled`, ignoring the AI toggle.

    For **reads**. FR-008 requires every AI read to answer 200 with AI
    disabled — browsable AI data is the whole point of the mode — so a
    conversation-history listing cannot sit behind the write gate. Split out
    rather than inlined so the two questions stay one implementation, the same
    way ``is_agentic_ai_licensed`` splits the agentic gate — and it IS
    ``is_agentic_ai_licensed``: chat is not licensed separately from anything
    else under the XTM Platform model.
    """
    if licensed is not None:
        return licensed
    return await is_agentic_ai_licensed(db)


# Why ``is_chat_enabled`` said no, for the callers that report rather than
# refuse over HTTP (the A2A probes and pre-flight; the routes use
# ``deps.require_chat_enabled``, which names each reason separately).
CHAT_UNAVAILABLE_DETAIL = (
    "Agentic chat is unavailable on this deployment: AI is disabled, or the "
    "deployment runs Community Edition (no Filigran license and no Enterprise "
    "Edition OpenCTI, OpenAEV or OpenCRQ platform connected)."
)


def _verify_registration_token(authorization: str | None) -> None:
    """Validate the Bearer token matches PLATFORM_REGISTRATION_TOKEN.

    The register answer can carry the Filigran license certificate, so the
    check fails closed - with no token configured every request is refused,
    an empty Bearer value included - and the comparison is
    ``registration_token_matches`` (constant-time).
    """
    if registration_token_state() == "unset":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=REGISTRATION_DISABLED_DETAIL,
        )
    token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not token.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing registration token",
        )
    if not registration_token_matches(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid registration token",
        )


# Failed registration-token checks one client may make per minute before the
# register / unregister routes answer 429 without checking. A check that passes
# is refunded, so a platform's heartbeat (every few minutes) never spends it.
_REGISTRATION_RATE_LIMIT = "platform-register"
_REGISTRATION_FAILED_CHECKS_PER_MINUTE = 10

# Namespace for the per-platform registration lock (the first key of
# ``pg_advisory_xact_lock(int4, int4)``), so the identifier hash can't collide
# with advisory locks taken elsewhere for unrelated features.
_REGISTRATION_LOCK_NS = 0x50524547  # "PREG" - Platform REGistration


async def _lock_platform_registration(db: AsyncSession, platform_identifier: str) -> None:
    """Serialize the register / unregister calls of one platform.

    Each reads the registration, decides from it what the call may record -
    on the published default token, what is kept whatever the call sends - and
    writes the row back. Interleaved, the later commit writes back a row read
    before the earlier one changed it: a renewal, an unregister, undone by a
    heartbeat that never saw them. Taken before the first read and released
    with the commit. An advisory lock rather than a row lock: a first
    registration has no row to lock, and the admin paths that delete a
    registration with its integrations never wait for it, so it adds no lock
    order to theirs.
    """
    await db.execute(
        sa_text("SELECT pg_advisory_xact_lock(CAST(:ns AS integer), hashtext(:scope))").bindparams(
            ns=_REGISTRATION_LOCK_NS, scope=f"platform_registration:{platform_identifier}"
        )
    )


async def _authorize_registration(request: Request, authorization: str | None) -> None:
    """:func:`_verify_registration_token` behind a per-client budget of failures.

    The token is a shared secret an operator may have chosen weak, so it must
    not be guessable at network speed. The budget is charged before the check,
    so a check in progress holds its unit and a burst of parallel guesses
    cannot outrun the count, and refunded once the check passes, so only
    failed checks keep spending it. With no token configured nothing can
    match: nothing is charged, and every caller keeps getting the refusal that
    says how to configure one. On the published default the unit is refunded
    by :func:`_refuse_published_default_change` instead, only for the call
    that token may make.
    """
    state = registration_token_state()
    if state != "unset":
        await enforce_rate_limit(
            request,
            _REGISTRATION_RATE_LIMIT,
            limit=_REGISTRATION_FAILED_CHECKS_PER_MINUTE,
            window_seconds=60,
        )
    _verify_registration_token(authorization)
    if state != "published_default":
        await refund_rate_limit(request, _REGISTRATION_RATE_LIMIT)


async def _refuse_published_default_change(
    request: Request,
    reg: PlatformRegistration | None = None,
    platform_url: str | None = None,
) -> None:
    """Refuse what the published default token may not do.

    It is accepted only so the platforms an existing deployment registered
    with it keep their heartbeat. A registered platform's URL is a trusted
    issuer of the cross-platform JWTs that sign XTM One users in
    (``platform_identity.platform_for_issuer``), so with a token everyone knows, choosing
    that URL would sign anyone in as any user: only the heartbeat of an active
    registration at its registered URL goes through, and gets back the unit of
    budget :func:`_authorize_registration` charged. A new registration, a URL
    change, reviving an unregistered row (its host may since have changed
    hands) and an unregister (called with no registration) are refused and
    keep it, like a wrong token: a refusal that cost nothing would tell anyone
    which URL is registered, at network speed.
    """
    if registration_token_state() != "published_default":
        return
    if reg is not None and reg.status == "active" and reg.platform_url == platform_url:
        await refund_rate_limit(request, _REGISTRATION_RATE_LIMIT)
        return
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=PUBLISHED_DEFAULT_REFUSED_DETAIL,
    )


def _registered_platform_title(
    reg: PlatformRegistration | None, sent_title: str
) -> str:
    """The title a register call records for the platform.

    The title becomes the name of the platform's company-managed integration,
    which every agent bound to it reads in its system prompt. On the published
    default the caller is anyone who knows the registered URL, so its
    heartbeat keeps the title already registered.
    """
    if reg is not None and registration_token_state() == "published_default":
        return reg.platform_title
    return sent_title


def _registered_business_vertical(
    reg: PlatformRegistration | None, sent_vertical: str | None
) -> str | None:
    """The business vertical a register call records for the platform.

    Part of what the platform declares, with its intents: the published
    default's heartbeat keeps the vertical already registered, as it leaves
    the declared intents alone (see ``register_platform``).
    """
    if reg is not None and registration_token_state() == "published_default":
        return reg.business_vertical
    # A platform build that predates a vertical rename still sends the old
    # name; it is recorded under the current one.
    return canonical_vertical(sent_vertical)


@dataclass(frozen=True)
class _RegistrationLicense:
    """The registration fields that decide a platform's edition.

    ``license_validated`` and the stored PEM make a connected platform grant
    Enterprise Edition (``ee_platform_status``: EE inheritance, the included
    ``agentic_source = ee_platform`` allowance, the quotas, the Filigran AI
    credential); ``platform_id`` is what the XTM sub-license is matched on;
    ``license_type`` is what the displayed contract reads when no license
    validated.
    """

    platform_id: str | None
    license_validated: bool
    license_type: str | None
    pem: str | None


def _sent_registration_license(
    body: RegisterRequest, license_info: LicenseInfo
) -> _RegistrationLicense:
    validated = license_info.license_validated
    return _RegistrationLicense(
        platform_id=body.platform_id,
        license_validated=validated,
        license_type=license_info.license_type if validated else body.license_type,
        pem=body.enterprise_license_pem if validated and body.enterprise_license_pem else None,
    )


def _license_is_newer(sent: LicenseInfo, stored: LicenseInfo) -> bool:
    """Whether *sent* is a later certificate than *stored*: a later expiry,
    or the same expiry issued later."""
    return (sent.license_expiration_date, sent.license_start_date) > (
        stored.license_expiration_date,
        stored.license_start_date,
    )


def _same_license_holder(sent: LicenseInfo, stored: LicenseInfo) -> bool:
    """Whether *sent* is issued to the customer (``O``) and the instance
    binding (``OU``) of *stored*: what a renewal of *stored* keeps."""
    return (sent.license_customer, sent.license_platform) == (
        stored.license_customer,
        stored.license_platform,
    )


def _license_bound_to(info: LicenseInfo, platform_id: str | None) -> bool:
    """Whether *info* may serve the registration of *platform_id*.

    The instance check every product makes on its own license (``OU`` is the
    instance id it registers as ``platform_id``, or ``global``), which
    ``validate_platform_license`` cannot make: it has no instance to compare
    with. A registration without an id has nothing to bind an instance's
    license to, so only a ``global`` one may serve it.
    """
    return info.license_global or (bool(platform_id) and info.license_platform == platform_id)


# platform_identifier -> the last unrecorded license logged for it, so the
# heartbeat repeating every 5 minutes logs a change, not each beat.
_unrecorded_license_logged: dict[str, tuple[str | None, str, str, datetime, str]] = {}


def _log_unrecorded_license(reg: PlatformRegistration, sent_info: LicenseInfo, reason: str) -> None:
    """Say once per change why a license that validated is not recorded (never its PEM)."""
    seen = (
        reg.platform_id,
        sent_info.license_platform,
        sent_info.license_customer,
        sent_info.license_expiration_date,
        reason,
    )
    if _unrecorded_license_logged.get(reg.platform_identifier) == seen:
        return
    _unrecorded_license_logged[reg.platform_identifier] = seen
    logger.info(
        "Platform %s: a license that validated is not recorded (%s): stored platform_id=%r, "
        "license ou=%r, customer=%r, expires=%s",
        reg.platform_identifier,
        reason,
        reg.platform_id,
        sent_info.license_platform,
        sent_info.license_customer,
        sent_info.license_expiration_date.date().isoformat(),
    )


def _registered_license(
    reg: PlatformRegistration | None,
    sent: _RegistrationLicense,
    sent_info: LicenseInfo,
) -> _RegistrationLicense:
    """The edition fields a register call records.

    On the published default the caller is anyone who knows the registered
    URL, so its heartbeat can neither clear nor downgrade what is stored, nor
    swap in a license of its own choosing: a stored license that still
    validates is kept unless the call brings its renewal - a license the
    platform license validator accepts, newer than the stored certificate and
    issued to the same customer and instance binding - and a registration
    holding no such license takes only one bound to its ``platform_id`` (or
    ``global``). The stored ``platform_id`` is kept. Only the stored
    certificate's own validity can end it, read afresh on every heartbeat, so
    an expired license never outlives its grace, and a validated flag with no
    readable certificate behind it (a row older than the stored certificate)
    is no license at all. Everything else in the call proceeds. On a
    token of the operator's own the call is recorded as sent.

    The stored certificate is read against the registration's own, canonical
    identifier, never the spelling this call sent: during a product rename a
    platform not yet upgraded still sends the old one, whose validator refuses
    a certificate issued under the new name (``PRODUCT_CA_CERTS``), and the
    renewal an upgraded instance recorded would read as no license at all.
    """
    if reg is None or registration_token_state() != "published_default":
        return sent
    stored_pem: str | None = None
    stored_info: LicenseInfo | None = None
    if reg.enterprise_license_pem_enc:
        try:
            stored_pem = decrypt_value(reg.enterprise_license_pem_enc)
        except Exception:
            stored_pem = None
        stored_info = (
            validate_platform_license(reg.platform_identifier, stored_pem) if stored_pem else LicenseInfo()
        )
    if stored_info is not None and stored_info.license_validated:
        stored = _RegistrationLicense(reg.platform_id, True, reg.license_type, stored_pem)
        renewal = (
            sent.license_validated
            and _same_license_holder(sent_info, stored_info)
            and _license_is_newer(sent_info, stored_info)
        )
        # Resending the stored certificate is the steady state, not a refusal.
        refusal = None
        if sent.license_validated and not renewal:
            resent = _same_license_holder(sent_info, stored_info) and not _license_is_newer(stored_info, sent_info)
            refusal = None if resent else "stored license kept"
    else:
        # The type a lapsed or unreadable certificate stated goes with it.
        stored = _RegistrationLicense(
            reg.platform_id, False, reg.license_type if stored_info is None else None, None
        )
        renewal = sent.license_validated and _license_bound_to(sent_info, reg.platform_id)
        refusal = "not bound to the stored platform_id" if sent.license_validated and not renewal else None
    if refusal:
        _log_unrecorded_license(reg, sent_info, refusal)
    if not renewal:
        return stored
    _unrecorded_license_logged.pop(reg.platform_identifier, None)
    return _RegistrationLicense(
        platform_id=reg.platform_id,
        license_validated=True,
        license_type=sent.license_type,
        pem=sent.pem,
    )


async def _sync_platform_intents(
    db: AsyncSession,
    platform_identifier: str,
    declared_intents: list,
    business_vertical: str | None,
) -> None:
    """Sync intents declared by a platform.

    1. Upsert Vertical from business_vertical
    2. Upsert Intent rows for each declared intent
    3. Sync PlatformIntentDeclaration rows
    """
    from app.api.v1.helpers import get_or_create_intent, get_or_create_vertical
    from app.models.intent import Intent, PlatformIntentDeclaration

    # A platform build that predates a vertical rename still declares the old
    # vertical and intent names: they resolve to the current ones, so the old
    # rows are never recreated next to the migrated ones.
    business_vertical = canonical_vertical(business_vertical)
    declared_intents = [decl.model_copy(update={"name": canonical_intent_name(decl.name)}) for decl in declared_intents]

    # 1. Upsert vertical from business_vertical (race-safe ON CONFLICT
    # upsert — a registration can run concurrently with the startup seed
    # pass on a booting worker)
    if business_vertical:
        _, vert_created = await get_or_create_vertical(
            db,
            business_vertical,
            source="platform",
            platform_identifier=platform_identifier,
        )
        if vert_created:
            logger.info(
                "Auto-created vertical '%s' from platform '%s'",
                business_vertical,
                platform_identifier,
            )

    if not declared_intents:
        await db.commit()
        return

    # 2. Upsert Intent rows (race-safe ON CONFLICT upsert)
    declared_names = {i.name for i in declared_intents}
    existing_result = await db.execute(
        select(Intent).where(Intent.name.in_(declared_names))
    )
    existing_intents = {i.name: i for i in existing_result.scalars().all()}

    for decl in declared_intents:
        if decl.name not in existing_intents:
            intent, intent_created = await get_or_create_intent(
                db, decl.name, decl.description or None
            )
            existing_intents[decl.name] = intent
            if intent_created:
                logger.info(
                    "Auto-created intent '%s' from platform '%s'",
                    decl.name,
                    platform_identifier,
                )
        elif decl.description and not existing_intents[decl.name].description:
            existing_intents[decl.name].description = decl.description

    await db.flush()

    # 3. Sync PlatformIntentDeclaration (add new, remove absent)
    pid_result = await db.execute(
        select(PlatformIntentDeclaration).where(
            PlatformIntentDeclaration.platform_identifier == platform_identifier
        )
    )
    existing_decls = {str(d.intent_id): d for d in pid_result.scalars().all()}

    # Add new declarations (ON CONFLICT DO NOTHING so two concurrent
    # registrations from the same platform can't crash on
    # ``uq_platform_intent``)
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    intent_ids_now = set()
    for decl in declared_intents:
        intent_obj = existing_intents[decl.name]
        intent_id_str = str(intent_obj.id)
        intent_ids_now.add(intent_id_str)
        if intent_id_str not in existing_decls:
            await db.execute(
                pg_insert(PlatformIntentDeclaration)
                .values(
                    platform_identifier=platform_identifier,
                    intent_id=intent_obj.id,
                )
                .on_conflict_do_nothing(constraint="uq_platform_intent")
            )

    # Remove declarations for intents no longer sent
    for intent_id_str, decl_obj in existing_decls.items():
        if intent_id_str not in intent_ids_now:
            await db.delete(decl_obj)

    await db.commit()


# ── Public endpoint ──────────────────────────────────────────────────────────


class RegistrationTokenResponse(BaseModel):
    registration_token: str
    state: RegistrationTokenState = Field(
        description=(
            "unset: registration is disabled; published_default: the public "
            "docker-compose.yml fallback, which authenticates nobody and must "
            "never be handed out as the value to configure; configured: a "
            "token of the operator's own."
        )
    )
    generate_command: str = Field(
        description="A command that prints a random token to set on XTM One and on every platform."
    )


@router.get("/registration-token", response_model=RegistrationTokenResponse)
async def get_registration_token(
    _admin: User = Depends(require_admin),
) -> RegistrationTokenResponse:
    """Return the platform registration token and its state. Admin-only.

    The token is a shared secret that authorizes platform ``/register`` and
    ``/unregister`` (those endpoints are guarded by the token, not by user
    role), so it must never be readable by non-admin users. ``state`` tells
    the UI whether the value is one to paste into a platform at all.
    """
    return RegistrationTokenResponse(
        registration_token=configured_registration_token(),
        state=registration_token_state(),
        generate_command=REGISTRATION_TOKEN_GENERATE_COMMAND,
    )


def _branding_asset_response(
    asset: BrandingAsset,
    *,
    if_none_match: str | None = None,
    versioned: bool = False,
) -> StarletteResponse:
    # ``public``: branding is served to anyone (login page, manifest, social
    # crawlers). A day in any cache only for a URL whose ``?v=`` names these
    # bytes; the bare path keeps its URL across an admin upload.
    cache_control = "public, max-age=86400" if versioned else "public, no-cache"
    headers = {"ETag": asset.etag, "Cache-Control": cache_control}
    if if_none_match_hits(if_none_match, asset.etag):
        return StarletteResponse(status_code=304, headers=headers)
    return StarletteResponse(content=asset.data, media_type=asset.mime, headers=headers)


@router.get("/branding/asset/{kind}")
async def get_branding_asset(
    kind: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Return the raw bytes of an admin-uploaded branding asset.

    Allows the dynamic manifest to reference uploaded icons via URL
    (data URLs are not valid manifest ``icons[].src`` values), and lets
    the browser cache them across navigations.

    ``kind`` must be one of ``logo``, ``favicon``, ``og-image``.  Returns
    404 when the field is unset or holds a non-data-URL value (legacy
    ``platform_logo_url`` or an absolute OG image URL — clients should
    consume those from ``/platform/config`` directly).
    """
    branding = await get_branding(db)
    asset = resolve_branding_asset(branding, kind)
    if asset is None:
        raise HTTPException(status_code=404, detail="Branding asset not found")
    versions = branding_asset_versions(branding, kind, is_xtm=settings.is_xtm_one)
    return _branding_asset_response(
        asset,
        if_none_match=request.headers.get("if-none-match"),
        versioned=request.query_params.get("v") in versions,
    )


@router.get("/manifest.webmanifest")
async def get_manifest(db: AsyncSession = Depends(get_db)):
    """Return a dynamic Web App Manifest reflecting current branding settings.

    No authentication required — browsers fetch this on page load.
    Text fields (name, description, theme_color, background_color) are
    populated from admin-configured branding with mode-specific defaults.
    When an admin-uploaded favicon is present, the manifest references it
    via the binary ``/branding/asset/favicon`` endpoint; otherwise we fall
    back to the mode-specific static icons bundled with the frontend.
    """
    branding = await get_branding(db)
    return JSONResponse(
        content=build_web_manifest(branding, is_xtm=settings.is_xtm_one),
        media_type="application/manifest+json",
    )


@router.get("/config", response_model=PlatformConfigResponse)
async def get_platform_config(db: AsyncSession = Depends(get_db)):
    """Return platform mode, branding, license status, and feature flags.

    No authentication required — the frontend fetches this on boot before login.

    OpenCTI ↔ OpenAEV link (``integration_status``)
    ----------------------------------------------
    The response carries the connectivity of both products and when it was
    last verified, so the state of the integration link between OpenCTI and
    OpenAEV can be read in a single unauthenticated call (status page,
    external probe, support triage):

    * ``integration_status.opencti`` / ``.openaev`` — per-product ``status``
      (``connected`` / ``disconnected`` / ``not_registered``), ``connected``,
      and ``last_checked_at``.
    * ``integration_status.linked`` — ``True`` only when BOTH products are
      currently connected, i.e. the link is live end to end.
    * ``integration_status.last_checked_at`` — the freshest of the two
      verification timestamps, so one read tells how current ``linked`` is.

    "Verified" means the last heartbeat: a platform keeps its registration
    alive by re-registering (~5 min), and a heartbeat older than
    ``HEARTBEAT_HEALTHY_WINDOW`` flips the product to ``disconnected``. Read
    live from the DB on every call — never cached, or the timestamp would
    misreport its own freshness.

    XTM One ``edition`` (single source of truth for gating)
    -------------------------------------------------------
    The response carries an explicit ``edition`` so the frontend doesn't
    re-derive the licence rule in multiple hooks (see ``EDITION_*``):

    * ``enterprise`` — a valid Filigran license OR a connected Enterprise Edition
      platform. Everything is on (``agentic_enabled``); ``agentic_quotas`` /
      ``agentic_source`` say which ceilings apply and where they come from
      (the license's own figures, or the included 30/100/300 allowance when
      Enterprise Edition is inherited from a connected EE platform).
    * ``community``  — no Filigran license and no EE platform. AI is off: chat
      is blocked at the API layer (``is_chat_enabled``) and at the UI layer
      (``chat_enabled``), creation flows are refused, and the home page
      leads with the upgrade path. The rest of the app stays navigable so
      users can browse configuration and chat history.

    Copilot mode does not use ``edition``; it relies on the
    ``license_required`` / ``license_valid`` pair instead.
    """
    ps = await _load_ps(db)
    branding = await get_branding(db)
    integration_status = await _load_integration_status(db)

    # Resolved once, above the branch: both response sites must report the same
    # value, and a field default plus one forgotten branch is exactly how the
    # licence-wall defect looked.
    # ``_for_display``, not the gate: reporting "off" because the database
    # blinked makes every client render the AI-disabled landing on a healthy
    # platform. Gating elsewhere still fails closed.
    from app.services.ai_state import ai_enabled_for_display

    ai_enabled = await ai_enabled_for_display(db)

    is_xtm = settings.is_xtm_one

    if is_xtm:
        xtm_info = resolve_xtm_license(ps)
        has_ee, active_reg_count = await ee_platform_status(db, xtm_info)
        edition = resolve_deployment_edition(
            True, xtm_validated=xtm_info.license_validated, has_ee=has_ee
        )
        licensed = edition == EDITION_ENTERPRISE
        # Always through ``is_chat_enabled`` — never re-derived here. Two copies
        # of the rule is what left the AI short-circuit out of the payload, so
        # the composer stayed live while the send route refused every send. The
        # licence answer is handed in because this branch has just resolved
        # it, and resolving it again costs a second ``ee_platform_status`` on
        # an unauthenticated endpoint hit on every page load.
        chat_enabled = await is_chat_enabled(db, licensed=licensed)

        lic_source = (
            "env_var"
            if xtm_info.license_by_configuration
            else "database" if xtm_info.license_validated else "none"
        )

        if xtm_info.license_validated:
            # Enterprise Edition, quotas from the license. The license decides
            # the ceilings and the module sub-licenses — nothing else; every
            # capability of the core is on whatever the certificate's legacy
            # capability flags say.
            xtm_license = XTMLicense(
                valid=True,
                customer=xtm_info.license_customer,
                license_type=xtm_info.license_type,
                expiration=xtm_info.license_expiration_date.isoformat(),
                expired=xtm_info.license_expired,
                expiration_prevention=xtm_info.license_expiration_prevention,
                extra_expiration=xtm_info.license_extra_expiration,
                extra_expiration_days=xtm_info.license_extra_expiration_days,
                agentic_quotas=AgenticQuotas.from_license(xtm_info),
                license_source=lic_source,
                sub_licenses=SubLicenses.from_license(xtm_info),
            )
            agentic_quotas = xtm_license.agentic_quotas
            agentic_source = "xtm_license"
            license_customer = xtm_info.license_customer
            license_type = xtm_info.license_type
            license_expiration = xtm_info.license_expiration_date.isoformat()
        elif has_ee:
            # Enterprise Edition inherited from a connected EE platform, no
            # Filigran license installed: the same fully activated platform, under
            # the included allowance. Installing a license only changes the
            # ceilings.
            xtm_license = XTMLicense(valid=False)
            agentic_quotas = AgenticQuotas(
                daily=DEFAULT_AGENTIC_DAILY_QUOTA,
                monthly=DEFAULT_AGENTIC_MONTHLY_QUOTA,
                yearly=DEFAULT_AGENTIC_YEARLY_QUOTA,
            )
            agentic_source = "ee_platform"
            license_customer = None
            license_type = None
            license_expiration = None
        else:
            # Community Edition: no Filigran license, no EE platform registered.
            # AI is off — chat is blocked (both API and UI), creation flows
            # refuse, and the home page leads with the upgrade path. The rest
            # of the app stays navigable so users can still explore
            # configuration and conversation history.
            xtm_license = XTMLicense(valid=False)
            agentic_quotas = AgenticQuotas()
            agentic_source = "none"
            license_customer = None
            license_type = None
            license_expiration = None

        branding_fields = resolved_branding_payload(branding, is_xtm=True)
        return PlatformConfigResponse(
            instance_id=str(ps.id) if ps else "",
            platform_mode=settings.platform_mode,
            **branding_fields,
            platform_language=_platform_language(ps),
            platform_version=settings.app_version,
            license_required=False,
            license_valid=True,
            license_customer=license_customer,
            license_type=license_type,
            license_expiration=license_expiration,
            # Every Enterprise feature follows the ONE licence answer — the
            # catalog, the templates and AI adoption included. Keying them on
            # ``license_validated`` alone kept them off for a deployment whose
            # Enterprise Edition came from a connected EE platform.
            features=PlatformFeatures(
                ai_adoption=licensed,
                all_integrations=licensed,
                all_templates=licensed,
                has_ee_platform=has_ee,
                active_registration_count=active_reg_count,
            ),
            integration_status=integration_status,
            xtm_license=xtm_license,
            agentic_enabled=licensed,
            agentic_quotas=agentic_quotas,
            agentic_source=agentic_source,
            edition=edition,
            chat_enabled=chat_enabled,
            disable_code_interpreter=security_option("disable_code_interpreter"),
            disable_custom_tools=security_option("disable_custom_tools"),
            disable_custom_variables=security_option("disable_custom_variables"),
            ai_catalog_enabled=settings.ai_catalog_enabled,
            ai_enabled=ai_enabled,
        )

    # Copilot mode — requires a valid Filigran license. The old separate Copilot CA
    # is deprecated; all licensing now goes through the unified Filigran XTM
    # license, and the retired ``copilot`` capability flag is not consulted:
    # a valid license is a valid license.
    xtm_info = resolve_xtm_license(ps)
    license_ok = xtm_info.license_validated
    chat_enabled = await is_chat_enabled(db, licensed=license_ok)

    copilot_lic_source = (
        "env_var"
        if xtm_info.license_by_configuration
        else "database" if xtm_info.license_validated else "none"
    )

    xtm_license_resp = None
    if xtm_info.license_validated:
        xtm_license_resp = XTMLicense(
            valid=True,
            customer=xtm_info.license_customer,
            license_type=xtm_info.license_type,
            expiration=xtm_info.license_expiration_date.isoformat(),
            expired=xtm_info.license_expired,
            expiration_prevention=xtm_info.license_expiration_prevention,
            extra_expiration=xtm_info.license_extra_expiration,
            extra_expiration_days=xtm_info.license_extra_expiration_days,
            agentic_quotas=AgenticQuotas.from_license(xtm_info),
            license_source=copilot_lic_source,
            sub_licenses=SubLicenses.from_license(xtm_info),
        )

    branding_fields = resolved_branding_payload(branding, is_xtm=False)
    return PlatformConfigResponse(
        instance_id=str(ps.id) if ps else "",
        platform_mode=settings.platform_mode,
        **branding_fields,
        platform_language=_platform_language(ps),
        platform_version=settings.app_version,
        license_required=True,
        license_valid=license_ok,
        license_customer=(xtm_info.license_customer if license_ok else None),
        license_type=xtm_info.license_type if license_ok else None,
        license_expiration=(
            xtm_info.license_expiration_date.isoformat() if license_ok else None
        ),
        features=PlatformFeatures(
            ai_adoption=license_ok,
            all_integrations=license_ok,
            all_templates=license_ok,
            has_ee_platform=False,
            active_registration_count=0,
        ),
        # Copilot mode has no platform registrations of its own, but read the
        # table anyway rather than hardcoding "not registered" — the payload
        # then means the same thing in both modes.
        integration_status=integration_status,
        xtm_license=xtm_license_resp,
        agentic_enabled=license_ok,
        agentic_quotas=(
            AgenticQuotas.from_license(xtm_info) if license_ok else AgenticQuotas()
        ),
        agentic_source="xtm_license" if license_ok else "none",
        # Copilot mode has no XTM One edition — leave it empty. Chat in
        # Copilot mode mirrors the license gate.
        edition="",
        chat_enabled=chat_enabled,
        disable_code_interpreter=security_option("disable_code_interpreter"),
        disable_custom_tools=security_option("disable_custom_tools"),
        disable_custom_variables=security_option("disable_custom_variables"),
        ai_catalog_enabled=settings.ai_catalog_enabled,
        ai_enabled=ai_enabled,
    )


def _xtm_sublicense_grants_ee(
    xtm_info: XTMLicenseInfo,
    platform_identifier: str,
    platform_id: str | None,
) -> bool:
    """Whether the Filigran license sub-licenses this platform instance.

    True when the validated Filigran license lists the ``platform_id`` under the
    platform type's sub-license ids, or carries the ``"global"`` grant for
    the platform type. Shared by :func:`_registration_ee_sources` (register
    response, including whether ``xtm_license_pem`` travels),
    :func:`_resolve_edition` callers (contract + integration tag), and
    mirrored by :func:`ee_platform_status`, so every edition surface
    resolves from the same predicate.
    """
    if not xtm_info.license_validated:
        return False
    ids = xtm_sublicensed_ids(xtm_info, platform_identifier)
    return "global" in ids or (platform_id is not None and platform_id in ids)


def _registration_ee_sources(
    reg: PlatformRegistration, xtm_info: XTMLicenseInfo
) -> list[RegistrationEESource]:
    """Every path that grants a single registration EE, in a fixed order.

    Mirrors the per-registration half of :func:`ee_platform_status`:
    ``platform_license`` when the platform presented a server-validated
    enterprise license PEM, ``xtm_sublicense`` when the Filigran license
    sub-licenses this platform (``opencti_ids`` / ``openaev_ids`` /
    ``opencrq_ids`` matching the ``platform_id``, or ``"global"`` for the
    platform type). Both may apply. The one resolution behind the register
    response's ``ee_enabled``, ``ee_sources`` and ``xtm_license_pem``, so the
    three can never disagree.
    """
    sources: list[RegistrationEESource] = []
    if reg.license_validated:
        sources.append(EE_SOURCE_PLATFORM_LICENSE)
    if _xtm_sublicense_grants_ee(xtm_info, reg.platform_identifier, reg.platform_id):
        sources.append(EE_SOURCE_XTM_SUBLICENSE)
    return sources


_license_withheld_warned = False


def _registration_license_withheld_reason(
    ee_sources: list[RegistrationEESource],
) -> LicenseWithheldReason | None:
    """Why the register answer withholds ``xtm_license_pem``, or ``None``.

    Only an ``xtm_sublicense`` grant has a proof to withhold, and it is
    withheld while the registration token is the published default:
    that value is public, so it would hand the license to anyone. The answer
    names the reason so the product can tell the operator what to fix.
    Warned once per process, not on every heartbeat, and never with a token
    value in the message.
    """
    global _license_withheld_warned
    if EE_SOURCE_XTM_SUBLICENSE not in ee_sources:
        return None
    if registration_token_state() != "published_default":
        return None
    if not _license_withheld_warned:
        _license_withheld_warned = True
        logger.warning(
            "The Filigran license certificate is withheld from platform registrations while "
            "PLATFORM_REGISTRATION_TOKEN is the published default value: sub-licensed "
            "platforms cannot prove their Enterprise Edition until a non-default token "
            "is set here and on every connected platform."
        )
    return LICENSE_WITHHELD_PUBLISHED_DEFAULT


def _registration_license_pem(
    xtm_info: XTMLicenseInfo,
    ee_sources: list[RegistrationEESource],
    withheld_reason: LicenseWithheldReason | None,
) -> str | None:
    """The ``xtm_license_pem`` a register answer carries, or ``None``.

    Present only as the proof of an ``xtm_sublicense`` grant, and never when
    :func:`_registration_license_withheld_reason` gave a reason. ``ee_enabled``
    and ``ee_sources`` are left as computed; the product then refuses the XTM
    entitlement itself.
    """
    if EE_SOURCE_XTM_SUBLICENSE not in ee_sources or withheld_reason is not None:
        return None
    return xtm_info.license_certificate_pem


def _registration_ee_enabled(
    reg: PlatformRegistration, xtm_info: XTMLicenseInfo
) -> bool:
    """Effective EE status of a single registration, for the register response.

    True when any path of :func:`_registration_ee_sources` applies, so it
    reflects the XTM-license source of truth and not just the legacy PEM path.
    Advisory for the products: the XTM path is proven by
    ``xtm_license_pem``, the PEM path by the platform's own license.
    """
    return bool(_registration_ee_sources(reg, xtm_info))


def _keep_admin_extra_fields(
    provider: str, stored_enc: str | None, registered_json: str
) -> str:
    """The registration's extra config, plus the admin-set keys of the stored one.

    Only the catalog's ``registration_editable_extra_fields`` carry over; an
    unreadable stored config carries nothing (the registration still lands).
    """
    from app.api.v1.integration_catalog import registration_editable_extra_fields

    editable = registration_editable_extra_fields(provider)
    if not editable or not stored_enc:
        return registered_json
    try:
        stored = decrypt_json(stored_enc) or {}
    except Exception:
        return registered_json
    if not isinstance(stored, dict):
        return registered_json
    merged = json.loads(registered_json)
    merged.update({k: v for k, v in stored.items() if k in editable})
    return json.dumps(merged)


# Platforms already told that the published default token gets them no sign-in
# client: said once per process, not on every heartbeat.
_SIGN_IN_REFUSAL_LOGGED: set[str] = set()


async def _sync_sign_in_client(db: AsyncSession, body: RegisterRequest, platform_title: str) -> str | None:
    """Keep the platform's sign-in client in step with this registration.

    Never with the published default token: anyone knows it, so a client it
    created would be no more trusted than a self-registered one, and it must
    not remove the client of a platform either. The callbacks
    were checked on the way in (:func:`register_platform`).
    """
    if body.sign_in_redirect_uris is None:
        # The heartbeat of every platform that does not sign users in: no query.
        return None
    if registration_token_state() == "published_default":
        # Neither created nor removed with it.
        if body.sign_in_redirect_uris and body.platform_identifier not in _SIGN_IN_REFUSAL_LOGGED:
            _SIGN_IN_REFUSAL_LOGGED.add(body.platform_identifier)
            logger.warning(
                "Platform '%s' asks for a sign-in client, refused with the published default "
                "registration token: set PLATFORM_REGISTRATION_TOKEN",
                body.platform_identifier,
            )
        return None
    if not body.sign_in_redirect_uris:
        # The platform stopped signing users in.
        await platform_sign_in_clients.remove_platform_sign_in_client(db, body.platform_identifier)
        return None
    return await platform_sign_in_clients.sync_platform_sign_in_client(
        db,
        platform_identifier=body.platform_identifier,
        platform_title=platform_title,
        redirect_uris=body.sign_in_redirect_uris,
    )


async def _record_procedure_manifest(db: AsyncSession, reg: PlatformRegistration, body: RegisterRequest) -> bool:
    """Record what a registration says of its procedure manifest; whether to ask for the manifest.

    Under the published default token nothing is recorded and the manifest is never asked for: its
    caller authenticates nobody, and what the manifest hides from the model is decided by it
    (the license is withheld the same way). A fingerprint that does not read as one is ignored and
    logged, so is a manifest that does not read, exceeds the size cap or carries another
    fingerprint: a registration is also the heartbeat, so nothing here may refuse it.

    The fingerprint reported is kept on the registration, ``None`` when none was reported: a
    manifest stored under another fingerprint decides nothing (the build fails open). The manifest is
    kept in a savepoint, so a failed write leaves the registration unharmed. Asked for exactly when a
    valid fingerprint came without a manifest and none is stored under it.
    """
    if registration_token_state() == "published_default":
        return False
    fingerprint = valid_fingerprint(body.procedure_manifest_fingerprint)
    reg.procedure_manifest_fingerprint = fingerprint
    sent = body.procedure_manifest
    if fingerprint is None:
        if body.procedure_manifest_fingerprint is not None or sent is not None:
            logger.warning(
                "Platform '%s' sent a procedure manifest without a fingerprint that reads as sha256 hex; ignored",
                body.platform_identifier,
            )
        return False
    try:
        if sent is None:
            # The heartbeat: the fingerprint alone says whether the manifest is held, nothing is written.
            held_fingerprint = (
                await db.execute(
                    select(PlatformProcedureManifest.fingerprint).where(
                        PlatformProcedureManifest.registration_id == reg.id
                    )
                )
            ).scalar_one_or_none()
            return held_fingerprint != fingerprint
        async with db.begin_nested():
            stored = (
                await db.execute(
                    select(PlatformProcedureManifest).where(PlatformProcedureManifest.registration_id == reg.id)
                )
            ).scalar_one_or_none()
            held = stored is not None and stored.fingerprint == fingerprint
            if held:
                return False
            manifest = parse_registered_manifest(sent, fingerprint)
            if manifest is None:
                logger.warning(
                    "Platform '%s' sent a procedure manifest that does not read or does not match its fingerprint; ignored",
                    body.platform_identifier,
                )
                return False
            procedures = {path: dict(entry) for path, entry in manifest.procedures.items()}
            if stored is None:
                db.add(
                    PlatformProcedureManifest(registration_id=reg.id, fingerprint=fingerprint, procedures=procedures)
                )
            else:
                stored.fingerprint = fingerprint
                stored.procedures = procedures
                stored.received_at = datetime.now(UTC)
            await db.flush()
            return False
    except Exception as exc:
        logger.warning(
            "Could not record the procedure manifest of platform '%s': %s",
            body.platform_identifier,
            format_exception_message(exc),
        )
        return False


# The manifest the platform may carry, plus the rest of the registration (license PEM, intents).
_REGISTER_BODY_MAX_BYTES = MAX_MANIFEST_BYTES + 1024 * 1024


async def _registration_authorized(request: Request, authorization: str | None = Header(None)) -> None:
    """:func:`_authorize_registration` as the dependency :func:`_bounded_register_body` depends on.

    FastAPI resolves dependencies before the route runs, so a check made in the
    route would come after the body is read: a caller with no valid token could
    make the worker buffer and parse up to :data:`_REGISTER_BODY_MAX_BYTES` per
    request, outside the budget of failed checks.
    """
    await _authorize_registration(request, authorization)


async def _bounded_register_body(
    request: Request, _authorized: Annotated[None, Depends(_registration_authorized)]
) -> RegisterRequest:
    """The registration body, read under :data:`_REGISTER_BODY_MAX_BYTES` before it is parsed.

    Read only once the token check passed. A body that does not validate
    answers the 422 FastAPI gives a model body.
    """
    raw = await read_bounded_body(request, _REGISTER_BODY_MAX_BYTES)
    if raw is None:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Registration too large")
    try:
        return RegisterRequest.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**error, "loc": ("body", *error["loc"])} for error in exc.errors(include_url=False)]
        ) from None


def _register_body_schema() -> dict[str, Any]:
    """:class:`RegisterRequest`'s JSON Schema with its ``$defs`` inlined: the route reads the body itself,
    so FastAPI adds no component for it to point at."""
    schema = RegisterRequest.model_json_schema()
    defs = schema.pop("$defs", {})

    def inline(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return inline(defs[ref.removeprefix("#/$defs/")])
            return {key: inline(value) for key, value in node.items()}
        if isinstance(node, list):
            return [inline(item) for item in node]
        return node

    return inline(schema)


@router.post(
    "/register",
    response_model=RegistrationResponse,
    openapi_extra={
        "requestBody": {"required": True, "content": {"application/json": {"schema": _register_body_schema()}}}
    },
)
async def register_platform(
    body: Annotated[RegisterRequest, Depends(_bounded_register_body)],
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Register an OpenCTI, OpenAEV, or OpenCRQ platform with XTM One.

    Accepts a list of platform users — each matched by email to a local
    XTM One user.  One integration is created per matched user so every
    user gets their own scoped connection to the platform.

    The registration token is checked by ``_bounded_register_body``'s own
    dependency, before the body is read.
    """
    # A misconfigured sign-in client is refused before anything is written. Logged as an
    # error: the registration is also the heartbeat, so the refusal ends the held entitlement too.
    # Not on the published default token: anyone may send it, so the log would carry anyone's text.
    if body.sign_in_redirect_uris:
        try:
            platform_sign_in_clients.check_identifier(_canonical_platform_identifier(body.platform_identifier))
            platform_sign_in_clients.check_redirect_uris(body.platform_url, body.sign_in_redirect_uris)
        except HTTPException as exc:
            if registration_token_state() != "published_default":
                logger.error(
                    "Registration of platform '%s' at %s refused, its sign-in client is not valid: %s",
                    body.platform_identifier,
                    body.platform_url,
                    exc.detail,
                )
            raise

    license_info = validate_platform_license(
        body.platform_identifier, body.enterprise_license_pem
    )
    # Normalised only after the licence check, which needs the identifier as
    # sent: a pre-rename licence carries the old product string and validates
    # solely against the legacy ``PRODUCT_CA_CERTS`` entry. Everything below
    # — EE resolution, the persisted registration, the integration provider
    # key, intent sync — must see the canonical spelling instead, or an
    # OpenGRC platform would register into rows no canonical lookup finds.
    body.platform_identifier = _canonical_platform_identifier(
        body.platform_identifier
    )
    await _lock_platform_registration(db, body.platform_identifier)
    # Loaded up-front (not just for the response) because the Filigran license
    # sub-licensing path must feed the displayed contract/tag too, not only
    # the ``ee_enabled`` response field — otherwise an XTM-sublicensed
    # platform with no product PEM (OpenCRQ) registers as EE but every UI
    # surface keeps showing CE.
    xtm_info = resolve_xtm_license(await _load_ps(db))

    # ── Upsert platform registration ────────────────────────────────────
    result = await db.execute(
        select(PlatformRegistration).where(
            PlatformRegistration.platform_identifier == body.platform_identifier
        )
    )
    reg = result.scalar_one_or_none()
    previous_platform_url = reg.platform_url if reg else None
    # Read under the registration lock: the version the previous heartbeat
    # committed, compared after this one's commit (integration.version.changed).
    previous_platform_version = reg.platform_version if reg else None
    # A version change or a heartbeat back after a gap reads as a restart (#958).
    previous_heartbeat_at = reg.last_heartbeat_at if reg else None
    await _refuse_published_default_change(request, reg, body.platform_url)
    platform_title = _registered_platform_title(reg, body.platform_title)
    business_vertical = _registered_business_vertical(reg, body.business_vertical)
    recorded = _registered_license(
        reg,
        _sent_registration_license(body, license_info),
        license_info,
    )
    contract = _resolve_edition(
        license_validated=recorded.license_validated,
        license_type=recorded.license_type,
        xtm_sublicensed=_xtm_sublicense_grants_ee(
            xtm_info, body.platform_identifier, recorded.platform_id
        ),
    )

    # Persist the platform's EE license PEM (encrypted) ONLY when it validated
    # — this is the credential the Filigran AI extraction path forwards when
    # Enterprise Edition is inherited from this platform (no Filigran license).
    # Cleared (None) when the license is absent/invalid so a stale PEM never
    # outlives its entitlement.
    pem_enc = encrypt_value(recorded.pem) if recorded.pem else None

    if reg:
        reg.platform_url = body.platform_url
        reg.platform_title = platform_title
        reg.platform_version = body.platform_version
        reg.platform_id = recorded.platform_id
        # The capability is refreshed on every heartbeat, so shipping approval
        # support and re-registering restores gating on its own. The policy
        # columns are deliberately untouched: re-seeding them here would
        # revert an administrator's choice minutes after they made it, with
        # the settings page still showing what they picked.
        if body.supports_approval_prompts is not None:
            reg.supports_approval_prompts = body.supports_approval_prompts
        reg.contract = contract
        reg.license_type = recorded.license_type
        reg.license_validated = recorded.license_validated
        reg.enterprise_license_pem_enc = pem_enc
        reg.status = "active"
        reg.last_heartbeat_at = datetime.now(UTC)
    else:
        reg = PlatformRegistration(
            platform_identifier=body.platform_identifier,
            platform_url=body.platform_url,
            platform_title=platform_title,
            platform_version=body.platform_version,
            platform_id=recorded.platform_id,
            contract=contract,
            license_type=recorded.license_type,
            license_validated=recorded.license_validated,
            enterprise_license_pem_enc=pem_enc,
            status="active",
            supports_approval_prompts=body.supports_approval_prompts,
            # Seeded here and only here — this is the single moment a platform
            # has no operator decision behind it. ``_seed_platform_policy``
            # reads the deployment default; every later write comes from an
            # administrator.
            **_seed_platform_policy(),
        )
        db.add(reg)

    # ── Per-user integrations ───────────────────────────────────────────
    provider_key = body.platform_identifier
    if provider_key == "openbas":
        provider_key = "openaev"

    # Allow Docker/operator override: if an env var like OPENCTI_API_URL or
    # OPENAEV_API_URL is set, use that as the base_url for API calls instead
    # of the platform-reported URL (which may be an external/localhost URL
    # unreachable from within Docker).
    #
    # With the override, ``extra_config[base_url]`` (the internal URL)
    # differs from ``PlatformRegistration.platform_url`` (the external URL),
    # recorded as ``reg.api_url`` below: the scope matcher accepts either as
    # this platform, and every link a person is shown is moved onto
    # ``platform_url`` (``platform_identity.RegisteredPlatform.shown``).
    api_url_override, override_var = _resolve_api_url_override(provider_key)
    effective_base_url = api_url_override if api_url_override else body.platform_url
    # The platform stays known by ``platform_url``; its keys are fetched and its
    # autonomous run callbacks sent where XTM One reaches it, recorded here from
    # XTM One's own environment and nowhere a user can edit.
    from app.core.platform_identity import captured_api_url

    reg.api_url = captured_api_url(api_url_override, body.platform_url)
    if api_url_override:
        logger.info(
            "Platform '%s': base_url overridden from env %s (external=%s, internal=%s)",
            body.platform_identifier,
            override_var,
            body.platform_url,
            effective_base_url,
        )
    # Build the JSON via ``json.dumps`` rather than f-string interpolation
    # so an env value containing JSON-special characters (quote, backslash,
    # control character) cannot produce malformed JSON that downstream
    # ``decrypt_json`` / ``json.loads`` calls would fail on. Matches the
    # ``json.dumps(effective_extra)`` pattern used in
    # ``api/v1/integrations.py`` for the same field. The tokens sent to that
    # URL stay addressed to the platform's identity
    # (``platform_identity.platform_token_audience``).
    extra_config_json = json.dumps({"base_url": effective_base_url})

    # -- Upsert single company-managed integration ──────────────
    # Instead of per-user integrations, we create ONE company-managed
    # integration with auth_type="platform_managed".  No credentials are
    # stored — authentication is handled by the platform generating a JWT
    # for the user at runtime.
    #
    # The always-set-owner invariant requires company-managed
    # rows to keep a real ``user_id`` owner (the admin) — visibility is driven
    # by ``is_company_managed``, not by nulling the owner. This endpoint has no
    # authenticated user (it is token-authenticated), so the owner is resolved
    # to the admin via the shared resolver, matching the seed pass and the
    # owner-backfill migration.
    from app.api.v1.helpers import resolve_company_managed_owner_id

    owner_id = await resolve_company_managed_owner_id(db)

    integ_result = await db.execute(
        select(Integration).where(
            Integration.provider == provider_key,
            # access_restricted: bypass - token-authenticated registration upserts the platform's own row.
            Integration.is_company_managed.is_(True),
        )
    )
    company_integ = integ_result.scalar_one_or_none()

    if company_integ:
        # Update existing company-managed integration. The registration owns
        # the config, except the keys an admin sets on the integration page
        # (``registration_editable_extra_fields``, e.g. the OpenAEV tenant):
        # those survive every re-registration.
        company_integ.extra_config_enc = encrypt_value(
            _keep_admin_extra_fields(
                provider_key, company_integ.extra_config_enc, extra_config_json
            )
        )
        company_integ.name = platform_title
        company_integ.auth_type = "platform_managed"
        company_integ.status = "connected"
        # Backfill the owner on legacy ownerless rows (created before the
        # always-set-owner invariant) without rewriting an owner already set
        # by the migration or a prior registration.
        if company_integ.user_id is None and owner_id is not None:
            company_integ.user_id = owner_id
        # Mirror ``PlatformRegistration.contract`` (resolved via
        # ``_resolve_edition``) so the integration-list edition badge matches
        # the home-page platform card and the two surfaces never diverge.
        license_tag = contract
        tags = [t for t in (company_integ.tags or []) if t not in ("CE", "EE")]
        tags.append(license_tag)
        company_integ.tags = tags
        # Platform-managed integrations need scope disambiguation when
        # multiple instances of the same provider exist.
        company_integ.scope_config = _PLATFORM_SCOPE_CONFIG
    else:
        license_tag = contract
        company_integ = Integration(
            id=uuid.uuid4(),
            provider=provider_key,
            name=platform_title,
            auth_type="platform_managed",
            extra_config_enc=encrypt_value(extra_config_json),
            is_company_managed=True,
            user_id=owner_id,
            status="connected",
            tags=[license_tag],
            scope_config=_PLATFORM_SCOPE_CONFIG,
        )
        db.add(company_integ)

    await db.flush()

    # Link integration to platform registration
    reg.integration_id = company_integ.id
    reg.business_vertical = business_vertical
    manifest_wanted = await _record_procedure_manifest(db, reg, body)
    from app.services.health.snapshots import record_snapshot, registration_restarted

    await record_snapshot(
        db,
        reg,
        body.health,
        published_default=registration_token_state() == "published_default",
        restarted=registration_restarted(
            previous_platform_version, body.platform_version, previous_heartbeat_at, datetime.now(UTC)
        ),
    )
    sign_in_client_id = await _sync_sign_in_client(db, body, platform_title)

    await db.commit()
    # A registration can move the edition (an EE platform connecting, or
    # re-registering without its license): drop the background gate's cached
    # licence answer, after the commit.
    from app.services import agentic_gate

    agentic_gate.invalidate_cache()
    await db.refresh(reg)
    # A registered platform may frame the platform's pages: a new origin joins
    # the policy's frame-ancestors now, and the one a moved platform left
    # drops out, not at the next background refresh. Registration is
    # also the heartbeat, so a known origin - or any origin while registered
    # platforms may not frame - costs no query.
    from app.services.registered_platform_origins import refresh_for_registration

    await refresh_for_registration(reg.platform_url, previous_url=previous_platform_url)
    # A known platform reporting another version: an upgrade an assignment can
    # wait for instead of polling. Never on the published default token: its
    # caller authenticates nobody, so the version it sends proves nothing.
    if registration_token_state() != "published_default":
        from app.services.integration_versions import publish_version_change

        await publish_version_change(
            provider=provider_key,
            platform_identifier=reg.platform_identifier,
            integration_id=str(company_integ.id),
            previous_version=previous_platform_version,
            version=reg.platform_version,
        )

    logger.info(
        "Platform '%s' registered: contract=%s, company-managed integration=%s",
        body.platform_identifier,
        contract,
        str(company_integ.id),
    )

    # ── Sync intents declared by the platform ────────────────────────────
    # Never on the published default: its caller authenticates nobody, and the
    # sync creates intents and verticals every platform shares and replaces
    # this platform's declarations with whatever the call lists.
    if registration_token_state() != "published_default":
        try:
            await _sync_platform_intents(
                db, body.platform_identifier, body.intents, body.business_vertical
            )
        except Exception as exc:
            logger.warning("Intent sync failed for '%s': %s", body.platform_identifier, format_exception_message(exc))

    # Effective edition for the registering platform: validated PEM OR XTM
    # sub-license (``xtm_info`` loaded before the upsert, shared with the
    # contract resolution above). ``ee_enabled`` stays exactly what older
    # products read, but it is advisory.
    ee_sources = _registration_ee_sources(reg, xtm_info)
    withheld_reason = _registration_license_withheld_reason(ee_sources)
    return RegistrationResponse(
        status=reg.status,
        platform_identifier=reg.platform_identifier,
        ee_enabled=_registration_ee_enabled(reg, xtm_info),
        ee_sources=ee_sources,
        # The proof of the XTM path: the certificate the decode behind
        # ``xtm_info`` verified (whichever of ENTERPRISE_LICENSE and the
        # installed license won), re-serialised there, so exactly one
        # CERTIFICATE block leaves and never the stored text around it. No
        # chain travels: XTM One verifies the signature directly against the
        # pinned CA key, so an intermediate is never part of the decision.
        xtm_license_pem=_registration_license_pem(xtm_info, ee_sources, withheld_reason),
        xtm_license_withheld_reason=withheld_reason,
        user_integrations=1,  # Always 1 company-managed integration
        version=settings.app_version,
        procedure_manifest_wanted=manifest_wanted,
        sign_in_client_id=sign_in_client_id,
    )


@router.delete("/register/{platform_identifier}")
async def unregister_platform(
    platform_identifier: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: str | None = Header(None),
):
    """Unregister a platform."""
    await _authorize_registration(request, authorization)
    await _refuse_published_default_change(request)

    # A platform that has not shipped its own rename unregisters under the
    # name it still knows; the row it is looking for is stored canonically.
    platform_identifier = _canonical_platform_identifier(platform_identifier)
    await _lock_platform_registration(db, platform_identifier)

    result = await db.execute(
        select(PlatformRegistration).where(
            PlatformRegistration.platform_identifier == platform_identifier
        )
    )
    reg = result.scalar_one_or_none()
    if not reg:
        raise HTTPException(status_code=404, detail="Platform not registered")

    reg.status = "unregistered"
    await platform_sign_in_clients.remove_platform_sign_in_client(db, platform_identifier)
    await db.commit()
    from app.services import agentic_gate

    agentic_gate.invalidate_cache()
    from app.services.registered_platform_origins import refresh_registered_platform_origins

    await refresh_registered_platform_origins()
    return {"status": "unregistered", "platform_identifier": platform_identifier}


# How often an observation is refreshed. The value is only ever read as "has
# this happened", so rewriting it on every message would be a write on the chat
# hot path to change nothing. Half a day keeps it recent enough to reason about
# without that cost.
_APPROVAL_SUPPORT_REFRESH = timedelta(hours=12)


async def _note_client_approval_support(product: str | None) -> None:
    """Record that a client of ``product`` will answer approval prompts.

    Evidence for a nudge, never a control: it cannot make tools gate, only let
    the settings page offer that an operator reconsider. So every failure here
    is swallowed — a platform chat turn must not fail because a hint could not
    be written — and it runs in its own session, since the caller's is about to
    be held open for the length of a streamed answer.

    Attribution comes from ``X-Platform-Product``, which the host's chat proxy
    sets server-side. A direct client sends no such header and is not
    attributable to a registration, so nothing is recorded rather than guessed.
    """
    identifier = (product or "").strip().lower()
    if not identifier:
        return

    from sqlalchemy import func, or_

    from app.core.database import async_session
    from app.tools.approval import platform_identifiers

    now = datetime.now(UTC)
    try:
        async with async_session() as session:
            await session.execute(
                update(PlatformRegistration)
                .where(
                    # Compared case-insensitively: the header is normalised here
                    # while registration stores ``platform_identifier`` verbatim
                    # from the request body, so a platform that registers as
                    # "OpenCTI" would otherwise never match its own header and
                    # its observed capability would be silently dropped.
                    func.lower(PlatformRegistration.platform_identifier).in_(
                        platform_identifiers(identifier)
                    ),
                    or_(
                        PlatformRegistration.approval_support_seen_at.is_(None),
                        PlatformRegistration.approval_support_seen_at
                        < now - _APPROVAL_SUPPORT_REFRESH,
                    ),
                )
                .values(approval_support_seen_at=now)
            )
            await session.commit()
    except Exception:
        logger.debug(
            "Could not record client approval support for '%s'",
            identifier,
            exc_info=True,
        )


def _registration_out(r: PlatformRegistration) -> PlatformRegistrationOut:
    """Serialize one registration, deciding tool approval once.

    ``gates_tools`` is computed here rather than left to each caller so the
    settings page and the dashboard card can never disagree about whether a
    platform's tools are gated.
    """
    from app.tools.approval import platform_capability_nudge, platform_gates_tools

    _approval = {
        "supports_approval_prompts": r.supports_approval_prompts,
        "approval_policy": r.approval_policy,
        "approval_policy_set_by": r.approval_policy_set_by,
    }
    # Only the nudge weighs what a client was observed to do; gating never
    # does. One capable client does not speak for the platform.
    _nudge = platform_capability_nudge(
        **_approval,
        approval_support_seen=r.approval_support_seen_at is not None,
    )
    return PlatformRegistrationOut(
        id=str(r.id),
        platform_identifier=r.platform_identifier,
        platform_id=r.platform_id,
        platform_url=r.platform_url,
        platform_title=r.platform_title,
        platform_version=r.platform_version,
        **_approval,
        gates_tools=platform_gates_tools(**_approval),
        capability_nudge=_nudge is not None,
        capability_nudge_reason=_nudge,
        contract=r.contract,
        license_type=r.license_type,
        license_validated=r.license_validated,
        integration_id=str(r.integration_id) if r.integration_id else None,
        status=r.status,
        registered_at=r.registered_at.isoformat(),
        # ``last_heartbeat_at`` is NOT NULL at the DB level (it defaults to
        # the registration time), but guard defensively - mirroring
        # ``is_healthy()`` - so a malformed row can never 500 this endpoint
        # and break the live ``healthy`` indicator. Fall back to the
        # registration time as the next-best "last seen".
        last_heartbeat_at=(r.last_heartbeat_at or r.registered_at).isoformat(),
        healthy=r.is_healthy(),
    )


# ── Admin endpoints ──────────────────────────────────────────────────────────


@router.patch(
    "/registrations/{registration_id}/approval",
    response_model=PlatformRegistrationOut,
)
async def set_platform_approval_policy(
    registration_id: str,
    body: PlatformApprovalUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Decide whether a registered platform's tools ask for approval.

    Writing here marks the choice as an administrator's, which is what stops
    it being overwritten later: a heartbeat re-registration never touches the
    policy, and a platform that subsequently reports it can display prompts
    does not silently re-arm a control somebody deliberately switched off. The
    UI surfaces that case as a nudge instead.
    """
    from app.api.v1.helpers import parse_path_id
    from app.tools.approval import SET_BY_ADMIN

    reg_uuid = parse_path_id(registration_id, not_found_detail="Registration not found")

    reg = (
        await db.execute(
            select(PlatformRegistration).where(PlatformRegistration.id == reg_uuid)
        )
    ).scalar_one_or_none()
    if reg is None:
        raise HTTPException(status_code=404, detail="Registration not found")

    reg.approval_policy = body.approval_policy
    reg.approval_policy_set_by = SET_BY_ADMIN
    await db.commit()
    await db.refresh(reg)

    logger.info(
        "Platform %s tool approval set to %r by an administrator",
        reg.platform_identifier,
        body.approval_policy,
    )
    return _registration_out(reg)


@router.get("/registrations", response_model=list[PlatformRegistrationOut])
async def list_registrations(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all registered platforms. Available to all authenticated users in XTM One mode."""
    result = await db.execute(
        select(PlatformRegistration).order_by(PlatformRegistration.created_at)
    )
    return [_registration_out(r) for r in result.scalars().all()]


class PlatformMCPServerOut(BaseModel):
    provider: str
    name: str
    description: str
    url: str
    tool_count: int
    connected: bool
    healthy: bool
    platform_title: str | None = None
    platform_version: str | None = None


class XtmMCPServerOut(BaseModel):
    """XTM One's own MCP server (``/mcp/xtm``).

    Deliberately not a ``PlatformMCPServerOut``: it is not a *connected*
    platform, so it carries no registration, no heartbeat and no static tool
    count — its surface is resolved per caller from that user's own visibility.
    It also has its own admin toggle, separate from ``platform_mcp_enabled``.
    """

    enabled: bool
    name: str
    description: str
    url: str


class PlatformMCPServersOut(BaseModel):
    # Global admin toggle (PlatformSettings.platform_mcp_enabled).
    enabled: bool
    servers: list[PlatformMCPServerOut]
    # XTM One's own MCP server, with its own independent toggle.
    xtm_server: XtmMCPServerOut


@router.get("/mcp-servers", response_model=PlatformMCPServersOut)
async def list_platform_mcp_servers(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Describe the MCP servers XTM One hosts.

    Covers the platform bridges (OpenCTI / OpenAEV / OpenCRQ) and XTM One's
    own ``/mcp/xtm`` server, which is reported separately because it has no
    registration to be connected to and carries its own admin toggle.

    Single source of truth for every UI surface (Profile > MCP Endpoint,
    Settings > Integrations > MCP Endpoint): endpoint URL, live connection status, and the
    exposed tool count per server, plus the global enable toggle. Available
    to all authenticated users: the registration-derived fields (connection
    state, title, version) disclose nothing beyond what ``GET /registrations``
    already returns, and the rest (description, endpoint URL, tool count) is
    static catalog data compiled into the backend.
    """
    from app.api.v1.mcp_platform import (
        MCP_TOOL_ALLOW_LISTS,
        PLATFORM_MCP_DESCRIPTIONS,
        PLATFORM_MCP_PROVIDERS,
    )
    from app.api.v1.mcp_xtm import XTM_MCP_DESCRIPTION
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.services.branding import get_cached_branding

    ps = await _load_ps(db)
    enabled = bool(getattr(ps, "platform_mcp_enabled", True))

    result = await db.execute(
        select(PlatformRegistration).where(PlatformRegistration.status == "active")
    )
    regs = {r.platform_identifier: r for r in result.scalars().all()}
    # Legacy identifier: OpenAEV instances registered as "openbas".
    if "openbas" in regs and "openaev" not in regs:
        regs["openaev"] = regs["openbas"]

    base = settings.base_url.rstrip("/")
    servers = []
    for provider, display in PLATFORM_MCP_PROVIDERS.items():
        reg = regs.get(provider)
        connected = reg is not None and reg.integration_id is not None
        servers.append(
            PlatformMCPServerOut(
                provider=provider,
                name=display,
                description=PLATFORM_MCP_DESCRIPTIONS.get(provider, ""),
                url=f"{base}/mcp/{provider}",
                tool_count=len(MCP_TOOL_ALLOW_LISTS.get(provider, frozenset())),
                connected=connected,
                # Gated on ``connected`` so a registration without a bound
                # integration can never report healthy while shown as
                # "Not connected".
                healthy=bool(reg.is_healthy()) if connected and reg else False,
                platform_title=reg.platform_title if connected and reg else None,
                platform_version=reg.platform_version if connected and reg else None,
            )
        )
    return PlatformMCPServersOut(
        enabled=enabled,
        servers=servers,
        xtm_server=XtmMCPServerOut(
            enabled=bool(getattr(ps, "xtm_mcp_enabled", False)),
            name=get_cached_branding().name,
            description=XTM_MCP_DESCRIPTION,
            url=f"{base}/mcp/xtm",
        ),
    )


class XtmMCPPackageRequest(BaseModel):
    package_format: str = Field("claude-code", alias="format")
    # Which MCP server the package installs. One per package, deliberately:
    # a client loads every tool of every server it connects to, so bundling
    # all four would put ~213 tool definitions in its context on each request
    # and degrade tool selection. Installing two packages stays possible; it
    # is just the user's explicit choice rather than a default.
    target: str = "xtm"
    # One-click install: embed a credential so the client asks nothing. Opt-in,
    # because it turns the download into a live secret — and it MINTS a key
    # rather than reusing one, since stored keys are bcrypt hashes and the
    # plaintext exists only at creation.
    include_key: bool = False

    model_config = {"populate_by_name": True}


async def _mint_package_api_key(
    db: AsyncSession,
    user: User,
    *,
    package_format: str,
    target_label: str,
    request: Request,
) -> tuple[str, str]:
    """Create a personal API key dedicated to one downloaded package.

    Returns ``(plaintext_key, key_name)``.

    A new key rather than an existing one is not a preference: ``UserApiKey``
    stores a bcrypt hash and the plaintext is shown once at creation, so there
    is nothing to read back.  A dedicated key is also the better shape — it is
    revocable on its own, and its name says which download to blame.

    Scoped to the MCP endpoints, the only surface a package calls, and given
    the longest expiry the maximum lifetime allows when one is set.
    """
    from app.api.v1.api_keys import _generate_key, _hash_key
    from app.models.api_key import UserApiKey
    from app.services import api_key_access
    from app.services.audit import log_audit

    label = {
        "claude-code": "Claude Code plugin",
        "mcpb": "Claude Desktop extension",
    }.get(package_format, package_format)
    if target_label:
        label = f"{label} — {target_label}"
    name = f"{label} ({datetime.now(UTC).date().isoformat()})"

    now = datetime.now(UTC)
    expires_at = api_key_access.latest_allowed_expiry(now)
    caller_key = api_key_access.principal_of(user)
    if caller_key is not None and caller_key.expires_at is not None:
        # Minted with another key: it ends no later than that key does.
        expires_at = min(expires_at, caller_key.expires_at) if expires_at else caller_key.expires_at
    surfaces = [api_key_access.ApiKeySurface.MCP.value]
    api_key_access.require_within_caller_key(
        user,
        surfaces=surfaces,
        read_only=False,
        agent_ids=None,
        expires_at=expires_at,
        rate_limit_per_minute=None,
        quota_limit=None,
        quota_period=None,
        creates=True,
    )
    raw_key = _generate_key()
    api_key = UserApiKey(
        user_id=user.id,
        name=name,
        key_prefix=api_key_access.display_prefix(raw_key),
        key_hash=await _hash_key(raw_key),
        surfaces=surfaces,
        expires_at=expires_at,
        expiry_notified_days=api_key_access.due_expiry_notice(expires_at, now),
        created_at=now,
    )
    db.add(api_key)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="api_key",
        entity_id=api_key.id,
        entity_name=name,
        summary=f"Created API key '{name}' for an MCP package download",
        details={"surfaces": api_key.surfaces, "expires_at": expires_at.isoformat() if expires_at else None},
        request=request,
    )
    return raw_key, name


async def _resolve_package_context(db: AsyncSession, target_key: str):
    """Resolve a target and check the gates that govern the server it installs.

    Shared by the package and snippet routes: they hand out different
    artefacts for the same endpoint, and a gate honoured by one but not the
    other would be a way around it.
    """
    from app.api.v1.mcp_platform import (
        PLATFORM_MCP_PROVIDERS,
        _get_active_registration,
        _platform_mcp_enabled,
    )
    from app.api.v1.mcp_xtm import xtm_mcp_enabled
    from app.services.mcp_packages import UnknownPackageTargetError, resolve_target

    try:
        target = resolve_target(target_key)
    except UnknownPackageTargetError as err:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown package target `{target_key}`. Use one of: "
                "xtm, opencti, openaev, opencrq."
            ),
        ) from err

    if target.key == "xtm":
        if not await xtm_mcp_enabled(db):
            raise HTTPException(
                status_code=403,
                detail=(
                    "The XTM One MCP server is disabled on this deployment. "
                    "An administrator can enable it in Settings > Integrations > MCP Endpoint."
                ),
            )
        return target, None

    if not await _platform_mcp_enabled(db):
        raise HTTPException(
            status_code=403,
            detail=(
                "Platform MCP servers are disabled on this deployment. "
                "An administrator can enable them in Settings > Integrations > MCP Endpoint."
            ),
        )
    registration = await _get_active_registration(db, target.path)
    if registration is None:
        display = PLATFORM_MCP_PROVIDERS.get(target.path, target.path)
        raise HTTPException(
            status_code=404,
            detail=(
                f"No {display} platform is connected. Register the platform "
                "with XTM One to package its MCP server."
            ),
        )
    return target, registration


class XtmMCPSnippetRequest(BaseModel):
    client: str = "codex"
    target: str = "xtm"
    include_key: bool = False


class XtmMCPSnippetResponse(BaseModel):
    client: str
    display: str
    #: Where the fragment is pasted.
    path: str
    language: str
    content: str
    #: Name of the key minted for this snippet, when one was.
    api_key_created: str | None = None


@router.post("/mcp-servers/xtm/snippet", response_model=XtmMCPSnippetResponse)
async def build_xtm_mcp_snippet(
    body: XtmMCPSnippetRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Config fragment for a client that installs by file, not by package.

    Codex (which the Codex CLI, the Codex IDE extension and the ChatGPT
    desktop app all share) and Cursor are configured by editing a file that
    already holds the user's other servers. So this returns a **fragment to
    merge** rather than a downloadable file: shipping a whole ``config.toml``
    would invite someone to overwrite a working configuration with ours.

    Like the package route it can mint a dedicated key, which is why it is a
    POST and why it refuses while impersonating.
    """
    from app.services.mcp_packages import (
        SNIPPET_CLIENTS,
        UnknownSnippetClientError,
        build_snippet,
    )

    if body.client not in SNIPPET_CLIENTS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown client `{body.client}`. Use one of: "
                f"{', '.join(sorted(SNIPPET_CLIENTS))}."
            ),
        )

    target, _registration = await _resolve_package_context(db, body.target)

    api_key: str | None = None
    key_name = ""
    if body.include_key:
        if getattr(user, "impersonated_by", None) is not None:
            raise HTTPException(
                status_code=403,
                detail="Cannot create an API key while impersonating",
            )
        api_key, key_name = await _mint_package_api_key(
            db,
            user,
            package_format=SNIPPET_CLIENTS[body.client].display,
            target_label=target.display or "",
            request=request,
        )

    try:
        client, content = build_snippet(
            body.client,
            url=f"{settings.base_url.rstrip('/')}/mcp/{target.path}",
            target=target,
            api_key=api_key,
        )
    except UnknownSnippetClientError as err:  # pragma: no cover - guarded above
        raise HTTPException(status_code=422, detail=str(err)) from err

    # Commit before answering: a client configured with the key just minted
    # authenticates right away, and the get_db teardown only commits after the
    # response is sent.
    await db.commit()
    return XtmMCPSnippetResponse(
        client=client.key,
        display=client.display,
        path=client.path,
        language=client.language,
        content=content,
        api_key_created=key_name or None,
    )


@router.post("/mcp-servers/xtm/package")
async def download_xtm_mcp_package(
    body: XtmMCPPackageRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Download an installer package for the ``/mcp/xtm`` server.

    Three shapes of the same server — a Claude Code plugin, a Claude Desktop
    ``.mcpb`` extension, and a vendor-neutral Agent Plugins package — each
    carrying the skill that documents the tool surface.

    The tool reference inside the skill is generated from the definitions
    **this** caller would be offered, so the package describes the surface
    they will actually get rather than a maintained-by-hand list. That is
    also why the endpoint is per-user rather than a static asset.

    With ``include_key`` the package embeds a freshly minted API key so the
    install asks nothing. It is a POST for that reason: the download is not
    idempotent when it creates a credential, and a GET that mints keys would
    hand one out to every prefetch and every double-click. The new key's name
    is echoed in ``X-Api-Key-Created`` so the UI can tell the user what was
    created without opening the archive.
    """
    from app.api.v1.mcp_platform import _build_platform_tools
    from app.api.v1.mcp_xtm import _build_xtm_tools
    from app.core.utils import build_content_disposition
    from app.services.branding import get_cached_branding
    from app.services.mcp_packages import (
        PACKAGE_FORMATS,
        UnknownPackageFormatError,
        build_package,
        embeds_api_key,
    )

    # Validated FIRST, before anything is minted. ``embeds_api_key`` answers
    # False for any unrecognised string, so checking it first made
    # ``{"format": "bogus", "include_key": true}`` answer with the Agent
    # Plugins message; and a format added to the builders but not to
    # ``embeds_api_key`` would mint a key and write an audit row before
    # raising, leaving the session rollback to undo both.
    if body.package_format not in PACKAGE_FORMATS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unsupported package format `{body.package_format}`. "
                f"Use one of: {', '.join(PACKAGE_FORMATS)}."
            ),
        )

    target, registration = await _resolve_package_context(db, body.target)

    api_key: str | None = None
    key_name = ""
    if body.include_key:
        # Reached only for a known format (validated above), so this can ask
        # the capability question directly rather than naming one format: a
        # future format that cannot carry a credential gets the right answer
        # without editing this branch.
        if not embeds_api_key(body.package_format):
            raise HTTPException(
                status_code=422,
                detail=(
                    "The Agent Plugins package cannot embed a credential: the "
                    "specification requires mcp.json headers to be literal, "
                    "non-secret package data. Download it without a key and "
                    "configure authentication in your client."
                ),
            )
        # Same guard as ``POST /api-keys``: an impersonated session must not be
        # able to mint a live credential for the identity it is borrowing.
        if getattr(user, "impersonated_by", None) is not None:
            raise HTTPException(
                status_code=403,
                detail="Cannot create an API key while impersonating",
            )
        api_key, key_name = await _mint_package_api_key(
            db,
            user,
            package_format=body.package_format,
            # Names the server too: a user with one key per client AND per
            # platform cannot tell which to revoke from "Claude Code plugin".
            target_label=target.display or "",
            request=request,
        )

    # The reference inside the skill is generated from the surface this caller
    # would actually be offered, so it is resolved the same way the endpoint
    # itself resolves it rather than from a static allow-list.
    if registration is None:
        definitions, _executor = await _build_xtm_tools(db, user)
    else:
        definitions, _executor = await _build_platform_tools(
            db, target.path, registration, user
        )

    try:
        filename, payload = build_package(
            body.package_format,
            url=f"{settings.base_url.rstrip('/')}/mcp/{target.path}",
            platform_name=get_cached_branding().name,
            app_version=settings.app_version,
            tools=definitions,
            api_key=api_key,
            key_name=key_name,
            target=target,
        )
    except UnknownPackageFormatError as err:  # pragma: no cover - guarded above
        # Unreachable now that the format is checked before minting. Kept so a
        # builder registered without updating ``PACKAGE_FORMATS`` fails loudly
        # instead of serving a half-built package.
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unsupported package format `{body.package_format}`. "
                f"Use one of: {', '.join(PACKAGE_FORMATS)}."
            ),
        ) from err

    headers = {"Content-Disposition": build_content_disposition(filename)}
    if key_name:
        headers["X-Api-Key-Created"] = key_name
        headers["Access-Control-Expose-Headers"] = "X-Api-Key-Created"

    # Commit before answering: the package installs with the key just minted,
    # and the get_db teardown only commits after the response is sent.
    await db.commit()
    return StarletteResponse(
        content=payload,
        media_type="application/zip",
        headers=headers,
    )


class PurgeSummary(BaseModel):
    platform_identifier: str
    integrations_deleted: int
    agents_cleaned: int


@router.delete(
    "/registrations/{registration_id}/purge",
    response_model=PurgeSummary,
)
async def purge_registration(
    registration_id: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Purge a platform registration and ALL integrations for ALL users of that provider.

    Admin-only. Deletes the PlatformRegistration, every Integration row for the
    provider across all users, and cleans agent integration_bindings that referenced
    the deleted integrations.
    """
    from app.api.v1.helpers import parse_path_id
    from app.models.agent import Agent
    from app.services.audit import log_audit

    reg_uuid = parse_path_id(registration_id, not_found_detail="Platform registration not found")
    reg_result = await db.execute(
        select(PlatformRegistration).where(PlatformRegistration.id == reg_uuid)
    )
    reg = reg_result.scalar_one_or_none()
    if not reg:
        raise HTTPException(status_code=404, detail="Platform registration not found")

    provider_key = reg.platform_identifier
    if provider_key == "openbas":
        provider_key = "openaev"

    integrations_result = await db.execute(
        select(Integration).where(Integration.provider == provider_key)
    )
    integrations_to_delete = integrations_result.scalars().all()
    integration_ids_to_remove = {str(i.id) for i in integrations_to_delete}

    agents_cleaned = 0
    if integration_ids_to_remove:
        agents_result = await db.execute(select(Agent))
        for agent in agents_result.scalars().all():
            bindings = agent.integration_bindings or {}
            if any(
                iid in integration_ids_to_remove
                for ids in bindings.values()
                for iid in ids
            ):
                cleaned = {
                    k: [iid for iid in ids if iid not in integration_ids_to_remove]
                    for k, ids in bindings.items()
                }
                agent.integration_bindings = {k: v for k, v in cleaned.items() if v}
                agents_cleaned += 1

    for integration in integrations_to_delete:
        await db.delete(integration)

    # Its sign-in client goes with it.
    await platform_sign_in_clients.remove_platform_sign_in_client(db, reg.platform_identifier)
    await db.delete(reg)

    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="platform_registration",
        entity_id=reg.id,
        entity_name=reg.platform_title,
        summary=(
            f"Purged platform '{reg.platform_title}' ({reg.platform_identifier}): "
            f"deleted {len(integrations_to_delete)} integration(s), "
            f"cleaned {agents_cleaned} agent(s)"
        ),
        request=request,
        details={
            "platform_identifier": reg.platform_identifier,
            "platform_url": reg.platform_url,
            "integrations_deleted": len(integrations_to_delete),
            "agents_cleaned": agents_cleaned,
        },
    )

    logger.info(
        "Admin %s purged platform '%s': %d integration(s) deleted, %d agent(s) cleaned",
        user.email,
        reg.platform_identifier,
        len(integrations_to_delete),
        agents_cleaned,
    )

    # Purging an EE platform can drop the deployment to Community Edition:
    # commit, then drop the background gate's cached licence answer.
    await db.commit()
    from app.services import agentic_gate

    agentic_gate.invalidate_cache()
    # A purged platform stops framing the platform's pages now, not at the
    # next background refresh.
    from app.services.registered_platform_origins import refresh_registered_platform_origins

    await refresh_registered_platform_origins()

    return PurgeSummary(
        platform_identifier=reg.platform_identifier,
        integrations_deleted=len(integrations_to_delete),
        agents_cleaned=agents_cleaned,
    )


# ── Platform chat (JWT-authenticated) ────────────────────────────────────


class InlineFile(BaseModel):
    """Base64-encoded file sent inline by a platform backend (e.g. OpenAEV).

    The ``filename`` field is propagated into :class:`FileRef.filename`
    and reaches downstream consumers:

    * ``extract_text_from_file(data, ref.filename)`` keys file-type
      sniffing off the extension — a path-shaped value here would
      steer the wrong extractor.
    * Structured logging emits the filename verbatim — a multi-MB
      string would balloon log volume.
    * The sandbox surfaces it as ``entry["filename"]`` so a
      ``"../etc/passwd"`` value would leak the traversal token into
      the sandbox's view of the workspace.

    To contain that surface we (a) bound the length at the Pydantic
    layer and (b) strip directory components via ``os.path.basename``
    and reject ``""``/``"."``/``".."`` and NUL-containing values.  The
    caller still owns the *meaning* of the filename (no MIME inference
    here), but it can no longer be path-shaped, empty, or oversized.
    """
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=255)
    data: str  # base64-encoded bytes

    @field_validator("filename")
    @classmethod
    def _sanitize_filename(cls, value: str) -> str:
        import os.path

        if "\x00" in value:
            raise ValueError("filename must not contain NUL bytes")
        base = os.path.basename(value.replace("\\", "/"))
        if not base or base in (".", ".."):
            raise ValueError(
                "filename must be a non-empty basename, not a path component"
            )
        return base


# Bounds on inline file uploads — guards against DoS from oversized
# multi-file requests and keeps memory usage predictable in the base64
# decode loop below. Values match the platform's per-file storage cap.
_MAX_INLINE_FILES_PER_REQUEST = 20
_MAX_INLINE_FILE_BYTES = 25 * 1024 * 1024  # 25 MiB per file (decoded)
_MAX_INLINE_FILES_TOTAL_BYTES = 100 * 1024 * 1024  # 100 MiB total per request


def _max_b64_length_for(decoded_bytes: int) -> int:
    """Return the maximum base64 string length that can decode to ``decoded_bytes``.

    Base64 encoding produces 4 output chars for every 3 input bytes,
    rounded up to the next multiple of 4 with ``=`` padding.  We add
    a tiny slack (``+ 4``) to absorb any padding/whitespace the
    decoder will silently strip — better to err on the side of
    accepting a barely-over-budget string than to spuriously reject
    a valid encoded payload that lands exactly on the boundary.

    This is the pre-decode budget: if an inbound base64 string is
    longer than the cap returned here, it CANNOT decode to <=N bytes
    no matter what ``base64.b64decode`` does, so we can reject before
    spending the memory on decode.
    """
    return ((decoded_bytes + 2) // 3) * 4 + 4


_MAX_INLINE_FILE_B64_LENGTH = _max_b64_length_for(_MAX_INLINE_FILE_BYTES)
_MAX_INLINE_FILES_TOTAL_B64_LENGTH = _max_b64_length_for(_MAX_INLINE_FILES_TOTAL_BYTES)


class PlatformChatRequest(BaseModel):
    """Platform chat request payload.

    Inline file attachments (``files``)
    -----------------------------------
    Inline files are accepted but are **ephemeral** — they are decoded,
    handed to the agent run for this single message, and then discarded.
    They are NOT persisted as ``FileAttachment`` rows linked to the
    stored user ``Message``, so subsequent turns in the same
    conversation will not see them.

    **External integrators (e.g. OpenAEV AskAI) MUST re-send any inline
    attachments on every turn that needs them.**  If you need
    persistent attachments that survive history rehydration (e.g. an
    LLM tool call in turn 3 referring back to a file uploaded in turn 1),
    upload the file separately and pass its file id via the usual
    attachment surface.

    The hard caps below are enforced before base64 decoding to prevent
    OOM from oversized payloads:

    * Per-file cap: ``_MAX_INLINE_FILE_BYTES`` (≈ 25 MiB).
    * Total per-request cap: ``_MAX_INLINE_FILES_TOTAL_BYTES`` (≈ 100 MiB).
    """

    content: str
    conversation_id: str | None = None
    agent_slug: str | None = None
    stream: bool = True
    files: list[InlineFile] | None = None
    # Arbitrary host page/application context forwarded by the embedded chat
    # (OpenCTI / OpenAEV "Ask Ariane"). Optional and flexible — today it
    # carries the user's current relative URL
    # (e.g. ``{"url": "/dashboard/analyses/reports/<id>/overview"}``) so the
    # agent is aware of where the question is asked from, and can be extended
    # later (page title, selected entity, user role, etc.). Injected into the
    # agent's system prompt as a compact JSON block; omitted when empty.
    context: dict | None = None
    # @conversations: conversations the message references, picked
    # from the panel's @ menu (``GET /platform/chat/conversation-references``)
    # - the web chat's rule: only those the user can open, read by the agent
    # as a brief and through ``read_conversation``.
    referenced_conversation_ids: list[str] | None = None
    # See the class docstring: absence means an older client, and the
    # server must degrade rather than wait on an answer that will never
    # arrive.
    supports_tool_approval: bool = False
    # Will this client perform ``act_on_page`` actions on its host page and
    # answer them? A promise like ``supports_tool_approval``: absent,
    # the tool is never offered and no action frame is sent.
    supports_page_actions: bool = False
    # Does this client render the deck, report and video markers itself
    # ? With it the markers stay in the text and ``done`` carries an
    # ``artifacts`` descriptor per marker; absent, the markers become links
    # to the XTM One viewer and none is streamed
    # (``services/platform_chat_artifacts``).
    supports_artifacts: bool = False
    # Will this client offer the user's likely next message
    # (``reply_suggestion``, ``services/chat_reply_suggestions.py``)? Absent,
    # none is generated: a one-shot generation call ("Generate with AI") reads
    # to the end of the stream and must neither wait on one nor pay for it.
    supports_reply_suggestions: bool = False


class PlatformChatSessionRequest(BaseModel):
    conversation_id: str | None = None
    agent_slug: str | None = None
    #: File a NEW conversation into this workspace - the panel's "new
    #: conversation" inside a workspace group. Ignored when an existing
    #: conversation is restored: moving one is ``PATCH /chat/sessions/{id}``.
    workspace_id: str | None = None
    #: The same promise as ``PlatformChatRequest.supports_artifacts``, for
    #: the restored answers.
    supports_artifacts: bool = False


def attribute_message_agents(
    messages: list[dict], starting_agent: str | None, recorded: Mapping[str, str | None] | None = None
) -> None:
    """Stamp each assistant message with the agent that actually wrote it.

    A transfer does not start a new conversation — it rewrites
    ``conv.agent_id`` in place — so one thread can hold answers from several
    agents, and labelling the lot with the current one is wrong for everything
    before the handover.

    No message records its author, but the two facts needed to derive it are
    already persisted: ``Conversation.original_agent_id`` is write-once and
    survives every handover, and each turn that transferred stored its
    ``transfer_chain``, whose entries are the *target* agents in order. Walking
    forward and moving the pointer on each handover therefore reconstructs
    exact attribution, retroactively, with nothing new written.

    Mutates *messages* in place. ``starting_agent`` of ``None`` means the
    beginning is unknown (an older conversation, or a truncated restore): only
    messages after the first in-window handover are then named, and the rest
    are left for the panel to fall back on rather than being guessed at.

    *recorded* (:func:`recorded_message_agents`, by message id) is what a
    message says itself, and wins: the author an answer was stamped with, and
    the agent a change-of-agent note hands the conversation to. Both
    move the pointer; ``None`` there is the General Assistant or a name gone,
    left for the panel to fall back on.
    """
    current = starting_agent
    recorded = recorded or {}
    for entry in messages:
        # The handover happens *during* the turn, so the agent it lands on is
        # the one that finished this message — not merely the next one.
        chain = entry.get("transfer_chain")
        if isinstance(chain, list) and chain:
            last = chain[-1]
            if isinstance(last, dict) and last.get("agent_name"):
                current = str(last["agent_name"])
        if entry.get("id") in recorded:
            current = recorded[entry["id"]]
        # Assistant rows only: a user message has no author to name, and
        # stamping one would put an agent's name on the user's own words.
        if entry.get("role") != "assistant":
            continue
        # A mentioned agent's reply records its author; it answered this one
        # message, so the conversation's agent (the pointer) stays.
        answered_by = entry.get("answered_by")
        if isinstance(answered_by, dict) and answered_by.get("agent_name"):
            entry["agent_name"] = answered_by["agent_name"]
        elif current:
            entry["agent_name"] = current


async def recorded_message_agents(db: AsyncSession, rows: Sequence[Message]) -> dict[str, str | None]:
    """What the assistant messages of *rows* record of their agent, by message id, for
    :func:`attribute_message_agents`: a change-of-agent note the agent it hands the
    conversation to, an answer the author it was stamped with. A mentioned agent's reply
    (``answered_by``) answered that one message and is left to the walk, as is a message
    stored before authors were recorded.
    """
    from app.core.utils import safe_uuid
    from app.services.conversation_continuity import AGENT_SWITCH_KEY, agent_labels, message_author
    from app.services.mention_dispatch import ANSWERED_BY_KEY

    found: dict[str, uuid.UUID | None] = {}
    for m in rows:
        if m.role != "assistant" or not m.metadata_json:
            continue
        try:
            meta = json.loads(m.metadata_json)
        except ValueError:
            continue
        if not isinstance(meta, dict) or meta.get(ANSWERED_BY_KEY):
            continue
        switch = meta.get(AGENT_SWITCH_KEY)
        if isinstance(switch, dict):
            to = switch.get("to") if isinstance(switch.get("to"), dict) else {}
            found[str(m.id)] = safe_uuid(str(to["agent_id"])) if to.get("agent_id") else None
            continue
        known, agent_id = message_author(meta)
        if known:
            found[str(m.id)] = agent_id
    labels = await agent_labels(db, found.values())
    return {mid: labels[aid].name if aid in labels else None for mid, aid in found.items()}


def _record_transfer_start(event: dict, chain: list[dict[str, str]]) -> None:
    """Append a ``transfer_start`` hop to *chain*.

    The ONE reading of a handover event for both platform chat paths (streaming
    and non-streaming, #4086), so the ``transfer_chain`` they persist - which
    :func:`attribute_message_agents` walks on restore - has one shape.
    """
    if event.get("status") != "transfer_start" or not event.get("agent_name"):
        return
    chain.append({"agent_id": event.get("agent_id", ""), "agent_name": event["agent_name"]})


def _completed_transfer_chain(
    chain: list[dict[str, str]], transfer_agent_id: str | None
) -> list[dict[str, str]]:
    """The hops of *chain* that took the conversation, ending at *transfer_agent_id*.

    A hop is recorded when its handover STARTS. A target whose run then fails
    hands the turn back: ``run_agent_chat`` returns the agent before it (``None``
    for a failed first hop), and the conversation stays there. A failed hop must
    therefore not reach the persisted ``transfer_chain``, or
    :func:`attribute_message_agents` names it the author of this answer and of
    every later one. What both platform chat paths persist and answer.
    """
    if not transfer_agent_id:
        return []
    for index in range(len(chain) - 1, -1, -1):
        if str(chain[index].get("agent_id")) == str(transfer_agent_id):
            return chain[: index + 1]
    return list(chain)


#: The ``error_type`` of a platform chat turn that failed, shared by the
#: non-streaming error body and the streaming ``status: error`` frame.
PLATFORM_CHAT_RATE_LIMITED = "rate_limited"
PLATFORM_CHAT_PROVIDER_UNAVAILABLE = "provider_unavailable"
PLATFORM_CHAT_PROVIDER_REJECTED = "provider_rejected"
PLATFORM_CHAT_INTERNAL_ERROR = "internal_error"
PLATFORM_CHAT_DATABASE_TIMEOUT = "database_timeout"
#: A platform stop (a deployment, a scale-in, a node drain, a worker recycle)
#: cut the turn before it answered: the same request succeeds on a live worker.
PLATFORM_CHAT_PLATFORM_RESTARTING = "platform_restarting"
#: The ``Retry-After`` of a turn a platform stop cut: a replacement worker
#: usually serves within seconds of the drain.
PLATFORM_CHAT_RESTART_RETRY_AFTER_SECONDS = 5

_PROVIDER_WEATHER_CAUSES = frozenset(
    {
        "all_providers_failed",
        "provider_overloaded",
        "provider_timeout",
        "provider_connection_failed",
    }
)


@dataclass(frozen=True, slots=True)
class PlatformTurnFailure:
    """How a failed platform chat turn is answered to the calling platform.

    A caller such as a playbook step retries on ``retryable`` and reads
    ``error_type``; an answer text would be taken for the agent's output.
    """

    status_code: int
    error_type: str
    retryable: bool
    detail: str
    last_status: int | None = None
    retry_after: int | None = None
    #: Why each target of an exhausted failover chain failed (model, kind,
    #: decline category, HTTP status, short error), redacted and bounded.
    legs: tuple[dict[str, Any], ...] = ()

    def body(self, conversation_id: object) -> dict[str, Any]:
        return {
            "error_type": self.error_type,
            "retryable": self.retryable,
            "detail": self.detail,
            "last_status": self.last_status,
            "legs": [dict(leg) for leg in self.legs],
            "conversation_id": str(conversation_id),
        }

    def frame(self) -> dict[str, Any]:
        return {
            "type": "status",
            "status": "error",
            "error_type": self.error_type,
            "retryable": self.retryable,
            "last_status": self.last_status,
            "legs": [dict(leg) for leg in self.legs],
        }


def _retry_after_seconds(exc: BaseException) -> int | None:
    """The provider's ``Retry-After`` in whole seconds, when it sent one."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:
        return None
    try:
        seconds = int(float(str(raw)))
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def classify_platform_turn_failure(exc: BaseException) -> PlatformTurnFailure:
    """Map an exception of ``run_agent_chat`` to the answer a platform caller gets.

    The cause is read on the leg that ended the failover chain: a rate limit
    is 429, provider weather (outage, overload, timeout, connection, an
    exhausted chain) is 503, a database statement timeout is 503
    ``database_timeout``, a turn a platform stop cut (uvicorn's graceful
    drain, or ``worker_shutdown.TurnWoundDown``) is 503
    ``platform_restarting``, a provider that refused the request or the
    credential is 502, and anything else is 500. Only 429 and 503 are
    retryable.
    """
    from app.llm.router import is_provider_unavailable_error
    from app.services.failure_causes import (
        FailureCause,
        classify_run_failure,
        last_provider_failure,
    )

    if is_graceful_shutdown_cancellation(exc):
        from app.core.worker_shutdown import drain_cut_cause
        from app.services.interrupted_turn import cut_cause_text

        return PlatformTurnFailure(
            503,
            PLATFORM_CHAT_PLATFORM_RESTARTING,
            True,
            f"This turn was cut before it answered: {cut_cause_text(drain_cut_cause(), turn='it')}. "
            "Send the same request again.",
            retry_after=PLATFORM_CHAT_RESTART_RETRY_AFTER_SECONDS,
        )
    last, last_status = last_provider_failure(exc)
    detail = format_exception_message(exc)[:500]
    cause = classify_run_failure(last).cause
    legs = tuple(leg.as_dict() for leg in getattr(exc, "legs", ()) or ())
    if _is_statement_timeout(exc):
        return PlatformTurnFailure(503, PLATFORM_CHAT_DATABASE_TIMEOUT, True, detail, last_status, legs=legs)
    if cause is FailureCause.PROVIDER_RATE_LIMITED or last_status == 429:
        return PlatformTurnFailure(
            429, PLATFORM_CHAT_RATE_LIMITED, True, detail, last_status, _retry_after_seconds(last), legs
        )
    if (
        cause.value in _PROVIDER_WEATHER_CAUSES
        or is_provider_unavailable_error(exc)
        or (cause is FailureCause.UNKNOWN and last is not exc)
    ):
        return PlatformTurnFailure(
            503, PLATFORM_CHAT_PROVIDER_UNAVAILABLE, True, detail, last_status, _retry_after_seconds(last), legs
        )
    if last is not exc and cause in (
        FailureCause.PROVIDER_AUTH_REJECTED,
        FailureCause.MODEL_REJECTED_REQUEST,
        FailureCause.MODEL_DECLINED,
    ):
        return PlatformTurnFailure(502, PLATFORM_CHAT_PROVIDER_REJECTED, False, detail, last_status, legs=legs)
    return PlatformTurnFailure(500, PLATFORM_CHAT_INTERNAL_ERROR, False, detail, last_status, legs=legs)


def _aware_isoformat(value: datetime) -> str:
    """*value* as ISO 8601 with its timezone; a naive value is read as UTC."""
    return (value if value.tzinfo is not None else value.replace(tzinfo=UTC)).isoformat()


class PlatformChatSessionResponse(BaseModel):
    conversation_id: str
    # A restored entry: ``id``, ``role``, ``content``, ``created_at`` and, on
    # an answer the caller rated, ``feedback`` ({"rating", "comment"});
    # on a user message that referenced conversations with @, its
    # ``conversation_refs`` ({"conversation_id", "title", "key"}, #5065); on
    # a mentioned agent's reply, ``answered_by`` (``agent_name`` names the
    # entry) and ``mention_failed``.
    messages: list[dict]
    # The agent the conversation belongs to, so a restored thread is labelled
    # with who actually answered it rather than with whichever agent happens to
    # be selected. Null for conversations that predate per-conversation routing.
    agent_id: str | None = None
    agent_name: str | None = None
    #: The workspace the conversation is filed in, or ``None``.
    workspace_id: str | None = None
    #: The channel it was started in (``slack``, ``teams``, ...), as its list
    #: row says: the panel names it in the header before the list is read.
    channel: str | None = None


class PlatformAgentOut(BaseModel):
    id: str
    name: str
    slug: str | None
    icon: str | None
    description: str | None
    tags: list[str] = []


class PlatformChatQuotaOut(BaseModel):
    """The agentic quota the embedded chat shows, as the web composer shows it."""

    # Agentic actions consumed this period, the sandbox slice included.
    used: int
    # The resolved agentic limit; null = unlimited.
    limit: int | None
    # The period of the limit ("monthly", "daily", ...).
    period: str
    # "global": ``used`` is the shared platform-wide pool, not the caller's slice.
    scope: Literal["user", "global"]
    # Today, this month and this year: the rows of the quota panel, the same
    # rows ``GET /settings/my-quotas`` serves. The fields above stay for older
    # chatbot versions.
    periods: list[QuotaPeriodUsage] = []


class PlatformChatPromptOut(BaseModel):
    id: str
    title: str
    content: str
    description: str | None


class PlatformChatPromptsOut(BaseModel):
    prompts: list[PlatformChatPromptOut]


PLATFORM_CHAT_CHANNEL = "api"

#: ``CutTurn.entry`` of a platform API stream turn a platform stop cut.
PLATFORM_CUT_ENTRY = "platform_api"


async def queue_cut_platform_turn(
    exc: BaseException, conversation_id: object, *, user_id: str, user_message_id: object
) -> bool:
    """Queue a platform API turn a platform stop cut for its automatic resume; whether it was.

    Only the graceful drain's cancellation counts (a client that leaves is
    not a cut); the cause and the cap are ``cut_turn_resume``'s. The next
    process's sweep answers the same stored message in the conversation,
    where the platform's session restore reads it. Never raises.
    """
    from app.core.utils import is_graceful_shutdown_cancellation
    from app.core.worker_shutdown import drain_cut_cause
    from app.services.cut_turn_resume import enqueue_cut_turn

    if user_message_id is None or not is_graceful_shutdown_cancellation(exc):
        return False
    try:
        return await enqueue_cut_turn(
            conversation_id,
            user_id=user_id,
            user_message_id=user_message_id,
            turn_id=None,
            cause=drain_cut_cause(),
            entry=PLATFORM_CUT_ENTRY,
        )
    except Exception:
        logger.debug("Could not queue the cut platform turn of %s", conversation_id, exc_info=True)
        return False


async def forget_cut_platform_turn(conversation_id: object, *, user_message_id: object) -> None:
    """Drop a queued resume: the cut turn answered before its process ended.

    Only the record of *user_message_id*: a concurrent stream of the same
    conversation cut meanwhile keeps its own.
    """
    from app.services.cut_turn_resume import forget_cut_turn

    with contextlib.suppress(Exception):
        await forget_cut_turn(conversation_id, user_message_id=user_message_id)


async def settle_platform_checkpoint(
    checkpoint: Any,
    conversation_id: object,
    *,
    cut: bool,
    cut_queued: bool,
    answered: bool,
    user_message_id: object,
) -> bool:
    """Close a platform API turn's checkpoint; whether the stored answer deletes its row.

    A turn a platform stop cut (*cut*) and still unanswered flushes its
    completed tool rounds in a shutdown finalizer, whether or not its
    automatic resume was queued (*cut_queued*): the resume, or the one-click
    Resume when the queue refused it, replays them instead of running them
    again. A turn that answered (cut or not) closes without flushing and
    drops a resume queued for it.
    """
    from app.core.shutdown_finalizers import start_shutdown_finalizer

    if cut and not answered:
        start_shutdown_finalizer(
            checkpoint.aclose(flush=True), name=f"turn-checkpoint:{conversation_id}"
        )
        return False
    if cut_queued:
        await forget_cut_platform_turn(conversation_id, user_message_id=user_message_id)
    await checkpoint.aclose(flush=False)
    return bool(checkpoint.may_hold_row)


def platform_cut_unanswered(*, cut: bool, answer: str | None) -> bool:
    """Whether a platform API segment ends with no reply at all: a platform stop cut it unanswered.

    Its stream stores and sends nothing (not today's error fallback, which
    would read as an answer before the real one): the queued resume, or the
    person, answers the same message.
    """
    return cut and not (answer and answer.strip())


# Max messages returned on session restore. Mirrors the default ``limit`` of
# ``channels.router.get_conversation_history`` (the latest-N semantics used by
# the message-send path) so reopening a long conversation stays bounded.
_PLATFORM_CHAT_RESTORE_LIMIT = 50

# The roles the panel renders, and so the only rows that take a place in the
# restore window: tool and system rows are internal.
_PLATFORM_CHAT_RESTORED_ROLES = ("user", "assistant")

# Max conversations returned by the history listing (chatbot history menu).
_PLATFORM_CHAT_LIST_LIMIT = 50

# ── Mid-run steering bridge (REST / SSE) ─────────────────────────────────
# The embedded chatbot (OpenCTI / OpenAEV proxies) steers via a SEPARATE
# HTTP POST that may land on a different uvicorn worker than the one running
# the SSE stream — the bridge therefore goes through Redis (the native web
# chat's steer route has its own: in process when the turn runs there, else
# the turn guard's steering inbox):
#
#   * the SSE run maintains ``platform:chat:steer:active:{conv}`` (short
#     TTL, refreshed every poll tick) so the steer endpoint can tell
#     whether a run is live (409 otherwise → the chatbot rolls the
#     optimistic bubble back and restores the composer text);
#   * steer POSTs RPUSH onto ``platform:chat:steer:queue:{conv}``, in one
#     script with the marker check (``_STEER_ENQUEUE_LUA``);
#   * the SSE run polls the list and feeds the texts into the local
#     ``asyncio.Queue`` consumed by ``run_agent_loop`` at iteration
#     boundaries, and closes the bridge by deleting the marker and taking
#     the list in one script (``_STEER_CLOSE_LUA``): a steer is either in
#     what the turn takes or refused with a 409, never queued after it.
_STEER_LIST_TTL_S = 3600
_STEER_ACTIVE_TTL_S = 30
_STEER_POLL_INTERVAL_S = 1.0
# Bound on steering fold-in follow-up segments per request — guards against
# a client that keeps steering forever holding the SSE stream open.
_MAX_STEER_SEGMENTS = 3

# KEYS: active marker, steering list. ARGV: text, list TTL. 1 when queued.
_STEER_ENQUEUE_LUA = """
if redis.call('exists', KEYS[1]) == 0 then
    return 0
end
redis.call('rpush', KEYS[2], ARGV[1])
redis.call('expire', KEYS[2], ARGV[2])
return 1
"""

# KEYS: active marker, steering list. Returns the texts still queued.
_STEER_CLOSE_LUA = """
redis.call('del', KEYS[1])
local items = redis.call('lrange', KEYS[2], 0, -1)
redis.call('del', KEYS[2])
return items
"""


def _steer_list_key(conversation_id: object) -> str:
    return f"platform:chat:steer:queue:{conversation_id}"


def _steer_active_key(conversation_id: object) -> str:
    return f"platform:chat:steer:active:{conversation_id}"


def _should_forward_platform_status_event(event: dict) -> bool:
    """Decide whether a status event flows to platform chat SSE consumers.

    ``thinking_text`` reasoning prose is forwarded since the embedded
    chatbot (OpenCTI / OpenAEV) renders it in the same dedicated reasoning
    pane as the native web chat — small, dimmed, capped-height — instead
    of leaking it into the answer bubble.  Every event forwards except the
    agentic-action charge: it feeds the web chat's own quota
    counter, and the embedded chatbot has none.
    """
    return event.get("status") != AGENTIC_ACTION_STATUS


def _platform_attachments_from_metadata(metadata_json: str | None) -> list[dict] | None:
    """Extract the agent-generated attachment blocks from a message's metadata.

    Returns the stored attachment dicts as-is (``filename`` / ``type`` /
    ``size`` / ``file_id`` / ``content_type`` / ``file_tag``).

    No ``download_url`` is added on purpose.  The embedded chat in OpenCTI
    and OpenAEV reaches XTM One through a per-platform backend proxy that
    mints the XTM One JWT server-side — the end user is authenticated only
    against OpenCTI / OpenAEV and never logs in to XTM One directly.  The
    ``@filigran/chatbot`` client therefore builds the download URL relative
    to its own ``apiBaseUrl`` (the proxy) from ``file_id``; emitting an
    absolute ``/api/v1/chat/...`` URL here would tempt a client into hitting
    XTM One directly (which it cannot authenticate to).
    """
    if not metadata_json:
        return None
    try:
        meta = json.loads(metadata_json)
    except Exception:
        return None
    if not isinstance(meta, dict):
        return None
    raw = meta.get("attachments")
    if not isinstance(raw, list) or not raw:
        return None
    return [a for a in raw if isinstance(a, dict)] or None


# Metadata keys re-surfaced on a restored assistant message. Same key names as
# the live ``done`` SSE payload, so the chatbot renders a restored turn exactly
# like one it watched arrive.
RESTORED_ASSISTANT_META_KEYS: tuple[str, ...] = (
    "tool_names",
    "tool_call_count",
    "iterations",
    "reasoning",
    "transfer_chain",
    "tool_call_trace",
    "is_truncated",
    *CONTEXT_USAGE_META_KEYS,
)


async def _collect_generated_attachments(
    db: AsyncSession,
    file_context: object,
    initial_file_ids: set[str],
    *,
    conversation_id: object,
    reply_text: str | None = None,
) -> tuple[list, list[dict]]:
    """Resolve files generated during an agent run into attachment metadata.

    Compares the post-run ``FileContext`` against the file ids that were
    present before the run (``initial_file_ids``) to isolate the
    newly-generated files, loads their ``FileAttachment`` rows, and maps each
    to the same display shape the native chat surfaces use
    (``attachment_meta_from_file_attachment``). A file the run only fetched
    rides the reply when *reply_text* names it.

    Returns ``(attachment_rows, attachment_meta)``.  ``attachment_meta`` is
    intentionally download-url-free — see
    :func:`_platform_attachments_from_metadata`.
    """
    from app.tools.base import files_for_reply

    list_files = getattr(file_context, "list_files", None)
    if not callable(list_files):
        return [], []
    generated_ids = set(
        files_for_reply(
            file_context,
            [
                ref.file_id
                for ref in list_files()
                if getattr(ref, "file_id", None) and ref.file_id not in initial_file_ids
            ],
            reply_text,
        )
    )
    if not generated_ids:
        return [], []

    # Keep only UUID-parseable ids. Some tools may add a FileRef with a
    # non-UUID file_id (e.g. an integration attaching an in-memory file with
    # an external id). Converting the whole set in one comprehension would
    # then raise and the except below would drop EVERY generated attachment —
    # including the valid ones. Parse defensively and skip the unparseable.
    from app.core.utils import safe_uuid

    generated_uuids: list = []
    for fid in generated_ids:
        parsed = safe_uuid(fid)
        if parsed is not None:
            generated_uuids.append(parsed)
    if not generated_uuids:
        return [], []

    from app.services.chat_messages import attachment_meta_from_file_attachment
    from app.services.sub_agent_files import load_attachment_rows

    # The shared admission read: only rows no message shows yet that
    # are unbound or already this conversation's. A file that reached the turn
    # from a background task, or another conversation's message, is never
    # re-homed onto this reply.
    try:
        rows = await load_attachment_rows(
            [str(u) for u in generated_uuids], db=db, conversation_id=conversation_id
        )
    except Exception:
        logger.exception(
            "Failed to load FileAttachment rows for platform-chat generated files"
        )
        return [], []

    meta = [attachment_meta_from_file_attachment(fa) for fa in rows]
    return rows, meta


async def _link_generated_attachments(
    db: AsyncSession,
    attachment_rows: list,
    *,
    conversation_id: object,
    message_id: object,
) -> list:
    """Bind the turn's generated files to the reply that shows them.

    Goes through the shared atomic bind, which sets BOTH ``conversation_id``
    and ``message_id``: a file born without a conversation (an MCP download,
    a sub-agent output) is then found by ``load_conversation_files`` on the
    next turn. Returns the rows actually bound; a row another message
    bound meanwhile is left where it is. Best-effort: failure is logged and
    binds nothing, never aborting the response - the UPDATE runs in a
    SAVEPOINT so a failed bind cannot leave the session rollback-only and
    lose the reply's commit.
    """
    if not attachment_rows:
        return []
    from app.services.sub_agent_files import bind_attachment_rows

    try:
        async with db.begin_nested():
            return await bind_attachment_rows(
                db, attachment_rows, conversation_id=conversation_id, message_id=message_id
            )
    except Exception:
        logger.exception(
            "Failed to link platform-chat generated FileAttachment rows to "
            "message %s",
            message_id,
        )
        return []


def _narrow_attachment_meta(meta: dict, bound_rows: list) -> list[dict] | None:
    """Rewrite *meta*'s ``attachments`` to the rows the bind actually bound.

    Only that key changes - the rest of the message metadata is kept - so no
    chip points at a file another message kept. Returns the new block, or
    ``None`` (the key dropped) when nothing was bound.
    """
    from app.services.chat_messages import attachment_meta_from_file_attachment

    attachments = [attachment_meta_from_file_attachment(fa) for fa in bound_rows]
    if attachments:
        meta["attachments"] = attachments
    else:
        meta.pop("attachments", None)
    return attachments or None


async def _present_answer_artifacts(
    db: AsyncSession, payload: dict, text: str, *, supported: bool, conversation_id: object, user: User
) -> None:
    """Show the decks, reports and videos of one answer the way the client can.

    A client that renders them gets ``artifacts``; another gets the markers of
    ``content`` as links to the XTM One viewer. A dashboard is a link for both.
    The stored message keeps its markers either way: the web chat renders them.
    """
    from app.services.channel_notices import notice_language
    from app.services.platform_chat_artifacts import describe_answers, link_only_markers_as_links, markers_as_links

    if supported:
        [artifacts] = await describe_answers(db, [text], conversation_id=conversation_id, user_id=user.id)
        if artifacts:
            payload["artifacts"] = artifacts
        payload["content"] = await link_only_markers_as_links(
            db, text, conversation_id=conversation_id, user_id=user.id, language=notice_language(user)
        )
        return
    payload["content"] = await markers_as_links(
        db, text, conversation_id=conversation_id, user_id=user.id, language=notice_language(user)
    )


async def _present_restored_artifacts(
    db: AsyncSession, messages: list[dict], *, supported: bool, conversation_id: object, user: User
) -> None:
    """:func:`_present_answer_artifacts` for the answers of a restore, one read per kind for them all."""
    from app.services.channel_notices import notice_language
    from app.services.platform_chat_artifacts import describe_answers, link_only_markers_as_links, markers_as_links

    answers = [entry for entry in messages if entry.get("role") == "assistant" and "[[" in (entry.get("content") or "")]
    if not answers:
        return
    language = notice_language(user)
    if supported:
        described = await describe_answers(
            db, [entry["content"] for entry in answers], conversation_id=conversation_id, user_id=user.id
        )
        for entry, artifacts in zip(answers, described, strict=True):
            if artifacts:
                entry["artifacts"] = artifacts
            entry["content"] = await link_only_markers_as_links(
                db, entry["content"], conversation_id=conversation_id, user_id=user.id, language=language
            )
        return
    for entry in answers:
        entry["content"] = await markers_as_links(
            db, entry["content"], conversation_id=conversation_id, user_id=user.id, language=language
        )


@router.get("/chat/suggestions", response_model=list[str])
async def get_platform_chat_suggestions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    agent_slug: str | None = None,
):
    """Suggested opening actions for the embedded chat welcome screen.

    Same resolver as the native route, so the panel embedded in OpenCTI /
    OpenAEV and the XTM One web chat never disagree about what an agent is
    good for. Visibility matches ``/chat/agents``: no admin bypass on this
    external-facing surface.
    """
    from app.api.v1.helpers import get_user_group_ids
    from app.services.chat_suggestions import resolve_chat_suggestions

    user_group_ids = await get_user_group_ids(db, user.id)
    return await resolve_chat_suggestions(db, user.id, user_group_ids, agent_slug)


@router.get("/chat/agents")
async def list_platform_chat_agents(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    tag: str | None = None,
):
    """List agents available for platform chat.

    Pass ``?tag=openaev`` to filter agents by tag.
    Visibility: three-tier (own + group-shared + company-managed). Admins do not
    bypass this scope on the platform-chat endpoint — it is external-facing and
    must not leak private agents owned by other users to admin / service-account
    callers.
    """
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.agent import Agent

    user_group_ids = await get_user_group_ids(db, user.id)

    # The platform's own assistant leads the list, then alphabetical.
    #
    # This is not cosmetic: the embedded chat panel selects ``agents[0]`` when
    # the user has no previously chosen agent, so the ordering here *is* the
    # first-run default for every consumer of this endpoint. Sorting purely by
    # name handed that role to whichever agent happened to sort first
    # ("AEV Detection Rules Generator"), which is a poor front door for someone
    # opening the chat for the first time. A host that wants a different
    # shortlist narrows it with ``?tag=`` rather than relying on the order.
    result = await db.execute(
        select(Agent)
        .where(
            Agent.enabled.is_(True),
            user_visible_filter(Agent, user.id, user_group_ids),
        )
        .order_by(
            # CASE rather than `Agent.slug != ...`: the column is nullable, and a
            # NULL slug would make that comparison NULL — which Postgres sorts
            # into its own bucket (last, ascending), exiling every user-created
            # slug-less agent to the end instead of its alphabetical place. The
            # CASE puts "not the default" and "no slug at all" in the same bucket.
            case((Agent.slug == PLATFORM_DEFAULT_CHAT_AGENT_SLUG, 0), else_=1),
            Agent.name,
        )
    )
    agents = result.scalars().all()
    out = []
    for a in agents:
        if a.disable_chat:
            continue
        if tag and tag not in (a.tags or []):
            continue
        out.append(
            PlatformAgentOut(
                id=str(a.id),
                name=a.name,
                slug=a.slug,
                icon=a.icon,
                description=a.description,
                tags=a.tags or [],
            )
        )
    return out


@router.get("/chat/quota", response_model=PlatformChatQuotaOut | None)
async def get_platform_chat_quota(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The caller's agentic quota, for the embedded chat's quota indicator.

    The numbers the web chat's composer shows (``services.platform_chat_extras
    .resolve_platform_chat_quota``); ``null`` where agentic AI is not licensed
    (Community Edition), where the web chat shows no indicator either.
    """
    quota = await resolve_platform_chat_quota(db, user)
    return PlatformChatQuotaOut(**quota) if quota is not None else None


@router.get("/chat/prompts", response_model=PlatformChatPromptsOut)
async def list_platform_chat_prompts_route(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The prompts the embedded chat's picker offers.

    The web picker's list: own + shared + company-managed, ``hide_from_chat``
    excluded, newest first. Like ``/chat/agents``, no admin bypass on this
    external-facing surface.
    """
    prompts = await list_platform_chat_prompts(db, user)
    return PlatformChatPromptsOut(
        prompts=[
            PlatformChatPromptOut(
                id=str(p.id),
                title=p.name,
                content=p.content or "",
                description=p.description or None,
            )
            for p in prompts
        ]
    )


async def restored_message_entries(
    db: AsyncSession, user: User, rows: list, *, conversation_id: uuid.UUID, supports_artifacts: bool
) -> list[dict]:
    """*rows* (chronological) as the chatbot panel restores messages: hidden wakes left out.

    The agent that wrote each answer is stamped by :func:`attribute_message_agents`.
    """
    from app.api.v1.chat import _answered_by_payload, _message_is_hidden, _resumed_from_payload
    from app.services.interrupted_turn import INTERRUPTED_META_KEY
    from app.services.mention_dispatch import MENTION_FAILED_KEY

    # A hidden parent wake is the platform's prompt, not
    # the user's words: the web chat never renders one either.
    _shown = [m for m in rows if m.role in _PLATFORM_CHAT_RESTORED_ROLES and not _message_is_hidden(m)]
    # The caller's own ratings of the window's answers, in one query:
    # the panel shows a thumb it already gave as given.
    _feedback = await load_user_feedback_by_message(
        db, [m.id for m in _shown if m.role == "assistant"], user.id
    )

    messages = []
    for m in _shown:
        # ``id`` is what the panel rates a message by; ``created_at``
        # always carries its timezone.
        entry: dict = {
            "id": str(m.id),
            "role": m.role,
            "content": m.content,
            "created_at": _aware_isoformat(m.created_at),
        }
        _rating = _feedback.get(m.id) if m.role == "assistant" else None
        if _rating is not None:
            entry["feedback"] = {"rating": _rating.rating, "comment": _rating.comment}
        # Re-surface agent-generated file chips on conversation
        # restore. The marker text stays in ``content`` so the
        # chatbot can strip it and render the chips in source order.
        attachments = _platform_attachments_from_metadata(m.metadata_json)
        if attachments:
            entry["attachments"] = attachments
        # The conversations a user message referenced with @,
        # shown as chips like the web chat's.
        if m.role == "user" and m.metadata_json:
            from app.services.conversation_references import read_conversation_refs

            try:
                _refs = read_conversation_refs(json.loads(m.metadata_json))
            except (TypeError, ValueError):
                _refs = []
            if _refs:
                entry["conversation_refs"] = _refs
        # Re-surface the reasoning-details fields ("i" button) on
        # restored assistant messages — same keys as the live
        # ``done`` SSE payload so the chatbot renders them alike.
        if m.role == "assistant" and m.metadata_json:
            try:
                _meta = json.loads(m.metadata_json)
            except (TypeError, ValueError):
                _meta = None
            if isinstance(_meta, dict):
                for _k in RESTORED_ASSISTANT_META_KEYS:
                    if _meta.get(_k) is not None:
                        entry[_k] = _meta[_k]
                # The author of a mentioned agent's reply and its failure
                # note, as the live ``done`` and the REST serializer carry them.
                _answered_by = _answered_by_payload(_meta)
                if _answered_by:
                    entry["answered_by"] = _answered_by
                if _meta.get(MENTION_FAILED_KEY):
                    entry["mention_failed"] = True
                # A reply a cut turn left, and the reply its resume ended
                # with, as the REST serializer carries them: a host folds
                # them into one card.
                if _meta.get(INTERRUPTED_META_KEY):
                    entry["interrupted"] = True
                _resumed = _resumed_from_payload(_meta)
                if _resumed is not None:
                    entry["resumed_from"] = _resumed.model_dump()

        messages.append(entry)

    await _drop_deleted_attachments(db, messages)
    await _present_restored_artifacts(
        db, messages, supported=supports_artifacts, conversation_id=conversation_id, user=user
    )
    return messages


async def _drop_deleted_attachments(db: AsyncSession, messages: list[dict[str, Any]]) -> None:
    """Leave the files whose bytes are gone (#396) out of the panel's ``attachments``.

    ``@filigran/chatbot`` draws every attachment it is given as a live card (a
    download button, an image preview): a tombstone would show as downloadable,
    fetch a ``410`` and log it. The host draws the deleted card itself from its
    own copy of the conversation (``deleted_at`` / ``deleted_reason`` on the
    REST messages), so the panel never asks for those bytes. One query per page.
    """
    from app.models.file_attachment import FileAttachment

    ids: set[uuid.UUID] = set()
    for entry in messages:
        for att in entry.get("attachments") or []:
            try:
                ids.add(uuid.UUID(str(att.get("file_id"))))
            except (TypeError, ValueError):
                continue
    if not ids:
        return
    gone = {
        str(row[0])
        for row in (
            await db.execute(
                select(FileAttachment.id).where(FileAttachment.id.in_(ids), FileAttachment.deleted_at.is_not(None))
            )
        ).all()
    }
    if not gone:
        return
    for entry in messages:
        kept = [a for a in entry.get("attachments") or [] if str(a.get("file_id")).lower() not in gone]
        if kept:
            entry["attachments"] = kept
        else:
            entry.pop("attachments", None)


async def restore_chat_session_window(
    db: AsyncSession, user: User, conv: Any, *, supports_artifacts: bool
) -> tuple[PlatformChatSessionResponse, bool]:
    """The latest messages of *conv*, as the chatbot panel restores them, and whether older ones exist.

    Shared by the embedded chat and the web chat.
    """
    from app.models.chat import Message
    from app.services.chat_messages import read_visible_message_page

    # Latest-N (chronological) restore on the web chat's history
    # page: newest first with its ``(sequence, created_at, id)``
    # tie-breakers - legacy rows share ``sequence=0`` - and one
    # sentinel row past the window, so a thread of exactly N messages
    # is complete and only N + 1 proves there is more. N counts
    # the messages the panel shows: hidden parent wakes count for nothing.
    msg_criteria = [
        Message.conversation_id == conv.id,
        Message.role.in_(_PLATFORM_CHAT_RESTORED_ROLES),
    ]
    # Per-message attribution, reconstructed rather than stored.
    #
    # A transfer does not start a new conversation: it rewrites
    # ``conv.agent_id`` in place (see ``send_platform_chat_message``),
    # so one thread can hold answers from several agents and labelling
    # it all with the *current* one is wrong for everything before the
    # handover.
    #
    # No message records its author, but the two facts needed to derive
    # it already exist: ``original_agent_id`` is write-once and survives
    # every handover, and each turn that transferred persisted its
    # ``transfer_chain`` — whose entries are the *target* agents, in
    # order. So walk forward from the original agent and move the
    # pointer whenever a turn handed off. No new persistence, and it
    # works retroactively on history already in the database.
    # The walk is only sound when it starts at the real beginning. This
    # restore is capped at the latest N messages, and a handover that
    # happened before that window would be invisible here — starting
    # from the original agent would then confidently mislabel the whole
    # window. So on a truncated restore the walk is skipped entirely and
    # the panel falls back to the conversation's current agent, which is
    # right for the tail and merely unimproved for the rest. Saying
    # nothing beats saying something wrong.
    _rows, _truncated = await read_visible_message_page(db, msg_criteria, _PLATFORM_CHAT_RESTORE_LIMIT)

    # Both names in one round trip: for a thread never transferred the
    # original and the current agent are the same row.
    from app.models.agent import Agent

    _original_id = conv.original_agent_id if not _truncated else None
    _name_ids = [i for i in dict.fromkeys((_original_id, conv.agent_id)) if i]
    _agent_names: dict[uuid.UUID, str] = {}
    if _name_ids:
        _agent_names = {
            row[0]: row[1]
            for row in (
                await db.execute(
                    select(Agent.id, Agent.name).where(Agent.id.in_(_name_ids))
                )
            ).all()
        }
    _running_agent: str | None = (
        _agent_names.get(_original_id) if _original_id else None
    )

    messages = await restored_message_entries(
        db, user, _rows, conversation_id=conv.id, supports_artifacts=supports_artifacts
    )
    attribute_message_agents(messages, _running_agent, await recorded_message_agents(db, _rows))

    # The response reports the conversation's stored agent fields as-is.
    # ``agent_id`` stays nullable for legacy rows, and ``ON DELETE SET
    # NULL`` clears it with its agent; ``agent_name`` is only returned
    # when that row still exists.
    conv_agent_id = str(conv.agent_id) if conv.agent_id else None
    conv_agent_name: str | None = (
        _agent_names.get(conv.agent_id) if conv.agent_id else None
    )

    restored = PlatformChatSessionResponse(
        conversation_id=str(conv.id),
        messages=messages,
        agent_id=conv_agent_id,
        agent_name=conv_agent_name,
        workspace_id=str(conv.workspace_id) if conv.workspace_id else None,
        channel=conv.channel,
    )
    return restored, _truncated


class ChatPanelSessionResponse(PlatformChatSessionResponse):
    """A session as the web chat and the agent detail's chat panel restore it."""

    #: Older messages exist beyond the restored ones: page them with
    #: ``GET /chat/sessions/{id}/messages?before=<oldest id>``.
    has_more: bool = False
    #: Where the conversation's turn stands, as ``GET /api/v1/chat/conversations/{id}``
    #: says it (``chat.read_conversation_turn_state``).
    is_processing: bool = False
    pending_approvals: list[dict] | None = None
    interrupted_turn: InterruptedTurnInfo | None = None


async def read_message_page(
    db: AsyncSession, user: User, conv: Any, *, before: str | None, limit: int, supports_artifacts: bool
) -> dict:
    """The page of *conv* older than message *before*, as the chat panel pages a thread: ``{messages, has_more}``.

    Both count the messages the panel shows: hidden parent wakes count for nothing.
    The caller has decided *user* may read *conv*.
    """
    from app.api.v1.helpers import parse_conversation_id
    from app.models.chat import Message
    from app.services.chat_messages import message_cursor_filter, read_visible_message_page

    criteria = [Message.conversation_id == conv.id, Message.role.in_(("user", "assistant"))]
    if before:
        anchor = (
            await db.execute(
                select(Message).where(
                    Message.id == parse_conversation_id(before, not_found_detail="Cursor message not found"),
                    Message.conversation_id == conv.id,
                )
            )
        ).scalar_one_or_none()
        if anchor is None:
            raise HTTPException(status_code=404, detail="Cursor message not found")
        criteria.append(message_cursor_filter(anchor))
    rows, has_more = await read_visible_message_page(db, criteria, limit)
    messages = await restored_message_entries(
        db, user, rows, conversation_id=conv.id, supports_artifacts=supports_artifacts
    )
    # A page starts mid-thread: only the handovers and recorded authors inside it name an agent.
    attribute_message_agents(messages, None, await recorded_message_agents(db, rows))
    return {"messages": messages, "has_more": has_more}


@router.post("/chat/sessions")
async def create_platform_chat_session(
    body: PlatformChatSessionRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Create or retrieve a chat session for the authenticated user."""
    from app.core.utils import safe_uuid

    agent = await _resolve_platform_agent(db, body.agent_slug)

    # The chatbot replays whatever id its localStorage holds, so a malformed
    # one is a stale id like an unknown one: both start a fresh conversation.
    conv_uuid = safe_uuid(body.conversation_id)
    if conv_uuid is not None:
        from app.models.chat import Conversation

        conv_result = await db.execute(
            select(Conversation).where(
                Conversation.id == conv_uuid,
                Conversation.user_id == user.id,
            )
        )
        conv = conv_result.scalar_one_or_none()
        if conv:
            restored, _has_more = await restore_chat_session_window(
                db, user, conv, supports_artifacts=body.supports_artifacts
            )
            return restored

    from app.models.chat import Conversation

    workspace_uuid = None
    if body.workspace_id:
        # Resolved exactly as the web chat's creation resolves it: the manage
        # right on the workspace, and never an archived one.
        from app.api.v1.helpers import get_manageable_entity
        from app.models.workspace import Workspace
        from app.services.workspaces import (
            WorkspaceActionRefusedError,
            ensure_workspace_accepts_conversations,
        )

        workspace = await get_manageable_entity(
            db, Workspace, user, body.workspace_id, action="use", entity_label="Workspace"
        )
        try:
            ensure_workspace_accepts_conversations(workspace)
        except WorkspaceActionRefusedError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
        workspace_uuid = workspace.id

    conv = Conversation(
        user_id=user.id,
        channel=PLATFORM_CHAT_CHANNEL,
        agent_id=agent.id,
        title=f"Platform chat — {user.email}",
        workspace_id=workspace_uuid,
    )
    db.add(conv)
    await db.commit()
    return PlatformChatSessionResponse(
        conversation_id=str(conv.id),
        messages=[],
        agent_id=str(agent.id),
        agent_name=agent.name,
        workspace_id=str(workspace_uuid) if workspace_uuid else None,
        channel=conv.channel,
    )


class PlatformConversationReference(BaseModel):
    """A conversation the embedded chat's @ menu offers.

    Proxied by the host: who shared a conversation stays in XTM One, so the
    panel learns only whether it is the caller's own.
    """

    id: str
    title: str
    key: str
    updated_at: str | None = None
    is_own: bool


class PlatformConversationReferences(BaseModel):
    conversations: list[PlatformConversationReference]


@router.get(
    "/chat/conversation-references", response_model=PlatformConversationReferences
)
async def search_platform_conversation_references(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
    exclude: Annotated[str | None, Query()] = None,
):
    """The web chat's ``GET /chat/conversation-references``, for the panel.

    Gated on authentication alone, like ``/chat/sessions``: it reads only what
    the caller may open.
    """
    from app.core.utils import safe_uuid as _safe_uuid
    from app.services.conversation_references import search_reference_candidates

    rows = await search_reference_candidates(
        db, user, query=q, limit=limit, exclude=_safe_uuid(exclude) if exclude else None
    )
    return PlatformConversationReferences(
        conversations=[
            PlatformConversationReference(
                **{k: v for k, v in row.items() if k != "owner_name"}
            )
            for row in rows
        ]
    )


class PlatformArtifactDownloadRequest(BaseModel):
    #: ``pdf`` / ``pptx`` / ``pptx_native`` for a deck, ``pdf`` for a report,
    #: ``mp4`` for a video: the ``downloads`` of its descriptor.
    format: str = Field(min_length=1, max_length=20)


@router.get("/chat/artifacts/{kind}/{artifact_id}")
async def get_platform_chat_artifact(
    kind: str,
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """A deck, report or video an answer showed, described as of now.

    The panel asks again for a video whose render was still running when the
    answer arrived. The artifact's own reading rule applies; anything the
    caller may not read is the same 404 as an unknown id.
    """
    from app.services.platform_chat_artifacts import describe_artifact

    descriptor = await describe_artifact(db, kind, artifact_id, user)
    if descriptor is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return descriptor


@router.post("/chat/artifacts/{kind}/{artifact_id}/download")
async def download_platform_chat_artifact(
    kind: str,
    artifact_id: str,
    body: PlatformArtifactDownloadRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The file of a deck, report or video an answer showed.

    A deck (PDF, PowerPoint) or a report (PDF) is exported now, against the
    caller's export budget; a video downloads its current render. Reached
    through the product's proxy, which mints the caller's token.
    """
    from app.services.platform_chat_artifacts import ArtifactDownloadError, download_response

    try:
        return await download_response(db, kind, artifact_id, body.format, user)
    except ArtifactDownloadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None


@router.get("/chat/sessions")
async def list_platform_chat_sessions(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List the caller's platform-chat conversations (chatbot history menu).

    Deliberately ungated beyond authentication, and it returns only the caller's
    own conversations. ``require_chat_enabled`` refused it as soon as AI was
    switched off; swapping to a licence-only gate then still refused it on a
    plain ``PLATFORM_MODE=ai_disabled`` install, which has no licence and so
    resolves to Community Edition — the mode's own flagship configuration,
    and exactly the read FR-008 requires to answer 200.

    That also restores what ``require_chat_enabled`` documents: "existing chat
    history stays reachable for read-only exploration … only chat *write* paths
    return 403".

    Returns ``{"conversations": [...]}`` with the snake_case fields the
    ``@filigran/chatbot`` history menu parses (``conversation_id``,
    ``title``, ``updated_at``, ``message_count``, which counts the messages a
    restore shows).  Conversations without one are skipped — the
    session-create endpoint provisions an empty conversation shell before
    the first message, and listing those would clutter the menu with
    untitled entries.

    ``agent_id`` / ``agent_name`` name the agent the conversation actually
    belongs to.  Without them the panel had nothing to label history with
    and fell back to whichever agent happened to be selected, so reopening a
    CTEM Assistant thread while another agent was picked relabelled the whole
    conversation — while the stored agent went on answering it (see the
    ``conv.agent_id`` branch in ``send_platform_chat_message``).  Nullable:
    conversations predating per-conversation routing have no agent, and the
    panel falls back as before.

    ``last_message_preview`` is the row's line under its title, as the web
    chat's list writes it (``chat.last_message_previews``): the panel sets it
    on a send, and a list read without it took it away again. ``channel`` is
    where the conversation was started.
    """
    from sqlalchemy import and_, func
    from sqlalchemy.orm import aliased

    from app.models.agent import Agent as AgentModel
    from app.models.chat import Conversation, Message
    from app.services.chat_messages import message_shown_clause

    # The messages the panel restores: user and assistant rows, not
    # a hidden parent wake (the web chat's sidebar reads the same
    # marker). Correlated to each listed conversation and served by the
    # ``messages.conversation_id`` index, instead of a GROUP BY over every
    # message of every user joined afterwards.
    visible = and_(
        Message.conversation_id == Conversation.id,
        Message.role.in_(("user", "assistant")),
        message_shown_clause(),
    )
    visible_count = (
        select(func.count()).select_from(Message).where(visible).scalar_subquery()
    )
    # Outer join: an agent deleted since the conversation ran must not make
    # the conversation itself vanish from the history menu.
    conv_agent = aliased(AgentModel)
    result = await db.execute(
        select(Conversation, visible_count, conv_agent.name)
        .outerjoin(conv_agent, conv_agent.id == Conversation.agent_id)
        .where(
            Conversation.user_id == user.id,
            Conversation.channel == PLATFORM_CHAT_CHANNEL,
            Conversation.is_archived.is_(False),
            select(Message.id).where(visible).exists(),
        )
        .order_by(Conversation.updated_at.desc())
        .limit(_PLATFORM_CHAT_LIST_LIMIT)
    )
    rows = result.all()
    previews = await last_message_previews(db, [conv.id for conv, _, _ in rows])
    conversations = [
        {
            "conversation_id": str(conv.id),
            "title": conv.title or "",
            "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
            "message_count": int(n or 0),
            "agent_id": str(conv.agent_id) if conv.agent_id else None,
            "agent_name": agent_name,
            # The workspace the panel groups it under, or None.
            "workspace_id": str(conv.workspace_id) if conv.workspace_id else None,
            "last_message_preview": previews.get(conv.id),
            "channel": conv.channel,
        }
        for conv, n, agent_name in rows
    ]
    return {"conversations": conversations}


@router.delete("/chat/sessions/{conversation_id}", status_code=204)
async def delete_platform_chat_session(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Remove a conversation from the chatbot history menu.

    Mirrors the native chat semantics: the conversation is **archived**
    (not hard-deleted), preserving the audit trail while dropping it from
    every listing.  Only the owner can archive, and only platform-channel
    conversations are reachable here — a forged id pointing at someone
    else's (or a web) conversation 404s.
    """
    from app.api.v1.helpers import parse_conversation_id
    from app.models.chat import Conversation

    conv_uuid = parse_conversation_id(conversation_id)

    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conv_uuid,
            Conversation.user_id == user.id,
            Conversation.channel == PLATFORM_CHAT_CHANNEL,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    conv.is_archived = True
    await db.commit()


async def _get_platform_chat_answer(db: AsyncSession, user: User, conversation_id: str, message_id: str) -> Message:
    """The assistant message the embedded chat rates, or the same 404 for anything else.

    Only an answer in a platform-channel conversation the caller owns: a
    malformed id, an unknown one, someone else's conversation, a web chat
    conversation and a user message all get ``Message not found`` - the
    platform surface never says which of them it was.
    """
    from app.api.v1.helpers import parse_conversation_id, parse_path_id
    from app.models.chat import Conversation, Message

    conv_uuid = parse_conversation_id(conversation_id, not_found_detail="Message not found")
    msg_uuid = parse_path_id(message_id, not_found_detail="Message not found")
    result = await db.execute(
        select(Message)
        .join(Conversation, Message.conversation_id == Conversation.id)
        .where(
            Conversation.id == conv_uuid,
            Conversation.user_id == user.id,
            Conversation.channel == PLATFORM_CHAT_CHANNEL,
            Message.id == msg_uuid,
            Message.role == "assistant",
        )
    )
    msg = result.scalar_one_or_none()
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


@router.post(
    "/chat/conversations/{conversation_id}/messages/{message_id}/feedback",
    response_model=MessageFeedbackResponse,
)
async def upsert_platform_chat_message_feedback(
    conversation_id: str,
    message_id: str,
    body: MessageFeedbackUpsert,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Rate an answer of the embedded chat: the web chat's thumbs.

    The web chat's one write path (``chat.apply_message_feedback``), so the
    rating reaches Langfuse and the trace store the same way. Not AI-gated:
    rating an answer already given runs no model.
    """
    from app.api.v1.chat import apply_message_feedback
    from app.services.annotations import normalize_comment

    msg = await _get_platform_chat_answer(db, user, conversation_id, message_id)
    feedback = await apply_message_feedback(db, msg, user, body.rating, normalize_comment(body.comment))
    return MessageFeedbackResponse(rating=feedback.rating, comment=feedback.comment)


@router.delete(
    "/chat/conversations/{conversation_id}/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_platform_chat_message_feedback(
    conversation_id: str,
    message_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Retract the caller's rating of an answer; nothing to retract is a 204 too."""
    from app.api.v1.chat import retract_message_feedback

    msg = await _get_platform_chat_answer(db, user, conversation_id, message_id)
    await retract_message_feedback(db, msg, user)
    return StarletteResponse(status_code=status.HTTP_204_NO_CONTENT)


class PlatformChatSteerRequest(BaseModel):
    """Mid-run steering message for a conversation with an active SSE run."""

    conversation_id: str
    content: str
    # Accepted for parity with the chatbot's steer POST body; the running
    # loop already knows its agent, so the slug is informational only.
    agent_slug: str | None = None


class PlatformChatApprovalDecision(BaseModel):
    """One reviewer verdict on one proposed tool call."""

    tool_call_id: str
    # Constrained like the run path's ``ApprovalDecision``: an unvalidated
    # string here reached an applier that ran anything not spelled "reject",
    # so a typo approved the call instead of refusing it.
    decision: Literal["approve", "approve_always", "reject"]
    rejection_reason: str | None = None


class PlatformChatPageActionAnswer(BaseModel):
    """The page's answer to one ``act_on_page`` action."""

    action_id: str = Field(max_length=64)
    ok: bool
    error: str | None = Field(default=None, max_length=500)
    url: str = Field(default="", max_length=2048)
    title: str = Field(default="", max_length=500)
    snapshot: str = Field(default="", max_length=20_000)


class PlatformChatApprovalRequest(BaseModel):
    """What a running turn is waiting on: tool-call decisions, or a page's answer.

    One route for both because the host proxies (OpenCTI's chatbot proxy)
    forward this one whole; a new route would need a release of every host.
    """

    conversation_id: str
    decisions: list[PlatformChatApprovalDecision] = []
    page_action: PlatformChatPageActionAnswer | None = None


@router.get("/chat/conversations/{conversation_id}/pending-approvals")
async def get_platform_chat_pending_approvals(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """What a paused turn in this conversation is still waiting on.

    Ungated beyond authentication, like ``list_platform_chat_sessions`` above
    and for the same reason: it is a caller-own read, and FR-008 says a
    read must still answer 200 with AI off. ``require_chat_enabled`` consults
    the AI state, so it turned a recoverable pause into a 403.

    ``approval_required`` is a single event on a stream, so a widget whose page
    reloaded has lost it: it holds no proposals, not even the ``tool_call_id``s
    a decision has to name. Without somewhere to ask, the turn waits for an
    answer that can no longer be given and the conversation simply stops
    replying — with nothing on screen to say why.

    So the client asks on mount. An empty list is the ordinary answer and not an
    error: most conversations are not paused.

    Same ownership rule as the decision endpoint, and 404 rather than 403 so
    this never confirms that someone else's conversation exists.
    """
    from app.api.v1.helpers import parse_path_id
    from app.models.chat import Conversation
    from app.services.chat_turn_guard import get_pending_approvals, mark_client_seen

    conv_uuid = parse_path_id(conversation_id, not_found_detail="Conversation not found")

    conv = (
        await db.execute(
            select(Conversation).where(
                Conversation.id == conv_uuid,
                Conversation.user_id == user.id,
                Conversation.channel == PLATFORM_CHAT_CHANNEL,
            )
        )
    ).scalar_one_or_none()
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Asking is itself proof somebody is there. The stream that carried the
    # prompt is gone after a reload and can never say so again, so a paused turn
    # would otherwise count a present reviewer as absent.
    await mark_client_seen(conversation_id)

    # Whether a turn is still going, from the marker the steering path already
    # refreshes every second for the life of the turn. A decision answered on a
    # recovered prompt resumes a turn whose stream the reload closed, so the
    # reply lands in the conversation with nothing arriving live — without this
    # a client can only poll the transcript blind and guess when to stop. Here
    # it polls one cheap call and stops when the turn does.
    from app.core.redis import redis_client

    try:
        _redis = await redis_client()
        _turn_running = bool(await _redis.get(_steer_active_key(conv_uuid)))
    except Exception as exc:  # advisory only — never fail the recovery read
        logger.warning(
            "Turn-state check failed for conv %s: %s",
            conversation_id,
            format_exception_message(exc),
        )
        _turn_running = False

    return {
        "conversation_id": conversation_id,
        "proposals": await get_pending_approvals(conversation_id) or [],
        "turn": "running" if _turn_running else "idle",
    }


async def approval_conversation(
    db: AsyncSession,
    user: User,
    conversation_id: str,
    *,
    channel: str | None,
    surface: str | None,
) -> uuid.UUID:
    """The id of the caller's conversation a running turn's answer is for, else 404.

    404 rather than 403, so the route never confirms that someone else's
    conversation exists. *channel* narrows it to one surface's conversations.
    The parsed id is the one the turn's keys are written under, whatever
    casing the client sent.
    """
    from app.core.utils import safe_uuid
    from app.models.chat import Conversation
    from app.services import chat_approval_resume

    conv_uuid = safe_uuid(conversation_id)
    if conv_uuid is None:
        raise HTTPException(status_code=404, detail="Conversation not found") from None
    query = select(Conversation.id).where(Conversation.id == conv_uuid, Conversation.user_id == user.id)
    if channel is not None:
        query = query.where(Conversation.channel == channel)
    if (await db.execute(query)).scalar_one_or_none() is None:
        # Only the conversation's owner may answer (FR-008a); a refusal is
        # recorded only for a conversation that exists, so invented ids write
        # no audit rows.
        if surface is not None:
            _exists = await db.execute(select(Conversation.id).where(Conversation.id == conv_uuid))
            if _exists.scalar_one_or_none() is not None:
                await chat_approval_resume.record_refused_decision(
                    db,
                    user_id=str(user.id),
                    conversation_id=str(conv_uuid),
                    reason="not_the_paused_user",
                    surface=surface,
                    summary="Refused a tool approval decision for a conversation belonging to another user",
                )
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv_uuid


#: Decisions Redis could not carry to the turn: none of them was applied.
APPROVAL_UNAVAILABLE_DETAIL = (
    "The running response cannot be reached right now, so no decision was applied. Try again shortly."
)


async def apply_approval_decisions(
    db: AsyncSession,
    user: User,
    conversation_id: str,
    decisions: list[PlatformChatApprovalDecision],
    *,
    surface: str,
    channel: str | None,
) -> int:
    """Push a reviewer's verdicts to the waiting turn through Redis (it may run on another replica).

    Returns how many tool calls were decided. Every refusal is recorded
    (FR-008a). 409 when no turn is waiting (finished, cancelled, already
    answered): the client refreshes rather than retries, since a decision on a
    settled call has nothing to apply to. 503 when Redis cannot carry them.
    """
    from app.services import chat_approval_resume, chat_turn_guard
    from app.tools.approval import DECISION_IMPERSONATED_BY

    conv_key = str(await approval_conversation(db, user, conversation_id, channel=channel, surface=surface))
    # The approver rides with each decision: the turn may have been sent by
    # another of the owner's sessions, impersonated or not (FR-017b).
    _impersonated_by = getattr(user, "impersonated_by", None)
    verdicts = {
        d.tool_call_id: {
            "decision": d.decision,
            "rejection_reason": d.rejection_reason,
            DECISION_IMPERSONATED_BY: str(_impersonated_by) if _impersonated_by else None,
        }
        for d in decisions
    }
    try:
        await chat_turn_guard.mark_client_seen(conv_key)
        pushed = await chat_turn_guard.push_approval_decision(conv_key, verdicts)
    except Exception as exc:
        logger.warning("Approval hand-off failed for conv %s: %s", conv_key, exc)
        raise HTTPException(status_code=503, detail=APPROVAL_UNAVAILABLE_DETAIL) from exc
    if not pushed:
        # Recorded (FR-008a): the reviewer saw the prompt go and has no other way to tell.
        await chat_approval_resume.record_refused_decision(
            db,
            user_id=str(user.id),
            conversation_id=conv_key,
            reason="no_pending_proposal",
            surface=surface,
            summary="Refused a tool approval decision: no turn is awaiting approval",
        )
        raise HTTPException(
            status_code=409,
            detail="No turn is currently awaiting approval for this conversation",
        )
    return len(verdicts)


@router.post("/chat/messages/approve")
async def approve_platform_chat_tools(
    body: PlatformChatApprovalRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Answer a running turn that paused for tool approval, or for a page's answer.

    Twin of the steering endpoint below, and deliberately shaped the same way:
    the client already knows how to POST into a running turn, so approval is
    another payload on a familiar path rather than a new mechanism.
    """
    if bool(body.decisions) == (body.page_action is not None):
        raise HTTPException(
            status_code=400, detail="Send either decisions or a page action"
        )

    if body.decisions:
        decided = await apply_approval_decisions(
            db,
            user,
            body.conversation_id,
            body.decisions,
            surface="platform_stream",
            channel=PLATFORM_CHAT_CHANNEL,
        )
        return {"status": "accepted", "decided": decided}

    from app.services import chat_turn_guard

    # A page action is not an approval decision, so it leaves no approval
    # refusal in the trail.
    conv_uuid = await approval_conversation(
        db, user, body.conversation_id, channel=PLATFORM_CHAT_CHANNEL, surface=None
    )
    await chat_turn_guard.mark_client_seen(body.conversation_id)
    # The parsed id, as the tool keys its wait: an uppercase or braced
    # spelling passes the ownership check and would miss the key.
    if not await chat_turn_guard.push_page_action_result(
        str(conv_uuid), body.page_action.model_dump()
    ):
        raise HTTPException(
            status_code=409, detail="No page action is waiting for this answer"
        )
    return {"status": "accepted"}


@router.post("/chat/messages/steer")
async def steer_platform_chat_message(
    body: PlatformChatSteerRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Inject a user message into a conversation's RUNNING agent loop.

    The embedded chatbot's twin of the native web chat's steer route
    (``POST /api/v1/web/chat/messages/steer``).  The message is persisted immediately (thread ordering: the
    steering user message lands before the assistant text it influences)
    and pushed onto the conversation's Redis steering list; the SSE run
    polls that list and injects the text at the next iteration boundary
    (see ``send_platform_chat_message``).

    Responds 409 when no run is currently active for the conversation —
    the chatbot rolls back its optimistic bubble and restores the text
    into the composer, exactly like a backend without steering support.
    """
    from app.api.v1.chat import CHAT_MESSAGES_PER_MINUTE
    from app.api.v1.helpers import parse_conversation_id
    from app.channels.router import store_message
    from app.core.rate_limit import enforce_rate_limit
    from app.core.redis import redis_client
    from app.models.chat import Conversation

    content = (body.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    conv_uuid = parse_conversation_id(body.conversation_id)

    result = await db.execute(
        select(Conversation).where(
            Conversation.id == conv_uuid,
            Conversation.user_id == user.id,
            Conversation.channel == PLATFORM_CHAT_CHANNEL,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Same per-user throttle bucket as the send paths (web + platform).
    await enforce_rate_limit(
        None,
        "chat-send",
        limit=CHAT_MESSAGES_PER_MINUTE,
        window_seconds=60,
        bucket=str(user.id),
    )

    # The row is written before the enqueue and committed after it: its
    # sequence is taken under the conversation row lock, which the turn's
    # next ``store_message`` waits on, so the steering message lands before
    # the assistant text it influences. A refused enqueue rolls it back.
    await store_message(
        db,
        conv_uuid,
        role="user",
        content=content,
        channel=PLATFORM_CHAT_CHANNEL,
    )

    # Steering only makes sense against a live run; without one the text
    # would be persisted but never answered. 409 → client-side rollback.
    try:
        redis = await redis_client()
        queued = await redis.eval(
            _STEER_ENQUEUE_LUA,
            2,
            _steer_active_key(conv_uuid),
            _steer_list_key(conv_uuid),
            content,
            str(_STEER_LIST_TTL_S),
        )
    except Exception as exc:
        await db.rollback()
        logger.warning("Steering enqueue failed for conv %s: %s", conv_uuid, format_exception_message(exc))
        raise HTTPException(
            status_code=503, detail="Steering is temporarily unavailable"
        ) from exc
    if not queued:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="No response is currently being generated for this conversation",
        )
    await db.commit()

    return {"status": "steered", "conversation_id": str(conv_uuid)}


@router.post("/chat/messages")
@known_secrets_scoped()
async def send_platform_chat_message(
    body: PlatformChatRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _chat_gate: None = Depends(require_chat_enabled),
):
    """Send a message as the authenticated user and get the agent's streamed response.

    Inline file attachments
    -----------------------
    ``body.files`` is processed for THIS message only — files are
    handed to the agent run in memory and **never persisted as
    ``FileAttachment`` rows**.  Subsequent turns in the same
    conversation will NOT see them; rehydration of history only
    reconstructs ``FileRef`` entries from persisted attachments.

    Clients that need a file to be visible across multiple turns
    (e.g. the OpenAEV AskAI integration where turn 3 may reference
    a file uploaded in turn 1) must either:

    * Re-send the inline ``files`` array on every turn that needs it, or
    * Upload the file through the standard attachment endpoint, which
      writes a ``FileAttachment`` row that history rehydration picks up
      automatically.

    Caps (enforced before base64 decode to prevent OOM):
      * per-file: ``_MAX_INLINE_FILE_BYTES`` ≈ 25 MiB.
      * per-request total: ``_MAX_INLINE_FILES_TOTAL_BYTES`` ≈ 100 MiB.
    """
    import asyncio
    import json

    from starlette.responses import StreamingResponse

    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    from app.api.v1.helpers import parse_conversation_id
    from app.channels.router import get_conversation_history, store_message, without_current_turn
    from app.models.chat import Conversation
    from app.services.chat_turn_stream import slim_frame

    if body.conversation_id:
        conv_result = await db.execute(
            select(Conversation).where(
                Conversation.id == parse_conversation_id(body.conversation_id),
                Conversation.user_id == user.id,
            )
        )
        conv = conv_result.scalar_one_or_none()
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")

        # This turn runs without the conversation's lock: it stays off every
        # conversation a Slack or Teams turn can reach.
        from app.services.channel_continuation import platform_chat_refusal

        _continuation_refusal = await platform_chat_refusal(db, conv)
        if _continuation_refusal:
            raise HTTPException(status_code=409, detail=_continuation_refusal)

        # Legacy migration: platform chat conversations created BEFORE
        # this PR were stored with ``channel="web"`` and therefore still
        # appear in the personal chat sidebar (which filters on
        # ``channel == "web"`` — see ``chat.py::list_conversations``).
        # When the API caller re-uses such a conversation we want to
        # opportunistically flip its channel to ``api`` so it drops
        # out of the sidebar on the next refresh.
        #
        # We MUST NOT do this unconditionally on every
        # ``(id, user_id)`` match: a user (or any third-party
        # integration with their token) could otherwise call this
        # endpoint with the ``conversation_id`` of one of their
        # *genuine* personal-chat conversations and silently hide it
        # from their own sidebar — a confusing "where did my chat go?"
        # incident with no audit trail.
        #
        # Gate on the platform-chat default title prefix
        # (``f"Platform chat — {user.email}"`` set by
        # ``create_platform_chat_session``).  That title is rewritten
        # to ``content[:80]`` as soon as the first message arrives
        # (see a few lines below), so the prefix is unique to
        # session-only legacy conversations created by the platform
        # endpoint, and the personal-chat UI never produces a title
        # starting with "Platform chat".  A regular web conversation
        # therefore cannot match this guard and cannot be silently
        # hidden by an API call.
        if conv.channel != PLATFORM_CHAT_CHANNEL and (
            isinstance(conv.title, str) and conv.title.startswith("Platform chat")
        ):
            conv.channel = PLATFORM_CHAT_CHANNEL

        # After a transfer the conversation's agent_id points to the
        # target agent.  Honour that instead of always using the slug
        # from the request body (which still reflects the original agent
        # selected by the client).
        if conv.agent_id:
            from app.models.agent import Agent as _AgentModel
            from app.services.autonomous_pentest import DEFAULT_ORCHESTRATOR_SLUG

            _conv_agent_result = await db.execute(
                select(_AgentModel).where(
                    _AgentModel.id == conv.agent_id,
                    _AgentModel.enabled.is_(True),
                    # Only the run-only orchestrator is barred here, matching the
                    # slug branch of ``_resolve_platform_agent``. A conversation
                    # opened against a ``disable_chat`` GENERATION agent (phishing
                    # landing page / email, ...) must keep resolving to that same
                    # agent - excluding every ``disable_chat`` agent here is what
                    # re-broke generation for any session-backed chatbot call.
                    # ``is_distinct_from`` keeps this NULL-safe (a null slug is
                    # "distinct from" the orchestrator, so it is not excluded).
                    _AgentModel.slug.is_distinct_from(DEFAULT_ORCHESTRATOR_SLUG),
                )
            )
            _conv_agent = _conv_agent_result.scalar_one_or_none()
            if _conv_agent:
                agent = _conv_agent
            else:
                agent = await _resolve_platform_agent(db, body.agent_slug)
        else:
            agent = await _resolve_platform_agent(db, body.agent_slug)
    else:
        agent = await _resolve_platform_agent(db, body.agent_slug)
        conv = Conversation(
            user_id=user.id,
            channel=PLATFORM_CHAT_CHANNEL,
            agent_id=agent.id,
            title=content[:80],
        )
        db.add(conv)
        await db.flush()

    # Conversations referenced with @ - the web chat's rule.
    from app.services.conversation_references import (
        build_reference_brief,
        resolve_referenced_conversations,
        with_conversation_refs,
    )

    _reference_resolution = await resolve_referenced_conversations(
        db, user, body.referenced_conversation_ids, current_conversation_id=conv.id
    )
    platform_reference_brief = await build_reference_brief(db, _reference_resolution)

    # A new message retires anything still pending (#173, FR-002c4). This
    # surface holds its proposals in the same Redis key the native web chat uses
    # and serves them back from ``GET pending-approvals``, so without this it
    # kept offering an abandoned prompt as answerable — the behaviour already
    # fixed on REST and the web chat. The rule lives in one helper precisely
    # because applying it per call site is what missed this one.
    #
    # Not while a browser turn holds the conversation: this surface starts its
    # turn without the lock, so retiring then wiped the only copy of a prompt
    # that turn is still waiting on - the ordering the web chat keeps by retiring
    # only once ``acquire_or_fold`` gave it the turn.
    from app.services.chat_approval_resume import retire_for_new_turn
    from app.services.chat_turn_guard import is_turn_active

    try:
        _turn_in_flight = await is_turn_active(str(conv.id))
    except Exception:
        _turn_in_flight = False
    if not _turn_in_flight:
        await retire_for_new_turn(db, conv.id, surface="platform_stream")

    _pf_user_msg = await store_message(
        db,
        conv.id,
        role="user",
        content=content,
        channel=PLATFORM_CHAT_CHANNEL,
        metadata_json=with_conversation_refs(None, _reference_resolution),
    )
    # The message a platform stop cutting this turn resumes from.
    _pf_user_msg_id = getattr(_pf_user_msg, "id", None)

    if conv.title.startswith("Platform chat"):
        conv.title = content[:80]

    # Commit user message before the agent loop so it persists even if
    # streaming is interrupted (same pattern as chat.py line 755).
    await db.commit()

    # Capture ORM attributes into locals before the agentic loop — a
    # db.rollback() during tool execution expires session-bound objects.
    conv_id = conv.id
    bind_known_secrets(conv_id)
    agent_obj = agent
    user_id_str = str(user.id)
    # Host page/application context (e.g. current URL) — forwarded to the
    # agent so it is aware of where the user is. Captured into a local for use
    # inside the streaming closure below. Enriched with the embedding host's
    # product NAME (from the ``X-Platform-Product`` header set by the OpenCTI /
    # OpenAEV chatbot proxy) so the agent knows which host application it is
    # embedded in — without it the agent only sees a bare path (e.g.
    # ``/dashboard``) and misattributes the page to XTM One. URLs are left to
    # the integration layer (see _enrich_page_context) to avoid a competing
    # base URL in the prompt.
    page_context = _enrich_page_context(body.context, request.headers)

    # A client declaring it will answer approval prompts is the only evidence
    # of that capability for a platform that never declared one itself.
    # Recorded so the settings page can offer to gate again; it decides
    # nothing on its own.
    if body.supports_tool_approval:
        await _note_client_approval_support(request.headers.get("X-Platform-Product"))

    # The message stored above is the newest entry; run_agent_chat appends
    # ``content`` itself, so the model reads the turn once and a large input
    # document in it is detached from its only copy.
    history = without_current_turn(await get_conversation_history(db, conv_id), content)

    # ── Pre-flight quota check ──────────────────────────────────────────
    # Must happen before the stream starts (HTTP 200 is sent), so the
    # client receives a proper 429 status instead of a 200 with error text.
    from app.services.quota import (
        QuotaExceededError,
        check_agentic_quota,
        seconds_until_period_reset,
    )

    try:
        await check_agentic_quota(db, user.id)
    except QuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": str(seconds_until_period_reset(exc.period))},
        ) from exc

    # Pass all request headers — the resolver extracts scope values
    # from headers listed in each integration's scope_config.
    _scope_headers: dict[str, str] = dict(request.headers)

    # ── Convert inline base64 file attachments to FileRef objects ────
    # Size validation runs BEFORE ``base64.b64decode`` so a malicious or
    # buggy caller can't OOM the worker by sending a multi-hundred-MB
    # base64 string and letting the decoder allocate the decoded bytes
    # before we even look at the size.  The decoded-byte caps
    # (``_MAX_INLINE_FILE_BYTES`` / ``_MAX_INLINE_FILES_TOTAL_BYTES``)
    # are translated to base64-length caps via ``_max_b64_length_for``;
    # the per-file ratio is 4 base64 chars per 3 decoded bytes so the
    # length cap is roughly ``decoded_cap * 4/3`` plus a small slack
    # for padding.  Anything longer than the cap CANNOT decode to a
    # valid-sized payload, so we reject without spending the decode
    # memory.  After the cheap length check we still measure the
    # decoded bytes and reject if the actual length is over budget
    # (defence in depth — covers edge cases where the encoder produced
    # a shorter-than-typical base64 representation).  Malformed base64
    # returns 400 with a precise error instead of surfacing as a 500.
    #
    # Inline FileRefs are kept in memory only for this single agent run.
    # Subsequent turns must re-send any attachments (history rehydration
    # only restores FileRefs from FileAttachment rows). The platform
    # client is responsible for re-uploading attachments per turn —
    # documented at the endpoint level.
    import binascii

    _file_refs: list | None = None
    if body.files:
        from app.llm.base import FileRef

        if len(body.files) > _MAX_INLINE_FILES_PER_REQUEST:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Too many inline files: {len(body.files)} "
                    f"(max {_MAX_INLINE_FILES_PER_REQUEST} per request)."
                ),
            )

        _file_refs = []
        total_bytes = 0
        total_b64_length = 0
        for idx, inline in enumerate(body.files):
            # ── Pre-decode length guard ─────────────────────────
            # Reject before allocating the decoded bytes.
            b64_length = len(inline.data or "")
            if b64_length > _MAX_INLINE_FILE_B64_LENGTH:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Inline file #{idx} ({inline.filename!r}) "
                        f"base64 payload is {b64_length} chars — even "
                        f"after decoding it would exceed the "
                        f"{_MAX_INLINE_FILE_BYTES}-byte per-file cap."
                    ),
                )
            total_b64_length += b64_length
            if total_b64_length > _MAX_INLINE_FILES_TOTAL_B64_LENGTH:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cumulative inline base64 payload "
                        f"({total_b64_length} chars) — even after "
                        f"decoding it would exceed the "
                        f"{_MAX_INLINE_FILES_TOTAL_BYTES}-byte request cap."
                    ),
                )
            try:
                raw = base64.b64decode(inline.data, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid base64 payload for file #{idx} "
                        f"({inline.filename!r}): {exc}"
                    ),
                ) from exc
            # Defence in depth: re-check the decoded length.  The
            # pre-decode guard above is the primary protection
            # (it rejects on the cheap base64-length side without
            # ever allocating the decoded bytes), but a base64
            # string with embedded whitespace can decode to fewer
            # bytes than ``_max_b64_length_for`` upper-bounds — so
            # the post-decode check stays for completeness and to
            # catch any future caps drift.
            if len(raw) > _MAX_INLINE_FILE_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Inline file #{idx} ({inline.filename!r}) is "
                        f"{len(raw)} bytes — exceeds the "
                        f"{_MAX_INLINE_FILE_BYTES}-byte per-file cap."
                    ),
                )
            total_bytes += len(raw)
            if total_bytes > _MAX_INLINE_FILES_TOTAL_BYTES:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Cumulative inline file payload "
                        f"({total_bytes} bytes) exceeds the "
                        f"{_MAX_INLINE_FILES_TOTAL_BYTES}-byte request cap."
                    ),
                )
            _file_refs.append(
                FileRef.from_bytes(
                    raw,
                    filename=inline.filename,
                    content_type=inline.content_type,
                )
            )

    # ── Build a FileContext so agent-generated files can be surfaced ─────
    # Load the FULL conversation context (all persisted ``FileAttachment``
    # rows for this conversation) plus this turn's inline refs, using the
    # same loader ``run_agent_chat`` would call internally
    # (``load_conversation_files``).  We build it here — rather than letting
    # ``run_agent_chat`` build it from ``file_context=None`` — only so we can
    # diff it against ``_pf_initial_file_ids`` after the run to isolate the
    # files the agent produced this turn (via the ``generate_file`` built-in
    # tool or a custom tool's ``$output_files``) and turn them into download
    # chips for the embedded chat.  Passing the full context keeps multi-turn
    # file access at parity with native chat — seeding it from this turn's
    # inline refs alone would make ``run_agent_chat`` skip conversation-file
    # loading and hide previously uploaded / generated files from the agent.
    from app.services.agent_chat import load_conversation_files

    _pf_file_context = await load_conversation_files(db, str(conv_id), _file_refs)
    _pf_initial_file_ids: set[str] = {
        ref.file_id
        for ref in _pf_file_context.list_files()
        if getattr(ref, "file_id", None)
    }

    # ── Non-streaming mode: just run the agent and return plain JSON ─────
    if not body.stream:
        from app.services.agent_chat import run_agent_chat

        # The same handover record the streaming path keeps: the
        # restore walks ``transfer_chain`` to name who wrote each answer, so a
        # transfer that leaves none mislabels every later message.
        _ns_transfer_chain: list[dict[str, str]] = []

        async def _ns_on_event(event: dict) -> None:
            _record_transfer_start(event, _ns_transfer_chain)

        try:
            result_text, transfer_id = await run_agent_chat(
                db,
                user_id_str,
                agent_obj,
                history,
                content,
                conversation_id=str(conv_id),
                page_context=page_context,
                on_event=_ns_on_event,
                scope_headers=_scope_headers or None,
                files=_file_refs,
                file_context=_pf_file_context,
                reference_brief=platform_reference_brief,
            )
        except asyncio.CancelledError as e:
            # A platform stop cut the turn before it answered: uvicorn's
            # graceful drain, or the loop wound down at an iteration boundary
            # (``worker_shutdown.TurnWoundDown``). The caller gets a
            # deterministic retryable answer instead of a dropped connection;
            # nothing is queued for resume, since the caller sends the same
            # request again and a queued resume would answer it twice. Any
            # other cancellation (the client left) propagates as before.
            if not is_graceful_shutdown_cancellation(e):
                raise
            from app.core.worker_shutdown import drain_cut_cause

            # Survive the delivered cancellation to write the answer, as
            # ``GracefulShutdownDrainMiddleware`` does (main.py).
            _ns_task = asyncio.current_task()
            if _ns_task is not None:
                while _ns_task.cancelling():
                    _ns_task.uncancel()
            failure = classify_platform_turn_failure(e)
            logger.warning(
                "Platform chat turn of conversation %s cut by a platform stop (cause %s) before it answered: "
                "answered %d %s, retryable, not queued for resume",
                conv_id,
                drain_cut_cause(),
                failure.status_code,
                failure.error_type,
            )
            await db.commit()
            return JSONResponse(
                status_code=failure.status_code,
                content=failure.body(conv_id),
                headers={"Retry-After": str(failure.retry_after)},
            )
        except Exception as e:
            logger.error("Platform chat error: %s", format_exception_message(e), exc_info=True)
            # An error status, never a 200 whose content the caller would take
            # for the agent's answer (a playbook step parses it as its output).
            failure = classify_platform_turn_failure(e)
            await db.commit()
            headers = {"Retry-After": str(failure.retry_after)} if failure.retry_after is not None else None
            return JSONResponse(
                status_code=failure.status_code,
                content=redact_known_secrets_for_display(failure.body(conv_id)),
                headers=headers,
            )

        if not result_text or not result_text.strip():
            result_text = "I encountered an issue. Please try again."

        if transfer_id:
            conv.transfer_source_agent_id = conv.agent_id
            conv.agent_id = uuid.UUID(transfer_id)

        _ns_attachment_rows, _ns_attachment_meta = await _collect_generated_attachments(
            db,
            _pf_file_context,
            _pf_initial_file_ids,
            conversation_id=conv_id,
            reply_text=result_text,
        )
        _ns_chain = _completed_transfer_chain(_ns_transfer_chain, transfer_id)
        _ns_meta_dict: dict = {"attachments": _ns_attachment_meta} if _ns_attachment_meta else {}
        if _ns_chain:
            _ns_meta_dict["transfer_chain"] = _ns_chain

        _msg_id: str | None = None
        try:
            _stored = await store_message(
                db,
                conv_id,
                role="assistant",
                content=result_text,
                channel=PLATFORM_CHAT_CHANNEL,
                metadata_json=json.dumps(_ns_meta_dict) if _ns_meta_dict else None,
            )
            _msg_id = str(_stored.id)
            _ns_bound = await _link_generated_attachments(
                db, _ns_attachment_rows, conversation_id=conv_id, message_id=_stored.id
            )
            if len(_ns_bound) != len(_ns_attachment_rows):
                _ns_attachment_meta = _narrow_attachment_meta(_ns_meta_dict, _ns_bound)
                _stored.metadata_json = json.dumps(_ns_meta_dict) if _ns_meta_dict else None
            await db.commit()
        except SQLAlchemyError as e:
            # The failure ended the transaction: nothing it held can land.
            logger.error(
                "Failed to persist assistant message for conv %s: %s", conv_id, format_exception_message(e)
            )
        except Exception as e:
            logger.error(
                "Failed to persist assistant message for conv %s: %s", conv_id, format_exception_message(e)
            )
            # The transaction outlived the failure: land what it holds before
            # answering, not in the get_db teardown after the response.
            await db.commit()

        response: dict = {
            "content": result_text,
            "conversation_id": str(conv_id),
        }
        if _msg_id:
            response["message_id"] = _msg_id
        if _ns_attachment_meta:
            response["attachments"] = _ns_attachment_meta
        await _present_answer_artifacts(
            db, response, result_text, supported=body.supports_artifacts, conversation_id=conv_id, user=user
        )
        # Same keys, on the same conditions, as the streaming ``done`` payload.
        if transfer_id:
            response["transfer_agent_id"] = transfer_id
            if _ns_chain:
                response["transfer_agent_name"] = _ns_chain[-1]["agent_name"]
        if _ns_chain:
            response["transfer_chain"] = _ns_chain
        # Read in another platform's chat, by people who cannot read the variables.
        return redact_known_secrets_for_display(response)

    # ── Streaming mode (default): SSE with tool events + content chunks ──
    # Head+tail bounded accumulation mirrors the web chat (single source
    # of truth in chat.py — see ReasoningAccumulator, #2399).
    from app.api.v1.chat import (
        ReasoningAccumulator,
        strip_reasoning_echoed_in_reply,
    )

    # Iterated after this route returned: the turn's known-secrets scope is the stream's own.
    @known_secrets_scoped()
    async def _event_stream():
        from app.core.redis import redis_client
        from app.llm.base import ChatMessage as _LLMChatMessage
        from app.services.agent_chat import run_agent_chat
        from app.tools.agent_loop import drain_steering_queue

        bind_known_secrets(conv_id)
        stream_q: asyncio.Queue[dict | None] = asyncio.Queue()

        # ── Mid-run steering bridge ──────────────────────────────────
        # Steer POSTs (possibly served by another uvicorn worker) RPUSH
        # onto the conversation's Redis list; this task polls the list,
        # feeds the local queue consumed by ``run_agent_loop`` at
        # iteration boundaries, and keeps the "run active" marker alive
        # so the steer endpoint can 409 when nothing is running. Its
        # exit closes the bridge: the steers still queued move to the
        # local queue, and later ones are refused.
        steering_queue: asyncio.Queue[str] = asyncio.Queue()

        def _queue_steers(items: list) -> None:
            for item in items:
                text = (item.decode() if isinstance(item, bytes) else str(item)).strip()
                if text:
                    steering_queue.put_nowait(text)

        async def _steer_bridge(stop: asyncio.Event) -> None:
            try:
                redis = await redis_client()
            except Exception as exc:
                logger.warning(
                    "Steering bridge unavailable for conv %s: %s", conv_id, format_exception_message(exc)
                )
                return
            active_key = _steer_active_key(conv_id)
            list_key = _steer_list_key(conv_id)
            # Discard stale entries before the first poll tick: a list
            # left by a run that could not close its bridge holds texts
            # already persisted in history — re-injecting them here would
            # duplicate them.
            with contextlib.suppress(Exception):
                await redis.delete(list_key)
            try:
                while not stop.is_set():
                    with contextlib.suppress(Exception):
                        await redis.set(active_key, "1", ex=_STEER_ACTIVE_TTL_S)
                        while True:
                            item = await redis.lpop(list_key)
                            if item is None:
                                break
                            _queue_steers([item])
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            stop.wait(), timeout=_STEER_POLL_INTERVAL_S
                        )
            finally:
                with contextlib.suppress(Exception):
                    _queue_steers(
                        await redis.eval(_STEER_CLOSE_LUA, 2, active_key, list_key)
                    )

        def _open_bridge() -> tuple[asyncio.Task, asyncio.Event]:
            stop = asyncio.Event()
            return asyncio.create_task(_steer_bridge(stop)), stop

        async def _close_bridge() -> None:
            # ``gather(return_exceptions=True)`` waits the bridge out and
            # retrieves its exception without re-raising.
            _steer_stop.set()
            await asyncio.gather(_bridge_task, return_exceptions=True)

        _bridge_task, _steer_stop = _open_bridge()

        # Per-segment accumulators — reset for every fold-in pass so each
        # assistant message carries ITS OWN tool/reasoning metadata.
        accumulated: list[str] = []
        tool_names_seen: list[str] = []
        iterations_seen = 0
        reasoning_acc = ReasoningAccumulator()
        tool_call_trace_seen: list[dict] = []
        is_truncated_seen = False

        # Context-window occupancy, read off the agent loop's own budgeting
        # figures (see the ``thinking`` event it emits per iteration).
        # Conversation-level rather than per-segment, so deliberately NOT reset
        # in the segment loop below: a fold-in segment keeps the last honest
        # reading instead of dropping the gauge back to nothing.
        context_usage = ContextUsageTracker()

        # A client that cannot render the deck, report and video markers
        # never sees one streamed: its ``done`` carries them as links.
        # No client renders a dashboard marker: it is always left out.
        from app.services.platform_chat_artifacts import ArtifactMarkerStreamFilter

        _marker_filter = ArtifactMarkerStreamFilter(link_only=bool(body.supports_artifacts))

        async def _on_token(chunk: str) -> None:
            accumulated.append(chunk)
            shown = _marker_filter.feed(chunk) if _marker_filter is not None else chunk
            if shown:
                await stream_q.put({"type": "stream", "content": shown})

        _platform_transfer_chain: list[dict[str, str]] = []

        async def _request_approval(proposals: list[dict]) -> dict[str, dict]:
            """Ask the client to decide, then wait for the answer.

            Emitting and waiting are deliberately separate steps: the decision
            arrives on a different HTTP request (and possibly a different
            replica), so it comes back through the conversation's Redis inbox
            rather than anything held in this process.
            """
            from app.services.chat_turn_guard import (
                await_approval_decision,
                clear_pending_approvals,
                set_pending_approvals,
            )

            # Record the pause before announcing it. This path holds no turn
            # marker, so the record is what tells the decision endpoint a turn
            # is actually waiting — without it every decision this client sent
            # was refused while the turn went on blocking. It also survives the
            # request the prompt was streamed on, which is the whole difficulty
            # here: the answer arrives on a different one.
            await set_pending_approvals(str(conv_id), proposals)
            await stream_q.put(
                {
                    "type": "approval_required",
                    "conversation_id": str(conv_id),
                    "proposals": proposals,
                }
            )
            try:
                # The pause survives a reload — the client re-reads it from
                # ``pending-approvals`` and answers as if nothing happened. But
                # a stream that drops and never comes back leaves nobody able to
                # answer at all, and the turn would wait for them forever, so
                # that case alone is bounded. Reconnecting resets it; a reviewer
                # who is simply thinking is never on a clock.
                return await await_approval_decision(
                    str(conv_id), client_present=lambda: not _client_gone
                )
            finally:
                await clear_pending_approvals(str(conv_id))

        async def _on_event(event: dict) -> None:
            nonlocal iterations_seen
            nonlocal is_truncated_seen
            if event.get("status") == "tool_start" and event.get("tools"):
                tool_names_seen.extend(event["tools"])
            if event.get("status") == "thinking" and event.get("iteration"):
                iterations_seen = max(iterations_seen, event["iteration"])
            context_usage.observe(event)
            # Capture per-tool-call details for the reasoning-details dialog,
            # mirroring the native web chat. Both input and output are
            # hard-bounded with the shared ``_trim_trace_text`` helper so the
            # metadata_json + SSE ``done`` frame stay well within limits.
            if event.get("status") == "tool_done" and event.get("tool"):
                from app.tools.agent_loop import _trim_trace_text

                _raw_input = event.get("tool_input_full") or event.get("tool_input") or ""
                _raw_output = event.get("tool_output", "") or ""
                tool_call_trace_seen.append(
                    {
                        "name": event["tool"],
                        "input": _trim_trace_text(_raw_input, 30_000),
                        "output": _trim_trace_text(_raw_output, 30_000),
                        "success": bool(event.get("success", True)),
                    }
                )
            # Detect iteration-budget truncation (same signals as the web chat:
            # "wind_down" / "composing" mean the final answer is best-effort).
            # Mid-loop re-prompts (stall guard, steering resume-promise
            # guard — reasons single-sourced in
            # ``CONTINUATION_COMPOSING_REASONS``) are NOT truncation — the
            # run continues with its full budget.
            if event.get("status") in ("wind_down", "composing"):
                from app.tools.agent_loop import CONTINUATION_COMPOSING_REASONS

                if event.get("status") == "wind_down" or (
                    event.get("reason") not in CONTINUATION_COMPOSING_REASONS
                ):
                    is_truncated_seen = True
            # Accumulate reasoning prose so the embedded chatbot can surface
            # it in its reasoning-details panel ("i" button), mirroring the
            # native web chat, which persists it with the turn.
            if event.get("status") == "thinking_text" and event.get("content"):
                reasoning_acc.add(str(event["content"]))
            _record_transfer_start(event, _platform_transfer_chain)
            if _should_forward_platform_status_event(event):
                # Without the prompt copies and full tool payloads the trace
                # above was built from: no consumer of this stream reads them,
                # and they weighed up to ~190K characters per LLM
                # call plus every tool's whole output.
                await stream_q.put(slim_frame({"type": "status", **event}))

        _platform_transfer_id: str | None = None

        async def _run(
            seg_content: str,
            seg_history: list,
            seg_files: list | None,
            seg_reference_brief: str | None = None,
            seg_checkpoint=None,
        ):
            nonlocal _platform_transfer_id
            try:
                result_text, transfer_id = await run_agent_chat(
                    db,
                    user_id_str,
                    agent_obj,
                    seg_history,
                    seg_content,
                    conversation_id=str(conv_id),
                    page_context=page_context,
                    on_event=_on_event,
                    # Mid-turn approval. The turn stays open: the
                    # proposals go out on this same SSE stream, the client POSTs
                    # a decision to /chat/messages/approve, and the loop
                    # continues with the tool result. Ending and restarting the
                    # turn instead would discard the agent's reasoning and make
                    # an approval feel like a rejection.
                    # Opt-in. A client that has not declared support
                    # would ignore the approval_required event and never
                    # answer, leaving this turn blocked forever — a silent
                    # hang with no error to explain it. Absence therefore
                    # means "end the turn instead", which any client version
                    # can render as ordinary assistant text.
                    request_approval=(
                        _request_approval if body.supports_tool_approval else None
                    ),
                    # The widget acts on its host page and answers.
                    page_actions=body.supports_page_actions,
                    # So a standing approval granted from an embedded assistant
                    # names the administrator behind an impersonated session
                    # (FR-017b), like the run path does.
                    impersonated_by=getattr(user, "impersonated_by", None),
                    on_token=_on_token,
                    scope_headers=_scope_headers or None,
                    files=seg_files,
                    file_context=_pf_file_context,
                    steering_queue=steering_queue,
                    reference_brief=seg_reference_brief,
                    # The completed tool rounds a resume replays.
                    on_checkpoint=seg_checkpoint,
                )
                _platform_transfer_id = transfer_id
                return result_text
            except Exception as e:
                logger.error("Platform chat error: %s", format_exception_message(e), exc_info=True)
                # The same error_type and retryable as the non-streaming body,
                # ahead of the text the chat panel shows.
                await stream_q.put(classify_platform_turn_failure(e).frame())
                return "I'm sorry, I encountered an error processing your request."
            finally:
                if _marker_filter is not None and (_unfinished := _marker_filter.flush()):
                    await stream_q.put({"type": "stream", "content": _unfinished})
                await stream_q.put(None)

        # ── Segment loop ─────────────────────────────────────────────
        # A steered turn can produce multiple response segments on one
        # SSE stream: steering texts that arrive too late for in-loop
        # injection (e.g. during the final answer stream) are folded
        # into an immediate follow-up pass — intermediate ``done`` +
        # fresh ``thinking`` + ``stream`` + final ``done`` — which the
        # ``@filigran/chatbot`` multi-segment handling renders as its
        # own assistant bubble.  Mirrors the web chat's fold-in in ``chat.py``.
        _seg_history = history
        _seg_content = content
        _client_gone = False
        # The first segment's tool rounds, kept for the automatic resume of a
        # turn a platform stop cuts; a fold-in segment answers
        # steering messages, which the resume declines.
        from app.services import turn_checkpoint

        _pf_checkpoint = turn_checkpoint.TurnCheckpoint(conv_id, user_id=user_id_str)
        # Every segment's answer: the final one carries the turn's files,
        # a fetched one when any of them names it.
        _seg_texts: list[str] = []
        # The finished turn the stream proposes a next message after, once its
        # ``done`` is out (``services/chat_reply_suggestions.py``).
        _pf_suggestion_turn: dict | None = None

        for _seg_index in range(_MAX_STEER_SEGMENTS + 1):
            accumulated.clear()
            tool_names_seen.clear()
            iterations_seen = 0
            reasoning_acc.reset()
            tool_call_trace_seen.clear()
            is_truncated_seen = False

            # Inline file refs accompany only the ORIGINAL message — a
            # fold-in steering segment must not re-inline them.
            chat_task = asyncio.create_task(
                _run(
                    _seg_content,
                    _seg_history,
                    _file_refs if _seg_index == 0 else None,
                    # The references, like the files, belong to the original message.
                    platform_reference_brief if _seg_index == 0 else None,
                    _pf_checkpoint.offer if _seg_index == 0 else None,
                )
            )

            _text = _SSETextCoalescer()
            _clock = asyncio.get_running_loop().time
            try:
                while True:
                    if _text.holding and _clock() >= _text.deadline:
                        yield f"data: {json.dumps(redact_known_secrets_for_display(_text.flush()))}\n\n"
                        continue
                    try:
                        item = await asyncio.wait_for(
                            stream_q.get(),
                            timeout=(
                                _text.deadline - _clock()
                                if _text.holding
                                else _SSE_KEEPALIVE_INTERVAL_SECONDS
                            ),
                        )
                    except TimeoutError:
                        if _text.holding:
                            continue
                        # Heartbeat: keep the SSE connection warm during long
                        # silent windows (e.g. while the LLM generates a large
                        # tool-call argument such as a multi-page PDF report,
                        # which produces no text tokens and no status events
                        # for a minute or more). Without this, intermediaries
                        # in front of the backend (k8s ingress, nginx) close
                        # the idle connection and the browser aborts the
                        # stream mid-flight with a generic error even though
                        # the agent loop completes successfully server-side.
                        # SSE comment lines (starting with ':') are ignored by
                        # the chatbot SSE parser and the OpenCTI proxy pipes
                        # them through untouched.
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        _held = _text.flush()
                        if _held is not None:
                            yield f"data: {json.dumps(redact_known_secrets_for_display(_held))}\n\n"
                        break
                    for _event in _text.offer(item, _clock()):
                        yield f"data: {json.dumps(redact_known_secrets_for_display(_event))}\n\n"
            except (asyncio.CancelledError, GeneratorExit) as _cut_exc:
                logger.warning(
                    "Platform chat stream interrupted for conv %s", conv_id
                )
                _client_gone = True
                # A platform stop (not the client leaving): queued for the
                # next process, which answers the same message.
                _pf_cut = _seg_index == 0 and is_graceful_shutdown_cancellation(_cut_exc)
                _pf_cut_queued = _pf_cut and await queue_cut_platform_turn(
                    _cut_exc, conv_id, user_id=user_id_str, user_message_id=_pf_user_msg_id
                )
            else:
                _pf_cut = _pf_cut_queued = False

            try:
                assistant_text = await chat_task
            except asyncio.CancelledError as _task_exc:
                # The turn wound down at an iteration boundary inside the
                # task (``worker_shutdown.TurnWoundDown``, its checkpoint
                # holding the work) while the stream still ran: the same
                # platform cut, queued the same way. Any other
                # cancellation of the task goes on as before.
                if not is_graceful_shutdown_cancellation(_task_exc):
                    raise
                if _seg_index == 0 and not _pf_cut:
                    _pf_cut = True
                    _pf_cut_queued = await queue_cut_platform_turn(
                        _task_exc, conv_id, user_id=user_id_str, user_message_id=_pf_user_msg_id
                    )
                assistant_text = None
            except Exception as e:
                logger.error(
                    "Platform chat task failed for conv %s: %s", conv_id, format_exception_message(e)
                )
                assistant_text = None
            # A cut turn still unanswered keeps its work for the resume; one
            # that answered (the drain included) drops it with the answer.
            _pf_discard_checkpoint = _seg_index == 0 and await settle_platform_checkpoint(
                _pf_checkpoint,
                conv_id,
                cut=_pf_cut,
                cut_queued=_pf_cut_queued,
                answered=bool(assistant_text and assistant_text.strip()),
                user_message_id=_pf_user_msg_id,
            )

            if platform_cut_unanswered(cut=_pf_cut, answer=assistant_text):
                # A platform stop cut the turn before it answered: no answer
                # is stored or sent (a false error reply would precede the
                # real one); the resume, or the person, answers it.
                await _close_bridge()
                break
            # Whether the agent answered: the stand-in below is no answer to
            # suggest a next message after.
            _pf_answered = bool(assistant_text and assistant_text.strip())
            if not assistant_text or not assistant_text.strip():
                assistant_text = "I encountered an issue. Please try again."

            # Steering texts that the loop could not inject (arrived during
            # the final answer stream / after the loop returned).  They are
            # already persisted by the steer endpoint — fold them into an
            # immediate follow-up pass unless the client is gone, a
            # transfer happened (the follow-up would target the wrong
            # agent), or the segment budget is exhausted.
            _leftover = drain_steering_queue(steering_queue)
            _is_final = (
                _client_gone
                or _platform_transfer_id is not None
                or _seg_index >= _MAX_STEER_SEGMENTS
            )
            if _is_final or not _leftover:
                # Close the bridge before the turn ends: a steer queued
                # since its last poll comes back with the close and is
                # folded in like any leftover; a later one gets a 409.
                await _close_bridge()
                _leftover.extend(drain_steering_queue(steering_queue))
                if _leftover and not _is_final:
                    _bridge_task, _steer_stop = _open_bridge()
            _is_final = _is_final or not _leftover

            _attachment_meta: list[dict] | None = None
            _attachment_rows: list = []
            _seg_texts.append(assistant_text)
            if _is_final:
                (
                    _attachment_rows,
                    _attachment_meta,
                ) = await _collect_generated_attachments(
                    db,
                    _pf_file_context,
                    _pf_initial_file_ids,
                    conversation_id=conv_id,
                    reply_text="\n".join(_seg_texts),
                )

            metadata_json: str | None = None
            _meta_dict: dict = {}
            # Same reasoning/reply de-duplication as the web chat
            #: held-back pre-tool text merged into the reply by
            # the deliverable rescue must not also sit in the reasoning
            # panel.
            _reasoning_text = strip_reasoning_echoed_in_reply(
                reasoning_acc.render(), assistant_text
            ).strip()
            if tool_names_seen:
                _meta_dict["tool_names"] = list(dict.fromkeys(tool_names_seen))
                _meta_dict["tool_call_count"] = len(tool_names_seen)
                _meta_dict["iterations"] = iterations_seen
            if tool_call_trace_seen:
                _meta_dict["tool_call_trace"] = list(tool_call_trace_seen)
            if is_truncated_seen:
                _meta_dict["is_truncated"] = True
            if _reasoning_text:
                _meta_dict["reasoning"] = _reasoning_text
            _final_transfer_chain = (
                _completed_transfer_chain(_platform_transfer_chain, _platform_transfer_id)
                if _is_final
                else []
            )
            if _final_transfer_chain:
                _meta_dict["transfer_chain"] = _final_transfer_chain
            if _attachment_meta:
                _meta_dict["attachments"] = _attachment_meta
            # Persisted so reopening the conversation restores the gauge
            # instead of leaving it blank until the next turn — the newest
            # assistant message carrying these IS the conversation's current
            # context occupancy.
            _meta_dict.update(context_usage.meta())
            _meta_dict.update(context_usage.command_meta())
            if _meta_dict:
                metadata_json = json.dumps(_meta_dict)

            if _is_final and _platform_transfer_id:
                import uuid as _uuid_mod

                conv.transfer_source_agent_id = conv.agent_id
                conv.agent_id = _uuid_mod.UUID(_platform_transfer_id)

            _msg_id: str | None = None
            try:
                _stored = await store_message(
                    db,
                    conv_id,
                    role="assistant",
                    content=assistant_text,
                    channel=PLATFORM_CHAT_CHANNEL,
                    metadata_json=metadata_json,
                )
                _msg_id = str(_stored.id)
                if _attachment_rows:
                    _bound_rows = await _link_generated_attachments(
                        db, _attachment_rows, conversation_id=conv_id, message_id=_stored.id
                    )
                    if len(_bound_rows) != len(_attachment_rows):
                        _attachment_meta = _narrow_attachment_meta(_meta_dict, _bound_rows)
                        _stored.metadata_json = json.dumps(_meta_dict) if _meta_dict else None
                if _pf_discard_checkpoint:
                    await turn_checkpoint.discard(db, conv_id)
                await db.commit()
            except SQLAlchemyError as e:
                # The failure ended the transaction: nothing it held can land.
                logger.error(
                    "Failed to persist assistant message for conv %s: %s", conv_id, format_exception_message(e)
                )
            except Exception as e:
                logger.error(
                    "Failed to persist assistant message for conv %s: %s", conv_id, format_exception_message(e)
                )
                # The transaction outlived the failure: land what it holds before
                # the done event, not in the get_db teardown after the stream.
                await db.commit()

            done_payload: dict = {
                "type": "done",
                "content": assistant_text,
                "conversation_id": str(conv_id),
            }
            if _msg_id:
                done_payload["message_id"] = _msg_id
            if tool_names_seen:
                done_payload["tool_names"] = list(dict.fromkeys(tool_names_seen))
                done_payload["tool_call_count"] = len(tool_names_seen)
                done_payload["iterations"] = iterations_seen
            if tool_call_trace_seen:
                done_payload["tool_call_trace"] = list(tool_call_trace_seen)
            if is_truncated_seen:
                done_payload["is_truncated"] = True
            if _reasoning_text:
                done_payload["reasoning"] = _reasoning_text
            # Final reading for the turn. The chatbot already tracks the live
            # value off the ``thinking`` events, but a turn whose last
            # iteration compacted ends lower than it peaked — so the closing
            # frame gets the last word.
            done_payload.update(context_usage.meta())
            # ``attachments`` carry only ``file_id`` + display metadata (no
            # absolute download URL).  The embedded chat resolves the download
            # through its own backend proxy (which mints the XTM One JWT), so
            # the end user never authenticates to XTM One directly.
            if _attachment_meta:
                done_payload["attachments"] = _attachment_meta
            await _present_answer_artifacts(
                db,
                done_payload,
                assistant_text,
                supported=body.supports_artifacts,
                conversation_id=conv_id,
                user=user,
            )
            if _is_final and _platform_transfer_id:
                done_payload["transfer_agent_id"] = _platform_transfer_id
                if _final_transfer_chain:
                    done_payload["transfer_agent_name"] = _final_transfer_chain[-1]["agent_name"]
            if _final_transfer_chain:
                done_payload["transfer_chain"] = _final_transfer_chain
            if not _client_gone:
                yield f"data: {json.dumps(redact_known_secrets_for_display(done_payload))}\n\n"

            if _is_final:
                if body.supports_reply_suggestions and _pf_answered and _msg_id and not _client_gone:
                    _pf_suggestion_turn = {
                        "conversation_id": conv_id,
                        "message_id": _msg_id,
                        "user_id": user.id,
                        "agent_id": conv.agent_id,
                        "user_text": _seg_content,
                        "assistant_text": assistant_text,
                        "turn_meta": done_payload,
                    }
                break

            # ── Fold-in follow-up segment ────────────────────────────
            # Fresh ``thinking`` re-arms the chatbot UI.  History is
            # re-fetched from the DB — it now contains the original user
            # message, the persisted steering user message(s) and the
            # just-persisted assistant segment, in thread order — and the
            # steering texts become the follow-up pass's user message
            # (the persisted steering messages stay in history: the
            # follow-up message is their joined text, not a stored row).
            yield f"data: {json.dumps({'type': 'status', 'status': 'thinking'})}\n\n"
            try:
                _seg_history = await get_conversation_history(db, conv_id)
            except Exception as e:
                logger.warning(
                    "Fold-in history refresh failed for conv %s: %s", conv_id, format_exception_message(e)
                )
                _seg_history = [
                    *_seg_history,
                    _LLMChatMessage(role="assistant", content=assistant_text),
                ]
            _seg_content = "\n\n".join(_leftover)

        if _pf_suggestion_turn is not None:
            from app.services.chat_reply_suggestions import suggest_after_turn

            _suggestion = await suggest_after_turn(**_pf_suggestion_turn)
            if _suggestion is not None:
                yield f"data: {json.dumps(_suggestion)}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            # Disable proxy buffering so heartbeats and tokens are flushed to
            # the client immediately (mirrors the OpenCTI chatbot proxy).
            "X-Accel-Buffering": "no",
        },
    )


# _resolve_platform_user removed — chat endpoints now use Depends(get_current_user)


async def _resolve_orchestrator_agent(db: AsyncSession, slug: str):
    """Resolve an autonomous-run orchestrator by slug, with NO chat fallback.

    Deliberately not ``_resolve_platform_agent``: that resolver is built for chat
    and answers the built-in orchestrator's slug with a 404, as it does an
    unknown slug; its fallback to the first company-managed chat agent
    only runs when no agent is named. The run start and the run purge name the
    orchestrator on purpose, so they need this strict exact-slug resolver: an
    exact match or a 404, which the purge already treats as "no memory to clear".

    ``disable_chat`` is not filtered here: the built-in orchestrator IS
    ``disable_chat``, precisely because only a trusted run may drive it.
    """
    from app.models.agent import Agent

    result = await db.execute(
        select(Agent).where(Agent.slug == slug, Agent.enabled.is_(True))
    )
    agent = result.scalar_one_or_none()
    if agent is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Autonomous runs require the '{slug}' orchestrator agent, which "
                "is not available. Seed the built-in agents first."
            ),
        )
    return agent


async def _resolve_platform_agent(db: AsyncSession, slug: str | None):
    """Resolve the platform-chat agent for a request.

    The ``/chat/*`` endpoints carry two different resolution semantics, told
    apart by whether the caller named an agent:

    * **Explicit slug** - the caller is being specific. This covers both a user
      picking a particular chatbot AND every programmatic "Generate with AI"
      call OpenAEV/OpenCTI make (phishing landing page / email, detection rules,
      NLQ, ...). The named agent is returned *exactly*, even when it is
      ``disable_chat``: that flag means "hidden from the interactive picker", NOT
      "never invocable by slug" - the generation agents are ``disable_chat``
      precisely so a human does not pick them by hand, yet they are invoked by
      slug on every generation. A slug that does not resolve is a **404, never a
      silent substitution**: answering a phishing-landing-page request with "the
      OpenCTI Dashboard Creator" or "the XTM One Assistant" (whatever happened to
      be the first company-managed chat agent on that instance) is exactly the
      incident this branch exists to prevent.

    * **No slug** - the generic "Ask AI" widget with no chosen agent. Fall
      through to the first company-managed CHAT agent. Here ``disable_chat`` IS
      excluded: there is no caller intent, so a backend-run-only surface must
      never be handed back as the default.

    The autonomous attack-path orchestrator is the ONE agent barred from this
    user-facing endpoint in both branches. It has a run-only contract (large
    OpenAEV write surface + agent-level tradecraft memory writer) and may
    only be driven by a trusted run, which resolves it through the dedicated
    :func:`_resolve_orchestrator_agent`. Naming it here is a 404.
    """
    from app.models.agent import Agent
    from app.services.autonomous_pentest import DEFAULT_ORCHESTRATOR_SLUG

    if slug:
        result = await db.execute(
            select(Agent).where(Agent.slug == slug, Agent.enabled.is_(True))
        )
        agent = result.scalar_one_or_none()
        # A precise request: return that agent (generation agents are
        # ``disable_chat`` by design) or fail loudly. The run-only orchestrator
        # is the sole exception - this endpoint must never open a chat with it.
        if agent is not None and agent.slug != DEFAULT_ORCHESTRATOR_SLUG:
            return agent
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{slug}' is not available for platform chat",
        )

    result = await db.execute(
        select(Agent)
        .where(
            Agent.enabled.is_(True),
            Agent.is_company_managed.is_(True),
            Agent.disable_chat.isnot(True),
        )
        .order_by(Agent.created_at)
        .limit(1)
    )
    agent = result.scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="No chat-enabled agent available")
    return agent


# ── Autonomous attack-path orchestration (OpenAEV-driven) ────────────────────


class AutonomousRunStartRequest(BaseModel):
    """Request from OpenAEV to start a durable autonomous attack-path run.

    OpenAEV creates the ``AutonomousRun`` + chained simulation, then fires this
    (JWT-authenticated as the operator) to engage the XTM One orchestrator. The
    call is a short, fire-and-forget enqueue: the orchestrator then drives OpenAEV
    back through its tools + the run callback endpoints.
    """

    objective: str
    openaev_run_id: str
    simulation_id: str | None = None
    # Author-scenario (AI planning) mode: the orchestrator DESIGNS a reusable attack path by
    # authoring steps directly onto the scenario's workflow TEMPLATE - there is no simulation to
    # execute against. OpenAEV sets ``author_scenario=True`` with ``simulation_id=None`` and the
    # scenario id here; the OpenAEV run-coordination tools then route the authored steps onto the
    # scenario. A normal run (live or launched-from-scenario) leaves ``author_scenario=False`` and
    # carries a ``simulation_id``. ``scenario_id`` is also present for a launched-from-scenario live
    # run (as the seed origin), so it is the pairing of ``author_scenario`` + a null
    # ``simulation_id`` - not the mere presence of ``scenario_id`` - that means "author, don't run".
    scenario_id: str | None = None
    author_scenario: bool = False
    scope_asset_group_id: str | None = None
    # Optional pre-selected audience (a TEAM id) for identity-targeted objectives
    # (phishing, human credential harvesting). An OpenAEV inject can only be delivered
    # to a team, never to a bare person, so this is the wrapper the human techniques target.
    scope_team_id: str | None = None
    # Authoritative mixed scope: a list of {"type": ASSETS|ASSETS_GROUPS|TEAMS|PLAYERS,
    # "id": "..."} entries. The orchestrator attacks within this perimeter, using whichever
    # targets the technique needs (hosts for technical steps, teams/persons for human ones).
    scope: list[dict] | None = None
    # 'environment' (whole authorized scope, no target choice) or 'target' (needs a
    # specific target the operator pre-selected or the orchestrator must ask for).
    # None for a free-text objective, where the orchestrator classifies it itself.
    scope_mode: str | None = None
    agent_slug: str | None = None
    # Build mode: when true the orchestrator only DESIGNS the attack path (scope, authored
    # steps, decisions) and never executes anything - no evaluate, no result verification.
    # OpenAEV never starts the simulation's RUN workflow while building, so there is nothing
    # to dispatch; the orchestrator sets the run to PLANNED when the logic is complete.
    plan_mode: bool = False
    # For a live run seeded with previously built logic: the plan summary to follow while
    # adapting to live findings ("follow this plan but adjust in real time").
    prior_plan: str | None = None
    # OpenAEV base URL the orchestrator (and the dead-letter reporter) calls back.
    callback_base_url: str | None = None
    # Additional XTM One agent ids the orchestrator may CONSULT during this run
    # (payload creation, code generation, recon, exploitation support). Unioned in
    # memory onto the seeded default (payload creator) at each cycle; never persisted
    # onto the shared built-in orchestrator row. Empty/None -> seeded default only.
    handover_agent_ids: list[str] | None = None

    # Per-agent discovery mode (agent id -> EXISTING_ONLY / SCOPED / EXPANSIVE): how much
    # latitude each agent has to bring newly discovered entities into the attack path. OpenAEV
    # is the enforcement authority (it gates asset/team creation at its run-scoped choke points
    # keyed to the acting agent); XTM One uses this to inform the orchestrator of each specialist's
    # latitude and to funnel create tools. An agent absent from the map falls back to SCOPED.
    handover_agent_modes: dict[str, str] | None = None

    @model_validator(mode="after")
    def _normalize_author_scenario_mode(self) -> AutonomousRunStartRequest:
        """Enforce the author-scenario invariants at the request boundary.

        OpenAEV does not have three independent flags: it DERIVES author mode from the other two
        (``authorScenario = planMode && !hasText(simulationId)`` in ``AutonomousRunService``), so
        ``author_scenario=True`` there always means "plan mode, no simulation". This API accepts the
        three fields independently, so it re-establishes that same shape rather than assuming it.

        1. Author-scenario mode has no simulation, so the scenario IS the target. Without a
           ``scenario_id`` the orchestrator would run with no target context at all (prompts
           degrade to ``Target: SCENARIO ``) and the authored steps would have nowhere to land.
           Reject that combination rather than starting a run that can never converge.

        2. Conversely, author-scenario mode is defined by the ABSENCE of a simulation - the authored
           steps land on the scenario's workflow template, not on a simulation. A request carrying
           both is self-contradictory: ``_build_cycle_prompt`` resolves author mode as
           ``author_scenario and not simulation_id``, so it would silently drop the author framing
           and drive a build against a real simulation. Reject it instead of running that hybrid.

        3. Author-scenario is a *kind* of build: it only DESIGNS the path and never executes, so it
           is meaningless without ``plan_mode``, and OpenAEV's own derivation guarantees the two
           travel together. But every downstream branch keys off ``plan_mode`` alone - the
           engagement deadline (``engagement_deadline_seconds``), the "do not execute" cycle
           instruction, and the settle-to-PLANNED backstop - so an
           ``author_scenario=True, plan_mode=False`` request would get the 7-day LIVE deadline and
           settle to COMPLETED instead of PLANNED. Coerce ``plan_mode`` on so author mode can never
           diverge from build semantics, whatever the caller sent.
        """
        if self.author_scenario:
            if not (self.scenario_id or "").strip():
                raise ValueError("scenario_id is required when author_scenario is true")
            if (self.simulation_id or "").strip():
                raise ValueError(
                    "simulation_id must not be set when author_scenario is true "
                    "(author mode designs a scenario and has no simulation)"
                )
            self.plan_mode = True
        return self


class AutonomousRunStartResponse(BaseModel):
    session_id: str
    agent_slug: str


class AutonomousRunWakeRequest(BaseModel):
    """OpenAEV -> XTM One wake for a parked run (event-driven resume).

    Fired when a relevant expectation/finding is filled (e.g. phishing
    credentials arrived) or an operator directive was queued, so a run parked on
    a slow ``await_finding`` / ``waiting_input`` wakes the instant the result
    lands instead of only at the scheduled re-check.
    """

    reason: str | None = None


async def _require_autonomous_licensing(db: AsyncSession) -> None:
    """License gate for the autonomous attack path (403 when unlicensed).

    The autonomous attack path is an OpenAEV Enterprise Edition feature that is
    merely *driven* through XTM One: OpenAEV already enforces its own EE +
    ``INJECT_CHAINING`` gate (autonomy is a launch mode of chained scenarios, so
    it shares the chaining flag) before it ever calls these endpoints. The gate
    here is the ONE licence check every Enterprise feature shares
    (``ee_features_enabled`` == ``is_agentic_ai_licensed``): a connected EE
    platform or a Filigran license unlocks it; only Community Edition XTM One
    deployments are locked out. In Copilot mode the platform license already
    gates app entry, so nothing extra is required.
    """
    if settings.is_copilot_like:
        return
    if not await ee_features_enabled(db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "The autonomous attack path requires a connected Enterprise "
                "Edition platform (or a Filigran license)"
            ),
        )


def _execution_payload(execution) -> dict | None:
    """The run payload of an autonomous execution, ``None`` when there is none."""
    if execution is None or not isinstance(execution.payload, dict):
        return None
    return execution.payload


async def _audit_refused_autonomous_run_action(
    db: AsyncSession,
    *,
    user: User,
    request: Request,
    execution_id: uuid.UUID,
    openaev_run_id: str,
    operation: str,
    purge: bool | None = None,
) -> None:
    """Record a refused attempt to act on an autonomous run that exists.

    Only attempts against an existing run are recorded: that is somebody else's
    engagement, while a refusal for a run that never ran here is the benign
    no-op a platform sends for a run it never engaged. Committed at once - the
    caller answers without writing anything else.
    """
    from app.services.audit import log_audit

    run_label = openaev_run_id[:200]
    details: dict = {
        "source": "autonomous_run",
        "operation": operation,
        "openaev_run_id": run_label,
        "credential": getattr(user, "auth_credential_kind", None),
    }
    if purge is not None:
        details["purge"] = purge
    if getattr(user, "platform_identifier", None):
        details["platform"] = user.platform_identifier
    if getattr(user, "platform_issuer", None):
        details["platform_issuer"] = user.platform_issuer
    if getattr(user, "impersonated_by", None):
        details["impersonated_by"] = str(user.impersonated_by)
    # The refused caller gets the unknown-run answer and a platform ignores it,
    # so this line is what makes a misrouted platform call diagnosable.
    logger.warning(
        "Refused %s of autonomous run %r (execution %s): user %s may not manage it "
        "(credential=%s, platform=%s, issuer=%s)",
        operation,
        run_label,
        execution_id,
        getattr(user, "id", None),
        details["credential"],
        details.get("platform"),
        details.get("platform_issuer"),
    )
    await log_audit(
        db,
        user=user,
        action="security_alert",
        entity_type="security",
        entity_id=execution_id,
        entity_name=f"autonomous_run:{run_label}",
        summary=f"Refused {operation} of an autonomous run the caller may not manage",
        details=details,
        request=request,
    )
    await db.commit()


@router.post("/autonomous/runs", response_model=AutonomousRunStartResponse)
async def start_autonomous_run(
    body: AutonomousRunStartRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Enqueue a durable autonomous attack-path orchestration run.

    Creates one ``OrchestrationExecution`` (adapter ``autonomous_pentest``,
    deduped on the OpenAEV run id) whose cycles drive the orchestrator agent. The
    engagement deadline bounds the whole run as the ultimate safety net; each
    slow finding is parked cheaply between cycles (see the adapter). Returns the
    execution id as ``session_id`` so OpenAEV can persist it for reconnection.

    Re-engaging a run that already exists resets it and takes over its recorded
    starter, so it is held to the rule of cancel and purge
    (:func:`caller_may_manage_autonomous_run`): a caller not entitled to
    that run gets a 404 and the run is left untouched. Starting a run id that
    never ran here is unchanged.
    """
    from datetime import timedelta

    from app.services import orchestration_engine as engine
    from app.services.autonomous_pentest import (
        DEFAULT_ORCHESTRATOR_SLUG,
        caller_may_manage_autonomous_run,
        engagement_deadline_seconds,
    )
    from app.services.orchestration_adapters.autonomous import (
        ADAPTER_NAME,
        execution_dedup_key,
    )

    await _require_autonomous_licensing(db)

    objective = (body.objective or "").strip()
    if not objective:
        raise HTTPException(status_code=400, detail="Objective cannot be empty")
    if not (body.openaev_run_id or "").strip():
        raise HTTPException(status_code=400, detail="openaev_run_id is required")

    # ``callback_base_url`` becomes BOTH the audience (``aud``) of the
    # cross-platform JWTs we mint (``issue_xtm_jwt``) and the target of every
    # outbound call back to OpenAEV (status/scope reads, dead-letter reporting),
    # so it must be a clean http(s) origin. Normalize + validate it through the
    # same helper that builds the JWT ``iss`` - it rejects non-http(s) schemes
    # and a missing host and strips any query/fragment, userinfo and default
    # port / trailing slash - so a malformed or hostile value can never be
    # signed as an ``aud`` or used verbatim as a request target. This is the
    # single write site for the run payload (create AND reset re-use this dict),
    # so sanitizing here keeps every downstream reader safe.
    callback_base_url: str | None = None
    if (body.callback_base_url or "").strip():
        from app.core.xtm_auth import _normalize_url

        try:
            callback_base_url = _normalize_url(body.callback_base_url)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"callback_base_url is not a valid http(s) URL: {exc}",
            ) from exc

    # An autonomous run MUST be driven by the dedicated attack-path orchestrator
    # (its persona + the OpenAEV run-coordination tools are what produce reasoning,
    # decisions and proof events). OpenAEV forwards no slug for the default path, so
    # fall back to the orchestrator here rather than to the generic chat agent -
    # otherwise the run is handed to xtm-one-assistant, which has no OpenAEV tools
    # and silently produces no timeline activity.
    agent = await _resolve_orchestrator_agent(
        db, body.agent_slug or DEFAULT_ORCHESTRATOR_SLUG
    )

    now = datetime.now(UTC)
    # A build pass (plan mode) only designs the path and normally finishes in minutes, so cap its
    # absolute engagement deadline hard at 1h as a stuck-loop safety net; a live run keeps the long
    # multi-day dead-letter window (human-timescale steps legitimately span days). This absolute
    # deadline bounds the run even while it is parked (e.g. on waiting_input), so a build cannot
    # linger for days waiting on an operator either.
    engagement_deadline = now + timedelta(
        seconds=engagement_deadline_seconds(body.plan_mode)
    )
    payload = {
        "openaev_run_id": body.openaev_run_id,
        "simulation_id": body.simulation_id,
        "scenario_id": body.scenario_id,
        "author_scenario": bool(body.author_scenario),
        "scope_asset_group_id": body.scope_asset_group_id,
        "scope_team_id": body.scope_team_id,
        "scope": body.scope,
        "scope_mode": body.scope_mode,
        "plan_mode": bool(body.plan_mode),
        "prior_plan": body.prior_plan,
        "objective": objective,
        "callback_base_url": callback_base_url,
        "agent_slug": agent.slug,
        "user_id": str(user.id),
        # The registered platform acting for the operator, when the start came in
        # over a cross-platform JWT (None for a direct session/API-key start).
        # Read back by every route that acts on the run to entitle the SAME
        # platform, for any of its authorized operators.
        "started_by_platform": getattr(user, "platform_identifier", None),
        "started_by_issuer": getattr(user, "platform_issuer", None),
        "engagement_deadline": engagement_deadline.isoformat(),
        "handover_agent_ids": body.handover_agent_ids or [],
        "handover_agent_modes": body.handover_agent_modes or {},
    }
    # Start is idempotent AND re-engaging: an autonomous run's dedup key is the
    # (stable) OpenAEV run id, so a RESTART / RESUME re-hits the same key. A plain
    # create would ``ON CONFLICT DO NOTHING`` and hand back the STALE prior
    # execution (terminal after a stop, or still parked) instead of starting
    # clean, so re-engaging a run would silently do nothing. When the execution
    # already exists we hard-reset it to a fresh engagement; otherwise we create.
    dedup_key = execution_dedup_key(body.openaev_run_id)
    existing = await engine.get_execution_by_dedup_key(db, dedup_key, for_update=True)
    if existing is not None:
        if not caller_may_manage_autonomous_run(
            user=user, payload=_execution_payload(existing)
        ):
            await _audit_refused_autonomous_run_action(
                db,
                user=user,
                request=request,
                execution_id=existing.id,
                openaev_run_id=body.openaev_run_id,
                operation="re-engage",
            )
            raise HTTPException(status_code=404, detail="Autonomous run not found")
        execution = await engine.reset_execution(
            db,
            execution_id=existing.id,
            payload=payload,
            deadline_at=engagement_deadline,
        )
    else:
        execution = None
    if execution is None:
        execution = await engine.create_execution(
            db,
            adapter=ADAPTER_NAME,
            payload=payload,
            deadline_at=engagement_deadline,
            dedup_key=dedup_key,
        )
    await db.commit()
    await engine.notify_orchestration_wake()

    return AutonomousRunStartResponse(
        session_id=str(execution.id), agent_slug=agent.slug
    )


@router.post("/autonomous/runs/{openaev_run_id}/wake", status_code=202)
async def wake_autonomous_run(
    openaev_run_id: str,
    body: AutonomousRunWakeRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Wake a parked autonomous run (event-driven resume from OpenAEV).

    Emits a ``finding_arrived`` outbox event against the run's execution so the
    adapter re-arms it for the next decision cycle immediately, instead of waiting
    for the scheduled re-check. Idempotent and best-effort: a missing/terminal
    execution is a no-op (the scheduled resume / deadline sweep is the backstop).

    Held to the rule of cancel and purge: a caller not entitled to the
    run gets the unknown-run answer and nothing is emitted, so the route neither
    tells a stranger that a run exists nor lets them spend its owner's cycles.
    """
    from app.services import orchestration_engine as engine
    from app.services.autonomous_pentest import caller_may_manage_autonomous_run
    from app.services.orchestration_adapters.autonomous import execution_dedup_key

    await _require_autonomous_licensing(db)

    # Locked through the decision and the event insert: a re-engage either
    # lands first and is what the decision reads, or waits and then
    # neutralizes the event with the rest of the prior engagement's.
    execution = await engine.get_execution_by_dedup_key(
        db, execution_dedup_key(openaev_run_id), for_update=True
    )
    if execution is not None and not caller_may_manage_autonomous_run(
        user=user, payload=_execution_payload(execution)
    ):
        await _audit_refused_autonomous_run_action(
            db,
            user=user,
            request=request,
            execution_id=execution.id,
            openaev_run_id=openaev_run_id,
            operation="wake",
        )
        return {"status": "ignored", "openaev_run_id": openaev_run_id}
    if execution is None or execution.state in {
        "completed",
        "failed",
        "cancelled",
        "dead_letter",
    }:
        # The session only commits after the response is sent: release the lock now.
        await db.rollback()
        return {"status": "ignored", "openaev_run_id": openaev_run_id}

    # Unique per wake so successive findings each re-arm (the rearm itself is a
    # no-op when the run is already pending/claimed, so duplicates are harmless).
    await engine.emit_outbox_event(
        db,
        execution_id=execution.id,
        event_type="finding_arrived",
        dedup_key=f"finding-arrived:{execution.id}:{uuid.uuid4()}",
        payload={"reason": (body.reason or "finding arrived")[:500]},
    )
    await db.commit()
    await engine.notify_orchestration_wake()
    return {"status": "woken", "openaev_run_id": openaev_run_id}


@router.post("/autonomous/runs/{openaev_run_id}/cancel", status_code=202)
async def cancel_autonomous_run(
    openaev_run_id: str,
    request: Request,
    purge: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Terminally stop a running / parked autonomous run's orchestration.

    OpenAEV calls this when the operator stops, pauses, restarts, or deletes an
    autonomous run so the orchestrator loop halts on the XTM One side too -
    otherwise the durable execution keeps self-resuming every few seconds (and,
    on a deleted run, keeps dispatching injects against a vanished simulation:
    the exact "still creating atomic tests after I deleted everything" bug this
    fixes).

    ``purge`` also drops the run's coordination state (SharedState + WorkItems).
    OpenAEV sets it for stop / restart / delete so a subsequent restart starts
    from a clean slate and re-asks the operator instead of re-reading a stale
    resolved target or an open exploitation task. Pause leaves it ``False`` so a
    resume continues where the run left off.

    **Only a caller entitled to the run may cancel or purge it**: the
    user who started it, an XTM One admin, or the registered platform that
    started it acting for an operator (OpenAEV's normal per-user cross-platform
    JWT). Anyone else - a plain signed-in user who merely knows or guesses the
    ``openaev_run_id``, or another registered platform - is turned away with the
    SAME answer an unknown run yields (no existence oracle) and neither the
    cancel nor the purge runs, so a stranger can never stop or wipe another
    operator's engagement (cancel is terminal, and a purge also clears the shared
    orchestrator's cross-run memory). See :func:`caller_may_manage_autonomous_run`.

    Cancelling is terminal (there is no ``cancelled -> pending`` transition), so
    even a run parked on a long ``await_finding`` stops immediately and a
    scheduled self-resume can never revive it; the autonomous adapter also
    no-ops resume events once the execution is terminal. Idempotent and
    best-effort for an entitled caller: an already-terminal execution is a no-op
    cancel (its purge still runs). A missing execution is a no-op for everyone;
    executions are never deleted, so it is a run that never ran here, whose purge
    only an admin may ask for.
    """
    from app.services import orchestration_engine as engine
    from app.services.autonomous_pentest import (
        DEFAULT_ORCHESTRATOR_SLUG,
        caller_may_manage_autonomous_run,
        purge_autonomous_run_state,
    )
    from app.services.orchestration_adapters.autonomous import execution_dedup_key

    await _require_autonomous_licensing(db)

    # Locked, so a re-engage cannot change who the run belongs to between the
    # decision below and the cancel it allows.
    execution = await engine.get_execution_by_dedup_key(
        db, execution_dedup_key(openaev_run_id), for_update=True
    )
    exec_payload = _execution_payload(execution)
    if not caller_may_manage_autonomous_run(user=user, payload=exec_payload):
        if execution is not None:
            await _audit_refused_autonomous_run_action(
                db,
                user=user,
                request=request,
                execution_id=execution.id,
                openaev_run_id=openaev_run_id,
                operation="purge" if purge else "cancel",
                purge=purge,
            )
        if purge:
            return {
                "status": "purged",
                "openaev_run_id": openaev_run_id,
                "purged": True,
            }
        return {"status": "ignored", "openaev_run_id": openaev_run_id}

    if execution is None and not purge:
        return {"status": "ignored", "openaev_run_id": openaev_run_id}
    if execution is not None:
        await engine.cancel_execution(db, execution_id=execution.id)
    if purge:
        # Resolve the orchestrator agent so the purge also clears its cross-run memory
        # (persistent + Redis tiers). Prefer the slug the run actually used; fall back to
        # the default orchestrator so a purge still works when the execution is already gone.
        slug = None
        if exec_payload is not None:
            slug = exec_payload.get("agent_slug")
        agent_id = None
        try:
            agent = await _resolve_orchestrator_agent(
                db, slug or DEFAULT_ORCHESTRATOR_SLUG
            )
            agent_id = str(agent.id)
        except HTTPException:
            # No resolvable orchestrator (e.g. agent removed): still purge the run-scoped
            # rows; the memory clear is best-effort and simply skipped.
            agent_id = None
        await purge_autonomous_run_state(
            db, openaev_run_id, orchestrator_agent_id=agent_id
        )
    await db.commit()
    return {
        "status": "cancelled" if execution is not None else "purged",
        "openaev_run_id": openaev_run_id,
        "purged": purge,
    }
