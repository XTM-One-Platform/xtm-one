# SPDX-License-Identifier: Apache-2.0

"""Schemas of the OpenRouter console (``/integrations/{id}/openrouter``)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.v1.schemas.settings import LLMProviderResponse
from app.integrations.providers.openrouter import is_valid_model_id
from app.llm.model_limits import MAX_MODEL_ID_LENGTH, is_model_id


def _model_id(value: str) -> str:
    if not is_valid_model_id(value):
        raise ValueError("not an OpenRouter model id (author/model)")
    return value


#: An OpenRouter model id, validated in the body model so a bad one is a 422.
ModelId = Annotated[str, Field(min_length=3, max_length=320), AfterValidator(_model_id)]


def _offered_model_id(value: str) -> str:
    if not is_model_id(value):
        raise ValueError("not a model id")
    return value


#: A model a saved provider offers: any model id (``is_model_id``: non-blank,
#: at most 255 characters, the bound of a context-window key), not only an
#: OpenRouter one, since a provider adopted from Settings keeps the models it
#: had there and a save sends them back. A model the save adds must be in the
#: integration's catalog (the route checks it), which holds OpenRouter ids only.
OfferedModelId = Annotated[str, Field(max_length=MAX_MODEL_ID_LENGTH), AfterValidator(_offered_model_id)]
LimitReset = Literal["daily", "weekly", "monthly"]

#: The most models one provider config is given from the console.
MAX_SELECTED_MODELS = 500


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


class FreeModelRequests(BaseModel):
    limit: int | None = None
    remaining: int | None = None
    used: int | None = None


class KeyInfo(BaseModel):
    label: str | None = None
    limit: float | None = None
    limit_remaining: float | None = None
    limit_reset: LimitReset | None = None
    include_byok_in_limit: bool = False
    usage: float = 0.0
    usage_daily: float = 0.0
    usage_weekly: float = 0.0
    usage_monthly: float = 0.0
    byok_usage: float = 0.0
    byok_usage_daily: float = 0.0
    byok_usage_weekly: float = 0.0
    byok_usage_monthly: float = 0.0
    is_free_tier: bool = False
    is_management_key: bool = False
    expires_at: str | None = None
    free_model_daily_requests: FreeModelRequests | None = None
    allowed_data_regions: list[str] = Field(default_factory=list)


class Credits(BaseModel):
    total_credits: float
    total_usage: float
    remaining: float


class ConsoleError(BaseModel):
    """Why one part of the overview could not be read (a fixed sentence)."""

    code: str
    message: str


class LinkableProvider(BaseModel):
    id: str
    display_name: str | None = None
    default_model: str | None = None


class OverviewResponse(BaseModel):
    key: KeyInfo | None = None
    key_error: ConsoleError | None = None
    credits: Credits | None = None
    credits_error: ConsoleError | None = None
    has_api_key: bool
    has_management_key: bool
    #: The viewer may create, edit and delete the account's API keys.
    can_manage_keys: bool
    #: The viewer may feed the platform LLM provider from this integration.
    can_link_provider: bool
    #: Why the integration may not feed it, when it may not (any viewer).
    link_refusal: str | None = None
    llm_providers: list[LLMProviderResponse] = Field(default_factory=list)
    #: OpenRouter configs made in Settings that this integration may adopt
    #: (listed for a viewer who may link only).
    adoptable_providers: list[LinkableProvider] = Field(default_factory=list)


class ActivityRow(BaseModel):
    date: str
    model: str
    provider_name: str
    usage: float
    byok_usage_inference: float
    requests: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    cached_tokens: int


class ActivityResponse(BaseModel):
    rows: list[ActivityRow]


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class ModelPricing(BaseModel):
    """USD per million tokens, or per unit for ``request`` / ``image`` / ``web_search``."""

    prompt: float | None = None
    completion: float | None = None
    input_cache_read: float | None = None
    input_cache_write: float | None = None
    internal_reasoning: float | None = None
    request: float | None = None
    image: float | None = None
    web_search: float | None = None


class CatalogModel(BaseModel):
    id: str
    name: str
    author: str
    created: str | None = None
    description: str | None = None
    context_length: int | None = None
    max_completion_tokens: int | None = None
    is_moderated: bool = False
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    pricing: ModelPricing
    variable_pricing: bool = False
    is_free: bool = False
    supports_tools: bool = False
    supports_reasoning: bool = False
    supports_structured_outputs: bool = False
    supported_parameters: list[str] = Field(default_factory=list)
    knowledge_cutoff: str | None = None
    expiration_date: str | None = None


class CatalogResponse(BaseModel):
    kind: Literal["chat", "embeddings"]
    models: list[CatalogModel]


class EndpointPricing(BaseModel):
    prompt: float | None = None
    completion: float | None = None
    input_cache_read: float | None = None


class ModelEndpoint(BaseModel):
    provider_name: str
    tag: str | None = None
    quantization: str | None = None
    context_length: int | None = None
    max_completion_tokens: int | None = None
    status: int | None = None
    uptime_last_30m: float | None = None
    uptime_last_1d: float | None = None
    latency_p50_ms: float | None = None
    throughput_p50: float | None = None
    supports_implicit_caching: bool = False
    supports_tools: bool = False
    pricing: EndpointPricing


class EndpointsResponse(BaseModel):
    model_id: str
    endpoints: list[ModelEndpoint]


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

KeyUse = Literal["integration", "llm_provider"]


class ApiKey(BaseModel):
    hash: str
    name: str
    label: str
    disabled: bool
    limit: float | None = None
    limit_remaining: float | None = None
    limit_reset: LimitReset | None = None
    include_byok_in_limit: bool = False
    usage: float = 0.0
    usage_daily: float = 0.0
    usage_weekly: float = 0.0
    usage_monthly: float = 0.0
    created_at: str | None = None
    updated_at: str | None = None
    expires_at: str | None = None
    #: What on this platform runs on the key: deleting or disabling it is
    #: refused while this is not empty.
    in_use: list[KeyUse] = Field(default_factory=list)


class KeysResponse(BaseModel):
    keys: list[ApiKey]
    truncated: bool = False


def _clean_name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("must not be blank")
    return cleaned


class CreateKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    #: Spending limit in USD; omitted or null = no limit.
    limit: float | None = Field(default=None, ge=0, le=1_000_000)
    limit_reset: LimitReset | None = None
    include_byok_in_limit: bool = False
    #: When the key stops working; must be in the future.
    expires_at: datetime | None = None

    @field_validator("name")
    @classmethod
    def _clean(cls, value: str) -> str | None:
        return _clean_name(value)

    @field_validator("expires_at")
    @classmethod
    def _future(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("must carry a time zone")
        if value <= datetime.now(UTC):
            raise ValueError("must be in the future")
        return value

    def to_openrouter(self) -> dict:
        body: dict = {"name": self.name, "include_byok_in_limit": self.include_byok_in_limit}
        if self.limit is not None:
            body["limit"] = self.limit
        if self.limit_reset is not None:
            body["limit_reset"] = self.limit_reset
        if self.expires_at is not None:
            # OpenRouter wants UTC with seconds and refuses minute precision.
            body["expires_at"] = self.expires_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return body


class UpdateKeyRequest(BaseModel):
    """Only the fields sent are changed; ``limit: null`` removes the limit."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    disabled: bool | None = None
    limit: float | None = Field(default=None, ge=0, le=1_000_000)
    limit_reset: LimitReset | None = None
    include_byok_in_limit: bool | None = None

    @field_validator("name")
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return _clean_name(value)

    @model_validator(mode="after")
    def _something(self) -> UpdateKeyRequest:
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        for field in ("name", "disabled", "include_byok_in_limit"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self

    def to_openrouter(self) -> dict:
        return {field: getattr(self, field) for field in self.model_fields_set}


class CreatedKeyResponse(BaseModel):
    key: ApiKey
    #: The key itself. OpenRouter shows it once: it is not stored here either.
    secret: str


# ---------------------------------------------------------------------------
# Platform LLM provider
# ---------------------------------------------------------------------------


def _unique(models: list[str]) -> list[str]:
    if len(set(models)) != len(models):
        raise ValueError("lists a model twice")
    return models


class LinkProviderRequest(BaseModel):
    """Create the platform OpenRouter provider, or adopt ``provider_config_id``."""

    model_config = ConfigDict(extra="forbid")

    provider_config_id: uuid.UUID | None = None
    display_name: str | None = Field(default=None, max_length=120)
    available_models: Annotated[list[ModelId], AfterValidator(_unique)] = Field(
        default_factory=list, max_length=MAX_SELECTED_MODELS
    )
    default_model: ModelId | None = None

    @model_validator(mode="after")
    def _consistent(self) -> LinkProviderRequest:
        if self.provider_config_id is not None and (
            self.available_models or self.default_model or self.display_name
        ):
            raise ValueError("adopting a provider keeps its own models and name")
        if self.default_model and self.default_model not in self.available_models:
            raise ValueError("default_model must be one of available_models")
        return self


class ProviderModelsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_models: Annotated[list[OfferedModelId], AfterValidator(_unique)] = Field(
        max_length=MAX_SELECTED_MODELS
    )
    default_model: OfferedModelId | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ProviderModelsRequest:
        if self.default_model and self.default_model not in self.available_models:
            raise ValueError("default_model must be one of available_models")
        return self

