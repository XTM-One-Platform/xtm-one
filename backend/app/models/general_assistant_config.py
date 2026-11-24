# SPDX-License-Identifier: Apache-2.0

"""GeneralAssistantConfig model – platform-wide general assistant settings.

Singleton table (one row).  Admins configure the LLM model, system prompt,
and resource bindings (KBs, MCPs, tools, integrations, handover agents) used
by the General Assistant (the default chat when no specific agent is bound).
Users can override the model in their profile but not the system prompt.

By default the General Assistant has **no** resources bound — it answers
questions directly using only the LLM.  Admins must explicitly bind
company-managed resources for the GA to use them.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text, false
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

# Re-exported so the settings API and the admin UI clamp wait bounds against the
# same numbers the runtime enforces.  ``app.tools.wait_policy`` is the single
# source of truth and imports nothing from the model layer (see its docstring).
from app.tools.wait_policy import (  # noqa: F401  (re-exported for the API layer)
    DEFAULT_MAXIMUM_TOTAL_WAIT_TIME,
    DEFAULT_MAXIMUM_WAIT_TIME,
    MAXIMUM_TOTAL_WAIT_TIME_CEILING,
    MAXIMUM_WAIT_TIME_CEILING,
    clamp_maximum_total_wait_time,
    clamp_maximum_wait_time,
)

# Built-in tools a brand-new General Assistant ships with, whatever the
# platform mode. ``generate_file`` (downloadable artifacts) has been ON by
# default since the file-output surface landed; ``generate_presentation`` and
# ``template_search`` join it so the seeded Filigran brand deck is
# reachable from the Copilot General Assistant out of the box — before, a
# Copilot deployment had the tool catalog entry but no way to turn it on short
# of an admin editing Settings -> General Assistant. ``agent_builder`` and
# ``web_fetch`` join it: a person's first request is often "create an
# agent that ...", and the platform builder acts as that person, through the
# REST API's own permission checks. ``video_studio`` joins so every
# person can record a demo and produce a video from the General Assistant,
# ``custom_dashboards`` so the Dashboard Studio has an assistant out of the
# box (its tool is built only while ``DASHBOARD_STUDIO`` is on),
# and ``generate_report`` too, so it writes the report the default
# assistants write. ``platform_insights`` joins as well; ``general_chat`` wires
# it for administrators only (``ADMIN_ONLY_GA_BUILTIN_TOOL_IDS``).
# The single source of truth for EVERY
# ``GeneralAssistantConfig(...)`` construction site (settings API and the
# env-driven boot seed in ``main.py``); admins can remove any of them later.
# A tool added here reaches the platforms whose row already exists only
# through a migration (``g5209gadefault``, ``v5774gadflt02``, ``r5931gareport``,
# ``g6258gainsights``, ``f7030fleetsga``, ``q8317gadflt``, ``s1925gadash01``).
# It is THE default set of both default assistants: the CTEM
# Assistant seed (``builtin_agents.py``, the General Assistant in XTM One
# mode) carries exactly these ids, pinned by
# ``tests/test_default_assistant_set.py``. The sandbox (``code_interpreter``)
# and the CTI toolkit (``stix_bundle_validator`` + ``fimi_analytics``, the
# ``cti`` group) join the set the consolidation assessment puts on by default.
# The platform group (``agent_builder`` + ``fleets``) stays on, its
# administrator-only functions wired for an administrator only.
DEFAULT_GA_BUILTIN_TOOL_IDS: tuple[str, ...] = (
    "generate_file",
    "generate_presentation",
    "template_search",
    "agent_builder",
    "web_fetch",
    "video_studio",
    "custom_dashboards",
    "generate_report",
    "platform_insights",
    "fleets",
    "code_interpreter",
    "stix_bundle_validator",
    "fimi_analytics",
)

# The General Assistant suites a turn carries only for an administrator (the
# role): the platform's own analytics, objects and audit trail. An agent
# that carries the suite serves a non-admin their own slice instead.
ADMIN_ONLY_GA_BUILTIN_TOOL_IDS: frozenset[str] = frozenset({"platform_insights"})

# Of those, the ones a group manager's turn keeps too: the suite reads
# their teams' LLM analytics in its ``team`` scope (``platform_insights.scope``).
GROUP_MANAGER_GA_BUILTIN_TOOL_IDS: frozenset[str] = frozenset({"platform_insights"})


def general_assistant_turn_tools(
    builtin_tool_ids: list[str], *, is_admin: bool, is_group_manager: bool = False
) -> list[str]:
    """The built-in ids a General Assistant turn wires for its user: the
    administrator-only suites (``ADMIN_ONLY_GA_BUILTIN_TOOL_IDS``) dropped for
    anyone else - but a group manager's - whatever list the turn came with."""
    if is_admin:
        return list(builtin_tool_ids)
    kept = GROUP_MANAGER_GA_BUILTIN_TOOL_IDS if is_group_manager else frozenset()
    return [
        tool_id
        for tool_id in builtin_tool_ids
        if tool_id not in ADMIN_ONLY_GA_BUILTIN_TOOL_IDS or tool_id in kept
    ]

