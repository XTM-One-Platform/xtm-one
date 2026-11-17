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

"""Pydantic schemas for the Nova prompt-safety rules API (#nova).

A Nova rule is a ``.nov`` detection rule (see ``app.services.nova_guard``) with
a per-rule ``block``/``log`` action. The three-tier visibility columns mirror
``Prompt`` / ``CustomTool``; only company-managed rules are loaded by the
active pre-LLM guard, and only admins may create/edit those.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.nova_rule import NOVA_ACTIONS

# Reused literal for request validation — keeps the API in lock-step with the
# model's CHECK constraint without importing Literal string duplicates.
_ActionField = Field(
    default="log",
    description="What a match does: 'block' refuses the prompt, 'log' only flags it.",
)


class NovaRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    category: str = Field(default="", max_length=100)
    action: str = _ActionField
    rule_text: str = Field(min_length=1)
    enabled: bool = True
    tags: list[str] | None = None
    is_company_managed: bool = False
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None


class NovaRuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    category: str | None = Field(default=None, max_length=100)
    action: str | None = None
    rule_text: str | None = Field(default=None, min_length=1)
    enabled: bool | None = None
    tags: list[str] | None = None
    is_company_managed: bool | None = None
    user_id: str | None = None  # owner transfer (admin only)
    shared_group_ids: list[str] | None = None
    shared_group_permissions: dict[str, str] | None = None


class NovaRuleResponse(BaseModel):
    id: str
    user_id: str | None
    owner_name: str | None = None
    name: str
    description: str
    category: str
    action: str
    rule_text: str
    enabled: bool
    tags: list[str]
    is_company_managed: bool
    shared_group_ids: list[str] | None
    shared_group_permissions: dict[str, str] | None
    created_at: str
    updated_at: str
    # Whether the rule carries a ``semantics:`` pattern, whose threshold only
    # means what it says on a calibrated embedding model.
    has_semantics: bool = False


class NovaRuleValidateRequest(BaseModel):
    rule_text: str = Field(min_length=1)


class NovaRuleValidateResponse(BaseModel):
    """Result of a dry parse — the builder uses it for live feedback."""

    valid: bool
    error: str | None = None
    name: str | None = None
    category: str | None = None
    keyword_count: int = 0
    # Rule tiers present in the source; 'semantics'/'llm' are parsed but inert
    # until the optional Nova engine is installed (surfaced so the UI can warn).
    tiers: list[str] = Field(default_factory=list)


def valid_actions() -> tuple[str, ...]:
    return NOVA_ACTIONS
