"""The in-place slide editor: the frontend catalog the inspector reads
is pinned to the Pydantic models, every palette default validates, and the
renderer publishes the targets the editor resolves.

The editor exposes exactly what ``presentation_spec.py`` accepts - one control
per model field, one option per ``Literal`` value - and nothing else. Two
readings of ``block-schema.ts`` / ``slide-schema.ts`` keep that true:

* a REGEX reading of the field descriptors (``en("size", "Size", SIZES)``,
  ``flag("fill", ...)``, ...), which needs nothing but the source and runs on
  every machine: every key is a field of the model, every enum's values are
  the ``Literal``'s;
* a NODE reading (``scripts/dump-presentation-schema.mts``) of the defaults
  the palette inserts, validated through ``validate_spec`` like a saved deck -
  skipped where Node cannot strip types, so it is an extra net, not the only
  one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import NoneType, UnionType
from typing import Annotated, Literal, Union, get_args, get_origin

import pytest
from pydantic import BaseModel

from app.models.template_container import ASSET_ROLES
from app.services.presentation_spec import (
    _BLOCK_MODELS,
    BLOCK_TYPES,
    SLIDE_LAYOUTS,
    PresentationSpecError,
    slide_model,
    validate_spec,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_ROOT = BACKEND_ROOT.parent / "frontend"
PRESENTATION_DIR = FRONTEND_ROOT / "src" / "components" / "presentation"


def _read(*parts: str) -> str:
    return PRESENTATION_DIR.joinpath(*parts).read_text(encoding="utf-8")


# ── Reading the TypeScript ───────────────────────────────────────────────────

_CONST_RE = re.compile(r"export const ([A-Z_]+) = \[([^\]]*)\] as const;")
_ENTRY_RE = re.compile(r"^  ([a-z_]+): \{$", re.MULTILINE)
_FIELD_RE = re.compile(
    r"\b(en|flag|num|markup|text|icons|icon|asset|strings|group|objects)\(\s*\"(\w+)\"(?:,\s*\"[^\"]*\")?(?:,\s*([A-Z_]+))?"
)


def _constants(source: str) -> dict[str, list[str | int]]:
    out: dict[str, list[str | int]] = {}
    for name, body in _CONST_RE.findall(source):
        values: list[str | int] = []
        for token in re.findall(r'"([^"]+)"|(\d+)', body):
            values.append(token[0] if token[0] else int(token[1]))
        out[name] = values
    return out


def _entries(source: str, start_marker: str) -> dict[str, str]:
    """The object literal of every ``  <key>: {`` entry after *start_marker*."""
    body = source[source.index(start_marker) :]
    matches = list(_ENTRY_RE.finditer(body))
    entries: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else body.index("\n};", match.end())
        entries[match.group(1)] = body[match.end() : end]
    return entries


def _shared_lists(source: str) -> dict[str, str]:
    """``export const METRIC_FIELDS: SchemaField[] = [ ... ];`` bodies by name."""
    out: dict[str, str] = {}
    for match in re.finditer(r"export const ([A-Z_]+_FIELDS): SchemaField\[\] = \[", source):
        start = match.end()
        end = source.index("\n];", start)
        out[match.group(1)] = source[start:end]
    return out


def _literal_values(annotation: object) -> set[object] | None:
    """The ``Literal`` values behind an annotation (``Optional`` / ``Annotated``
    unwrapped); an ``int`` bounded both ways stands for every whole number in
    its bounds (a chart's ``value_label_size``)."""
    origin = get_origin(annotation)
    if origin is Annotated:
        base = get_args(annotation)[0]
        if base is int:
            lo, hi = _numeric_bounds([], annotation)
            return set(range(lo, hi + 1)) if lo is not None and hi is not None else None
        return _literal_values(base)
    if origin is Literal:
        return set(get_args(annotation))
    if origin in (Union, UnionType):
        values: set[object] = set()
        for arg in get_args(annotation):
            if arg is NoneType:
                continue
            inner = _literal_values(arg)
            if inner is None:
                return None
            values |= inner
        return values or None
    return None


def _unwrap(annotation: object) -> object:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _unwrap(get_args(annotation)[0])
    if origin in (Union, UnionType):
        args = [arg for arg in get_args(annotation) if arg is not NoneType]
        if len(args) == 1:
            return _unwrap(args[0])
    return annotation


def _item_model(annotation: object) -> type[BaseModel] | None:
    """The model of ``list[Model]`` (Annotated / Optional unwrapped), or None."""
    inner = _unwrap(annotation)
    if get_origin(inner) is list:
        (element,) = get_args(inner)
        element = _unwrap(element)
        return element if isinstance(element, type) and issubclass(element, BaseModel) else None
    return None


def _call_text(body: str, kind: str, key: str) -> str | None:
    """The arguments of the ``<kind>("<key>", ...)`` descriptor call in *body*,
    up to its own closing paren (strings skipped, nested parens balanced)."""
    # The formatter may break the call before its key (`objects(\n  "rows", ...`).
    found = re.search(rf'\b{kind}\(\s*"{key}"', body)
    if found is None:
        return None
    open_at = body.index("(", found.start())
    depth = 0
    quote: str | None = None
    index = open_at
    while index < len(body):
        char = body[index]
        if quote:
            if char == "\\":
                index += 2  # the escaped character is part of the string
                continue
            if char == quote:
                quote = None
        elif char in "\"'`":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return body[open_at + 1 : index]
        index += 1
    return None


def _check_fields(model: type[BaseModel], body: str, constants: dict[str, list[str | int]], where: str) -> None:
    """Every descriptor in *body* names a field of *model* with a matching shape."""
    found = 0
    for kind, key, const in _FIELD_RE.findall(body):
        found += 1
        assert key in model.model_fields, f"{where}: {kind}({key!r}) is not a field of {model.__name__}"
        annotation = model.model_fields[key].annotation
        if kind == "en":
            assert const, f"{where}: en({key!r}) names no constant"
            assert const in constants, f"{where}: unknown constant {const}"
            literal = _literal_values(annotation)
            assert literal is not None, f"{where}: {model.__name__}.{key} is not a Literal"
            assert set(constants[const]) == literal, (
                f"{where}: {const} = {constants[const]} but {model.__name__}.{key} accepts {sorted(map(str, literal))}"
            )
            # A look the draft reads in normal form (`defaultValue`) is
            # written back unset at that value, so it must be the default the
            # model materialises - any other value reads dirty or saves a change.
            declared = re.search(r'defaultValue:\s*"(\w+)"', _call_text(body, "en", key) or "")
            if declared:
                model_default = model.model_fields[key].default
                assert declared.group(1) == model_default, (
                    f"{where}: en({key!r}) says defaultValue {declared.group(1)!r} but "
                    f"{model.__name__}.{key} defaults to {model_default!r}"
                )
        elif kind == "flag":
            assert _unwrap(annotation) is bool, f"{where}: flag({key!r}) but {model.__name__}.{key} is not a bool"
            # A switch shows the model's default while the field is unset: a
            # flag the model defaults to True must say so (`defaultOn`), and
            # only such a flag may - otherwise the switch reads off while the
            # slide draws on (or the reverse).
            call = _call_text(body, "flag", key)
            assert call is not None, f"{where}: flag({key!r}) call not found"
            default_on = "defaultOn: true" in call
            model_default = model.model_fields[key].default
            if _unwrap(annotation) is bool and model_default is True:
                assert default_on, f"{where}: {model.__name__}.{key} defaults to True but flag({key!r}) has no defaultOn"
            else:
                assert not default_on, f"{where}: flag({key!r}) says defaultOn but {model.__name__}.{key} does not default to True"
        elif kind == "num":
            assert _unwrap(annotation) in (int, float), (
                f"{where}: num({key!r}) but {model.__name__}.{key} is not a number"
            )
            # `num("number", "Number", 1, 99, ...)`: the control's range is the
            # model's `ge` / `le` where the model has one - a wider range is a
            # 422 the person meets at the save, a narrower one a value the
            # agent may write but the person cannot.
            call = _call_text(body, "num", key)
            assert call is not None, f"{where}: num({key!r}) call not found"
            bounds = re.match(r'\s*"\w+"\s*,\s*"[^"]*"\s*,\s*(-?[\d_]+)\s*,\s*(-?[\d_]+)', call)
            assert bounds, f"{where}: num({key!r}) declares no min / max"
            lo, hi = (int(bounds.group(1).replace("_", "")), int(bounds.group(2).replace("_", "")))
            model_lo, model_hi = _numeric_bounds(model.model_fields[key].metadata, annotation)
            if model_lo is not None:
                assert lo == model_lo, f"{where}: num({key!r}) min {lo} but {model.__name__}.{key} has ge={model_lo}"
            if model_hi is not None:
                assert hi == model_hi, f"{where}: num({key!r}) max {hi} but {model.__name__}.{key} has le={model_hi}"
        elif kind in ("markup", "text"):
            assert _unwrap(annotation) is str, f"{where}: {kind}({key!r}) but {model.__name__}.{key} is not a str"
        elif kind == "icon":
            assert _unwrap(annotation) is str, f"{where}: icon({key!r}) but {model.__name__}.{key} is not a str"
        elif kind == "asset":
            # A picture picked from the template: a file id field, and
            # roles an asset can actually carry - an unknown role offers nothing.
            assert _unwrap(annotation) is str and key.endswith("file_id"), (
                f"{where}: asset({key!r}) but {model.__name__}.{key} is not a file id"
            )
            assert const in constants, f"{where}: asset({key!r}) names no role constant"
            unknown = set(constants[const]) - set(ASSET_ROLES)
            assert not unknown, f"{where}: asset({key!r}) offers roles no template asset has: {sorted(unknown)}"
        elif kind in ("strings", "icons"):
            # `icons("icon_strip", "Icons", 6)` is a list of icon names,
            # each one checked against the catalog like an `icon` field.
            inner = _unwrap(annotation)
            assert get_origin(inner) is list and _unwrap(get_args(inner)[0]) is str, (
                f"{where}: {kind}({key!r}) but {model.__name__}.{key} is not a list of str"
            )
            # `strings("items", "Bullets", 8)`: the list editor stops adding at
            # the model's `max_length`, never one past it.
            call = _call_text(body, kind, key)
            assert call is not None, f"{where}: {kind}({key!r}) call not found"
            declared = re.match(r'\s*"\w+"\s*,\s*"[^"]*"\s*,\s*(\d+)', call)
            assert declared, f"{where}: {kind}({key!r}) declares no max"
            _, model_max = _length_bounds(model.model_fields[key].metadata, annotation)
            assert int(declared.group(1)) == model_max, (
                f"{where}: {kind}({key!r}) max {declared.group(1)} but {model.__name__}.{key} has max_length={model_max}"
            )
        elif kind == "group":
            assert isinstance(_unwrap(annotation), type) and issubclass(_unwrap(annotation), BaseModel), (
                f"{where}: group({key!r}) but {model.__name__}.{key} is not a model"
            )
        elif kind == "objects":
            # `objects("rows", "Rows", 1, 6, [...])`: a nested list of models,
            # its editor bounded like the model's list (its own fields are
            # checked against the row model by the caller).
            assert _item_model(annotation) is not None, (
                f"{where}: objects({key!r}) but {model.__name__}.{key} is not a list of models"
            )
            call = _call_text(body, kind, key)
            assert call is not None, f"{where}: objects({key!r}) call not found"
            declared = re.match(r'\s*"\w+"\s*,\s*"[^"]*"\s*,\s*(\d+)\s*,\s*(\d+)', call)
            assert declared, f"{where}: objects({key!r}) declares no min / max"
            model_min, model_max = _length_bounds(model.model_fields[key].metadata, annotation)
            assert (int(declared.group(1)), int(declared.group(2))) == (model_min, model_max), (
                f"{where}: objects({key!r}) bounds {declared.group(1)}-{declared.group(2)} but "
                f"{model.__name__}.{key} has {model_min}-{model_max}"
            )
    assert found > 0, f"{where}: no field descriptor found"


def _nested_body(body: str, key: str) -> str:
    """The `[...]` list that follows ``"<key>"`` in a group descriptor (or a
    literal *key* such as ``fields:`` in an items descriptor)."""
    start = body.index(key if key.endswith(":") else f'"{key}"')
    open_at = body.index("[", start)
    depth = 0
    for index in range(open_at, len(body)):
        if body[index] == "[":
            depth += 1
        elif body[index] == "]":
            depth -= 1
            if depth == 0:
                return body[open_at + 1 : index]
    raise AssertionError(f"unbalanced list after {key!r}")


def _items_of(body: str) -> tuple[str, str] | None:
    """(key, fields body) of an ``items: { key: "...", ... fields: [...] }`` entry."""
    match = re.search(r'items: \{\s*key: "(\w+)"', body)
    if not match:
        return None
    key = match.group(1)
    fields_at = body.index("fields:", match.end())
    fields_expr = body[fields_at + len("fields:") :].lstrip()
    if fields_expr.startswith("["):
        return key, _nested_body(body[fields_at:], "fields:")
    # `fields: METRIC_FIELDS,` - a shared list, resolved by the caller.
    return key, fields_expr.split(",", 1)[0].strip()


# ── The catalogs mirror the models ───────────────────────────────────────────


def test_frontend_block_schema_mirrors_the_block_models():
    source = _read("block-schema.ts")
    constants = _constants(source)
    shared = _shared_lists(source)
    entries = _entries(source, "export const BLOCK_SCHEMA")
    missing = [block for block in BLOCK_TYPES if block not in entries]
    assert missing == [], f"block-schema.ts has no entry for: {missing}"
    extra = [key for key in entries if key not in BLOCK_TYPES]
    assert extra == [], f"block-schema.ts names blocks the catalog lacks: {extra}"

    def resolve(body: str) -> str:
        # `fields: METRIC_FIELDS,` / `fields: [...PHASE_FIELDS, flag(...)]` - inline the shared lists.
        for name, list_body in shared.items():
            body = body.replace(name, list_body)
        return body

    for block, body in entries.items():
        model = _BLOCK_MODELS[block]
        # Top-level fields, groups included (a group's own fields are checked
        # against the group's model below), items excluded. Shared lists
        # (`...PHASE_FIELDS`) are inlined first so their groups are read too.
        top = resolve(body.split("items:", 1)[0] if "items: {" in body else body)
        # A group's children must be read against the group's model, not the block's.
        groups = re.findall(r'group\(\s*"(\w+)"', top)
        without_groups = top
        for group_key in groups:
            group_model = _unwrap(model.model_fields[group_key].annotation)
            assert isinstance(group_model, type) and issubclass(group_model, BaseModel)
            nested = _nested_body(top, group_key)
            _check_fields(group_model, nested, constants, f"{block}.{group_key}")
            without_groups = without_groups.replace(nested, "")
        _check_fields(model, without_groups, constants, block)
        items = _items_of(body)
        if items:
            key, fields_body = items
            assert key in model.model_fields, f"{block}: items key {key!r} is not a field"
            item_model = _item_model(model.model_fields[key].annotation)
            assert item_model is not None, f"{block}.{key} is not a list of models"
            item_body = shared.get(fields_body, fields_body)
            item_groups = re.findall(r'group\(\s*"(\w+)"', item_body)
            for group_key in item_groups:
                group_model = _unwrap(item_model.model_fields[group_key].annotation)
                _check_fields(group_model, _nested_body(item_body, group_key), constants, f"{block}.{key}.{group_key}")
                item_body = item_body.replace(_nested_body(item_body, group_key), "")
            # A nested list of objects (a configuration card's rows): its
            # fields are read against the row model, then left out.
            for list_key in re.findall(r'objects\(\s*"(\w+)"', item_body):
                row_model = _item_model(item_model.model_fields[list_key].annotation)
                assert row_model is not None, f"{block}.{key}.{list_key} is not a list of models"
                nested = _nested_body(item_body, list_key)
                _check_fields(row_model, nested, constants, f"{block}.{key}.{list_key}")
                item_body = item_body.replace(nested, "")
            _check_fields(item_model, item_body, constants, f"{block}.{key}")


def test_frontend_slide_schema_mirrors_the_layout_models():
    source = _read("slide-schema.ts")
    block_source = _read("block-schema.ts")
    constants = {**_constants(block_source), **_constants(source)}
    shared = _shared_lists(block_source)
    entries = _entries(source, "export const SLIDE_SCHEMA")
    missing = [layout for layout in SLIDE_LAYOUTS if layout not in entries]
    assert missing == [], f"slide-schema.ts has no entry for: {missing}"
    extra = [key for key in entries if key not in SLIDE_LAYOUTS]
    assert extra == [], f"slide-schema.ts names layouts the catalog lacks: {extra}"
    speaker_body = _nested_body(source[source.index("const SPEAKER = group(") :], "speaker")
    for layout, body in entries.items():
        model = slide_model(layout)
        assert model is not None
        top = body.split("items:", 1)[0] if "items: {" in body else body
        if "SPEAKER" in top:
            speaker_model = _unwrap(model.model_fields["speaker"].annotation)
            _check_fields(speaker_model, speaker_body, constants, f"{layout}.speaker")
            top = top.replace("SPEAKER", "")
        _check_fields(model, top, constants, layout)
        items = _items_of(body)
        if items:
            key, fields_body = items
            assert key in model.model_fields, f"{layout}: items key {key!r} is not a field"
            item_model = _item_model(model.model_fields[key].annotation)
            assert item_model is not None, f"{layout}.{key} is not a list of models"
            item_body = shared.get(fields_body, fields_body)
            for group_key in re.findall(r'group\(\s*"(\w+)"', item_body):
                group_model = _unwrap(item_model.model_fields[group_key].annotation)
                _check_fields(group_model, _nested_body(item_body, group_key), constants, f"{layout}.{key}.{group_key}")
                item_body = item_body.replace(_nested_body(item_body, group_key), "")
            _check_fields(item_model, item_body, constants, f"{layout}.{key}")


# ── Every model field is edited by hand, or its absence is a decision ────────

#: Model fields the inspector does NOT carry, on purpose - each one named, so
#: a field the catalog misses is a failing build, never a silent gap. The
#: structural ones are edited ON THE STAGE (a block is selected, moved,
#: inserted, duplicated, deleted; a table's rows by its own editor; a dashboard
#: panel's block is selected like any block); `*.footer_block` is the footer
#: slot of every layout that has one.
_EDITED_ON_THE_STAGE = {
    "callout.children",
    "columns.columns",
    "table.rows",
    "content.blocks",
    "content.columns",
    "dashboard.panels",
    "*.footer_block",
    # A profile's EXISTING lines are text runs edited in place on the stage
    # (one row each - the editor commits on Enter there); adding or
    # removing a line, or giving one an icon, is the assistant's for now (a
    # line's logo is a file, and no list-of-objects control exists yet).
    "profiles.items.lines",
}
#: What needs a file, numbers or a graph: the assistant's, said so in the
#: palette and the inspector (`image`, `logo_grid`, `chart`, `architecture`).
_ASSISTANT_ONLY = {
    "image.file_id",
    # A video's source is a Video Studio video or an MP4 file the assistant
    # places; its render, poster and captions are filled from video_id on save.
    "video.video_id",
    "video.file_id",
    "video.poster_file_id",
    "video.captions_file_id",
    # An external link (YouTube / Vimeo player, or an MP4 / WebM imported on save).
    "video.url",
    "logo_grid.items.file_id",
    # A logo's own size override (0.5-1.5): a fractional number the inspector's
    # whole-number control cannot hold; the assistant sets it per logo.
    "logo_grid.items.scale",
    "logo_wall.groups.logos",
    # A formula's operators are one per gap, a list no control holds yet.
    "formula.operators",
    "profiles.items.photo_file_id",
    "metric.sparkline",
    "metrics_grid.items.sparkline",
    "dashboard.tiles.sparkline",
    "chart.series",
    "chart.categories",
    # A waterfall's totals are indices into its categories.
    "chart.totals",
    "architecture.nodes",
    "architecture.edges",
    "architecture.zones",
    "architecture.legend",
    # An org chart is its tree of seats: who reports to whom is the assistant's.
    "org_chart.nodes",
    # A positioning map's axes, quadrants and placed items are the assistant's.
    "positioning_map.x_axis",
    "positioning_map.y_axis",
    "positioning_map.quadrants",
    "positioning_map.items",
    "positioning_map.callouts",
    "org_chart.expand",
    "org_chart.roots",
    "org_chart.legend_items",
}
#: Common to every layout and handled by the inspector itself (see the header
#: of `slide-schema.ts`), or the model's own discriminator / identity. `theme`
#: and `overlay` are the inspector's own Theme and Overlay pickers.
#: `palette` is the inspector's own Palette control (`SLIDE_PALETTE_FIELD`).
_COMMON_SLIDE_FIELDS = {"layout", "id", "notes", "background_image_file_id", "theme", "overlay", "palette"}
#: Drawn by a dedicated control of the inspector rather than a schema
#: descriptor: the hero thumbnails of the title and closing slides and the
#: colour chips under them (`HeroPicker` / `HeroTonePicker` in
#: `SlideStylePickers.tsx`); a quote's four looks as thumbnails and its two
#: pictures, picked from the deck template's assets
#: (`QuoteVariantPicker`, `TemplateAssetField`).
_INSPECTOR_OWN_CONTROLS = {
    "title.hero",
    "title.hero_tone",
    "closing.hero",
    "closing.hero_tone",
    "quote.variant",
    "quote.logo_file_id",
    "quote.photo_file_id",
}
#: Shorthands the agent may write for fields the inspector edits directly:
#: a logo grid's `size` is `logo_height` for sm / md / lg, a callout's
#: `size` sets `icon_size` and `title_size` - one control per effect, so the
#: inspector draws the canonical fields and leaves the shorthand to the spec.
_ALIASES = {"logo_grid.size", "callout.size"}


def _descriptor_keys(body: str, shared: dict[str, str]) -> set[str]:
    """The model fields the descriptors of *body* name at the top level (groups
    count as their own field), plus the items list's key."""
    for name, list_body in shared.items():
        body = body.replace(name, list_body)
    top = body.split("items:", 1)[0] if "items: {" in body else body
    keys = {key for _kind, key, _const in _FIELD_RE.findall(top)}
    items = _items_of(body)
    if items:
        keys.add(items[0])
    return keys


def _item_descriptor_keys(body: str, shared: dict[str, str]) -> tuple[str, set[str]] | None:
    items = _items_of(body)
    if not items:
        return None
    key, fields_body = items
    item_body = shared.get(fields_body, fields_body)
    for name, list_body in shared.items():
        item_body = item_body.replace(name, list_body)
    return key, {k for _kind, k, _const in _FIELD_RE.findall(item_body)}


def test_every_model_field_is_edited_by_hand_or_deliberately_not():
    """The reverse of the mirror: every field of every block and layout model
    (and of their item models) is either a descriptor the inspector draws or
    an entry of the allow-lists above - and every allow-list entry still names
    a field, so the lists cannot go stale either way."""
    block_source = _read("block-schema.ts")
    slide_source = _read("slide-schema.ts")
    shared = _shared_lists(block_source)
    allowed = _EDITED_ON_THE_STAGE | _ASSISTANT_ONLY | _INSPECTOR_OWN_CONTROLS | _ALIASES
    seen: set[str] = set()

    def check(owner: str, model: type[BaseModel], covered: set[str], skip: set[str]) -> None:
        for field in model.model_fields:
            if field in skip or field in covered:
                continue
            entry = f"{owner}.{field}"
            wildcard = f"*.{field}"
            assert entry in allowed or wildcard in allowed, (
                f"{entry} is a field of {model.__name__} the inspector does not edit and no allow-list names - "
                "add a descriptor, or say why it is edited on the stage / left to the assistant"
            )
            seen.add(entry if entry in allowed else wildcard)

    for block, body in _entries(block_source, "export const BLOCK_SCHEMA").items():
        model = _BLOCK_MODELS[block]
        check(block, model, _descriptor_keys(body, shared), {"type"})
        items = _item_descriptor_keys(body, shared)
        if items:
            key, keys = items
            item_model = _item_model(model.model_fields[key].annotation)
            if item_model is not None:
                check(f"{block}.{key}", item_model, keys, set())
    for layout, body in _entries(slide_source, "export const SLIDE_SCHEMA").items():
        model = slide_model(layout)
        assert model is not None
        body = body.replace("SPEAKER", 'group("speaker"')
        check(layout, model, _descriptor_keys(body, shared), _COMMON_SLIDE_FIELDS)
        items = _item_descriptor_keys(body, shared)
        if items:
            key, keys = items
            item_model = _item_model(model.model_fields[key].annotation)
            if item_model is not None:
                check(f"{layout}.{key}", item_model, keys, set())

    stale = sorted(allowed - seen)
    assert stale == [], f"allow-list entries naming no model field the catalog leaves out: {stale}"


# ── The defaults the palette inserts validate ────────────────────────────────


def _node_dump() -> dict:
    """The catalog as `scripts/dump-presentation-schema.mts` prints it.

    A developer machine without Node (or with one too old to strip types)
    skips these tests with the reason; CI never does - the Backend job pins
    Node (`actions/setup-node`), so a dump that cannot run there is a broken
    tripwire, and a skipped tripwire is a green build that checked nothing.
    """
    on_ci = bool(os.environ.get("CI"))
    node = shutil.which("node")
    if not node:
        if on_ci:
            pytest.fail("node is not installed on the CI runner; the Backend job must set it up")
        pytest.skip("node is not installed")
    script = FRONTEND_ROOT / "scripts" / "dump-presentation-schema.mts"
    result = subprocess.run([node, str(script)], capture_output=True, text=True, cwd=FRONTEND_ROOT, check=False)  # noqa: S603
    if result.returncode != 0:
        reason = f"node cannot run the schema dump here: {result.stderr.strip()[:400]}"
        if on_ci:
            pytest.fail(reason)
        pytest.skip(reason)
    return json.loads(result.stdout)


def _deck(*slides: dict) -> dict:
    return {"title": "Editor defaults", "slides": list(slides)}


def _content(*blocks: dict) -> dict:
    return {"layout": "content", "title": "Defaults", "blocks": list(blocks)}


def test_every_palette_default_validates_where_the_editor_may_insert_it():
    dump = _node_dump()
    assert set(dump["blocks"]) == set(BLOCK_TYPES)
    for block_type in dump["insertable"]:
        entry = dump["blocks"][block_type]
        default = entry["create"]
        assert default and default["type"] == block_type
        # In a slide body.
        validate_spec(_deck(_content(default)))
        # Where the nesting rules admit it - and refused where they do not.
        callout = {"type": "callout", "title": "Host", "children": [default]}
        column_slide = {
            "layout": "content",
            "title": "Cols",
            "columns": [[default], [{"type": "text", "content": "x"}]],
        }
        comparison = {
            "type": "comparison",
            "left": {"label": "A", "blocks": [default]},
            "right": {"label": "B", "blocks": [{"type": "text", "content": "x"}]},
        }
        for host, allowed in (
            (callout, entry["allowed_in"]["callout"]),
            (comparison, entry["allowed_in"]["comparison"]),
        ):
            if allowed:
                validate_spec(_deck(_content(host)))
            else:
                with pytest.raises(PresentationSpecError):
                    validate_spec(_deck(_content(host)))
        if entry["allowed_in"]["column"]:
            validate_spec(_deck(column_slide))
        else:
            with pytest.raises(PresentationSpecError):
                validate_spec(_deck(column_slide))
        # One more item, as "Add" appends it (a table's editor adds the new
        # column's cell on every row, as the validator requires). Every list
        # the palette can insert has an item factory; a list without one is
        # never grown by hand (a logo needs a file), so nothing to check.
        items = entry["items"]
        if items:
            assert items["item"] is not None, f"{block_type}.{items['key']} has no item factory"
            grown = json.loads(json.dumps(default))
            grown.setdefault(items["key"], [])
            if len(grown[items["key"]]) < items["max"]:
                grown[items["key"]].append(items["item"])
                if block_type == "table":
                    for row in grown["rows"]:
                        row.append({"text": ""})
                validate_spec(_deck(_content(grown)))


def test_every_layout_default_validates_and_grows_by_one_item():
    dump = _node_dump()
    assert set(dump["slides"]) == set(SLIDE_LAYOUTS)
    for layout, entry in dump["slides"].items():
        default = entry["create"]
        assert default["layout"] == layout
        validate_spec(_deck(default))
        items = entry["items"]
        if items:
            grown = json.loads(json.dumps(default))
            grown.setdefault(items["key"], [])
            if len(grown[items["key"]]) < items["max"]:
                grown[items["key"]].append(items["item"])
                validate_spec(_deck(grown))


def test_item_bounds_in_the_schema_are_the_models_bounds():
    """The list editor refuses to add past ``max`` and to remove below ``min``:
    those numbers are read off the model's ``Field(min_length, max_length)``."""
    dump = _node_dump()
    for block_type, entry in dump["blocks"].items():
        items = entry["items"]
        if not items:
            continue
        field = _BLOCK_MODELS[block_type].model_fields[items["key"]]
        lo, hi = _length_bounds(field.metadata, field.annotation)
        assert items["min"] == lo, f"{block_type}.{items['key']}: schema min {items['min']} vs model {lo}"
        assert items["max"] == hi, f"{block_type}.{items['key']}: schema max {items['max']} vs model {hi}"
    for layout, entry in dump["slides"].items():
        items = entry["items"]
        if not items:
            continue
        field = slide_model(layout).model_fields[items["key"]]  # type: ignore[union-attr]
        lo, hi = _length_bounds(field.metadata, field.annotation)
        assert items["min"] == lo, f"{layout}.{items['key']}: schema min {items['min']} vs model {lo}"
        assert items["max"] == hi, f"{layout}.{items['key']}: schema max {items['max']} vs model {hi}"


def test_an_emptied_list_is_written_back_as_the_model_stores_no_items():
    """Removing a list's last item writes what the stored spec spells for no
    items: ``[]`` for a list with a ``default_factory``, the key UNSET
    for an optional list that defaults to ``None`` (``unsetWhenEmpty``, a
    quote's ``figures``) - otherwise adding then removing an item reads dirty
    and offers a Save that writes an empty list the model never stores."""
    dump = _node_dump()
    lists = [(block_type, _BLOCK_MODELS[block_type], entry["items"]) for block_type, entry in dump["blocks"].items()]
    lists += [(layout, slide_model(layout), entry["items"]) for layout, entry in dump["slides"].items()]
    for owner, model, items in lists:
        if not items:
            continue
        where = f"{owner}.{items['key']}"
        if items["min"] > 0:
            assert not items["unset_when_empty"], f"{where}: a list never emptied by hand needs no unsetWhenEmpty"
            continue
        field = model.model_fields[items["key"]]  # type: ignore[union-attr]
        stores_none = field.default_factory is None and field.default is None
        assert items["unset_when_empty"] == stores_none, (
            f"{where}: the model stores {'no key' if stores_none else '[]'} for no items, "
            f"so unsetWhenEmpty must be {str(stores_none).lower()}"
        )


def _constraint_items(metadata: list, annotation: object) -> list:
    """The constraint objects of a field: its own metadata plus the args of
    every ``Annotated`` layer (an ``Optional[Annotated[int, Field(ge=1)]]``
    keeps its bounds one level down)."""
    candidates = list(metadata)
    pending = [annotation]
    while pending:
        inner = pending.pop()
        origin = get_origin(inner)
        if origin is Annotated:
            args = get_args(inner)
            candidates.extend(args[1:])
            pending.append(args[0])
        elif origin in (Union, UnionType):
            pending.extend(arg for arg in get_args(inner) if arg is not NoneType)
    # A `Field(ge=1, le=99)` left inside an `Annotated` arm carries its
    # constraints as `FieldInfo.metadata` (`Ge(1)`, `Le(99)`, `MaxLen(8)`).
    flat: list = []
    for item in candidates:
        nested = getattr(item, "metadata", None)
        if isinstance(nested, list) and not isinstance(item, type):
            flat.extend(nested)
        flat.append(item)
    return flat


def test_the_slide_and_block_run_caps_in_the_schema_are_the_models_caps():
    """`Add slide`, `Duplicate` and the insert palette stop at the validator's
    caps (`DEFAULT_SLIDE_CAP`, `MAX_BLOCKS_PER_*`), read off the models here so
    the editor can never offer a save the validator refuses - nor stop short of
    one it would accept. The slide cap is the deck's own (the
    response's `max_slides`); the constant is what the editor falls back to
    when a cached response predates the field, so it is the platform default."""
    from app.services import presentation_spec as spec

    slide_source = _read("slide-schema.ts")
    block_source = _read("block-schema.ts")

    def constant(source: str, name: str) -> int:
        match = re.search(rf"^export const {name} = (\d+);$", source, re.MULTILINE)
        assert match, f"{name} is not exported as a plain number"
        return int(match.group(1))

    assert constant(slide_source, "DEFAULT_SLIDE_CAP") == spec.DEFAULT_SLIDE_CAP
    assert "MAX_SLIDES" not in slide_source, "the flat cap is gone: the deck's cap is the template's"
    # The editor reads the deck's own cap, never the constant alone.
    editor_source = _read("presentation-editor.ts")
    assert "export function canAddSlide(draft: DeckDraft, maxSlides" in editor_source
    assert "presentation?.max_slides" in editor_source or "presentation.max_slides" in editor_source
    panel_source = _read("PresentationEditorPanel.tsx")
    assert "max_slides" in panel_source
    for name in (
        "MAX_BLOCKS_PER_SLIDE",
        "MAX_BLOCKS_PER_COLUMN",
        "MAX_BLOCKS_PER_CONTAINER",
        "MAX_TABLE_COLUMNS",
        "MAX_TABLE_ROWS",
    ):
        assert constant(block_source, name) == getattr(spec, name), name
    # The table editor grows the grid by those, never by a literal of its own.
    controls = _read("SchemaFieldControls.tsx")
    assert "disabled={headers.length >= MAX_TABLE_COLUMNS}" in controls
    assert "disabled={rows.length >= MAX_TABLE_ROWS}" in controls
    # The capacity reader names the cap of every run the editor can grow: the
    # body, a column (of the slide or of a columns block), a callout's
    # children, a comparison side - the fields the models cap with them.
    assert "export function blockRunCapacity(listPath: string): number {" in block_source
    assert 'if (listPath === "blocks") return MAX_BLOCKS_PER_SLIDE;' in block_source
    assert r"if (/\.children$|\.(left|right)\.blocks$/.test(listPath)) return MAX_BLOCKS_PER_CONTAINER;" in block_source
    assert "return MAX_BLOCKS_PER_COLUMN;" in block_source
    # Enforced on every path that grows a run or the deck, not only in the UI.
    session = _read("presentation-editor.ts")
    assert session.count("canGrowRun(") >= 4, "insertBlock and duplicateBlock check the run's cap"
    assert session.count("canAddSlide(") >= 4, "insertSlide and duplicateSlide check the deck's cap"


def _length_bounds(metadata: list, annotation: object) -> tuple[int, int]:
    """``(min_length, max_length)`` of a list field, from its metadata or its ``Annotated`` args."""
    lo, hi = 0, 10**6
    for item in _constraint_items(metadata, annotation):
        if getattr(item, "min_length", None) is not None:
            lo = item.min_length
        if getattr(item, "max_length", None) is not None:
            hi = item.max_length
    return lo, hi


def _numeric_bounds(metadata: list, annotation: object) -> tuple[int | None, int | None]:
    """``(ge, le)`` of a numeric field, ``None`` where the model sets no bound."""
    lo: int | None = None
    hi: int | None = None
    for item in _constraint_items(metadata, annotation):
        if getattr(item, "ge", None) is not None:
            lo = item.ge
        if getattr(item, "le", None) is not None:
            hi = item.le
    return lo, hi


# ── The renderer publishes the targets, the editor never builds HTML ─────────


def test_every_block_view_publishes_its_edit_target():
    """Every block component spreads ``useBlockEditAttrs()`` (or the attrs a
    shared piece receives) on its root, or a click on that block selects
    nothing on the stage."""
    views = "\n".join(_read("blocks", path.name) for path in (PRESENTATION_DIR / "blocks").glob("*.tsx"))
    for block in BLOCK_TYPES:
        view = "".join(part.capitalize() for part in block.split("_")) + "BlockView"
        start = views.index(f"export function {view}(")
        body = views[start : views.index("\n}", start)]
        assert "useBlockEditAttrs()" in body or "editAttrs" in body, f"{view} publishes no data-edit-block"


_HTML_SINK_RE = re.compile(
    r"dangerouslySetInnerHTML|innerHTML|insertAdjacentHTML|outerHTML|document\.write|DOMParser|insertHTML|createContextualFragment"
)


def _code_lines(source: str) -> list[str]:
    """The lines of *source* that are not comments (a header may NAME a sink)."""
    return [line for line in source.splitlines() if not re.match(r"\s*(//|/\*|\*)", line)]


def test_the_inline_editor_and_the_runs_model_have_no_html_sink():
    """No file of the presentation module - the editor's layer, the editable
    field, the runs model, the inspector, the thumbnails, every renderer -
    reaches an HTML sink, wherever it sits in the tree. `blocks/Diagram.tsx`
    is the ONE exception (its own Mermaid SVG through DOMPurify's SVG profile,
    pinned by `test_presentation_spec_v2`), and `sanitize.ts` sanitizes for
    the REPORT renderer and builds no HTML itself."""
    allowed_sinks = {"blocks/Diagram.tsx"}
    offenders: list[str] = []
    for path in sorted(PRESENTATION_DIR.rglob("*.ts*")):
        relative = path.relative_to(PRESENTATION_DIR).as_posix()
        if relative in allowed_sinks or relative.endswith(".test.ts"):
            continue
        for line in _code_lines(path.read_text(encoding="utf-8")):
            if _HTML_SINK_RE.search(line):
                offenders.append(f"{relative}: {line.strip()[:80]}")
    assert offenders == [], offenders
    for name in (
        "InlineTextEditor.tsx",
        "inline-runs.ts",
        "inline-runs-dom.ts",
        "inline-markup.tsx",
        "SlideEditLayer.tsx",
        "SlideThumbnail.tsx",
        "SchemaFieldControls.tsx",
        "SlideInspector.tsx",
    ):
        assert _read(name)  # the files the editor is made of exist under these names
    dom = _read("inline-runs-dom.ts")
    # Nodes are built one to one with the renderer's segments.
    for tag in ('createElement("strong")', 'createElement("em")', 'createElement("code")', 'createElement("br")'):
        assert tag in dom, tag
    assert 'element.className = "deck-span"' in dom


def test_the_markup_problem_reader_mirrors_the_validator():
    """The editor flags what ``parse_inline_markup`` refuses, with the SAME
    regexes, so a string it lets through is a string the save accepts."""
    from app.services.presentation_markup import _MARKER_RE, _TAG_LIKE_RE

    markup = _read("markup.ts")
    # A JS regex literal escapes the slash the Python pattern writes bare.
    assert f"const TAG_LIKE_RE = /{_TAG_LIKE_RE.pattern.replace('/', r'\/')}/;" in markup
    assert f"const MARKER_RE = /{_MARKER_RE.pattern}/;" in markup
    assert "export function markupProblem(" in markup


def test_the_stage_and_the_pages_render_the_edit_session_the_same_way():
    """Both surfaces provide the session, mark the root, publish the slide
    index on every `.deck-slide`, and mount the layer over the slides."""
    deck = _read("PresentationDeck.tsx")
    assert 'data-deck-editing={edit?.enabled ? "true" : undefined}' in deck
    assert "<EditModeContext.Provider value={edit}>" in deck
    # The slide frame's index and id - what a click is resolved to - are
    # published in EDIT MODE ONLY: a presented deck, the chat card, the export
    # page and a thumbnail render the DOM they always did, attribute for
    # attribute (the outer `.deck-static-slide` keeps the id the audit reads).
    static = _read("StaticSlide.tsx")
    for source, name in ((deck, "PresentationDeck.tsx"), (static, "StaticSlide.tsx")):
        assert "data-slide-index={editing ? index : undefined}" in source, name
        assert "data-slide-id={editing ? slide.id : undefined}" in source, name
        assert "const editing = !!useEditMode();" in source, name
        assert 'className="deck-slide" data-layout={slide.layout} data-slide-index={index}' not in source, name
    assert (
        "<SlideIndexContext.Provider value={index}>" in static and "<SlideIndexContext.Provider value={index}>" in deck
    )
    stage = _read("DeckStage.tsx")
    assert "edit={editor?.enabled ? editor.stageSession : null}" in stage
    assert "!fullscreen.isFullscreen && (" in stage  # never over a presented stage
    pages = _read("PageDocument.tsx")
    assert 'data-deck-editing={editor?.enabled ? "true" : undefined}' in pages
    assert "followsActiveSlide={false}" in pages
    css = _read("presentation.css")
    for selector in (
        '.deck-root[data-deck-editing="true"] [data-edit-selected="true"]',
        '.deck-root[data-deck-editing="true"] .deck-slide[data-edit-selected="slide"]',
        '.deck-root [data-edit-editing="true"]',
    ):
        assert selector in css, selector
    # Outlines only: nothing here may move a pixel of the slide. (The swatches
    # of the stage bar live outside the canvas and may have a border.)
    editing_css = css[css.index("In-place editing") : css.index("The tone swatches of the stage")]
    assert not re.search(r"^\s+(margin|padding|border)\s*:", editing_css, re.MULTILINE), (
        "the edit styles must not change layout"
    )


def test_typing_targets_never_reach_the_deck_or_the_layer_shortcuts():
    """reveal ignores a contentEditable target; the layer's shortcuts ignore a
    field and the panel; the editor stops its own keys from bubbling."""
    layer = _read("SlideEditLayer.tsx")
    assert "const TYPING_TARGETS = \"input,textarea,select,[contenteditable]:not([contenteditable='false'])\";" in layer
    assert 'document.addEventListener("keydown", onKeyDown, true);' in layer
    editor = _read("InlineTextEditor.tsx")
    assert "contentEditable" in editor and "suppressContentEditableWarning" in editor
    assert "event.stopPropagation();" in editor
    assert 'const onBlur = () => finish("commit");' in editor
    # A key pressed while an IME composes (Enter confirming a Japanese
    # conversion, Escape dropping it) is the composition's, never a line break
    # or a cancelled edit.
    assert "if (event.isComposing) {" in editor
    # Pasting inserts the clipboard's PLAIN text: the grammar has no room for
    # a pasted `<font>`, and the DOM is never handed a string of HTML.
    assert 'event.clipboardData?.getData("text/plain")' in editor
    assert 'execCommand("insertText"' in editor
    assert "insertHTML" not in editor
    # So does a drop: the drag's plain text, never its HTML.
    assert 'element.addEventListener("drop", onDrop);' in editor
    assert 'event.dataTransfer?.getData("text/plain")' in editor
    assert 'getData("text/html")' not in editor
    # A held key auto-repeats: one commit attempt per press, never a refused
    # commit and its toast per repeat - the field's commit keys and
    # Ctrl+S alike (the repeat is still kept from the browser).
    assert editor.count('if (!event.repeat) finish("done");') == 2
    assert "if (!event.repeat) current.requestSave();" in layer
    # The formatting bar never takes the focus from the field.
    assert "onMouseDown={(event) => event.preventDefault()}" in layer
    # A text open in place hands its words to the draft before a revision
    # arriving meanwhile is merged; one it cannot hand over yet
    # (markup the save would refuse) holds the merge, and a text open is new
    # work a Discard a 409 deferred never resets under the caret. The
    # tab-close guard asks the field for its last words, not `dirty` alone.
    session = _read("presentation-editor.ts")
    assert 'if (textHeld) return "hold";' in session
    assert 'if (askedOver !== null && askedOver === timeline && !textOpen) return "reset";' in session
    assert 'const handover = open ? open.flush() : "clean";' in session
    assert "unsaved: dirty || editingText !== null," in session
    # The revision watch compares the query's revision with the one the
    # session is seeded on, whichever moved: a save answered after the
    # poll loaded a newer revision is rebased, then merged at once - and
    # the cache never rolls back to that older answer.
    assert "}, [presentation?.revision, baseRevision, saveInFlight, editingText, outOfDate ? draft : null]);" in session
    api = (FRONTEND_ROOT / "src" / "hooks" / "api" / "usePresentationApi.ts").read_text(encoding="utf-8")
    assert api.count("(cached) => newestDeck(cached, deck)") == 2
    autosave = _read("useDeckAutosave.ts")
    assert "const warn = (event: BeforeUnloadEvent) => {" in autosave
    assert 'const handover = activeInlineEditor()?.flush() ?? "clean";' in autosave
    # A save whose base the server is past (a 409, a collaborator's revision
    # not merged yet) is never sent - it could only meet another 409.
    assert "if (!enabledRef.current || abandoned.current || !ed || !ed.dirty || ed.outOfDate)" in autosave


def test_the_editor_and_the_cached_deck_never_move_back_to_an_older_revision():
    """The newest deck wins whatever order the answers land in: a
    save's answer is judged against the base it was sent from, the revision
    watch adopts only a newer revision, and the deck query keeps the newest
    revision against a read answered after a save."""
    session = _read("presentation-editor.ts")
    # A save's answer rebases the session only while it still sits on the
    # base the save extended - the rule a 409's refusal already follows.
    assert "if (!answersBase(sent, seeded.current)) return;" in session
    assert "return answersBase(sent, seeded) ? { id: sent.id, revision: sent.revision } : null;" in session
    autosave = _read("useDeckAutosave.ts")
    assert "ed.adoptSaved(saved, sent, sentDraft);" in autosave
    assert "ed.saveRefused(sent);" in autosave
    # Only a NEWER revision of the seeded deck arrives, in the revision watch
    # and in the merge that waited for an open text's words.
    assert "if (!presentation || !revisionArrives(presentation, seeded.current)) return;" in session
    assert "if (mergeTick === 0 || !presentation || !revisionArrives(presentation, seeded.current)) return;" in session
    assert "presentation.revision !== seeded.current.revision" not in session
    # Every write into the cached deck goes through the deck's sharing.
    api = (FRONTEND_ROOT / "src" / "hooks" / "api" / "usePresentationApi.ts").read_text(encoding="utf-8")
    start = api.index("export function usePresentation(")
    deck_query = api[start : api.index("export function useExportPresentation(")]
    assert "structuralSharing: shareNewestDeck," in deck_query
    # While a save is in flight nothing is merged - its own commit may be
    # what the poll brought - and leaving the editor waits for its answer.
    assert "if (defersToSave(saveInFlight, seeded.current)) return;" in session
    assert "ed.beginSave(sent);" in autosave
    assert "      } finally {\n        ed.endSave(sent);" in autosave
    assert "await ed.whenSaveSettles();" in autosave
    viewer = (FRONTEND_ROOT / "src" / "pages" / "PresentationViewer.tsx").read_text(encoding="utf-8")
    leave = viewer[viewer.index("const leaveEditor = useCallback(") :]
    assert leave.index("if (await autosaveRef.current.flush()) return true;") < leave.index("await confirmEditor(")
