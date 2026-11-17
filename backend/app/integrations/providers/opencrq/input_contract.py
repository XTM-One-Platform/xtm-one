"""Whether the connected OpenCRQ accepts the fields a tool is about to send.

OpenCRQ validates its inputs with Zod, which drops a key its schema does not know
without a word: a tool that sends a field the connected instance lacks gets an
answer that looks like success, with the field ignored. :class:`InputContract`
holds a call's input against the schema the instance's own manifest gives that
procedure, before anything is sent, and refuses an unknown key or a missing
required one.

Only the shape is checked: keys and ``required``, at any depth. A value's type,
``enum``, a ``const`` outside the choice of a union branch, a pattern or a bound
is OpenCRQ's to refuse, which it does loudly with ``BAD_REQUEST``.

The walk reads the reduced JSON Schema a manifest keeps (``UNDERSTOOD_KEYWORDS``)
and fails open on anything else, so an instance that learns a new construct never
refuses a call it does not understand: a subtree with an unknown keyword, a
``$ref`` that is not local and a union with no branch for the value's JSON type
all pass.

Pure: no I/O.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from .procedure_manifest import INSTANCE_LABEL, UNDERSTOOD_KEYWORDS, Manifest, load_tool_input_keys

_COMPOSITION = frozenset({"anyOf", "oneOf", "allOf", "$ref", "$defs"})
_STRUCTURE = frozenset({"type", "properties", "required", "items", "additionalProperties"})
# Deeper than any real input or schema; a reference cycle stops here and passes.
_MAX_DEPTH = 32
# A union nested in an intersection multiplies; past this the schema is not worth reading.
_MAX_LEAVES = 256
# What a refusal lists before it counts the rest: a path can come from the model's own arguments.
_MAX_LISTED = 5
_MAX_PATH_LENGTH = 80

Leaf = dict[str, Any]
Issues = tuple[list[str], list[str]]


def _resolve_ref(ref: object, root: Mapping[str, Any]) -> Mapping[str, Any] | None:
    prefix = "#/$defs/"
    if not isinstance(ref, str) or not ref.startswith(prefix):
        return None
    defs = root.get("$defs")
    target = defs.get(ref[len(prefix) :]) if isinstance(defs, Mapping) else None
    return target if isinstance(target, Mapping) else None


def _merge(left: Leaf, right: Leaf) -> Leaf:
    """Two constraints that hold together: what either knows, either requires, either opens."""
    merged: Leaf = {**left}
    if "type" in right:
        kinds = [right["type"]] if isinstance(right["type"], str) else list(right["type"])
        if "type" in left:
            held = [left["type"]] if isinstance(left["type"], str) else list(left["type"])
            kinds = [kind for kind in held if kind in kinds]
        merged["type"] = kinds
    if "properties" in right:
        properties = dict(left.get("properties", {}))
        for name, schema in right["properties"].items():
            properties[name] = {"allOf": [properties[name], schema]} if name in properties else schema
        merged["properties"] = properties
    if "required" in right:
        merged["required"] = [*left.get("required", []), *right["required"]]
    if "items" in right:
        merged["items"] = {"allOf": [left["items"], right["items"]]} if "items" in left else right["items"]
    extra = right.get("additionalProperties")
    if extra not in (None, False) and left.get("additionalProperties") in (None, False):
        merged["additionalProperties"] = extra
    for keyword in ("const", "enum", "propertyNames"):
        if keyword in right and keyword not in merged:
            merged[keyword] = right[keyword]
    return merged


def _leaves(schema: object, root: Mapping[str, Any], depth: int = 0) -> list[Leaf] | None:
    """The alternatives *schema* comes down to, each free of ``anyOf`` / ``oneOf`` / ``allOf`` / ``$ref``.

    ``None`` when the schema cannot be read, which the callers treat as "pass".
    """
    if depth > _MAX_DEPTH or not isinstance(schema, Mapping) or not schema.keys() <= UNDERSTOOD_KEYWORDS:
        return None
    # A conjunction of unions: the schema's own constraints, each union, each allOf member, the $ref target.
    conjuncts: list[list[Leaf]] = []
    own = {keyword: part for keyword, part in schema.items() if keyword not in _COMPOSITION}
    if own:
        conjuncts.append([own])
    for keyword in ("anyOf", "oneOf"):
        if keyword in schema:
            union: list[Leaf] = []
            for branch in schema[keyword]:
                leaves = _leaves(branch, root, depth + 1)
                if leaves is None:
                    return None
                union += leaves
            conjuncts.append(union)
    for member in schema.get("allOf", []):
        leaves = _leaves(member, root, depth + 1)
        if leaves is None:
            return None
        conjuncts.append(leaves)
    if "$ref" in schema:
        target = _resolve_ref(schema["$ref"], root)
        leaves = _leaves(target, root, depth + 1) if target is not None else None
        if leaves is None:
            return None
        conjuncts.append(leaves)
    combined: list[Leaf] = [{}]
    for union in conjuncts:
        combined = [_merge(leaf, alternative) for leaf in combined for alternative in union]
        if len(combined) > _MAX_LEAVES:
            return None
    return combined


def _json_types(value: object) -> set[str]:
    if value is None:
        return {"null"}
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, int):
        return {"integer", "number"}
    if isinstance(value, float):
        return {"number", "integer"} if value.is_integer() else {"number"}
    if isinstance(value, str):
        return {"string"}
    if isinstance(value, Mapping):
        return {"object"}
    if isinstance(value, list | tuple):
        return {"array"}
    return set()


def _accepts_type(leaf: Leaf, value: object) -> bool:
    kinds = leaf.get("type")
    if kinds is None:
        return True
    return bool(_json_types(value) & set([kinds] if isinstance(kinds, str) else kinds))


def _is_open(leaf: Leaf) -> bool:
    """Whether the object accepts keys it does not list: a schema or ``{}`` as ``additionalProperties``, or no object constraint at all.

    An absent ``additionalProperties`` is closed: Zod emits none for an object and drops the extra keys.
    """
    extra = leaf.get("additionalProperties")
    if extra is not None and extra is not False:
        return True
    return not leaf.keys() & _STRUCTURE


def _matches_discriminator(leaf: Leaf, value: Mapping[str, Any]) -> bool:
    """A union branch tells itself apart by a ``const`` property; the value's own decides which branch it is."""
    for key, schema in leaf.get("properties", {}).items():
        if isinstance(schema, Mapping) and "const" in schema and key in value and value[key] != schema["const"]:
            return False
    return True


