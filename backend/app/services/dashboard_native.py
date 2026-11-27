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

"""The stored form of a custom dashboard (version 3): an OpenCTI manifest.

A dashboard is stored as OpenCTI stores one (``dev-docs/dashboard-studio-next.md``
§4.7, decisions D6-D8)::

    {config: {relativeDate, startDate, endDate},
     variables: [{id, name, type, restriction, defaultValue, entityTypes?}],
     widgets: {id: widget},
     x_xtm_one: {version: 3, ...}}

* An OpenCTI widget is stored as OpenCTI's own (``{id, type, perspective,
  dataSelection, parameters, layout}``), an OpenAEV one as OpenAEV's
  (``{id, widget_type, widget_config, layout}``), placed on the manifest's
  grid. Their configuration is authoritative here: the extension never
  repeats it. A dashboard input a filter reads is referenced as each platform
  does: ``$var:<variable id>`` in an OpenCTI filter value, the variable id
  itself in an OpenAEV one (as OpenAEV names a parameter).
* A text widget is OpenCTI's ``text`` widget (``parameters.content``).
* Everything else - XTM One's own widgets (``x_xtm_one.<type>``), data sources
  other than a platform widget, inputs, tabs and canvases, interactions,
  visibility, defaults, expressions, theme - lives under ``x_xtm_one``, the one
  extension key (as STIX custom properties ``x_opencti_*``). An export to
  OpenCTI or OpenAEV drops it.
* ``variables``, each widget's ``layout`` and an XTM One widget's
  ``parameters.title`` are projections of the extension, written so the
  manifest reads as OpenCTI's; the extension stays authoritative for them.
  The grid is OpenCTI's (D8): a ``layout`` is the place the extension's layout
  gives, in the same rows. ``x_xtm_one.grid`` marks a manifest whose rows are
  these; a dashboard stored before (rows of 36 px) is read in them by
  :func:`current_form` when it is loaded (``models/custom_dashboard.py``).

The version 2 definition (``dashboard_definition``) remains the working model
every reader, renderer and edit operation uses: :func:`from_native` turns a
manifest back into it, :func:`to_native` writes one from it, and a round trip
gives back what it was given. Validation stays ``validate_definition``'s:
:func:`validate_native` converts, validates, and converts back.

Pure: no I/O, no database.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

from app.services.custom_dashboard_spec import DashboardSpecError
from app.services.dashboard_grid import GRID_UNIT

NATIVE_VERSION = 3
EXTENSION = "x_xtm_one"
#: The type prefix of XTM One's own widgets in the manifest.
TYPE_PREFIX = "x_xtm_one."
#: A variable of a type OpenCTI does not have: ``x_xtm_one.dropdown``...
VARIABLE_PREFIX = "x_xtm_one."
#: Seeds the id of the variable an input becomes, when it has none yet.
_VARIABLE_NAMESPACE = uuid.UUID("6f1c3c5e-7a2b-4f7e-9a1d-2b8f5d0c9e41")
#: A widget placed only on a canvas still has a place on the manifest's grid.
_UNPLACED = {"x": 0, "y": 0, "w": 4, "h": 2}

#: OpenCTI's variable types an input becomes, by input type.
_VARIABLE_TYPES = {"input.text": "text", "input.number": "numeric"}


class NativeFormatError(DashboardSpecError):
    """A manifest that cannot be read as a dashboard, with the reason."""


def is_native(spec: object) -> bool:
    """A stored dashboard in the native form (its ``widgets`` a map, not version 1's list)."""
    return isinstance(spec, dict) and isinstance(spec.get("widgets"), dict) and isinstance(spec.get(EXTENSION), dict)


def variable_id(input_id: str) -> str:
    """The id of the variable input *input_id* becomes, the same on every write."""
    return str(uuid.uuid5(_VARIABLE_NAMESPACE, input_id))


# ── Filter values that name an input ─────────────────────────────────────────


def rewrite_filter_values(node: Any, mapping: dict[str, str]) -> Any:
    """*node* with every string of a filter's ``values`` found in *mapping* replaced (a deep copy)."""
    if isinstance(node, list):
        return [rewrite_filter_values(item, mapping) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict = {}
    for key, value in node.items():
        if key == "values" and isinstance(value, list):
            out[key] = [mapping.get(item, item) if isinstance(item, str) else rewrite_filter_values(item, mapping) for item in value]
        else:
            out[key] = rewrite_filter_values(value, mapping)
    return out


def _token_references(inputs: dict, platform: str, *, inbound: bool) -> dict[str, str]:
    """How an input is named in *platform*'s filter values: ``$token$`` ↔ ``$var:<id>`` (OpenCTI) or ``<id>`` (OpenAEV).

    *inbound*: the manifest's spelling to the working model's.
    """
    pairs: dict[str, str] = {}
    for entry in inputs.values():
        token = entry.get("input", {}).get("token") if isinstance(entry, dict) else None
        vid = entry.get("variable") if isinstance(entry, dict) else None
        if not isinstance(token, str) or not isinstance(vid, str):
            continue
        native = f"$var:{vid}" if platform == "opencti" else vid
        working = f"${token}$"
        if inbound:
            pairs[native] = working
        else:
            pairs[working] = native
    return pairs


# ── Version 2 → manifest ─────────────────────────────────────────────────────


#: The variable types OpenCTI itself knows, among those an input becomes.
OPENCTI_VARIABLE_TYPES = frozenset({"entity", "text", "numeric"})


def opencti_variable(input_id: str, item: dict) -> dict | None:
    """The OpenCTI dashboard variable input *input_id* is, when OpenCTI has its type; ``None`` otherwise."""
    variable = _variable(item, variable_id(input_id))
    return variable if variable is not None and variable["type"] in OPENCTI_VARIABLE_TYPES else None


def _variable(item: dict, vid: str) -> dict | None:
    """The OpenCTI variable an input is shown as; ``None`` for a button, which sets no token of its own."""
    token = item.get("token")
    if not isinstance(token, str):
        return None
    options = item.get("options") if isinstance(item.get("options"), dict) else {}
    kind = item.get("type", "")
    variable: dict = {
        "id": vid,
        "name": token,
        "type": _VARIABLE_TYPES.get(kind, VARIABLE_PREFIX + kind.removeprefix("input.")),
        "restriction": {"mode": "none"},
        "defaultValue": item.get("defaultValue"),
    }
    if kind == "input.entity":
        variable["entityTypes"] = list(options.get("entityTypes") or [])
        if options.get("source") == "opencti":
            variable["type"] = "entity"
        else:
            variable["type"] = f"{VARIABLE_PREFIX}{options.get('source')}.entity"
    return variable


def _grid_places(layout: dict) -> dict[str, dict]:
    """Each visualization's place on the first grid tab that holds it (in XTM One rows)."""
    places: dict[str, dict] = {}
    definitions = layout.get("layoutDefinitions") or {}
    for tab in (layout.get("tabs") or {}).get("items") or []:
        definition = definitions.get(tab.get("layoutId")) or {}
        if definition.get("type", "grid") != "grid":
            continue
        for entry in definition.get("structure") or []:
            position = entry.get("position") or {}
            if entry.get("type", "block") == "block" and entry.get("item") not in places and "x" in position:
                places[entry["item"]] = position
    return places


def _manifest_layout(vid: str, place: dict | None) -> dict:
    from app.services.dashboard_grid import layout_to_platform

    grid = layout_to_platform(place, "opencti") if place else dict(_UNPLACED)
    return {"i": vid, **grid, "moved": False, "static": False}


def to_native(definition: dict) -> dict:
    """A version 2 definition (validated) in the native form."""
    definition = copy.deepcopy(definition)
    sources: dict = definition.get("dataSources") or {}
    visualizations: dict = definition.get("visualizations") or {}
    inputs: dict = definition.get("inputs") or {}
    layout: dict = definition.get("layout") or {}

    ext_inputs = {iid: {"variable": variable_id(iid), "input": item} for iid, item in inputs.items()}
    variables = [v for entry in ext_inputs.values() if (v := _variable(entry["input"], entry["variable"]))]
    outbound = {p: _token_references(ext_inputs, p, inbound=False) for p in ("opencti", "openaev")}

    places = _grid_places(layout)
    hosted: dict[str, str] = {}
    widgets: dict[str, dict] = {}
    ext_widgets: dict[str, dict] = {}
    for vid, viz in visualizations.items():
        sid = (viz.get("dataSources") or {}).get("primary")
        source = sources.get(sid) if sid else None
        place = _manifest_layout(vid, places.get(vid))
        platform = (source or {}).get("options", {}).get("source") if (source or {}).get("type") == "platform" else None
        if platform in ("opencti", "openaev"):
            native = rewrite_filter_values(source["options"]["native"], outbound[platform])
            widgets[vid] = {"id": vid, **native, "layout": place}
            hosted.setdefault(sid, vid)
            ext_widgets[vid] = {"platform": platform, "visualization": viz}
        elif viz.get("type") == "xtm.text":
            options = dict(viz.get("options") or {})
            body = options.pop("body", "")
            widgets[vid] = {
                "id": vid, "type": "text", "perspective": None, "dataSelection": [],
                "parameters": {"title": viz.get("title") or "", "content": body}, "layout": place,
            }  # fmt: skip
            ext_widgets[vid] = {"platform": "opencti", "visualization": {**viz, "options": options}}
        else:
            widgets[vid] = {
                "id": vid, "type": TYPE_PREFIX + str(viz.get("type", "")).removeprefix("xtm."), "perspective": None,
                "dataSelection": [], "parameters": {"title": viz.get("title") or ""}, "layout": place,
            }  # fmt: skip
            ext_widgets[vid] = {"platform": platform or "xtm_one", "visualization": viz}

    ext_sources: dict[str, dict] = {}
    for sid, source in sources.items():
        if sid in hosted:
            options = {k: v for k, v in source["options"].items() if k != "native"}
            ext_sources[sid] = {**source, "options": options, "hostedBy": hosted[sid]}
        else:
            ext_sources[sid] = source

    extension: dict = {
        "version": NATIVE_VERSION,
        "grid": GRID_UNIT,
        "title": definition.get("title"),
        "widgets": ext_widgets,
        "sources": ext_sources,
        "inputs": ext_inputs,
        "layout": layout,
    }
    for key in ("description", "theme", "defaults", "expressions", "applicationProperties"):
        if key in definition:
            extension[key] = definition[key]
    return {
        "config": {"relativeDate": None, "startDate": None, "endDate": None},
        "variables": variables,
        "widgets": widgets,
        EXTENSION: extension,
    }


# ── Manifest → version 2 ─────────────────────────────────────────────────────


def _body(widget: dict, platform: str) -> dict:
    """A platform widget of the manifest as its configuration (no id, no layout)."""
    keys = ("type", "perspective", "dataSelection", "parameters") if platform == "opencti" else ("widget_type", "widget_config")
    return {key: copy.deepcopy(widget[key]) for key in keys if key in widget}


def from_native(manifest: dict) -> dict:
    """A manifest as the version 2 working model. Never validates (a write does).

    Raises :class:`NativeFormatError` when a widget is not described well
    enough to be read: a platform widget with no extension entry has no
    integration to read through - an import chooses one first.
    """
    extension = manifest.get(EXTENSION)
    if not isinstance(extension, dict):
        raise NativeFormatError(f"The manifest has no '{EXTENSION}' entry: import it to choose the integrations it reads.")
    widgets = manifest.get("widgets") if isinstance(manifest.get("widgets"), dict) else {}
    ext_widgets = extension.get("widgets") if isinstance(extension.get("widgets"), dict) else {}
    ext_sources = extension.get("sources") if isinstance(extension.get("sources"), dict) else {}
    ext_inputs = extension.get("inputs") if isinstance(extension.get("inputs"), dict) else {}
    inbound = {p: _token_references(ext_inputs, p, inbound=True) for p in ("opencti", "openaev")}

    visualizations: dict[str, dict] = {}
    for vid, entry in ext_widgets.items():
        widget = widgets.get(vid)
        if not isinstance(entry, dict) or not isinstance(widget, dict):
            raise NativeFormatError(f"widgets.{vid}: the widget is missing from the manifest.")
        viz = copy.deepcopy(entry.get("visualization") or {})
        if entry.get("platform") == "opencti" and widget.get("type") == "text":
            parameters = widget.get("parameters") if isinstance(widget.get("parameters"), dict) else {}
            viz["options"] = {**(viz.get("options") or {}), "body": parameters.get("content") or ""}
        visualizations[vid] = viz
    for vid in widgets:
        if vid not in ext_widgets:
            raise NativeFormatError(f"widgets.{vid}: no '{EXTENSION}.widgets.{vid}' entry says which integration it reads.")

    sources: dict[str, dict] = {}
    for sid, entry in ext_sources.items():
        source = copy.deepcopy(entry)
        host = source.pop("hostedBy", None)
        if host is not None:
            widget = widgets.get(host)
            platform = (source.get("options") or {}).get("source")
            if not isinstance(widget, dict) or platform not in ("opencti", "openaev"):
                raise NativeFormatError(f"{EXTENSION}.sources.{sid}: its widget '{host}' is missing from the manifest.")
            source["options"] = {
                **source["options"],
                "native": rewrite_filter_values(_body(widget, platform), inbound[platform]),
            }
        sources[sid] = source

    definition: dict = {
        "version": 2,
        "title": extension.get("title") or "Dashboard",
        "dataSources": sources,
        "visualizations": visualizations,
        "inputs": {iid: copy.deepcopy(entry.get("input")) for iid, entry in ext_inputs.items() if isinstance(entry, dict)},
        "layout": copy.deepcopy(extension.get("layout") or {}),
    }
    for key in ("description", "theme", "defaults", "expressions", "applicationProperties"):
        if key in extension:
            definition[key] = copy.deepcopy(extension[key])
    return definition


def current_form(spec: object) -> object:
    """A stored dashboard as today's manifest: one already in it as it is; one stored before (version 1 or 2, or a
    manifest without ``x_xtm_one.grid``), its grid places read from rows of 36 px. Canvas places, in pixels, are
    kept. Never validates."""
    from app.services.dashboard_definition import upgrade
    from app.services.dashboard_grid import legacy_layout

    if not isinstance(spec, dict):
        return spec
    if is_native(spec) and spec[EXTENSION].get("grid") == GRID_UNIT:
        return spec
    if isinstance(spec.get("widgets"), list):
        # Version 1: its places before the widgets are read, so a widget it never placed is placed in today's rows.
        widgets = [{**w, "layout": legacy_layout(w["layout"])} if isinstance(w, dict) and "layout" in w else w for w in spec["widgets"]]
        return to_native(upgrade({**spec, "widgets": widgets}))
    working = upgrade(spec)
    for definition in ((working.get("layout") or {}).get("layoutDefinitions") or {}).values():
        if not isinstance(definition, dict) or definition.get("type", "grid") != "grid":
            continue
        for entry in definition.get("structure") or []:
            if isinstance(entry, dict) and isinstance(entry.get("position"), dict) and "x" in entry["position"]:
                entry["position"] = legacy_layout(entry["position"])
    return to_native(working)


def validate_native(manifest: object) -> dict:
    """Validate a manifest through the working model and return the normalized manifest."""
    from app.services.dashboard_definition import validate_definition

    if not isinstance(manifest, dict):
        raise NativeFormatError("A dashboard manifest must be a JSON object.")
    return to_native(validate_definition(from_native(manifest)))


__all__ = [
    "EXTENSION",
    "NATIVE_VERSION",
    "NativeFormatError",
    "current_form",
    "from_native",
    "is_native",
    "opencti_variable",
    "rewrite_filter_values",
    "to_native",
    "validate_native",
    "variable_id",
]
