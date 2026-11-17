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

"""Pydantic schemas for the report API routes."""

from __future__ import annotations

from pydantic import BaseModel


class ReportResponse(BaseModel):
    """A report as consumed by the frontend block renderer.

    ``spec`` is the validated ``ReportSpec`` (see ``app.services.report_spec``) —
    the frontend still type-guards it before rendering, but the backend is the
    authority on its shape. ``theme_config`` carries the whitelisted brand
    overrides resolved from the report's linked template container, empty
    when there is none.
    """

    id: str
    title: str
    theme: str
    block_count: int
    #: Bumped on every in-place edit — lets the renderer distinguish "same report,
    #: new blocks" (reconcile) from a different report (remount).
    revision: int
    conversation_id: str | None = None
    assignment_run_id: str | None = None
    template_container_id: str | None = None
    spec: dict
    theme_config: dict[str, str] = {}
    created_at: str | None = None
    updated_at: str | None = None


class ReportSummaryResponse(BaseModel):
    """List-view shape: everything but the (potentially large) spec."""

    id: str
    title: str
    theme: str
    block_count: int
    revision: int
    conversation_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
