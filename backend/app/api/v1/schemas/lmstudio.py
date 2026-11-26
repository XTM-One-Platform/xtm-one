# SPDX-License-Identifier: Apache-2.0

"""Schemas of the LM Studio console (``/integrations/{id}/lmstudio``)."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from app.api.v1.schemas.openrouter import (
    MAX_SELECTED_MODELS,
    ConsoleError,
    LinkableProvider,
    ProviderModelsRequest,
)
from app.api.v1.schemas.settings import LLMProviderResponse
from app.integrations.providers.lmstudio import (
    QUANTIZATION_RE,
    is_valid_download_source,
    is_valid_model_key,
)
from app.llm.model_limits import MAX_MODEL_ID_LENGTH, is_model_id

__all__ = [
    "ConsoleError",
    "DownloadJob",
    "DownloadRequest",
    "LinkProviderRequest",
    "LinkableProvider",
    "LoadRequest",
    "LoadResponse",
    "LoadedInstance",
    "ModelsResponse",
    "OverviewResponse",
    "ProviderModelsRequest",
    "ServerModel",
    "UnloadRequest",
    "UnloadResponse",
]


def _model_key(value: str) -> str:
    # A model a provider offers is also a context-window key (``is_model_id``).
    if not is_valid_model_key(value) or not is_model_id(value):
        raise ValueError("not an LM Studio model key")
    return value


#: An LM Studio model key or instance id, validated in the body model so a bad one is a 422.
ModelKey = Annotated[str, Field(min_length=1, max_length=MAX_MODEL_ID_LENGTH), AfterValidator(_model_key)]


def _download_source(value: str) -> str:
    if not is_valid_download_source(value):
        raise ValueError("not an LM Studio catalog id (owner/model) or a Hugging Face https link")
    return value


def _quantization(value: str) -> str:
    if not QUANTIZATION_RE.fullmatch(value):
        raise ValueError("not a quantization name (for example Q4_K_M)")
    return value


def _unique(models: list[str]) -> list[str]:
    if len(set(models)) != len(models):
        raise ValueError("lists a model twice")
    return models


ModelType = Literal["llm", "embedding", "decision"]
DownloadStatus = Literal["downloading", "paused", "completed", "failed", "already_downloaded"]


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


class OverviewResponse(BaseModel):
    """How the integration reaches its server, and the platform provider it feeds."""

    #: The integration is routed through a relay group (``False``: reached directly).
    routed: bool
    relay_group_id: str | None = None
    #: The server URL as normalized, or ``None`` when unset or invalid.
    base_url: str | None = None
    #: Why the server cannot be called, when it cannot (no route, no URL...).
    server_refusal: str | None = None
    has_api_token: bool
    #: The viewer may load, unload and download models.
    can_manage_server: bool
    #: The viewer may feed the platform LLM provider from this integration.
    can_link_provider: bool
    #: Why the integration may not feed it, when it may not (any viewer).
    link_refusal: str | None = None
    llm_providers: list[LLMProviderResponse] = Field(default_factory=list)
    #: Unlinked LM Studio configs this integration may adopt (for a viewer who may link only).
    adoptable_providers: list[LinkableProvider] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class LoadedInstance(BaseModel):
    id: str
    context_length: int | None = None
    flash_attention: bool | None = None
    parallel: int | None = None


class ServerModel(BaseModel):
    key: str
    type: ModelType
    display_name: str
    publisher: str | None = None
    architecture: str | None = None
    format: str | None = None
    quantization: str | None = None
    bits_per_weight: float | None = None
    size_bytes: int | None = None
    params: str | None = None
    max_context_length: int | None = None
    vision: bool = False
    tool_use: bool = False
    reasoning_options: list[str] = Field(default_factory=list)
    description: str | None = None
    loaded_instances: list[LoadedInstance] = Field(default_factory=list)
    #: The platform provider fed by this integration offers this model.
    offered: bool = False


class ModelsResponse(BaseModel):
    models: list[ServerModel]


class LoadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: ModelKey
    #: Tokens; LM Studio refuses more than the model's maximum.
    context_length: int | None = Field(default=None, ge=256, le=10_000_000)
    flash_attention: bool | None = None
    eval_batch_size: int | None = Field(default=None, ge=1, le=65_536)
    offload_kv_cache_to_gpu: bool | None = None


class LoadResponse(BaseModel):
    instance_id: str
    type: ModelType
    load_time_seconds: float | None = None


class UnloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance_id: ModelKey


class UnloadResponse(BaseModel):
    instance_id: str


class DownloadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: An LM Studio catalog id (``openai/gpt-oss-20b``) or a Hugging Face link.
    model: Annotated[str, Field(min_length=1, max_length=500), AfterValidator(_download_source)]
    #: Only for a Hugging Face link (``Q4_K_M``).
    quantization: Annotated[str, Field(max_length=40), AfterValidator(_quantization)] | None = None

    @model_validator(mode="after")
    def _consistent(self) -> DownloadRequest:
        if self.quantization is not None and not self.model.startswith("https://"):
            raise ValueError("a quantization is only chosen for a Hugging Face link")
        return self


class DownloadJob(BaseModel):
    job_id: str | None = None
    status: DownloadStatus
    total_size_bytes: int | None = None
    downloaded_bytes: int | None = None
    bytes_per_second: float | None = None
    started_at: str | None = None
    estimated_completion: str | None = None
    completed_at: str | None = None


# ---------------------------------------------------------------------------
# Platform LLM provider
# ---------------------------------------------------------------------------


class LinkProviderRequest(BaseModel):
    """Create the platform LM Studio provider, or adopt ``provider_config_id``."""

    model_config = ConfigDict(extra="forbid")

    provider_config_id: uuid.UUID | None = None
    display_name: str | None = Field(default=None, max_length=120)
    available_models: Annotated[list[ModelKey], AfterValidator(_unique)] = Field(
        default_factory=list, max_length=MAX_SELECTED_MODELS
    )
    default_model: ModelKey | None = None

    @model_validator(mode="after")
    def _consistent(self) -> LinkProviderRequest:
        if self.provider_config_id is not None and (
            self.available_models or self.default_model or self.display_name
        ):
            raise ValueError("adopting a provider keeps its own models and name")
        if self.default_model and self.default_model not in self.available_models:
            raise ValueError("default_model must be one of available_models")
        return self