def _child(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _one_of(schemas: list[Any]) -> Any:
    return schemas[0] if len(schemas) == 1 else {"anyOf": schemas}


def _walk(
    schema: object,
    value: object,
    path: str,
    root: Mapping[str, Any],
    unknown: list[str],
    missing: list[str],
    depth: int,
) -> None:
    if depth > _MAX_DEPTH or not isinstance(value, Mapping | list | tuple):
        return
    leaves = _leaves(schema, root)
    if leaves is None:
        return
    candidates = [leaf for leaf in leaves if _accepts_type(leaf, value)]
    if isinstance(value, Mapping):
        _walk_object(candidates, value, path, root, unknown, missing, depth)
    elif candidates and all("items" in leaf for leaf in candidates):
        items = _one_of([leaf["items"] for leaf in candidates])
        for index, element in enumerate(value):
            _walk(items, element, f"{path}[{index}]", root, unknown, missing, depth + 1)


def _walk_object(
    leaves: list[Leaf],
    value: Mapping[str, Any],
    path: str,
    root: Mapping[str, Any],
    unknown: list[str],
    missing: list[str],
    depth: int,
) -> None:
    candidates = [leaf for leaf in leaves if _matches_discriminator(leaf, value)]
    if not candidates:
        return
    for key, entry in value.items():
        children: list[Any] = []
        known = False
        for leaf in candidates:
            properties = leaf.get("properties", {})
            if key in properties:
                known = True
                children.append(properties[key])
            elif _is_open(leaf):
                known = True
                extra = leaf.get("additionalProperties")
                if isinstance(extra, Mapping):
                    children.append(extra)
        if not known:
            # A None under a key OpenCRQ does not know is a field left unset, and dropping it loses nothing.
            if entry is not None:
                unknown.append(_child(path, key))
        elif children:
            _walk(_one_of(children), entry, _child(path, key), root, unknown, missing, depth + 1)
    lacking = [[name for name in leaf.get("required", []) if name not in value] for leaf in candidates]
    if all(lacking):
        missing += [_child(path, name) for name in min(lacking, key=len)]


def issues(schema: object, value: object, root: Mapping[str, Any] | None = None) -> Issues:
    """``(unknown keys, missing required keys)`` of *value* against *schema*, as paths.

    ``root`` is where ``#/$defs/...`` references resolve, the procedure's own schema by default.
    A union is read through the branches that fit the value: a key is unknown when no branch knows
    it, and a required key is missing when every branch lacks one (the closest branch names which).
    """
    unknown: list[str] = []
    missing: list[str] = []
    _walk(
        schema,
        value,
        "",
        root if root is not None else schema if isinstance(schema, Mapping) else {},
        unknown,
        missing,
        0,
    )
    return unknown, missing


def _listed(paths: list[str]) -> str:
    shown = [f"`{path[:_MAX_PATH_LENGTH]}`" for path in paths[:_MAX_LISTED]]
    if len(paths) > _MAX_LISTED:
        shown.append(f"{len(paths) - _MAX_LISTED} more")
    return ", ".join(shown)


@dataclass(frozen=True)
class InputContract:
    """The inputs the connected OpenCRQ accepts, as its *manifest* gives them."""

    manifest: Manifest

    def issues(self, procedure: str, value: object) -> Issues:
        """What the manifest finds wrong with *value* as the input of *procedure*; nothing when it has no schema."""
        schema = self.manifest.procedures.get(procedure, {}).get("input")
        return issues(schema, value) if schema is not None else ([], [])

    def refusal(self, procedure: str, unknown: list[str], missing: list[str]) -> str:
        """What a call that would lose a field, or lacks a required one, answers instead of being sent."""
        clauses: list[str] = []
        if unknown:
            clauses.append(f"does not accept the field(s) {_listed(unknown)} and would drop them")
        if missing:
            clauses.append(f"requires the field(s) {_listed(missing)}")
        return f"Error: nothing was sent to OpenCRQ: {procedure} on {INSTANCE_LABEL} {' and '.join(clauses)}."

    def check(self, procedure: str, value: object) -> str | None:
        """The refusal for *value* as the input of *procedure*, ``None`` when the instance takes it."""
        unknown, missing = self.issues(procedure, value)
        return self.refusal(procedure, unknown, missing) if unknown or missing else None


def _collect_paths(
    schema: object, root: Mapping[str, Any], prefix: str, found: set[str], opaque: set[str], depth: int = 0
) -> None:
    """Every property path *schema* knows, below *prefix*: ``a.b``, ``list[].key``, ``map.*.key``.

    A subtree the walk cannot read goes into *opaque*: the caller cannot tell what it knows.
    """
    if depth > _MAX_DEPTH:
        opaque.add(prefix)
        return
    leaves = _leaves(schema, root)
    if leaves is None:
        opaque.add(prefix)
        return
    for leaf in leaves:
        for name, child in leaf.get("properties", {}).items():
            path = _child(prefix, name)
            found.add(path)
            _collect_paths(child, root, path, found, opaque, depth + 1)
        extra = leaf.get("additionalProperties")
        if isinstance(extra, Mapping) and extra:
            _collect_paths(extra, root, _child(prefix, "*"), found, opaque, depth + 1)
        if "items" in leaf:
            _collect_paths(leaf["items"], root, f"{prefix}[]", found, opaque, depth + 1)


def known_paths(schema: object) -> tuple[set[str], set[str]]:
    """``(paths, opaque prefixes)`` of *schema*: the keys its object accepts, at any depth.

    The static twin of :func:`issues`, which reads the same leaves for a value: a path is known
    here exactly when no value carrying it is refused as unknown there.
    """
    found: set[str] = set()
    opaque: set[str] = set()
    if isinstance(schema, Mapping):
        _collect_paths(schema, schema, "", found, opaque)
    return found, opaque


@dataclass(frozen=True)
class LimitedField:
    """A field a tool sends that the instance refuses, or one it requires that the tool never sends."""

    procedure: str
    path: str
    kind: Literal["unknown", "missing"]


@dataclass
class _Node:
    lists: int = 0
    children: dict[str, _Node] = field(default_factory=dict)


def _sample(paths: Collection[str]) -> dict[str, Any]:
    """A value carrying exactly *paths* (``a.b``, ``rows[].k``), each leaf a non-null scalar."""
    root: dict[str, _Node] = {}
    for path in paths:
        level = root
        for segment in path.split("."):
            node = level.setdefault(segment.split("[]")[0], _Node())
            node.lists = max(node.lists, segment.count("[]"))
            level = node.children
    return _build(root)


def _build(level: Mapping[str, _Node]) -> dict[str, Any]:
    built: dict[str, Any] = {}
    for name, node in level.items():
        value: Any = _build(node.children) if node.children else 1
        for _ in range(node.lists if node.children else 0):
            value = [value]
        built[name] = value
    return built


_INDEX = re.compile(r"\[\d+\]")


def limited_fields(manifest: Manifest, hidden: Collection[str] = ()) -> dict[str, list[LimitedField]]:
    """The tools *manifest* serves that send a field it refuses, or never send one it requires.

    Each call a tool can send (``tool_input_keys.json``) is held against the instance's schema by the
    walk ``_trpc`` runs on a real call, on a value carrying the keys the call spells out, so the card
    cannot say a field is refused when a call would pass, nor the reverse. A field the walk cannot
    decide (a schema it does not read, a union it cannot tell apart) is not listed. A required field
    is listed only at the top level and only for a call whose keys are all spelled out.
    Tools in *hidden* are skipped: they are not offered.
    """
    limited: dict[str, list[LimitedField]] = {}
    for tool, entries in load_tool_input_keys().items():
        if tool in hidden:
            continue
        found: set[LimitedField] = set()
        for entry in entries:
            sample = _sample(entry["fields"])
            for procedure in entry["procedures"]:
                schema = manifest.procedures.get(procedure, {}).get("input")
                if schema is None:
                    continue
                unknown, missing = issues(schema, sample)
                found |= {LimitedField(procedure, _INDEX.sub("[]", path), "unknown") for path in unknown}
                if entry.get("closed"):
                    found |= {
                        LimitedField(procedure, path, "missing")
                        for path in missing
                        if "." not in path and "[" not in path
                    }
        if found:
            limited[tool] = sorted(found, key=lambda f: (f.procedure, f.path, f.kind))
    return dict(sorted(limited.items()))
