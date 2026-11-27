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

"""Export a custom dashboard, or publish it to OpenCTI or OpenAEV as one of theirs.

The two directions of the interop (``dashboard_interop``) applied to a stored
dashboard:

* **Export** - the file each platform imports, downloaded: an OpenCTI
  dashboard export (JSON, base64 manifest), an OpenCTI custom view (the same,
  of the entity type the dashboard's entity input picks: the widgets reading it
  read the page's entity, ``SELF_ID``), an OpenAEV dashboard export (ZIP of
  JSON:API), or an XTM One widget pack. Only what the format can hold is
  written; the rest is listed in the report (an OpenCTI dashboard cannot draw
  an OpenAEV widget, nor an XTM One score). An OpenCTI saved filter a widget
  names is written in place (:func:`inline_saved_filters`): its id means
  nothing outside the OpenCTI it was saved in.
* **Publish** - the same file sent through an integration the person can use
  (``platform_access``: their own access - a platform-managed integration acts
  as them, so OpenCTI checks EXPLORE_EXUPDATE, OpenAEV its DASHBOARD write):
  OpenCTI ``workspaceConfigurationImport(file)`` (a custom view:
  ``customViewConfigurationImport``, SETTINGS_SETCUSTOMIZATION), OpenAEV
  ``POST /api/custom-dashboards/import``. What came back is recorded
  (``CustomDashboardPublication``). The platform's copy is its own from then on.

OpenCRQ has no custom dashboards (its dashboards are pages of its app), so a
dashboard is never published there: its OpenCRQ widgets stay XTM One's.

* **Share on the XTM Hub** - the OpenCTI dashboard export, published as a new
  resource of the Hub's OpenCTI custom dashboards library the person's Hub
  token may publish to (``xtm_hub.publish_hub_resource``: the uploader is the
  token's own user, the resource starts inactive until a person activates it
  on the Hub), through an XTM Hub integration the person can use.

Every failure is a :class:`PublishError` with a sentence safe to show; what a
platform said goes to the logs only.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.custom_dashboard import CustomDashboard, CustomDashboardPublication

logger = logging.getLogger(__name__)

ExportFormat = Literal["opencti", "opencti-custom-view", "openaev", "xtm"]
PublishTarget = Literal["opencti", "opencti-custom-view", "openaev"]

#: Publications per person per minute.
PUBLISHES_PER_MINUTE = 5
#: Files per person per minute whose OpenCTI saved filters are read to be written in place.
SAVED_FILTER_READS_PER_MINUTE = 20

_OPENCTI_IMPORT = """
mutation XtmOneDashboardImport($file: Upload!) {
  workspaceConfigurationImport(file: $file)
}"""
_OPENCTI_CUSTOM_VIEW_IMPORT = """
mutation XtmOneCustomViewImport($targetEntityType: String!, $file: Upload!) {
  customViewConfigurationImport(targetEntityType: $targetEntityType, file: $file) { id }
}"""

_SAFE_NAME = re.compile(r"[^\w .-]+")


class PublishError(Exception):
    """The dashboard could not be exported or published; the message is safe to show."""


@dataclass(slots=True)
class ExportFile:
    content: bytes
    filename: str
    media_type: str
    notes: list[str] = field(default_factory=list)


def _widgets(dashboard: CustomDashboard) -> list[dict]:
    """The dashboard's visualizations that have a widget form (content, platform), in reading order."""
    from app.services.dashboard_definition import summary_widgets

    return summary_widgets(dashboard.spec)


def _file_stem(name: str) -> str:
    return (_SAFE_NAME.sub("_", name).strip() or "dashboard")[:80]


def _inputs_read(dashboard: CustomDashboard, widgets: list[dict]) -> list[dict]:
    """The dashboard's entity inputs its *widgets* read: what a pack or an OpenAEV export keeps of its inputs."""
    from app.services.dashboard_widget_packs import entity_inputs_read

    return entity_inputs_read(dashboard.spec, widgets)


