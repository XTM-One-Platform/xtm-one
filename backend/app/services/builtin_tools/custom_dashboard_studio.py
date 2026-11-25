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
* ``find_dashboard_data`` - the integrations (and one's read tools) and the
  knowledge base documents an ``integration`` or ``knowledge`` source can read,
  with the user's own access and the ids the source takes;
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
#: Sources previewed without a widget added before each preview reminds the agent to build what reads data.
PREVIEWS_BEFORE_A_WIDGET = 8
#: ... and past this many, a preview is refused until a widget is added: the reminder alone was read and ignored
#: (sixty previews, no widget, the person watching an empty page).
PREVIEW_LIMIT = 2 * PREVIEWS_BEFORE_A_WIDGET
#: The id a previewed source is read under.
_PREVIEW_ID = "preview_source"
_ID_CHARS = re.compile(r"[^a-z0-9_]+")

#: What to do about each code a source answers (``dashboard_live.ERROR_CODES``, ``dashboard_sources.ERROR_CODES``).
REMEDIES: dict[str, str] = {
    "integration_unavailable": "the integration is not one the user can read through: list_platform_integrations",
    "access_denied": "the platform refused the user's account: another integration, or ask the user",
    "timeout": "the platform answered too slowly: narrow the filters or the period, then check again",
    "upstream_error": (
        "the system read could not answer this configuration: a platform, describe_platform_widget for a valid one; an "
        "integration tool, what it answered says why (nothing found is no data to draw)"
    ),
    "unsupported": "XTM One draws this type as a link only: pick another type",
    "rate_limited": "too many reads at once: check again in a minute",
    "tenant_required": "the integration reaches several tenants: the user picks one in the integration",
    "no_result": "the assignment has no completed run the user may see: run it, or wait for its next run",
    "not_run": "a question to an agent is asked only when the viewer presses Ask the agent",
    "running": "the question is being answered",
    "invalid_result": (
        "the answer holds no JSON fitting the shape, or a step does not apply: fix the path, the shape or the steps; "
        "a tool that answers in words, not JSON, is drawn as text (shape text), or create_feed lets an agent turn it "
        "into the JSON"
    ),
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
        from app.services.content_safety import wrap_untrusted

        code = str(result.get("code") or "upstream_error")
        # The detail may be what an integration answered: data, never instructions.
        detail = (
            f" ({wrap_untrusted(result['detail'], 'the answer of the system read')})"
            if isinstance(result.get("detail"), str) and result["detail"]
            else ""
        )
        return f"error {code}{detail}: {REMEDIES.get(code, 'check the configuration')}"
    data = result.get("data")
    if _is_empty(data):
        return f"empty ({result.get('shape')}): it reads nothing over this period or with these filters"
    sizes = [f"{len(v)} {k}" for k, v in (data or {}).items() if isinstance(v, list)]
    return f"ok ({result.get('shape')}" + (f", {', '.join(sizes)}" if sizes else "") + ")"


#: The dashboard grid's columns (OpenCTI's), and the views that draw a table's columns side by side.
GRID_COLUMNS = 12
ROW_VIEWS = ("xtm.table", "xtm.list")


def _widths(definition: dict) -> dict[str, int]:
    """Each placed visualization's width, in grid columns (its narrowest placement)."""
    widths: dict[str, int] = {}
    for layout in (definition.get("layout") or {}).get("layoutDefinitions", {}).values():
        for item in layout.get("structure") or []:
            width = (item.get("position") or {}).get("w")
            if isinstance(width, int) and item.get("item"):
                widths[item["item"]] = min(width, widths.get(item["item"], width))
    return widths


def describe_choices(options: dict, data: object) -> str:
    """The choices a dropdown reading *data* offers, as the page derives them (``inputs.ts`` ``choicesOf``): by
    default the first column of a table, which named one type for a list of 189 intrusion sets."""
    if not isinstance(data, dict):
        return "its source reads nothing: no choice offered"
    value_field = options.get("valueField")
    if isinstance(data.get("items"), list):
        entries, value_key = data["items"], value_field or "label"
        keys: list[str] = sorted({k for e in entries[:20] if isinstance(e, dict) for k in e})
    elif isinstance(data.get("rows"), list):
        entries = data["rows"]
        keys = [str(c.get("key")) for c in data.get("columns") or [] if isinstance(c, dict)]
        keys += sorted({k for e in entries[:20] if isinstance(e, dict) for k in e} - set(keys))
        value_key = value_field or (keys[0] if keys else "")
    else:
        return ""
    values = {str(e.get(value_key)) for e in entries[:200] if isinstance(e, dict) and e.get(value_key) not in (None, "")}
    line = f"{len(values)} choices from {len(entries)} rows (value: {value_key or 'none'})"
    if len(entries) >= 3 and len(values) * 2 < min(len(entries), 200):
        line += f"; collapsed: most rows give the same value - set valueField / labelField among {', '.join(keys[:15])}"
        line += ", or an input.entity for the viewer to search the platform's entities"
    return line


def unread_parts(source: object) -> str:
    """What a platform source holds that its platform does not read, told after a preview: the read itself looks
    right (an entities widget whose relationship filter is ignored drew every entity of its types)."""
    from app.services.dashboard_platform import NativeError, check_native

    options = source.get("options") if isinstance(source, dict) and source.get("type") == "platform" else None
    if not isinstance(options, dict):
        return ""
    try:
        ignored = check_native(str(options.get("source")), options.get("native")).ignored
    except NativeError:
        return ""
    return f"\nThe platform does not read: {'; '.join(ignored)}." if ignored else ""


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
    from app.services.dashboard_definition import SourceAlert

    # Sources previewed since the last widget added: past PREVIEWS_BEFORE_A_WIDGET, each preview says to add the
    # widgets whose sources read data first - the person watches an empty dashboard while the agent explores.
    previews = {"since_add": 0}
    from app.services.dashboard_pipeline import form_of
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
        from app.services.dashboard_tokens import starting_tokens

        user_uuid = safe_uuid(user_id)
        async with async_session() as db:
            # As a viewer opening the page reads it: the inputs at their default values (read without them, a
            # source on an intrusion set picker answered "waiting" to every check).
            try:
                tokens = starting_tokens(definition)
            except ValueError:
                tokens = {}
            return await read_source(db, user_uuid, definition, source_id, range_key, tokens=tokens)  # type: ignore[arg-type]

    async def _catalog(shape: str | None = None, type: str | None = None, **_kw: object) -> ToolResult:
        from app.services.dashboard_definition import DATA_TYPES, visualization_catalog
        from app.services.dashboard_visualization_options import option_names

        if type:
            if type not in DATA_TYPES:
                return _error(f"type must be a visualization drawing data: {', '.join(DATA_TYPES)}.")
            from app.services.dashboard_live import SHAPE_FORMS

            shapes = next((v["shapes"] for v in visualization_catalog() if v["type"] == type), [])
            forms = "\n".join(f"- {shape}: {SHAPE_FORMS[shape]}" for shape in shapes if shape in SHAPE_FORMS)
            return ToolResult(
                tool_call_id="",
                content=f"{type} draws {', '.join(shapes) or 'text'}" + (f", data of the form:\n{forms}" if forms else ".")
                + "\nIts options (camelCase, in visualization.options):\n"
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

    async def _find(
        kind: str, search: str | None = None, integration_id: str | None = None, knowledge_base_id: str | None = None, **_kw: object
    ) -> ToolResult:
        from app.core.database import async_session
        from app.services.content_safety import wrap_untrusted
        from app.services.dashboard_sources import LiveError, read_tools, readable_documents, readable_integrations

        user_uuid = safe_uuid(user_id)
        needle = str(search or "").strip().lower()
        if user_uuid is None or kind not in ("integrations", "knowledge"):
            return _error("kind is integrations or knowledge.")
        try:
            async with async_session() as db:
                if kind == "integrations" and integration_id:
                    try:
                        tools = await read_tools(db, user_uuid, safe_uuid(str(integration_id).strip()) or integration_id)
                    except LiveError as exc:
                        return _error(f"That integration cannot be read: {REMEDIES.get(exc.code, exc.code)}.")
                    lines = [
                        f"- {t.name}: {' '.join(str(t.description or '').split())[:240]} Arguments: "
                        + json.dumps((t.input_schema or {}).get("properties") or {}, ensure_ascii=False)[:400]
                        for t in tools
                        if not needle or needle in f"{t.name} {t.description}".lower()
                    ]
                    header = f"{len(lines)} tools of that integration only read (an integration source calls one with fixed arguments):"
                elif kind == "integrations":
                    rows = [r for r in await readable_integrations(db, user_uuid) if not needle or needle in f"{r.name} {r.provider}".lower()]
                    lines = [f"- {r.name} ({r.provider}): integration_id {r.id}" for r in rows]
                    header = f"{len(lines)} integrations a source can read through (find_dashboard_data integration_id=... lists one's read tools):"
                else:
                    pairs = await readable_documents(db, user_uuid, safe_uuid(str(knowledge_base_id or "").strip()))
                    lines = [
                        f"- {doc.filename} ({doc.source_type}, {doc.status}): knowledge_base_id {kb.id} ({kb.name}), document_id {doc.id}"
                        for kb, doc in pairs
                        if not needle or needle in f"{kb.name} {doc.filename}".lower()
                    ][:80]
                    header = f"{len(lines)} knowledge base documents a knowledge source can read (a table in JSON, JSON lines, CSV or Markdown):"
        except Exception as exc:
            logger.error("find_dashboard_data failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the data could not be listed. Please try again.")
        if not lines:
            return ToolResult(tool_call_id="", content="Nothing matches: try without search, or ask the user where the data is.")
        return ToolResult(tool_call_id="", content=header + "\n" + wrap_untrusted("\n".join(lines)[: _SAMPLE_CHARS * 2], f"{kind} the user can read"))

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

    async def _preview(
        source: object, dashboard_id: str | None = None, range: str | None = None, sources: object = None, **_kw: object
    ) -> ToolResult:
        from pydantic import TypeAdapter, ValidationError

        from app.services.content_safety import wrap_untrusted
        from app.services.dashboard_definition import DataSource

        if previews["since_add"] >= PREVIEW_LIMIT:
            return _error(
                f"{previews['since_add']} sources previewed and no widget added: no more previews until one is. Add "
                "the widgets whose source read data (add_widget, sources for the ones not saved yet), or end here and "
                "tell the person which parts read nothing and why."
            )
        source, problem = _parse("source", source)
        if problem:
            return _error(problem)
        try:
            checked = TypeAdapter(DataSource).validate_python(source).model_dump(mode="json", by_alias=True, exclude_none=True)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first["loc"] if not str(p).startswith(("function-", "tagged-union")))
            message = f"The source is invalid at {where or 'its root'}: {str(first.get('msg', '')).removeprefix('Value error, ')}"
            if ".steps" in where:
                from app.services.dashboard_pipeline import step_forms

                message += ". A step is one of: " + "; ".join(step_forms())
            return _error(message)
        definition: dict = {"dataSources": {}, "inputs": {}}
        if dashboard_id:
            working = await _working(dashboard_id)
            if isinstance(working, ToolResult):
                return working
            definition = working[1]
        # The sources a derived one reads that are not saved yet: read with it, never saved.
        unsaved, problem = _parse("sources", sources) if sources is not None else ({}, None)
        if problem or not isinstance(unsaved, dict) or len(unsaved) > 10:
            return _error(problem or "sources is {id: source}, at most 10: the unsaved sources the previewed one extends.")
        extra: dict = {}
        for sid, raw in unsaved.items():
            try:
                extra[str(sid)] = TypeAdapter(DataSource).validate_python(raw).model_dump(mode="json", by_alias=True, exclude_none=True)
            except ValidationError as exc:
                return _error(f"sources.{sid} is invalid: {str(exc.errors()[0].get('msg', ''))[:200]}")
        definition = {**definition, "dataSources": {**(definition.get("dataSources") or {}), **extra, _PREVIEW_ID: checked}}
        try:
            result = await _read(definition, _PREVIEW_ID, range)
        except Exception as exc:
            logger.error("preview_source failed: %s", format_exception_message(exc), exc_info=True)
            return _error("Error: the source could not be read. Please try again.")
        line = describe_read(result)
        previews["since_add"] += 1
        pace = (
            f"\n{previews['since_add']} sources previewed and no widget added since: add now the widgets whose source "
            "reads data (add_widget) - the person is watching an empty dashboard - and leave a source that still "
            "reads nothing for the hand-over, saying why."
            if previews["since_add"] >= PREVIEWS_BEFORE_A_WIDGET
            else ""
        )
        unread = unread_parts(checked)
        if result.get("status") != "ok":
            return ToolResult(tool_call_id="", content=f"The source reads nothing: {line}.{unread}{pace}")
        sample = json.dumps(result.get("data"), ensure_ascii=False)[:_SAMPLE_CHARS]
        return ToolResult(
            tool_call_id="",
            content=f"The source reads {line}, as the user sees it:\n" + wrap_untrusted(sample, "data source data") + unread + pace,
        )

    async def _add(
        dashboard_id: str,
        visualization: object,
        source: object = None,
        source_id: str | None = None,
        place: object = None,
        tab: str | None = None,
        revision: object = None,
        sources: object = None,
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
        # The sources the new one extends, saved with it under the ids given: a base no widget draws itself.
        bases, problem = _parse("sources", sources) if sources is not None else ({}, None)
        if problem or not isinstance(bases, dict) or len(bases) > 10:
            return _error(problem or "sources is {id: source}, at most 10: the sources the new one extends, saved with it.")
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
        clash = sorted(set(bases) & (set(sources) | set(definition.get("visualizations") or {})))
        if clash:
            return _error(f"sources would replace what the dashboard has: {', '.join(clash)}. Use other ids, or source_id.")
        if source_id is not None and source_id not in sources:
            return _error(f"The dashboard has no data source '{source_id}': {', '.join(sources) or 'none yet'}.")
        layouts = definition["layout"]["layoutDefinitions"]
        layout_id = tab or definition["layout"]["tabs"]["items"][0]["layoutId"]
        if layout_id not in layouts:
            return _error(f"tab must be a layout id of the dashboard: {', '.join(layouts)}.")
        taken = set(sources) | set(bases) | set(definition.get("visualizations") or {}) | set(definition.get("inputs") or {})
        title = str(visualization.get("title") or "")
        vid = _new_id(title or viz_type.removeprefix("xtm."), taken)
        operations: list[dict] = [{"op": "set", "path": f"dataSources.{bid}", "value": base} for bid, base in bases.items()]
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
        if not saved.is_error:
            previews["since_add"] = 0
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

        from .agent_builder import _load_writable_agent_for_builder, build_agent_builder_tool

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
        agent_uuid, user_uuid = safe_uuid(str(agent_id or "")), safe_uuid(user_id)
        if agent_uuid is None or user_uuid is None:
            return _error("agent_id must be an agent's id: list_agents.")
        async with async_session() as db:
            # Checked first, with what to do: told only "Cannot add an assignment to built-in agents", the agent
            # retried the same one.
            _agent, refused = await _load_writable_agent_for_builder(db, user_uuid, agent_uuid, action="add an assignment to", assignment_write=True)
            if refused:
                return _error(
                    f"The feed cannot run on this agent: {refused} A feed runs on an agent the user may give an "
                    "assignment: one of theirs that has the tools the data takes (list_agents), or a new one "
                    "(create_agent with those tools); then create_feed with its id."
                )
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
        columns: dict[str, int] = {}
        widths = _widths(definition)
        lines = []
        for vid, viz in visualizations.items():
            if viz.get("type") not in DATA_TYPES:
                continue
            sid = (viz.get("dataSources") or {}).get("primary")
            if sid not in read:
                try:
                    result = await _read(definition, sid)
                    read[sid] = describe_read(result)
                    data = result.get("data") if result.get("status") == "ok" else None
                    columns[sid] = len(data.get("columns") or []) if isinstance(data, dict) and isinstance(data.get("rows"), list) else 0
                except Exception as exc:
                    logger.warning("check_dashboard could not read %s: %s", sid, format_exception_message(exc))
                    read[sid] = "error upstream_error: the source could not be read"
            line = f"- {vid} [{viz.get('type')} <- {sid}]: {read[sid]}"
            unread = unread_parts((definition.get("dataSources") or {}).get(sid)).strip()
            if unread:
                line += f"; misread: {unread} - what it draws is not what it means to"
            # A table read in a tile narrower than its columns: each needs about one of the grid's 12 columns.
            needed, width = min(GRID_COLUMNS, columns.get(sid, 0)), widths.get(vid)
            if viz.get("type") in ROW_VIEWS and width is not None and width < needed:
                line += f"; cramped: {columns[sid]} columns in {width} grid columns, unreadable - widen it to {needed} (edit_dashboard)"
            lines.append(line)
        # A choice input read from a source: the choices the viewer gets, as the page derives them.
        for token, entry in (definition.get("inputs") or {}).items():
            sid = ((entry or {}).get("dataSources") or {}).get("primary")
            if (entry or {}).get("type") not in ("input.dropdown", "input.multiselect") or not sid:
                continue
            try:
                result = await _read(definition, sid)
            except Exception as exc:
                logger.warning("check_dashboard could not read %s: %s", sid, format_exception_message(exc))
                continue
            told = describe_choices(entry.get("options") or {}, result.get("data") if result.get("status") == "ok" else None)
            if told:
                lines.append(f"- input {token} [{entry['type']} <- {sid}]: {told}")
        problems = sum(
            1 for line in lines if ": error" in line or ": empty" in line or "; cramped:" in line or "; misread:" in line or "; collapsed:" in line
        )
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
            # Its own reason: "the headless browser is unavailable" sent the agent waiting for a browser that was up.
            content += f"\nThe tab could not be rendered ({format_exception_message(exc)[:200]}): say so, do not describe it."
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
            name="find_dashboard_data",
            description=(
                "Find the data an integration or knowledge source reads, with the user's access: kind integrations "
                "(the integrations, with their integration_id; with integration_id, its tools that only read and "
                "their arguments) or knowledge (the knowledge base documents, with knowledge_base_id and document_id). "
                "search narrows by name. Platform widgets use list_platform_integrations instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["integrations", "knowledge"]},
                    "search": {"type": "string"},
                    "integration_id": {"type": "string"},
                    "knowledge_base_id": {"type": "string"},
                },
                "required": ["kind"],
            },
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
                "filter key), entities (of the types), labels (of ids), saved_filters; search narrows any kind. Never "
                "guess a key, a type or a value."
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
                f"error and what to do. A source is {{type, name?, options}}; its options by type: platform {{source: "
                f"{sources}, integration_id, native, range?}}, static, assignment, ask, variable, integration "
                "{integration_id, tool (one that only reads), arguments, shape, path?}, knowledge {knowledge_base_id, "
                "document_id, section?, format, shape, path?}, derived {extends (a source id), steps} (with dashboard_id, "
                "a derived source may extend the dashboard's); refreshMode, refresh, cron, on and alerts (each "
                f"{form_of(SourceAlert)}, on the figure of a source read on the server) are options too. A table's rows "
                "drawn as a distribution: its first text column labels, its first number column counts. It is read live, as the user."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "object", "description": "A data source: {type, name?, options}."},
                    "sources": {
                        "type": "object",
                        "description": "{id: source}: sources not saved yet that a derived source extends, read with it.",
                    },
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
                "what the new widget reads; the sources a new derived one extends are saved with it (sources). A "
                "content visualization (xtm.text...) takes no source."
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
                    "sources": {
                        "type": "object",
                        "description": "{id: source}: the sources the new one extends, saved with it - a base no widget draws.",
                    },
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
                    "agent_id": {
                        "type": "string",
                        "description": "The agent that runs it: one the user may give an assignment (not a built-in one), with the tools the data takes.",
                    },
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
        "find_dashboard_data": _find,
        "describe_platform_widget": _describe,
        "lookup_platform_values": _lookup,
        "preview_source": _preview,
        "add_widget": _add,
        "check_dashboard": _check,
        "create_feed": _feed,
    }
    return definitions, handlers


__all__ = ["REMEDIES", "build_dashboard_studio_tools", "describe_read"]