# Every built-in suite ``services/general_chat.py`` wires for the General
# Assistant. The agent-scoped suites (work items, shared state, workset, trace
# inspection) are absent on purpose: what they read and write is keyed to an
# agent, and the General Assistant has none. The settings API refuses
# to add anything else, and the registry (``BUILTIN_TOOL_GROUPS`` in
# ``services/builtin_tool_groups.py``) offers exactly these; the three are pinned
# together by ``tests/test_general_assistant_tools.py``.
GENERAL_ASSISTANT_BUILTIN_TOOL_IDS: tuple[str, ...] = (
    "agent_builder",
    "flow_builder",
    "generate_file",
    "generate_image",
    "generate_presentation",
    "generate_report",
    "custom_dashboards",
    "phone",
    "video_studio",
    "template_search",
    "template_authoring",
    "document_vision",
    "code_interpreter",
    "web_fetch",
    "browser",
    "computer_use",
    "stix_bundle_validator",
    "fimi_analytics",
    "platform_insights",
    "fleets",
    # ROI and outcomes (#10006 S2), an alias of the platform group.
    "roi",
)

# The same suites as the registry offers them: a former id merged
# into a group is listed under its group. ``general_chat`` expands a group
# back into the ids above (``runtime_builtin_tool_ids``), so a row naming
# either form runs the same tools; the settings API accepts both.
GENERAL_ASSISTANT_BUILTIN_GROUP_IDS: tuple[str, ...] = (
    # ``agent_builder``, ``fleets``, ``flow_builder`` and ``roi``.
    "platform",
    "files",
    "generate_presentation",
    "generate_report",
    "custom_dashboards",
    "phone",
    "video_studio",
    "design",
    "code_interpreter",
    "web",
    "computer_use",
    "cti",
    "platform_insights",
)

# Handover types the General Assistant implements, one tool each:
# ``spawn_background_task``, ``consult_agent``, ``transfer_to_agent``.
# ``mandatory_transfer`` is routed after an agent's answer by ``agent_chat``, a
# path the General Assistant never takes.
GENERAL_ASSISTANT_HANDOVER_TYPES: tuple[str, ...] = ("delegate", "consult", "transfer")


def general_assistant_handover_type(value: object) -> str:
    """The handover type the General Assistant runs for a stored value.

    A type it does not implement - a ``mandatory_transfer`` saved before the
    settings API refused one - runs as ``delegate``, the default type: such a
    target was already announced to the model with the delegate instructions,
    while the tool those instructions name was never registered.
    """
    if isinstance(value, str) and value in GENERAL_ASSISTANT_HANDOVER_TYPES:
        return value
    return "delegate"

# Platform-wide default for max agentic loop iterations — used by chat (the
# General Assistant and the per-agent fallback for agents without their own
# override) AND as the final fallback for assignments: an assignment with
# max_iterations = NULL inherits the agent's override, then this platform
# default (see assignment_engine.resolve_effective_max_iterations).
# This is a CEILING, not a target: a simple chat still stops after 1-2 rounds,
# so a generous default only lets genuinely long multi-tool tasks finish instead
# of being cut off mid-flight — it does not make simple chats more expensive.
# Each iteration is one LLM<->tool round and can fire several parallel tool
# calls, so the effective tool-call budget is higher than this.
DEFAULT_MAX_ITERATIONS = 300

