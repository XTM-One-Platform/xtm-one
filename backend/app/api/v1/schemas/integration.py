# SPDX-License-Identifier: Apache-2.0

"""Pydantic schemas for integration endpoints."""

import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator

from app.api.v1.schemas.column_limits import column_length_validator
from app.api.v1.schemas.integration_openapi import OpenAPIImportOptions
from app.api.v1.schemas.vault_references import (
    VaultSecretStatus,
    no_vault_reference_validator,
    vault_reference_validator,
)
from app.models.integration import Integration

_INTEGRATION_COLUMN_FIELDS = ("name", "auth_type", "authorize_url", "token_url")

#: Never in a link target: whitespace and control characters, and the
#: backslash a browser reads as ``/`` where ``urlsplit`` does not, so
#: ``https://evil.example\@github.com`` would open ``evil.example``.
_UNSAFE_LINK_CHARS = re.compile(r"[\s\x00-\x1f\x7f\\]")


def safe_link_url(value: str, *, schemes: tuple[str, ...] = ("https",)) -> str | None:
    """*value* when a browser opens it on the host it names, else ``None``.

    An absolute ``<scheme>://host`` URL, the scheme lower-case and one of
    *schemes*, in ASCII (an IDN host is spelt in punycode), with a valid port
    and no userinfo: ``https://github.com@evil.example`` names ``evil.example``.
    """
    if _UNSAFE_LINK_CHARS.search(value) or not value.isascii():
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme not in schemes
        or not value.startswith(f"{parts.scheme}://")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port == 0
    ):
        return None
    return value


class SetupGuidePermission(BaseModel):
    scope: str
    description: str


class SetupGuideOpenSource(BaseModel):
    """The open-source project an integration is built on, credited in its guide."""

    #: The project's own name, which the guide links: not the integration's.
    name: str
    url: str
    author: str | None = None
    license: str | None = None

    @field_validator("url")
    @classmethod
    def _https_only(cls, v: str) -> str:
        if safe_link_url(v) is None:
            raise ValueError(
                "open_source.url must be an absolute https:// URL with a host "
                "and no credentials"
            )
        return v


class SetupGuide(BaseModel):
    title: str
    url: str | None = None
    steps: list[str]
    permissions: list[SetupGuidePermission] = []
    apis_to_enable: list[str] | None = None
    notes: str | None = None
    manifest_example: str | None = None
    open_source: SetupGuideOpenSource | None = None


class IntegrationTypeItem(BaseModel):
    key: str
    label: str
    description: str
    providers: list[str]


class ProviderEvent(BaseModel):
    event: str
    label: str
    description: str
    mechanism: str = "polling"  # "polling", "webhook", or "hybrid"
    # The ``trigger_config.filter`` keys the event takes, each with what it
    # does (``services/integration_event_filters.py``); empty = none.
    filter_keys: dict[str, str] = {}


class ProviderTool(BaseModel):
    name: str
    description: str
    tool_type: str = "read"  # "read" or "write"


class ProviderElementType(BaseModel):
    type: str  # e.g. "teams_channel", "slack_channel", "repository"
    name: str  # display name
    description: str
    multiple: bool = True  # whether multiple elements can be selected


class ProviderSettingSchemaItem(BaseModel):
    key: str
    label: str
    type: str
    required: bool
    placeholder: str | None = None
    options: list[dict[str, str]] | None = None


class ExtraFieldOption(BaseModel):
    value: str
    label: str


class ExtraFieldInput(BaseModel):
    """The control and rule of one extra field (catalog ``extra_field_inputs``).

    ``domains``: a list of domain names, drawn as tags; ``integer``: a whole
    number within ``minimum`` and ``maximum``, prefilled with ``default``. The
    create and update routes refuse a value outside the rule (422).
    """

    kind: str
    minimum: int | None = None
    maximum: int | None = None
    default: str | None = None
    help: str | None = None


class ProviderCatalogVariant(BaseModel):
    """The facets of a catalog entry that one selector value changes."""

    extra_fields: dict[str, str] | None = None
    setup_guide: SetupGuide | None = None


