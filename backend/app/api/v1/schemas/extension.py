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

"""Schemas for the browser extension stateless execution endpoint."""

from pydantic import BaseModel, Field


class ExtensionTaskRequest(BaseModel):
    """Request payload for ``POST /api/v1/extension/execute-task``.

    The extension identifies the target agent by its slug and sends
    the task input as a serialized JSON string in ``content``.
    """

    agent_slug: str = Field(
        ...,
        min_length=1,
        max_length=100,
        pattern=r"^browser-[a-z0-9-]+$",
        description="Slug of the browser-extension agent to execute.",
        examples=["browser-scan-all", "browser-entity-discovery"],
    )
    content: str = Field(
        ...,
        min_length=1,
        max_length=500_000,
        description="Serialized JSON payload for the agent.",
    )
    max_tokens: int | None = Field(
        default=None,
        ge=1000,
        le=100_000,
        description="Optional max output tokens override for the LLM call.",
    )
