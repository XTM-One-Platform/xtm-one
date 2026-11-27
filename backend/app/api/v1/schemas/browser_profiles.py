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

"""Request and response bodies of the Browser profiles API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.api.v1.schemas.vault_references import vault_reference_validator


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BrowserProfileApp(_Body):
    """Another application the profile's saved session signs in."""

    name: str = Field("", max_length=200)
    url: str = Field(min_length=1, max_length=2000)


class BrowserProfileWrite(_Body):
    """A profile write. Credentials are write-only: omit to keep, ``""`` to clear; each may be a vault reference."""

    name: str = Field(min_length=1, max_length=200)
    start_url: str = Field(min_length=1, max_length=2000)
    login_url: str | None = Field(None, max_length=2000)
    allowed_hosts: list[str] = Field(default_factory=list, max_length=50)
    #: The other applications the saved session signs in; omitted = keep, ``[]`` = none (``profiles.clean_apps``).
    apps: list[BrowserProfileApp] | None = Field(None, max_length=20)
    #: Selectors and URL patterns by name; ``keep_storage_keys`` a list of storage keys (``profiles``).
    login_config: dict[str, str | list[str]] | None = None
    browser_options: dict[str, str] | None = None
    description: str | None = Field(None, max_length=2000)
    #: Omitted = keep (240 minutes on a new profile: every profile renews by itself by default); 0 = never.
    keepalive_minutes: int | None = None
    #: Omitted = keep (the default on a new profile); bounds in ``profiles.TIMEOUT_BOUNDS``.
    sign_in_timeout_seconds: int | None = None
    session_save_timeout_seconds: int | None = None
    mfa_timeout_seconds: int | None = None
    username: str | None = Field(None, max_length=4000)
    password: str | None = Field(None, max_length=4000)
    totp_secret: str | None = Field(None, max_length=4000)
    is_company_managed: bool | None = None

    _check_references = vault_reference_validator("username", "password", "totp_secret")


class BrowserProfileList(BaseModel):
    items: list[dict[str, Any]]


class MfaCodeRequest(_Body):
    code: str = Field(min_length=4, max_length=20)


class SignInState(BaseModel):
    """``signin.SignInOutcome``: ``running`` | ``active`` | ``mfa_required`` (``kind`` code | push_number | push) |
    ``failed`` | ``unverified`` (signed in, but the application did not answer the probe of the saved session)."""

    status: str
    message: str
    challenge_id: str | None = None
    kind: str | None = None
    number: str | None = None
    screenshot_file_id: str | None = None
    expires_at: str | None = None
    profile_id: str | None = None
    #: A failure's stage (``signin.STAGES``) and the last URL the browser was on.
    stage: str | None = None
    last_url: str | None = None
    probe: dict[str, Any] | None = None
    #: The decisions the sign-in took (``{stage, url, decision, at}`` per step), for the operator.
    trace: list[dict[str, Any]] | None = None
