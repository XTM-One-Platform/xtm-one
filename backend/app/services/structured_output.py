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

"""Schema-Aligned Parsing (SAP) for structured LLM output.

Provides a BAML-style pipeline that works with **any** LLM model:

1. **Schema → Prompt** – convert a JSON Schema into readable output instructions
2. **Extract** – pull JSON from markdown fences, chain-of-thought text, etc.
3. **Repair** – fix common JSON syntax errors (trailing commas, unquoted keys…)
4. **Coerce** – align values to declared types (``"123"`` → ``123``)
5. **Validate** – verify the result against the original JSON Schema

Providers with native structured output (OpenAI ``response_format``,
Gemini ``response_schema``) bypass steps 2–3 but still run validation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from json_repair import repair_json
from jsonschema import Draft7Validator

from app.services.output_schema_validation import output_schema_errors, output_schema_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 0. Platform keywords never leave the platform
# ---------------------------------------------------------------------------

#: Prefix of the JSON Schema keywords the platform reads itself (``x-xtm-roi``,
#: the ROI unit an agent declares). A validator ignores them; a provider's strict
#: mode may refuse them and the model has no use for them, so they are dropped
#: from every schema sent to a provider or written into a prompt.
PLATFORM_KEYWORD_PREFIX = "x-xtm-"


def strip_platform_keywords(node: Any, _depth: int = 0) -> Any:
    """Return *node* without ``x-xtm-*`` keys at any depth (a copy, input untouched).

    Only keyword positions are dropped: a property NAMED ``x-xtm-...`` under
    ``properties`` is the author's data and stays.
    """
    if _depth > 64:
        return node
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if isinstance(key, str) and key.startswith(PLATFORM_KEYWORD_PREFIX):
                continue
            if key in ("properties", "$defs", "definitions") and isinstance(value, dict):
                out[key] = {k: strip_platform_keywords(v, _depth + 1) for k, v in value.items()}
            else:
                out[key] = strip_platform_keywords(value, _depth + 1)
        return out
    if isinstance(node, list):
        return [strip_platform_keywords(item, _depth + 1) for item in node]
    return node


# ---------------------------------------------------------------------------
# 1. Schema → Prompt
# ---------------------------------------------------------------------------


def build_schema_prompt(schema: dict, name: str = "") -> str:
    """Convert a JSON Schema dict into human-readable output instructions.

    The result is designed to be appended to the system prompt so the LLM
    knows exactly what shape of JSON to produce.
    """
    header = "You MUST respond with a valid JSON object"
    if name:
        header += f" (type: {name})"
    header += " that conforms to the following JSON Schema:"

    schema = strip_platform_keywords(schema)
    schema_str = json.dumps(schema, indent=2)

    parts = [
        header,
        f"```json\n{schema_str}\n```",
        "Rules:",
        "- Output ONLY the JSON object — no markdown fences, no explanation, no extra text.",
        "- Every required field must be present with the correct type.",
        "- Use null for optional fields you cannot determine.",
        "- Do NOT add fields that are not in the schema.",
    ]

    # Add per-property hints for common types
    props = schema.get("properties", {})
    if props:
        hints: list[str] = []
        for prop_name, prop_def in props.items():
            ptype = prop_def.get("type", "")
            if ptype == "array":
                items = prop_def.get("items", {})
                item_type = items.get("type", "any")
                hints.append(f'- "{prop_name}": array of {item_type}')
            elif "enum" in prop_def:
                allowed = ", ".join(f'"{v}"' for v in prop_def["enum"])
                hints.append(f'- "{prop_name}": one of [{allowed}]')
        if hints:
            parts.append("\nField hints:")
            parts.extend(hints)

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 2. Extract JSON from raw LLM output
# ---------------------------------------------------------------------------

# Pattern to find JSON inside markdown fences. The whitespace after the
# opening fence is possessive: a backtracking ``\s*`` let the lazy group
# restart at every whitespace prefix, quadratic on an unclosed fence followed
# by a long whitespace run. The group is stripped by every reader, so
# where it starts inside that whitespace never changed a result.
_MD_FENCE_RE = re.compile(r"```(?:json)?\s*+([\s\S]*?)```", re.IGNORECASE)

def _outermost_block(text: str) -> str | None:
    """The outermost ``{ … }`` or ``[ … ]`` of ``text``: from the first opening bracket that
    has a matching closing kind after it, to the LAST such closing bracket.

    What the greedy regex search it replaces matched, in one pass instead of quadratic
    time on a run of unclosed brackets: a 1 MiB answer of ``{`` took minutes.
    """
    last_brace, last_bracket = text.rfind("}"), text.rfind("]")
    for index, ch in enumerate(text):
        if ch == "{" and last_brace > index:
            return text[index : last_brace + 1]
        if ch == "[" and last_bracket > index:
            return text[index : last_bracket + 1]
    return None


def extract_json_text(raw: str) -> str:
    """Extract the JSON payload from potentially messy LLM output.

    Handles:
    - Markdown ````` ```json … ``` ````` fences
    - Chain-of-thought text surrounding the JSON
    - Plain JSON strings
    """
    text = raw.strip()

    # 1. Prefer JSON found inside a markdown fence — but scan ALL fences and
    #    pick the first whose content is actually JSON-shaped. Models
    #    sometimes emit an empty ```json``` stub (or a prose-only fence)
    #    before the real payload; returning that first (empty/prose) fence is
    #    what produced the char-0 "Expecting value" parse failure that
    #    silently dropped compacted directives.
    for m in _MD_FENCE_RE.finditer(text):
        fenced = m.group(1).strip()
        if not fenced:
            continue
        if fenced[0] in "{[":
            return fenced
        # Fence holds prose + JSON (e.g. "Here you go:\n{...}") — pull the
        # inner block out rather than skipping the fence entirely.
        inner = _outermost_block(fenced)
        if inner:
            return inner.strip()

    # 2. No fenced JSON — take the outermost { … } / [ … ] block (handles
    #    prose-prefixed, unfenced JSON).
    block = _outermost_block(text)
    if block:
        return block.strip()

    # 3. Return as-is (let repair_json handle it)
    return text


# ---------------------------------------------------------------------------
# 2b. Tell damaged JSON from prose
# ---------------------------------------------------------------------------

# Bare words damaged JSON legitimately carries as values: JSON and Python
# literals, which json-repair maps to JSON ones.
_LITERAL_WORDS = frozenset({
    "true", "false", "null", "True", "False", "None", "NaN", "Infinity", "undefined",
})
_NUMBER_RE = re.compile(r"[-+]?(?:\d[\d_]*(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
# Characters that end a bare word.
_WORD_STOP = frozenset("{}[],:\"'") | frozenset(" \t\r\n\f\v")
# Characters after which a quote closes a string: what can follow a JSON
# value, or a comment. Anything else makes it an unescaped quote inside the
# string, as json-repair reads it.
_STRING_CLOSERS = frozenset(",:]}/#")


def _skip_insignificant(text: str, i: int) -> int:
    """Index of the first character at or after ``i`` that is not whitespace
    or a ``//``, ``#`` or ``/* */`` comment (all of which json-repair drops)."""
    n = len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch == "#" or text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            break
    return i


def _skip_string(text: str, i: int) -> int:
    """Index just past the string opening at ``i`` (``"`` or ``'``).

    A quote closes the string only when what follows it can follow a JSON
    value; any other quote is taken as an unescaped quote inside the string.
    An unterminated string runs to the end.
    """
    quote, n, j = text[i], len(text), i + 1
    while j < n:
        ch = text[j]
        if ch == "\\":
            j += 2
            continue
        if ch == quote:
            # Whitespace only: a whitespace run holds no quote, so no
            # character is looked at twice (a comment may hold many quotes).
            k = j + 1
            while k < n and text[k].isspace():
                k += 1
            if k >= n or text[k] in _STRING_CLOSERS:
                return j + 1
        j += 1
    return n


def _is_prose_word(word: str) -> bool:
    return (
        word not in _LITERAL_WORDS
        and not _NUMBER_RE.fullmatch(word)
        and any(ch.isalpha() for ch in word)
    )


def reads_as_prose(span: str) -> bool:
    """True when ``span`` is a bracketed fragment of a sentence, not damaged JSON.

    ``extract_json_text`` takes the outermost ``{ … }`` / ``[ … ]`` of an
    answer. When the model answered in prose, that span is part of a
    sentence (``[high confidence]``, ``{sorry}``, ``[a] or [b]``) and
    json-repair turns it into a payload that was never the answer.
    Damaged JSON (trailing commas, single quotes, unquoted keys and values,
    Python literals, comments, unescaped inner quotes, a missing comma after
    a string or a number, missing or mismatched brackets) keeps the shape of
    JSON; a fragment of prose breaks it in one of three ways, all on a bare
    word (outside a string, holding a letter, not a number nor a literal):

    * in an array, the word is followed by another value with no comma
      between them (``[high confidence]``);
    * in an object, the word stands where a key goes and no ``:`` follows
      it (``{sorry}``, ``{see the logs}``);
    * after the first container has closed, the word stands outside any
      container (``[a] or [b]``).

    A word in an object value is not checked: ``{name: John Smith}`` is
    damaged JSON json-repair reads as intended. One pass over ``span``.
    """
    # Open containers: "[" an array, "{" an object awaiting a key, ":" an
    # object awaiting a value. Counters keep a mismatched close linear.
    stack: list[str] = []
    open_arrays = 0
    seen_container = False
    n = len(span)
    i = _skip_insignificant(span, 0)
    while i < n:
        ch = span[i]
        if ch in "{[":
            stack.append(ch)
            open_arrays += ch == "["
            seen_container = True
            i += 1
        elif ch == "]":
            # Close the innermost array, and whatever damaged JSON left open in it.
            if open_arrays:
                while stack.pop() != "[":
                    pass
                open_arrays -= 1
            i += 1
        elif ch == "}":
            if len(stack) > open_arrays:  # at least one object is open
                while stack.pop() == "[":
                    open_arrays -= 1
            i += 1
        elif ch == ":":
            if stack and stack[-1] == "{":
                stack[-1] = ":"
            i += 1
        elif ch == ",":
            if stack and stack[-1] == ":":
                stack[-1] = "{"
            i += 1
        elif ch in "\"'":
            i = _skip_string(span, i)
        else:
            end = i
            while end < n and span[end] not in _WORD_STOP:
                end += 1
            if _is_prose_word(span[i:end]):
                nxt = _skip_insignificant(span, end)
                following = span[nxt] if nxt < n else ""
                top = stack[-1] if stack else ""
                if not top and seen_container:
                    return True
                if top == "{" and following and following != ":":
                    return True
                if top == "[" and following and following not in ",]}:":
                    return True
            i = end
        i = _skip_insignificant(span, i)
    return False


def _strictly_parses(text: str) -> bool:
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return False
    return True


def _prose_fragment(extracted: str, *, log_context: str) -> bool:
    """True, and logged, when ``extracted`` (the ``extract_json_text`` span of
    an answer) needs repair and reads as prose: the answer is prose and must
    be delivered as written."""
    if _strictly_parses(extracted) or not reads_as_prose(extracted):
        return False
    logger.warning(
        "JSON-format output for %s is prose, not damaged JSON "
        "(content left unchanged)",
        log_context,
    )
    return True


# ---------------------------------------------------------------------------
# 3. Repair JSON
# ---------------------------------------------------------------------------


def repair_json_text(broken: str) -> str:
    """Fix common JSON syntax errors using json-repair.

    Handles trailing commas, single quotes, unquoted keys, missing brackets, etc.
    """
    result = repair_json(broken, return_objects=False)
    if isinstance(result, str):
        return result
    return str(result)


# ---------------------------------------------------------------------------
# 4. Type coercion
# ---------------------------------------------------------------------------


def _coerce_value(value: Any, prop_schema: dict) -> Any:
    """Coerce a single value to match the declared JSON Schema type."""
    if value is None:
        return value
    target = prop_schema.get("type")
    if target is None:
        return value

    if target == "integer":
        if isinstance(value, str):
            try:
                return int(value)
            except (ValueError, TypeError):
                try:
                    return int(float(value))
                except (ValueError, TypeError):
                    return value
        if isinstance(value, float) and math.isfinite(value) and value == int(value):
            return int(value)

    elif target == "number":
        if isinstance(value, str):
            try:
                return float(value)
            except (ValueError, TypeError):
                return value

    elif target == "boolean":
        if isinstance(value, str):
            low = value.lower().strip()
            if low in ("true", "1", "yes"):
                return True
            if low in ("false", "0", "no"):
                return False

    elif target == "string":
        if not isinstance(value, str):
            return str(value)

    elif target == "array":
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return parsed
            except (json.JSONDecodeError, TypeError) as exc:
                # If the value cannot be parsed as JSON, fall back to the original string.
                logger.debug(
                    "Failed to parse value as JSON array during type coercion: %r (%s)",
                    value,
                    exc,
                )
        items_schema = prop_schema.get("items", {})
        if isinstance(value, list) and items_schema:
            return [_coerce_value(item, items_schema) for item in value]

    elif target == "object":
        if isinstance(value, dict):
            return coerce_types(value, prop_schema)

    return value


def coerce_types(data: dict, schema: dict) -> dict:
    """Recursively coerce values in *data* to match *schema* types.

    Handles: ``"123"`` → ``123``, ``"true"`` → ``true``, nested objects,
    and strips fields not declared in the schema when
    ``additionalProperties`` is ``false``.
    """
    if not isinstance(data, dict):
        return data

    props = schema.get("properties", {})
    additional = schema.get("additionalProperties", True)
    result = {}
    for key, value in data.items():
        if key in props:
            result[key] = _coerce_value(value, props[key])
        elif additional is not False:
            # Keep unknown keys only when additionalProperties allows them
            result[key] = value
        # else: silently drop fields not in schema
    return result


# ---------------------------------------------------------------------------
# 5. Full SAP pipeline
# ---------------------------------------------------------------------------


#: How many schema validation errors ``validate_and_parse`` collects. Callers
#: read whether there are errors and log them; none needs the whole list.
MAX_SAP_VALIDATION_ERRORS = 50


#: How validate_and_parse words a schema error, apart from a repair or parse one.
_VALIDATION_ERROR_PREFIX = "Validation error at "


def validate_and_parse(
    raw_output: str,
    schema: dict,
    *,
    allow_partial: bool = False,
) -> tuple[dict | list | None, list[str]]:
    """Run the full Schema-Aligned Parsing pipeline.

    Returns ``(parsed_data, errors)``.
    - On success: ``(data, [])``
    - On failure: ``(best_effort_data_or_None, [error_messages])``
    """
    errors: list[str] = []

    if not raw_output:
        return None, ["Empty output"]

    # Step 1: Extract JSON from raw text
    json_text = extract_json_text(raw_output)

    # Step 2: Repair JSON
    try:
        repaired = repair_json_text(json_text)
    except Exception as exc:
        errors.append(f"JSON repair failed: {exc}")
        repaired = json_text

    # Step 3: Parse
    try:
        data = json.loads(repaired)
    except json.JSONDecodeError as exc:
        errors.append(f"JSON parse failed after repair: {exc}")
        return None, errors

    # Step 4: Type coercion (dicts and arrays)
    if isinstance(data, dict):
        data = coerce_types(data, schema)
    elif isinstance(data, list):
        items_schema = schema.get("items", {})
        if items_schema:
            data = [
                (
                    coerce_types(item, items_schema)
                    if isinstance(item, dict)
                    else _coerce_value(item, items_schema)
                )
                for item in data
            ]

    # Step 5: Validate against schema (bounded regexes, nothing retrieved). The
    # errors are only reported, so the first MAX_SAP_VALIDATION_ERRORS of them
    # are enough: collecting every error of a large payload (a STIX bundle of
    # thousands of objects) is what kept a worker busy for minutes.
    validator = output_schema_validator(schema, Draft7Validator)
    validation_errors = sorted(
        output_schema_errors(validator, data, limit=MAX_SAP_VALIDATION_ERRORS),
        key=lambda e: list(e.absolute_path),
    )

    if validation_errors:
        for err in validation_errors:
            path = ".".join(str(p) for p in err.absolute_path) or "(root)"
            errors.append(f"{_VALIDATION_ERROR_PREFIX}{path}: {err.message}")

        if not allow_partial:
            return data, errors

    return data, []


def _matches_schema_root(data: Any, schema: dict) -> bool:
    """Return True when *data*'s root type is plausible for *schema*.

    Guards the tolerant emit path against the extractor latching
    onto an incidental bracket fragment inside prose: ``"Use options [a]
    or [b]"`` repairs to ``["b"]``, which must never replace the original
    text when the schema declares an object payload.
    """
    declared = schema.get("type")
    if declared is None and "properties" in schema:
        declared = "object"
    if declared is None:
        # No declared root type — only containers are safe to emit.
        return isinstance(data, (dict, list))
    types = declared if isinstance(declared, list) else [declared]
    if data is None:
        return "null" in types
    if isinstance(data, bool):
        return "boolean" in types
    if isinstance(data, dict):
        return "object" in types
    if isinstance(data, list):
        return "array" in types
    if isinstance(data, str):
        return "string" in types
    if isinstance(data, int):
        return "integer" in types or "number" in types
    if isinstance(data, float):
        return "number" in types
    return False


def emit_repaired_json(
    raw_output: str,
    schema: dict,
    *,
    log_context: str,
) -> str | None:
    """The repaired JSON text of emit_repaired_json_checked, without its schema errors."""
    return emit_repaired_json_checked(raw_output, schema, log_context=log_context)[0]


def emit_repaired_json_checked(
    raw_output: str,
    schema: dict[str, Any],
    *,
    log_context: str,
) -> tuple[str | None, list[str]]:
    """Run the SAP pipeline: the re-serialized repaired JSON and its schema errors.

    The errors are those of the payload emitted (a required key missing, an
    array below its minItems); they are empty when the payload is valid and
    when nothing is emitted (the content is then left unchanged). Syntax damage
    the repair fixed is never one of them.

    As soon as the repaired output parses AND the parsed root type is
    plausible for the schema, the indent-2 re-serialization is returned —
    even when schema validation reports errors (downgraded to log-only
    warnings): a syntactically-valid-but-schema-imperfect payload is
    strictly better for downstream JSON consumers (e.g. OpenCTI playbooks)
    than the raw LLM text with broken escapes.

    Returns ``None`` when the repaired text does not parse, or when the
    parsed root is clearly not the schema's payload (e.g. a stray bracket
    fragment extracted from prose) — callers keep their existing content
    unchanged at this stage (later stages, such as a finalize tool, may
    still transform it).

    A legitimately parsed JSON ``null`` is emitted as ``"null"`` only in
    the unambiguous case: zero pipeline errors (so parsing definitely
    succeeded and the schema validated the null) AND a schema root that
    admits ``"null"``.

    Also returns ``None`` when the answer is prose whose bracketed span
    would only parse after repair (``reads_as_prose``): repaired,
    ``"Use options [a] or [b]"`` is ``["b"]``, which an array schema accepts.
    """
    if raw_output and _prose_fragment(
        extract_json_text(raw_output), log_context=log_context
    ):
        return None, []
    parsed_data, sap_errors = validate_and_parse(raw_output, schema)
    if parsed_data is None:
        # ``validate_and_parse`` overloads ``None``: it is both the
        # parse-failure sentinel and a legitimately parsed JSON ``null``.
        if not sap_errors and _matches_schema_root(None, schema):
            return "null", []
        if sap_errors:
            logger.warning(
                "SAP yielded no emittable payload for %s "
                "(content left unchanged at this stage): %s",
                log_context,
                sap_errors,
            )
        return None, []
    if not _matches_schema_root(parsed_data, schema):
        logger.warning(
            "SAP parsed root type %s does not match schema root for %s "
            "(content left unchanged at this stage): %s",
            type(parsed_data).__name__,
            log_context,
            sap_errors,
        )
        return None, []
    if sap_errors:
        logger.warning(
            "SAP schema validation errors for %s "
            "(emitting repaired JSON at this stage): %s",
            log_context,
            sap_errors,
        )
    return json.dumps(parsed_data, indent=2, ensure_ascii=False), [
        error for error in sap_errors if error.startswith(_VALIDATION_ERROR_PREFIX)
    ]


def emit_bare_json(raw_output: str, *, log_context: str) -> str | None:
    """Coerce ``output_format="json"`` output to bare, parseable JSON.

    The schema-less counterpart of ``emit_repaired_json``, for agents declared
    as JSON producers that have no ``output_schema``. It strips markdown fences
    and surrounding prose, repairs common syntax damage, and re-serializes -
    without any schema opinion about the shape.

    This exists because downstream consumers parse our answer STRICTLY. OpenAEV
    feeds the reply of its ``cti.ttp_harvester`` and
    ``aev.detection_rules_generator`` intents straight into
    ``mapper.readValue`` with no repair path: a single ```` ```json ```` fence
    yields zero extracted TTPs (silently) or a 502. Fixing that deterministically
    here is the alternative to telling the model harder not to use fences, which
    architecture invariants #15/#16 rule out.

    Returns ``None`` when the text does not yield parseable JSON, or when it is
    prose whose bracketed span would only parse after repair
    (``reads_as_prose``: ``[high confidence]`` in a sentence is not the
    answer), so the caller keeps the original content rather than emitting
    something worse.
    """
    if not raw_output or not raw_output.strip():
        return None
    extracted = extract_json_text(raw_output)
    if _prose_fragment(extracted, log_context=log_context):
        return None
    for candidate in (extracted, repair_json_text(extracted)):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        # A bare scalar is almost always prose that happened to parse (a bare
        # number, or `null` from an empty answer) - not the JSON payload the
        # consumer asked for. Leave those alone.
        if not isinstance(parsed, dict | list):
            return None
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    logger.warning(
        "Could not coerce JSON-format output to bare JSON for %s "
        "(content left unchanged)",
        log_context,
    )
    return None


def deliver_json_answer(
    content: str,
    output_format: str,
    output_schema: dict | None,
    *,
    log_context: str,
) -> str:
    """The answer an agent declared as a JSON producer delivers, from the model's text.

    One rule for a chat turn (``agent_chat``) and an LLM Only evaluation entry, so
    an evaluation scores what the agent delivers in use. An assignment run keeps its own
    schema-only step (``assignment_engine``).
    With a schema, the SAP pipeline repairs and re-serializes the payload
    (``emit_repaired_json``); without one, fences and surrounding prose are stripped
    (``emit_bare_json``). Any other format, an empty answer, or text that yields no
    payload is returned unchanged.

    A caller holding an answer the per-run budget cut short must not call this:
    repairing a partial payload would deliver it as a complete one.
    """
    return deliver_json_answer_checked(content, output_format, output_schema, log_context=log_context).content


#: The stop reason of a run whose final answer still violated its output
#: schema after the one corrective re-prompt: delivered, but partial.
SCHEMA_VIOLATION_STOP_REASON = "schema_violation"

#: How many schema errors a corrective re-prompt and a caller's result name.
SCHEMA_ERRORS_SHOWN = 10


@dataclass(frozen=True)
class JsonDelivery:
    """What deliver_json_answer_checked delivers: the answer and the schema
    errors of the payload it carries (empty when valid or schema-less)."""

    content: str
    schema_errors: list[str] = field(default_factory=list)


def deliver_json_answer_checked(
    content: str,
    output_format: str,
    output_schema: dict[str, Any] | None,
    *,
    log_context: str,
) -> JsonDelivery:
    """deliver_json_answer with the schema errors of the payload it delivers.

    The repair (fences, escapes, prose around the payload) is the same; the
    errors are kept so the run that owns the schema can re-prompt once and,
    when the answer is still off its schema, say so to its caller instead of
    storing a broken contract as a completed answer (#2765).
    """
    if output_format != "json" or not content:
        return JsonDelivery(content)
    if output_schema:
        repaired, errors = emit_repaired_json_checked(content, output_schema, log_context=log_context)
    else:
        repaired, errors = emit_bare_json(content, log_context=log_context), []
    return JsonDelivery(content if repaired is None else repaired, errors)


async def deliver_json_answer_checked_in_thread(
    content: str,
    output_format: str,
    output_schema: dict[str, Any] | None,
    *,
    log_context: str,
) -> JsonDelivery:
    """deliver_json_answer_checked in a worker thread (the repair is CPU-bound)."""
    return await asyncio.to_thread(
        deliver_json_answer_checked, content, output_format, output_schema, log_context=log_context
    )


@dataclass(frozen=True)
class SchemaContractDelivery:
    """The answer a run with an output schema delivers, after at most one
    corrective re-prompt: its schema errors are empty when the contract holds."""

    content: str
    schema_errors: list[str] = field(default_factory=list)
    reprompted: bool = False


#: The corrective call: given the re-prompt and the answer it corrects, the
#: model's new answer (None when it gave none).
SchemaReprompt = Callable[[str, str], Awaitable[str | None]]


async def deliver_json_answer_with_contract(
    content: str,
    output_format: str,
    output_schema: dict[str, Any] | None,
    *,
    log_context: str,
    reprompt: SchemaReprompt | None,
) -> SchemaContractDelivery:
    """Deliver a JSON answer and hold it to its output schema (#2765).

    The answer is repaired as ``deliver_json_answer`` repairs it (fences,
    escapes). When the repaired payload is still off its schema (a required
    key missing, an array below its minItems) and a *reprompt* is given, the
    model is asked once, with the error list, for the full object. A valid
    second answer is delivered clean; otherwise the payload with the fewer
    errors is delivered with its errors, for the run to mark itself partial.
    A valid first answer is never re-prompted.
    """
    first = await deliver_json_answer_checked_in_thread(content, output_format, output_schema, log_context=log_context)
    if not first.schema_errors or reprompt is None or not output_schema:
        return SchemaContractDelivery(first.content, first.schema_errors)
    logger.info(
        "Answer of %s is off its output schema (%d error(s)): one corrective re-prompt",
        log_context,
        len(first.schema_errors),
    )
    try:
        again = await reprompt(schema_corrective_message(first.schema_errors), first.content)
    except Exception as exc:
        logger.warning(
            "Corrective re-prompt of %s failed (%s): delivering the first answer",
            log_context,
            type(exc).__name__,
        )
        again = None
    if not again:
        return SchemaContractDelivery(first.content, first.schema_errors, reprompted=True)
    second = await deliver_json_answer_checked_in_thread(again, output_format, output_schema, log_context=log_context)
    best = second if len(second.schema_errors) <= len(first.schema_errors) else first
    return SchemaContractDelivery(best.content, best.schema_errors, reprompted=True)


def schema_violation_note(errors: list[str]) -> str:
    """The line a caller reads with an answer still off its output schema."""
    shown = "; ".join(errors[:3])
    more = len(errors) - 3
    tail = f"; and {more} more" if more > 0 else ""
    return (
        f"partial ({SCHEMA_VIOLATION_STOP_REASON}): the answer does not match the "
        f"requested output_schema: {shown}{tail}"
    )


def schema_corrective_message(errors: list[str]) -> str:
    """The one corrective re-prompt of an answer off its output schema."""
    shown = "\n".join(f"- {error}" for error in errors[:SCHEMA_ERRORS_SHOWN])
    more = len(errors) - SCHEMA_ERRORS_SHOWN
    tail = f"\n- ... and {more} more" if more > 0 else ""
    return (
        "Your answer does not match the required output schema:\n"
        f"{shown}{tail}\n"
        "Answer again with the full JSON object only, every required key present "
        "and every array at its required size, using what you already found."
    )


async def emit_repaired_json_in_thread(
    raw_output: str,
    schema: dict,
    *,
    log_context: str,
) -> str | None:
    """``emit_repaired_json`` in a worker thread, for a caller on the event loop.

    Repairing and validating a large answer is CPU-bound and can take minutes;
    run on the event loop it blocks every other request of the worker, its
    health probe included, until the worker is restarted and the caller's
    connection drops with no trace.
    """
    return await asyncio.to_thread(emit_repaired_json, raw_output, schema, log_context=log_context)


async def deliver_json_answer_in_thread(
    content: str,
    output_format: str,
    output_schema: dict | None,
    *,
    log_context: str,
) -> str:
    """``deliver_json_answer`` in a worker thread, for a caller on the event loop.

    Same rule as ``deliver_json_answer``; a non-JSON format returns at once
    without a thread hop.
    """
    if output_format != "json" or not content:
        return content
    return await asyncio.to_thread(
        deliver_json_answer,
        content,
        output_format,
        output_schema,
        log_context=log_context,
    )


# ---------------------------------------------------------------------------
# 6. Build native structured output config per provider
# ---------------------------------------------------------------------------


# Keywords unsupported by OpenAI strict mode (all models).
# Note: pattern, format, minItems, maxItems, minimum, maximum,
# exclusiveMinimum, exclusiveMaximum, multipleOf ARE supported
# for non-fine-tuned models — do NOT strip them.
# Ref: https://developers.openai.com/api/docs/guides/structured-outputs
_STRICT_UNSUPPORTED_KEYWORDS = frozenset({
    "default",
    # Composition keywords unsupported for all models
    "not",
    "dependentRequired",
    "dependentSchemas",
    "if",
    "then",
    "else",
    "patternProperties",
    # Misc unsupported
    "contentEncoding",
    "contentMediaType",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "minLength",
    "maxLength",
})


def _normalize_for_strict(schema: dict) -> dict:
    """Ensure a JSON schema conforms to OpenAI's strict mode requirements.

    OpenAI strict mode requires:
    - ``additionalProperties: false`` on every object schema
    - All object properties listed in ``required``
    - ``$ref`` nodes must not have sibling keywords (e.g. ``description``)
    - ``const`` is not supported (converted to single-element ``enum``)
    - ``allOf`` is not supported (unwrapped / merged)
    - ``oneOf`` is not supported (converted to ``anyOf``)
    - Unsupported composition/misc keywords are stripped (see ``_STRICT_UNSUPPORTED_KEYWORDS``)
    Returns a deep copy with these constraints applied.
    """
    schema = dict(schema)  # shallow copy top level

    # Unwrap allOf — unsupported by OpenAI strict mode.
    # Common Pydantic pattern: {"allOf": [{"$ref": "..."}], "description": "..."}
    if "allOf" in schema:
        allof_items = schema.pop("allOf")
        if len(allof_items) == 1:
            # Single-element allOf: merge inner schema into current
            for k, v in allof_items[0].items():
                schema[k] = v
        else:
            # Multi-element allOf: best-effort merge of all sub-schemas
            for item in allof_items:
                for k, v in item.items():
                    if k == "properties" and "properties" in schema:
                        schema["properties"].update(v)
                    elif k == "required" and "required" in schema:
                        seen = set(schema["required"])
                        for r in v:
                            if r not in seen:
                                schema["required"].append(r)
                                seen.add(r)
                    else:
                        schema[k] = v

    # Convert oneOf to anyOf (OpenAI only documents anyOf as supported)
    if "oneOf" in schema:
        schema.setdefault("anyOf", []).extend(schema.pop("oneOf"))

    # OpenAI strict mode: $ref must be the only key — strip siblings
    if "$ref" in schema:
        return {"$ref": schema["$ref"]}

    # Convert "const" to single-element "enum" (Pydantic Literal with one value)
    if "const" in schema:
        val = schema.pop("const")
        schema.setdefault("enum", [val])

    # Strip validation keywords unsupported by strict mode
    for kw in _STRICT_UNSUPPORTED_KEYWORDS:
        schema.pop(kw, None)

    stype = schema.get("type")

    if stype == "object" or "properties" in schema:
        schema.setdefault("additionalProperties", False)
        props = schema.get("properties", {})
        if props:
            # OpenAI strict mode requires ALL properties in required
            schema["required"] = list(props.keys())
            # Recurse into property schemas
            schema["properties"] = {
                k: _normalize_for_strict(v) for k, v in props.items()
            }

    if stype == "array" and "items" in schema:
        schema["items"] = _normalize_for_strict(schema["items"])

    # Recurse into anyOf sub-schemas
    if "anyOf" in schema:
        schema["anyOf"] = [_normalize_for_strict(s) for s in schema["anyOf"]]

    # Recurse into $defs / definitions (Pydantic-generated referenced schemas)
    for defs_key in ("$defs", "definitions"):
        if defs_key in schema:
            schema[defs_key] = {
                k: _normalize_for_strict(v) for k, v in schema[defs_key].items()
            }

    return schema


def build_openai_response_format(schema: dict, name: str = "response") -> dict:
    """Build an OpenAI-compatible ``response_format`` parameter.

    Works for OpenAI, Azure OpenAI, and OpenAI-compatible providers.
    Normalizes the schema for strict mode (additionalProperties, required).
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "strict": True,
            "schema": _normalize_for_strict(schema),
        },
    }


# Validation keywords Anthropic's structured output rejects on the wire.
# Unlike OpenAI strict mode, ``output_config.format.schema`` 400s on numeric
# and length bounds ("For 'integer' type, properties maximum, minimum are not
# supported"). Anthropic's SDKs strip this same set and validate it client-side,
# so dropping it here loses nothing.
# Ref: https://platform.claude.com/docs/en/build-with-claude/structured-outputs
_ANTHROPIC_UNSUPPORTED_KEYWORDS = frozenset({
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
})


def _normalize_for_anthropic(schema: dict) -> dict:
    """Ensure a JSON schema is well-formed for Anthropic structured output.

    Anthropic structured output requires ``additionalProperties: false`` on
    object schemas.  Unlike OpenAI strict mode it does *not* require every
    property to be listed in ``required``, so we leave that list as-is.
    It also rejects numeric/length bounds that OpenAI accepts, so we strip
    ``_ANTHROPIC_UNSUPPORTED_KEYWORDS``.
    """
    schema = dict(schema)  # shallow copy

    for kw in _ANTHROPIC_UNSUPPORTED_KEYWORDS:
        schema.pop(kw, None)

    stype = schema.get("type")

    if stype == "object" or "properties" in schema:
        schema.setdefault("additionalProperties", False)
        props = schema.get("properties", {})
        if props:
            schema["properties"] = {
                k: _normalize_for_anthropic(v) for k, v in props.items()
            }

    if stype == "array" and "items" in schema:
        schema["items"] = _normalize_for_anthropic(schema["items"])

    for combo_key in ("anyOf", "oneOf", "allOf"):
        if combo_key in schema:
            schema[combo_key] = [_normalize_for_anthropic(s) for s in schema[combo_key]]

    # Recurse into $defs / definitions (Pydantic-generated referenced schemas)
    for defs_key in ("$defs", "definitions"):
        if defs_key in schema:
            schema[defs_key] = {
                k: _normalize_for_anthropic(v) for k, v in schema[defs_key].items()
            }

    return schema


def build_anthropic_structured_output(schema: dict, name: str = "response") -> dict:
    """Build an Anthropic-compatible structured output config.

    Returns a dict to be set as ``output_config["format"]`` in the
    Anthropic Messages API (``type: "json_schema"``).

    Note: Anthropic's format only accepts ``type`` and ``schema`` —
    ``name`` is NOT a valid key (unlike OpenAI).
    """
    return {
        "type": "json_schema",
        "schema": _normalize_for_anthropic(schema),
    }


def build_bedrock_structured_output(schema: dict, name: str = "response") -> dict:
    """Build a Bedrock Converse API structured output config.

    Returns a dict to be set as ``outputConfig`` in the Bedrock Converse
    API request.  The schema is serialized to a JSON **string** as
    required by the API (JSON Schema Draft 2020-12).
    """
    return {
        "textFormat": {
            "type": "json_schema",
            "structure": {
                "jsonSchema": {
                    "schema": json.dumps(_normalize_for_strict(schema)),
                    "name": name,
                }
            },
        }
    }


def build_gemini_response_schema(schema: dict) -> dict:
    """Build a Gemini-compatible response schema config.

    Returns kwargs to merge into the GenerateContentConfig.
    """
    return {
        "response_mime_type": "application/json",
        "response_schema": schema,
    }


def unwrap_failover_llm(llm: object) -> object:
    """Return the underlying provider, transparently unwrapping a failover wrapper.

    `FailoverLLMProvider` exposes the wrapped primary provider via
    ``.primary``. Centralizing the unwrap convention here lets future
    callers that need to inspect the underlying provider class do so
    without re-deriving the attribute name.
    """
    return getattr(llm, "primary", llm)


def build_native_structured_output(
    llm: object,
    schema: dict,
) -> dict | None:
    """Build provider-native structured output config, if supported.

    Returns a dict to pass as ``structured_output`` kwarg to ``llm.chat()``
    (and ``run_agent_loop``), or ``None`` if the provider doesn't support
    native structured output (prompt-based SAP will be used instead).

    `FailoverLLMProvider` wraps a primary provider; we unwrap it via
    :func:`unwrap_failover_llm` so the underlying provider's native
    format is selected instead of silently falling back to prompt-based
    SAP.
    """
    from app.llm.openai_compat import OpenAICompatibleProvider

    target = unwrap_failover_llm(llm)
    schema = strip_platform_keywords(schema)

    provider = getattr(target, "provider_name", "") or getattr(llm, "provider_name", "")
    if provider in ("openai", "azure_openai"):
        return build_openai_response_format(schema)
    if isinstance(target, OpenAICompatibleProvider) or isinstance(
        llm, OpenAICompatibleProvider
    ):
        return build_openai_response_format(schema)
    if provider == "gemini":
        return build_gemini_response_schema(schema)
    if provider == "anthropic":
        return build_anthropic_structured_output(schema)
    if provider == "bedrock":
        return build_bedrock_structured_output(schema)
    return None
