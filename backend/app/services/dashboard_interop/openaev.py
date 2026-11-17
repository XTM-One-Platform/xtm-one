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

"""OpenAEV dashboard exports.

OpenAEV exports a custom dashboard as a ZIP (``GET /api/custom-dashboards/{id}/export``)
holding ``custom_dashboards.json``, a JSON:API document - the dashboard in
``data``, its widgets and parameters in ``included`` - and ``meta.json``
(``{"schema": {"kind": "jsonapi", "version": 1}}``). Its import
(``POST /api/custom-dashboards/import``) gives every resource a new id and
re-points a widget filter at a parameter of the same file.

Reading: every widget becomes a ``platform`` widget (source ``openaev``), in
reading order, width from its columns. A filter on one of the dashboard's own
parameters is read through an input: a simulation or scenario parameter (what
the dashboard is opened on) becomes an entity input of that type, not
required (a parameter nobody picked leaves its filters out, as OpenAEV), and
its filters read the input's ``$token$``; a custom range bound to the start /
end parameters falls back to the widget's time range. Writing: the OpenAEV
platform widgets of a dashboard become an export of the same shape, the
OpenAEV entity inputs they read its parameters again; anything else is listed
as left out.
"""

from __future__ import annotations

import io
import json
import zipfile

from app.services.custom_dashboard_spec import MAX_TITLE_CHARS, DashboardSpecError

from . import MAX_PACK_WIDGETS, ConversionReport, InteropError, ParsedWidgets, loads_bounded
from .layout import placement, platform_layouts, reading_order
from .opencti import slug
from .pack_format import unique_ids, validate_pack_widget

DOCUMENT_ENTRY = "custom_dashboards.json"
META_ENTRY = "meta.json"
META = {"schema": {"kind": "jsonapi", "version": 1}}
MAX_RESOURCES = 300


def _resources(document: dict) -> tuple[dict, dict[tuple[str, str], dict]]:
    data = document.get("data")
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict) and d.get("type") == "custom_dashboards"), None)
    if not isinstance(data, dict) or data.get("type") != "custom_dashboards":
        raise InteropError("The OpenAEV export holds no custom dashboard.")
    included = document.get("included") or []
    if not isinstance(included, list) or len(included) > MAX_RESOURCES:
        raise InteropError(f"The OpenAEV export holds more than {MAX_RESOURCES} resources.")
    index: dict[tuple[str, str], dict] = {}
    for item in included:
        if isinstance(item, dict) and isinstance(item.get("type"), str) and isinstance(item.get("id"), str):
            index[(item["type"], item["id"])] = item
    return data, index


def _related(resource: dict, name: str) -> list[dict]:
    relation = (resource.get("relationships") or {}).get(name)
    data = relation.get("data") if isinstance(relation, dict) else None
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


#: The parameters of an OpenAEV dashboard an entity input stands for: what the dashboard is opened on.
ENTITY_PARAMETERS = ("simulation", "scenario")


def _strip_parameter_refs(
    group: object, params: dict[str, str], tokens: dict[str, str], where: str, report: ConversionReport
) -> object:
    """*group* with a simulation or scenario parameter read as its input's ``$token$``, the filters on another left out."""
    if not isinstance(group, dict) or not isinstance(group.get("filters"), list):
        return group
    kept = []
    for item in group["filters"]:
        values = item.get("values") if isinstance(item, dict) else None
        if isinstance(values, list) and any(isinstance(v, str) and v in tokens for v in values):
            item = {**item, "values": [f"${tokens[v]}$" if isinstance(v, str) and v in tokens else v for v in values]}
            values = item["values"]
        refs = [v for v in values if isinstance(v, str) and v in params] if isinstance(values, list) else []
        if refs:
            kinds = ", ".join(sorted({params[r] for r in refs}))
            report.note(f"{where}: its filter on the dashboard's {kinds} was removed (it only means something in that dashboard).")
            continue
        kept.append(item)
    return {**group, "filters": kept}


def _clean_config(config: object, params: dict[str, str], tokens: dict[str, str], where: str, report: ConversionReport) -> object:
    if not isinstance(config, dict):
        return config
    clean = json.loads(json.dumps(config))
    for series in clean.get("series") or []:
        if isinstance(series, dict):
            series["filter"] = _strip_parameter_refs(series.get("filter"), params, tokens, where, report)
    perspective = clean.get("perspective")
    if isinstance(perspective, dict):
        perspective["filter"] = _strip_parameter_refs(perspective.get("filter"), params, tokens, where, report)
    bound = [key for key in ("start", "end") if clean.get(key) in params]
    if bound:
        for key in ("start", "end"):
            clean.pop(key, None)
        if clean.get("time_range") == "CUSTOM":
            clean["time_range"] = "DEFAULT"
        report.note(f"{where}: its range followed the dashboard's dates; it now uses its own time range.")
    return clean


