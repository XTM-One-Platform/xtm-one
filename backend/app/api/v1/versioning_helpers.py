# SPDX-License-Identifier: Apache-2.0

"""Shared helpers for versioning API responses."""

from __future__ import annotations

from typing import Any

from app.api.v1.schemas.versioning import VersionDetailResponse, VersionListItemResponse


def to_version_item(
    version: Any,
    *,
    author_name: str | None = None,
    is_active: bool = False,
) -> VersionListItemResponse:
    return VersionListItemResponse(
        id=str(version.id),
        version_number=version.version_number,
        author_id=str(version.author_id) if version.author_id else None,
        author_name=author_name,
        commit_message=version.commit_message,
        is_active=is_active,
        is_locked=bool(getattr(version, "is_locked", False) or False),
        created_at=version.created_at.isoformat(),
    )


def to_version_detail(
    version: Any,
    *,
    author_name: str | None = None,
    is_active: bool = False,
) -> VersionDetailResponse:
    item = to_version_item(
        version,
        author_name=author_name,
        is_active=is_active,
    )
    return VersionDetailResponse(**item.model_dump(), content=version.content)