def custom_view_entity(dashboard: CustomDashboard) -> tuple[str, str] | None:
    """The token and entity type of the OpenCTI entity input the dashboard's OpenCTI widgets read, or ``None``.

    That input is what an OpenCTI custom view reads from its page: the custom view
    import's own input (token ``entity``) first, else the first entity input of a
    single OpenCTI type that a widget reads.
    """
    from app.services.dashboard_definition import upgrade
    from app.services.dashboard_tokens import names_in

    definition = upgrade(dashboard.spec) if isinstance(dashboard.spec, dict) else {}
    read = set().union(*(names_in(w.get("native")) for w in _widgets(dashboard) if w.get("source") == "opencti"))
    candidates = []
    for entry in (definition.get("inputs") or {}).values():
        options = entry.get("options") if isinstance(entry, dict) and isinstance(entry.get("options"), dict) else {}
        types = options.get("entityTypes") or []
        if entry.get("type") == "input.entity" and options.get("source") == "opencti" and entry.get("token") in read and len(types) == 1:
            candidates.append((str(entry["token"]), str(types[0])))
    candidates.sort(key=lambda pair: pair[0] != "entity")
    return candidates[0] if candidates else None


async def inline_saved_filters(db: AsyncSession, user_id: uuid.UUID, widgets: list[dict]) -> tuple[list[dict], list[str]]:
    """*widgets* with the OpenCTI saved filters they name read and written in place, and the notes of those left.

    Each is read through the widget's integration with *user_id*'s own access
    (``resolve_platform_client``), as a widget is drawn: a file leaves the OpenCTI
    the filter was saved in, where its id means nothing. One that cannot be read
    (an integration the person cannot use, a filter they cannot see) keeps its id.
    """
    from app.core.egress import egress_route
    from app.services.dashboard_live import LiveError
    from app.services.dashboard_live.opencti import resolve_saved_filters
    from app.services.platform_access import PlatformAccessError, resolve_platform_client

    def names_saved(widget: dict) -> bool:
        native = widget.get("native")
        return (
            widget.get("kind") == "platform"
            and widget.get("source") == "opencti"
            and isinstance(native, dict)
            and any(
                isinstance(s, dict) and (s.get("filters_id") or s.get("dynamicFrom_id") or s.get("dynamicTo_id"))
                for s in native.get("dataSelection") or []
            )
        )

    if not any(names_saved(w) for w in widgets):
        return widgets, []
    if not await _saved_filter_budget(user_id):
        return widgets, ["Saved filters were read too often in the last minute: they keep their ids; export again shortly."]
    out: list[dict] = []
    notes: list[str] = []
    caches: dict[str, dict[str, dict]] = {}
    for widget in widgets:
        native = widget.get("native")
        if not names_saved(widget):
            out.append(widget)
            continue
        integration_id = str(widget.get("integration_id") or "")
        try:
            client = await resolve_platform_client(db, user_id, integration_id, "opencti")
            with egress_route(client.route):
                # Each filter on its own: one that cannot be read keeps its id, the others are written in place.
                resolved = await resolve_saved_filters(client, native, caches.setdefault(integration_id, {}), keep_unreadable=True)
        except (PlatformAccessError, LiveError) as exc:
            logger.info("Saved filters of widget %r not inlined: %s", str(widget.get("id"))[:80], getattr(exc, "code", "access"))
            resolved = native
        out.append({**widget, "native": resolved})
        if names_saved({**widget, "native": resolved}):
            label = widget.get("title") or widget.get("id")
            notes.append(f"Widget '{label}' reads an OpenCTI saved filter you cannot read: it keeps its id, which only that OpenCTI knows.")
    return out, notes


async def _saved_filter_budget(user_id: uuid.UUID) -> bool:
    """Whether *user_id* may have a file's saved filters read now (:data:`SAVED_FILTER_READS_PER_MINUTE`)."""
    from fastapi import HTTPException

    from app.core.rate_limit import enforce_rate_limit

    try:
        await enforce_rate_limit(
            None, "dashboard-saved-filter-reads", limit=SAVED_FILTER_READS_PER_MINUTE, window_seconds=60, bucket=str(user_id)
        )
    except HTTPException as exc:
        if exc.status_code == 429:
            return False
        raise
    return True


