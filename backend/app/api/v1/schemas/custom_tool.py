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

"""Pydantic schemas for custom tool API routes."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.api.v1.schemas.column_limits import column_length_validator
from app.models.custom_tool import CustomTool
from app.services.custom_tool_code_generation import (
    BRIEF_MAX_CHARS,
    CURRENT_CODE_MAX_CHARS,
    MAX_PARAMETERS,
    PARAMETER_DESCRIPTION_MAX_CHARS,
    TOOL_DESCRIPTION_MAX_CHARS,
)


class CustomToolCreate(BaseModel):
    name: str
    description: str = ""
    icon_url: str | None = None
    input_schema: dict | None = None
    js_function: str | None = None
    language: str = "javascript"
    tags: list[str] | None = None
    timeout: int = 300
    enabled: bool = True
    hide_from_chat: bool = False
    is_company_managed: bool = False
    # Declared tests (``services/custom_tool_tests.py``).
    test_cases: list[dict] | None = None
    test_code: str | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None

    _fit_columns = column_length_validator(CustomTool, "name", "icon_url", "language")


class CustomToolUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon_url: str | None = None
    input_schema: dict | None = None
    js_function: str | None = None
    language: str | None = None
    tags: list[str] | None = None
    timeout: int | None = None
    enabled: bool | None = None
    hide_from_chat: bool | None = None
    is_company_managed: bool | None = None
    # Declared tests: ``[]`` / ``""`` clear them, ``None`` leaves them.
    test_cases: list[dict] | None = None
    test_code: str | None = None
    # Owner (``user_id``) transfer — admin only. Ignored when omitted/unchanged.
    user_id: str | None = None
    is_public_catalog: bool | None = None
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None
    shared_user_ids: list[str] | None = None
    shared_user_permissions: dict[str, str] | None = None
    access_restricted: bool | None = None

    _fit_columns = column_length_validator(CustomTool, "name", "icon_url", "language")


class CustomToolResponse(BaseModel):
    id: str
    user_id: str | None = None
    owner_name: str | None = None
    name: str
    description: str
    icon_url: str | None = None
    input_schema: dict | None = None
    js_function: str | None = None
    language: str = "javascript"
    tags: list[str] = []
    timeout: int = 300
    enabled: bool
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
    #: Materialized by a skill package import: read-only on every write route
    #: whoever asks (``helpers.is_package_origin_custom_tool``), so the UI
    #: never offers those writes.
    is_package_origin: bool = False
    test_cases: list[dict] | None = None
    test_code: str | None = None
    #: "passed", "failed", "error" or "no_tests"; ``None`` before any run.
    last_test_status: str | None = None
    last_test_run_at: str | None = None
    last_test_results: dict | None = None
    #: The repository source the tool is synced from (read-only content).
    source_id: str | None = None
    source_name: str | None = None
    #: The tool's directory in the repository.
    source_path: str | None = None
    source_commit_sha: str | None = None
    #: The directory's web page at the active commit.
    source_url: str | None = None
    call_count: int = 0
    last_called_at: str | None = None
    created_at: str
    updated_at: str


class RunCustomToolTestsRequest(BaseModel):
    """``POST /custom-tools/{id}/run-tests``."""

    #: The data cases to run, by name; ``None`` runs them all.
    case_names: list[str] | None = None
    include_code_tests: bool = True
    #: Skip the cases that are not dry runs and force ``dry_run`` in the test code.
    dry_run_only: bool = False


class ImportCustomToolRequest(BaseModel):
    """Import a custom tool from an export bundle."""

    version: int
    export_type: str = "custom_tool"
    custom_tool: dict


class CustomToolSkillLinkResponse(BaseModel):
    skill_id: str
    skill_name: str
    skill_description: str | None = None
    resource_kind: str
    resource_path: str
    linked_at: str


class GenerateCodeParameter(BaseModel):
    """One input parameter as the editor holds it (not yet saved)."""

    name: str = Field(min_length=1, max_length=200)
    type: str = Field(default="string", max_length=40)
    description: str = ""
    required: bool = False

    @field_validator("description")
    @classmethod
    def _clip_description(cls, value: str) -> str:
        return value[:PARAMETER_DESCRIPTION_MAX_CHARS]


class GenerateCodeRequest(BaseModel):
    """``POST /custom-tools/generate-code`` - the editor's draft, never stored."""

    description: str = Field(max_length=BRIEF_MAX_CHARS)
    language: Literal["javascript", "python"]
    tool_name: str = Field(default="", max_length=200)
    tool_description: str = ""
    parameters: list[GenerateCodeParameter] = Field(default_factory=list, max_length=MAX_PARAMETERS)
    current_code: str | None = Field(default=None, max_length=CURRENT_CODE_MAX_CHARS)

    @field_validator("description")
    @classmethod
    def _describe_something(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Describe what the code should do")
        return value

    @field_validator("tool_description")
    @classmethod
    def _clip_tool_description(cls, value: str) -> str:
        return value[:TOOL_DESCRIPTION_MAX_CHARS]


class GenerateCodeResponse(BaseModel):
    code: str
    #: The answer was still cut at its output budget after one larger retry.
    truncated: bool = False
    #: ``lint_tool_code``'s verdict on the returned code (never executed).
    syntax_ok: bool
    syntax_error: str | None = None
    syntax_error_line: int | None = None
    #: A parser error in the first answer was handed back to the model once.
    repaired: bool = False
