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

"""STRUCTURED_JSON — per-field scoring modes and their scorers.

A field's treatment is derived from **its own schema declaration** and may be overridden
per JSON Pointer through :attr:`ComparisonPolicy.field_modes`. Derivation is total:
every mode follows from the schema alone, so ``field_modes`` refines rather than enables
per-field treatment.

A declaration is read through its local ``$ref`` (``#/$defs/...``, any ``#`` JSON
Pointer into the root schema): a pydantic-generated schema declares most of its fields
that way. A reference to another document is not followed, nor is one that leads back
to itself.

Scoring an ``object_list`` lives in :mod:`field_accuracy`, which pairs its entries first.
This module owns the scorers that compare one value to one value.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qsl, unquote, urlparse

from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.families.prose.metrics import prose_token_prf
from app.evaluation_metrics.quality.families.structured_json.pointer import child_pointer
from app.evaluation_metrics.quality.normalization.policies import (
    NORMALIZERS,
    ArraySemantics,
    ComparisonPolicy,
    FieldMode,
)
from app.evaluation_metrics.quality.primitives.prf import precision_recall_f1
from app.evaluation_metrics.quality.primitives.sets import set_diagnostics
from app.evaluation_metrics.quality.results import MetricStatus

# ``FieldMode`` is re-exported, not redefined: it lives in ``normalization/policies.py``
# because ``ComparisonPolicy`` validates against it, and ``normalization`` must not
# import from ``families``.
__all__ = [
    "FieldMode",
    "FieldScore",
    "FieldStatus",
    "derive_mode",
    "entry_schema",
    "nonnull_schema",
    "resolve_mode",
    "resolve_schema",
    "score_value",
]

FieldStatus = Literal["matched", "missing", "extra", "excluded"]

_URI_FORMATS = frozenset({"uri", "iri", "uri-reference", "iri-reference", "url"})

# The ``$ref`` hops one declaration may take before naming a shape: a chain of
# references to references is short in any real schema, and a longer one is a cycle.
_MAX_REF_HOPS = 32

# Keywords that describe a declaration without constraining it: two branches declaring a
# property alike may still word it differently.
_ANNOTATIONS = frozenset(
    {"title", "description", "examples", "default", "$comment", "deprecated", "readOnly", "writeOnly"}
)


@dataclass(frozen=True, slots=True)
class FieldScore:
    """One field's contribution to the aggregate.

    ``score is None`` means the field contributed nothing in either direction: excluded,
    or empty on one side only. ``0.0`` is a wrong, missing or extra field.
    """

    pointer: str
    mode: FieldMode
    score: float | None
    weight: float
    status: FieldStatus


def derive_mode(
    field_schema: Mapping[str, Any] | None, root: Mapping[str, Any] | None = None
) -> FieldMode:
    """Derive a field's scoring mode from its own schema declaration.

    Args:
        field_schema: The subschema declared for this field, or ``None`` when the field
            is absent from the schema (an unexpected field in the answer).
        root: The whole schema, against which a local ``$ref`` resolves. Without it a
            reference names no shape.

    Returns:
        The derived :class:`FieldMode`, falling back to the strict :attr:`FieldMode.exact`.
    """
    field_schema = resolve_schema(field_schema, root)
    if not isinstance(field_schema, Mapping):
        return FieldMode.exact

    declared_type = _declared_type(field_schema.get("type"))

    if (field_schema.get("enum") is not None or "const" in field_schema) and declared_type != "array":
        # A closed set of values, or the one value a `const` fixes.
        return FieldMode.enum

    if declared_type == "array":
        items = resolve_schema(field_schema.get("items"), root)
        if "prefixItems" in field_schema or isinstance(items, list):
            # A tuple: each position means something.
            return FieldMode.exact
        if _is_object_schema(items) or _object_branches(items, root) is not None:
            # Objects, of one shape or of several (an ``anyOf`` / ``oneOf`` of objects).
            return FieldMode.object_list
        # Plain values, closed or not, are a set: order is rarely meant.
        return FieldMode.enum_set

    if declared_type == "string":
        if field_schema.get("format") in _URI_FORMATS:
            return FieldMode.resource_address
        if field_schema.get("pattern") is None:
            return FieldMode.free_text

    return FieldMode.exact


def nonnull_schema(schema: object) -> object:
    """``anyOf`` / ``oneOf`` of one schema and ``{"type": "null"}`` (pydantic's optional
    field) is that schema."""
    if not isinstance(schema, Mapping):
        return schema
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            kept = [branch for branch in branches if not _is_null_schema(branch)]
            if len(kept) == 1 and len(kept) < len(branches) and isinstance(kept[0], Mapping):
                return kept[0]
    return schema


def _is_null_schema(schema: object) -> bool:
    """A declaration of ``null`` alone: ``{"type": "null"}``, ``{"type": ["null"]}``,
    ``{"const": null}`` or ``{"enum": [null]}``."""
    if not isinstance(schema, Mapping):
        return False
    return (
        schema.get("type") in ("null", ["null"])
        or ("const" in schema and schema["const"] is None)
        or schema.get("enum") == [None]
    )


def resolve_schema(schema: object, root: Mapping[str, Any] | None) -> object:
    """The shape a declaration names: its local ``$ref`` followed, the keywords beside the
    reference kept over the target's, and pydantic's optional (``anyOf`` of one schema
    and ``null``) unwrapped.

    A reference to another document, or that does not resolve, names no shape, nor does
    one that leads back to itself: only the keywords beside it are kept, so the field is
    scored ``exact``.
    """
    seen: set[str] = set()
    for _ in range(_MAX_REF_HOPS):
        schema = nonnull_schema(schema)
        if not isinstance(schema, Mapping):
            return schema
        reference = schema.get("$ref")
        if not isinstance(reference, str):
            return schema
        siblings = {key: value for key, value in schema.items() if key != "$ref"}
        target = _local_target(root, reference) if reference not in seen else None
        if not isinstance(target, Mapping):
            return siblings
        seen.add(reference)
        schema = {**target, **siblings}
    return {}


def _local_target(root: Mapping[str, Any] | None, reference: str) -> object:
    """What a ``#`` JSON Pointer reference names in ``root``, or ``None``."""
    if root is None or not reference.startswith("#"):
        return None
    pointer = unquote(reference[1:])
    if not pointer:
        return root
    if not pointer.startswith("/"):
        # A named anchor (``#name``): not a pointer.
        return None
    node: object = root
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            node = node.get(token)
        elif isinstance(node, list) and token.isascii() and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return None
    return node


