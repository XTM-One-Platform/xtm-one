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

"""OpenCTI dashboard and widget exports.

OpenCTI exports a dashboard as
``{"openCTI_version", "type": "dashboard", "configuration": {"name", "manifest"}}``
where ``manifest`` is base64 (standard or URL-safe) of
``{"config": {...}, "widgets": {"<id>": widget}}``, and a single widget as
``{"openCTI_version", "type": "widget", "configuration": "<base64 of the widget>"}``
(``workspace-domain.ts``: ``generateWorkspaceExportConfiguration``,
``toWidgetExport``). A custom view - a dashboard shown on the page of an
entity, ``"type": "custom-view"`` with ``configuration.target_entity_type`` -
is exported as a dashboard; its filters name the entity it is shown on as
``SELF_ID`` (``dashboardVizUtils.ts``: ``buildFiltersForCustomView``). It imports a dashboard file of version 5.12.16 or later
(``workspaceConfigurationImport``), resolving the STIX ids in its filters.

Reading: text widgets become the dashboard's ``text`` widgets; the others
become ``platform`` widgets (source ``opencti``) with the widget as their
native configuration, in reading order, width from their columns. A custom
view's ``SELF_ID`` becomes the ``$entity$`` token, which an entity input of
its target type sets (:data:`CUSTOM_VIEW_TOKEN`): the viewer picks the entity
the page is about. A dashboard's ``variables`` (OpenCTI's dashboard inputs,
named ``$var:<id>`` in filter values): an entity variable becomes an entity
input whose token the widgets read; another type has no input here, so its
widgets read its default value, or are left out when it has none.
Writing: ``text`` widgets and OpenCTI platform widgets become an OpenCTI
dashboard export, packed on the grid in order; anything else is listed as left
out - an OpenCTI dashboard cannot draw an XTM One score or an OpenAEV widget.
The dashboard inputs its widgets read become its variables when OpenCTI has
their type (an entity, a text, a number: ``dashboard_native.opencti_variable``),
with the ids the stored manifest gives them.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import uuid

from app.services.custom_dashboard_spec import MAX_BODY_CHARS, MAX_TITLE_CHARS, DashboardSpecError

from . import (
    MAX_DOCUMENT_BYTES,
    MAX_PACK_WIDGETS,
    ConversionReport,
    InteropError,
    ParsedWidgets,
    loads_bounded,
)
from .layout import placement, platform_layouts, reading_order
from .pack_format import unique_ids, validate_pack_widget

#: OpenCTI refuses an import older than this (``MINIMAL_COMPATIBLE_VERSION``).
MINIMAL_VERSION = (5, 12, 16)
#: The version written into an export when the target platform's is unknown.
DEFAULT_EXPORT_VERSION = "6.0.0"
#: A manifest holds at most this many widgets.
MAX_MANIFEST_WIDGETS = 200

#: What a custom view's filters name the entity it is shown on (``SELF_ID``), and the token it becomes.
SELF_ID = "SELF_ID"
CUSTOM_VIEW_TOKEN = "entity"  # noqa: S105 - a dashboard token, not a secret
_ENTITY_TYPE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,99}$")

_VERSION_RE = re.compile(r"^(\d{1,4})\.(\d{1,9})\.(\d{1,9})")
_SLUG_RE = re.compile(r"[^a-z0-9]+")



def parse_version(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.match(value.strip())
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def decode_base64_json(value: object, *, what: str) -> object:
    """Decode OpenCTI's base64 JSON (standard or URL-safe, padded or not)."""
    if not isinstance(value, str) or not value:
        raise InteropError(f"The {what} is missing.")
    if len(value) > MAX_DOCUMENT_BYTES * 4 // 3 + 4:
        raise InteropError(f"The {what} is larger than an import accepts.")
    text = value.strip().replace("-", "+").replace("_", "/")
    text += "=" * (-len(text) % 4)
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raise InteropError(f"The {what} is not valid base64.") from None
    return loads_bounded(raw, what=what)


