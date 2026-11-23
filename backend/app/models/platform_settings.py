# SPDX-License-Identifier: Apache-2.0

"""PlatformSettings model — platform-wide configuration singleton.

Stores settings that apply to the entire platform (not specific to the
General Assistant).  Previously these were incorrectly co-located in
``GeneralAssistantConfig``.

Fields owned by this table:
  - LLM defaults (agent default model, embedding model, image model, reasoning
    effort, routing / fallback / decision models)
  - Integration catalog control (disabled providers)
  - MCP Agent Endpoint config (exposed agents, global API key)
  - Platform branding (name, logo)
  - Enterprise license (Copilot mode)
  - Quota configuration
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class PlatformSettings(Base):
    __tablename__ = "platform_settings"
    # Enforces the singleton invariant at the DB level — a unique index on a
    # constant expression means at most one row can ever exist, regardless
    # of how many requests race the check-then-insert in
    # ``get_platform_settings()``.  Folded from ``n2s3g4l5e6t7`` so the squashed
    # baseline and ``create_all`` fresh installs carry the same index and the
    # autogenerate drift probe stays at zero operations.
    __table_args__ = (Index("ix_platform_settings_singleton", text("(true)"), unique=True),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # --- Platform-wide LLM defaults ───────────────────────────────────────
    # Runtime AI override. NULL = follow PLATFORM_MODE, and that state
    # is why this is not a plain boolean: without it the first boot pins the
    # platform and PLATFORM_MODE stops taking effect. Same convention as
    # ``platform_name``. Stored negatively to match the mode name; the wire is
    # ``ai_enabled`` both ways, inverted once in ``services/ai_state.py``.
    ai_disabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    agent_default_provider: Mapped[str | None] = mapped_column(
        String(50), nullable=True
    )
    agent_default_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    agent_default_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    default_reasoning_effort: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    embedding_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    embedding_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    embedding_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    image_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    image_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Video generation: the provider and model AI clips are made with
    # (``router.get_video_provider``); unset, the first provider with a
    # built-in video model.
    video_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    video_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    video_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Speech generation: the provider, model and voice the chat's
    # read-aloud and the Video Studio voice-over speak with
    # (``router.get_speech_provider``); unset, the first provider with a
    # built-in speech model. The voice is an id or name of that provider's.
    speech_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    speech_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    speech_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    speech_voice: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Music generation: the provider and model the Video Studio's AI
    # tracks are composed with (``router.get_music_provider``); unset, the
    # first provider with a built-in music model.
    music_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    music_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    music_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Video fallback: the models an AI shot is made on, in order, when the
    # default video model (``video_model``) fails - a provider failure or a
    # take failing a check of its frames. NULL or empty = none: every shot is
    # made on the default alone (``router.configured_video_fallbacks``).
    video_fallback_models: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # Routing model – lightweight LLM for evaluating mandatory transfer
    # routing conditions.  When NULL, falls back to the platform default model.
    routing_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    routing_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Fallback model – explicit model tried first when the primary model fails
    # (rate limit / overloaded / transient provider errors) after retries are
    # exhausted.  The owning provider is resolved from the model id.  When NULL,
    # failover falls back to other configured providers' own default models.
    fallback_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Which provider config owns ``routing_model`` / ``fallback_model``. Both were
    # resolved from the model id alone, which picks the newest config advertising
    # it — so the failover credentials flipped when an unrelated config was added.
    # No FK on either, like the ``*_provider_id`` columns above: a
    # dangling id is logged and falls back to the model-owner lookup, and
    # ON DELETE SET NULL would erase that evidence.
    fallback_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Decision model – the model ``act_on_page`` asks for each step on a host
    # page. NULL = no decision model: act_on_page is not offered. Its provider reference
    # carries no FK for the reason given on ``fallback_provider_id``.
    decision_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decision_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # --- Disabled integration providers ───────────────────────────────────
    disabled_integration_providers: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # --- MCP Agent Endpoint configuration ─────────────────────────────────
    mcp_exposed_agent_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )
    mcp_global_api_key_hash: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    mcp_global_api_key_prefix: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    # --- Emergency stop API token (services/panic.py) ─────────────────────
    # One platform-wide credential that does exactly one thing: press the
    # emergency stop from outside a session (runbook, monitoring hook).
    # Stored like the user API keys — bcrypt hash + display prefix, never
    # recoverable — which is why the procedure is printed at generation.
    emergency_stop_token_hash: Mapped[str | None] = mapped_column(
        String(200), nullable=True
    )
    emergency_stop_token_prefix: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    emergency_stop_token_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Denormalized email label (survives the user's deletion, like
    # ``AuditLog.user_email``) plus the id the token acts as when pressed.
    emergency_stop_token_created_by: Mapped[str | None] = mapped_column(
        String(320), nullable=True
    )
    emergency_stop_token_created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        # Named so ``create_all`` and the migration produce the same constraint.
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_platform_settings_emergency_stop_token_created_by_id_users",
        ),
        nullable=True,
    )
    emergency_stop_token_last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # --- Open platform-wide emergency stop (services/panic.py) ─────
    # The state of a platform stop that is in effect, kept here rather than
    # derived from the ``panic_stop`` / ``panic_resume`` audit rows: those
    # cascade with the user who pressed the stop, and the stop's effects
    # (paused assignments, AI off) must not outlive the record of it.
    # ``platform_stop_at`` is the ``stopped_at`` of the first stop of the open
    # sequence (NULL = no platform stop in effect); ``platform_stop_times`` the
    # ``stopped_at`` (ISO 8601) of every platform stop pressed while it is open,
    # i.e. the ``paused_at`` stamps the resume re-enables; and
    # ``platform_stop_ai_override`` the AI override (wire tri-state) the first
    # stop found, which the resume puts back.
    platform_stop_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    platform_stop_times: Mapped[list | None] = mapped_column(JSON, nullable=True)
    platform_stop_ai_override: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True
    )

    # Platform MCP servers (/mcp/opencti, /mcp/openaev) — global admin toggle.
    # Default ON: the servers activate as soon as a platform is registered.
    platform_mcp_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # XTM One's own MCP server (/mcp/xtm) — global admin toggle.
    # Default OFF, unlike the platform servers: this one exposes the
    # instance's own configuration surface (agents, prompts, skills,
    # knowledge bases, traces) to an external MCP client, so an upgrade must
    # not silently open it on a deployment that never asked for it.
    xtm_mcp_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Reply suggestions: once a chat turn is done, the routing model proposes
    # the user's next message as ghost text in the composer
    # (``services/chat_reply_suggestions.py``). Default ON, but nothing is
    # generated while no ``routing_model`` is set; each user may turn it off
    # (``User.reply_suggestions_enabled``).
    reply_suggestions_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Organization summary in every agent's system prompt (#11533): the
    # platform default of the cascade assignment > agent > platform that
    # ``services/organization_context_injection.py`` resolves. Default ON.
    organization_context_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    # The categories every inheriting agent preloads after the basic block
    # (#11533, ``services/organization_context_categories.py``); none by default.
    organization_context_categories: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list, server_default=text("'[]'::json")
    )

    # --- Tool approval gate ────────────────────────────────────────
    # Platform-wide kill switch for the per-tool human approval gate.
    # Off means no tool call pauses for anyone; every call is still recorded
    # with an explicit ``auto_run_approvals_disabled`` outcome. Deliberately
    # distinct from the agent-level ``human_in_the_loop`` switch and from the
    # ``request_human_input`` tool, which stay available either way.
    tool_approvals_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # What an agent with no ``approve_tool_calls`` of its own does.
    # Opt-in: FALSE on every install, fresh or upgraded, until an administrator
    # turns it on (``o1ptin0173``). A row an earlier release created TRUE keeps
    # its value.
    approve_tool_calls_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # --- Agent runs: the ``wait`` policy ─────────────────────────
    # Platform bounds of the ``wait`` primitive for EVERY run (chat, agents,
    # assignments, evaluations), moved here from ``general_assistant_config``
    # (the General Assistant is one agent among others). NULL reads as the
    # defaults of ``app.tools.wait_policy``; the values are clamped there and
    # are the ceilings of every per-agent / per-assignment ``wait_policy``
    # override. ``AGENT_MAXIMUM_WAIT_TIME`` / ``AGENT_MAXIMUM_TOTAL_WAIT_TIME``
    # win over the stored values (``services.wait_settings``).
    maximum_wait_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    maximum_total_wait_time: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # A wait naming ``until`` is not charged to the total budget (only blind
    # waits are). NULL = off.
    wait_unbounded_with_until: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Template container ``generate_presentation`` styles a deck with when the
    # agent names none. NULL means "the built-in brand deck" — the
    # Filigran brand container seeded in every mode — NOT "no template"; the
    # resolution order lives in ``presentations.resolve_default_template_id``.
    # ``SET NULL`` on delete so removing the chosen container falls back to the
    # built-in instead of dangling; the settings API only accepts a
    # company-managed presentation container, so the default is visible to
    # every user it is applied for.
    default_presentation_template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("template_containers.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Brand asset packs read from a GitHub folder at every asset seed
    # (``services/brand_asset_source.py``, Settings > Platform): the folder's
    # URL and the GitHub integration whose token reads it. NULL URL = none.
    # No foreign key: a deleted integration reads as a source that cannot
    # authenticate, which the sync reports, as a skill source does.
    brand_assets_source_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    brand_assets_source_integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # --- Platform branding ────────────────────────────────────────────────
    # All fields are admin-configurable and fully decoupled from PLATFORM_MODE.
    platform_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True, default=None
    )
    # DEPRECATED: legacy http(s) logo URL.  Kept for backward compatibility;
    # the branding resolver prefers ``platform_logo`` (base64) when set.
    platform_logo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Base64 ``data:image/...`` URL for the platform logo.
    platform_logo: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Short description (browser <meta name="description">, og:description).
    platform_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Markdown login-screen message (subtitle under the logo on the login page).
    # Supports links/formatting; rendered as Markdown by the frontend.  When
    # NULL the branding resolver falls back to the mode-aware default.
    platform_login_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # PWA / browser chrome theme color (e.g. "#0a0f1c").
    platform_theme_color: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # In-app accent/brand color (hex, e.g. "#00c8d4").
    # Overrides the --cyan / --accent / --ring CSS custom properties.
    platform_accent_color: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    # App surface/background color (hex, e.g. "#070d19").
    # Overrides the --background and related surface CSS custom properties.
    platform_background_color: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    # Base64 ``data:image/...`` URL for the favicon.
    platform_favicon: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Base64 ``data:image/...`` URL or absolute URL for the OpenGraph image.
    platform_og_image: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Platform-wide UI language. "auto" (default) = detect from the user's
    # browser. Concrete values are locale codes like "en-us", "fr-fr".
    # Users can override it via ``User.language`` (same "auto" semantics).
    platform_language: Mapped[str] = mapped_column(
        String(10), nullable=False, default="auto", server_default="auto"
    )

    # --- Enterprise license (Copilot mode) ────────────────────────────────
    enterprise_license: Mapped[str | None] = mapped_column(
        Text, nullable=True, default=None
    )

    # --- CTEM per-stage agent overrides (EE fork) ─────────────────────────
    # Vertical-aware map of CTEM stage -> custom agent UUID (string) replacing the
    # seeded ctem-{stage}-analyst for that (vertical, stage). EE admins fork a
    # seeded analyst (duplicate_agent) and point the stage here, per vertical, so a
    # FIMI deployment and a Cyber one can each drive a stage with their own agent.
    # Shape: {vertical: {stage: "<uuid>"}}, e.g.
    # {"cyber": {"validation": "<uuid>"}, "fimi": {"scoping": "<uuid>"}}.
    # The legacy flat shape {stage: "<uuid>"} is still read as a per-vertical
    # fallback (it applied to every vertical) and is migrated to the nested shape
    # on the next save. NULL/missing = use the seeded default.
    ctem_stage_agent_ids: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Vertical-aware map of CTEM vertical -> custom agent UUID (string) replacing
    # the seeded exposure-briefing-analyst for that vertical. Lets an admin assign
    # a dedicated Exposure Briefing Analyst per vertical. Shape: {vertical: "<uuid>"},
    # e.g. {"fimi": "<uuid>"}. NULL/missing = use the seeded default.
    ctem_briefing_agent_ids: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # --- Quota configuration ──────────────────────────────────────────────
    quota_config: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # --- Security options set in Settings > Access and security ──────────────
    # ``{option key: value}`` for the options of
    # ``app/core/security_options.py`` an administrator stored. NULL or a
    # missing key = the option's default. Its environment variable, when set,
    # wins over the stored value. Read through ``security_option()`` from a
    # per-process snapshot, never per request.
    security_options: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # --- Video Studio render retention ───────────────────────────
    # ``{failed_cancelled_days, previews_days, superseded_ready_days}`` an
    # administrator stored in Settings; NULL or a missing key = its default.
    # Read and written only by ``services/video_studio/render_retention.py``.
    video_render_retention: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # --- File retention ───────────────────────────────────────────
    # ``{<category>: days}`` an administrator stored in Settings > Storage &
    # retention; NULL or a missing key = the category's default (its env
    # value). Read and written only by ``services/file_retention.py``.
    file_retention: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # --- Platform Health ───────────────────────────────────────
    # Enable, supervision scope and budget caps; NULL or a missing key = its
    # default. Read and written only by ``services/health/settings.py``.
    health_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)

    # --- Terms of Use acceptance (platform-wide) ────────────────────
    # A SINGLE platform-level acceptance of the Terms of Use, replacing the
    # former per-user model. The first admin to accept clears the gate
    # for the whole platform; bumping ``TERMS_OF_USE_VERSION`` re-opens it for
    # everyone. The audit log carries the full who/when/version compliance
    # trail (``accept`` / ``decline`` on ``entity_type="terms_of_use"``); these
    # columns are the denormalised current state the request gate reads so
    # authenticating costs no extra join. ``tou_accepted_by_user_id`` is a plain
    # UUID (not an FK) so deleting the accepting admin never clears the
    # platform's acceptance.
    tou_accepted_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    tou_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    tou_accepted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # --- Native observability settings ────────────────────────────────────
    trace_storage_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    trace_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default="90"
    )
    # Days an audit log entry is kept before the hourly purge deletes it
    # (services/audit_retention.py).
    audit_log_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=90, server_default="90"
    )
    # Share of the turn model's context window a workspace's sources may fill
    # before the agent searches them instead.
    workspace_sources_context_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, default=15, server_default="15"
    )
    # Minutes a filed conversation stays idle before it is indexed into its
    # workspace's conversation search.
    workspace_conversation_idle_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30, server_default="30"
    )
    trace_store_payloads: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    last_aggregation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # How a person's department is resolved: ``{sources, directory_attribute}``,
    # NULL or a missing key = its default. Read and written only by
    # ``services/departments.py``.
    department_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True, default=None)
    # Whether every user also reads the organization-wide ROI totals on the
    # Value page, not only the agents and fleets they can see (#9836 R1,
    # dev-docs/roi-outcomes.md 10.2). Rows outside a non-administrator's scope
    # are folded into one "Other agents and fleets" row either way.
    roi_org_totals_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # k of the ROI k-anonymity rule (#12077 L5, dev-docs/roi-outcomes.md 12.5): for a
    # non-administrator, a group of a breakdown backed by fewer than k people is folded
    # into its "Other" row. 2 to 50, administrators, audited.
    roi_min_group_size: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    # Whether administrators read the "By person" breakdown (decision D12-1, default off);
    # each read is audited (action read, entity roi_by_person).
    roi_by_person_visible: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    # --- First-login organization setup proposal (#9173, S3) ──────────────
    # Proposed once per instance to the first administrator who signs in to an
    # XTM One platform whose organization profile was never saved. Answered
    # (``started``, ``later`` or ``dismissed``) it is never proposed again; the
    # setup stays reachable from Settings > Organization. NULL = not answered.
    # Read and written only by ``services/organization_setup_prompt.py``.
    organization_setup_prompted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    organization_setup_prompted_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    organization_setup_prompt_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Versioned since #1087 (CTEM-33): the setup version the answer above belongs to
    # (``ctem-loop-1``...). NULL with an answer = the unversioned #9173 proposal, so an
    # upgrade to a build carrying a setup version proposes that version once.
    setup_prompt_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # The steps of the guided flow done or skipped, for one version:
    # ``{"version": "ctem-loop-1", "steps": {"organization": "done", ...}}``.
    setup_prompt_progress: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True, default=None)

    # --- Timestamps ───────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
