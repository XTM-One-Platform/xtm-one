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

"""Agent output-schema checks that never fetch a document or run a regex unbounded.

An agent's ``output_schema`` is written by its owner - any user who can create
an agent - and the platform is the only enforcer of what it says. Two checks
run it against a model's output: the structured-output parse
(``structured_output.validate_and_parse``) and the ``finalize_on_tool``
terminal check (``agent_loop._terminal_tool_decision``).

Both validate through ``app.services.schema_guard``, the mechanism tool-argument
validation uses too: nothing is retrieved, the class the check chose
holds for the whole document, and the schema's regexes still decide, on the
bounded engine. A pattern that gives no verdict in time, or none ``re`` is sure
to give, and a subschema behind a reference to another document, are left
unjudged with a warning: the output passes that one constraint instead of the
check failing or holding the worker. Under ``not`` / ``oneOf`` / ``if`` /
``disallow`` / ``contains``, and in ``unevaluatedItems`` / ``unevaluatedProperties``
(which re-run those to find what was evaluated), reading it as met could refuse
a valid output, so there the whole keyword is left unjudged.

The Security Audit asks :func:`references_another_document` whether either
check would meet such a reference, of the validators the checks build.
"""

from __future__ import annotations

import functools
import itertools
import json
import logging
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from typing import Any

import jsonschema
from jsonschema.exceptions import ValidationError

from app.services import schema_guard

logger = logging.getLogger(__name__)

#: Regex time one output check may spend, all patterns together (CPU time,
#: ``schema_guard.regex_stopwatch``: a loaded host's pauses spend none of it).
OUTPUT_SCHEMA_REGEX_BUDGET_SECONDS = 1.0
#: Regex time one search may spend, so one pattern cannot starve the others.
OUTPUT_SCHEMA_PATTERN_TIMEOUT_SECONDS = 0.1
#: What one search may take before it spends the budget: about what checking
#: any string of the output costs, so the many quick searches of a large output
#: never exhaust it, while a slow pattern does.
OUTPUT_SCHEMA_SEARCH_ALLOWANCE_SECONDS = 0.0001

# The regex time the check in progress has left. Only a search's time past its
# allowance spends it, never the rest of the output's validation.
_regex_time_left: ContextVar[list[float] | None] = ContextVar("_output_schema_regex_time_left", default=None)
# Inside a keyword where reading an unjudged constraint as met can tighten, it raises instead.
_strict: ContextVar[bool] = ContextVar("_output_schema_strict", default=False)
_WARNED: set[str] = set()
_MAX_WARNED = 256

# ``contains`` for ``maxContains``: an item read as matching can exceed it.
_NON_MONOTONE_KEYWORDS = ("not", "oneOf", "if", "disallow", "contains")
# They re-run if / then / else / anyOf / oneOf to find what was evaluated: the
# branch an unjudged constraint picks can evaluate less.
_REEVALUATING_KEYWORDS = ("unevaluatedItems", "unevaluatedProperties")


def _warn_once(what: str, why: str) -> None:
    key = f"{why}:{what[:120]}"
    if key in _WARNED or len(_WARNED) >= _MAX_WARNED:
        return
    _WARNED.add(key)
    logger.warning("Agent output-schema check left a constraint unjudged: %s (%r)", why, what[:120])


class _UnjudgedError(Exception):
    """A constraint with no verdict, met where reading it as satisfied would tighten."""


def _unjudged(what: str, why: str) -> None:
    _warn_once(what, why)
    if _strict.get():
        raise _UnjudgedError(why)


def _search(pattern: Any, text: str) -> bool | None:
    """Does *pattern* match *text*? ``None`` when no verdict came within its bound."""
    time_left = _regex_time_left.get()
    budget = OUTPUT_SCHEMA_REGEX_BUDGET_SECONDS if time_left is None else time_left[0]
    stopwatch = schema_guard.regex_stopwatch()
    # The allowance is for a search that ends: one cut at its bound is charged all
    # it took, or the last fraction of a millisecond left would never run out and
    # each further search of a pattern that backtracks would spend it uncharged.
    allowance = OUTPUT_SCHEMA_SEARCH_ALLOWANCE_SECONDS
    try:
        return schema_guard.bounded_search(
            pattern, text, remaining=min(budget, OUTPUT_SCHEMA_PATTERN_TIMEOUT_SECONDS)
        )
    except schema_guard.PatternUnjudgedError as exc:
        if exc.why == schema_guard.TIMED_OUT:
            allowance = 0.0
        _unjudged(str(pattern), exc.why)
    except schema_guard.REGEX_ERROR:
        _unjudged(str(pattern), "pattern the bounded engine cannot compile")
    finally:
        if time_left is not None:
            time_left[0] -= max(0.0, stopwatch.charged() - allowance)
    return None