def encode_base64_json(value: object) -> str:
    return base64.b64encode(json.dumps(value, ensure_ascii=False).encode("utf-8")).decode("ascii")


def slug(title: object) -> str | None:
    if not isinstance(title, str):
        return None
    text = _SLUG_RE.sub("-", title.lower()).strip("-")[:40].strip("-")
    return text or None


def _widget_from_opencti(widget: dict, index: int, report: ConversionReport) -> dict | None:
    """One OpenCTI widget as a pack widget, or ``None`` (reported) when it cannot be kept."""
    params = widget.get("parameters") if isinstance(widget.get("parameters"), dict) else {}
    layout = widget.get("layout") if isinstance(widget.get("layout"), dict) else {}
    title = params.get("title") if isinstance(params.get("title"), str) else None
    label = f"Widget {index + 1}" + (f" ({title[:60]})" if title else "")
    place = placement(layout.get("x"), layout.get("y"), layout.get("w"), layout.get("h"), "opencti")
    if widget.get("type") == "text":
        content = params.get("content")
        if not isinstance(content, str) or not content.strip():
            report.note(f"{label} is an empty text widget and was left out.")
            return None
        body = content.strip()
        if len(body) > MAX_BODY_CHARS:
            report.note(f"{label}: its text was cut to {MAX_BODY_CHARS} characters.")
            body = body[:MAX_BODY_CHARS]
        # OpenCTI writes its text widgets in Markdown (``WidgetText``: GitHub-flavoured).
        candidate: dict = {"kind": "text", "body": body, "format": "markdown", **place}
        if title:
            candidate["title"] = title[:MAX_TITLE_CHARS]
    else:
        candidate = {"kind": "platform", "source": "opencti", **place, "native": widget}
        if title:
            candidate["title"] = title[:MAX_TITLE_CHARS]
    handle = slug(title)
    if handle:
        candidate["id"] = handle
    try:
        return validate_pack_widget(candidate, where=label)
    except DashboardSpecError as exc:
        report.note(f"{label} was left out: {str(exc).splitlines()[-1].strip(' -')}")
        return None


def _check_version(document: dict) -> str:
    version = document.get("openCTI_version")
    parsed = parse_version(version)
    if parsed is None:
        raise InteropError("The OpenCTI export does not name a valid openCTI_version.")
    if parsed < MINIMAL_VERSION:
        raise InteropError(
            f"The OpenCTI export is of version {version}; dashboards are read from version "
            f"{'.'.join(map(str, MINIMAL_VERSION))} on."
        )
    return str(version)


#: OpenCTI's relative periods (``DashboardRelativeDateSelect``) as range keys, and back.
RELATIVE_DATES: dict[str, str] = {
    "days-1": "1d", "days-7": "7d", "months-1": "1m", "months-3": "3m", "months-6": "6m", "years-1": "1y",
}  # fmt: skip
_RANGE_TO_RELATIVE: dict[str, str] = {
    **{key: relative for relative, key in RELATIVE_DATES.items()}, "30d": "months-1", "90d": "months-3",
}  # fmt: skip


def period_from_config(config: object) -> str | None:
    """A manifest's period (``config``: ``relativeDate``, else ``startDate`` / ``endDate``) as a range, or ``None``."""
    from app.services.dashboard_live import fixed_period

    if not isinstance(config, dict):
        return None
    relative = config.get("relativeDate")
    if isinstance(relative, str) and relative:
        return RELATIVE_DATES.get(relative)
    ends = [str(config.get(k))[:10] if isinstance(config.get(k), str) and config.get(k) else "" for k in ("startDate", "endDate")]
    value = "/".join(ends)
    return value if fixed_period(value) else None


def config_from_period(value: str | None) -> dict:
    """A range as a manifest's ``config``: an OpenCTI relative period, or start and end dates."""
    from app.services.dashboard_live import fixed_period

    if value in _RANGE_TO_RELATIVE:
        return {"relativeDate": _RANGE_TO_RELATIVE[value], "startDate": None, "endDate": None}
    fixed = fixed_period(value)
    if fixed is None:
        return {}
    start, end = (moment.strftime("%Y-%m-%dT00:00:00.000Z") if moment else None for moment in fixed)
    return {"relativeDate": None, "startDate": start, "endDate": end}


