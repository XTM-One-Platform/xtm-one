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

"""The XTM One widget pack file.

A pack is a named set of ready-made widgets, of every kind a dashboard takes:

    {"format": "xtm-one-widget-pack", "version": 1,
     "name": "...", "description": "...", "widgets": [ ... ]}

saved as ``.json``, or as ``pack.json`` inside a ZIP. Its widgets are checked
exactly as a widget an agent writes, with one difference: a ``platform`` widget
carries no ``integration_id``. A pack moves between installations, and which
OpenCTI, OpenAEV or OpenCRQ a widget reads is chosen when it is inserted into a
dashboard (:func:`bind_widget`), among the integrations the person can use.

A pack may also hold ``"inputs"``: the entity inputs its widgets read
(``input.entity``, an OpenCTI custom view's "current entity"), checked as a
definition's and unbound like its widgets (:func:`validate_pack_input`).
"""

from __future__ import annotations

import copy
import uuid

from app.services.custom_dashboard_spec import (
    MAX_TEXT_CHARS,
    MAX_TITLE_CHARS,
    DashboardSpecError,
    assign_widget_ids,
    validate_widget,
)

from . import MAX_PACK_WIDGETS, ConversionReport, InteropError, ParsedWidgets, loads_bounded

PACK_FORMAT = "xtm-one-widget-pack"
#: Version 2 places its widgets in rows of OpenCTI's grid; version 1 (rows of 36 px) is still read.
PACK_VERSION = 2
_READ_VERSIONS = (1, 2)
PACK_ENTRY = "pack.json"

#: Stands in for the integration while a pack widget is checked: never stored.
_UNBOUND = uuid.UUID(int=0)
#: The inputs a pack carries: entity inputs only (the others read the dashboard's own sources).
PACK_INPUT_TYPES = ("input.entity",)
MAX_PACK_INPUTS = 10


def validate_pack_input(raw: object, *, where: str = "input") -> dict:
    """An input as a pack keeps it: checked as a definition's, its integration left out; else ``DashboardSpecError``."""
    from pydantic import ValidationError

    from app.services.dashboard_definition import Input

    if not isinstance(raw, dict) or raw.get("type") not in PACK_INPUT_TYPES:
        raise DashboardSpecError(f"{where}: a pack holds entity inputs only ({', '.join(PACK_INPUT_TYPES)}).")
    options = {k: v for k, v in (raw.get("options") or {}).items() if k != "integration_id"} if isinstance(raw.get("options"), dict) else {}
    try:
        checked = Input.model_validate({**raw, "options": options}).model_dump(mode="json", by_alias=True, exclude_none=True)
    except ValidationError as exc:
        raise DashboardSpecError(f"{where} is invalid: {exc.errors()[0].get('msg', 'invalid')}") from None
    if raw.get("id") is not None:
        checked["id"] = str(raw["id"])[:60]
    return checked


def validate_pack_widget(widget: object, *, where: str = "widget") -> dict:
    """A widget as a pack keeps it: checked like a dashboard's, a platform one without an integration."""
    if isinstance(widget, dict) and widget.get("kind") == "platform":
        unbound = {**widget, "integration_id": str(_UNBOUND)}
        checked = validate_widget(unbound, where=where)
        checked.pop("integration_id", None)
        return checked
    return validate_widget(widget, where=where)


def bind_widget(widget: dict, integration_id: uuid.UUID | str | None) -> dict:
    """A copy of a pack widget for a dashboard: a platform widget reads through *integration_id*."""
    bound = copy.deepcopy(widget)
    if bound.get("kind") == "platform":
        if integration_id is None:
            raise DashboardSpecError(
                f"The {bound.get('source')} widget '{bound.get('id')}' needs an integration to read from."
            )
        bound["integration_id"] = str(integration_id)
    return bound