class ProviderCatalogItem(BaseModel):
    provider: str
    name: str
    category: str  # "integration" or "channel"
    integration_category: str = (
        # Cybersecurity-first taxonomy (see INTEGRATION_CATEGORY_GROUPS in the
        # frontend): threat_intelligence, siem_analytics, detection_response,
        # exposure_management, adversarial_validation, identity_access, grc,
        # ai_models, search_news, communication, social_media,
        # business_operations, hr, development, automation, observability
        "business_operations"
    )
    auth_type: str
    supported_auth_types: list[str] | None = (
        None  # When set (e.g. ["api_key", "platform_managed"]), the UI shows an auth mode selector
    )
    fields: list[str]
    # A2A only, absent for every other provider: the directions the creation
    # form offers. Without it on the RESPONSE model Pydantic drops it and the
    # UI cannot offer a direction, which is how an outbound integration
    # becomes uncreatable while every backend test passes.
    a2a_directions: list[str] | None = None
    # The integration is only ever reached through a relay group (a server in
    # the customer's network, LM Studio): creating or saving one without a
    # group is a 422, and the form offers no "Direct" route.
    requires_relay: bool = False
    # Created by an administrator, as a company-managed integration only (the
    # platform email): the form sets and locks the company-managed switch, and
    # the server refuses any other creation.
    company_managed_only: bool = False
    # Entries whose shape depends on one operator choice: ``variant_selector``
    # names the fact (``a2a_direction`` for A2A) and ``variants`` maps each of
    # its values to the facets that differ. The base entry is the default.
    variant_selector: str | None = None
    variants: dict[str, ProviderCatalogVariant] | None = None
    extra_fields: dict[str, str] | None = None
    extra_field_options: dict[str, list[ExtraFieldOption]] | None = (
        None  # dropdown options for extra fields
    )
    # Which fields belong to which choice: ``{field: {other_field: [values]}}``.
    # A field whose condition is unmet is hidden AND omitted when saving, so
    # picking one branch of a mutually exclusive pair clears the other rather
    # than leaving a credential stored and unused. Same warning as above: a key
    # missing from this model is dropped by Pydantic with every backend test
    # still passing.
    extra_field_visible_when: dict[str, dict[str, list[str]]] | None = None
    # The typed control of an extra field (a domain list, a bounded number);
    # absent, the field is a text input. Dropped by Pydantic if missing here.
    extra_field_inputs: dict[str, ExtraFieldInput] | None = None
    description: str | None = None
    logo: str | None = None
    integration_types: list[str] = []
    available_events: list[ProviderEvent] = []
    available_tools: list[ProviderTool] = []
    # A2A only. The provider answers as two questions what ``available_tools``
    # asks as one, because the two directions are not symmetric: what a
    # partner may invoke HERE, and what a binding gives YOUR agents.
    # ``available_tools`` carries the inbound surface; these carry the rest.
    a2a_inbound_agent_tools: list[ProviderTool] | None = None
    a2a_outbound_exposed: list[ProviderTool] | None = None
    element_types: list[ProviderElementType] = (
        []
    )  # selectable sub-elements (channels, repos, etc.)
    webhook_path: str | None = None
    needs_shared_webhook: bool = (
        False  # True for providers with a single app-level webhook (GitHub, Slack)
    )
    supports_assignment_webhooks: bool = (
        False  # True for providers that support per-assignment webhook URLs (e.g. OpenCTI)
    )
    supports_webhook_management: bool = (
        False  # True for providers that support auto-creating webhooks on repos/resources
    )
    setup_guide: SetupGuide | None = None
    has_oauth_defaults: bool = False  # True when admin pre-configured OAuth creds
    has_oauth_extra_config: bool = (
        False  # True when admin also pre-configured extra config (e.g. tenant_id)
    )
    is_custom: bool = False
    #: Qualified as Certified by XTM One (integration_catalog.CERTIFIED_PROVIDERS);
    #: False for every other provider and always for a custom one.
    is_certified: bool = False
    settings_schema: list[ProviderSettingSchemaItem] | None = None
    # Set when the provider's tools come from a document the user imports per
    # integration (``"openapi"``): the creation form then asks for the spec,
    # and tool listings read the integration, not ``available_tools``.
    spec_import: str | None = None
    is_bot: bool = (
        False  # True for bot-type providers with ambient channel access (security-sensitive)
    )
    is_disabled: bool = (
        False  # True when admin has globally disabled this provider from the catalog
    )
    is_registration_only: bool = (
        False  # True for providers only a platform registration adds (integration_catalog.REGISTRATION_ONLY_PROVIDERS)
    )
    # The extra fields still editable on a registration-only integration
    # (integration_catalog.registration_editable_extra_fields).
    registration_editable_extra_fields: list[str] | None = None


