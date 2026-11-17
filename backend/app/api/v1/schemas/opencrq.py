# SPDX-License-Identifier: Apache-2.0

"""Schemas of the OpenCRQ compatibility card (``/integrations/{id}/opencrq/compatibility``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

#: ``registration``: the OpenCRQ registered with XTM One, which holds its manifest. ``none``: no registration owns the
#: integration, the manifest is read from the instance at the first OpenCRQ call of a conversation or run.
ManifestSource = Literal["registration", "none"]
#: ``current``: the stored manifest is the one the last registration reported. ``pending``: a fingerprint came, its
#: manifest has not. ``unavailable``: the registration reports no fingerprint. ``not_accepted_default_token``: the
#: published default registration token is in force, so no manifest is stored.
ManifestState = Literal["current", "pending", "unavailable", "not_accepted_default_token"]
FieldKind = Literal["unknown", "missing"]


class HiddenToolResponse(BaseModel):
    """A tool the connected OpenCRQ cannot serve: withheld from the agents."""

    name: str
    #: The OpenCRQ procedures the tool sends that the connected instance does not list.
    missing_procedures: list[str]


class LimitedFieldResponse(BaseModel):
    """A field the connected instance does not accept (``unknown``) or requires and the tool never sends (``missing``)."""

    procedure: str
    path: str
    kind: FieldKind


class LimitedToolResponse(BaseModel):
    """A served tool whose calls are refused on the connected instance for some of its fields."""

    name: str
    fields: list[LimitedFieldResponse]


class CompatibilityResponse(BaseModel):
    #: What the registration reported (``1.2.3``, ``main-1a2b3c4d``, ``""`` for a local build); ``None``: no registration.
    reported_version: str | None = None
    manifest_source: ManifestSource
    #: ``None`` when ``manifest_source`` is ``none``.
    manifest_state: ManifestState | None = None
    #: The first 12 hex characters of the fingerprint the registration reported; ``None``: it reported none.
    fingerprint: str | None = None
    #: When the manifest in force was received; only when ``manifest_state`` is ``current``.
    received_at: datetime | None = None
    tool_count: int
    #: Only when ``manifest_state`` is ``current``.
    hidden_tools: list[HiddenToolResponse] = Field(default_factory=list)
    #: Only when ``manifest_state`` is ``current``.
    limited_tools: list[LimitedToolResponse] = Field(default_factory=list)
