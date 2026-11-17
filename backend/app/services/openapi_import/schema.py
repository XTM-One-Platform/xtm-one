# SPDX-License-Identifier: Apache-2.0

"""``$ref`` resolution and OpenAPI schema → tool-argument JSON Schema conversion.

OpenAPI schemas are not tool schemas. They lean on ``$ref`` (Gemini's tool
API accepts none), inheritance through ``allOf``, dialect quirks (3.0's
``nullable`` and boolean ``exclusiveMinimum``, 2.0's ``x-nullable`` and
``type: file``) and response-only fields (``readOnly``). The converter emits
the plain, self-contained subset every LLM provider accepts:

* every local ``$ref`` is inlined; a reference cycle is cut into a stub that
  still names the type, so recursive models (trees, comments) stay usable;
* ``allOf`` is merged into one object; ``oneOf`` becomes ``anyOf`` — the
  agent loop validates arguments with ``jsonschema`` before dispatch, and
  ``oneOf`` over overlapping object shapes (discriminated models, very
  common) would reject valid calls; ``null`` branches are dropped;
* ``readOnly`` properties are removed (these schemas describe REQUESTS);
* object keys every provider accepts (``^[A-Za-z0-9_.-]{1,64}$``) are kept,
  others are named in the description instead of breaking the whole request;
* size is bounded: nesting past ``max_depth`` and anything past the node
  budget become typed stubs ("pass it as JSON"), enums past a cap become a
  description sample, descriptions are trimmed by depth.

External references (another file or a URL) are never fetched: they are
reported through :class:`RefResolver` and replaced by a permissive schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

#: Property names every LLM provider accepts in a tool input schema — the
#: strictest published rule (Anthropic's). Top-level tool arguments are
#: renamed to fit; nested object keys that do not fit are described instead.
SAFE_PROPERTY_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_MAX_REF_HOPS = 32
_MAX_REF_STACK = 64
_MAX_VARIANTS = 12
_MAX_ENUM_VALUES = 100
_ENUM_SAMPLE = 12
_MAX_PROPERTIES = 120
#: Longest ``pattern`` described to the model (a cut regex would mislead).
_MAX_PATTERN_CHARS = 200
_DESCRIPTION_LIMITS = (600, 300)
_NESTED_DESCRIPTION_LIMIT = 160

_PRIMITIVE_TYPES = frozenset({"object", "array", "string", "integer", "number", "boolean", "null"})
_KEPT_FORMATS = frozenset(
    {
        "date-time",
        "date",
        "time",
        "duration",
        "email",
        "idn-email",
        "hostname",
        "ipv4",
        "ipv6",
        "uri",
        "uri-reference",
        "iri",
        "uuid",
        "int32",
        "int64",
        "float",
        "double",
        "byte",
        "password",
    }
)
_NUMERIC_KEYWORDS = (
    "minimum",
    "maximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "minProperties",
    "maxProperties",
)
#: Keys of a Swagger 2.0 non-body parameter (and of its "Items Object") that
#: describe the value — the parameter IS its schema in 2.0.
SWAGGER2_PARAMETER_SCHEMA_KEYS = frozenset(
    {
        "type",
        "format",
        "items",
        "enum",
        "default",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "multipleOf",
    }
)


class UnresolvedRefError(Exception):
    """A ``$ref`` that cannot be followed."""

    def __init__(self, ref: str, reason: str) -> None:
        self.ref = ref
        self.reason = reason
        super().__init__(f"{ref}: {reason}")


class RefResolver:
    """Follows same-document JSON-pointer references (``#/components/...``)."""

    def __init__(self, document: dict[str, Any]) -> None:
        self._document = document
        #: ref → why it could not be followed (reported once per ref).
        self.unresolved: dict[str, str] = {}

    def lookup(self, ref: Any) -> Any:
        """The node *ref* points at; raises :class:`UnresolvedRefError`."""
        if not isinstance(ref, str) or not ref.startswith("#"):
            raise UnresolvedRefError(str(ref), "references to other files or URLs are not followed")
        pointer = ref[1:]
        node: Any = self._document
        if pointer == "":
            return node
        if not pointer.startswith("/"):
            raise UnresolvedRefError(ref, "not a JSON pointer")
        for raw_token in pointer[1:].split("/"):
            # A URI fragment: percent-decode first, then the JSON-pointer escapes.
            token = unquote(raw_token).replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and token in node:
                node = node[token]
            elif (
                isinstance(node, list)
                and token.isascii()
                and token.isdigit()
                and len(token) < 19
                and int(token) < len(node)
            ):
                node = node[int(token)]
            else:
                raise UnresolvedRefError(ref, "the referenced definition does not exist")
        return node

    def deref(self, node: Any) -> Any:
        """Resolve a ``$ref`` chain on a NON-schema object.

        Parameters, request bodies, path items, security schemes and
        responses. An OpenAPI 3.1 Reference Object may carry ``summary`` /
        ``description`` siblings that override the target's.
        """
        seen: list[str] = []
        while isinstance(node, dict) and isinstance(node.get("$ref"), str):
            ref = node["$ref"]
            if ref in seen or len(seen) >= _MAX_REF_HOPS:
                raise UnresolvedRefError(ref, "circular reference")
            seen.append(ref)
            overrides = {k: v for k, v in node.items() if k in ("summary", "description")}
            target = self.lookup(ref)
            node = {**target, **overrides} if overrides and isinstance(target, dict) else target
        return node

    def record(self, exc: UnresolvedRefError) -> None:
        self.unresolved.setdefault(exc.ref, exc.reason)


@dataclass
class _Run:
    """Per-conversion state: the node budget and the depth limit."""

    max_depth: int
    max_nodes: int
    nodes: int = 0


def clean_text(text: Any, limit: int) -> str:
    """Collapse whitespace and trim to *limit* characters (with an ellipsis).

    Spec text reaches the model (tool and argument descriptions): control and
    format characters - zero-width, bidirectional overrides, Unicode tags -
    are dropped, so no instruction can hide in text a person reviewing the
    tool cannot see.
    """
    if not isinstance(text, str):
        return ""
    visible = "".join(ch for ch in text if ch.isprintable() or ch.isspace())
    collapsed = " ".join(visible.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(limit - 1, 0)].rstrip() + "…"


def _description_limit(depth: int) -> int:
    return _DESCRIPTION_LIMITS[depth] if depth < len(_DESCRIPTION_LIMITS) else _NESTED_DESCRIPTION_LIMIT


def _append_note(out: dict[str, Any], note: str) -> None:
    current = out.get("description")
    out["description"] = f"{current} {note}".strip() if current else note


def _merge(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    """Merge two converted schemas; *primary* wins, objects are unioned."""
    out = dict(secondary)
    for key, value in primary.items():
        if key == "properties" and isinstance(value, dict):
            out["properties"] = {**(secondary.get("properties") or {}), **value}
        elif key == "required" and isinstance(value, list):
            merged = list(secondary.get("required") or [])
            merged.extend(r for r in value if r not in merged)
            out["required"] = merged
        elif key == "description" and "description" in secondary and value != secondary["description"]:
            out["description"] = value
        else:
            out[key] = value
    return out


def _is_null_schema(schema: dict[str, Any]) -> bool:
    return schema.get("type") == "null"


def declared_type(schema: Any) -> str | None:
    """The single non-null JSON type a raw schema declares or implies."""
    if not isinstance(schema, dict):
        return None
    raw = schema.get("type")
    if isinstance(raw, list):
        candidates = [t for t in raw if isinstance(t, str) and t != "null"]
        raw = candidates[0] if len(candidates) == 1 else None
    if isinstance(raw, str) and raw in _PRIMITIVE_TYPES:
        return raw
    if "properties" in schema or "additionalProperties" in schema:
        return "object"
    if "items" in schema:
        return "array"
    return None


def is_binary_schema(schema: Any) -> bool:
    """A converted schema that carries file content (cannot be sent as text)."""
    if not isinstance(schema, dict):
        return False
    if schema.get("format") == "binary":
        return True
    items = schema.get("items")
    return isinstance(items, dict) and items.get("format") == "binary"


class SchemaConverter:
    """Converts OpenAPI schemas of one document (see the module docstring)."""

    def __init__(self, resolver: RefResolver, *, family: str) -> None:
        self.resolver = resolver
        self.family = family

    def convert(
        self,
        schema: Any,
        *,
        max_depth: int = 8,
        max_nodes: int = 4000,
    ) -> dict[str, Any] | None:
        """Convert a REQUEST schema. ``None`` means "no value can satisfy it"."""
        return self._convert(schema, 0, (), _Run(max_depth=max_depth, max_nodes=max_nodes))

    def is_read_only(self, schema: Any) -> bool:
        """Whether a property schema is ``readOnly`` (itself or its ``$ref`` target)."""
        if not isinstance(schema, dict):
            return False
        if schema.get("readOnly") is True:
            return True
        ref = schema.get("$ref")
        if isinstance(ref, str):
            try:
                target = self.resolver.lookup(ref)
            except UnresolvedRefError:
                return False
            return isinstance(target, dict) and target.get("readOnly") is True
        return False

    # -- internals ---------------------------------------------------------

    def _convert(
        self,
        schema: Any,
        depth: int,
        ref_stack: tuple[str, ...],
        run: _Run,
    ) -> dict[str, Any] | None:
        if schema is None or schema is True:
            return {}
        if schema is False:
            return None
        if not isinstance(schema, dict):
            return {}
        run.nodes += 1

        ref = schema.get("$ref")
        if isinstance(ref, str):
            return self._convert_ref(schema, ref, depth, ref_stack, run)
        if run.nodes > run.max_nodes:
            return self._stub(schema, depth, "(structure omitted; pass it as JSON)")

        all_of = schema.get("allOf")
        if isinstance(all_of, list) and all_of:
            return self._convert_all_of(schema, all_of, depth, ref_stack, run)

        for combinator in ("oneOf", "anyOf"):
            variants = schema.get(combinator)
            if isinstance(variants, list) and variants:
                return self._convert_variants(schema, variants, depth, ref_stack, run)

        raw_type = schema.get("type")
        if isinstance(raw_type, list):
            non_null = [t for t in raw_type if isinstance(t, str) and t != "null"]
            if len(non_null) > 1:
                options = [
                    c
                    for c in (
                        self._convert({**schema, "type": t}, depth, ref_stack, run)
                        for t in non_null[:_MAX_VARIANTS]
                    )
                    if c is not None
                ]
                out: dict[str, Any] = {"anyOf": options}
                description = clean_text(
                    schema.get("description") or schema.get("title"), _description_limit(depth)
                )
                if description:
                    out["description"] = description
                return out
        return self._convert_plain(schema, declared_type(schema), depth, ref_stack, run)

    def _convert_ref(
        self,
        schema: dict[str, Any],
        ref: str,
        depth: int,
        ref_stack: tuple[str, ...],
        run: _Run,
    ) -> dict[str, Any] | None:
        if ref in ref_stack or len(ref_stack) >= _MAX_REF_STACK:
            return self._recursive_stub(ref)
        try:
            target = self.resolver.lookup(ref)
        except UnresolvedRefError as exc:
            self.resolver.record(exc)
            return {"description": f"Schema {ref} could not be resolved; pass it as JSON."}
        resolved = self._convert(target, depth, (*ref_stack, ref), run)
        # OpenAPI 3.1 allows keywords beside $ref (description, nullable...):
        # they refine the target, and win over it.
        siblings = {k: v for k, v in schema.items() if k != "$ref"}
        if siblings and resolved is not None:
            refined = self._convert(siblings, depth, ref_stack, run)
            if refined:
                resolved = _merge(refined, resolved)
        return resolved

    def _convert_all_of(
        self,
        schema: dict[str, Any],
        members: list[Any],
        depth: int,
        ref_stack: tuple[str, ...],
        run: _Run,
    ) -> dict[str, Any] | None:
        parts = [self._convert(member, depth, ref_stack, run) for member in members]
        if any(part is None for part in parts):
            return None
        own_raw = {k: v for k, v in schema.items() if k != "allOf"}
        merged = self._convert(own_raw, depth, ref_stack, run) if own_raw else {}
        if merged is None:
            return None
        for part in parts:
            merged = _merge(merged, part or {})
        return merged

    def _convert_variants(
        self,
        schema: dict[str, Any],
        variants: list[Any],
        depth: int,
        ref_stack: tuple[str, ...],
        run: _Run,
    ) -> dict[str, Any] | None:
        own_raw = {k: v for k, v in schema.items() if k not in ("oneOf", "anyOf", "discriminator")}
        own = (self._convert(own_raw, depth, ref_stack, run) if own_raw else {}) or {}
        pool = list(schema.get("oneOf") or []) + list(schema.get("anyOf") or [])
        options: list[dict[str, Any]] = []
        for variant in pool[:_MAX_VARIANTS]:
            converted = self._convert(variant, depth, ref_stack, run)
            if converted is None or _is_null_schema(converted):
                continue
            if converted not in options:
                options.append(converted)
        if not options:
            return own
        if len(options) == 1:
            return _merge(own, options[0])
        out = dict(own)
        # A declared object type beside the variants would make every
        # non-object variant unsatisfiable; the variants carry their types.
        if out.get("type") == "object" and not out.get("properties"):
            out.pop("type")
        out["anyOf"] = options
        discriminator = schema.get("discriminator")
        if isinstance(discriminator, dict) and isinstance(discriminator.get("propertyName"), str):
            _append_note(
                out, f"The '{discriminator['propertyName']}' field selects the variant."
            )
        return out

    def _convert_plain(
        self,
        schema: dict[str, Any],
        json_type: str | None,
        depth: int,
        ref_stack: tuple[str, ...],
        run: _Run,
    ) -> dict[str, Any] | None:
        out: dict[str, Any] = {}
        raw_type = schema.get("type")
        if raw_type == "file":
            # Swagger 2.0 file upload.
            out = {"type": "string", "format": "binary"}
            description = clean_text(schema.get("description"), _description_limit(depth))
            if description:
                out["description"] = description
            return out
        if json_type:
            out["type"] = json_type

        description = clean_text(
            schema.get("description") or schema.get("title"), _description_limit(depth)
        )
        if schema.get("deprecated") is True:
            description = f"Deprecated. {description}".strip()
        if description:
            out["description"] = description

        self._copy_value_keywords(schema, json_type, out)

        if json_type == "array":
            if depth >= run.max_depth:
                _append_note(out, "(item structure omitted; pass the items as JSON)")
            else:
                items = self._convert(schema.get("items"), depth + 1, ref_stack, run)
                if items:
                    out["items"] = items
        elif json_type == "object":
            self._convert_object(schema, depth, ref_stack, run, out)
        return out

    def _copy_value_keywords(
        self, schema: dict[str, Any], json_type: str | None, out: dict[str, Any]
    ) -> None:
        fmt = schema.get("format")
        media = schema.get("contentMediaType")
        if fmt == "binary" or (
            isinstance(media, str)
            and not media.startswith(("text/", "application/json"))
            and json_type in (None, "string")
        ):
            out["format"] = "binary"
        elif isinstance(fmt, str) and fmt in _KEPT_FORMATS:
            out["format"] = fmt

        enum = schema.get("enum")
        if "const" in schema and isinstance(schema["const"], str | int | float | bool):
            enum = [schema["const"]]
        if isinstance(enum, list):
            self._copy_enum(enum, json_type, out)

        default = schema.get("default")
        if isinstance(default, str | int | float | bool) and not (
            json_type in ("integer", "number") and isinstance(default, str)
        ):
            out["default"] = default

        for key in _NUMERIC_KEYWORDS:
            value = schema.get(key)
            if isinstance(value, int | float) and not isinstance(value, bool):
                out[key] = value
        for exclusive, inclusive in (
            ("exclusiveMinimum", "minimum"),
            ("exclusiveMaximum", "maximum"),
        ):
            value = schema.get(exclusive)
            if isinstance(value, bool):
                # OpenAPI 2.0 / 3.0 boolean form → JSON Schema 2020-12 number.
                if value and inclusive in out:
                    out[exclusive] = out.pop(inclusive)
            elif isinstance(value, int | float):
                out[exclusive] = value
        if schema.get("uniqueItems") is True:
            out["uniqueItems"] = True

        pattern = schema.get("pattern")
        if isinstance(pattern, str) and pattern.strip() and len(pattern) <= _MAX_PATTERN_CHARS:
            # Described, never enforced: tool arguments are validated with a
            # backtracking regex engine, on which a pattern like ``^(a+)+$``
            # can hold the worker for minutes. The API checks it anyway.
            _append_note(out, f"Pattern: {pattern}")

    @staticmethod
    def _copy_enum(enum: list[Any], json_type: str | None, out: dict[str, Any]) -> None:
        values: list[Any] = []
        for value in enum:
            if value is None or not isinstance(value, str | int | float | bool):
                continue
            if json_type == "string" and not isinstance(value, str):
                # YAML reads ``- 1`` under ``type: string`` as an int; the
                # spec means the string.
                value = ("true" if value else "false") if isinstance(value, bool) else str(value)
            if value not in values:
                values.append(value)
        if not values:
            return
        if len(values) <= _MAX_ENUM_VALUES:
            out["enum"] = values
            return
        sample = ", ".join(str(v) for v in values[:_ENUM_SAMPLE])
        _append_note(out, f"One of {len(values)} values, e.g. {sample}, …")

    def _convert_object(
        self,
        schema: dict[str, Any],
        depth: int,
        ref_stack: tuple[str, ...],
        run: _Run,
        out: dict[str, Any],
    ) -> None:
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        if depth >= run.max_depth:
            if properties:
                _append_note(out, "(nested fields omitted; pass the object as JSON)")
            return
        converted: dict[str, Any] = {}
        undescribable: list[str] = []
        omitted = 0
        for index, (name, prop_schema) in enumerate(properties.items()):
            if index >= _MAX_PROPERTIES:
                omitted = len(properties) - _MAX_PROPERTIES
                break
            if self.is_read_only(prop_schema):
                continue
            if not SAFE_PROPERTY_KEY_RE.match(name):
                undescribable.append(name)
                continue
            value = self._convert(prop_schema, depth + 1, ref_stack, run)
            if value is None:
                continue
            converted[name] = value
        if converted:
            out["properties"] = converted
        required = schema.get("required")
        if isinstance(required, list):
            kept = [r for r in required if isinstance(r, str) and r in converted]
            if kept:
                out["required"] = kept

        additional = schema.get("additionalProperties")
        if isinstance(additional, dict):
            value = self._convert(additional, depth + 1, ref_stack, run)
            if value:
                out["additionalProperties"] = value
        elif additional is False and not undescribable and not omitted:
            out["additionalProperties"] = False

        if undescribable:
            _append_note(out, "Also accepts the fields: " + ", ".join(undescribable[:20]) + ".")
        if omitted:
            _append_note(out, f"({omitted} more fields not listed.)")

    def _stub(self, schema: dict[str, Any], depth: int, note: str) -> dict[str, Any]:
        out: dict[str, Any] = {}
        json_type = declared_type(schema)
        if json_type:
            out["type"] = json_type
        description = clean_text(schema.get("description"), _description_limit(depth))
        out["description"] = f"{description} {note}".strip()
        return out

    def _recursive_stub(self, ref: str) -> dict[str, Any]:
        """Where a reference cycle is cut: the type survives, the recursion does not."""
        name = ref.rsplit("/", 1)[-1] or ref
        out: dict[str, Any] = {"description": f"{name} (recursive structure; pass it as JSON)."}
        try:
            json_type = declared_type(self.resolver.lookup(ref))
        except UnresolvedRefError:
            json_type = None
        if json_type in ("object", "array"):
            out["type"] = json_type
        return out