# Upper bound an admin / agent owner may configure for ``max_iterations`` (the
# slider max in Settings -> General Assistant and per-agent config). Enforced
# server-side on both the GA save path and the per-agent save paths so the API
# and the UI agree on the ceiling; raising it mainly lets power users push
# genuinely long multi-tool tasks further. Also the most iterations a run
# that keeps extending its budget while it makes progress may reach
# (:func:`iteration_reach`).
MAX_ITERATIONS_CEILING = 1000


def clamp_max_iterations(value: int) -> int:
    """Clamp a configured ``max_iterations`` into ``[1, MAX_ITERATIONS_CEILING]``.

    Shared by every server-side write path so the API enforces the same ceiling
    the UI slider advertises (``max=MAX_ITERATIONS_CEILING``) instead of silently snapping values
    back: the General Assistant save path (``settings.py``), the per-agent save
    paths (``agents.py``), the assignment create/update paths
    (``assignments.py``), the agent import / duplicate paths (``agents.py``),
    and the agent-builder chat tool. Callers handle ``None`` (= inherit /
    platform default) before calling.
    """
    return max(1, min(value, MAX_ITERATIONS_CEILING))


# Wall-clock budget for one interactive chat turn (and for a background
# sub-agent task spawned from chat, which runs the same agentic loop).
# 120 s/iter keeps a 100-iteration specialist at 12 000 s. The 1800 s
# floor is what a low-iteration specialist actually needs:
# ``generate_file`` + ``code_interpreter`` + a Notion write routinely
# take more than the historical 600 s floor, which killed the turn
# before the forced-answer synthesis could run and surfaced the
# "wasn't able to finish composing" digest instead. Assignment runs
# keep their own ``_MIN_ASSIGNMENT_TIMEOUT_SECONDS`` (600 s) — they
# have an explicit ``timeout_seconds`` the owner can raise.
CHAT_SECS_PER_ITERATION = 120
CHAT_MIN_LOOP_TIMEOUT_SECONDS = 1800
# A specialist with ``max_iterations=5`` strips tools at iteration 3
# (``_wind_down_points``). ``generate_file`` + ``code_interpreter`` + a
# Notion write is already three tool rounds, so the forced-answer
# backstop fires before the specialist can file, verify, or compose.
# The 1800 s timeout floor is wasted if the iteration cap is
# still 5. Assignment pins keep their explicit cap; an evaluated agent gets
# the floor, as chat runs it.
CHAT_MIN_LOOP_ITERATIONS = 15


def chat_loop_max_iterations(value: int) -> int:
    """Clamp then floor the iteration cap for a chat / chat-spawned run."""
    return max(CHAT_MIN_LOOP_ITERATIONS, clamp_max_iterations(int(value)))


def configured_max_iterations(
    agent_max_iterations: int | None,
    platform_max_iterations: int,
    evaluation_overrides: dict | None = None,
) -> int:
    """A turn's iteration cap before the chat floor: the replayed evaluation
    snapshot's when it holds the key (the platform's if that value is not an
    ``int``, ``True`` included), else the agent's, else the platform's.

    The one reading ``run_agent_chat`` and the reuse fingerprint share.
    """
    if isinstance(evaluation_overrides, dict) and "max_iterations" in evaluation_overrides:
        replayed = evaluation_overrides["max_iterations"]
        if isinstance(replayed, int) and not isinstance(replayed, bool):
            return replayed
        return platform_max_iterations
    if agent_max_iterations is not None:
        return agent_max_iterations
    return platform_max_iterations


def chat_loop_timeout_seconds(max_iterations: int) -> int:
    """Return the wall-clock budget (seconds) for one chat / chat-spawned run.

    ``max_iterations`` is the already-resolved, clamped (and, for chat,
    floored) iteration cap.
    """
    return max(
        CHAT_MIN_LOOP_TIMEOUT_SECONDS,
        int(max_iterations) * CHAT_SECS_PER_ITERATION,
    )


