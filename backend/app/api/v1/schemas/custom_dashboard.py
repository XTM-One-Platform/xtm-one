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

"""Pydantic schemas for the custom dashboard API routes."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.v1.schemas.versioning import VersionDiffResponse
from app.services.custom_dashboard_spec import (
    MAX_OPERATIONS,
    MAX_SHORT_TEXT_CHARS,
    MAX_TITLE_CHARS,
)
from app.services.custom_dashboards import MAX_TAG_CHARS, MAX_TAGS
from app.services.dashboard_definition import MAX_STRUCTURE
from app.services.dashboard_platform import DataShape
from app.services.share_principals import SharingChange


class CustomDashboardSummaryResponse(BaseModel):
    """List-view shape: everything but the spec."""

    id: str
    title: str
    widget_count: int
    revision: int
    conversation_id: str | None = None
    assignment_run_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    #: The owner (null once the account is deleted).
    user_id: str | None = None
    is_company_managed: bool = False
    #: Shared with people or groups (who, only for those who may change it).
    is_shared: bool = False
    #: The caller's tier: ``owner`` / ``config`` edit, ``read_only`` reads.
    permission: Literal["owner", "manage", "config", "read_only"] = "owner"
    #: What it is filed under (``custom_dashboards.clean_tags``).
    tags: list[str] = Field(default_factory=list)


class CustomDashboardCreate(BaseModel):
    """A dashboard made by hand: its title and, optionally, its description (Markdown)."""

    model_config = ConfigDict(extra="forbid")

    title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE_CHARS)]
    description: Annotated[str | None, Field(default=None, max_length=MAX_SHORT_TEXT_CHARS)] = None

    @field_validator("title")
    @classmethod
    def _title_has_words(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("The title cannot be blank.")
        return value.strip()


class CustomDashboardResponse(CustomDashboardSummaryResponse):
    """A dashboard as the frontend widget renderer consumes it.

    ``spec`` is the stored ``DashboardSpec``. The renderer still reads it
    defensively (``normalizeDashboard``): a widget stored under an older catalog
    is drawn as a placeholder, never a reason to fail the page.
    """

    spec: dict
    #: The stored form: an OpenCTI manifest, OpenCTI and OpenAEV widgets in their platform's own shape and
    #: what XTM One adds under ``x_xtm_one`` (``dashboard_native``). What the source editor shows and writes.
    manifest: dict = Field(default_factory=dict)
    #: The visualizations that have a widget form (``dashboard_definition.widget_view``): what
    #: the studio's inspector edits and sends back as an ``upsert``.
    widgets: list[dict] = Field(default_factory=list)
    #: Whether the caller may edit and share it (the owner, a ``config`` grantee,
    #: an admin on a company dashboard; an admin reads anyone else's only).
    can_manage: bool = False
    #: Who it is shared with: sent to those who may change it, empty otherwise.
    shared_user_ids: list[str] = Field(default_factory=list)
    shared_user_permissions: dict[str, str] = Field(default_factory=dict)
    shared_group_ids: list[str] = Field(default_factory=list)
    shared_group_permissions: dict[str, str] = Field(default_factory=dict)
    access_restricted: bool = False
    #: Whether the caller may show it on their /dashboard (they read it as
    #: themselves, not through an admin's reach). Set on ``GET`` only.
    can_use_as_home: bool = False


class CustomDashboardTagsUpdate(BaseModel):
    """``PUT .../tags``: the whole list of a dashboard's tags."""

    model_config = ConfigDict(extra="forbid")

    tags: Annotated[list[Annotated[str, Field(max_length=MAX_TAG_CHARS)]], Field(max_length=MAX_TAGS)]


class CustomDashboardImportResponse(CustomDashboardResponse):
    """A dashboard made from a file, with what the conversion changed or left out."""

    import_notes: list[str] = Field(default_factory=list)


class CustomDashboardSharingUpdate(SharingChange):
    """``PUT .../sharing``: the sharing fields, with ``PATCH`` semantics (unset = kept)."""

    model_config = ConfigDict(extra="forbid")


class DashboardVersionDiffResponse(VersionDiffResponse):
    """Two versions compared: their definitions line by line, and which widgets changed."""

    changes: str | None = None


class CustomDashboardVersionResponse(BaseModel):
    """One kept version of a dashboard (``spec`` only when one version is read)."""

    revision: int
    title: str
    widget_count: int
    source: str
    summary: str | None = None
    restored_from_revision: int | None = None
    author_id: str | None = None
    author_name: str | None = None
    created_at: str | None = None
    spec: dict | None = None
    manifest: dict | None = None


class DashboardRestoreRequest(BaseModel):
    """Restore at the revision the client read (409 when it moved on)."""

    model_config = ConfigDict(extra="forbid")

    revision: Annotated[int, Field(ge=1)]


