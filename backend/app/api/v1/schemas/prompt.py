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

"""Pydantic schemas for prompt API routes."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.api.v1.schemas.column_limits import column_length_validator
from app.models.prompt import Prompt


class PromptCreate(BaseModel):
    name: str
    description: str = ""
    content: str = ""
    tags: list[str] | None = None
    hide_from_chat: bool = False
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None

    _fit_columns = column_length_validator(Prompt, "name")


class PromptUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    hide_from_chat: bool | None = None
    is_company_managed: bool | None = None
    # Owner (``user_id``) transfer — admin only. Ignored when omitted/unchanged.
    user_id: str | None = None
    is_public_catalog: bool | None = None
    commit_message: str | None = Field(default=None, max_length=500)
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None

    _fit_columns = column_length_validator(Prompt, "name")


class PromptResponse(BaseModel):
    id: str
    user_id: str | None = None
    owner_name: str | None = None
    name: str
    description: str
    content: str
    tags: list[str] = []
    hide_from_chat: bool = False
    is_company_managed: bool = False
    is_public_catalog: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    #: Whether the REQUESTING user may edit it - ``helpers.can_manage``, share
    #: tiers included, so the UI never offers an edit the API refuses.
    can_manage: bool = False
    created_at: str
    updated_at: str


class ImportPromptRequest(BaseModel):
    """Import a prompt from an export bundle."""

    version: int
    export_type: str = "prompt"
    prompt: dict