class IntegrationSignatureSetting(BaseModel):
    """The Email signature setting of an integration whose tools send email."""

    #: Whether what it sends is signed.
    enabled: bool = True
    #: The signature integration read; None = picked when the tools are built.
    integration_id: str | None = None


class IntegrationResponse(BaseModel):
    id: str
    user_id: str | None = None
    owner_name: str | None = None
    provider: str
    auth_type: str
    status: str
    #: The result of the last connectivity test: ``untested`` (a typed
    #: credential nobody tested), ``ok`` or ``error``; None before any (#11837).
    connection_test_status: str | None = None
    #: Why the status left ``connected`` (the refresh sweep's verdict) and when
    #: the status last changed (#1665); None when nothing was recorded.
    status_reason: str | None = None
    status_reason_at: datetime | None = None
    name: str
    # The suffix its tools carry (``<tool>__<slug>``) when an agent binds
    # several integrations of this provider; a rename never changes it.
    tool_slug: str | None = None
    description: str | None = None
    is_company_managed: bool = False
    is_builtin: bool = False
    is_custom: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    #: Whether the REQUESTING user may edit it - ``helpers.can_manage``, share
    #: tiers included, so the UI never offers an edit the API refuses.
    can_manage: bool = False
    #: Whether an OAuth authorization by the REQUESTING user can land here -
    #: ``helpers.can_authorize_integration``, the rule ``start_oauth`` and the
    #: OAuth callback enforce (company-managed: admins; personal: the owner).
    can_authorize: bool = False
    #: Created and rewritten by a platform registration (XTM One mode), so
    #: read-only on the integration page - ``is_registration_only_provider``,
    #: the list the catalog and the create route read too.
    is_registration_only: bool = False
    #: A2A only: which way the partner relationship points (``inbound`` or
    #: ``outbound``, its settings row); None for every other provider. The
    #: page reads it instead of probing the outbound-only ``/a2a/client``
    #: endpoints, which answer 404 for an inbound one (#11790).
    a2a_direction: str | None = None
    #: Outbound A2A only: the partner credential's state (#12346): ``usable``,
    #: ``last_token_minted_at`` and ``last_error`` ({code, detail, at}). Never a
    #: secret. Set by the single-integration read; None elsewhere. On an
    #: INBOUND oauth_a2a row, read by a manager, it carries
    #: ``last_refused_token_request`` ({at, client_id, reason, what_to_do},
    #: #105) instead.
    a2a_credential: dict | None = None
    tags: list[str] = []
    # Whether this integration's tools pause for human approval.
    #
    # For a registered Suite platform this reports the platform's policy rather
    # than the stored column, because that policy is what the gate enforces.
    # ``approval_managed_by_platform`` says which of the two is answering, so a
    # client can send the reader to the control that actually decides instead of
    # offering one that would be refused.
    requires_approval: bool = True
    approval_managed_by_platform: bool = False
    #: Only for an integration whose tools send email; None otherwise.
    signature: IntegrationSignatureSetting | None = None
    scopes: str | None = None
    required_scopes: str | None = (
        None  # Latest scopes from provider catalog (for drift detection)
    )
    #: The scopes the stored token was granted (token endpoint ``scope``);
    #: None when the provider did not say.
    granted_scopes: list[str] | None = None
    #: Requested scopes the token was not granted (a declined consent box);
    #: None when the grant is unknown.
    missing_scopes: list[str] | None = None
    # OAuth endpoints (public URLs, not secrets). Editable for integrations
    # that own them — OpenAPI ones pre-fill them from their spec.
    authorize_url: str | None = None
    token_url: str | None = None
    has_client_id: bool = False
    has_client_secret: bool = False
    has_access_token: bool = False
    has_api_key: bool = False
    has_extra_config: bool = False
    # Whether this shared OAuth integration shares its owner's connected
    # account with everyone it is shared with: recorded when it
    # is shared, cleared by the opt-out or when it stops being shared.
    oauth_share_acknowledged: bool = False
    # The stored OAuth token was connected by someone else than the owner - an
    # owner transfer keeps the previous owner's account. The page says
    # so, naming them when the detail route resolved the name.
    oauth_connected_by_other: bool = False
    oauth_token_owner_name: str | None = None
    #: The token's recorded owner when it is not the integration's owner: with
    #: the owner and admins, the one person who may consent to share it.
    oauth_token_owner_id: str | None = None
    #: Whose account the people it is shared with act with, the share
    #: choice above as stored: ``shared`` (the connected account) or
    #: ``per_user`` (shared without it: each person authorizes their own).
    credential_mode: Literal["shared", "per_user"] = "shared"
    #: ``per_user`` only: whether the REQUESTING user acts with their own
    #: account here rather than the integration's (everyone but its holder).
    uses_own_credential: bool = False
    #: ``per_user`` only: the REQUESTING user's own account on it, if any.
    my_credential: "UserCredentialStatus | None" = None
    #: ``per_user`` only, for those who manage it: how many people connected
    #: their own account.
    connected_user_count: int | None = None
    # Whether it runs on the admin's OAuth app: the recorded column, or
    # for a row created before it existed, its stored client id compared with
    # the provider's current OAuth default. None only when that cannot be
    # read, which the frontend answers with the provider-wide flag.
    uses_admin_oauth_credentials: bool | None = None
    extra_config: dict[str, Any] | None = (
        None  # Decrypted extra config values (e.g. base_url)
    )
    #: The secret fields read from a vault, and their recent failures.
    vault_secrets: VaultSecretStatus | None = None
    scope_config: dict | None = None  # Scope disambiguation config (read-only)
    # The relay group its requests go through; None = sent directly.
    relay_group_id: str | None = None
    call_count: int = 0
    last_called_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UserCredentialStatus(BaseModel):
    """A person's own account on a ``per_user`` integration: never its secret."""

    status: str
    has_access_token: bool = False
    token_expires_at: datetime | None = None
    connected_at: datetime | None = None
    updated_at: datetime | None = None


