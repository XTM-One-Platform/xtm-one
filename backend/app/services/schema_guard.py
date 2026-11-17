# SPDX-License-Identifier: Apache-2.0

"""Schema checks that never fetch a document or run a regex unbounded.

Two places check a value against a JSON Schema the platform did not write:
tool-argument validation and coercion (``app.tools.agent_loop``: schemas from
MCP servers, custom tools and imported bundles) and the agent output-schema
checks (``app.services.output_schema_validation``: schemas written by an
agent's owner). A stock ``jsonschema`` validator is unsafe there twice over:
its registry retrieves every non-local ``$ref`` / ``$dynamicRef`` with
``urllib`` (``http(s)``, ``file`` and ``data`` URLs, no timeout, on the event
loop), and it runs ``pattern`` / ``patternProperties`` on the backtracking
``re`` with no time bound.

What every such check needs lives here, once: the registry that retrieves
nothing, the bounded pattern search, the regex keywords that ask it, the census
of what a schema holds, the class pin that keeps a whole document on those
keywords, and the walk that finds, without an instance, the references a
validator would refuse to resolve. What a check does with a pattern is the ``judge`` it passes:
tool-argument pre-validation never runs one (the tool validates its own
input), argument coercion and the output-schema checks judge them within a
time bound, each with its own reading of a pattern that gets no verdict.

The bounded engine is ``regex``, which reads a few constructs ``re`` accepts
differently: a ``{`` that opens no repetition (``{id}``, the fuzzy ``{e}``), a
``[`` inside a set (the POSIX ``[[:alpha:]]``), ``\\w`` / ``\\d`` / ``\\s`` on some
characters, case-insensitive ``İ`` / ``ı``. The search escapes the first two so
``regex`` reads the pattern as ``re`` does, and gives no verdict where the
others could make the two engines disagree: a verdict is always the one ``re``
would give, or none.

A check's regex budget is charged on :func:`regex_stopwatch`, the CPU time of
the thread that searches, never the wall-clock time a loaded host adds.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, NoReturn

import attrs
import jsonschema
from jsonschema import _legacy_keywords
from jsonschema._utils import extras_msg
from jsonschema.exceptions import ValidationError
from referencing import Registry, Specification
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import lookup_recursive_ref, specification_with

from app.core.cpu_time import CpuStopwatch, thread_search_time

try:  # arrives with tiktoken, a pinned dependency
    import regex as bounded_regex
except ImportError:  # pragma: no cover
    bounded_regex = None

#: What the bounded engine raises for a pattern it cannot compile (never raised without it).
REGEX_ERROR: type[BaseException] | tuple[()] = bounded_regex.error if bounded_regex is not None else ()

REF_KEYWORDS = ("$ref", "$dynamicRef", "$recursiveRef")

#: Why a pattern got no verdict (:attr:`PatternUnjudgedError.why`).
NO_TIME = "no regex time left"
TIMED_OUT = "pattern did not finish in time"
READ_DIFFERENTLY = "the bounded engine may not read this pattern as re does"


# ---------------------------------------------------------------------------
# References: nothing is ever retrieved
# ---------------------------------------------------------------------------


def _refuse_retrieval(uri: str) -> NoReturn:
    raise NoSuchResource(ref=uri)


#: Resolves local ``#`` references (and the bundled meta-schemas) only.
LOCAL_SCHEMA_REFS_ONLY = Registry(retrieve=_refuse_retrieval)


def retrieval_refused(exc: BaseException | None) -> bool:
    """Was *exc* raised because :data:`LOCAL_SCHEMA_REFS_ONLY` refused a document?

    Only the refusal raises ``NoSuchResource``; a local pointer that misses is a
    ``PointerToNowhere``. The resolver wraps it, so the chain is walked.
    """
    seen = 0
    while exc is not None and seen < 16:
        if isinstance(exc, NoSuchResource):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


# ---------------------------------------------------------------------------
# The bounded search, read as re reads the pattern
# ---------------------------------------------------------------------------


class PatternUnjudgedError(Exception):
    """A pattern gave no verdict within its bound, or none ``re`` is sure to give."""

    def __init__(self, pattern: Any, why: str) -> None:
        super().__init__(pattern, why)
        self.pattern = pattern
        self.why = why


# The two engines agree on these characters for every class and case folding.
_PLAIN = frozenset(chr(code) for code in range(0x20, 0x7F)) | frozenset("\t\n\r\x0b\x0c")
# Past this many distinct characters to check, a text gets no verdict rather
# than an unbounded pre-check on the event loop.
_MAX_CHECKED_CHARACTERS = 512
# What follows a ``{`` that ``re`` reads as a repetition (``{}`` excepted): ASCII digits only.
_REPETITION_TAIL = re.compile(r"[0-9]*(?:,[0-9]*)?\}")
_FLAG_GROUP = re.compile(r"\(\?([aiLmsux]*)(?:-([imsx]*))?[:)]")
# ``\b`` / ``\B`` are boundaries between ``\w`` and the rest.
_CLASS_OF_ESCAPE = {"w": "w", "W": "w", "b": "w", "B": "w", "d": "d", "D": "d", "s": "s", "S": "s"}


@functools.cache
def _class_probes(cls: str, ascii_only: bool) -> tuple[Any, Any]:
    return (
        re.compile(f"\\{cls}", re.ASCII if ascii_only else 0),
        bounded_regex.compile(f"\\{cls}", bounded_regex.ASCII if ascii_only else 0),
    )


@functools.lru_cache(maxsize=8192)
def _class_agrees(cls: str, char: str, ascii_only: bool) -> bool:
    with_re, with_regex = _class_probes(cls, ascii_only)
    return (with_re.fullmatch(char) is None) == (with_regex.fullmatch(char) is None)


def _case_mapped(char: str) -> set[str]:
    mapped: set[str] = set()
    for form in (char.lower(), char.upper(), char.title(), char.casefold()):
        mapped.update(form)
    return mapped


@functools.lru_cache(maxsize=8192)
def _case_agrees(char: str, ascii_only: bool) -> bool:
    """Do both engines equate *char* with the same characters when case is ignored?

    The characters an engine can equate with *char* are its case mappings and
    theirs (``ı`` uppercases to ``I``, which ``re`` then equates with ``i``).
    """
    if unicodedata.category(char) == "Cn":
        return False  # unassigned for Python: the engines' Unicode versions may disagree on it
    partners = _case_mapped(char)
    for mapped in list(partners):
        partners |= _case_mapped(mapped)
    partners.discard(char)
    re_flags = re.IGNORECASE | (re.ASCII if ascii_only else 0)
    regex_flags = bounded_regex.IGNORECASE | (bounded_regex.ASCII if ascii_only else 0)
    for partner in partners:
        for one, other in ((char, partner), (partner, char)):
            with_re = re.fullmatch(re.escape(one), other, re_flags) is None
            with_regex = bounded_regex.fullmatch(bounded_regex.escape(one), other, regex_flags) is None
            if with_re != with_regex:
                return False
    return True


@dataclass(frozen=True)
class _Reading:
    """A pattern as the bounded engine should run it, and what could make it read differently."""

    compiled: Any
    classes: frozenset[str]
    ascii_only: bool
    caseless: bool
    pattern_agrees: bool
    # A scoped ``(?a:...)`` / ``(?u:...)``: the engines disagree on what it does to
    # a class or to case folding, so only plain text can be judged.
    scoped_modes: bool

    def agrees_on(self, text: str) -> bool:
        if not self.pattern_agrees:
            return False
        if not self.classes and not self.caseless:
            return True
        to_check = set(text) - _PLAIN
        if len(to_check) > _MAX_CHECKED_CHARACTERS or (self.scoped_modes and to_check):
            return False
        for char in to_check:
            if any(not _class_agrees(cls, char, self.ascii_only) for cls in self.classes):
                return False
            if self.caseless and not _case_agrees(char, self.ascii_only):
                return False
        return True


@functools.lru_cache(maxsize=1024)
def _read_as_re(pattern: str) -> _Reading | None:
    """*pattern* for the bounded engine, read the way ``re`` reads it; ``None`` if it may not be.

    ``re`` takes a ``{`` that opens no repetition, and a ``[`` inside a set, as
    that character; ``regex`` reads some of them as a fuzzy constraint or a
    POSIX class, or refuses them. Both are escaped. A verbose pattern is not
    rewritten. Raises :data:`REGEX_ERROR` when the bounded engine cannot compile it.
    """
    flags = re.compile(pattern).flags
    if flags & re.VERBOSE:
        return None
    caseless = bool(flags & re.IGNORECASE)
    scoped_modes = False
    classes: set[str] = set()
    out: list[str] = []
    in_set = False
    set_started = False  # a member has been read since the set opened, so ``]`` closes it
    index, end = 0, len(pattern)
    while index < end:
        char = pattern[index]
        if char == "\\":
            follower = pattern[index + 1 : index + 2]
            if follower in _CLASS_OF_ESCAPE and not (in_set and follower == "b"):
                classes.add(_CLASS_OF_ESCAPE[follower])
            stop = index + 2
            if follower == "N" and pattern.startswith("{", stop):
                stop = pattern.index("}", stop) + 1
            out.append(pattern[index:stop])
            set_started = set_started or in_set
            index = stop
        elif in_set:
            if char == "]" and set_started:
                in_set = False
            else:
                set_started = True
            out.append("\\[" if char == "[" else char)
            index += 1
        elif char == "[":
            in_set, set_started = True, False
            index += 1
            if pattern.startswith("^", index):
                index += 1
                out.append("[^")
            else:
                out.append("[")
        elif char == "{":
            tail = _REPETITION_TAIL.match(pattern, index + 1)
            if tail is not None and not pattern.startswith("}", index + 1):
                out.append(pattern[index : tail.end()])
                index = tail.end()
            else:
                out.append("\\{")
                index += 1
        elif pattern.startswith("(?#", index):
            stop = index + 3
            while stop < end and pattern[stop] != ")":
                stop += 2 if pattern[stop] == "\\" else 1
            out.append(pattern[index : stop + 1])
            index = stop + 1
        else:
            group = _FLAG_GROUP.match(pattern, index)
            if group is not None:
                letters = (group.group(1) or "") + (group.group(2) or "")
                if "x" in letters:
                    return None
                caseless = caseless or "i" in letters
                scoped_modes = scoped_modes or (group.group(0).endswith(":") and not set(letters).isdisjoint("aLu"))
            out.append(char)
            index += 1
    ascii_only = bool(flags & re.ASCII)
    in_pattern = set(pattern) - _PLAIN
    pattern_agrees = not caseless or (
        not (scoped_modes and in_pattern) and all(_case_agrees(char, ascii_only) for char in in_pattern)
    )
    return _Reading(
        compiled=bounded_regex.compile("".join(out)),
        classes=frozenset(classes),
        ascii_only=ascii_only,
        caseless=caseless,
        pattern_agrees=pattern_agrees,
        scoped_modes=scoped_modes,
    )


def regex_stopwatch() -> CpuStopwatch:
    """What a check's regex budget is charged on: the CPU time of the thread that searches.

    Start it and read it on that thread, around searches run in one
    synchronous call. ``time.process_time()`` would also count the process's
    other threads, which run while the engine has released the GIL; a garbage
    collection that lands in a search is left out too.
    """
    return CpuStopwatch(thread_search_time)


def bounded_search(pattern: Any, text: str, *, remaining: float) -> bool:
    """Does *pattern* match *text*, judged within *remaining* seconds?

    ``re`` still refuses what it always refused (``re.error`` propagates), then
    the match runs on the ``regex`` engine. :class:`PatternUnjudgedError` when no
    verdict comes in time, or when the engines could disagree on this text. A
    pattern the bounded engine cannot compile raises :data:`REGEX_ERROR`:
    whether that is an error or a pattern with no verdict is the caller's call.

    The engine ends the search itself, on C ``clock()``: the process's CPU
    time on Linux, wall-clock time on Windows, never less than the searching
    thread's own CPU time, so no search spends more CPU than *remaining*.
    """
    re.compile(pattern)
    if bounded_regex is None or remaining <= 0:
        raise PatternUnjudgedError(pattern, NO_TIME)
    if not isinstance(pattern, str):
        return bounded_regex.search(pattern, text, timeout=remaining) is not None
    reading = _read_as_re(pattern)
    if reading is None or not reading.agrees_on(text):
        raise PatternUnjudgedError(pattern, READ_DIFFERENTLY)
    try:
        return reading.compiled.search(text, timeout=remaining) is not None
    except TimeoutError:
        raise PatternUnjudgedError(pattern, TIMED_OUT) from None


# ---------------------------------------------------------------------------
# The regex keywords, and the class pin that keeps a document on them
# ---------------------------------------------------------------------------

#: ``judge(pattern, text)``: the verdict, or ``None`` for none (already reported by the judge).
Judge = Callable[[Any, str], bool | None]
#: ``no_verdict(what, why)``: a keyword gives no verdict at all.
NoVerdict = Callable[[str, str], None]


class _NoPatternVerdictError(Exception):
    """A ``patternProperties`` regex gave no verdict while the evaluated keys were counted."""


def _no_error(errors: Iterator[ValidationError]) -> bool:
    return next(errors, None) is None


def _evaluated_property_keys(
    validator: Any, instance: Any, schema: Any, matches: Callable[[Any, str], bool], draft2019: bool
) -> list[Any]:
    """The keys of *instance* that *schema* evaluates, a ``patternProperties`` regex asked through *matches*.

    jsonschema 4.26's ``find_evaluated_property_keys_by_schema`` (``_utils`` for
    2020-12, ``_legacy_keywords`` for 2019-09) with its one ``re.search`` replaced
    by *matches*; ``tests/test_schema_guard.py`` fails on another jsonschema, so
    the copy is compared again.
    """
    if validator.is_type(schema, "boolean"):
        return []
    evaluated: list[Any] = []

    def follow(resolved: Any) -> None:
        evolved = validator.evolve(schema=resolved.contents, _resolver=resolved.resolver)
        evaluated.extend(_evaluated_property_keys(evolved, instance, resolved.contents, matches, draft2019))

    ref = schema.get("$ref")
    if ref is not None:
        follow(validator._resolver.lookup(ref))
    if draft2019:
        if "$recursiveRef" in schema:
            follow(lookup_recursive_ref(validator._resolver))
        for keyword in ("properties", "additionalProperties", "unevaluatedProperties"):
            if keyword in schema:
                value = schema[keyword]
                if validator.is_type(value, "boolean") and value:
                    evaluated += instance.keys()
                elif validator.is_type(value, "object"):
                    evaluated.extend(key for key in value if key in instance)
    else:
        dynamic_ref = schema.get("$dynamicRef")
        if dynamic_ref is not None:
            follow(validator._resolver.lookup(dynamic_ref))
        properties = schema.get("properties")
        if validator.is_type(properties, "object"):
            evaluated += properties.keys() & instance.keys()
        for keyword in ("additionalProperties", "unevaluatedProperties"):
            if (subschema := schema.get(keyword)) is not None:
                evaluated.extend(key for key, item in instance.items() if _no_error(validator.descend(item, subschema)))
    if "patternProperties" in schema:
        for key in instance:
            for patrn in schema["patternProperties"]:
                if matches(patrn, key):
                    evaluated.append(key)
    if "dependentSchemas" in schema:
        for key, subschema in schema["dependentSchemas"].items():
            if key in instance:
                evaluated += _evaluated_property_keys(validator, instance, subschema, matches, draft2019)
    for keyword in ("allOf", "oneOf", "anyOf"):
        for subschema in schema.get(keyword, []):
            if _no_error(validator.descend(instance, subschema)):
                evaluated += _evaluated_property_keys(validator, instance, subschema, matches, draft2019)
    if "if" in schema:
        if validator.evolve(schema=schema["if"]).is_valid(instance):
            evaluated += _evaluated_property_keys(validator, instance, schema["if"], matches, draft2019)
            if "then" in schema:
                evaluated += _evaluated_property_keys(validator, instance, schema["then"], matches, draft2019)
        elif "else" in schema:
            evaluated += _evaluated_property_keys(validator, instance, schema["else"], matches, draft2019)
    return evaluated


def _judged_unevaluated_properties(base: type, judge: Judge, no_verdict: NoVerdict) -> Callable[..., Any]:
    """*base*'s ``unevaluatedProperties`` with its ``patternProperties`` regexes asked through *judge*."""
    draft2019 = base.VALIDATORS["unevaluatedProperties"] is _legacy_keywords.unevaluatedProperties_draft2019

    def matches(patrn: Any, key: str) -> bool:
        verdict = judge(patrn, key)
        if verdict is None:
            raise _NoPatternVerdictError
        return verdict

    def unevaluated_properties(validator: Any, value: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
        if not validator.is_type(instance, "object"):
            return
        try:
            evaluated = _evaluated_property_keys(validator, instance, schema, matches, draft2019)
        except _NoPatternVerdictError:
            no_verdict("unevaluatedProperties", "a patternProperties regex gave no verdict")
            return
        unevaluated = []
        for key in instance:
            if key not in evaluated:
                # One entry per error, as jsonschema lists them.
                unevaluated.extend(key for _ in validator.descend(instance[key], value, path=key, schema_path=key))
        if unevaluated and value is False:
            error = "Unevaluated properties are not allowed (%s %s unexpected)"
            yield ValidationError(error % extras_msg(sorted(unevaluated, key=str)))
        elif unevaluated:
            error = "Unevaluated properties are not valid under the given schema (%s %s unevaluated and invalid)"
            yield ValidationError(error % extras_msg(unevaluated))

    return unevaluated_properties


def regex_keywords(
    base: type,
    *,
    judge: Judge,
    no_verdict: NoVerdict,
    holds_pattern_properties: bool,
    judge_unevaluated_properties: bool = False,
) -> dict[str, Callable[..., Any]]:
    """The keywords of *base* that evaluate a schema regex, each asking *judge*.

    A string with no verdict meets its ``pattern``; a key with none is not
    matched by ``patternProperties`` and is not additional. A key is additional
    when no ``patternProperties`` regex matches it, each read on its own. A
    ``patternProperties`` that is not a mapping admits every key.
    ``unevaluatedProperties`` matches keys against every ``patternProperties`` it
    can reach with ``re`` itself, so when the schema holds one it gives no
    verdict at all and tells *no_verdict*; with *judge_unevaluated_properties*
    it asks *judge* for those instead, and gives no verdict only when one of
    them gives none.
    """

    def pattern(validator: Any, patrn: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
        if validator.is_type(instance, "string") and judge(patrn, instance) is False:
            yield ValidationError(f"{instance!r} does not match {patrn!r}")

    def pattern_properties(validator: Any, value: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
        if not isinstance(value, dict) or not validator.is_type(instance, "object"):
            return
        for patrn, subschema in value.items():
            for key, item in instance.items():
                if judge(patrn, key):
                    yield from validator.descend(item, subschema, path=key, schema_path=patrn)

    keywords: dict[str, Callable[..., Any]] = {"pattern": pattern, "patternProperties": pattern_properties}

    if "additionalProperties" in base.VALIDATORS:
        stock_additional = base.VALIDATORS["additionalProperties"]

        def additional_properties(validator: Any, value: Any, instance: Any, schema: Any) -> Iterator[ValidationError]:
            patterns = schema.get("patternProperties")
            if not patterns:
                yield from stock_additional(validator, value, instance, schema) or ()
                return
            if not isinstance(patterns, dict) or not validator.is_type(instance, "object"):
                return
            properties = schema.get("properties", {})
            # Each pattern on its own: jsonschema joins them with "|", which fails
            # to compile beside an inline flag and renumbers group references.
            extras = {
                key
                for key in instance
                if key not in properties and all(judge(patrn, key) is False for patrn in patterns)
            }
            if validator.is_type(value, "object"):
                for extra in extras:
                    yield from validator.descend(instance[extra], value, path=extra)
            elif not value and extras:
                verb = "does" if len(extras) == 1 else "do"
                listed = ", ".join(repr(each) for each in sorted(extras))
                regexes = ", ".join(repr(each) for each in sorted(patterns))
                yield ValidationError(f"{listed} {verb} not match any of the regexes: {regexes}")

        keywords["additionalProperties"] = additional_properties

    if holds_pattern_properties and "unevaluatedProperties" in base.VALIDATORS:
        if judge_unevaluated_properties:
            keywords["unevaluatedProperties"] = _judged_unevaluated_properties(base, judge, no_verdict)
        else:

            def unevaluated_properties(validator: Any, value: Any, instance: Any, schema: Any) -> tuple[()]:
                if validator.is_type(instance, "object"):
                    no_verdict("unevaluatedProperties", "unevaluatedProperties beside patternProperties")
                return ()

            keywords["unevaluatedProperties"] = unevaluated_properties
    return keywords


_PINNED_CLASSES: set[type] = set()


def extend_pinned(base: type, keywords: dict[str, Callable[..., Any]], rebuild: Callable[[type], type]) -> type:
    """``jsonschema.validators.extend(base, keywords)``, kept on its kind as it descends.

    ``jsonschema`` picks the class again from a subschema's own ``$schema`` each
    time it descends (``evolve`` calls ``validator_for``), and the stock class it
    picks knows none of *keywords*: its ``pattern`` runs on ``re``, unbounded. A
    subschema naming a dialect is checked by ``rebuild(<that stock class>)``
    instead, so the document keeps its dialects and every keyword override.
    """
    cls = jsonschema.validators.extend(base, keywords)
    stock_evolve = cls.evolve

    def evolve(self: Any, **changes: Any) -> Any:
        evolved = stock_evolve(self, **changes)
        kind = type(evolved)
        if kind in _PINNED_CLASSES:
            return evolved
        fields = {field.alias: getattr(evolved, field.name) for field in attrs.fields(kind) if field.init}
        return rebuild(kind)(**fields)

    cls.evolve = evolve
    _PINNED_CLASSES.add(cls)
    return cls


# ---------------------------------------------------------------------------
# The census: what a schema holds that decides how it can be checked
# ---------------------------------------------------------------------------

# Keywords whose value maps NAMES to subschemas: only a ``$ref`` reads the
# names as keywords.
_SCHEMA_NAME_MAPS = frozenset(
    {
        "properties",
        "patternProperties",
        "$defs",
        "definitions",
        "dependentSchemas",
        "dependentRequired",
        "dependencies",
    }
)
# Keywords whose value is instance data: only a ``$ref`` reads it as a schema.
_SCHEMA_DATA_KEYS = frozenset({"enum", "const", "default", "examples"})
# Under (or, for ``maxContains``, beside) these, ignoring a keyword can tighten.
_SCHEMA_NON_MONOTONE_KEYS = frozenset({"not", "oneOf", "if", "disallow", "maxContains"})


def _holds_patterns(pattern_properties: Any) -> bool:
    # jsonschema joins or iterates the value whatever its type: a list of
    # regexes reaches ``re`` as surely as a mapping.
    return bool(pattern_properties)


@dataclass
class SchemaCensus:
    """What a schema holds that decides how it can be checked."""

    regex: bool = False
    pattern_properties: bool = False
    ref: bool = False
    # A reference to another document rather than to a ``#`` fragment.
    remote_ref: bool = False
    non_monotone: bool = False


def _census_keyword(census: SchemaCensus, key: Any, value: Any) -> None:
    if key == "pattern" and isinstance(value, str):
        census.regex = True
    elif key == "patternProperties" and _holds_patterns(value):
        census.regex = census.pattern_properties = True
    elif key in REF_KEYWORDS and isinstance(value, str):
        census.ref = True
        census.remote_ref = census.remote_ref or not value.startswith("#")
    elif key in _SCHEMA_NON_MONOTONE_KEYS:
        census.non_monotone = True


def _walk_census(census: SchemaCensus, pending: list[Any], ref_targets: list[Any] | None) -> None:
    """Record what the nodes in *pending* hold, however deep they go.

    With *ref_targets*, a node is read the way the validator reads it, and the
    instance data and maps of names met are set aside there. Without, every
    key is a keyword: the way a ``$ref`` pointing into them reads them.
    """
    seen: set[int] = set()
    while pending:
        node = pending.pop()
        if not isinstance(node, (dict, list)) or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, list):
            pending.extend(node)
            continue
        for key, value in node.items():
            _census_keyword(census, key, value)
            if ref_targets is not None and key in _SCHEMA_DATA_KEYS:
                ref_targets.append(value)
            elif ref_targets is not None and key in _SCHEMA_NAME_MAPS and isinstance(value, dict):
                ref_targets.append(value)
                pending.extend(value.values())
            else:
                pending.append(value)


def take_census(schema: Any) -> SchemaCensus:
    census = SchemaCensus()
    ref_targets: list[Any] = []
    _walk_census(census, [schema], ref_targets)
    if census.ref:
        _walk_census(census, ref_targets, None)
    return census


# ---------------------------------------------------------------------------
# The references a validator would refuse to resolve, found without an instance
# ---------------------------------------------------------------------------

#: The keywords whose value holds the subschemas they evaluate: the value
#: itself, a list of them, or (the four maps) a map of names to them. ``then``
#: and ``else`` are evaluated by ``if``, never on their own.
_APPLICATORS = frozenset(
    {
        "additionalItems",
        "additionalProperties",
        "allOf",
        "anyOf",
        "contains",
        "dependencies",
        "dependentSchemas",
        "extends",
        "if",
        "items",
        "not",
        "oneOf",
        "patternProperties",
        "prefixItems",
        "properties",
        "propertyNames",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
)
_APPLICATOR_MAPS = frozenset({"dependencies", "dependentSchemas", "patternProperties", "properties"})
# Evaluated through ``evolve``, which keeps the parent's base URI where
# ``descend`` moves it to the subschema's own ``$id``. ``oneOf`` descends into
# its branches up to the first that matches and evolves the rest.
_EVOLVED = frozenset({"contains", "if", "not"})
_UNEVALUATED = frozenset({"unevaluatedItems", "unevaluatedProperties"})

#: The subschemas one reference walk visits at most.
REFERENCE_WALK_MAX_VISITS = 20_000


@dataclass(frozen=True)
class ReferenceWalk:
    """The references a validator would refuse to resolve, and whether the walk read all it can reach."""

    refused: tuple[str, ...]
    complete: bool


@functools.cache
def _specification_of(cls: type) -> Any:
    # The one ``jsonschema.validators.create`` builds the class's resources with.
    return specification_with(cls.ID_OF(cls.META_SCHEMA) or "urn:unknown-dialect", default=Specification.OPAQUE)


def _subschemas(keyword: str, value: Any) -> list[Any]:
    if keyword in _APPLICATOR_MAPS:
        return list(value.values()) if isinstance(value, dict) else []
    return list(value) if isinstance(value, list) else [value]


class _ReferenceWalk:
    """One walk: (subschema, resolver, its class, the keyword selector it is read with, re-read) visits.

    The selector is the class's ``applicable_validators`` of whoever evaluates
    the subschema; ``None`` for what an ``unevaluated*`` keyword re-reads, key
    by key.
    """

    def __init__(self, validator: Any) -> None:
        cls = type(validator)
        self.pending: list[tuple[Any, Any, type, Any, bool]] = [
            (validator.schema, validator._resolver, cls, cls._APPLICABLE_VALIDATORS, False)
        ]
        self.refused: list[str] = []

    # A subschema the validator cannot read (a ``$schema`` or an ``$id`` that is
    # not a string) makes it raise there: nothing is resolved in it, and its
    # siblings are still read.

    def reach(self, parent: type, resolver: Any, child: Any, *, evolved: bool = False, reread: bool = False) -> None:
        if not isinstance(child, dict):
            return  # a boolean schema holds no reference
        try:
            cls = jsonschema.validators.validator_for(child, default=parent)
            if evolved or reread:
                visit = (child, resolver, cls, None if reread else cls._APPLICABLE_VALIDATORS, reread)
            else:
                base = resolver.in_subresource(_specification_of(parent).create_resource(child))
                visit = (child, base, cls, parent._APPLICABLE_VALIDATORS, False)
        except Exception:
            return
        self.pending.append(visit)

    def follow(self, parent: type, resolver: Any, keyword: str, value: Any, *, reread: bool) -> None:
        try:
            resolved = lookup_recursive_ref(resolver) if keyword == "$recursiveRef" else resolver.lookup(value)
            cls = jsonschema.validators.validator_for(resolved.contents, default=parent)
        except Exception as exc:
            if retrieval_refused(exc):
                self.refused.append(str(value))
            return
        if isinstance(resolved.contents, dict):
            selector = None if reread else parent._APPLICABLE_VALIDATORS
            self.pending.append((resolved.contents, resolved.resolver, cls, selector, reread))

    def evaluate(self, node: dict, resolver: Any, cls: type, selector: Any) -> None:
        try:
            keywords = list(selector(node))
        except Exception:
            return
        for keyword, value in keywords:
            if keyword not in cls.VALIDATORS:
                continue
            if keyword in REF_KEYWORDS:
                self.follow(cls, resolver, keyword, value, reread=False)
            elif keyword in _APPLICATORS:
                for child in _subschemas(keyword, value):
                    self.reach(cls, resolver, child, evolved=keyword in _EVOLVED)
                    if keyword == "oneOf":
                        self.reach(cls, resolver, child, evolved=True)
                if keyword == "if":
                    self.reach(cls, resolver, node.get("then"))
                    self.reach(cls, resolver, node.get("else"))
            if keyword in _UNEVALUATED:
                self.pending.append((node, resolver, cls, None, True))

    def reread(self, node: dict, resolver: Any, cls: type) -> None:
        # What ``find_evaluated_item_indexes_by_schema`` / ``..._property_keys_by_schema`` read.
        for keyword in REF_KEYWORDS:
            if keyword in node and (keyword != "$recursiveRef" or keyword in cls.VALIDATORS):
                self.follow(cls, resolver, keyword, node[keyword], reread=True)
        for keyword in ("allOf", "anyOf", "oneOf"):
            for child in _subschemas(keyword, node.get(keyword, [])):
                self.reach(cls, resolver, child)
                self.reach(cls, resolver, child, reread=True)
        for child in _subschemas("dependentSchemas", node.get("dependentSchemas")):
            self.reach(cls, resolver, child, reread=True)
        if "if" in node:
            self.reach(cls, resolver, node["if"], evolved=True)
            for branch in ("if", "then", "else"):
                self.reach(cls, resolver, node.get(branch), reread=True)
        for keyword in ("additionalProperties", "unevaluatedProperties"):
            self.reach(cls, resolver, node.get(keyword))
        for keyword in ("contains", "unevaluatedItems"):
            self.reach(cls, resolver, node.get(keyword), evolved=True)

    def run(self, max_visits: int) -> ReferenceWalk:
        seen: set[tuple[int, Any, type, Any, bool]] = set()
        while self.pending:
            node, resolver, cls, selector, reread = self.pending.pop()
            key = (id(node), getattr(resolver, "_base_uri", None), cls, selector, reread)
            if key in seen:
                continue
            if len(seen) >= max_visits:
                return ReferenceWalk(tuple(self.refused), complete=False)
            seen.add(key)
            if reread:
                self.reread(node, resolver, cls)
            else:
                self.evaluate(node, resolver, cls, selector)
        return ReferenceWalk(tuple(self.refused), complete=True)


def refused_references(validator: Any, *, max_visits: int | None = None) -> ReferenceWalk:
    """The ``$ref`` / ``$dynamicRef`` / ``$recursiveRef`` *validator* could meet that its registry refuses to retrieve.

    No instance is needed: every subschema the validator can evaluate is
    visited as ``jsonschema`` reaches it - with the keywords its class applies
    (a draft 7 ``$ref`` hides its siblings), a subschema's own ``$schema``
    picking its dialect, ``descend`` moving the base URI to a subschema's own
    ``$id`` where ``evolve`` keeps its parent's (``not`` / ``contains`` / ``if``,
    the later ``oneOf`` branches, what the ``unevaluated*`` keywords re-read),
    and a reference followed into its target - and every reference is resolved
    by the validator's own resolver, :func:`retrieval_refused` telling a
    refused retrieval from a pointer that misses. Instance data, unknown
    keywords and ``$defs`` no reference reaches are never read. At most
    *max_visits* (:data:`REFERENCE_WALK_MAX_VISITS`) subschemas are visited;
    ``complete`` says whether the walk ended on its own.
    """
    return _ReferenceWalk(validator).run(REFERENCE_WALK_MAX_VISITS if max_visits is None else max_visits)
