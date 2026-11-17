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

"""Pydantic schemas for the Deep Investigation pack builder routes.

The pack's content (`PackFields`: name, subject types, steps, settings) is
validated in the body model, so a bad definition is a 422 and never reaches a
handler. PUT replaces the whole content: the builder always saves the method
it shows, and a step's subject types are checked against the pack's together.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.investigation_pack_assistant import MAX_DRAFT_CHARS, draft_text
from app.services.investigation_pack_builder import PackFields


class _Sharing(BaseModel):
    is_company_managed: bool | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None


class InvestigationPackCreate(PackFields, _Sharing):
    model_config = ConfigDict(extra="forbid")

    commit_message: str | None = Field(default=None, max_length=500)


class InvestigationPackUpdate(PackFields, _Sharing):
    model_config = ConfigDict(extra="forbid")

    commit_message: str | None = Field(default=None, max_length=500)
    # Owner (``user_id``) transfer - admin only. Ignored when omitted.
    user_id: str | None = None


class InvestigationPackResponse(BaseModel):
    id: str
    slug: str
    user_id: str | None = None
    owner_name: str | None = None
    name: str
    description: str
    subject_types: list[str]
    recommended: bool
    max_iterations: int
    definition: dict
    version_number: int
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    #: Whether the REQUESTING user may edit it (``helpers.can_manage``).
    can_manage: bool = False
    created_at: str
    updated_at: str


class InvestigationPackVersionItem(BaseModel):
    version_number: int
    author_id: str | None = None
    author_name: str | None = None
    commit_message: str | None = None
    created_at: str
    is_current: bool


class InvestigationPackVersionDetail(InvestigationPackVersionItem):
    subject_types: list[str]
    definition: dict


class BuiltinPackItem(BaseModel):
    """A built-in pack, offered to duplicate."""

    slug: str
    label: str
    description: str
    subject_types: list[str]


class PackPreviewRequest(BaseModel):
    """What a pack would do on a subject, for the caller: a saved pack
    (``pack_id``) or the content being edited (``pack``)."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=16_000)
    pack_id: str | None = None
    pack: PackFields | None = None
    options: dict[str, str] | None = None


class PreviewSource(BaseModel):
    name: str
    kind: str


class PreviewAction(BaseModel):
    slug: str
    label: str
    description: str
    icon: str
    produces_report: bool
    servable: bool
    sources: list[PreviewSource]


class PackPreviewResponse(BaseModel):
    subject: str
    subject_type: str
    reachable: bool
    actions: list[PreviewAction]
    #: The pack's description for the caller, naming what is not available.
    description: str


class PackAssistTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class PackAssistRequest(BaseModel):
    """One round with the builder's assistant: nothing is stored."""

    model_config = ConfigDict(extra="forbid")

    instruction: str = Field(min_length=3, max_length=4000)
    #: The draft as edited, possibly not yet valid: the assistant repairs it.
    #: At most `MAX_DRAFT_CHARS` as the JSON the model reads (`draft_text`):
    #: a longer one is a 422, never cut into JSON the model cannot keep.
    current: dict | None = None
    history: list[PackAssistTurn] = Field(default_factory=list, max_length=20)

    @field_validator("current")
    @classmethod
    def _current_within_bound(cls, current: dict | None) -> dict | None:
        text = draft_text(current)
        if text is not None and len(text) > MAX_DRAFT_CHARS:
            raise ValueError(
                "This pack is too large for the assistant. Shorten its descriptions or remove steps, then try again."
            )
        return current


class PackAssistResponse(BaseModel):
    reply: str
    #: A pack `PackFields` accepts, to apply to the draft.
    pack: dict