async def export_dashboard_file(
    db: AsyncSession, user_id: uuid.UUID, dashboard: CustomDashboard, fmt: ExportFormat, *, name: str | None = None
) -> ExportFile:
    """:func:`build_export`, with the saved filters the file would carry read as *user_id* and written in place."""
    if fmt == "openaev":
        return build_export(dashboard, fmt, name=name)
    widgets, notes = await inline_saved_filters(db, user_id, _widgets(dashboard))
    export = build_export(dashboard, fmt, name=name, widgets=widgets)
    export.notes = [*notes, *export.notes]
    return export


def build_export(dashboard: CustomDashboard, fmt: ExportFormat, *, name: str | None = None, widgets: list[dict] | None = None) -> ExportFile:
    """The file *fmt* imports, from the stored dashboard (or *widgets*, its widgets as they leave); :class:`PublishError` when nothing fits."""
    from app.services.dashboard_definition import upgrade
    from app.services.dashboard_interop import InteropError
    from app.services.dashboard_interop import openaev as aev
    from app.services.dashboard_interop import opencti as cti
    from app.services.dashboard_interop.pack_format import build_pack

    title = (name or dashboard.title or "Dashboard").strip()
    definition = upgrade(dashboard.spec)
    description = definition.get("description")
    widgets = _widgets(dashboard) if widgets is None else widgets
    stem = _file_stem(title)
    try:
        if fmt == "opencti":
            export, report = cti.build_dashboard_export(
                title, widgets, description=description, inputs=definition.get("inputs") or {}
            )
            return ExportFile(json.dumps(export, ensure_ascii=False).encode("utf-8"), f"{stem}.json", "application/json", report.notes)
        if fmt == "opencti-custom-view":
            entity = custom_view_entity(dashboard)
            if entity is None:
                raise PublishError(
                    "An OpenCTI custom view reads the entity of its page: this dashboard has no OpenCTI entity input of "
                    "one type that its OpenCTI widgets read."
                )
            token, target_type = entity
            export, report = cti.build_custom_view_export(title, widgets, target_type=target_type, token=token, description=description)
            return ExportFile(json.dumps(export, ensure_ascii=False).encode("utf-8"), f"{stem}.json", "application/json", report.notes)
        if fmt == "openaev":
            content, report = aev.build_export_zip(title, widgets, description=description, inputs=_inputs_read(dashboard, widgets))
            return ExportFile(content, f"{stem}.zip", "application/zip", report.notes)
    except InteropError as exc:
        raise PublishError(str(exc)) from None
    document = build_pack(title, description, widgets, _inputs_read(dashboard, widgets))
    return ExportFile(json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8"), f"{stem}.json", "application/json")


async def _enforce_rate(user_id: uuid.UUID) -> None:
    from fastapi import HTTPException

    from app.core.rate_limit import enforce_rate_limit

    try:
        await enforce_rate_limit(
            None, "dashboard-publish", limit=PUBLISHES_PER_MINUTE, window_seconds=60, bucket=str(user_id)
        )
    except HTTPException as exc:
        if exc.status_code == 429:
            raise PublishError("Too many publications in a minute: try again shortly.") from None
        raise


async def publish_dashboard(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    dashboard: CustomDashboard,
    target: PublishTarget,
    integration_id: uuid.UUID | str,
    name: str | None = None,
) -> tuple[CustomDashboardPublication, list[str]]:
    """Send the dashboard to *target* through *integration_id*, as *user_id*; the record and the report."""
    from app.core.egress import egress_route
    from app.services.dashboard_live import LiveError
    from app.services.dashboard_live._http import request_json
    from app.services.platform_access import PlatformAccessError, resolve_platform_client

    if target not in ("opencti", "opencti-custom-view", "openaev"):
        raise PublishError("A dashboard is published to OpenCTI or OpenAEV; OpenCRQ has no custom dashboards.")
    # The budget first: nothing is read, not even the file's saved filters, past it.
    await _enforce_rate(user_id)
    export = await export_dashboard_file(db, user_id, dashboard, target, name=name)
    try:
        client = await resolve_platform_client(db, user_id, integration_id, _PROVIDERS[target])
    except PlatformAccessError:
        raise PublishError(f"The {_NAMES[target]} integration is not one you can use.") from None
    try:
        with egress_route(client.route):
            if target == "opencti":
                answer = await request_json(
                    "POST",
                    f"{client.base_url}/graphql",
                    client.headers,
                    files={"0": (export.filename, export.content, export.media_type)},
                    form={
                        "operations": json.dumps({"query": _OPENCTI_IMPORT, "variables": {"file": None}}),
                        "map": json.dumps({"0": ["variables.file"]}),
                    },
                    what="OpenCTI",
                )
                remote_id = _opencti_id(answer)
                remote_url = f"{client.base_url}/dashboard/workspaces/dashboards/{quote(remote_id, safe='')}"
            elif target == "opencti-custom-view":
                target_type = json.loads(export.content)["configuration"]["target_entity_type"]
                answer = await request_json(
                    "POST",
                    f"{client.base_url}/graphql",
                    client.headers,
                    files={"0": (export.filename, export.content, export.media_type)},
                    form={
                        "operations": json.dumps(
                            {"query": _OPENCTI_CUSTOM_VIEW_IMPORT, "variables": {"targetEntityType": target_type, "file": None}}
                        ),
                        "map": json.dumps({"0": ["variables.file"]}),
                    },
                    what="OpenCTI",
                )
                remote_id = _opencti_id(answer, "customViewConfigurationImport")
                remote_url = (
                    f"{client.base_url}/dashboard/settings/customization/entity_types/"
                    f"{quote(target_type, safe='')}/custom-views/{quote(remote_id, safe='')}"
                )
            else:
                prefix = f"/api/tenants/{quote(client.tenant_id, safe='')}" if client.tenant_id else "/api"
                answer = await request_json(
                    "POST",
                    f"{client.base_url}{prefix}/custom-dashboards/import",
                    client.headers,
                    files={"file": (export.filename, export.content, export.media_type)},
                    what="OpenAEV",
                )
                remote_id = _openaev_id(answer)
                remote_url = f"{client.base_url}/admin/workspaces/custom_dashboards/{quote(remote_id, safe='')}"
    except LiveError as exc:
        reasons = {
            "access_denied": f"{_NAMES[target]} refused: your account may not create dashboards there.",
            "timeout": f"{_NAMES[target]} took too long to answer.",
        }
        raise PublishError(reasons.get(exc.code, f"{_NAMES[target]} could not import the dashboard.")) from None
    record = CustomDashboardPublication(
        dashboard_id=dashboard.id,
        revision=dashboard.revision,
        integration_id=client.integration_id,
        target=target,
        remote_id=remote_id,
        remote_url=remote_url[:1000],
        published_by=user_id,
    )
    db.add(record)
    await db.flush()
    return record, export.notes


_NAMES = {"opencti": "OpenCTI", "opencti-custom-view": "OpenCTI", "openaev": "OpenAEV"}
#: The integration each target is reached through.
_PROVIDERS = {"opencti": "opencti", "opencti-custom-view": "opencti", "openaev": "openaev"}

#: The Hub library a dashboard is shared to.
HUB_DASHBOARDS_LIBRARY = "opencti_custom_dashboards"
_HUB_SHORT_DESCRIPTION = 300


async def share_on_hub(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    dashboard: CustomDashboard,
    integration_id: uuid.UUID | str,
    name: str | None = None,
    short_description: str | None = None,
    description: str | None = None,
) -> tuple[CustomDashboardPublication, list[str]]:
    """Publish the dashboard's OpenCTI export on the XTM Hub, as *user_id*; the record and the report."""
    import secrets

    from app.core.egress import egress_route
    from app.integrations.providers.xtm_hub import (
        find_publishable_library,
        hub_api_token,
        library_page_url,
        publish_hub_resource,
        xtm_hub_base_url,
    )
    from app.services.dashboard_interop.opencti import slug
    from app.services.platform_access import PlatformAccessError, resolve_platform_client

    await _enforce_rate(user_id)
    export = await export_dashboard_file(db, user_id, dashboard, "opencti", name=name)
    version = str(json.loads(export.content).get("openCTI_version") or "")
    title = (name or dashboard.title or "Dashboard").strip()[:200]
    from app.services.dashboard_definition import upgrade

    subtitle = upgrade(dashboard.spec).get("description")
    short = " ".join(
        (short_description or subtitle or f"{title}: an OpenCTI dashboard shared from XTM One").split()
    )[:_HUB_SHORT_DESCRIPTION]
    try:
        client = await resolve_platform_client(db, user_id, integration_id, "xtm_hub")
    except PlatformAccessError:
        raise PublishError("The XTM Hub integration is not one you can use.") from None
    base_url = xtm_hub_base_url(client.base_url)
    with egress_route(client.route):
        token, err = await hub_api_token(base_url, client.credential)
        library_id = None
        if not err:
            library_id, err = await find_publishable_library(base_url, token, HUB_DASHBOARDS_LIBRARY)
        created = None
        if not err and library_id:
            created, err = await publish_hub_resource(
                base_url,
                token,
                library_id=library_id,
                name=title,
                slug=f"{slug(title) or 'dashboard'}-{secrets.token_hex(3)}",
                short_description=short,
                description=(description or "").strip() or short,
                product_version=version,
                file_bytes=export.content,
                filename=export.filename,
                content_type=export.media_type,
            )
    if err or created is None or library_id is None:
        logger.warning("Dashboard share on the XTM Hub refused: %r", str(err)[:300])
        if err and "UPLOAD" in err:
            raise PublishError(
                "Your XTM Hub account may not publish to an OpenCTI dashboards library: ask for the library's "
                "upload right in your organization."
            ) from None
        raise PublishError("The XTM Hub could not publish the dashboard.") from None
    record = CustomDashboardPublication(
        dashboard_id=dashboard.id,
        revision=dashboard.revision,
        integration_id=client.integration_id,
        target="xtm_hub",
        remote_id=str(created["id"])[:200],
        remote_url=library_page_url(base_url, HUB_DASHBOARDS_LIBRARY, library_id, str(created["id"]))[:1000],
        published_by=user_id,
    )
    db.add(record)
    await db.flush()
    return record, export.notes
_REMOTE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")


def _checked_id(value: object, what: str) -> str:
    if not isinstance(value, str) or not _REMOTE_ID.match(value):
        logger.warning("Dashboard publish: %s answered no usable id: %r", what, str(value)[:100])
        raise PublishError(f"{what} did not say which dashboard it created.")
    return value


def _opencti_id(answer: object, field: str = "workspaceConfigurationImport") -> str:
    if not isinstance(answer, dict) or answer.get("errors"):
        errors = answer.get("errors") if isinstance(answer, dict) else None
        logger.warning("Dashboard publish: OpenCTI refused the import: %r", str(errors)[:300])
        raise PublishError("OpenCTI could not import the dashboard.")
    data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
    created = data.get(field)
    # A custom view comes back as an object, a dashboard as its id.
    return _checked_id(created.get("id") if isinstance(created, dict) else created, "OpenCTI")


def _openaev_id(answer: object) -> str:
    data = answer.get("data") if isinstance(answer, dict) else None
    if isinstance(data, list):
        data = data[0] if data else None
    return _checked_id(data.get("id") if isinstance(data, dict) else None, "OpenAEV")


def serialize_publication(record: CustomDashboardPublication) -> dict:
    return {
        "id": str(record.id),
        "target": record.target,
        "revision": record.revision,
        "integration_id": str(record.integration_id),
        "remote_id": record.remote_id,
        "remote_url": record.remote_url,
        "published_by": str(record.published_by) if record.published_by else None,
        "published_at": record.published_at.isoformat() if record.published_at else None,
    }


__all__ = ["ExportFile", "PublishError", "build_export", "publish_dashboard", "serialize_publication", "share_on_hub"]
