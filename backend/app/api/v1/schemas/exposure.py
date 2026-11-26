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

"""Schemas for the resource exposure routes: who can use a resource a
person lends to others, and through what. Read-only; see
``app.services.resource_exposure``."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

ResourceKind = Literal[
    "integration", "mcp_server", "custom_tool", "channel_bot", "variable", "knowledge_base", "skill", "prompt"
]
PreviewEntityType = Literal[
    "integration",
    "mcp_server",
    "custom_tool",
    "channel_bot",
    "variable",
    "agent",
    "assignment",
    "flow",
    "knowledge_base",
    "skill",
    "prompt",
    "general_assistant",
    "group",
    "platform_mcp",
]


class AudiencePerson(BaseModel):
    id: str
    name: str


class AudienceGroup(BaseModel):
    id: str
    name: str | None = None
    member_count: int = 0


class AudienceOut(BaseModel):
    """Who a path reaches: named people and groups (bounded), a head count, and
    the audiences no account names."""

    everyone: bool = False
    people_count: int = 0
    people: list[AudiencePerson] = Field(default_factory=list)
    groups: list[AudienceGroup] = Field(default_factory=list)
    public_link: bool = False
    anonymous_channels: list[str] = Field(default_factory=list)
    mcp_key_holders: bool = False
    a2a_partners: bool = False
    webhook_callers: bool = False


class CarrierOut(BaseModel):
    """The entity a path goes through. ``id`` and ``name`` are withheld when
    the viewer cannot open it; its owner is still named."""

    type: str
    id: str | None = None
    name: str | None = None
    visible: bool = False
    is_company_managed: bool = False
    owner_name: str | None = None


class SecretOut(BaseModel):
    """What the resource holds that its audience acts with, never the value."""

    kind: str
    holder_id: str | None = None
    holder_name: str | None = None
    is_company: bool = False
    account_shared: bool = True
    scopes: list[str] = Field(default_factory=list)
    is_yours: bool | None = None


class ResourceRefOut(BaseModel):
    kind: str
    id: str | None = None
    name: str | None = None
    provider: str | None = None
    auth_type: str | None = None
    is_company_managed: bool = False
    owner_id: str | None = None
    owner_name: str | None = None


class ExposurePathOut(BaseModel):
    kind: str
    reasons: list[str] = Field(default_factory=list)
    restricted: bool = False
    entity: CarrierOut | None = None
    parent: CarrierOut | None = None
    usable: bool = True
    #: An agent bound to an integration its owner can no longer see: its runs
    #: refuse the integration's tools until the access or the binding changes.
    orphaned: bool = False
    notes: list[str] = Field(default_factory=list)
    audience: AudienceOut
    #: Who acts with what the resource stores through this path (carrier paths
    #: only): ``audience``, except an agent's chat audience where its chats use
    #: their own integration.
    lent: AudienceOut | None = None
    #: The part of ``audience`` the resource's own shares do not already reach
    #: (carrier paths only).
    beyond_direct: AudienceOut | None = None


class ResourceExposureOut(BaseModel):
    resource: ResourceRefOut
    secret: SecretOut
    direct: AudienceOut
    paths: list[ExposurePathOut]
    reached: AudienceOut
    #: The agents bound to the integration whose owner can no longer see it.
    orphaned_agents: list[CarrierOut] = Field(default_factory=list)
    truncated: bool = False


class ExposureSummaryOut(BaseModel):
    id: str
    kind: str
    secret_kind: str
    is_company: bool = False
    account_shared: bool = True
    ways: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    public_link: bool = False


class ExposureSummariesOut(BaseModel):
    summaries: dict[str, ExposureSummaryOut]
    truncated: bool = False


class ExposurePreviewRequest(BaseModel):
    """A proposed change: only what it sets is read (see
    ``resource_exposure._PROPOSABLE``). ``entity_id`` omitted = creating one;
    ``parent_id`` is an assignment's agent when creating it. ``group`` (its
    members) and ``platform_mcp`` (the agents its key reaches) are for
    administrators."""

    entity_type: PreviewEntityType
    entity_id: str | None = None
    parent_id: str | None = None
    change: dict[str, Any] = Field(default_factory=dict)


class PreviewCarrierOut(BaseModel):
    type: str
    id: str | None = None
    name: str | None = None


class ExposurePreviewItemOut(BaseModel):
    """One resource the change lends to new people (``gained``) or takes from
    people who could use it (``lost``)."""

    resource: ResourceRefOut
    secret: SecretOut
    via: PreviewCarrierOut | None = None
    gained: AudienceOut
    lost: AudienceOut
    notes: list[str] = Field(default_factory=list)
    opt_outs: list[str] = Field(default_factory=list)
    #: The agents bound to the integration whose owner the change takes it
    #: from: their runs would refuse its tools (an orphaned binding).
    orphaned_agents: list[CarrierOut] = Field(default_factory=list)


class ExposurePreviewOut(BaseModel):
    items: list[ExposurePreviewItemOut]
    requires_confirmation: bool = False
    truncated: bool = False
