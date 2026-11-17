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

"""Pydantic schemas for agent skill API routes."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.api.v1.schemas.column_limits import column_length_validator
from app.models.agent_skill import AgentSkill


class AgentSkillCreate(BaseModel):
    """Create a new skill.

    Three creation modes:
    1. **URL** – provide ``url``; the backend fetches the SKILL.md, parses
       frontmatter (name, description, compatibility), and caches the content.
    2. **Paste** – provide ``content`` (and ``name``) directly; no fetch.
    3. **Upload** – handled by a separate multipart endpoint.

    At least one of ``url`` or ``content`` is required (validated in the route).
    Callers can override any auto-detected field.
    """

    url: str | None = None
    content: str | None = None  # direct markdown/text content
    # Optional GitHub integration whose credential authenticates the fetch —
    # required when ``url`` points into a private repository.  The
    # integration must be visible to the caller, have provider ``github`` and
    # be connected; the file is read once at create time to prove the
    # credential works before the row is saved.
    source_integration_id: str | None = None
    # Branch / tag / commit to track.  Omit to follow the repository's default
    # branch HEAD, i.e. "always the latest merged version".
    source_ref: str | None = Field(default=None, max_length=255)
    name: str | None = None  # auto-detected from frontmatter if omitted (URL mode)
    description: str | None = None  # auto-detected from frontmatter if omitted
    compatibility: str | None = None  # auto-detected from frontmatter if omitted
    license: str | None = Field(default=None, max_length=1000)  # frontmatter ``license`` if omitted
    tags: list[str] | None = None
    hide_from_chat: bool = False
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None

    _fit_columns = column_length_validator(AgentSkill, "name")


class AgentSkillUpdate(BaseModel):
    """Update an existing skill (rename, change URL, toggle flags, etc.)."""

    url: str | None = None
    content: str | None = None  # replace cached content directly
    # Attach, change, or detach (explicit ``null``) the GitHub credential used
    # to authenticate the URL fetch.  Changing either field re-validates
    # the source immediately instead of waiting for the next hourly refresh.
    source_integration_id: str | None = None
    source_ref: str | None = Field(default=None, max_length=255)
    name: str | None = None
    description: str | None = None
    compatibility: str | None = None
    license: str | None = Field(default=None, max_length=1000)
    tags: list[str] | None = None
    enabled: bool | None = None
    hide_from_chat: bool | None = None
    is_company_managed: bool | None = None
    # Owner (``user_id``) transfer — admin only. Ignored when omitted/unchanged.
    user_id: str | None = None
    is_public_catalog: bool | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None
    commit_message: str | None = Field(default=None, max_length=500)

    _fit_columns = column_length_validator(AgentSkill, "name")


class AgentSkillResponse(BaseModel):
    id: str
    user_id: str | None = None
    owner_name: str | None = None
    url: str | None = None
    name: str
    description: str | None = None
    compatibility: str | None = None
    #: The skill's licence (frontmatter ``license``), shown in its Catalog tab.
    license: str | None = None
    content: str | None = None
    fetched_at: str | None = None
    # Number of consecutive 4xx failures during auto-refresh.
    # Resets to 0 on a successful refresh or when the URL is changed.
    # When this equals the configured quarantine threshold the skill is
    # excluded from further auto-refresh attempts.
    refresh_failure_count: int = 0
    # Private-repository skill source.  ``source_integration_id`` is a
    # reference only — the credential itself never leaves the integration row.
    source_integration_id: str | None = None
    source_ref: str | None = None
    # Number of consecutive authentication failures during auto-refresh.
    # Non-zero means the bound credential is being rejected (expired, revoked,
    # or lacking access to the repository) — the skill keeps retrying daily and
    # is never quarantined for this, but its content is stale.
    auth_failure_count: int = 0
    tags: list[str] = []
    enabled: bool = True
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
    package_exportable: bool = True
    frontmatter_warnings: list[str] = Field(default_factory=list)
    #: Provenance of a fork (``POST /skills/{id}/fork``); None = not a fork.
    forked_from: dict | None = None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


class SkillFork(BaseModel):
    """Fork a skill: an editable personal copy, a built-in one included (#9174)."""

    #: Name of the copy; empty is ``Copy of`` the source's name.
    name: str | None = Field(default=None, max_length=200)
    #: A fleet whose member agents bound to the source are re-bound to the copy.
    repoint_fleet_id: str | None = None


class SkillForkResponse(BaseModel):
    skill: AgentSkillResponse
    resources_copied: int = 0
    repointed_agent_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SkillPackageValidationResponse(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    detected_name: str | None = None
    detected_description: str | None = None
    detected_compatibility: str | None = None
    resource_count: int = 0
    version_count: int = 0


class BulkSkillCandidateResponse(BaseModel):
    """One skill found in a bulk archive, as shown on the review screen."""

    path: str
    name: str | None = None
    description: str | None = None
    compatibility: str | None = None
    resource_count: int = 0
    version_count: int = 0
    # False when the entry cannot be imported at all (malformed frontmatter,
    # duplicate name, unavailable built-in tool) — `errors` says why.
    importable: bool = True
    # True when a skill with the same name is already visible to the user; the
    # review screen turns this into a per-skill collision choice.
    collision: bool = False
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class IgnoredArchiveEntryResponse(BaseModel):
    path: str
    # One of app.services.skill_package_manager.BULK_IGNORE_REASONS.
    reason: str


class SkillBulkPackageValidationResponse(BaseModel):
    valid: bool
    # Archive-level failures only (unreadable ZIP, size caps, no skill found).
    errors: list[str] = Field(default_factory=list)
    skills: list[BulkSkillCandidateResponse] = Field(default_factory=list)
    ignored: list[IgnoredArchiveEntryResponse] = Field(default_factory=list)
    skill_count: int = 0
    importable_count: int = 0
    ignored_count: int = 0


class BulkSkillImportOutcome(BaseModel):
    path: str
    name: str | None = None
    # created | replaced | skipped | failed
    status: str
    skill_id: str | None = None
    message: str | None = None


class SkillBulkImportResponse(BaseModel):
    created_count: int = 0
    replaced_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    results: list[BulkSkillImportOutcome] = Field(default_factory=list)


class SkillTier3ToolLinkResponse(BaseModel):
    id: str
    name: str
    description: str
    resource_path: str


class SkillPackageBindingsResponse(BaseModel):
    """Entities a packaged skill carries onto any agent it is attached to.

    ``scripts/`` resources become Custom Tools and ``references/`` resources
    become Knowledge Base documents at import time; both follow the skill's
    agent bindings (see ``services/skill_package_bindings.py``). Skills with no
    packaged resources are omitted from the listing entirely.
    """

    skill_id: str
    skill_name: str
    custom_tool_ids: list[str] = Field(default_factory=list)
    knowledge_base_ids: list[str] = Field(default_factory=list)


class SkillLicenseResponse(BaseModel):
    """A skill's licence, as its Catalog tab shows it."""

    #: The licence notice (frontmatter ``license``).
    license: str | None = None
    #: The licence file bundled at the skill's root, when there is one.
    file_path: str | None = None
    text: str | None = None
    #: True when ``text`` was cut at the display limit.
    truncated: bool = False

