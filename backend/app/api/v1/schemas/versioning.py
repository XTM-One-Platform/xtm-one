# SPDX-License-Identifier: Apache-2.0

"""Shared schemas for prompt/persona versioning APIs."""

from __future__ import annotations

from pydantic import BaseModel, Field


class VersionListItemResponse(BaseModel):
    id: str
    version_number: int
    author_id: str | None = None
    author_name: str | None = None
    commit_message: str | None = None
    is_active: bool = False
    is_locked: bool = False
    created_at: str


class VersionListResponse(BaseModel):
    items: list[VersionListItemResponse]
    total: int


class VersionDetailResponse(VersionListItemResponse):
    content: str


class DiffLineResponse(BaseModel):
    type: str
    content: str
    old_line: int | None = None
    new_line: int | None = None


class VersionDiffResponse(BaseModel):
    from_version: int
    to_version: int
    added: int
    removed: int
    lines: list[DiffLineResponse]
    unified_diff: str


class RollbackRequest(BaseModel):
    commit_message: str | None = Field(default=None, max_length=500)