def widgets_from_manifest(manifest: object, report: ConversionReport) -> list[dict]:
    """The widgets of a decoded manifest, in reading order; its period on each platform widget."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("widgets"), dict):
        raise InteropError("The OpenCTI manifest has no widgets.")
    items = [w for w in manifest["widgets"].values() if isinstance(w, dict)]
    if len(items) > MAX_MANIFEST_WIDGETS:
        raise InteropError(f"The OpenCTI dashboard holds more than {MAX_MANIFEST_WIDGETS} widgets.")
    positions = []
    for item in items:
        layout = item.get("layout") if isinstance(item.get("layout"), dict) else {}
        positions.append((layout.get("x"), layout.get("y")))
    widgets: list[dict] = []
    for index in reading_order(positions):
        converted = _widget_from_opencti(items[index], len(widgets), report)
        if converted is not None:
            widgets.append(converted)
    if len(widgets) > MAX_PACK_WIDGETS:
        report.note(f"The dashboard holds {len(widgets)} widgets; the first {MAX_PACK_WIDGETS} were kept.")
        widgets = widgets[:MAX_PACK_WIDGETS]
    config = manifest.get("config")
    period = period_from_config(config)
    if period:
        # OpenCTI reads every widget over the dashboard's period; here each keeps it, as whoever edits it set it.
        for widget in widgets:
            if widget.get("kind") == "platform":
                widget["range"] = period
        report.note(f"The dashboard reads the period {period}: its widgets keep it.")
    elif isinstance(config, dict) and (config.get("relativeDate") or config.get("startDate") or config.get("endDate")):
        report.note("The dashboard's period could not be read: its widgets read the page's period.")
    return unique_ids(widgets)


def _with_token(value: object) -> object:
    """*value* with ``SELF_ID`` written as the custom view's token, wherever OpenCTI would put the entity's id."""
    if isinstance(value, str):
        return value.replace(SELF_ID, f"${CUSTOM_VIEW_TOKEN}$")
    if isinstance(value, list):
        return [_with_token(v) for v in value]
    if isinstance(value, dict):
        return {k: _with_token(v) for k, v in value.items()}
    return value


_TOKEN_CHARS = re.compile(r"[^A-Za-z0-9_]+")


def _variables_as_inputs(manifest: object, report: ConversionReport) -> tuple[object, list[dict]]:
    """*manifest* with its variables read: an entity variable as an entity input (its ``$var:<id>`` written as
    the input's ``$token$``), another as its default value; a widget left reading a variable is left out."""
    from app.services.dashboard_definition import Input
    from app.services.dashboard_native import rewrite_filter_values

    if not isinstance(manifest, dict) or not isinstance(manifest.get("variables"), list):
        return manifest, []
    inputs: list[dict] = []
    mapping: dict[str, str] = {}
    tokens: set[str] = set()
    for variable in manifest["variables"][:50]:
        if not isinstance(variable, dict) or not isinstance(variable.get("id"), str):
            continue
        name = variable.get("name") if isinstance(variable.get("name"), str) and variable.get("name") else "variable"
        reference = f"$var:{variable['id']}"
        if variable.get("type") == "entity":
            base = (_TOKEN_CHARS.sub("_", name).strip("_").lower() or "variable")[:50]
            base = base if base[0].isalpha() or base[0] == "_" else f"v_{base}"
            token, n = base, 2
            while token in tokens or token == CUSTOM_VIEW_TOKEN:
                token, n = f"{base}_{n}", n + 1
            types = [t for t in variable.get("entityTypes") or [] if isinstance(t, str) and _ENTITY_TYPE_RE.match(t)][:10]
            entry = {"type": "input.entity", "title": name[:120], "token": token,
                     "options": {"source": "opencti", "entityTypes": types or ["Stix-Core-Object"]}}  # fmt: skip
            checked = Input.model_validate(entry).model_dump(mode="json", by_alias=True, exclude_none=True)
            inputs.append({**checked, "id": f"input_{token}"})
            tokens.add(token)
            mapping[reference] = f"${token}$"
            report.note(f"The dashboard variable '{name}' is the dashboard's '{name}' input.")
            continue
        default = variable.get("defaultValue")
        if isinstance(default, str | int | float) and not isinstance(default, bool) and str(default).strip():
            mapping[reference] = str(default)
            report.note(
                f"The dashboard variable '{name}' ({variable.get('type')}) has no input here: "
                f"its widgets read its default value, {default}."
            )
        else:
            report.note(f"The dashboard variable '{name}' ({variable.get('type')}) has no input here and no default value.")
    read = rewrite_filter_values(manifest, mapping)
    widgets = read.get("widgets") if isinstance(read.get("widgets"), dict) else {}
    for wid, widget in list(widgets.items()):
        if "$var:" in json.dumps(widget):
            params = widget.get("parameters") if isinstance(widget, dict) and isinstance(widget.get("parameters"), dict) else {}
            report.note(f"Widget '{params.get('title') or wid}' reads a dashboard variable that has no value here and was left out.")
            del widgets[wid]
    return read, inputs


def custom_view_input(target_type: object) -> dict:
    """The input picking a custom view's entity: an entity of its target type (any entity when it names none)."""
    kind = target_type if isinstance(target_type, str) and _ENTITY_TYPE_RE.match(target_type) else None
    return {
        "type": "input.entity",
        "title": kind.replace("-", " ") if kind else "Entity",
        "token": CUSTOM_VIEW_TOKEN,
        "options": {"source": "opencti", "entityTypes": [kind or "Stix-Core-Object"]},
    }


def parse_export_document(document: object) -> ParsedWidgets:
    """Read an OpenCTI dashboard, custom view or widget export."""
    if not isinstance(document, dict):
        raise InteropError("The OpenCTI export is not a JSON object.")
    version = _check_version(document)
    kind = document.get("type")
    report = ConversionReport()
    configuration = document.get("configuration")
    inputs: list[dict] = []
    if kind in ("dashboard", "custom-view"):
        if not isinstance(configuration, dict):
            raise InteropError(f"The OpenCTI {kind} export has no configuration.")
        manifest = decode_base64_json(configuration.get("manifest"), what=f"OpenCTI {kind} manifest")
        name = configuration.get("name") if isinstance(configuration.get("name"), str) else "OpenCTI dashboard"
        description = configuration.get("description") if isinstance(configuration.get("description"), str) else None
        origin = "opencti-dashboard"
        if kind == "dashboard":
            manifest, variable_inputs = _variables_as_inputs(manifest, report)
            inputs.extend(variable_inputs)
        if kind == "custom-view":
            from app.services.dashboard_definition import Input

            manifest = _with_token(manifest)
            entity = custom_view_input(configuration.get("target_entity_type"))
            inputs.append(Input.model_validate(entity).model_dump(mode="json", by_alias=True, exclude_none=True))
            origin = "opencti-custom-view"
            report.note(
                f"A custom view of {entity['title']}: the entity it is about is picked with the dashboard's "
                f"'{entity['title']}' input (its widgets read ${CUSTOM_VIEW_TOKEN}$ where OpenCTI reads the page's entity)."
            )
        widgets = widgets_from_manifest(manifest, report)
    elif kind == "widget":
        widget = decode_base64_json(configuration, what="OpenCTI widget configuration")
        if not isinstance(widget, dict):
            raise InteropError("The OpenCTI widget export holds no widget.")
        converted = _widget_from_opencti(widget, 0, report)
        widgets = unique_ids([converted]) if converted else []
        title = (widget.get("parameters") or {}).get("title") if isinstance(widget.get("parameters"), dict) else None
        name = title if isinstance(title, str) and title.strip() else "OpenCTI widget"
        description = None
        origin = "opencti-widget"
    else:
        raise InteropError(f"OpenCTI exports of type {kind!r:.30} are not dashboards, custom views or widgets.")
    if not widgets:
        raise InteropError("None of the OpenCTI widgets could be read: " + "; ".join(report.notes[:3]))
    return ParsedWidgets(
        origin=origin,
        name=name.strip()[:MAX_TITLE_CHARS] or "OpenCTI dashboard",
        description=(description or "").strip()[:600] or None,
        widgets=widgets,
        report=report,
        platform_version=version,
        inputs=inputs,
    )


def _to_opencti_widget(widget: dict) -> dict:
    if widget["kind"] == "text":
        parameters = {"content": widget["body"]}
        if widget.get("title"):
            parameters["title"] = widget["title"]
        return {
            "type": "text",
            "perspective": None,
            "dataSelection": [
                {
                    "label": "", "attribute": "entity_type", "date_attribute": "created_at", "perspective": None,
                    "isTo": True, "filters": {"mode": "and", "filters": [], "filterGroups": []},
                    "dynamicFrom": {"mode": "and", "filters": [], "filterGroups": []},
                    "dynamicTo": {"mode": "and", "filters": [], "filterGroups": []},
                }
            ],
            "parameters": parameters,
        }  # fmt: skip
    native = json.loads(json.dumps(widget["native"]))
    parameters = dict(native.get("parameters") or {})
    if widget.get("title"):
        parameters["title"] = widget["title"]
    native["parameters"] = parameters
    return native


def _config_of(widgets: list[dict], report: ConversionReport) -> dict:
    """The period an OpenCTI dashboard of *widgets* reads: theirs, when they read one (OpenCTI has one per dashboard)."""
    periods = [w.get("range") for w in widgets if w.get("kind") == "platform" and w.get("range")]
    fixed = [p for p in periods if "$" not in p]
    if len(fixed) < len(periods):
        report.note("Some widgets read the period of a dashboard input; OpenCTI dashboards have none, so they read its own.")
    if not fixed:
        return {}
    chosen = max(set(fixed), key=fixed.count)
    if len(set(fixed)) > 1:
        report.note(f"The widgets read different periods; the OpenCTI dashboard reads one, {chosen}.")
    if any(w.get("kind") == "platform" and not w.get("range") for w in widgets):
        report.note(f"The widgets that read the page's period read the OpenCTI dashboard's, {chosen}.")
    config = config_from_period(chosen)
    if not config:
        report.note(f"OpenCTI has no period {chosen}; the dashboard is exported without one.")
    return config


def _opencti_widgets(
    widgets: list[dict], report: ConversionReport, *, token: str | None = None, variables: dict[str, dict] | None = None
) -> list[dict]:
    """The widgets an OpenCTI dashboard (or, with *token*, a custom view) can hold; the others reported.

    A widget reading an input it cannot hold is left out: an OpenCTI dashboard holds
    the inputs that are its *variables* (by token), and a custom view has one, its
    entity (*token*, written ``SELF_ID``). Its filters would otherwise keep a
    ``$token$`` OpenCTI matches nothing with.
    """
    from app.services.dashboard_tokens import names_in

    kept: list[dict] = []
    for widget in widgets:
        label = widget.get("title") or widget.get("id")
        if not (widget.get("kind") == "text" or (widget.get("kind") == "platform" and widget.get("source") == "opencti")):
            what = widget.get("source") if widget.get("kind") == "platform" else widget.get("kind")
            report.note(f"Widget '{label}' ({what}) has no OpenCTI equivalent and was left out.")
            continue
        read = names_in(widget.get("native")) - ({token} if token else set()) - set(variables or {})
        if read:
            names = ", ".join(f"${name}$" for name in sorted(read))
            where = "a custom view has only its entity" if token else "it is not a variable of the OpenCTI dashboard"
            report.note(f"Widget '{label}' reads the dashboard's input {names}; {where}, so it was left out.")
            continue
        kept.append(widget)
    return kept


def _manifest(
    kept: list[dict], report: ConversionReport, *, token: str | None = None, variables: dict[str, dict] | None = None
) -> str:
    """The base64 manifest of *kept*, laid out as OpenCTI's grid, with their period; *token* written ``SELF_ID``,
    the inputs that are its *variables* written ``$var:<id>`` and listed."""
    from app.services.dashboard_native import rewrite_filter_values
    from app.services.dashboard_tokens import names_in

    converted = [_to_opencti_widget(w) for w in kept]
    if token:
        converted = json.loads(json.dumps(converted).replace(f"${token}$", SELF_ID))
    used = [variables[name] for name in sorted(set().union(*(names_in(w) for w in converted)) & set(variables or {}))]
    if used:
        converted = rewrite_filter_values(converted, {f"${v['name']}$": f"$var:{v['id']}" for v in used})
    placed = platform_layouts(kept, "opencti")
    manifest_widgets: dict[str, dict] = {}
    for item, position in zip(converted, placed, strict=True):
        wid = str(uuid.uuid4())
        manifest_widgets[wid] = {
            "id": wid,
            **item,
            "layout": {"i": wid, **position, "moved": False, "static": False},
        }
    manifest: dict = {"config": _config_of(kept, report), "widgets": manifest_widgets}
    if used:
        manifest["variables"] = used
    return encode_base64_json(manifest)


def _version(version: str | None) -> str:
    return version if parse_version(version) and parse_version(version) >= MINIMAL_VERSION else DEFAULT_EXPORT_VERSION  # type: ignore[return-value,operator]


def build_dashboard_export(
    name: str,
    widgets: list[dict],
    *,
    version: str | None = None,
    description: str | None = None,
    inputs: dict[str, dict] | None = None,
) -> tuple[dict, ConversionReport]:
    """An OpenCTI dashboard export of the widgets OpenCTI can draw; the others reported.

    *inputs*: the dashboard's inputs by id; those OpenCTI has a variable type for become its variables.
    """
    from app.services.dashboard_native import opencti_variable

    variables = {
        v["name"]: v for iid, item in (inputs or {}).items() if isinstance(item, dict) and (v := opencti_variable(iid, item))
    }
    report = ConversionReport()
    kept = _opencti_widgets(widgets, report, variables=variables)
    if not kept:
        raise InteropError("This dashboard has no widget OpenCTI can draw (OpenCTI widgets or text).")
    configuration: dict = {"name": name[:MAX_TITLE_CHARS], "manifest": _manifest(kept, report, variables=variables)}
    if description:
        configuration["description"] = description
    return {"openCTI_version": _version(version), "type": "dashboard", "configuration": configuration}, report


def build_custom_view_export(
    name: str,
    widgets: list[dict],
    *,
    target_type: str,
    token: str,
    version: str | None = None,
    description: str | None = None,
) -> tuple[dict, ConversionReport]:
    """An OpenCTI custom view of *target_type*: the widgets reading the entity input *token* read the page's entity."""
    if not _ENTITY_TYPE_RE.match(target_type) or target_type == "Stix-Core-Object":
        raise InteropError("A custom view is of one entity type: the dashboard's entity input names none.")
    report = ConversionReport()
    kept = _opencti_widgets(widgets, report, token=token)
    if not kept:
        raise InteropError("This dashboard has no widget an OpenCTI custom view can draw (OpenCTI widgets or text).")
    configuration: dict = {"name": name[:MAX_TITLE_CHARS], "manifest": _manifest(kept, report, token=token), "target_entity_type": target_type}
    if description:
        configuration["description"] = description
    return {"openCTI_version": _version(version), "type": "custom-view", "configuration": configuration}, report


def build_widget_export(widget: dict, *, version: str | None = None) -> dict:
    """An OpenCTI widget export of one text or OpenCTI widget."""
    if not (widget.get("kind") == "text" or (widget.get("kind") == "platform" and widget.get("source") == "opencti")):
        raise InteropError("Only text and OpenCTI widgets can be exported as an OpenCTI widget.")
    item = _to_opencti_widget(widget)
    item["layout"] = {**platform_layouts([widget], "opencti")[0], "x": 0, "y": 0}
    return {"openCTI_version": _version(version), "type": "widget", "configuration": encode_base64_json(item)}
