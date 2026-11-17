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

"""Pydantic schemas for channel bot endpoints.

Channel bots are always company-managed (platform-wide resources), so the
group-sharing fields other entities carry (``shared_group_ids`` /
``shared_group_permissions``) are intentionally absent — they were previously
accepted on create/update but never persisted, a trap for API consumers.
"""

from pydantic import BaseModel

from app.api.v1.schemas.column_limits import column_length_validator
from app.api.v1.schemas.integration import (
    ProviderTool,
    SetupGuide,
)
from app.api.v1.schemas.vault_references import (
    VaultSecretStatus,
    no_vault_reference_validator,
    vault_reference_validator,
)
from app.models.channel_bot import ChannelBot


class ChannelBotCatalogItem(BaseModel):
    provider: str
    name: str
    fields: list[str]
    extra_fields: dict[str, str] | None = None
    description: str | None = None
    available_tools: list[ProviderTool] = []
    webhook_path: str | None = None
    setup_guide: SetupGuide | None = None


class ChannelBotResponse(BaseModel):
    id: str
    user_id: str | None = None
    owner_name: str | None = None
    is_company_managed: bool = True
    provider: str
    name: str
    status: str
    has_api_key: bool = False
    has_client_id: bool = False
    has_client_secret: bool = False
    has_extra_config: bool = False
    allow_anonymous: bool = False
    allow_private: bool = True
    only_mention: bool = False
    #: Whether this bot's tools pause for approval.
    requires_approval: bool = True
    allowed_receive_channels: list[dict] | None = None
    allowed_publish_channels: list[dict] | None = None
    #: The secret fields read from a vault, and their recent failures.
    vault_secrets: VaultSecretStatus | None = None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class CreateChannelBotRequest(BaseModel):
    provider: str
    name: str
    is_company_managed: bool = True
    api_key: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    extra_config: dict[str, str] | None = None
    allow_anonymous: bool = False
    allow_private: bool = True
    only_mention: bool = False
    #: Whether this bot's tools pause for approval. Per bot, not per
    #: provider: two Slack bots can disagree.
    requires_approval: bool = True
    allowed_receive_channels: list[dict] | None = None
    allowed_publish_channels: list[dict] | None = None

    # ``provider`` is allow-listed by the route before anything is written.
    _fit_columns = column_length_validator(ChannelBot, "name")
    _vault_references = vault_reference_validator("api_key", "client_secret", "extra_config")
    _no_vault_references = no_vault_reference_validator("client_id")


class UpdateChannelBotRequest(BaseModel):
    name: str | None = None
    is_company_managed: bool | None = None
    api_key: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    extra_config: dict[str, str] | None = None
    allow_anonymous: bool | None = None
    allow_private: bool | None = None
    only_mention: bool | None = None
    requires_approval: bool | None = None
    allowed_receive_channels: list[dict] | None = None
    allowed_publish_channels: list[dict] | None = None

    _fit_columns = column_length_validator(ChannelBot, "name")
    _vault_references = vault_reference_validator("api_key", "client_secret", "extra_config")
    _no_vault_references = no_vault_reference_validator("client_id")
