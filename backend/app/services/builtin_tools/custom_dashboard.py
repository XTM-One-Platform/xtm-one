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

"""Built-in custom dashboard tools - an agent builds a UI for its user.

Gated behind the ``custom_dashboards`` built-in tool id:

* ``build_dashboard`` - create a dashboard from widgets, or replace one in place
  by passing its ``dashboard_id``.
* ``edit_dashboard`` - upsert, remove or move widgets by id: refresh one figure
  without regenerating (and risking) the rest.
* ``get_dashboard`` - read a dashboard's widgets back, with their ids.
* ``list_dashboards`` - the user's dashboards, to find the one to refresh.

The suite is built only with the Dashboard Studio (``DASHBOARD_STUDIO``) on,
and then offers live OpenCTI / OpenAEV / OpenCRQ widgets (kind ``platform``)
too, with:

* ``list_platform_integrations`` - the integrations the user can read a
  platform widget through (their ids);
* ``preview_platform_widget`` - draw a platform widget before saving it, as the
  user sees it;
* ``list_widget_packs`` / ``get_widget_pack`` - the library of ready-made widgets;
* ``insert_widgets_from_pack`` - copy a pack's widgets into a dashboard, each
  platform widget bound to an integration the user can use;
* ``save_dashboard_as_pack`` / ``import_widget_pack`` - a pack from a dashboard,
  or from a file of the conversation (an XTM One pack, an OpenCTI or OpenAEV
  dashboard export - after ``download_xtm_hub_resource``, say);
* ``publish_dashboard`` - send a dashboard to OpenCTI or OpenAEV as one of
  theirs, with the user's own access there (a write: gated by the agent's
  approvals like any other);
* ``dashboard_catalog``, ``find_dashboard_data``, ``describe_platform_widget``,
  ``lookup_platform_values``, ``preview_source``, ``add_widget``,
  ``check_dashboard`` - find the data, write a widget right the first time and
  check it (``custom_dashboard_studio``).

What the agent may and may not author
-------------------------------------
Structured widgets, never markup: each widget picks one ``kind`` of
``app.services.custom_dashboard_spec`` and fills its values; the web app draws
it with the design system. Colours are tones, links are platform paths, and a
button that "asks" only puts a prompt in the assistant's composer: a dashboard
an agent wrote can show anything and do nothing on its own.

Every call acts with the user's own tier on the dashboard
(``custom_dashboards.load_dashboard_for``, no admin bypass): their own
dashboards and those shared with them for edit are written, those shared for
reading are read. Each runs on a dedicated short-lived session, never the
caller's ``db``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from app.core.utils import format_exception_message, safe_uuid
from app.services.custom_dashboard_spec import (
    DASHBOARD_STAGE_STATES,
    DASHBOARD_TONES,
    DASHBOARD_WIDGET_KINDS,
    DASHBOARD_WIDTHS,
    MAX_WIDGETS,
    DashboardSpecError,
)
from app.services.dashboard_definition import DATA_TYPES
from app.services.dashboard_platform import (
    IMPORT_FORMS,
    PLATFORM_SOURCES,
    PLATFORM_VIEWS,
    PUBLISH_NOTE,
    PUBLISH_TARGETS,
    native_forms,
    platform_list,
)
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult
from app.tools.tool_events import DASHBOARD_SAVED_EVENT, emit_tool_event

from ._shared import logger

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_SOURCE = "builtin:custom_dashboard"

#: Budget of the spec JSON ``get_dashboard`` returns.
_SPEC_READBACK_BUDGET = 20000

_SIGNED_OUT = "Dashboards can only be built for a signed-in user."

#: A widget's place on the dashboard's 12-column grid.
_LAYOUT_SCHEMA: dict = {
    "type": "object",
    "description": "Place on the 12-column grid: x 0-11, w 1-12 (x + w <= 12), y >= 0, h 1-40 rows of 82 px (a 62 px row and its gap).",
    "properties": {
        "x": {"type": "integer"},
        "y": {"type": "integer"},
        "w": {"type": "integer"},
        "h": {"type": "integer"},
    },
}


def _error(message: str) -> ToolResult:
    return ToolResult(tool_call_id="", content=message, is_error=True)


def _parse_json_arg(name: str, value: object) -> tuple[object, str | None]:
    """An object- or array-typed argument the model sent as a JSON string, parsed."""
    if not isinstance(value, str):
        return value, None
    try:
        return json.loads(value), None
    except ValueError:
        return value, f"{name} must be JSON (an array of objects), not a plain string."


def _optional_revision(value: object) -> tuple[int | None, str | None]:
    if value is None or value == "":
        return None, None
    try:
        revision = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, "revision must be the integer a previous call returned."
    return (revision, None) if revision >= 1 else (None, "revision must be 1 or more.")


def _visualization_ids(spec: object) -> list[str]:
    """The ids of a definition's visualizations, in their stored order."""
    from app.services.dashboard_definition import upgrade

    return list(upgrade(spec).get("visualizations") or {})


def _focus_widget(before: list[str], after: list[str], operations: object = None) -> str | None:
    """The widget the studio's stage should move to after a save, or ``None``.

    The last visualization the save added; else the last one an operation
    named (an upsert, a move, a ``set`` of ``visualizations.ID``, a layout)
    that still exists. A removal or a whole rewrite names none.
    """
    added = [vid for vid in after if vid not in set(before)]
    if added:
        return added[-1]
    kept = set(after)
    for op in reversed(operations if isinstance(operations, list) else []):
        if not isinstance(op, dict):
            continue
        named: object = None
        if op.get("op") == "upsert" and isinstance(op.get("widget"), dict):
            named = op["widget"].get("id")
        elif op.get("op") == "move":
            named = op.get("id")
        elif op.get("op") == "set" and isinstance(op.get("path"), str) and op["path"].startswith("visualizations."):
            named = op["path"].split(".", 1)[1]
        elif op.get("op") == "layout" and isinstance(op.get("items"), list) and op["items"]:
            last = op["items"][-1]
            named = last.get("id") if isinstance(last, dict) else None
        if isinstance(named, str) and named in kept:
            return named
    return None


def _summarize(spec: dict) -> str:
    """One line per visualization: its id, type and place, and its heading - the agent's edit map."""
    from app.services.dashboard_studio import outline

    return outline(spec)