class DashboardOperationIn(BaseModel):
    """One human edit: remove or move a widget, upsert one from the studio, or place widgets on the grid.

    An upserted widget is validated by ``apply_operations`` exactly as an
    agent's (``validate_widget``); a platform widget also needs the Dashboard
    Studio and an integration the editor can use. A ``layout`` (a drag or a
    resize) carries the new places, checked like a widget's own ``layout``.
    """

    model_config = ConfigDict(extra="forbid")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    op: Literal["remove", "move", "upsert", "layout", "set", "delete"]
    id: Annotated[str | None, Field(default=None, min_length=1, max_length=60)] = None
    after: Annotated[str | None, Field(default=None, min_length=1, max_length=60)] = None
    widget: dict | None = None
    #: Positions are checked against the layout they land in (a grid's columns, a canvas's pixels).
    items: Annotated[list[dict] | None, Field(default=None, min_length=1, max_length=MAX_STRUCTURE)] = None
    layout_id: Annotated[str | None, Field(default=None, min_length=1, max_length=60, alias="layoutId")] = None
    path: Annotated[str | None, Field(default=None, min_length=1, max_length=200)] = None
    value: Any = None

    @model_validator(mode="after")
    def _shape(self) -> DashboardOperationIn:
        if self.op in ("set", "delete"):
            if self.path is None or self.id is not None or self.widget is not None or self.items is not None:
                raise ValueError(f"a {self.op} names a 'path' (and a 'value' for a set) and nothing else")
            if self.op == "set" and self.value is None:
                raise ValueError("a set carries the 'value' it writes")
            return self
        if self.path is not None or self.value is not None:
            raise ValueError(f"a {self.op} carries no 'path' nor 'value'")
        if self.op == "layout":
            if self.items is None or self.id is not None or self.widget is not None or self.after is not None:
                raise ValueError("a layout carries 'items' ([{id, x, y, w, h}]) and nothing else")
            return self
        if self.items is not None:
            raise ValueError(f"a {self.op} carries no 'items'")
        if self.op == "upsert" and self.widget is None:
            raise ValueError("an upsert needs 'widget'")
        if self.op != "upsert" and (self.id is None or self.widget is not None):
            raise ValueError(f"a {self.op} names the widget's 'id' and carries no 'widget'")
        return self


class DashboardDefinitionUpdate(BaseModel):
    """``PUT .../definition``: the whole definition (the source editor), at the revision read."""

    model_config = ConfigDict(extra="forbid")

    revision: Annotated[int, Field(ge=1)]
    definition: dict


class CustomDashboardUpdate(BaseModel):
    """``PATCH``: rename, remove or reorder widgets, at the revision read."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    revision: Annotated[int, Field(ge=1)]
    title: Annotated[str | None, Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)] = None
    subtitle: Annotated[str | None, Field(default=None, max_length=MAX_SHORT_TEXT_CHARS)] = None
    operations: Annotated[list[DashboardOperationIn], Field(default_factory=list, max_length=MAX_OPERATIONS)]


#: The time ranges a viewer may draw a live widget over (``dashboard_live.RANGES``).
WidgetDataRange = Literal["1d", "7d", "30d", "1m", "90d", "3m", "6m", "1y"]


class WidgetDataRequest(BaseModel):
    """``POST .../widgets/{id}/data``: the widget comes from the stored dashboard; the body picks a range and gives the tokens."""

    model_config = ConfigDict(extra="forbid")

    range: WidgetDataRange | None = None
    #: The page's tokens the source uses (``dashboard_tokens``): put into its filters, never into query text.
    tokens: dict[str, Any] = Field(default_factory=dict)
    #: Read the platform again rather than the answer kept a minute (the page's Refresh); still rate-limited.
    fresh: bool = False

    @field_validator("tokens")
    @classmethod
    def _tokens(cls, value: dict) -> dict:
        from app.services.dashboard_tokens import TokenValues

        return TokenValues(values=value).values


class WidgetPreviewRequest(BaseModel):
    """``POST .../widgets/preview``: a platform widget not saved yet, checked like a write."""

    model_config = ConfigDict(extra="forbid")

    widget: dict
    range: WidgetDataRange | None = None


class WidgetDataResponse(BaseModel):
    """A live widget's data, or why there is none (a fixed code)."""

    status: Literal["ok", "error"]
    shape: DataShape | None = None
    data: dict | None = None
    code: str | None = None
    fetched_at: str | None = None
    open_url: str | None = None
    #: How the widget's module draws it (``dashboard_live.presentation_of``): ``{"view", "options"}``.
    presentation: dict | None = None
    #: A source refreshed on the server (``dashboard_refresh``): ``{"mode", "every", "refreshed_at", "publisher"}`` -
    #: the snapshot shown, read with its publisher's access.
    snapshot: dict | None = None
    #: What changed since the source's previous read on the server (``dashboard_refresh.with_changes``):
    #: ``{"since", "figure": {"before", "after"}, "items"?}``.
    changes: dict | None = None


