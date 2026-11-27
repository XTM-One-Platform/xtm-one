# SPDX-License-Identifier: Apache-2.0

"""Pydantic schemas for settings endpoints."""

import uuid
from collections.abc import Sequence
from typing import Annotated, Any, ClassVar, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.v1.schemas.column_limits import column_length_validator
from app.api.v1.schemas.vault_references import (
    VaultSecretStatus,
    no_vault_reference_validator,
    vault_reference_validator,
)
from app.llm.model_limits import (
    MAX_CONTEXT_WINDOW_TOKENS,
    MAX_MODEL_ID_LENGTH,
    is_model_id,
    parse_token_count,
)
from app.models.user import User
from app.services.ctem_verticals import CTEM_VERTICALS
from app.services.dashboard_layouts import (
    COPILOT_DASHBOARD_WIDGETS,
    CTEM_DASHBOARD_WIDGETS,
    GRID_COLUMNS,
    MAX_WIDGET_HEIGHT,
    check_widget_ids,
    max_grid_row,
    max_widget_id_length,
)
from app.services.model_catalog_announcements import MAX_ANNOUNCED_MODELS as _MAX_ANNOUNCED_MODELS


def _parse_context_windows(value: Any) -> Any:
    """``{model: tokens}`` with each count typed as ``128000``, ``128K`` or ``1.05M``.

    Stored as whole token counts. An entry that is not a count between 1 and
    ``MAX_CONTEXT_WINDOW_TOKENS`` is refused (422), never stored to be ignored.
    """
    if not isinstance(value, dict):
        return value  # None, or a type error pydantic reports itself.
    parsed: dict[str, int] = {}
    for model, raw in value.items():
        if not is_model_id(model):
            raise ValueError("Each context window must be keyed by a model id.")
        tokens = parse_token_count(raw)
        if tokens is None:
            raise ValueError(
                f"The context window of {model[:80]!r} is not a token count: "
                f"use a whole number up to {MAX_CONTEXT_WINDOW_TOKENS:,}, "
                "or a shorthand such as 128K or 1.05M."
            )
        parsed[model] = tokens
    return parsed


class LLMProviderRequest(BaseModel):
    provider: str  # anthropic | openai | gemini
    api_key: str
    display_name: str | None = None
    default_model: str | None = None
    available_models: list[str] | None = None
    context_windows: dict[str, int] | None = None
    max_output_tokens: dict[str, int] | None = None
    # Restricted to the groups and users below; everyone otherwise.
    access_restricted: bool = False
    shared_group_ids: list[str] | None = None
    shared_user_ids: list[str] | None = None
    # Never used for these groups and users, whatever the grants.
    excluded_group_ids: list[str] | None = None
    excluded_user_ids: list[str] | None = None

    _vault_references = vault_reference_validator("api_key")
    _context_windows = field_validator("context_windows", mode="before")(_parse_context_windows)


class LLMProviderUpdateRequest(BaseModel):
    display_name: str | None = None
    default_model: str | None = None
    available_models: list[str] | None = None
    context_windows: dict[str, int] | None = None
    max_output_tokens: dict[str, int] | None = None
    # Omitted = unchanged.
    access_restricted: bool | None = None
    shared_group_ids: list[str] | None = None
    shared_user_ids: list[str] | None = None
    excluded_group_ids: list[str] | None = None
    excluded_user_ids: list[str] | None = None

    _context_windows = field_validator("context_windows", mode="before")(_parse_context_windows)


class LLMModelOverlap(BaseModel):
    """One model id offered by more than one provider config, and who else offers it."""

    model: str
    provider_ids: list[str]


class CatalogModelSummary(BaseModel):
    """A chat model and the provider that offers it (``""`` when the platform never recorded one)."""

    id: str
    provider: str = ""


class ModelCatalogChangeSummary(BaseModel):
    """The chat models the admin's provider saves added or retired for the users, not emailed yet.

    The net of the saves of ``MODEL_CATALOG_CHANGES_WINDOW_HOURS``.
    ``id`` is the latest save it covers: emailing it consumes the saves up to it.
    """

    id: str
    added_models: list[str] = Field(default_factory=list)
    removed_models: list[str] = Field(default_factory=list)
    added: list[CatalogModelSummary] = Field(default_factory=list)
    removed: list[CatalogModelSummary] = Field(default_factory=list)
    # The latest save it covers, the first one, and how many saves it consolidates.
    created_at: str
    first_saved_at: str
    saves: int = 1


class RefreshModelsRequest(BaseModel):
    """``POST /settings/llm-providers/{id}/refresh-models``."""

    # Replace an admin-edited catalog with the listing, which it then follows.
    discard_edits: bool = False