def iteration_reach(max_iterations: int) -> int:
    """The iterations a run capped at ``max_iterations`` may reach.

    With ``AGENT_LOOP_AUTO_EXTEND_ITERATIONS`` (the default), a run still making
    progress at its cap gets another block of the same size, up to
    :data:`MAX_ITERATIONS_CEILING` (``run_agent_loop(extend_iterations_to=)``),
    and its wall clock is sized for that reach. Otherwise the cap is the reach.
    """
    from app.core.config import settings

    if not settings.agent_loop_auto_extend_iterations:
        return int(max_iterations)
    return max(int(max_iterations), MAX_ITERATIONS_CEILING)


def default_system_prompt() -> str:
    from app.services.branding import get_cached_branding

    name = get_cached_branding().name
    return (
        f"You are {name}, a corporate AI assistant. "
        "Help with tasks, communications, information retrieval, and tool orchestration. "
        "Be concise, professional, and action-oriented. "
        "Always try to source your findings with associated links when available."
    )


DEFAULT_HANDOVER_PROMPT = (
    "Use `transfer_to_agent` to hand the conversation to a specialized agent "
    "when a request clearly fits their expertise. Provide the agent's ID and a "
    "brief reason. For general questions, answer directly."
)


class GeneralAssistantConfig(Base):
    __tablename__ = "general_assistant_config"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # LLM model for the general assistant's own chat (admin-configured)
    llm_provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    llm_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Which provider config serves ``llm_model``. A model id is not unique across
    # configs, so without this the owner is guessed newest-row-first. No
    # FK: a dangling id is logged and falls back to ``llm_provider``, and
    # ON DELETE SET NULL would erase that evidence.
    llm_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Optional link to a specific Agent that acts as the General Assistant.
    # When set, the GA chat routes to this agent — model, persona, tools, and
    # all bindings are inherited from the linked agent.  The rest of the GA
    # config fields are ignored (but preserved for fallback if the link is removed).
    linked_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, default=None
    )

    # Whether an administrator chose ``linked_agent_id``. NULL means
    # both "never configured" and "an admin chose no agent"; this tells them
    # apart. ``provisioning.wire_default_agents`` links the CTEM Assistant only
    # while it is false; the settings API sets it on any save of the link, and
    # deleting the linked agent resets it with the link.
    linked_agent_configured: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )

    # NOTE: agent_default_provider, agent_default_model, embedding_*,
    # image_*, default_reasoning_effort have moved to PlatformSettings.
    # The old columns remain in DB for backward compatibility but are
    # no longer read from this model.

    # Platform-wide default for max agentic loop iterations.
    # Used by General Assistant chat and as fallback for agents without an override.
    # ``server_default`` is kept in lockstep with ``DEFAULT_MAX_ITERATIONS`` (see
    # the migration raising it 25 -> 300, ``i5940iter01``) so a row created via
    # the DB default matches the Python default end-to-end.
    max_iterations: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_MAX_ITERATIONS, server_default="300"
    )

    # Platform-wide bounds for the ``wait`` tool — the agent's timer.
    # ``maximum_wait_time`` caps a SINGLE pause; ``maximum_total_wait_time``
    # caps the sum across one run.  Waiting deliberately does not consume the
    # iteration budget above (a pause is not an action), so these two values
    # are what actually bound it — together with the run's wall-clock deadline.
    # Semantics, clamping and the runtime primitive live in
    # ``app.tools.wait_policy``; ``server_default`` is kept in lockstep with the
    # Python defaults there so a row created by the DB default matches
    # end-to-end.
    maximum_wait_time: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_MAXIMUM_WAIT_TIME,
        server_default=str(DEFAULT_MAXIMUM_WAIT_TIME),
    )
    maximum_total_wait_time: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=DEFAULT_MAXIMUM_TOTAL_WAIT_TIME,
        server_default=str(DEFAULT_MAXIMUM_TOTAL_WAIT_TIME),
    )

    # System prompt (admin-editable; users cannot override)
    system_prompt: Mapped[str] = mapped_column(
        Text, nullable=False, default=default_system_prompt
    )

    # --- Resource bindings (explicit, admin-managed) ---
    # Only company-managed resources should be bound here.

    # Knowledge base bindings: list of KnowledgeBase UUIDs for RAG
    knowledge_base_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # MCP server bindings: list of MCP server UUIDs
    mcp_server_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Custom tool bindings: list of CustomTool UUIDs
    custom_tool_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Built-in tool IDs enabled for the General Assistant, among
    # ``GENERAL_ASSISTANT_BUILTIN_TOOL_IDS``.
    # New rows are seeded with ``DEFAULT_GA_BUILTIN_TOOL_IDS`` by every
    # construction site (``api/v1/settings.py`` and the boot seed in
    # ``main.py``) so the platform ships file generation and the brand deck
    # ON for fresh Copilot installs; admins can edit the list at any time via
    # Settings → AI and models → General Assistant. The column-level default stays ``[]`` so
    # explicitly-cleared lists are honoured.
    builtin_tool_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Skill bindings: company-managed AgentSkill UUIDs, served as a
    # catalog with ``load_skill`` like an agent's. NULL = the built-in General
    # Assistant skills (``services/general_assistant_skills``), so a release's
    # new one reaches every assistant whose admin never chose; a list (even
    # empty) is the admin's choice.
    skill_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Integration bindings: maps provider keys → lists of Integration UUIDs
    # e.g. {"microsoft": ["550e8400-..."], "linkedin": ["660e8400-...", "770e8400-..."]}
    integration_bindings: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=dict
    )

    # --- Channel availability ---
    # Which messaging channels can route conversations to the General Assistant.
    # NULL = all channels (legacy rows).  An explicit list restricts to only
    # those channels (e.g. ["web", "slack"]).
    # New installations default to ["web"] (web chat only) — external channels
    # must be explicitly enabled.  The default is applied in the API layer
    # (settings.py) when the config row is first created, not here, so that
    # existing NULL rows keep their "all channels" semantics.
    # Valid values: "web", "slack", "teams", "whatsapp", "telegram".
    available_channel_types: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # Unified channel/bot configuration — same structure as Agent.channel_config.
    # Maps channel type → config entry:
    #   "slack" → { "channel_bot_id": "uuid", "receive_enabled": bool,
    #               "publish_enabled": bool, "receive_channels": [...],
    #               "publish_channels": [...], "allowed_tools": [...] | null }
    # When set, this supersedes available_channel_types for non-web channels.
    # NULL = no channel/bot configuration (legacy, uses available_channel_types).
    channel_config: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=None
    )

    # NOTE: disabled_integration_providers moved to PlatformSettings.

    # --- Handover configuration ---
    # List of company-managed Agent UUIDs the GA can hand over to (empty = no
    # handover). The settings API refuses to add any other agent.
    handover_agent_ids: Mapped[list | None] = mapped_column(
        JSON, nullable=True, default=list
    )

    # Per-target handover type — maps agent UUID string → type, among
    # ``GENERAL_ASSISTANT_HANDOVER_TYPES``.
    # When a target agent has no entry, it defaults to "delegate".
    handover_types: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, default=dict
    )

    # Legacy: admin-editable prompt (no longer used — prompt is auto-generated
    # from handover_types).  Kept for backward compatibility.
    handover_prompt: Mapped[str] = mapped_column(
        Text, nullable=False, default=DEFAULT_HANDOVER_PROMPT
    )

    # NOTE: mcp_exposed_agent_ids, mcp_global_api_key_*, platform_name,
    # platform_logo_url, all AI adoption fields, enterprise_license, and
    # quota_config have moved to PlatformSettings.

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


def general_assistant_builtin_tool_ids(platform_tool_ids: list[str] | None, user: object | None) -> list[str]:
    """The General Assistant's built-in tools for *user*: their profile's override, else *platform_tool_ids*."""
    override = getattr(user, "ga_builtin_tool_ids", None)
    return list(override if override is not None else platform_tool_ids or [])