def _parameter_inputs(
    parameters: list[tuple[str, str, str]], report: ConversionReport
) -> tuple[dict[str, str], list[dict]]:
    """The entity inputs of a dashboard's simulation and scenario parameters, and the token of each parameter.

    Not required: as OpenAEV, a parameter nobody picked leaves its filters out
    (every simulation is read), and one picked narrows them to it.
    """
    from app.services.dashboard_definition import Input

    tokens: dict[str, str] = {}
    inputs: list[dict] = []
    for pid, kind, name in parameters:
        if kind not in ENTITY_PARAMETERS:
            continue
        token, n = kind, 2
        while token in tokens.values():
            token, n = f"{kind}_{n}", n + 1
        tokens[pid] = token
        title = (name or kind).strip()[:120] or kind
        entry = {
            "type": "input.entity",
            "title": title[:1].upper() + title[1:],
            "token": token,
            "options": {"source": "openaev", "entityTypes": [kind], "required": False},
        }
        inputs.append(Input.model_validate(entry).model_dump(mode="json", by_alias=True, exclude_none=True))
        report.note(
            f"The dashboard's {kind} parameter '{title}' is its '{entry['title']}' input: the widgets read ${token}$, "
            f"every {kind} while none is picked."
        )
    return tokens, inputs


def parse_export_document(document: object) -> ParsedWidgets:
    """Read the JSON:API document of an OpenAEV dashboard export."""
    if not isinstance(document, dict):
        raise InteropError("The OpenAEV export is not a JSON:API document.")
    dashboard, index = _resources(document)
    attributes = dashboard.get("attributes") if isinstance(dashboard.get("attributes"), dict) else {}
    params: dict[str, str] = {}
    declared: list[tuple[str, str, str]] = []
    for ref in _related(dashboard, "custom_dashboard_parameters"):
        resource = index.get(("custom_dashboards_parameters", str(ref.get("id"))))
        if resource:
            attrs = resource.get("attributes") or {}
            kind = str(attrs.get("custom_dashboards_parameter_type") or "parameter")
            params[str(resource["id"])] = kind
            name = attrs.get("custom_dashboards_parameter_name")
            declared.append((str(resource["id"]), kind, name if isinstance(name, str) else ""))
    resources = [
        index[("widgets", str(ref.get("id")))]
        for ref in _related(dashboard, "custom_dashboard_widgets")
        if ("widgets", str(ref.get("id"))) in index
    ]
    report = ConversionReport()
    tokens, inputs = _parameter_inputs(declared, report)
    positions = []
    for resource in resources:
        layout = (resource.get("attributes") or {}).get("widget_layout") or {}
        layout = layout if isinstance(layout, dict) else {}
        positions.append((layout.get("widget_layout_x"), layout.get("widget_layout_y")))
    widgets: list[dict] = []
    for position in reading_order(positions):
        attrs = resources[position].get("attributes") or {}
        config = attrs.get("widget_config") if isinstance(attrs.get("widget_config"), dict) else {}
        title = config.get("title") if isinstance(config.get("title"), str) else None
        label = f"Widget {len(widgets) + 1}" + (f" ({title[:60]})" if title else "")
        layout = attrs.get("widget_layout") if isinstance(attrs.get("widget_layout"), dict) else {}
        candidate: dict = {
            "kind": "platform",
            "source": "openaev",
            **placement(
                layout.get("widget_layout_x"),
                layout.get("widget_layout_y"),
                layout.get("widget_layout_w"),
                layout.get("widget_layout_h"),
                "openaev",
            ),
            "native": {
                "widget_type": attrs.get("widget_type"),
                "widget_config": _clean_config(config, params, tokens, label, report),
            },
        }
        if title:
            candidate["title"] = title[:MAX_TITLE_CHARS]
        handle = slug(title)
        if handle:
            candidate["id"] = handle
        try:
            widgets.append(validate_pack_widget(candidate, where=label))
        except DashboardSpecError as exc:
            report.note(f"{label} was left out: {str(exc).splitlines()[-1].strip(' -')}")
    if len(widgets) > MAX_PACK_WIDGETS:
        report.note(f"The dashboard holds {len(widgets)} widgets; the first {MAX_PACK_WIDGETS} were kept.")
        widgets = widgets[:MAX_PACK_WIDGETS]
    if not widgets:
        raise InteropError("None of the OpenAEV widgets could be read: " + "; ".join(report.notes[:3]))
    name = attributes.get("custom_dashboard_name")
    description = attributes.get("custom_dashboard_description")
    return ParsedWidgets(
        origin="openaev-dashboard",
        name=(name.strip()[:MAX_TITLE_CHARS] if isinstance(name, str) and name.strip() else "OpenAEV dashboard"),
        description=(description.strip()[:600] or None) if isinstance(description, str) else None,
        widgets=unique_ids(widgets),
        report=report,
        inputs=[i for i in inputs if f"${i['token']}$" in json.dumps([w["native"] for w in widgets])],
    )