def _unless_retrieval_refused(base: Callable[..., Any]) -> Callable[..., Any]:
    def keyword(validator: Any, value: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
        try:
            yield from base(validator, value, instance, schema) or ()
        except _UnjudgedError:
            raise
        except Exception as exc:
            if not schema_guard.retrieval_refused(exc):
                raise
            _unjudged(str(value), "reference to another document, never retrieved")

    return keyword


def _unjudged_as_a_whole(base: Callable[..., Any]) -> Callable[..., Any]:
    # A constraint without a verdict inside leaves this whole keyword unjudged:
    # met at the top level, raised again inside an enclosing strict keyword.
    def keyword(validator: Any, value: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
        enclosing = _strict.get()
        token = _strict.set(True)
        try:
            errors = list(base(validator, value, instance, schema) or ())
        except _UnjudgedError:
            if enclosing:
                raise
            errors = []
        finally:
            _strict.reset(token)
        yield from errors

    return keyword


_CLASSES: dict[tuple[type, bool], type] = {}


def _output_class(base: type, holds_pattern_properties: bool) -> type:
    key = (base, holds_pattern_properties)
    if key not in _CLASSES:
        keywords = schema_guard.regex_keywords(
            base,
            judge=_search,
            no_verdict=_unjudged,
            holds_pattern_properties=holds_pattern_properties,
            judge_unevaluated_properties=True,
        )
        for name in schema_guard.REF_KEYWORDS:
            if name in base.VALIDATORS:
                keywords[name] = _unless_retrieval_refused(base.VALIDATORS[name])
        for name in _REEVALUATING_KEYWORDS:
            if name in base.VALIDATORS:
                keywords[name] = _unjudged_as_a_whole(_unless_retrieval_refused(keywords.get(name, base.VALIDATORS[name])))
        for name in _NON_MONOTONE_KEYWORDS:
            if name in base.VALIDATORS:
                keywords[name] = _unjudged_as_a_whole(base.VALIDATORS[name])
        _CLASSES[key] = schema_guard.extend_pinned(
            base, keywords, functools.partial(_output_class, holds_pattern_properties=holds_pattern_properties)
        )
    return _CLASSES[key]


def output_schema_validator(schema: Any, base: type) -> Any:
    """A *base* validator for *schema* whose regexes are bounded and that fetches nothing."""
    holds_pattern_properties = schema_guard.take_census(schema).pattern_properties
    return _output_class(base, holds_pattern_properties)(schema, registry=schema_guard.LOCAL_SCHEMA_REFS_ONLY)


def output_schema_errors(validator: Any, instance: Any, *, limit: int | None = None) -> list[ValidationError]:
    """The errors *validator* finds in *instance* (the first *limit* of them), its regexes within one budget."""
    token = _regex_time_left.set([OUTPUT_SCHEMA_REGEX_BUDGET_SECONDS])
    try:
        return list(itertools.islice(validator.iter_errors(instance), limit))
    finally:
        _regex_time_left.reset(token)


# JSON Schema ``type`` keywords that identify a *raw* JSON Schema (as opposed
# to a provider-native wrapper). ``type`` may also be a list for union types
# (e.g. ``["string", "null"]``), which is unhashable, so membership is tested
# via set intersection rather than a bare ``in {...}`` check that would raise
# ``TypeError: unhashable type: 'list'`` and abort the agent loop.
_RAW_JSON_SCHEMA_TYPES = frozenset({"object", "array", "string", "number", "integer", "boolean", "null"})


def raw_json_schema(schema: dict[str, Any] | None) -> dict[str, Any] | None:
    """The raw JSON Schema the ``finalize_on_tool`` check validates, from either a raw or a provider-native config."""
    if not isinstance(schema, dict) or not schema:
        return None
    # ``type`` is normally a string but may be a list for JSON Schema union
    # types; normalise to a set of candidate type names so the membership test
    # never receives an unhashable value.
    raw_type = schema.get("type")
    if isinstance(raw_type, str):
        type_names: set[str] = {raw_type}
    elif isinstance(raw_type, list):
        type_names = {t for t in raw_type if isinstance(t, str)}
    else:
        type_names = set()
    if isinstance(schema.get("properties"), dict) or (type_names & _RAW_JSON_SCHEMA_TYPES):
        return schema

    openai_schema = schema.get("json_schema")
    if isinstance(openai_schema, dict) and isinstance(openai_schema.get("schema"), dict):
        return openai_schema["schema"]

    anthropic_schema = schema.get("schema")
    if raw_type == "json_schema" and isinstance(anthropic_schema, dict):
        return anthropic_schema

    gemini_schema = schema.get("response_schema")
    if isinstance(gemini_schema, dict):
        return gemini_schema

    try:
        bedrock_schema = schema["textFormat"]["structure"]["jsonSchema"]["schema"]
    except (KeyError, TypeError):
        return None
    if isinstance(bedrock_schema, str):
        try:
            parsed = json.loads(bedrock_schema)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return bedrock_schema if isinstance(bedrock_schema, dict) else None


def references_another_document(output_schema: Any) -> bool:
    """Would an output check meet a reference to another document, never retrieved, in *output_schema*?

    Each check is asked through the validator it builds: the structured-output
    parse reads the stored schema as draft 7, ``finalize_on_tool`` the JSON
    Schema :func:`raw_json_schema` finds in it, in its own dialect.
    :func:`schema_guard.refused_references` then names what that validator's
    registry would refuse, so a reference the validator resolves itself (a
    ``#`` pointer, an anchor, the schema's own or a nested ``$id``, a bundled
    meta-schema) never counts, nor one where no validator looks. When a walk
    cannot finish or a validator cannot be built, a reference outside ``#``
    anywhere counts.
    """
    readings: list[tuple[Any, type | None]] = [(output_schema, jsonschema.Draft7Validator)]
    terminal = raw_json_schema(output_schema)
    if terminal is not None:
        readings.append((terminal, None))
    for schema, base in readings:
        try:
            base = base or jsonschema.validators.validator_for(schema, default=jsonschema.Draft202012Validator)
            walk = schema_guard.refused_references(output_schema_validator(schema, base))
        except Exception:
            walk = None
        if walk is None or not walk.complete:
            if schema_guard.take_census(schema).remote_ref:
                return True
        elif walk.refused:
            return True
    return False