class PlatformSchemaRequest(BaseModel):
    """``POST /custom-dashboards/platform-schema``: one question a platform source editor asks."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    source: Literal["opencti", "openaev", "opencrq"]
    integration_id: uuid.UUID
    kind: Literal["entity_types", "filter_keys", "attributes", "values", "labels", "entities", "saved_filters"]
    perspective: Literal["entities", "relationships", "audits"] | None = None
    types: Annotated[list[Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")]], Field(default_factory=list, max_length=20)]
    key: Annotated[str | None, Field(default=None, min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")] = None
    search: Annotated[str, Field(default="", max_length=100)] = ""
    ids: Annotated[list[Annotated[str, Field(min_length=1, max_length=200)]], Field(default_factory=list, max_length=50)]

    @model_validator(mode="after")
    def _needs(self) -> PlatformSchemaRequest:
        if self.kind in ("values", "labels") and not self.key:
            raise ValueError(f"a {self.kind} question names the filter key")
        if self.kind == "labels" and not self.ids:
            raise ValueError("a labels question names the values (ids)")
        return self


class PlatformSchemaResponse(BaseModel):
    """An editor's answer: entity types, filter keys, attributes or values, or a fixed code."""

    status: Literal["ok", "error"]
    items: list[dict] = Field(default_factory=list)
    code: str | None = None


class PlatformFormEntry(BaseModel):
    """One platform's widget editor: its name and its form description (``dashboard_platform.forms``)."""

    source: Literal["opencti", "openaev", "opencrq"]
    name: str
    form: dict


class PlatformFormsResponse(BaseModel):
    """``GET /custom-dashboards/platform-forms``: every platform's widget editor."""

    forms: list[PlatformFormEntry]


class DashboardPublishRequest(BaseModel):
    """``POST .../publish``: send the dashboard to OpenCTI or OpenAEV through an integration."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target: Literal["opencti", "opencti-custom-view", "openaev"]
    integration_id: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str | None, Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)] = None


class DashboardHubShareRequest(BaseModel):
    """``POST .../share-on-hub``: share the dashboard's OpenCTI export on the XTM Hub."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    integration_id: Annotated[str, Field(min_length=1, max_length=64)]
    name: Annotated[str | None, Field(default=None, min_length=1, max_length=MAX_TITLE_CHARS)] = None
    short_description: Annotated[str | None, Field(default=None, max_length=300)] = None
    description: Annotated[str | None, Field(default=None, max_length=5000)] = None


class DashboardRenderExportRequest(BaseModel):
    """A dashboard as a PDF or a PNG (its chosen tabs, all by default), or one visualization as a PNG."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["pdf", "png"] = "pdf"
    layout_ids: Annotated[list[Annotated[str, Field(min_length=1, max_length=60)]] | None, Field(default=None, max_length=10)]
    visualization_id: Annotated[str | None, Field(default=None, min_length=1, max_length=60)]

    @model_validator(mode="after")
    def _one_visualization_is_a_picture(self) -> DashboardRenderExportRequest:
        if self.visualization_id is not None and (self.format != "png" or self.layout_ids):
            raise ValueError("one visualization is exported as a PNG, with no tabs named")
        return self


#: How often a published link's snapshot may be read again, in minutes (``dashboard_public_links.REFRESH_CHOICES``).
RefreshMinutes = Literal[15, 60, 240, 1440]
#: A marking definition's name, as OpenCTI writes it (``TLP:AMBER+STRICT``, ``PAP:GREEN``).
MarkingName = Annotated[str, Field(min_length=1, max_length=60, pattern=r"^[A-Za-z0-9][A-Za-z0-9:+_. -]*$")]
#: A link's highest markings: one per marking type at most is meaningful; an empty list is no ceiling.
MarkingCeiling = Annotated[list[MarkingName], Field(max_length=10)]


class DashboardPublicLinkRequest(BaseModel):
    """Publish the dashboard as a link (or hand back the one it has); ``regenerate`` issues a new address."""

    model_config = ConfigDict(extra="forbid")

    regenerate: bool = False
    refresh_minutes: RefreshMinutes | None = None
    marking_ceiling: MarkingCeiling | None = None


class DashboardPublicLinkUpdate(BaseModel):
    """How often the link's snapshot is read again, and its marking ceiling; at least one."""

    model_config = ConfigDict(extra="forbid")

    refresh_minutes: RefreshMinutes | None = None
    marking_ceiling: MarkingCeiling | None = None

    @model_validator(mode="after")
    def _something(self) -> DashboardPublicLinkUpdate:
        if self.refresh_minutes is None and self.marking_ceiling is None:
            raise ValueError("give refresh_minutes or marking_ceiling")
        return self


class DashboardPublicLinkListItem(BaseModel):
    """One published link, as administrators review them all: whose dashboard, who published it, how fresh."""

    dashboard_id: str
    dashboard_title: str
    publisher_name: str | None = None
    created_at: str | None = None
    refresh_minutes: int
    snapshot_at: str | None = None


class DashboardPublicLinkResponse(BaseModel):
    """The dashboard's published link as its managers see it; ``url`` is ``None`` without one."""

    enabled_on_platform: bool
    url: str | None = None
    #: The page's address under the web app (``/public/dashboards/<token>``), for a client that adds its own origin.
    path: str | None = None
    created_at: str | None = None
    publisher_name: str | None = None
    refresh_minutes: int | None = None
    snapshot_at: str | None = None
    #: The highest markings the snapshot shows (OpenCTI's data); empty, no ceiling.
    marking_ceiling: list[str] = Field(default_factory=list)