def parse_pack_document(document: object) -> ParsedWidgets:
    """Read a pack document; widgets that fail their checks are left out and reported."""
    if not isinstance(document, dict) or document.get("format") != PACK_FORMAT:
        raise InteropError(f"A widget pack names its format: '{PACK_FORMAT}'.")
    version = document.get("version")
    if version not in _READ_VERSIONS:
        raise InteropError(f"This widget pack is of version {version!r:.20}; this platform reads versions 1 and {PACK_VERSION}.")
    name = document.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_TITLE_CHARS:
        raise InteropError(f"A widget pack needs a name of at most {MAX_TITLE_CHARS} characters.")
    description = document.get("description")
    if description is not None and (not isinstance(description, str) or len(description) > MAX_TEXT_CHARS):
        raise InteropError(f"A widget pack's description is text of at most {MAX_TEXT_CHARS} characters.")
    widgets = document.get("widgets")
    if not isinstance(widgets, list) or not widgets:
        raise InteropError("A widget pack holds a non-empty 'widgets' list.")
    report = ConversionReport()
    if len(widgets) > MAX_PACK_WIDGETS:
        report.note(f"The pack holds {len(widgets)} widgets; the first {MAX_PACK_WIDGETS} were kept.")
        widgets = widgets[:MAX_PACK_WIDGETS]
    if version == 1:
        from app.services.dashboard_grid import legacy_layout

        widgets = [{**w, "layout": legacy_layout(w["layout"])} if isinstance(w, dict) and "layout" in w else w for w in widgets]
    kept: list[dict] = []
    for index, widget in enumerate(widgets):
        try:
            kept.append(validate_pack_widget(widget, where=f"widgets[{index}]"))
        except DashboardSpecError as exc:
            report.note(f"Widget {index + 1} was left out: {str(exc).splitlines()[-1].strip(' -')}")
    if not kept:
        raise InteropError("None of the pack's widgets could be read.")
    inputs: list[dict] = []
    raw_inputs = document.get("inputs")
    for index, entry in enumerate(raw_inputs[:MAX_PACK_INPUTS] if isinstance(raw_inputs, list) else []):
        try:
            inputs.append(validate_pack_input(entry, where=f"inputs[{index}]"))
        except DashboardSpecError as exc:
            report.note(f"Input {index + 1} was left out: {str(exc).splitlines()[-1].strip(' -')}")
    return ParsedWidgets(
        origin="xtm-one-pack",
        name=name.strip(),
        description=description,
        widgets=unique_ids(kept, report),
        report=report,
        inputs=inputs,
    )


def parse_pack(raw: bytes) -> ParsedWidgets:
    return parse_pack_document(loads_bounded(raw, what="widget pack"))


def build_pack(name: str, description: str | None, widgets: list[dict], inputs: list[dict] | None = None) -> dict:
    """A pack document from widgets (a dashboard's or a pack's) and the entity inputs they read: platform
    widgets and inputs lose their integration."""
    unbound = []
    for widget in widgets[:MAX_PACK_WIDGETS]:
        item = copy.deepcopy(widget)
        item.pop("integration_id", None)
        unbound.append(item)
    document: dict = {"format": PACK_FORMAT, "version": PACK_VERSION, "name": name, "widgets": unbound}
    if description:
        document["description"] = description
    loose = []
    for entry in (inputs or [])[:MAX_PACK_INPUTS]:
        item = copy.deepcopy(entry)
        if isinstance(item.get("options"), dict):
            item["options"].pop("integration_id", None)
        loose.append(item)
    if loose:
        document["inputs"] = loose
    return document


def unique_ids(widgets: list[dict], report: ConversionReport | None = None) -> list[dict]:
    """Every widget with an id of its own: a duplicate id is renamed, not refused.

    *report* notes a rename of ids the file itself gave; ids derived from titles
    (the platform converters) are renamed silently.
    """
    seen: set[str] = set()
    for widget in widgets:
        wid = widget.get("id")
        if wid is not None and wid in seen:
            if report is not None:
                report.note(f"Two widgets had the id '{wid}'; the second was renamed.")
            widget.pop("id")
        elif wid is not None:
            seen.add(wid)
    return assign_widget_ids(widgets)
