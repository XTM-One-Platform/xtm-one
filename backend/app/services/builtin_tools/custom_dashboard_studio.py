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

"""The Dashboard Studio's tools that let an agent find data, write a widget right the first time and check it.

Built with the custom dashboards suite while ``DASHBOARD_STUDIO`` is on
(``dev-docs/dashboard-studio-next.md`` §5.3, phase 2):

* ``dashboard_catalog`` - the visualizations, the data shapes each draws and,
  for one type, its options: read from the options models, never a copy;
* ``describe_platform_widget`` - a platform's widget types, or one type with an
  example configuration that passes the platform's own checks and the rules
  it follows (``dashboard_platform.describe_widget``);
* ``lookup_platform_values`` - entity types, filter keys, attributes, values,
  entities and saved filters, read from the platform with the user's own
  access (``dashboard_platform_schema.read_schema``): what fills a filter;
* ``preview_source`` - any data source read before it is saved, as the user
  sees it: its shape and a sample, or why it reads nothing and what to do;
* ``add_widget`` - a data source and the visualization drawing it, placed, in
  one revision, and what the new widget reads;
* ``check_dashboard`` - every visualization's data (ok, empty, waiting on an
  input, or the error and its remedy), and with ``capture`` a picture of the
  tab as rendered, attached as an image the agent looks at;
* ``create_feed`` - a feed: an assignment on an agent, on a schedule or an
  event, whose JSON answer a dashboard source reads (its last one, or the trend
  of its last runs), run once now; the conversation is told when that run ends
  (``owed_conversation_id``), and open pages show each new answer
  (``dashboard_refresh.hear_platform_event``). It is created through the agent
  builder's own ``create_assignment`` and ``trigger_assignment``, with their
  permission gates - never a second way to write an assignment.

Every call acts as the user (their own tier on the dashboard, their own access
to each platform, no admin reach) on short sessions of its own. Data read from
a platform or a source is untrusted (``content_safety.wrap_untrusted``).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable

from app.core.utils import format_exception_message, safe_uuid
from app.tools.base import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

#: How much of a source's data a preview or a check shows the agent.
_SAMPLE_CHARS = 4_000
#: The id an agent builder answer names an assignment by.
_CREATED_ID = re.compile(r"ID: `([0-9a-fA-F-]{36})`")
#: What a feed's answer ends with, by shape: the run's JSON the source reads.
_FEED_SHAPES = {
    "number": '{"value": <number>}',
    "series": '{"series": [{"name": "...", "points": [{"x": "<ISO date or label>", "y": <number>}]}]}',
    "distribution": '{"items": [{"label": "...", "value": <number>}]}',
    "rows": '[{"<column>": <value>, ...}, ...]',
}
#: The id a previewed source is read under.
_PREVIEW_ID = "preview_source"
_ID_CHARS = re.compile(r"[^a-z0-9_]+")

#: What to do about each code a source answers (``dashboard_live.ERROR_CODES``, ``dashboard_sources.ERROR_CODES``).
REMEDIES: dict[str, str] = {
    "integration_unavailable": "the integration is not one the user can read through: list_platform_integrations",
    "access_denied": "the platform refused the user's account: another integration, or ask the user",
    "timeout": "the platform answered too slowly: narrow the filters or the period, then check again",
    "upstream_error": "the platform could not answer this configuration: describe_platform_widget for a valid one",
    "unsupported": "XTM One draws this type as a link only: pick another type",
    "rate_limited": "too many reads at once: check again in a minute",
    "tenant_required": "the integration reaches several tenants: the user picks one in the integration",
    "no_result": "the assignment has no completed run the user may see: run it, or wait for its next run",
    "not_run": "a question to an agent is asked only when the viewer presses Ask the agent",
    "running": "the question is being answered",
    "invalid_result": "the answer does not fit the shape, or a step does not apply: fix the path, the shape or the steps",
    "waiting": "an input the source reads has no value yet: give the input a default value",
}


def _error(message: str) -> ToolResult:
    return ToolResult(tool_call_id="", content=message, is_error=True)


def _parse(name: str, value: object) -> tuple[object, str | None]:
    if not isinstance(value, str):
        return value, None
    try:
        return json.loads(value), None
    except ValueError:
        return value, f"{name} must be a JSON object, not a plain string."


def _is_empty(data: object) -> bool:
    """Data that draws nothing: no value, or only empty lists."""
    if not isinstance(data, dict) or not data:
        return True
    if any(isinstance(v, int | float) and not isinstance(v, bool) for v in data.values()):
        return False
    if isinstance(data.get("text"), str) and data["text"].strip():
        return False
    lists = [v for v in data.values() if isinstance(v, list)]
    return not lists or all(not v for v in lists)


def describe_read(result: dict) -> str:
    """One line on what a source read: its shape and how much, empty, or the error with its remedy."""
    if result.get("status") != "ok":
        code = str(result.get("code") or "upstream_error")
        return f"error {code}: {REMEDIES.get(code, 'check the configuration')}"
    data = result.get("data")
    if _is_empty(data):
        return f"empty ({result.get('shape')}): it reads nothing over this period or with these filters"
    sizes = [f"{len(v)} {k}" for k, v in (data or {}).items() if isinstance(v, list)]
    return f"ok ({result.get('shape')}" + (f", {', '.join(sizes)}" if sizes else "") + ")"


def _new_id(base: str, taken: set[str], prefix: str = "") -> str:
    slug = _ID_CHARS.sub("_", base.lower()).strip("_")[:40] or "widget"
    candidate = f"{prefix}{slug}" if slug[0].isalnum() else f"{prefix}w_{slug}"
    found, n = candidate, 2
    while found in taken:
        found, n = f"{candidate}_{n}", n + 1
    return found


def _place(definition: dict, layout_id: str, viz_type: str) -> dict:
    """Where a new visualization goes in *layout_id*: below the others, half the width, as tall as its view."""
    from app.services.dashboard_definition import TYPE_TO_VIEW
    from app.services.dashboard_grid import default_rows

    layout = ((definition.get("layout") or {}).get("layoutDefinitions") or {}).get(layout_id) or {}
    boxes = [e.get("position") or {} for e in layout.get("structure") or [] if "h" in (e.get("position") or {})]
    bottom = max((p.get("y", 0) + p.get("h", 0) for p in boxes), default=0)
    if layout.get("type") == "absolute":
        return {"x": 40, "y": bottom + 40, "w": 480, "h": 320}
    return {"x": 0, "y": bottom, "w": 6, "h": default_rows({"kind": "platform", "view": TYPE_TO_VIEW.get(viz_type, "number")})}


def build_dashboard_studio_tools(
    *,
    user_id: str,
    conversation_id: str | None,
    assignment_run_id: str | None,
    edit: Callable[..., Awaitable[ToolResult]],
    source: str,
) -> tuple[list[ToolDefinition], dict[str, Callable[..., Awaitable[ToolResult]]]]:
    """The studio tools, acting for *user_id*; *edit* is the suite's ``edit_dashboard`` (one save path)."""
    from app.services.dashboard_platform import PLATFORM_SOURCES, platform_list

    async def _working(dashboard_id: object, *, need: str = "read") -> tuple[object, dict] | ToolResult:
        from app.core.database import async_session
        from app.services.custom_dashboards import load_dashboard_for
        from app.services.dashboard_definition import upgrade

        user_uuid, target = safe_uuid(user_id), safe_uuid(str(dashboard_id or "").strip())
        if user_uuid is None or target is None:
            return _error("dashboard_id must be the id of a dashboard (list_dashboards lists them).")
        async with async_session() as db:
            found = await load_dashboard_for(db, target, user_id=user_uuid, need=need)
            if found is None:
                return _error(f"No dashboard `{dashboard_id}` you can {'edit' if need == 'edit' else 'open'} was found.")
            return found[0], upgrade(found[0].spec)

    async def _read(definition: dict, source_id: str, range_key: str | None = None) -> dict:
        from app.core.database import async_session
        from app.services.dashboard_sources import read_source

        user_uuid = safe_uuid(user_id)
        async with async_session() as db:
            return await read_source(db, user_uuid, definition, source_id, range_key)  # type: ignore[arg-type]

    async def _catalog(shape: str | None = None, type: str | None = None, **_kw: object) -> ToolResult:
        from app.services.dashboard_definition import DATA_TYPES, visualization_catalog
        from app.services.dashboard_visualization_options import option_names

        if type:
            if type not in DATA_TYPES:
                return _error(f"type must be a visualization drawing data: {', '.join(DATA_TYPES)}.")
            shapes = next((v["shapes"] for v in visualization_catalog() if v["type"] == type), [])
            return ToolResult(
                tool_call_id="",
                content=f"{type} draws {', '.join(shapes) or 'text'}. Its options (camelCase, in visualization.options):\n"
                + json.dumps(option_names(type), ensure_ascii=False),
            )
        catalog = visualization_catalog(shape)
        if not catalog:
            return _error("No visualization draws that shape: number, series, distribution, rows, geo, graph, events, tree, text, outcomes.")
        lines = [f"- {v['type']}: {', '.join(v['shapes'])}" for v in catalog]
        return ToolResult(
            tool_call_id="",
            content="Visualizations and the data shapes they draw (dashboard_catalog type=... gives one's options):\n"
            + "\n".join(lines),
        )

    async def _describe(source: str, widget_type: str | None = None, **_kw: object) -> ToolResult:
        from app.services.dashboard_platform import NativeError, describe_widget

        try:
            described = describe_widget(source, widget_type or None)
        except NativeError as exc:
            return _error(str(exc))
        hint = (
            "Pass widget_type for one type's example configuration and rules."
            if widget_type is None
            else "Start from the example, fill it with values looked up on the platform (lookup_platform_values), "
            "check it with preview_source, then add_widget."
        )
        return ToolResult(tool_call_id="", content=json.dumps(described, ensure_ascii=False) + "\n" + hint)

    async def _lookup(
        integration_id: str,
        kind: str,
        perspective: str | None = None,
        types: object = None,
        key: str | None = None,
        search: str | None = None,
        ids: object = None,
        **_kw: object,
    ) -> ToolResult:
        from app.core.database import async_session
        from app.services.content_safety import wrap_untrusted
        from app.services.dashboard_platform_schema import KINDS, read_schema
        from app.services.platform_access import visible_platform_integrations

        if kind not in KINDS:
            return _error(f"kind must be one of: {', '.join(KINDS)}.")
        user_uuid, integration = safe_uuid(user_id), safe_uuid(str(integration_id or "").strip())
        if user_uuid is None or integration is None:
            return _error("integration_id must be an integration list_platform_integrations gave.")
        params = {
            "perspective": perspective,
            "types": [str(t) for t in types][:20] if isinstance(types, list) else None,
            "key": key,
            "search": search,
            "ids": [str(i) for i in ids][:50] if isinstance(ids, list) else None,
        }
        try:
            async with async_session() as db:
                row = next((r for r in await visible_platform_integrations(db, user_uuid) if r.id == integration), None)
                if row is None:
                    return _error("No integration you can read a platform widget through has that id: list_platform_integrations.")
                result = await read_schema(db, user_uuid, integration, row.provider, kind, {k: v for k, v in params.items() if v})
        except Exception as exc:
            logger.error("lookup_platform_values failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the platform could not be read. Please try again.")
        if result.get("status") != "ok":
            code = str(result.get("code"))
            return _error(f"The platform answered {code}: {REMEDIES.get(code, 'try again later')}.")
        items = result.get("items") or []
        listing = json.dumps(items[:200], ensure_ascii=False)[:_SAMPLE_CHARS]
        return ToolResult(
            tool_call_id="",
            content=f"{len(items)} {kind} (value, label):\n" + wrap_untrusted(listing, f"{kind} of the platform"),
        )

    async def _preview(source: object, dashboard_id: str | None = None, range: str | None = None, **_kw: object) -> ToolResult:
        from pydantic import TypeAdapter, ValidationError

        from app.services.content_safety import wrap_untrusted
        from app.services.dashboard_definition import DataSource

        source, problem = _parse("source", source)
        if problem:
            return _error(problem)
        try:
            checked = TypeAdapter(DataSource).validate_python(source).model_dump(mode="json", by_alias=True, exclude_none=True)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"] if not str(p).startswith(("function-", "tagged-union")))
            return _error(f"The source is invalid at {where or 'its root'}: {str(first.get('msg', '')).removeprefix('Value error, ')}")
        definition: dict = {"dataSources": {}, "inputs": {}}
        if dashboard_id:
            working = await _working(dashboard_id)
            if isinstance(working, ToolResult):
                return working
            definition = working[1]
        definition = {**definition, "dataSources": {**(definition.get("dataSources") or {}), _PREVIEW_ID: checked}}
        try:
            result = await _read(definition, _PREVIEW_ID, range)
        except Exception as exc:
            logger.error("preview_source failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the source could not be read. Please try again.")
        line = describe_read(result)
        if result.get("status") != "ok":
            return ToolResult(tool_call_id="", content=f"The source reads nothing: {line}.")
        sample = json.dumps(result.get("data"), ensure_ascii=False)[:_SAMPLE_CHARS]
        return ToolResult(
            tool_call_id="",
            content=f"The source reads {line}, as the user sees it:\n" + wrap_untrusted(sample, "data source data"),
        )

    async def _add(
        dashboard_id: str,
        visualization: object,
        source: object = None,
        source_id: str | None = None,
        place: object = None,
        tab: str | None = None,
        revision: object = None,
        **_kw: object,
    ) -> ToolResult:
        from app.services.dashboard_definition import CONTENT_TYPES, DATA_TYPES

        visualization, problem = _parse("visualization", visualization)
        if problem:
            return _error(problem)
        source, problem = _parse("source", source)
        if problem:
            return _error(problem)
        place, problem = _parse("place", place)
        if problem:
            return _error(problem)
        if not isinstance(visualization, dict) or not isinstance(visualization.get("type"), str):
            return _error("visualization is {type, title?, description?, options?}: dashboard_catalog lists the types.")
        viz_type = visualization["type"]
        draws_data = viz_type in DATA_TYPES
        if not draws_data and viz_type not in CONTENT_TYPES:
            return _error("visualization.type must be a type of dashboard_catalog, or an xtm.<widget kind> holding its own content.")
        if draws_data and (source is None) == (source_id is None):
            return _error("A visualization drawing data takes either 'source' (a new data source) or 'source_id' (one of the dashboard's).")
        working = await _working(dashboard_id, need="edit")
        if isinstance(working, ToolResult):
            return working
        _row, definition = working
        sources = definition.get("dataSources") or {}
        if source_id is not None and source_id not in sources:
            return _error(f"The dashboard has no data source '{source_id}': {', '.join(sources) or 'none yet'}.")
        layouts = definition["layout"]["layoutDefinitions"]
        layout_id = tab or definition["layout"]["tabs"]["items"][0]["layoutId"]
        if layout_id not in layouts:
            return _error(f"tab must be a layout id of the dashboard: {', '.join(layouts)}.")
        taken = set(sources) | set(definition.get("visualizations") or {}) | set(definition.get("inputs") or {})
        title = str(visualization.get("title") or "")
        vid = _new_id(title or viz_type.removeprefix("xtm."), taken)
        operations: list[dict] = []
        viz = {k: v for k, v in visualization.items() if k in ("type", "title", "description", "options", "context", "eventHandlers", "containerOptions")}
        if draws_data:
            sid = source_id or _new_id(vid, taken | {vid}, prefix="ds_")
            if source is not None:
                operations.append({"op": "set", "path": f"dataSources.{sid}", "value": source})
            viz["dataSources"] = {"primary": sid}
        operations.append({"op": "set", "path": f"visualizations.{vid}", "value": viz})
        box = place if isinstance(place, dict) else _place(definition, layout_id, viz_type)
        operations.append({"op": "layout", "items": [{"id": vid, **{k: box.get(k) for k in ("x", "y", "w", "h")}}], "layoutId": layout_id})
        saved = await edit(dashboard_id=dashboard_id, operations=operations, revision=revision)
        if saved.is_error or not draws_data:
            return saved
        after = await _working(dashboard_id)
        if isinstance(after, ToolResult):
            return saved
        try:
            reads = describe_read(await _read(after[1], viz["dataSources"]["primary"]))
        except Exception as exc:
            logger.warning("add_widget could not read the new source: %s", format_exception_message(exc))
            reads = "not read (check_dashboard reads it again)"
        return ToolResult(tool_call_id="", content=f"{saved.content}\nWidget `{vid}` added; its data: {reads}.", files=saved.files)

    async def _feed(
        dashboard_id: str,
        agent_id: str,
        name: str,
        prompt: str,
        trigger: object = None,
        shape: str = "number",
        history: object = None,
        knowledge: object = None,
        visualization: object = None,
        **_kw: object,
    ) -> ToolResult:
        from app.core.database import async_session
        from app.tools.base import ToolCall

        from .agent_builder import build_agent_builder_tool

        trigger, problem = _parse("trigger", trigger)
        if problem:
            return _error(problem)
        knowledge, problem = _parse("knowledge", knowledge)
        if problem:
            return _error(problem)
        if shape not in _FEED_SHAPES:
            return _error(f"shape is one of: {', '.join(_FEED_SHAPES)}.")
        if history is not None and (shape != "number" or not isinstance(history, int) or not 2 <= history <= 60):
            return _error("history (2-60 runs) draws the trend of a number: shape 'number'.")
        trigger = trigger if isinstance(trigger, dict) else {"type": "manual"}
        trigger_type = str(trigger.get("type") or "manual")
        if trigger_type not in ("schedule", "integration_event", "platform_event", "manual"):
            return _error("trigger.type is schedule (config {cron}), integration_event, platform_event or manual.")
        working = await _working(dashboard_id, need="edit")
        if isinstance(working, ToolResult):
            return working
        lines = [prompt.strip(), "", f"End your answer with this JSON alone, in a ```json block: {_FEED_SHAPES[shape]}."]
        if isinstance(knowledge, dict) and knowledge.get("document_id"):
            lines.append(
                f"Before it, keep the consolidated table in the knowledge base document `{knowledge['document_id']}` "
                "(update_knowledge_document), so it can be searched and read later."
            )
        async with async_session() as db:
            _defs, builder = build_agent_builder_tool(db, user_id, conversation_id=conversation_id)
            created = await builder.execute_tool(ToolCall(id="feed", name="create_assignment", arguments={
                "agent_id": agent_id, "name": name, "prompt": "\n".join(lines), "trigger_type": trigger_type,
                "trigger_config": json.dumps(trigger.get("config") or {}),
                "description": f"Feeds a widget of dashboard {dashboard_id}.",
            }))  # fmt: skip
            found = _CREATED_ID.search(created.content or "")
            if created.is_error or found is None:
                return _error(f"The feed's assignment was not created: {created.content}")
            assignment_id = found.group(1)
            started = await builder.execute_tool(ToolCall(id="feed-run", name="trigger_assignment", arguments={"assignment_id": assignment_id}))
        source = {"type": "assignment", "name": name[:80], "options": {
            "assignment_id": assignment_id, "shape": "series" if history else shape, **({"history": history} if history else {}),
        }}  # fmt: skip
        viz = visualization if isinstance(visualization, dict) else {
            "type": "xtm.line" if history else {"number": "xtm.singlevalue", "series": "xtm.line", "distribution": "xtm.bar", "rows": "xtm.table"}[shape],
            "title": name,
        }  # fmt: skip
        added = await _add(dashboard_id=dashboard_id, visualization=viz, source=source)
        return ToolResult(
            tool_call_id="",
            content=(
                f"Feed **{name}** created: assignment `{assignment_id}` ({trigger_type}).\n{added.content}\n"
                f"First run: {started.content.splitlines()[0] if started.content else 'started'}. The widget reads "
                "'No completed run' until it ends; the conversation is told when it does, and the page shows its answer."
            ),
            is_error=added.is_error,
        )

    async def _check(dashboard_id: str, capture: object = False, tab: str | None = None, **_kw: object) -> ToolResult:
        from app.services.dashboard_definition import DATA_TYPES

        working = await _working(dashboard_id)
        if isinstance(working, ToolResult):
            return working
        row, definition = working
        visualizations = definition.get("visualizations") or {}
        read: dict[str, str] = {}
        lines = []
        for vid, viz in visualizations.items():
            if viz.get("type") not in DATA_TYPES:
                continue
            sid = (viz.get("dataSources") or {}).get("primary")
            if sid not in read:
                try:
                    read[sid] = describe_read(await _read(definition, sid))
                except Exception as exc:
                    logger.warning("check_dashboard could not read %s: %s", sid, format_exception_message(exc))
                    read[sid] = "error upstream_error: the source could not be read"
            lines.append(f"- {vid} [{viz.get('type')} <- {sid}]: {read[sid]}")
        problems = sum(1 for line in lines if ": error" in line or ": empty" in line)
        summary = (
            f"Dashboard **{definition.get('title')}** (revision {row.revision}): "  # type: ignore[attr-defined]
            f"{len(lines)} visualizations draw data, {problems} need attention."
        )
        content = summary + ("\n" + "\n".join(lines) if lines else "")
        if not capture:
            return ToolResult(tool_call_id="", content=content + "\ncapture=true adds a picture of the tab as rendered.")
        files = []
        try:
            files = [await _capture(row, definition, tab)]
            content += "\nThe attached picture is the tab as the user sees it: look at it before saying the dashboard is done."
        except Exception as exc:
            logger.warning("check_dashboard capture failed: %s", format_exception_message(exc))
            content += "\nThe tab could not be rendered (the platform's headless browser is unavailable): say so, do not describe it."
        return ToolResult(tool_call_id="", content=content, files=files)

    async def _capture(row: object, definition: dict, tab: str | None) -> object:
        from app.llm.base import AGENT_CHECK_FILE_SOURCE
        from app.services.dashboard_export import export_dashboard

        from ._shared import persist_tool_artifact

        layouts = definition["layout"]["layoutDefinitions"]
        layout_id = tab if tab in layouts else definition["layout"]["tabs"]["items"][0]["layoutId"]
        exported = await export_dashboard(
            dashboard_id=row.id,  # type: ignore[attr-defined]
            user_id=safe_uuid(user_id),  # type: ignore[arg-type]
            title=row.title,  # type: ignore[attr-defined]
            export_format="png",
            layout_ids=[layout_id],
        )
        return await persist_tool_artifact(
            user_id=user_id,
            filename=f"check-{str(row.id)[:8]}-{layout_id}.png",  # type: ignore[attr-defined]
            raw_bytes=exported.data,
            content_type="image/png",
            conversation_id=conversation_id,
            assignment_run_id=assignment_run_id,
            source=AGENT_CHECK_FILE_SOURCE,
        )

    sources = "|".join(PLATFORM_SOURCES)
    definitions = [
        ToolDefinition(
            name="dashboard_catalog",
            description=(
                "The visualizations a data source can be drawn with, and the data shapes each draws "
                "(number, series, distribution, rows, geo, graph, events, tree, text, outcomes). With shape: only "
                "those drawing it. With type: that visualization's options."
            ),
            input_schema={"type": "object", "properties": {"shape": {"type": "string"}, "type": {"type": "string"}}},
            source=source,
            read_only=True,
        ),
        ToolDefinition(
            name="describe_platform_widget",
            description=(
                f"What a live widget of {platform_list()} can be: its widget types, or (widget_type) one type's example "
                "configuration that the platform accepts, and the rules its configuration follows. Read it before "
                "writing a platform source's 'native' configuration."
            ),
            input_schema={
                "type": "object",
                "properties": {"source": {"type": "string", "enum": list(PLATFORM_SOURCES)}, "widget_type": {"type": "string"}},
                "required": ["source"],
            },
            source=source,
            read_only=True,
        ),
        ToolDefinition(
            name="lookup_platform_values",
            description=(
                "Read from a platform, with the user's access, what fills a widget's configuration: kind "
                "entity_types, filter_keys (for a perspective / the types), attributes (of the types), values (of a "
                "filter key, matching search), entities (of the types, matching search), labels (of ids), "
                "saved_filters. Never guess a key, a type or a value."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "integration_id": {"type": "string", "description": "From list_platform_integrations."},
                    "kind": {"type": "string"},
                    "perspective": {"type": "string"},
                    "types": {"type": "array", "items": {"type": "string"}},
                    "key": {"type": "string"},
                    "search": {"type": "string"},
                    "ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["integration_id", "kind"],
            },
            source=source,
            read_only=True,
        ),
        ToolDefinition(
            name="preview_source",
            description=(
                f"Read a data source before saving it, as the user sees it: its shape and a sample, empty, or the "
                f"error and what to do. Any type: platform {{source: {sources}, integration_id, native, range?}}, static, "
                "assignment, ask, variable, integration {integration_id, tool (one that only reads), arguments, shape, "
                "path}, knowledge {knowledge_base_id, document_id, section?, format, shape, path}, derived (with "
                "dashboard_id, a derived source may extend the dashboard's). It is read live, as the user."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "object", "description": "A data source: {type, name?, options}."},
                    "dashboard_id": {"type": "string"},
                    "range": {"type": "string"},
                },
                "required": ["source"],
            },
            source=source,
            read_only=True,
        ),
        ToolDefinition(
            name="add_widget",
            description=(
                "Add a widget to a dashboard in one step: a new data source (source) or one of the dashboard's "
                "(source_id), the visualization drawing it, and its place (below the others when omitted). Returns "
                "what the new widget reads. A content visualization (xtm.text...) takes no source."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dashboard_id": {"type": "string"},
                    "visualization": {
                        "type": "object",
                        "description": "{type (dashboard_catalog), title?, description?, options?, eventHandlers?, containerOptions?}",
                    },
                    "source": {"type": "object", "description": "A new data source {type, name?, options} (preview_source's)."},
                    "source_id": {"type": "string"},
                    "place": {"type": "object", "description": "{x, y, w, h}: grid cells (82 px rows), or pixels on a canvas tab."},
                    "tab": {"type": "string", "description": "The layout id of the tab (default: the first)."},
                    "revision": {"type": "integer"},
                },
                "required": ["dashboard_id", "visualization"],
            },
            source=source,
        ),
        ToolDefinition(
            name="check_dashboard",
            description=(
                "Check a dashboard as the user will see it: for each visualization drawing data, ok, empty, waiting "
                "on an input, or the error and its remedy. capture=true also renders the tab (tab: its layout id) "
                "and attaches the picture: look at it before telling the user the dashboard is done."
            ),
            input_schema={
                "type": "object",
                "properties": {"dashboard_id": {"type": "string"}, "capture": {"type": "boolean"}, "tab": {"type": "string"}},
                "required": ["dashboard_id"],
            },
            source=source,
            read_only=True,
        ),
    ]
    definitions.append(
        ToolDefinition(
            name="create_feed",
            description=(
                "Create a feed for a dashboard: an assignment on an agent (agent_id) that gathers and consolidates "
                "data with its own tools - integrations, knowledge bases - on a trigger, and answers JSON a new "
                "widget draws; run once now. trigger: {type: schedule, config: {cron}} | {type: integration_event, "
                "config: {provider, event, integration_id?}} | {type: platform_event, config: {event}} | manual. shape: "
                "number, series, distribution or rows; history (2-60) draws a number's trend over the last runs. "
                "knowledge: {document_id} also keeps the consolidated table in that knowledge base document. For "
                "data one read tool gives, an integration source refreshed on the server is enough: no agent runs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "dashboard_id": {"type": "string"},
                    "agent_id": {"type": "string", "description": "The agent that runs it (it needs the tools the data takes)."},
                    "name": {"type": "string"},
                    "prompt": {"type": "string", "description": "What to gather and how to consolidate it."},
                    "trigger": {"type": "object"},
                    "shape": {"type": "string", "enum": list(_FEED_SHAPES)},
                    "history": {"type": "integer"},
                    "knowledge": {"type": "object", "description": "{document_id}: a knowledge base document the agent may write."},
                    "visualization": {"type": "object", "description": "{type, title?, options?} (default: by shape)."},
                },
                "required": ["dashboard_id", "agent_id", "name", "prompt"],
            },
            source=source,
        )
    )
    handlers = {
        "dashboard_catalog": _catalog,
        "describe_platform_widget": _describe,
        "lookup_platform_values": _lookup,
        "preview_source": _preview,
        "add_widget": _add,
        "check_dashboard": _check,
        "create_feed": _feed,
    }
    return definitions, handlers


__all__ = ["REMEDIES", "build_dashboard_studio_tools", "describe_read"]