def entry_schema(
    items: object, entry: object, root: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """The schema one entry of a list of objects is scored under.

    Items of one shape are that shape. Items of several (an ``anyOf`` / ``oneOf`` of
    objects) are the branch the entry matches, read from the values the branches fix
    differently (``const``, ``enum``), as a discriminator does; when no branch or several
    match, the properties every branch declares alike. An object field of several shapes
    is read the same way, from its value.
    """
    items = resolve_schema(items, root)
    branches = _object_branches(items, root)
    if branches is None:
        return items if isinstance(items, Mapping) else None
    discriminators = _discriminators(branches, root)
    matching = [
        branch for branch in branches if _admits(branch, entry, discriminators, root)
    ]
    if len(matching) == 1:
        return matching[0]
    return _common_properties(branches, root)


def _object_branches(
    schema: object, root: Mapping[str, Any] | None
) -> list[Mapping[str, Any]] | None:
    """The branches of an ``anyOf`` / ``oneOf`` whose every non-null branch is an object
    schema, resolved; ``None`` for any other schema."""
    if not isinstance(schema, Mapping):
        return None
    for keyword in ("anyOf", "oneOf"):
        declared = schema.get(keyword)
        if not isinstance(declared, list):
            continue
        branches = [resolve_schema(branch, root) for branch in declared]
        kept = [branch for branch in branches if not _is_null_schema(branch)]
        if kept and all(_is_object_schema(branch) for branch in kept):
            return [branch for branch in kept if isinstance(branch, Mapping)]
    return None


def _is_object_schema(schema: object) -> bool:
    return isinstance(schema, Mapping) and (
        _declared_type(schema.get("type")) == "object" or "properties" in schema
    )


def _fixed_values(
    branch: Mapping[str, Any], key: str, root: Mapping[str, Any] | None
) -> list[Any] | None:
    """The values a branch's ``key`` may take (its ``const``, its ``enum``), or ``None``
    when it fixes none."""
    properties = branch.get("properties")
    declared = resolve_schema(properties.get(key), root) if isinstance(properties, Mapping) else None
    if not isinstance(declared, Mapping):
        return None
    if "const" in declared:
        return [declared["const"]]
    allowed = declared.get("enum")
    return list(allowed) if isinstance(allowed, list) else None


def _discriminators(
    branches: Sequence[Mapping[str, Any]], root: Mapping[str, Any] | None
) -> list[str]:
    """The properties whose fixed values tell the branches apart: one an enum every
    branch shares (an operator, a mode) tells nothing, and an entry outside it must not
    match no branch for that."""
    keys = sorted(
        {
            key
            for branch in branches
            if isinstance(branch.get("properties"), Mapping)
            for key in branch["properties"]
        }
    )
    telling: list[str] = []
    for key in keys:
        fixed = [_fixed_values(branch, key, root) for branch in branches]
        forms = {None if values is None else _canon_set(values) for values in fixed}
        if len(forms) > 1 and forms != {None}:
            telling.append(key)
    return telling


def _canon_set(values: Sequence[Any]) -> frozenset[str]:
    return frozenset(json.dumps(value, sort_keys=True) for value in values)


def _admits(
    branch: Mapping[str, Any],
    entry: object,
    discriminators: Sequence[str],
    root: Mapping[str, Any] | None,
) -> bool:
    """Whether ``entry`` holds no value the branch's discriminators rule out."""
    if not isinstance(entry, Mapping):
        return False
    for key in discriminators:
        if key not in entry:
            continue
        allowed = _fixed_values(branch, key, root)
        if allowed is not None and entry[key] not in allowed:
            return False
    return True


def _common_properties(
    branches: Sequence[Mapping[str, Any]], root: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    """An object of the properties every branch declares; one declared differently by
    two branches names no shape (``exact``)."""
    declared = [branch.get("properties") for branch in branches]
    tables = [table if isinstance(table, Mapping) else {} for table in declared]
    shared = set(tables[0]).intersection(*tables[1:]) if tables else set()
    properties: dict[str, Any] = {}
    for key in sorted(shared):
        shapes = [resolve_schema(table[key], root) for table in tables]
        alike = all(_constraints(shape) == _constraints(shapes[0]) for shape in shapes)
        properties[key] = shapes[0] if alike else {}
    return {"type": "object", "properties": properties}


def _constraints(schema: object) -> object:
    """``schema`` without its annotation keywords, at every level."""
    if isinstance(schema, Mapping):
        return {
            key: _constraints(value) for key, value in schema.items() if key not in _ANNOTATIONS
        }
    if isinstance(schema, list):
        return [_constraints(item) for item in schema]
    return schema


def _declared_type(declared: object) -> object:
    """A nullable type (``["string", "null"]``) is its non-null type."""
    if isinstance(declared, list):
        kinds = [kind for kind in declared if kind != "null"]
        return kinds[0] if len(kinds) == 1 else None
    return declared


def resolve_mode(
    pointer: str,
    field_schema: Mapping[str, Any] | None,
    policy: ComparisonPolicy,
    root: Mapping[str, Any] | None = None,
) -> FieldMode:
    """Resolve the mode for ``pointer``: the author's override, else derivation.

    Args:
        pointer: The field's JSON Pointer.
        field_schema: The subschema declared for the field, if any.
        policy: The comparison policy carrying any per-pointer override.
        root: The whole schema, against which a local ``$ref`` resolves.

    Returns:
        The effective :class:`FieldMode` for this field.
    """
    override = policy.field_mode(pointer)
    if override is not None:
        return override
    derived = derive_mode(field_schema, root)
    if derived is FieldMode.free_text and policy.prose_policy is None:
        # Every plain string field derives `free_text`, so refusing to score without a
        # prose policy would fail every caller holding a bare `ComparisonPolicy()`. The
        # strict `exact` fallback computes no prose number, so no per-field prose score
        # sits beside a whole-answer one under different normalization. An explicit
        # `free_text` override with no prose policy is refused by `ComparisonPolicy`.
        return FieldMode.exact
    return derived


def require_prose_policy(pointer: str, policy: ComparisonPolicy) -> None:
    """Refuse to score a ``free_text`` field with no prose policy.

    The schema-derived half of ``ComparisonPolicy``'s free-text check, which cannot see
    the schema. Defaulting one silently would put a per-field similarity beside a
    whole-answer similarity computed under different normalization rules.

    Raises:
        ConfigurationError: If ``policy.prose_policy`` is unset.
    """
    if policy.prose_policy is None:
        raise ConfigurationError(
            f"field '{pointer or '/'}' resolves to free_text but the comparison policy "
            "carries no prose_policy. Supply the same ProseNormalizationPolicy the "
            "whole-answer prose metrics use, so the two scores stay comparable."
        )


def score_value(
    mode: FieldMode,
    reference: object,
    prediction: object,
    pointer: str,
    policy: ComparisonPolicy,
) -> float:
    """Score one predicted value against one reference value under ``mode``.

    Args:
        mode: The resolved scoring mode. ``object_list`` is not handled here; the
            orchestrator pairs entries first and scores their fields through this
            function.
        reference: The reference value.
        prediction: The predicted value.
        pointer: The field's JSON Pointer, used for per-pointer policy lookups.
        policy: The comparison policy.

    Returns:
        A score in ``[0.0, 1.0]``.
    """
    if mode is FieldMode.enum:
        return 1.0 if _exact_equal(reference, prediction, pointer, policy) else 0.0

    if mode is FieldMode.enum_set:
        if _is_plain_list(reference) and _is_plain_list(prediction):
            return _score_enum_set(reference, prediction)
        return 1.0 if _exact_equal(reference, prediction, pointer, policy) else 0.0

    if mode is FieldMode.free_text:
        return _score_free_text(reference, prediction, pointer, policy)

    if mode is FieldMode.resource_address:
        return _score_resource_address(reference, prediction)

    if _is_plain_list(reference) and _is_plain_list(prediction):
        return _score_exact_array(reference, prediction, pointer, policy)

    return 1.0 if _exact_equal(reference, prediction, pointer, policy) else 0.0


def _score_exact_array(
    reference: Sequence[Any],
    prediction: Sequence[Any],
    pointer: str,
    policy: ComparisonPolicy,
) -> float:
    """Compare two arrays under the pointer's declared ``array_semantics``.

    All-or-nothing: an array reaches ``exact`` by an override or as a tuple.
    """
    mode = policy.array_mode(pointer)
    if mode is ArraySemantics.set:
        return 1.0 if {_canon(v) for v in reference} == {_canon(v) for v in prediction} else 0.0
    if mode is ArraySemantics.multiset:
        return (
            1.0
            if sorted(_canon(v) for v in reference) == sorted(_canon(v) for v in prediction)
            else 0.0
        )
    if len(reference) != len(prediction):
        return 0.0
    return (
        1.0
        if all(
            _exact_equal(r, p, child_pointer(pointer, index), policy)
            for index, (r, p) in enumerate(zip(reference, prediction, strict=True))
        )
        else 0.0
    )


def _is_plain_list(value: object) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _canon(value: object) -> str:
    """Canonical, order-stable string form used to compare array elements."""
    return json.dumps(_json_numbers(value), sort_keys=True, ensure_ascii=False)


def _json_numbers(value: object) -> object:
    """JSON has one number type: ``1.0`` is ``1``."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_json_numbers(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _json_numbers(item) for key, item in value.items()}
    return value


def _score_enum_set(reference: Sequence[Any], prediction: Sequence[Any]) -> float:
    """Set-overlap F1, order-insensitive regardless of ``array_semantics``.

    An author who needs order on such a field overrides it to ``exact``.
    """
    ref_items = _as_canonical_set(reference)
    pred_items = _as_canonical_set(prediction)
    if not ref_items and not pred_items:
        return 1.0
    diagnostics = set_diagnostics(ref_items, pred_items)
    _, _, f1 = precision_recall_f1(
        tp=diagnostics.tp, fp=diagnostics.fp, fn=diagnostics.fn
    )
    return f1


def _score_free_text(
    reference: object, prediction: object, pointer: str, policy: ComparisonPolicy
) -> float:
    """Token-level P/R/F1 under the policy's own prose policy.

    ``prose_token_prf`` rather than ``prose_rouge_l``: ROUGE-L credits the longest common
    subsequence, so it penalises the faithful reordering this mode exists to credit (on
    one measured paraphrase, 47.1 against 23.5).
    """
    require_prose_policy(pointer, policy)
    if not isinstance(reference, str) or not isinstance(prediction, str):
        return 1.0 if _exact_equal(reference, prediction, pointer, policy) else 0.0
    if not policy.prose_policy.tokenize(reference) and not policy.prose_policy.tokenize(prediction):
        # No token on either side: an empty answer, correct.
        return 1.0
    result = prose_token_prf(reference, prediction, policy.prose_policy)
    if result.status is not MetricStatus.scored or result.value is None:
        return 0.0
    return result.value / 100.0


def _score_resource_address(reference: object, prediction: object) -> float:
    """Score a resource address: well-formed and naming the reference's resource.

    1. Well-formedness: ``prediction`` must parse as an address. Reachability is never
       tested, so the score does not depend on the network.
    2. Comparison: it must name the same resource as ``reference`` after folding
       scheme/host case, a default port, one trailing slash on a non-root path and
       query parameter order. Path segments and query values stay significant, so two
       valid but distinct addresses never tie.
    """
    if not _is_well_formed_address(prediction):
        return 0.0
    if not isinstance(reference, str) or not _is_well_formed_address(reference):
        # No comparable reference address to normalize against: strict equality.
        return 1.0 if reference == prediction else 0.0
    return 1.0 if _normalized_address(reference) == _normalized_address(prediction) else 0.0


def _is_well_formed_address(value: object) -> bool:
    """Whether ``value`` parses as a resource address; reachability is never tested."""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme and parsed.netloc:
        return True
    # A relative URI reference is legal for `uri-reference`; require it to at least carry
    # a path and no whitespace, so an arbitrary sentence does not read as an address.
    return bool(parsed.path) and not any(ch.isspace() for ch in value)


_DEFAULT_PORTS: dict[str, str] = {"http": ":80", "https": ":443"}


def _normalized_address(value: str) -> str:
    """Fold representational differences that do not change the address.

    Only scheme/host case, an explicit default port, one trailing path slash and query
    parameter order are folded; ``.../T1566/`` and ``.../T1566/002/`` stay distinct.
    """
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    port_suffix = _DEFAULT_PORTS.get(scheme)
    if port_suffix and netloc.endswith(port_suffix):
        netloc = netloc[: -len(port_suffix)]
    path = parsed.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = "&".join(sorted(f"{k}={v}" for k, v in parse_qsl(parsed.query, keep_blank_values=True)))
    return f"{scheme}://{netloc}{path}?{query}#{parsed.fragment}"


def _as_canonical_set(items: Sequence[Any]) -> set[str]:
    """The elements in their canonical JSON form: ``"1"`` is not ``1``, and objects compare
    by content, not key order."""
    return {_canon(item) for item in items}


def _exact_equal(
    reference: object, prediction: object, pointer: str, policy: ComparisonPolicy
) -> bool:
    """Strict equality under the policy's per-pointer normalization rules.

    ``normalization``, ``type_coercion`` and ``numeric_tolerance`` apply.
    """
    name = policy.normalization.get(pointer)
    if name is not None and isinstance(reference, str) and isinstance(prediction, str):
        normalize = NORMALIZERS[name]
        reference, prediction = normalize(reference), normalize(prediction)
    if policy.type_coercion.get(pointer, False):
        return str(reference) == str(prediction)
    tolerance = policy.numeric_tolerance.get(pointer)
    if (
        tolerance is not None
        and isinstance(reference, (int, float))
        and isinstance(prediction, (int, float))
        and not isinstance(reference, bool)
        and not isinstance(prediction, bool)
    ):
        return math.isclose(reference, prediction, abs_tol=tolerance)
    # Preserve JSON type distinctions (e.g. True is not 1).
    if isinstance(reference, bool) != isinstance(prediction, bool):
        return False
    return reference == prediction
