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

"""Pydantic schemas for the template container API routes."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.template_container import MAX_ASSET_DESCRIPTION_CHARS


class TemplateAssetResponse(BaseModel):
    """One asset (font, image, logo, …) bound to a template container."""

    id: str
    filename: str
    content_type: str
    size: int
    asset_role: str | None = None
    #: What the asset depicts — the caption an agent picks it by.
    description: str | None = None
    #: Changes when the binary is replaced in place (the download's ``ETag``) -
    #: the key a client's own cache of the bytes follows.
    version: str
    #: The stored geometry of an image asset: ``{w, h, trim[, ink]}``,
    #: what a logo wall crops and sizes the mark by. ``None`` when not read.
    image_meta: dict | None = None
    created_at: str


class TemplateContainerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    #: ``presentation`` (the default) or ``report`` - the closed choice,
    #: refused for anything else by the route (422).
    kind: str = Field(default="presentation", max_length=50)
    tags: list[str] | None = None
    theme: str | None = Field(default=None, max_length=100)
    theme_config: dict | None = None
    aspect_ratio: str | None = Field(default=None, max_length=30)
    layouts: list[dict] | None = None
    extra_metadata: dict | None = None
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None


class TemplateContainerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    #: ``presentation`` or ``report``; ``None`` leaves the kind as it is.
    kind: str | None = Field(default=None, max_length=50)
    tags: list[str] | None = None
    theme: str | None = Field(default=None, max_length=100)
    theme_config: dict | None = None
    aspect_ratio: str | None = Field(default=None, max_length=30)
    layouts: list[dict] | None = None
    extra_metadata: dict | None = None
    is_company_managed: bool | None = None
    # Owner (``user_id``) transfer — admin only. Ignored when omitted/unchanged.
    user_id: str | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None


class TemplateContainerResponse(BaseModel):
    id: str
    user_id: str | None = None
    owner_name: str | None = None
    name: str
    description: str | None = None
    kind: str
    tags: list[str] = []
    #: Legacy free-text search label, served for compatibility. Nothing
    #: renders it - the colours live in ``theme_config`` - and the UI no longer
    #: writes it; a saved ``?theme=<label>`` filter still matches it exactly.
    theme: str | None = None
    theme_config: dict = {}
    #: The colour schemes (``dark`` / ``light``) this template can dress a deck
    #: in, derived from ``theme_config`` + ``extra_metadata.theme_variants`` on
    #: every read. A deck linked to it may only pick a theme among them.
    #: Empty for a container that is not a presentation template (an asset
    #: library, a report skin): it dresses no deck.
    supported_schemes: list[str] = []
    aspect_ratio: str | None = None
    layouts: list[dict] = []
    extra_metadata: dict = {}
    lookup_count: int = 0
    asset_count: int = 0
    is_company_managed: bool = False
    #: Seeded at startup and full-synced on every boot: read-only for
    #: everyone, admins included — the UI offers "Duplicate" instead of "Edit".
    is_builtin: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    #: Whether the REQUESTING user may edit it - ``helpers.can_manage``, share
    #: tiers included, so the UI never offers an edit the API refuses.
    #: A built-in stays read-only whatever this says (``is_builtin``).
    can_manage: bool = False
    created_at: str
    updated_at: str


class TemplateContainerDetailResponse(TemplateContainerResponse):
    """Container plus its full asset list."""

    assets: list[TemplateAssetResponse] = []


class UpdateAssetRoleRequest(BaseModel):
    """Relabel or re-describe an existing asset without re-uploading it.

    Both fields are patch-style: a field that is OMITTED is left as it is, a
    field sent as ``null`` is cleared. The route tells the two apart through
    ``model_fields_set`` — a PATCH that only changes the role must not wipe the
    description on the way.
    """

    asset_role: str | None = Field(default=None, max_length=50)
    description: str | None = Field(default=None, max_length=MAX_ASSET_DESCRIPTION_CHARS)