class LLMProviderResponse(BaseModel):
    id: str
    provider: str
    display_name: str | None = None
    default_model: str | None
    available_models: list[str] | None = None
    context_windows: dict[str, int] | None = None
    max_output_tokens: dict[str, int] | None = None
    has_key: bool = True
    # Filigran AI Gateway only, admins only: its URL, parsed out of the stored
    # credential so the token never travels with it.
    gateway_url: str | None = None
    is_from_env: bool = False
    # True when the model catalog is owned by the environment (env-seeded
    # provider, with neither ``LLM_PROVIDER_MODELS_EDITABLE=true`` nor
    # ``BYOLLM_ACTIVATED=true`` handing it to admins): the Settings
    # UI hides the model editor and ``PUT`` refuses ``available_models`` /
    # ``default_model`` changes. ``is_from_env`` alone only says the API key
    # is env-managed.
    models_env_locked: bool = False
    # Env-seeded with no ``LLM_PROVIDER_{P}_MODELS``: its catalog is its own
    # listing, which ``POST .../refresh-models`` reads again. Set on the listing.
    models_listed: bool = False
    # Editable mode, same provider, catalog edited by an admin: ``refresh-models``
    # with ``discard_edits`` makes it follow the listing again. Set on the listing.
    models_followable: bool = False
    # Model ids this config advertises that another config advertises too, each
    # with the other configs offering it. Overlap is legitimate — a gateway
    # fronting a vendor beside that vendor's own key — but it is what makes a
    # bare model id ambiguous, so an admin should learn of it at save time rather
    # than from a mis-billed call.
    model_overlaps: list[LLMModelOverlap] = []
    # On the answer to a create or an update that added or retired a model the
    # users pick from: Settings offers to email them about it.
    model_catalog_change: ModelCatalogChangeSummary | None = None
    # Who may use it: everyone, or only these groups and users. The
    # lists are served to admins only.
    access_restricted: bool = False
    shared_group_ids: list[str] = Field(default_factory=list)
    shared_user_ids: list[str] = Field(default_factory=list)
    # Who it is never used for, whatever the grants; admins only.
    excluded_group_ids: list[str] = Field(default_factory=list)
    excluded_user_ids: list[str] = Field(default_factory=list)
    #: The API key read from a vault, and its recent failures.
    vault_secrets: VaultSecretStatus | None = None
    # The company-managed integration this config is fed from (its API key is
    # kept in step with the integration's), or None for a Settings-made one.
    source_integration_id: str | None = None
    # What its provider type is known to serve (``router.provider_capabilities``):
    # ``chat`` or its one capability (``embedding``, ``speech``), and
    # ``decision``. Settings offers chat models only for a provider that chats.
    capabilities: list[str] = Field(default_factory=list)
    # Whether its decision listing names every decision model it serves
    # (``lists_decision_models``). Settings asks for a typed model id only
    # from a provider that cannot list.
    lists_decision_models: bool = False

    model_config = {"from_attributes": True}


class ProviderRouteTarget(BaseModel):
    """A model on a provider config, as a route stores it."""

    provider: str | None = None
    provider_id: str | None = None
    display_name: str | None = None
    model: str | None = None


class ProviderDeletionRoute(BaseModel):
    """What one route that used a provider config becomes when it is deleted.

    ``reseeded``: moved to the replacement, pinned by id. ``cleared``: emptied,
    so the route falls back to the next resolution tier. ``unchanged``: it did
    not use the deleted config.
    """

    affected: bool
    action: Literal["unchanged", "reseeded", "cleared"]
    before: ProviderRouteTarget | None = None
    after: ProviderRouteTarget | None = None
    # Owned by a ``DEFAULT_*`` environment variable, re-applied at the next restart.
    env_managed: bool = False
    # For a ``reseeded`` route: True when it keeps its model on the replacement
    # (the replacement offers it), False when it moves to the replacement's
    # default model; None otherwise.
    model_kept: bool | None = None


class ProviderDeletionEmbeddingRoute(ProviderDeletionRoute):
    """The embedding default: cleared when affected, never switched to another model."""

    # What embeddings fall back to while no embedding default is set.
    fallback: ProviderRouteTarget | None = None
    # The fallback embeds with the model the stored vectors were built with.
    fallback_same_model: bool = False
    # Knowledge bases holding indexed chunks, which a new embedding default re-embeds.
    knowledge_base_count: int = 0
    # No embedding default is stored and embeddings fall back to the deleted
    # config today: they move to ``fallback``; ``previous_fallback`` is today's.
    fallback_moves: bool = False
    previous_fallback: ProviderRouteTarget | None = None


class ProviderDeletionReferences(BaseModel):
    """Agents or users whose own model choice points at the deleted config."""

    count: int = 0
    names: list[str] = Field(default_factory=list)


class LLMProviderDeletionImpactResponse(BaseModel):
    """``GET /settings/llm-providers/{id}/deletion-impact`` - what deleting it does.

    Computed by the same function the ``DELETE`` applies, so the confirmation
    and the deletion cannot disagree.
    """

    provider_id: str
    provider: str
    display_name: str | None = None
    # The config the chat defaults move to: the requested one, else the rule's pick.
    replacement: ProviderRouteTarget | None = None
    replacement_explicit: bool = False
    # The rule's pick: the first surviving chat-capable config, oldest first.
    default_replacement_id: str | None = None
    # Surviving configs that can take over chat, each with its default chat model.
    candidates: list[ProviderRouteTarget] = Field(default_factory=list)
    # False when no surviving config can serve chat.
    chat_available: bool = True
    # Chat-capable survivors not offered as the replacement: they list no model
    # and a default that moves runs on one.
    unlisted: list[ProviderRouteTarget] = Field(default_factory=list)
    # Where a chat call no stored default decides lands after the deletion, set
    # when chat_available is false (a config listing only non-chat models, on
    # its provider's built-in chat model) or when implicit_chat_moves is true.
    implicit_chat_fallback: ProviderRouteTarget | None = None
    # No chat default is stored and chat implicitly goes to the deleted config
    # today (previous_implicit_chat): it moves to implicit_chat_fallback.
    implicit_chat_moves: bool = False
    previous_implicit_chat: ProviderRouteTarget | None = None
    general_assistant: ProviderDeletionRoute
    agent_default: ProviderDeletionRoute
    image_default: ProviderDeletionRoute
    embedding_default: ProviderDeletionEmbeddingRoute
    routing_model: ProviderDeletionRoute
    fallback_model: ProviderDeletionRoute
    decision_model: ProviderDeletionRoute
    agents: ProviderDeletionReferences
    users: ProviderDeletionReferences
    # Sent back as the delete's ``expected_impact``: it applies this or nothing (409).
    impact_fingerprint: str


class ResolveModelsRequest(BaseModel):
    provider: str  # anthropic | openai | gemini
    api_key: str


class ResolveModelsResponse(BaseModel):
    provider: str
    models: list[str]
    # Optional: models grouped by upstream provider (``owned_by``), set by
    # providers that proxy multiple upstreams (the Filigran AI Gateway).
    model_groups: dict[str, list[str]] | None = None
    # Filigran AI Gateway only: every id it offers, whatever the capability. ``models``
    # lists chat models alone, so a stored embedding or image model is judged against this.
    offered_models: list[str] | None = None