def parse_export_entries(entries: dict[str, bytes]) -> ParsedWidgets:
    """Read the JSON entries of an OpenAEV export ZIP (``read_zip_entries``)."""
    name = DOCUMENT_ENTRY if DOCUMENT_ENTRY in entries else next((n for n in entries if n != META_ENTRY), None)
    if name is None:
        raise InteropError("The archive is neither a widget pack nor an OpenAEV dashboard export.")
    return parse_export_document(loads_bounded(entries[name], what="OpenAEV export"))


def _parameters_of(inputs: list[dict] | None) -> dict[str, tuple[str, str]]:
    """The OpenAEV simulation and scenario inputs an export writes as parameters: ``{token: (type, name)}``."""
    found: dict[str, tuple[str, str]] = {}
    for entry in inputs or []:
        options = entry.get("options") if isinstance(entry, dict) and isinstance(entry.get("options"), dict) else {}
        types = options.get("entityTypes") or []
        if entry.get("type") == "input.entity" and options.get("source") == "openaev" and len(types) == 1 and types[0] in ENTITY_PARAMETERS:
            found[str(entry.get("token"))] = (types[0], str(entry.get("title") or types[0])[:120])
    return found


def build_export(
    name: str, widgets: list[dict], *, description: str | None = None, inputs: list[dict] | None = None
) -> tuple[dict, ConversionReport]:
    """The JSON:API document of an OpenAEV export of the OpenAEV widgets; the others reported.

    A widget reading a simulation or scenario input (*inputs*, the dashboard's)
    reads a parameter of that type of the exported dashboard; one reading any
    other input is left out, as OpenAEV has nothing to set it.
    """
    from app.services.dashboard_tokens import names_in

    report = ConversionReport()
    available = _parameters_of(inputs)
    kept = []
    for widget in widgets:
        label = widget.get("title") or widget.get("id")
        if not (widget.get("kind") == "platform" and widget.get("source") == "openaev"):
            what = widget.get("source") if widget.get("kind") == "platform" else widget.get("kind")
            report.note(f"Widget '{label}' ({what}) has no OpenAEV equivalent and was left out.")
            continue
        unknown = names_in(widget.get("native")) - set(available)
        if unknown:
            names = ", ".join(f"${name}$" for name in sorted(unknown))
            report.note(f"Widget '{label}' reads the dashboard's input {names}, which OpenAEV cannot set, so it was left out.")
            continue
        kept.append(widget)
    if not kept:
        raise InteropError("This dashboard has no OpenAEV widget to export.")
    used = sorted(set().union(*(names_in(w["native"]) for w in kept)))
    parameter_ids = {token: f"p{n + 1}" for n, token in enumerate(used)}
    parameters = [
        {
            "id": pid,
            "type": "custom_dashboards_parameters",
            "attributes": {
                "custom_dashboards_parameter_id": pid,
                "custom_dashboards_parameter_name": available[token][1],
                "custom_dashboards_parameter_type": available[token][0],
            },
        }
        for token, pid in parameter_ids.items()
    ]
    placed = platform_layouts(kept, "openaev")
    included: list[dict] = list(parameters)
    refs: list[dict] = []
    for position, (widget, cell) in enumerate(zip(kept, placed, strict=True)):
        wid = f"w{position + 1}"
        text = json.dumps(widget["native"]["widget_config"])
        for token, pid in parameter_ids.items():
            text = text.replace(f"${token}$", pid)
        config = json.loads(text)
        if widget.get("title"):
            config["title"] = widget["title"]
        refs.append({"id": wid, "type": "widgets"})
        included.append(
            {
                "id": wid,
                "type": "widgets",
                "attributes": {
                    "widget_type": widget["native"]["widget_type"],
                    "widget_config": config,
                    "widget_layout": {
                        "widget_layout_w": cell["w"], "widget_layout_h": cell["h"],
                        "widget_layout_x": cell["x"], "widget_layout_y": cell["y"],
                    },
                },
            }
        )  # fmt: skip
    attributes: dict = {"custom_dashboard_name": name[:MAX_TITLE_CHARS], "custom_dashboard_description": description or ""}
    document = {
        "data": {
            "id": "d1",
            "type": "custom_dashboards",
            "attributes": attributes,
            "relationships": {
                "custom_dashboard_widgets": {"data": refs},
                "custom_dashboard_parameters": {"data": [{"id": p["id"], "type": p["type"]} for p in parameters]},
            },
        },
        "included": included,
    }
    return document, report


def build_export_zip(
    name: str, widgets: list[dict], *, description: str | None = None, inputs: list[dict] | None = None
) -> tuple[bytes, ConversionReport]:
    """The ZIP OpenAEV imports (``custom_dashboards.json`` + ``meta.json``)."""
    document, report = build_export(name, widgets, description=description, inputs=inputs)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(DOCUMENT_ENTRY, json.dumps(document, ensure_ascii=False))
        archive.writestr(META_ENTRY, json.dumps(META))
    return buffer.getvalue(), report