def _saved(dashboard, *, verb: str) -> ToolResult:
    from app.services.custom_dashboards import DASHBOARD_MARKER
    from app.services.dashboard_definition import upgrade

    spec = upgrade(dashboard.spec)
    return ToolResult(
        tool_call_id="",
        content=(
            f"{verb} dashboard **{spec.get('title')}** ({dashboard.widget_count} widgets, "
            f"revision {dashboard.revision}). Widgets by id:\n{_summarize(spec)}\n\n"
            f"dashboard_id: `{dashboard.id}`, revision: {dashboard.revision} - pass them to "
            "edit_dashboard to change a widget; the user's page updates in place.\n"
            f"{DASHBOARD_MARKER.format(id=dashboard.id)}"
        ),
    )


def build_custom_dashboard_tools(
    db: AsyncSession | None,
    user_id: str,
    conversation_id: str | None = None,
    *,
    assignment_run_id: str | None = None,
    dashboard_sink: list[str] | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build the dashboard tools, acting for ``user_id``.

    ``dashboard_sink`` collects the id of every dashboard a call built or
    edited, so the chat shows its card even when the model leaves the marker
    out of its answer (``custom_dashboards.append_markers``).
    """
    from app.services.dashboard_live import ALL_TIME, RANGES

    def _record(dashboard_id: object) -> None:
        if dashboard_sink is not None:
            dashboard_sink.append(str(dashboard_id))
    del db  # every operation opens its own short-lived session

    def _scope() -> dict:
        return {
            "conversation_id": safe_uuid(conversation_id),
            "assignment_run_id": safe_uuid(assignment_run_id),
        }

    async def _build_dashboard(
        title: str,
        widgets: object = None,
        subtitle: str | None = None,
        definition: object = None,
        dashboard_id: str | None = None,
        revision: object = None,
        **_kw: object,
    ) -> ToolResult:
        from app.core.database import async_session
        from app.services.custom_dashboards import load_dashboard_for, save_dashboard

        widgets, problem = _parse_json_arg("widgets", widgets)
        if problem:
            return _error(problem)
        definition, problem = _parse_json_arg("definition", definition)
        if problem:
            return _error(problem)
        if (widgets is None) == (definition is None):
            return _error("Pass either 'widgets' (a list) or 'definition' (a whole version 2 definition), not both.")
        expected, problem = _optional_revision(revision)
        if problem:
            return _error(problem)
        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _error(_SIGNED_OUT)
        target = None
        if dashboard_id:
            target = safe_uuid(str(dashboard_id).strip())
            if target is None:
                return _error("dashboard_id must be the id a previous call returned (omit it to create one).")

        if definition is not None:
            from app.services.dashboard_native import EXTENSION, is_native

            if is_native(definition):
                # A manifest (what the source editor shows): its title and description live in the extension.
                extension = {**definition[EXTENSION], "title": title}  # type: ignore[index]
                if subtitle:
                    extension["description"] = subtitle
                payload = {**definition, EXTENSION: extension}  # type: ignore[dict-item]
            else:
                payload = {**definition, "version": 2, "title": title} if isinstance(definition, dict) else definition
                if subtitle and isinstance(payload, dict):
                    payload["description"] = subtitle
        else:
            payload = {"title": title, "widgets": widgets}
            if subtitle:
                payload["subtitle"] = subtitle
        try:
            async with async_session() as own_db:
                existing = None
                if target is not None:
                    found = await load_dashboard_for(
                        own_db, target, user_id=user_uuid, need="edit", for_update=True
                    )
                    if found is None:
                        return _error(
                            f"No dashboard `{dashboard_id}` you can edit was found. Omit dashboard_id to create one."
                        )
                    existing = found[0]
                dashboard = await save_dashboard(
                    own_db,
                    user_id=user_uuid,
                    spec_payload=payload,
                    dashboard=existing,
                    expected_revision=expected,
                    **_scope(),
                )
                # Read before the commit: nothing of the row is reloaded after it.
                result = _saved(dashboard, verb="Updated" if existing else "Created")
                saved_id, saved_revision = dashboard.id, dashboard.revision
                await own_db.commit()
                _record(saved_id)
                await emit_tool_event(DASHBOARD_SAVED_EVENT, dashboard_id=str(saved_id), revision=saved_revision)
                return result
        except DashboardSpecError as exc:
            return _error(str(exc))
        except Exception as exc:
            logger.error("build_dashboard failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the dashboard could not be saved. Please try again.")

    async def _edit_dashboard(
        dashboard_id: str,
        operations: object = None,
        title: str | None = None,
        subtitle: str | None = None,
        revision: object = None,
        **_kw: object,
    ) -> ToolResult:
        from app.core.database import async_session
        from app.services.custom_dashboards import edit_dashboard, load_dashboard_for

        operations, problem = _parse_json_arg("operations", operations if operations is not None else [])
        if problem:
            return _error(problem)
        expected, problem = _optional_revision(revision)
        if problem:
            return _error(problem)
        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _error(_SIGNED_OUT)
        target = safe_uuid(str(dashboard_id or "").strip())
        if target is None:
            return _error("dashboard_id must be the id build_dashboard or list_dashboards returned.")
        try:
            async with async_session() as own_db:
                found = await load_dashboard_for(own_db, target, user_id=user_uuid, need="edit", for_update=True)
                if found is None:
                    return _error(f"No dashboard `{dashboard_id}` you can edit was found.")
                dashboard = found[0]
                before = _visualization_ids(dashboard.spec)
                await edit_dashboard(
                    own_db,
                    dashboard,
                    operations=operations,
                    title=title,
                    subtitle=subtitle,
                    expected_revision=expected,
                    acting_user_id=user_uuid,
                    **_scope(),
                )
                result = _saved(dashboard, verb="Updated")
                saved_id, saved_revision = dashboard.id, dashboard.revision
                focus = _focus_widget(before, _visualization_ids(dashboard.spec), operations)
                await own_db.commit()
                _record(saved_id)
                await emit_tool_event(
                    DASHBOARD_SAVED_EVENT, dashboard_id=str(saved_id), revision=saved_revision, focus_widget_id=focus
                )
                return result
        except DashboardSpecError as exc:
            return _error(str(exc))
        except Exception as exc:
            logger.error("edit_dashboard failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the dashboard could not be saved. Please try again.")

    async def _get_dashboard(dashboard_id: str, part: str | None = None, **_kw: object) -> ToolResult:
        from app.core.database import async_session
        from app.services.custom_dashboards import load_dashboard_for

        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _error(_SIGNED_OUT)
        target = safe_uuid(str(dashboard_id or "").strip())
        if target is None:
            return _error("dashboard_id must be a dashboard id (list_dashboards lists them).")
        try:
            async with async_session() as own_db:
                found = await load_dashboard_for(own_db, target, user_id=user_uuid)
                if found is None:
                    return _error(f"No dashboard `{dashboard_id}` you can read was found.")
                dashboard, tier = found
                from app.services.dashboard_definition import upgrade

                spec, current = upgrade(dashboard.spec), dashboard.revision
        except Exception as exc:
            logger.error("get_dashboard failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the dashboard could not be read. Please try again.")
        if part:
            # One element of a definition too long to read whole: what an edit of it starts from.
            group, _, eid = str(part).strip().partition(".")
            element = spec.get(group)
            if eid and isinstance(element, dict):
                element = element.get(eid)
            if element is None or group not in ("dataSources", "visualizations", "inputs", "layout", "defaults", "expressions", "applicationProperties"):
                return _error(
                    "part is a whole element: layout, defaults, expressions, applicationProperties, dataSources, "
                    "visualizations, inputs, or one of them by id (visualizations.ID, dataSources.ID, inputs.ID)."
                )
            text = json.dumps(element, ensure_ascii=False, separators=(",", ":"))
            if len(text) > _SPEC_READBACK_BUDGET:
                text = text[:_SPEC_READBACK_BUDGET] + f"… (truncated, {len(text)} chars total)"
            return ToolResult(tool_call_id="", content=f"`{part}` of dashboard **{spec.get('title')}** (revision {current}):\n{text}")
        payload = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
        if len(payload) > _SPEC_READBACK_BUDGET:
            payload = payload[:_SPEC_READBACK_BUDGET] + (
                f"… (truncated, {len(payload)} chars total: read one element with get_dashboard part, "
                "such as part='layout' or part='visualizations.ID')"
            )
        access = "you can edit it" if tier in ("owner", "manage", "config") else "shared with you to read: you cannot change it"
        return ToolResult(
            tool_call_id="",
            content=(
                f"Dashboard **{spec.get('title')}** (revision {current}; {access}). Widgets by id:\n"
                f"{_summarize(spec)}\n\nCurrent definition:\n{payload}"
            ),
        )

    async def _list_dashboards(**_kw: object) -> ToolResult:
        from app.core.database import async_session
        from app.services.custom_dashboards import list_visible_dashboards

        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _error(_SIGNED_OUT)
        try:
            async with async_session() as own_db:
                rows = await list_visible_dashboards(own_db, user_id=user_uuid, limit=50)
                lines = [
                    f"- `{row.id}` {row.title} ({row.widget_count} widgets, revision {row.revision}, "
                    f"updated {row.updated_at.date().isoformat() if row.updated_at else 'unknown'}"
                    f"{'' if tier == 'owner' else ', shared with you to edit' if tier in ('config', 'manage') else ', read only'})"
                    for row, tier in rows
                ]
        except Exception as exc:
            logger.error("list_dashboards failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the dashboards could not be listed. Please try again.")
        if not lines:
            return ToolResult(tool_call_id="", content="The user has no dashboard yet. build_dashboard creates one.")
        return ToolResult(tool_call_id="", content="The user's dashboards, most recent first:\n" + "\n".join(lines))

    async def _list_platform_integrations(**_kw: object) -> ToolResult:
        from app.core.database import async_session
        from app.services.content_safety import wrap_untrusted
        from app.services.platform_access import visible_platform_integrations

        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _error(_SIGNED_OUT)
        try:
            async with async_session() as own_db:
                rows = await visible_platform_integrations(own_db, user_uuid)
                lines = [f"- {row.provider} `{row.id}` {row.name}" for row in rows]
        except Exception as exc:
            logger.error("list_platform_integrations failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the integrations could not be listed. Please try again.")
        if not lines:
            return ToolResult(
                tool_call_id="",
                content=f"The user can read no {platform_list()} integration: platform widgets are not possible.",
            )
        listing = wrap_untrusted("\n".join(lines), "integration names")
        return ToolResult(
            tool_call_id="",
            content=f"Integrations a platform widget can read through (source, integration_id, name):\n{listing}",
        )

    async def _preview_platform_widget(widget: object, range: str | None = None, **_kw: object) -> ToolResult:
        from app.core.database import async_session
        from app.services.content_safety import wrap_untrusted
        from app.services.custom_dashboard_spec import validate_widget
        from app.services.dashboard_live import ALL_TIME, RANGES, read_widget_data, request_for

        widget, problem = _parse_json_arg("widget", widget)
        if problem:
            return _error(problem)
        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _error(_SIGNED_OUT)
        if not isinstance(widget, dict) or widget.get("kind") != "platform":
            return _error("widget must be a platform widget: {kind: 'platform', source, integration_id, native}.")
        if range is not None and range not in RANGES and range != ALL_TIME:
            return _error(f"range must be one of: {', '.join([*RANGES, ALL_TIME])}.")
        try:
            checked = validate_widget(widget, where="widget")
        except DashboardSpecError as exc:
            return _error(str(exc))
        try:
            async with async_session() as own_db:
                result = await read_widget_data(own_db, user_uuid, request_for(checked, range))
        except Exception as exc:
            logger.error("preview_platform_widget failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the widget could not be previewed. Please try again.")
        if result.get("status") != "ok":
            return _error(
                f"The widget could not be drawn: {result.get('code')}. "
                + (
                    "XTM One does not draw this type; it is kept and shown as a link to the platform."
                    if result.get("code") == "unsupported"
                    else "Check the integration and the configuration, or try again later."
                )
            )
        data = json.dumps(result.get("data"), ensure_ascii=False, separators=(",", ":"))[:_SPEC_READBACK_BUDGET]
        block = wrap_untrusted(data, f"{checked['source']} widget data")
        return ToolResult(
            tool_call_id="",
            content=(
                f"The widget draws {result.get('shape')} data (view '{checked.get('view')}'), as the user sees it:\n"
                f"{block}"
            ),
        )

    async def _list_widget_packs(source: str | None = None, **_kw: object) -> ToolResult:
        from app.core.database import async_session
        from app.services.content_safety import wrap_untrusted
        from app.services.dashboard_widget_packs import list_visible_packs

        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _error(_SIGNED_OUT)
        if source is not None and source not in PLATFORM_SOURCES:
            return _error(f"source must be one of: {', '.join(PLATFORM_SOURCES)}.")
        try:
            async with async_session() as own_db:
                rows = await list_visible_packs(own_db, user_id=user_uuid, source=source, limit=50)
                lines = [
                    f"- `{pack.id}` {pack.name} ({pack.widget_count} widgets"
                    f"{', reads ' + '/'.join(pack.sources) if pack.sources else ''}"
                    f"{', built-in' if 'builtin' in (pack.tags or []) else ''})"
                    for pack, _manage in rows
                ]
        except Exception as exc:
            logger.error("list_widget_packs failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the widget packs could not be listed. Please try again.")
        if not lines:
            return ToolResult(tool_call_id="", content="No widget pack is available to the user yet.")
        listing = wrap_untrusted("\n".join(lines), "widget pack names")
        return ToolResult(tool_call_id="", content=f"Widget packs (id, name, widgets):\n{listing}")

    async def _get_widget_pack(pack_id: str, **_kw: object) -> ToolResult:
        from app.core.database import async_session
        from app.services.content_safety import wrap_untrusted
        from app.services.dashboard_widget_packs import load_pack_for

        user_uuid = safe_uuid(user_id)
        target = safe_uuid(str(pack_id or "").strip())
        if user_uuid is None:
            return _error(_SIGNED_OUT)
        if target is None:
            return _error("pack_id must be an id list_widget_packs returned.")
        async with async_session() as own_db:
            found = await load_pack_for(own_db, target, user_id=user_uuid)
        if found is None:
            return _error(f"No widget pack `{pack_id}` you can use was found.")
        pack = found[0]
        body = json.dumps(pack.widgets or [], ensure_ascii=False, separators=(",", ":"))[:_SPEC_READBACK_BUDGET]
        return ToolResult(
            tool_call_id="",
            content=(
                f"Widget pack `{pack.id}` ({pack.widget_count} widgets). Its widgets (platform ones carry no "
                "integration_id: insert_widgets_from_pack binds them):\n" + wrap_untrusted(body, "widget pack")
            ),
        )

    async def _insert_widgets_from_pack(
        pack_id: str,
        dashboard_id: str,
        widget_ids: object = None,
        bindings: object = None,
        after: str | None = None,
        revision: object = None,
        **_kw: object,
    ) -> ToolResult:
        from app.core.database import async_session
        from app.services.custom_dashboards import load_dashboard_for
        from app.services.dashboard_widget_packs import PackError, insert_pack_widgets, load_pack_for

        widget_ids, problem = _parse_json_arg("widget_ids", widget_ids)
        if problem:
            return _error(problem)
        bindings, problem = _parse_json_arg("bindings", bindings)
        if problem:
            return _error(problem)
        expected, problem = _optional_revision(revision)
        if problem:
            return _error(problem)
        user_uuid = safe_uuid(user_id)
        pack_uuid = safe_uuid(str(pack_id or "").strip())
        dashboard_uuid = safe_uuid(str(dashboard_id or "").strip())
        if user_uuid is None:
            return _error(_SIGNED_OUT)
        if pack_uuid is None or dashboard_uuid is None:
            return _error("pack_id and dashboard_id must be ids the other dashboard tools returned.")
        if widget_ids is not None and (not isinstance(widget_ids, list) or not all(isinstance(w, str) for w in widget_ids)):
            return _error("widget_ids must be a list of widget ids of the pack.")
        if bindings is not None and not isinstance(bindings, dict):
            return _error(f"bindings must map a platform ({', '.join(PLATFORM_SOURCES)}) to an integration_id.")
        try:
            async with async_session() as own_db:
                found_pack = await load_pack_for(own_db, pack_uuid, user_id=user_uuid)
                if found_pack is None:
                    return _error(f"No widget pack `{pack_id}` you can use was found.")
                found = await load_dashboard_for(own_db, dashboard_uuid, user_id=user_uuid, need="edit", for_update=True)
                if found is None:
                    return _error(f"No dashboard `{dashboard_id}` you can edit was found.")
                dashboard = found[0]
                before = _visualization_ids(dashboard.spec)
                inserted = await insert_pack_widgets(
                    own_db, user_id=user_uuid, dashboard=dashboard, pack=found_pack[0], widget_ids=widget_ids,
                    bindings={str(k): str(v) for k, v in (bindings or {}).items()}, after=after,
                    expected_revision=expected, source="agent",
                )  # fmt: skip
                result = _saved(dashboard, verb=f"Inserted {len(inserted)} widgets into")
                saved_id, saved_revision = dashboard.id, dashboard.revision
                focus = _focus_widget(before, _visualization_ids(dashboard.spec))
                await own_db.commit()
                _record(saved_id)
                await emit_tool_event(
                    DASHBOARD_SAVED_EVENT, dashboard_id=str(saved_id), revision=saved_revision, focus_widget_id=focus
                )
                return result
        except (PackError, DashboardSpecError) as exc:
            return _error(str(exc))
        except Exception as exc:
            logger.error("insert_widgets_from_pack failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the widgets could not be inserted. Please try again.")

    async def _save_dashboard_as_pack(
        dashboard_id: str, name: str, description: str | None = None, widget_ids: object = None, **_kw: object
    ) -> ToolResult:
        from app.core.database import async_session
        from app.services.custom_dashboards import load_dashboard_for
        from app.services.dashboard_widget_packs import PackError, pack_from_dashboard

        widget_ids, problem = _parse_json_arg("widget_ids", widget_ids)
        if problem:
            return _error(problem)
        user_uuid = safe_uuid(user_id)
        dashboard_uuid = safe_uuid(str(dashboard_id or "").strip())
        if user_uuid is None:
            return _error(_SIGNED_OUT)
        if dashboard_uuid is None:
            return _error("dashboard_id must be the id of a dashboard.")
        try:
            async with async_session() as own_db:
                found = await load_dashboard_for(own_db, dashboard_uuid, user_id=user_uuid)
                if found is None:
                    return _error(f"No dashboard `{dashboard_id}` you can read was found.")
                pack = await pack_from_dashboard(
                    own_db, user_id=user_uuid, dashboard=found[0], name=str(name or ""), description=description,
                    widget_ids=widget_ids if isinstance(widget_ids, list) else None,
                )  # fmt: skip
                pack_id, count = pack.id, pack.widget_count
                await own_db.commit()
        except PackError as exc:
            return _error(str(exc))
        except Exception as exc:
            logger.error("save_dashboard_as_pack failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the widget pack could not be saved. Please try again.")
        return ToolResult(tool_call_id="", content=f"Saved widget pack `{pack_id}` ({count} widgets), private to the user.")

    async def _import_widget_pack(file_id: str, name: str | None = None, **_kw: object) -> ToolResult:
        from app.core.database import async_session
        from app.models.user import User
        from app.services.dashboard_interop import MAX_FILE_BYTES
        from app.services.dashboard_widget_packs import PackError, import_pack
        from app.services.file_access import load_readable_file
        from app.services.file_storage import file_storage

        user_uuid = safe_uuid(user_id)
        if user_uuid is None:
            return _error(_SIGNED_OUT)
        try:
            async with async_session() as own_db:
                user = await own_db.get(User, user_uuid)
                try:
                    row = await load_readable_file(own_db, file_id, user)
                except Exception:
                    return _error(f"No file `{file_id}` you can read was found.")
                if (row.size or 0) > MAX_FILE_BYTES:
                    return _error("The file is larger than 5 MB.")
                data = await file_storage.download(row.s3_object_name)
                pack = await import_pack(own_db, user_id=user_uuid, data=data, filename=row.filename, name=name)
                pack_id, count, notes = pack.id, pack.widget_count, list((pack.source_meta or {}).get("notes") or [])
                await own_db.commit()
        except PackError as exc:
            return _error(str(exc))
        except Exception as exc:
            logger.error("import_widget_pack failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the widget pack could not be imported. Please try again.")
        said = f"Imported widget pack `{pack_id}` ({count} widgets), private to the user."
        if notes:
            said += "\nWhat the conversion changed:\n" + "\n".join(f"- {n}" for n in notes[:10])
        return ToolResult(tool_call_id="", content=said)

    async def _publish_dashboard(
        dashboard_id: str, target: str, integration_id: str, name: str | None = None, **_kw: object
    ) -> ToolResult:
        from app.core.database import async_session
        from app.services.custom_dashboards import load_dashboard_for
        from app.services.dashboard_publish import PublishError, publish_dashboard

        user_uuid = safe_uuid(user_id)
        dashboard_uuid = safe_uuid(str(dashboard_id or "").strip())
        if user_uuid is None:
            return _error(_SIGNED_OUT)
        if dashboard_uuid is None:
            return _error("dashboard_id must be the id of a dashboard.")
        if target not in PUBLISH_TARGETS:
            return _error(f"target must be one of: {', '.join(PUBLISH_TARGETS)}. {PUBLISH_NOTE}")
        try:
            async with async_session() as own_db:
                found = await load_dashboard_for(own_db, dashboard_uuid, user_id=user_uuid)
                if found is None:
                    return _error(f"No dashboard `{dashboard_id}` you can read was found.")
                record, notes = await publish_dashboard(
                    own_db, user_id=user_uuid, dashboard=found[0], target=target,  # type: ignore[arg-type]
                    integration_id=str(integration_id or ""), name=name,
                )  # fmt: skip
                url = record.remote_url
                await own_db.commit()
        except PublishError as exc:
            return _error(str(exc))
        except Exception as exc:
            logger.error("publish_dashboard failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the dashboard could not be published. Please try again.")
        said = f"Published to {target}: {url}"
        if notes:
            said += "\nLeft out (the platform cannot hold them):\n" + "\n".join(f"- {n}" for n in notes[:10])
        return ToolResult(tool_call_id="", content=said)

    async def _export_dashboard(dashboard_id: str, format: str = "pdf", tabs: object = None, **_kw: object) -> ToolResult:
        from app.core.database import async_session
        from app.llm.base import FileRef
        from app.services.custom_dashboards import load_dashboard_for
        from app.services.dashboard_definition import upgrade
        from app.services.dashboard_export import DashboardRenderError, export_dashboard

        from ._shared import _persist_generated_file

        export_format = (format or "pdf").strip().lower()
        if export_format not in ("pdf", "png"):
            return _error("format is pdf or png.")
        doc_id, user_uuid = safe_uuid((dashboard_id or "").strip()), safe_uuid(user_id)
        if doc_id is None or user_uuid is None:
            return _error("dashboard_id must be the id of a dashboard you can open.")
        layout_ids = [str(t) for t in tabs][:10] if isinstance(tabs, list) and tabs else None
        try:
            async with async_session() as own_db:
                # The user's own tiers, as every dashboard tool reads (no admin reach for an agent).
                found = await load_dashboard_for(own_db, doc_id, user_id=user_uuid)
                if found is None:
                    return _error(f"No dashboard `{dashboard_id}` you can open was found.")
                title = found[0].title
                layouts = upgrade(found[0].spec)["layout"]["layoutDefinitions"]
            if layout_ids and any(lid not in layouts for lid in layout_ids):
                return _error(f"tabs are layout ids of the dashboard: {', '.join(list(layouts)[:10])}.")
            # Outside the session: the render takes seconds.
            exported = await export_dashboard(
                dashboard_id=doc_id, user_id=user_uuid, title=title, export_format=export_format, layout_ids=layout_ids
            )
            file_att = await _persist_generated_file(
                user_id=user_id,
                filename=exported.filename,
                raw_bytes=exported.data,
                content_type=exported.content_type,
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
            )
        except DashboardRenderError:
            return _error("The dashboard could not be rendered. Try again later, or download it from its page.")
        except Exception as exc:
            logger.error("export_dashboard failed: %s", format_exception_message(exc), exc_info=True)
            return _error("The dashboard could not be exported (the platform's headless browser is unavailable).")
        return ToolResult(
            tool_call_id="",
            content=f"Exported “{title}” as a {export_format.upper()}: [[FILE:{file_att.id}]]",
            files=[FileRef.from_attachment(file_att)],
        )

    async def _send_dashboard_export(
        dashboard_id: str,
        recipients: object,
        format: str = "pdf",
        subject: str | None = None,
        message: str | None = None,
        tabs: object = None,
        **_kw: object,
    ) -> ToolResult:
        from app.services.dashboard_export import MAX_RECIPIENTS, send_dashboard_export

        export_format = (format or "pdf").strip().lower()
        if export_format not in ("pdf", "png"):
            return _error("format is pdf or png.")
        doc_id, user_uuid = safe_uuid((dashboard_id or "").strip()), safe_uuid(user_id)
        if doc_id is None or user_uuid is None:
            return _error("dashboard_id must be the id of a dashboard you can open.")
        if not isinstance(recipients, list) or not recipients:
            return _error(f"recipients is a list of email addresses of people with an account (at most {MAX_RECIPIENTS}).")
        layout_ids = [str(t) for t in tabs][:10] if isinstance(tabs, list) and tabs else None
        try:
            report = await send_dashboard_export(
                dashboard_id=doc_id,
                sender_id=user_uuid,
                recipients=[str(r) for r in recipients][:50],
                export_format=export_format,  # type: ignore[arg-type]
                subject=subject,
                message=message,
                layout_ids=layout_ids,
            )
        except LookupError:
            return _error(f"No dashboard `{dashboard_id}` you can open was found.")
        except RuntimeError:
            return _error("Email is not configured on this platform: the export cannot be sent.")
        reasons = {
            "no_account": "no account on this platform",
            "no_access": "cannot open this dashboard",
            "invalid": "not an email address",
            "too_many": f"more than {MAX_RECIPIENTS} recipients",
            "render_failed": "the dashboard could not be rendered",
            "send_failed": "the email could not be sent",
        }
        lines = [f"Sent to: {', '.join(report.sent)}." if report.sent else "Sent to nobody."]
        lines += [f"Not sent to {address}: {reasons.get(code, code)}." for address, code in report.not_sent.items()]
        return ToolResult(tool_call_id="", content="\n".join(lines), is_error=not report.sent)

    executor = CallableToolExecutor()
    executor.register("build_dashboard", _build_dashboard)
    executor.register("edit_dashboard", _edit_dashboard)
    executor.register("get_dashboard", _get_dashboard)
    executor.register("list_dashboards", _list_dashboards)
    from app.core.feature_flags import is_feature_enabled

    studio = is_feature_enabled("DASHBOARD_STUDIO")
    if studio:
        executor.register("list_platform_integrations", _list_platform_integrations)
        executor.register("preview_platform_widget", _preview_platform_widget)
        executor.register("list_widget_packs", _list_widget_packs)
        executor.register("get_widget_pack", _get_widget_pack)
        executor.register("insert_widgets_from_pack", _insert_widgets_from_pack)
        executor.register("save_dashboard_as_pack", _save_dashboard_as_pack)
        executor.register("import_widget_pack", _import_widget_pack)
        executor.register("publish_dashboard", _publish_dashboard)
        executor.register("export_dashboard", _export_dashboard)
        executor.register("send_dashboard_export", _send_dashboard_export)
        from .custom_dashboard_studio import build_dashboard_studio_tools

        studio_defs, studio_handlers = build_dashboard_studio_tools(
            user_id=user_id,
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
            edit=_edit_dashboard,
            source=_SOURCE,
        )
        for name, handler in studio_handlers.items():
            executor.register(name, handler)

    tones = "|".join(DASHBOARD_TONES)
    action = "{label, href: a platform path like /agents | prompt: a question for the assistant}"
    widget_help = (
        "Widgets (each needs 'kind' plus that kind's keys; unknown keys are rejected). Every widget also takes "
        f"'id' (a short stable handle like 'score', for later edits), 'title' (its heading), 'layout' "
        "({x, y, w, h} on a 12-column grid of 82 px rows; omitted: placed below the others), 'width' "
        f"({'|'.join(DASHBOARD_WIDTHS)}: how wide a widget without a layout is; phones stack every widget) and 'link' "
        f"({action}, shown beside the heading):\n"
        f"- score: label, value (number), max, unit, trend {{direction: up|down|flat, label, tone}}, series "
        f"([numbers] oldest first, 2-60, drawn as a line), series_label, stats (0-4 of {{label, value, caption, tone}})\n"
        "- stats: items (1-6 of {label, value, caption, tone}) - a row of figures\n"
        f"- stages: items (1-8 of {{label, state: {'|'.join(DASHBOARD_STAGE_STATES)}, status, progress 0-1}}) - "
        "a cycle or pipeline\n"
        f"- decision: eyebrow, time, text, tags ([str] 0-4), actions (1-2 of {action}; the first is primary) - "
        "something waiting for the user\n"
        "- feed: items (1-12 of {badge (<=6 chars), title, time, text, href}) - recent events\n"
        f"- callout: eyebrow, text, tone, actions (0-2) - a highlighted statement\n"
        "- ranked_list: items (1-12 of {title, meta, badge {label, tone}, href}), numbered (bool)\n"
        "- steps: caption, items (1-12 of {label, detail, tone}) - a sequence with outcomes (an attack path)\n"
        "- checklist: eyebrow, items (1-20 of {label, done}), actions (0-2) - a read-only plan\n"
        "- progress: status {label, tone}, text, progress 0-1, progress_label, lines (0-8 of {label, tone}), "
        "actions (0-2) - work under way\n"
        "- prompts: placeholder, suggestions ([str] 1-6) - follow-up questions for the assistant\n"
        "- text: body - prose\n"
        f"Tones: {tones}. Text fields take inline markup only: **bold**, _em_, `code`, [[tone:text]]."
    )
    if studio:
        widget_help += (
            f"\n- platform: LIVE data from {platform_list()}, read with the viewer's own access each time "
            f"the page opens. source ({'|'.join(PLATFORM_SOURCES)}), integration_id (from list_platform_integrations), "
            f"view ({'|'.join(PLATFORM_VIEWS)}; must suit the data), native: the platform's own "
            f"configuration - {platform_list(native_forms())}. Check it with preview_platform_widget before saving."
        )
    kinds = [k for k in DASHBOARD_WIDGET_KINDS if studio or k != "platform"]

    definitions = [
        ToolDefinition(
            name="build_dashboard",
            description=(
                "Build a CUSTOM DASHBOARD the user opens in the web app (and on their phone): a page of live-looking "
                "widgets you compose from data you gathered with your other tools - a morning brief, an exposure "
                "overview, the status of their agents. Use it when the user asks for a dashboard, a cockpit, a "
                "page or a view to keep and come back to; a one-off answer stays in the chat, a printable document "
                "is generate_report.\n"
                "You compose structured widgets, NOT HTML: the renderer owns the layout, colours and responsive "
                "behaviour. Use real figures you obtained; never invent data. A button never acts by itself: "
                "'href' opens a platform page, 'prompt' puts a question in the assistant's composer.\n"
                f"{widget_help}\n"
                "To change an existing dashboard prefer edit_dashboard (one widget at a time). Passing dashboard_id "
                "here replaces ALL its widgets.\n"
                "Include the returned [[DASHBOARD:...]] marker verbatim in your reply so the dashboard renders in "
                "the chat."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Dashboard title (max 120 characters)."},
                    "subtitle": {"type": "string", "description": "A line under the title, e.g. the as-of date."},
                    "widgets": {
                        "type": "array",
                        "description": f"Ordered widgets, top to bottom (1-{MAX_WIDGETS}).",
                        "items": {
                            "type": "object",
                            "properties": {
                                # Live platform widgets are offered with the Dashboard Studio.
                                "kind": {"type": "string", "enum": kinds},
                                "id": {"type": "string", "description": "Stable handle, e.g. 'score'."},
                                "title": {"type": "string"},
                                "width": {"type": "string", "enum": list(DASHBOARD_WIDTHS)},
                                "layout": _LAYOUT_SCHEMA,
                            },
                            "required": ["kind"],
                        },
                    },
                    "dashboard_id": {
                        "type": "string",
                        "description": "Replace this dashboard's widgets in place. Omit to create a new dashboard.",
                    },
                    "revision": {
                        "type": "integer",
                        "description": "With dashboard_id: the revision you read; refused if it changed since.",
                    },
                    **(
                        {
                            "definition": {
                                "type": "object",
                                "description": (
                                    "Instead of widgets: the whole version 2 definition - dataSources (platform | "
                                    "static {shape, data} | assignment {assignment_id, shape, path} | ask {agent_id, "
                                    "question, shape} | variable {variable_id, shape} | integration {integration_id, tool (one that only "
                                    "reads), arguments, shape, path} | knowledge {knowledge_base_id, document_id, section?, "
                                    "format, shape, path} | derived {extends, steps: where / "
                                    "sort / head / tail / fields / rename / eval / top / stats}; shape number | series | "
                                    "distribution | rows | text | geo | graph | events | tree; refresh '5m'; refreshMode server (read by "
                                    "the platform every refresh, 5m at least, with your access, shown to every viewer), "
                                    "schedule (cron, UTC), event (on {platformEvent} | {integrationEvent, integrationId?}) or "
                                    "manual; alerts [{id, op gt|gte|lt|lte|eq|ne, value, label?, severity info|warning|critical}] on "
                                    "a source read on the server tell the dashboard's owner and its publisher when its figure "
                                    "starts to cross them; an assignment source's history (2-60) draws its runs' figure as a series), "
                                    "visualizations (xtm.KIND (a widget kind) with its fields as options, or a data "
                                    "type reading dataSources.primary: "
                                    f"{', '.join(DATA_TYPES)}; its options in camelCase, "
                                    "e.g. stackMode, legendDisplay, majorValue, columnFormat, and dynamic options "
                                    "like \"> primary | seriesByName('count') | lastPoint()\"; a colour by ranges is "
                                    "majorColor '> majorValue | rangeValue(majorColorEditorConfig)' with context.majorColorEditorConfig "
                                    "[{from, to, value: '#rrggbb'}]), inputs (input.timerange | dropdown | multiselect | "
                                    "text | number with a token and defaultValue, items or dataSources.primary; "
                                    "input.button with actions setToken / unsetToken / link / switchTab; a platform "
                                    "filter value, a derived step value or a platform source's range may be '$token$'; "
                                    "expressions.tokens computes tokens; layout.options.submitButton), a visualization's "
                                    "eventHandlers (drilldown.setToken {tokens: [{token, key: name|value|x|y|row.FIELD.value}]}, "
                                    "unsetToken, linkToUrl, linkToDashboard {dashboardId, tokens}, openInPlatform, switchTab, "
                                    "setTimeRange) and containerOptions.visibility (showConditions / hideConditions naming "
                                    "expressions.conditions.ID {name, value: 'isSet($t$)'}, hideWhenNoData), layout (tabs + layoutDefinitions with "
                                    "structure [{item, position {x, y, w, h}}]), defaults, expressions. "
                                    "get_dashboard shows one. The dashboard's stored manifest (platform widgets in their "
                                    "platform's shape, the rest under x_xtm_one, as the source editor shows it) is accepted too."
                                ),
                            }
                        }
                        if studio
                        else {}
                    ),
                },
                "required": ["title"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="edit_dashboard",
            description=(
                "Change an existing dashboard widget by widget: refresh a figure, add a widget, drop one, reorder. "
                "operations run in order and are saved together or not at all:\n"
                "- {op: 'upsert', widget: {...full widget with its id...}, after?: id|'start'} replaces the widget "
                "with that id, or adds it (last, or after the named widget)\n"
                "- {op: 'remove', id}\n"
                "- {op: 'move', id, after: id|'start'} (the grid re-flows in the new order)\n"
                "- {op: 'layout', items: [{id, x, y, w, h}], layoutId?} places or resizes widgets on the 12-column grid "
                "(or in pixels on a canvas tab); a visualization set but placed in no tab yet is added to that tab\n"
                + (
                    "- {op: 'set', path: 'visualizations.ID' | 'dataSources.ID' | 'inputs.ID' | 'layout' | "
                    "'defaults' | 'expressions', value: the whole element} and {op: 'delete', path} edit the "
                    "definition directly (get_dashboard shows it)\n"
                    if studio
                    else ""
                )
                + "An upsert without 'layout' keeps the widget where it is. Widget shapes are build_dashboard's. Use "
                "get_dashboard to read the current widgets, their layouts and the revision."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dashboard_id": {"type": "string"},
                    "operations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": ["upsert", "remove", "move", "layout", *(["set", "delete"] if studio else [])],
                                },
                                "layoutId": {"type": "string"},
                                "path": {"type": "string"},
                                "value": {},
                                "widget": {"type": "object"},
                                "id": {"type": "string"},
                                "after": {"type": "string"},
                                "items": {
                                    "type": "array",
                                    "items": {**_LAYOUT_SCHEMA, "properties": {"id": {"type": "string"}, **_LAYOUT_SCHEMA["properties"]}},
                                },
                            },
                            "required": ["op"],
                        },
                    },
                    "title": {"type": "string", "description": "A new title."},
                    "subtitle": {"type": "string", "description": "A new subtitle ('' removes it)."},
                    "revision": {"type": "integer", "description": "The revision you read; refused if it changed."},
                },
                "required": ["dashboard_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_dashboard",
            description=(
                "Read a dashboard's current widgets (with their ids), definition and revision, before editing it; "
                "'part' reads one element of a long definition (layout, dataSources, visualizations.ID, ...)."
            ),
            input_schema={
                "type": "object",
                "properties": {"dashboard_id": {"type": "string"}, "part": {"type": "string"}},
                "required": ["dashboard_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_dashboards",
            description="List the user's dashboards (id, title, revision), most recently updated first.",
            input_schema={"type": "object", "properties": {}},
            source=_SOURCE,
        ),
    ]
    if studio:
        definitions += [
            ToolDefinition(
                name="list_platform_integrations",
                description=(
                    f"List the {platform_list(last='and')} integrations the user can read a live 'platform' "
                    "dashboard widget through, with their integration_id."
                ),
                input_schema={"type": "object", "properties": {}},
                source=_SOURCE,
            ),
            ToolDefinition(
                name="preview_platform_widget",
                description=(
                    "Draw a live 'platform' widget before saving it, with the user's own access: returns the "
                    "figures it shows, or why it cannot be drawn. Use it to check a native configuration."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "widget": {"type": "object", "description": "A full platform widget (build_dashboard's shape)."},
                        "range": {"type": "string", "enum": [*RANGES, ALL_TIME]},
                    },
                    "required": ["widget"],
                },
                source=_SOURCE,
            ),
            ToolDefinition(
                name="list_widget_packs",
                description=(
                    "List the widget packs the user can use - ready-made dashboard widgets, built-in or imported - "
                    "optionally only those reading one platform."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"source": {"type": "string", "enum": list(PLATFORM_SOURCES)}},
                },
                source=_SOURCE,
            ),
            ToolDefinition(
                name="get_widget_pack",
                description="Read a widget pack's widgets (their ids, kinds and configuration).",
                input_schema={
                    "type": "object",
                    "properties": {"pack_id": {"type": "string"}},
                    "required": ["pack_id"],
                },
                source=_SOURCE,
            ),
            ToolDefinition(
                name="insert_widgets_from_pack",
                description=(
                    "Insert a widget pack's widgets (all, or widget_ids) into a dashboard the user can edit. Each live "
                    "platform widget reads through the integration named in bindings for its platform, else the only "
                    "one the user has; when there are several, the error lists them to choose."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "pack_id": {"type": "string"},
                        "dashboard_id": {"type": "string"},
                        "widget_ids": {"type": "array", "items": {"type": "string"}},
                        "bindings": {
                            "type": "object",
                            "description": "One entry per platform the pack reads: the platform's source -> an integration_id.",
                        },
                        "after": {"type": "string", "description": "Insert after this widget id ('start': at the top)."},
                        "revision": {"type": "integer", "description": "The dashboard revision you read."},
                    },
                    "required": ["pack_id", "dashboard_id"],
                },
                source=_SOURCE,
            ),
            ToolDefinition(
                name="save_dashboard_as_pack",
                description="Save a dashboard's widgets (all, or widget_ids) as a new private widget pack.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "dashboard_id": {"type": "string"},
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "widget_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["dashboard_id", "name"],
                },
                source=_SOURCE,
            ),
            ToolDefinition(
                name="import_widget_pack",
                description=(
                    f"Import a widget pack from a file of the conversation: {IMPORT_FORMS}. Returns the pack id and what the "
                    "conversion changed."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"file_id": {"type": "string"}, "name": {"type": "string"}},
                    "required": ["file_id"],
                },
                source=_SOURCE,
            ),
            ToolDefinition(
                name="publish_dashboard",
                description=(
                    f"Publish a dashboard to {platform_list(PUBLISH_TARGETS)} as one of their own dashboards, through "
                    "one of the user's integrations (list_platform_integrations), with the user's own rights there. "
                    f"{PUBLISH_NOTE} The answer gives the new dashboard's address. Ask the user before publishing."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "dashboard_id": {"type": "string"},
                        "target": {"type": "string", "enum": list(PUBLISH_TARGETS)},
                        "integration_id": {"type": "string"},
                        "name": {"type": "string", "description": "The name there (default: the dashboard's title)."},
                    },
                    "required": ["dashboard_id", "target", "integration_id"],
                },
                source=_SOURCE,
            ),
            ToolDefinition(
                name="export_dashboard",
                description=(
                    "Render a dashboard as a PDF (a page per tab) or a PNG, with the user's own access, and keep it as "
                    "a file: the answer's [[FILE:...]] can be attached to a message or posted to a channel."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "dashboard_id": {"type": "string"},
                        "format": {"type": "string", "enum": ["pdf", "png"]},
                        "tabs": {"type": "array", "items": {"type": "string"}, "description": "Layout ids of the tabs (default: all)."},
                    },
                    "required": ["dashboard_id"],
                },
                source=_SOURCE,
            ),
            ToolDefinition(
                name="send_dashboard_export",
                description=(
                    "Email a dashboard as a PDF or a PNG through the platform's email. Recipients are people with an "
                    "account who can open the dashboard; each receives it rendered with their own access. Any other "
                    "address is not sent to and is reported."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "dashboard_id": {"type": "string"},
                        "recipients": {"type": "array", "items": {"type": "string"}, "description": "Email addresses."},
                        "format": {"type": "string", "enum": ["pdf", "png"]},
                        "subject": {"type": "string", "description": "Default: the dashboard's title."},
                        "message": {"type": "string", "description": "A short text above the link."},
                        "tabs": {"type": "array", "items": {"type": "string"}, "description": "Layout ids of the tabs (default: all)."},
                    },
                    "required": ["dashboard_id", "recipients"],
                },
                source=_SOURCE,
            ),
        ]
    if studio:
        definitions += studio_defs
    return definitions, executor