class ScanContextWindowsRequest(BaseModel):
    """The models of a provider whose context windows to look up."""

    models: list[str] = Field(min_length=1, max_length=1000)

    @field_validator("models")
    @classmethod
    def _validate_models(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for model in value:
            model = model.strip()
            if not model or len(model) > MAX_MODEL_ID_LENGTH:
                raise ValueError(
                    f"Each model id must be 1 to {MAX_MODEL_ID_LENGTH} characters."
                )
            if model not in cleaned:
                cleaned.append(model)
        return cleaned


class ScannedContextWindowItem(BaseModel):
    model: str
    #: ``None`` when no source knows the model: nothing is filled in for it.
    context_window: int | None = None
    #: ``provider``: the provider's model listing; ``catalog``: the bundled
    #: model catalog; ``family``: the platform's estimate for the model family.
    source: Literal["provider", "catalog", "family"] | None = None


class ScanContextWindowsResponse(BaseModel):
    provider: str
    #: ``ok``: the provider's listing was read; ``unsupported``: it reports no
    #: window; ``failed``: it could not be read, the catalog answered alone.
    live_lookup: Literal["ok", "unsupported", "failed"]
    results: list[ScannedContextWindowItem]


#: The capabilities ``POST /settings/llm-providers/validate`` discovers models for.
#: Every provider's ``only_capability`` must be one: the UI validates such a provider for it.
PROVIDER_VALIDATION_CAPABILITIES = ("reasoning", "embedding", "image", "speech", "video")


class ValidateProviderRequest(BaseModel):
    provider: str
    api_key: str
    capability: str = Field(default="reasoning")

    @field_validator("capability")
    @classmethod
    def _validate_capability(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in PROVIDER_VALIDATION_CAPABILITIES:
            raise ValueError(
                f"Unsupported capability '{value}'. "
                f"Use one of: {', '.join(PROVIDER_VALIDATION_CAPABILITIES)}."
            )
        return normalized


class ValidateProviderResponse(BaseModel):
    provider: str
    capability: str
    connection_ok: bool
    models_supported: bool
    models: list[str] = Field(default_factory=list)
    model_groups: dict[str, list[str]] | None = None
    message: str


class EmbeddingModelTestRequest(BaseModel):
    provider: str
    model: str
    text: str | None = None
    provider_id: str | None = None


class EmbeddingModelTestResponse(BaseModel):
    provider: str
    model: str
    capable: bool
    returned_dimensions: int | None = None
    requested_dimensions: int | None = None
    suggested_dimensions: list[int] = Field(default_factory=list)
    expected_dimensions: int | None = None
    message: str


_WIDGET_ID_LENGTH = max_widget_id_length(CTEM_DASHBOARD_WIDGETS + COPILOT_DASHBOARD_WIDGETS)


class DashboardLayoutItem(BaseModel):
    """Where a widget sits on a dashboard grid, in grid units."""

    model_config = ConfigDict(extra="forbid")

    i: str = Field(max_length=_WIDGET_ID_LENGTH)
    x: int = Field(ge=0, lt=GRID_COLUMNS)
    y: int = Field(ge=0)
    w: int = Field(ge=1, le=GRID_COLUMNS)
    h: int = Field(ge=1, le=MAX_WIDGET_HEIGHT)

    @model_validator(mode="after")
    def _fits_the_grid(self) -> Self:
        if self.x + self.w > GRID_COLUMNS:
            raise ValueError("a dashboard widget runs past the last column")
        return self


class _DashboardLayout(BaseModel):
    """A dashboard built on the grid: the widgets shown and where, and the ones
    removed. Each dashboard names its ``widgets`` and bounds its lists by them;
    the grid's bounds are the same for all.
    """

    model_config = ConfigDict(extra="forbid")

    widgets: ClassVar[tuple[str, ...]]

    # Read only here; a dashboard may hold a subclass of the item (CTEM: its settings).
    items: Sequence[DashboardLayoutItem]
    hidden: list[Annotated[str, Field(max_length=_WIDGET_ID_LENGTH)]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _known_widgets_once(self) -> Self:
        max_row = max_grid_row(self.widgets)
        if any(item.y > max_row for item in self.items):
            raise ValueError("a dashboard widget starts below the last row")
        # A widget is shown or removed, never both: one id list over the two.
        check_widget_ids([item.i for item in self.items] + list(self.hidden), self.widgets)
        return self


# The periods the CTEM context bar offers, in days (dev-docs/ctem-stage-viz.md 8.2).
CtemContextPeriod = Literal[7, 30, 90]
# A kill chain name as the data carries it (mitre-attack, disarm, any custom name).
_FRAMEWORK_NAME_LENGTH = 128


class CtemDashboardContext(BaseModel):
    """The context bar of the command center (section 8.2): every widget reads
    it unless it has its own value. Absent fields are the defaults."""

    model_config = ConfigDict(extra="forbid")

    framework: str | None = Field(default=None, min_length=1, max_length=_FRAMEWORK_NAME_LENGTH)
    period: CtemContextPeriod | None = None


class CtemWidgetSettings(BaseModel):
    """A widget's own values over the dashboard context (section 8.3)."""

    model_config = ConfigDict(extra="forbid")

    period: CtemContextPeriod | None = None


class CtemDashboardLayoutItem(DashboardLayoutItem):
    """A widget of the command center, with its optional own settings."""

    settings: CtemWidgetSettings | None = None


class CtemDashboardLayout(_DashboardLayout):
    """The CTEM command center of one vertical. ``context`` and the items'
    ``settings`` are optional: a layout saved without them reads unchanged."""

    widgets: ClassVar[tuple[str, ...]] = CTEM_DASHBOARD_WIDGETS

    items: list[CtemDashboardLayoutItem] = Field(max_length=len(CTEM_DASHBOARD_WIDGETS))
    context: CtemDashboardContext | None = None
    hidden: list[Annotated[str, Field(max_length=_WIDGET_ID_LENGTH)]] = Field(
        default_factory=list, max_length=len(CTEM_DASHBOARD_WIDGETS)
    )


class CopilotDashboardLayout(_DashboardLayout):
    """The Copilot dashboard (one layout: the Copilot mode has no verticals)."""

    widgets: ClassVar[tuple[str, ...]] = COPILOT_DASHBOARD_WIDGETS

    items: list[DashboardLayoutItem] = Field(max_length=len(COPILOT_DASHBOARD_WIDGETS))
    hidden: list[Annotated[str, Field(max_length=_WIDGET_ID_LENGTH)]] = Field(
        default_factory=list, max_length=len(COPILOT_DASHBOARD_WIDGETS)
    )


class CtemDashboardLayoutUpdate(BaseModel):
    """The layout of one vertical's command center; ``layout: null`` resets it."""

    model_config = ConfigDict(extra="forbid")

    vertical: str
    layout: CtemDashboardLayout | None

    @field_validator("vertical")
    @classmethod
    def _known_vertical(cls, value: str) -> str:
        if value not in CTEM_VERTICALS:
            raise ValueError("unknown CTEM vertical")
        return value


MAX_SIDEBAR_EXPANDED_GROUPS = 500
"""Ceiling on ``users.chat_sidebar_expanded_workspaces`` (a person's open groups)."""

MAX_SIDEBAR_GROUP_KEY_LENGTH = 64
"""A group key is a workspace id (36 characters) or the ``__elsewhere__`` group."""


MAX_FLEET_INVITE_ENTRIES = 100
"""Ceiling on each list of ``users.ctem_fleet_invite_dismissed``."""

_InviteEntry = Annotated[str, Field(min_length=1, max_length=120)]


MAX_DASHBOARD_FLEETS = 50
"""Ceiling on ``users.ctem_dashboard_fleet_ids`` and ``users.copilot_dashboard_fleet_ids``."""

_DashboardFleetId = Annotated[str, Field(min_length=1, max_length=64)]


MAX_ORG_NOTICE_FIELDS = 64
"""Ceiling on ``users.organization_context_notice_dismissed``."""

_OrgNoticeField = Annotated[str, Field(min_length=1, max_length=120)]


class CtemFleetInviteDismissal(BaseModel):
    """What the person saw when they closed the fleets invitation (#9566)."""

    fleets: list[_InviteEntry] = Field(default_factory=list, max_length=MAX_FLEET_INVITE_ENTRIES)
    modules: list[_InviteEntry] = Field(default_factory=list, max_length=MAX_FLEET_INVITE_ENTRIES)


class EmailChangeRequest(BaseModel):
    """``POST /settings/email-change``: the address is checked by the route (400, not 422)."""

    email: str = Field(max_length=320)
    current_password: str = Field(max_length=1024)


class EmailChangeConfirmRequest(BaseModel):
    code: str = Field(max_length=32)


class UserPreferencesRequest(BaseModel):
    email: str | None = None
    full_name: str | None = None
    title: str | None = None
    timezone: str | None = None
    theme: str | None = None
    # UI language override: "auto" or a supported locale code (e.g. "fr-fr").
    language: str | None = None
    # CTEM command center (XTM One home) per-user preferences.
    ctem_vertical: str | None = None
    ctem_splash_seen: bool | None = None
    # The command center this person built for one vertical (the others are
    # left as they are).
    ctem_dashboard_layout: CtemDashboardLayoutUpdate | None = None
    # The command center's fleets invitation closed (#9566); null shows it again.
    ctem_fleet_invite_dismissed: CtemFleetInviteDismissal | None = None
    # The organization context fields missing when the person closed the
    # command center's incomplete-context notice (#11586); null shows it again.
    organization_context_notice_dismissed: list[_OrgNoticeField] | None = Field(
        default=None, max_length=MAX_ORG_NOTICE_FIELDS
    )
    # The fleets the command center shows (#11464), in order; null restores the
    # built-in CTEM fleets.
    ctem_dashboard_fleet_ids: list[_DashboardFleetId] | None = Field(
        default=None, max_length=MAX_DASHBOARD_FLEETS
    )
    # The fleets the Copilot home shows (#12063), in order; null restores every fleet.
    copilot_dashboard_fleet_ids: list[_DashboardFleetId] | None = Field(
        default=None, max_length=MAX_DASHBOARD_FLEETS
    )
    # The Copilot dashboard this person built (one layout: the Copilot mode has
    # no verticals); an explicit null resets the default layout.
    copilot_dashboard_layout: CopilotDashboardLayout | None = None
    default_knowledge_base_ids: list[str] | None = None
    default_mcp_server_ids: list[str] | None = None
    default_agent_id: str | None = None
    # The custom dashboard shown on ``/dashboard`` instead of the platform's: one
    # of the caller's own (checked by the route); an explicit null clears it.
    home_dashboard_id: uuid.UUID | None = None
    # Per-user General Assistant overrides (NULL = use platform defaults)
    ga_builtin_tool_ids: list[str] | None = None
    ga_custom_tool_ids: list[str] | None = None
    ga_integration_bindings: dict[str, list[str]] | None = None
    ga_mcp_server_ids: list[str] | None = None
    ga_knowledge_base_ids: list[str] | None = None
    # The browser profiles the person's General Assistant may open.
    ga_browser_profile_ids: list[str] | None = None
    # Per-user channel agent overrides for DM conversations
    channel_agent_config: dict[str, str] | None = None
    # MCP endpoint – user-selected agents to expose on /mcp/user
    mcp_exposed_agent_ids: list[str] | None = None
    # Favorite agents starred on /agents (insertion-ordered set, not a display order)
    favorite_agent_ids: list[str] | None = None
    # Own integration ids whose "personal OAuth token is shared" dashboard
    # reminder this user closed. Normalized server-side to ids
    # naming an OAuth integration the caller owns whose token is currently
    # acknowledged as shared - see ``_clean_oauth_share_dismissed_ids``.
    oauth_share_dismissed_integration_ids: list[str] | None = None
    # The bell shows the work items waiting for this person (#9437).
    notify_waiting_work_items: bool | None = None
    # The composer's reply suggestion after a chat turn.
    reply_suggestions_enabled: bool | None = None
    # The chat sidebar's workspace groups shown expanded (#9179), written
    # whole; an explicit null forgets the choice (every group collapsed).
    chat_sidebar_expanded_workspaces: (
        Annotated[
            list[Annotated[str, Field(min_length=1, max_length=MAX_SIDEBAR_GROUP_KEY_LENGTH)]],
            Field(max_length=MAX_SIDEBAR_EXPANDED_GROUPS),
        ]
        | None
    ) = None

    # ``theme``, ``language`` and ``ctem_vertical`` are normalized to a known
    # value by the route before they are written.
    _fit_columns = column_length_validator(User, "email", "full_name", "title", "timezone")


class AvailableModelItem(BaseModel):
    model: str
    provider: str
    provider_id: str | None = None
    # Its provider is restricted to some groups and users.
    access_restricted: bool = False


class MediaModelsInUse(BaseModel):
    """The image and speech models generation runs on for the caller; ``None`` when none can."""

    image: AvailableModelItem | None = None
    speech: AvailableModelItem | None = None


# Why a provider that lists its decision models offered what it did:
# "incomplete" is OpenAI's check still running or cut short, "unavailable" a
# provider the caller may not use, so never asked.
DecisionListingStatus = Literal["listed", "empty", "incomplete", "failed", "unavailable"]


class DecisionListing(BaseModel):
    provider_id: str
    status: DecisionListingStatus


class AvailableDecisionModelsResponse(BaseModel):
    models: list[AvailableModelItem]
    listings: list[DecisionListing]


class SpeechVoiceItem(BaseModel):
    id: str
    name: str
    # premade, cloned, generated, professional, prebuilt...; "" for a fixed set.
    category: str = ""
    description: str = ""


class VideoModelStatus(BaseModel):
    """Why the video model chosen in Settings can no longer make a video: a retired model."""

    unavailable: str | None = None


class SpeechVoicesResponse(BaseModel):
    """The voices of the speech model a text would be spoken with."""

    provider: str | None = None
    provider_id: str | None = None
    model: str | None = None
    voices: list[SpeechVoiceItem] = Field(default_factory=list)
    # Why the list is empty, when it is.
    message: str | None = None


class GeneralAssistantConfigRequest(BaseModel):
    linked_agent_id: str | None = None
    llm_provider: str | None = None
    llm_provider_id: str | None = None
    llm_model: str | None = None
    agent_default_provider: str | None = None
    agent_default_provider_id: str | None = None
    agent_default_model: str | None = None
    default_reasoning_effort: str | None = None
    embedding_provider: str | None = None
    embedding_provider_id: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    image_provider: str | None = None
    image_provider_id: str | None = None
    image_model: str | None = None
    video_provider: str | None = None
    video_provider_id: str | None = None
    video_model: str | None = None
    # The video models an AI shot falls back to, in order, after the default one fails; [] clears it.
    video_fallback_models: list[str] | None = Field(None, max_length=5)
    speech_provider: str | None = None
    speech_provider_id: str | None = None
    speech_model: str | None = None
    # A voice id or name of the speech model's provider; "" clears it.
    speech_voice: str | None = Field(None, max_length=200)
    music_provider: str | None = None
    music_provider_id: str | None = None
    music_model: str | None = Field(None, max_length=100)
    routing_model: str | None = None
    routing_provider_id: str | None = None
    fallback_model: str | None = None
    fallback_provider_id: str | None = None
    # The model ``act_on_page`` decides with, and the config serving it.
    # Capped at the columns' sizes, so a longer value is a 422, not a 500.
    decision_model: str | None = Field(None, max_length=100)
    decision_provider_id: str | None = Field(None, max_length=36)
    max_iterations: int | None = None
    # The ``wait`` bounds moved to the platform AI settings:
    # ``PUT /settings/platform`` (``maximum_wait_time``,
    # ``maximum_total_wait_time``, ``wait_unbounded_with_until``).
    system_prompt: str | None = None
    knowledge_base_ids: list[str] | None = None
    mcp_server_ids: list[str] | None = None
    custom_tool_ids: list[str] | None = None
    builtin_tool_ids: list[str] | None = None
    # Company-managed skill ids; an explicit null goes back to the
    # built-in General Assistant skills.
    skill_ids: list[str] | None = None
    integration_bindings: dict[str, list[str]] | None = None
    handover_agent_ids: list[str] | None = None
    handover_types: dict[str, str] | None = None
    available_channel_types: list[str] | None = None
    channel_config: dict | None = None
    mcp_exposed_agent_ids: list[str] | None = None
    platform_mcp_enabled: bool | None = None
    xtm_mcp_enabled: bool | None = None
    # The composer's reply suggestion after a chat turn, on the routing model.
    reply_suggestions_enabled: bool | None = None
    enterprise_license: str | None = None
    # Email every user that ``agent_default_model`` is now the default,
    # delivered by the scheduler like the env announcements.
    announce_default_model: bool = False

    @field_validator("embedding_dimensions")
    @classmethod
    def validate_embedding_dimensions(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if value <= 0:
            raise ValueError("embedding_dimensions must be greater than 0")
        return value


class GeneralAssistantConfigResponse(BaseModel):
    linked_agent_id: str | None = None
    llm_provider: str | None = None
    llm_provider_id: str | None = None
    llm_model: str | None = None
    agent_default_provider: str | None = None
    agent_default_provider_id: str | None = None
    agent_default_model: str | None = None
    default_reasoning_effort: str | None = None
    embedding_provider: str | None = None
    embedding_provider_id: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = None
    image_provider: str | None = None
    image_provider_id: str | None = None
    image_model: str | None = None
    video_provider: str | None = None
    video_provider_id: str | None = None
    video_model: str | None = None
    video_fallback_models: list[str] = Field(default_factory=list)
    speech_provider: str | None = None
    speech_provider_id: str | None = None
    speech_model: str | None = None
    speech_voice: str | None = None
    music_provider: str | None = None
    music_provider_id: str | None = None
    music_model: str | None = None
    routing_model: str | None = None
    routing_provider_id: str | None = None
    fallback_model: str | None = None
    fallback_provider_id: str | None = None
    decision_model: str | None = None
    decision_provider_id: str | None = None
    max_iterations: int
    system_prompt: str
    knowledge_base_ids: list[str]
    mcp_server_ids: list[str]
    custom_tool_ids: list[str]
    builtin_tool_ids: list[str]
    # The admin's skill choice; null = the built-in ones, the skills
    # whose ``slug:`` tag is in ``default_skill_slugs``.
    skill_ids: list[str] | None = None
    default_skill_slugs: list[str] = Field(default_factory=list)
    integration_bindings: dict[str, list[str]]
    handover_agent_ids: list[str]
    handover_types: dict[str, str]
    available_channel_types: list[str] | None = None
    channel_config: dict | None = None
    mcp_exposed_agent_ids: list[str]
    mcp_global_api_key_prefix: str | None = None
    # Platform MCP servers (/mcp/opencti, /mcp/openaev) global toggle.
    platform_mcp_enabled: bool = True
    # XTM One's own MCP server (/mcp/xtm) global toggle. Default off — it
    # exposes this instance's configuration surface, not a connected platform's.
    xtm_mcp_enabled: bool = False
    # The composer's reply suggestion after a chat turn, generated on the
    # routing model: nothing is generated while ``routing_model`` is unset.
    reply_suggestions_enabled: bool = True
    # Names of default-model fields unconditionally re-seeded from environment
    # variables on every startup and therefore read-only in the UI (e.g.
    # ``agent_default_model``, ``default_reasoning_effort``, ``routing_model``,
    # ``image_model``).  Mirrors the env-seeded LLM provider lock.  Embedding
    # defaults are included too, since their seeding is unconditional.
    # Always empty under ``BYOLLM_ACTIVATED=true``, where every ``DEFAULT_*``
    # only furnishes an install that has no LLM configuration yet
    # (``_env_may_seed``) and the administrator owns the lot.
    env_managed_fields: list[str] = Field(default_factory=list)
    # Only on the answer to a save that asked for ``announce_default_model``:
    # whether an email is now on its way (``False`` when the users were never
    # told about the default this save restores, so there is nothing new).
    default_model_announced: bool | None = None


class CapabilityNewsSummary(BaseModel):
    """A capability an announcement says arrived or changed model."""

    # image | video | speech | music | decision
    capability: str
    model: str
    provider: str = ""
    # ``None`` when the capability is new.
    previous_model: str | None = None


class ModelAnnouncementSummary(BaseModel):
    """The latest model announcement and how far it went."""

    id: str
    source: Literal["env", "admin"]
    # pending | sending | sent | skipped | expired | failed
    status: str
    skip_reason: str | None = None
    # What the email says: the new default (when it moved), the models it
    # announces as newly available, the ones it says were retired, and the
    # capabilities that arrived or changed model.
    default_model: str | None = None
    previous_default_model: str | None = None
    added_models: list[str] = Field(default_factory=list)
    removed_models: list[str] = Field(default_factory=list)
    capabilities: list[CapabilityNewsSummary] = Field(default_factory=list)
    created_at: str
    sent_at: str | None = None
    delivered: int = 0
    refused: int = 0
    unknown: int = 0
    # Pending / sending / failed: the users it has not reached yet, those
    # being emailed right now included.
    remaining: int = 0
    # Pending / sending: the open announcements recorded before it, sent first.
    queued_behind: int = 0
    # Pending after a failed batch: when the next batch may run. Pending /
    # sending / failed: the failed batches in a row and the last SMTP error.
    retry_at: str | None = None
    failed_attempts: int = 0
    last_error: str | None = None
    # Pending env change waiting for the env catalog to stay unchanged
    # (``MODEL_CATALOG_EMAILS_SETTLE_MINUTES``): when it may go out.
    held_until: str | None = None
    # Pending / sending behind an older announcement: when that one's next
    # attempt runs, if it is waiting for one (a back-off, a retry, a hold).
    blocked_until: str | None = None
    # Pending / sending: the addresses the email server refused for now
    # (counted in ``remaining``), retried once after everyone else; when that
    # retry runs, once nobody else is left to email, or whether it is under
    # way; and the server's last reply to one of them. Never a failed batch.
    deferred: int = 0
    deferred_retry_at: str | None = None
    deferred_retrying: bool = False
    last_refusal: str | None = None


class ModelAnnouncementStatusResponse(BaseModel):
    email_configured: bool
    # ``MODEL_ANNOUNCEMENT_EMAILS_DISABLED``: no announcement may be emailed, whatever SMTP.
    emails_disabled: bool = False
    # Active users an announcement is emailed to (the bootstrap admin excluded).
    recipient_count: int
    # How many users the scheduler emails per batch, one batch a minute.
    emails_per_minute: int = 0
    default_model: str | None = None
    default_provider: str | None = None
    # The default ``default_model`` replaced, as the recorded changes tell:
    # what "Email users" would say it replaces.
    previous_default_model: str | None = None
    latest: ModelAnnouncementSummary | None = None
    # The model changes saved and not emailed yet, consolidated over
    # ``changes_window_hours``: what the admin is offered to email.
    unannounced_change: ModelCatalogChangeSummary | None = None
    changes_window_hours: int = 24
    # What the admin may announce: as new, any model the users can pick now;
    # as retired, any model at all - these are the ones the users could
    # pick before, the providers' own lists come from ``/catalogs``.
    offered_models: list[CatalogModelSummary] = Field(default_factory=list)
    formerly_offered_models: list[CatalogModelSummary] = Field(default_factory=list)


class RetiredModelChoice(BaseModel):
    """A model announced as retired, with the provider the email names (``""``: the platform's guess)."""

    id: str = Field(..., max_length=255)
    provider: str = Field("", max_length=64)


class ModelAnnouncementTestRequest(BaseModel):
    # The model the admin is about to make the default; omitted = the current default.
    model: str | None = Field(None, max_length=255)
    provider: str | None = Field(None, max_length=64)
    # Model changes instead, as the admin edited them: the models announced as
    # new and as retired (either list given switches to a model changes email).
    added_models: list[str] | None = Field(None, max_length=_MAX_ANNOUNCED_MODELS)
    removed_models: list[str | RetiredModelChoice] | None = Field(None, max_length=_MAX_ANNOUNCED_MODELS)


class ModelChangesAnnouncementRequest(BaseModel):
    """Email the users the model changes the admin chose."""

    # The draft the admin reviewed (``unannounced_change.id``): its saves are
    # consumed. Omitted for an announcement of the admin's own.
    draft_id: str | None = Field(None, max_length=64)
    added_models: list[str] = Field(default_factory=list, max_length=_MAX_ANNOUNCED_MODELS)
    removed_models: list[str | RetiredModelChoice] = Field(default_factory=list, max_length=_MAX_ANNOUNCED_MODELS)


class ModelAnnouncementTestResponse(BaseModel):
    sent_to: str


class ModelAnnouncementPreviewResponse(BaseModel):
    """The email the users would receive, rendered and not sent."""

    subject: str
    html: str
    text: str


class ProviderCatalogSummary(BaseModel):
    """One configured provider's own model list: how many chat models it serves, or why it could not say."""

    provider_id: str
    provider: str
    label: str | None = None
    model_count: int = 0
    error: str | None = None
    # It served more chat models than are listed: ``model_count`` is the cap, the first ones in id order.
    truncated: bool = False


class ModelAnnouncementCatalogsResponse(BaseModel):
    """The chat models the configured providers serve that the users cannot pick."""

    models: list[CatalogModelSummary] = Field(default_factory=list)
    providers: list[ProviderCatalogSummary] = Field(default_factory=list)


class OAuthDefaultRequest(BaseModel):
    provider: str  # microsoft | google | github | slack | ...
    client_id: str
    client_secret: str
    extra_config: dict[str, str] | None = None  # e.g. {"tenant_id": "..."}

    # The client secret may come from a vault; the JSON extra config may not.
    _vault_references = vault_reference_validator("client_secret")
    _no_vault_references = no_vault_reference_validator("client_id", "extra_config")


class OAuthDefaultResponse(BaseModel):
    id: str
    provider: str
    has_client_id: bool
    has_client_secret: bool
    has_extra_config: bool
    # Secret extra-config keys holding a stored value (names only, never the
    # value): the form sends the mask for one left empty, which keeps it.
    stored_secret_keys: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class SSOOIDCTestRequest(BaseModel):
    oidc_discovery_url: str


class SSOOIDCTestResponse(BaseModel):
    ok: bool
    # ``failed`` when the provider looks down, ``misconfigured`` when the URL is wrong.
    status: str
    detail: str


class SSOConfigRequest(BaseModel):
    # OIDC
    oidc_enabled: bool = False
    oidc_display_name: str = "Microsoft"
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_discovery_url: str = ""
    oidc_authorization_url: str = ""
    oidc_token_url: str = ""
    oidc_userinfo_url: str = ""
    oidc_scopes: str = "openid email profile"
    # SAML
    saml_enabled: bool = False
    saml_display_name: str = "Corporate SSO"
    saml_idp_metadata_url: str = ""
    saml_sp_entity_id: str = ""
    saml_requested_authn_context: bool = False
    saml_groups_mapping_enabled: bool = True
    saml_groups_auto_create: bool = True
    # Auto-admin at every SSO sign-in, existing accounts included
    oidc_auto_admin_new_users: bool = False
    saml_auto_admin_new_users: bool = False
    # On-the-fly user creation on first SSO login
    oidc_auto_create_users: bool = True
    saml_auto_create_users: bool = True
    # General auth
    local_auth_enabled: bool = True
    registration_enabled: bool = False
    allowed_domains: str = ""

    _vault_references = vault_reference_validator("oidc_client_secret")
    _no_vault_references = no_vault_reference_validator("oidc_client_id")


class SSOConfigResponse(BaseModel):
    # OIDC
    oidc_enabled: bool = False
    oidc_display_name: str = "Microsoft"
    oidc_has_client_id: bool = False
    oidc_has_client_secret: bool = False
    oidc_discovery_url: str = ""
    oidc_authorization_url: str = ""
    oidc_token_url: str = ""
    oidc_userinfo_url: str = ""
    oidc_scopes: str = "openid email profile"
    # SAML
    saml_enabled: bool = False
    saml_display_name: str = "Corporate SSO"
    saml_idp_metadata_url: str = ""
    saml_sp_entity_id: str = ""
    saml_requested_authn_context: bool = False
    saml_groups_mapping_enabled: bool = True
    saml_groups_auto_create: bool = True
    # Auto-admin at every SSO sign-in, existing accounts included
    oidc_auto_admin_new_users: bool = False
    saml_auto_admin_new_users: bool = False
    # On-the-fly user creation on first SSO login
    oidc_auto_create_users: bool = True
    saml_auto_create_users: bool = True
    # SAML computed URLs (for guidance)
    saml_entity_id: str = ""
    saml_acs_url: str = ""
    saml_metadata_url: str = ""
    # General auth
    local_auth_enabled: bool = True
    registration_enabled: bool = False
    allowed_domains: str = ""
    # ``allowed_domains`` as self-registration and SSO account creation read it
    allowed_domain_list: list[str] = []
    #: The OIDC client secret read from a vault, and its recent failures.
    vault_secrets: VaultSecretStatus | None = None


class LangfuseConfigRequest(BaseModel):
    enabled: bool = False
    public_key: str = ""
    secret_key: str = ""
    host: str = "https://cloud.langfuse.com"

    _vault_references = vault_reference_validator("public_key", "secret_key")


class LangfuseConfigResponse(BaseModel):
    enabled: bool
    has_public_key: bool
    has_secret_key: bool
    host: str
    # True when the config is seeded from LANGFUSE_* environment variables and
    # is therefore read-only in the UI (mirrors the env-seeded LLM provider
    # lock and the env-managed default-model lock).
    is_from_env: bool = False
    #: The keys read from a vault, and their recent failures.
    vault_secrets: VaultSecretStatus | None = None


# --- Security options (Settings > Access and security) ----------------------------


class SecurityOptionItem(BaseModel):
    """One option of ``app/core/security_options.py`` as Settings > Access and security shows it."""

    key: str
    env_var: str
    kind: Literal["bool", "choice", "int", "origins", "modules", "tools", "certificates"]
    group: str
    choices: list[str] = Field(default_factory=list)
    minimum: int | None = None
    maximum: int | None = None
    # The one value outside the bounds an ``int`` option also accepts, which
    # turns it off.
    off_value: int | None = None
    # The effective value, the default, and what the database holds (null
    # when nothing is stored). ``bool`` / ``str`` / ``int`` / ``list[str]``
    # by ``kind`` (``certificates``: PEM text, a ``str``).
    value: bool | int | str | list[str] | None = None
    default: bool | int | str | list[str] | None = None
    stored: bool | int | str | list[str] | None = None
    source: Literal["platform_mode", "environment", "database", "default"]
    # The field is read-only and a write to it is refused (403): the platform
    # mode pins it (in XTM One, quotas always fail closed), or its
    # environment variable is set, like an env-managed default model.
    locked: bool = False
    locked_reason: Literal["platform_mode", "environment"] | None = None


class SecurityOptionsResponse(BaseModel):
    options: list[SecurityOptionItem]
    # The option keys their environment variable decides (never a pinned
    # one) - the same name and meaning as
    # ``GeneralAssistantConfigResponse.env_managed_fields``.
    env_managed_fields: list[str] = Field(default_factory=list)


class SecurityOptionsUpdateRequest(BaseModel):
    # ``{option key: value}``; ``null`` clears the stored value (back to the
    # default). Values are validated per option, never coerced.
    values: dict[str, bool | int | str | list[str] | None] = Field(default_factory=dict)


class ScannableTool(BaseModel):
    """A tool the prompt guard can screen, as the scan-scope editor offers it."""

    # What ``prompt_guard_scanned_tools`` stores for it: its registered name,
    # or ``custom:<id>`` for a custom tool, whose name its owner may change.
    entry: str
    name: str
    description: str = ""


class ScannableToolGroup(BaseModel):
    # ``builtin``, ``custom``, ``mcp:<server id>`` or an integration provider key.
    key: str
    label: str
    tools: list[ScannableTool]


class ScannableToolsResponse(BaseModel):
    groups: list[ScannableToolGroup]


# --- Quota schemas --------------------------------------------------------


class QuotaTypeConfig(BaseModel):
    platform_limit: int | None = None
    default: int | None = None
    per_model: dict[str, int] | None = None
    period: str = "monthly"  # "monthly" or "daily"
    # User / group overrides only: an explicit Unlimited, which an emptied
    # ``default`` (inherit) is not. ``default`` is then ``None``.
    unlimited: bool | None = None
    # Stamped on every save (2 = the current rules); a stored value without it
    # is read with the rules it was saved under.
    rules_version: int | None = None


class QuotaConfig(BaseModel):
    llm_proxy: QuotaTypeConfig | None = None
    agentic: QuotaTypeConfig | None = None
    # No `sandbox` field: sandbox usage has no enforceable quota tier or
    # configurable period of its own (spec 002-sandbox-quota-usage-tracking)
    # — it always shares the agentic entry's limit and resolved period.


class QuotaUsageEntry(BaseModel):
    usage_type: str
    model: str | None = None
    period: str
    count: int
    # "user" (a per-user counter) or "global" (a shared platform-wide pool,
    # e.g. the XTM One included agentic allowance — ``count`` is then the total
    # across all users and entry points, not this user's slice). Constrained to
    # the two emitted values so the OpenAPI schema / TS client union stays exact
    # (matches the frontend ``QuotaUsageEntry.scope`` type).
    scope: Literal["user", "global"] = "user"


class QuotaPeriodUsage(BaseModel):
    """One row of the composer's quota panel (today, this month, this year)."""

    period: Literal["daily", "monthly", "yearly"]
    # Agentic actions spent in the period, the sandbox slice included.
    used: int
    # What gates the user in this period; null = unlimited.
    limit: int | None = None
    # "global": ``used`` is the shared platform-wide pool, not the caller's slice.
    scope: Literal["user", "global"] = "user"
    # UTC instant the period's counter starts again (ISO 8601, ``Z``).
    resets_at: str


class SupportCodeRedeemRequest(BaseModel):
    """A Filigran-signed support code pasted by an admin.

    ``max_length`` mirrors the service-side bound: the code is one signed
    token, not a document, and rejecting an oversized body here keeps a paste
    accident from reaching the verifier at all.
    """

    code: str = Field(min_length=1, max_length=8192)


class UserQuotaInfoResponse(BaseModel):
    # User's own raw override (from ``User.quota_config`` column).
    # ``None`` means the user has no override and inherits from group /
    # platform / license. Distinct from ``resolved_config`` which is the
    # effective config after walking the full resolution chain.
    user_config: QuotaConfig | None = None
    # quota_type -> notice code for a legacy value the next save would change.
    user_config_notices: dict[str, str] = {}
    resolved_config: QuotaConfig | None = None
    usage: list[QuotaUsageEntry] = []
    sources: dict[str, str] = {}  # quota_type -> "user"|"group"|"global"|"none"
    # Agentic actions per period (today, this month, this year): the quota panel.
    periods: list[QuotaPeriodUsage] = []