class IntegrationUserCredentialItem(UserCredentialStatus):
    """One connected person, as the integration's managers see it."""

    user_id: str
    user_name: str | None = None
    user_email: str | None = None


class CreateIntegrationRequest(BaseModel):
    id: str | None = (
        None  # Optional pre-generated UUID (frontend generates for webhook URLs)
    )
    provider: str
    name: str
    description: str | None = None
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    tags: list[str] | None = None
    auth_type: str | None = None  # auto-detected from provider catalog
    # A2A only: which way this partner relationship points. Exactly one, never
    # both — the directions need different credentials and confer opposite
    # capabilities (FR-055). Required for an A2A provider, ignored otherwise.
    a2a_direction: str | None = None
    # OAuth app credentials
    client_id: str | None = None
    client_secret: str | None = None
    # API key / bot token
    api_key: str | None = None
    # Extra config (provider-specific)
    extra_config: dict[str, Any] | None = None
    # OAuth URL overrides
    authorize_url: str | None = None
    token_url: str | None = None
    scopes: str | None = None
    # The relay group its requests go through: one the writer can see.
    relay_group_id: str | None = None
    # Whether a shared OAuth integration shares its connected account.
    # ``true`` shares it: the people it is shared with act using the owner's
    # token. ``false`` shares the integration without it - the stored account
    # stays the owner's, and everyone else authorizes their own. Omitted (``null``) keeps the
    # baseline meaning (decision 15c): a share of an account nobody consented
    # for removes the token, as sharing always did. The UI always sends it.
    keep_oauth_credentials_on_share: bool | None = None
    # OpenAPI integrations only (required there): the spec to import and the
    # choices made on its preview. ``auth_type`` is derived from the chosen
    # authentication option, never taken from the request.
    openapi: OpenAPIImportOptions | None = None

    _fit_columns = column_length_validator(Integration, "provider", *_INTEGRATION_COLUMN_FIELDS)
    _vault_references = vault_reference_validator("client_secret", "api_key")
    _no_vault_references = no_vault_reference_validator("client_id", "extra_config")


class UpdateIntegrationRequest(BaseModel):
    name: str | None = None
    # The tool-name suffix: lowercase letters, digits and underscores,
    # unique per provider within the owner scope. Renaming the integration
    # (``name``) never changes it; omitted it changes nothing. The column's
    # length here; the stricter rules are ``tool_slug_error``'s.
    tool_slug: str | None = Field(default=None, max_length=32)
    description: str | None = None
    auth_type: str | None = None
    # A2A only. Changing it is refused while the integration is bound to any
    # agent (FR-057), and clears the previous direction's credentials on a
    # permitted change (FR-058).
    a2a_direction: str | None = None
    is_company_managed: bool | None = None
    # Owner (``user_id``) transfer — admin only. Ignored when omitted/unchanged.
    user_id: str | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    tags: list[str] | None = None
    # One approval toggle for the whole integration. Per-tool granularity
    # is deferred: there are ~2,350 integration tool definitions across ~110
    # providers, so per-tool curation waits until the flow is proven end to end
    # on one of our own integrations.
    requires_approval: bool | None = None
    # The Email signature setting, on an integration whose tools send
    # email only. ``signature_integration_id`` sent as null (or empty) picks it
    # automatically again; omitted it changes nothing.
    signature_enabled: bool | None = None
    signature_integration_id: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    api_key: str | None = None
    extra_config: dict[str, Any] | None = None
    authorize_url: str | None = None
    token_url: str | None = None
    scopes: str | None = None
    # The relay group its requests go through. Sent as null it routes
    # the integration directly again; omitted it changes nothing.
    relay_group_id: str | None = None
    # See ``CreateIntegrationRequest.keep_oauth_credentials_on_share``. On an
    # update that does not change sharing, omitting it changes nothing.
    keep_oauth_credentials_on_share: bool | None = None

    _fit_columns = column_length_validator(Integration, *_INTEGRATION_COLUMN_FIELDS)
    _vault_references = vault_reference_validator("client_secret", "api_key")
    _no_vault_references = no_vault_reference_validator("client_id", "extra_config")


class IntegrationBundleSettingResponse(BaseModel):
    key: str
    label: str
    type: str
    required: bool
    placeholder: str | None = None
    options: list[dict[str, str]] | None = None


class IntegrationBundleTestConnectionResponse(BaseModel):
    mode: str
    path: str | None = None
    method: str | None = None


class IntegrationBundleImportValidationResponse(BaseModel):
    format: str
    version: str
    name: str
    slug: str
    normalized_provider: str
    description: str
    author: str
    auth_type: str
    icon: str | None = None
    settings: list[IntegrationBundleSettingResponse]
    supports_polling: bool
    supports_webhook: bool
    test_connection: IntegrationBundleTestConnectionResponse | None = None
    has_integration_instance: bool = False
    integration_instance_name: str | None = None
    checksum_sha256: str


class IntegrationBundleImportResponse(BaseModel):
    provider: str
    version: str
    upgraded: bool
    name: str
    integration_created: bool = False
    integration_id: str | None = None
    integration_name: str | None = None


class CustomIntegrationProviderStatusResponse(BaseModel):
    provider: str
    enabled: bool



class PerUserProviderReadiness(BaseModel):
    """Whether a provider is safe to bind in "resolved per user at runtime" mode.

    A per-user binding uses the acting user's OWN integration of the provider,
    falling back to a genuine company-wide instance. Without either, the user
    gets no tools at all for that provider — and the agent config UI must say so
    up front rather than implying a company-wide integration exists.
    """

    provider: str
    #: A connected company-managed / platform-managed instance exists, so users
    #: without their own connection still resolve to something company-owned.
    has_company_wide_fallback: bool
    company_wide_instance_count: int
    #: The requesting user has their own connected instance of this provider.
    user_has_own_instance: bool
    #: Per-user resolution would find an integration for the requesting user.
    resolvable_for_user: bool
    #: Owner names of connected PERSONAL instances that are group-shared while
    #: no company-wide instance exists — the "a colleague's account is acting as
    #: the shared one" state. These are never used by per-user resolution; they
    #: are reported so admins can promote one to company-managed (or accept that
    #: every user must connect their own).
    de_facto_shared_owners: list[str] = []


class PerUserReadinessResponse(BaseModel):
    providers: list[PerUserProviderReadiness]


IntegrationResponse.model_rebuild()
