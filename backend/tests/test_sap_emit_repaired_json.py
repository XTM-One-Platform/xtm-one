"""SAP tolerant emit contract.

When an agent has ``output_format = json`` with an ``output_schema``, the
post-loop SAP step used to emit the repaired/re-serialized JSON only when
schema validation passed with ZERO errors — any validation error passed
the original raw LLM text (potentially with broken escapes) through to
downstream JSON consumers (e.g. OpenCTI playbooks).

``emit_repaired_json`` pins the new contract:

* repaired JSON is emitted as soon as it parses, even with schema
  validation errors (downgraded to log-only warnings),
* parse failures return ``None`` (callers keep their content unchanged),
* a root-type guard prevents the extractor from replacing prose with an
  incidental bracket fragment (``"Use options [a] or [b]"`` repairs to
  ``["b"]`` — which must never win over the original text when the
  schema declares an object payload).
"""

from __future__ import annotations

import json

import pytest

from app.services.structured_output import (
    _matches_schema_root,
    emit_repaired_json,
)

_OBJECT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "type": {"type": "string"},
        "objects": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["type", "objects"],
}

_ARRAY_SCHEMA: dict = {
    "type": "array",
    "items": {"type": "integer"},
}


# ---------------------------------------------------------------------------
# Schema validation errors no longer block the emit
# ---------------------------------------------------------------------------


def test_valid_json_with_schema_errors_is_emitted():
    """A parseable payload missing a required field must still be emitted
    as re-serialized JSON (previously the raw text passed through)."""
    raw = '{"type": "bundle"}'  # missing required "objects"
    result = emit_repaired_json(raw, _OBJECT_SCHEMA, log_context="agent test")
    assert result is not None
    assert json.loads(result) == {"type": "bundle"}


def test_broken_escapes_are_repaired_and_emitted():
    r"""The motivating case: bad escapes in HTML-bearing string
    values (e.g. ``\&`` emitted by the model) parse after json-repair and
    the cleaned re-serialization must be emitted."""
    raw = '{"type": "bundle", "objects": [{"description": "a \\& b"}]}'
    result = emit_repaired_json(raw, _OBJECT_SCHEMA, log_context="agent test")
    assert result is not None
    parsed = json.loads(result)  # must not raise
    assert parsed["type"] == "bundle"


def test_fully_valid_payload_is_emitted():
    raw = 'Here you go:\n```json\n{"type": "bundle", "objects": []}\n```'
    result = emit_repaired_json(raw, _OBJECT_SCHEMA, log_context="agent test")
    assert result is not None
    assert json.loads(result) == {"type": "bundle", "objects": []}


# ---------------------------------------------------------------------------
# Parse failures keep the content unchanged
# ---------------------------------------------------------------------------


def test_pure_prose_returns_none():
    raw = "Sorry, I cannot generate the bundle right now."
    assert emit_repaired_json(raw, _OBJECT_SCHEMA, log_context="agent test") is None


def test_empty_output_returns_none():
    assert emit_repaired_json("", _OBJECT_SCHEMA, log_context="agent test") is None


# ---------------------------------------------------------------------------
# Root-type guard: incidental fragments must never replace prose
# ---------------------------------------------------------------------------


def test_incidental_bracket_fragment_in_prose_returns_none():
    """``[b]`` inside prose repairs to ``["b"]`` — an array root must not
    overwrite the answer of an object-schema agent."""
    raw = "Use options [a] or [b] please."
    assert emit_repaired_json(raw, _OBJECT_SCHEMA, log_context="agent test") is None


def test_inline_list_in_prose_returns_none_for_object_schema():
    raw = "I found 3 items: [1, 2, 3] in the list."
    assert emit_repaired_json(raw, _OBJECT_SCHEMA, log_context="agent test") is None


def test_scalar_extraction_returns_none_for_object_schema():
    assert emit_repaired_json("42", _OBJECT_SCHEMA, log_context="agent test") is None
    assert emit_repaired_json("true", _OBJECT_SCHEMA, log_context="agent test") is None


def test_array_payload_matches_array_schema_despite_item_errors():
    """An array root is emitted for an array schema even when item
    validation reports errors (log-only)."""
    raw = 'Result: [1, 2, "three"]'
    result = emit_repaired_json(raw, _ARRAY_SCHEMA, log_context="agent test")
    assert result is not None
    assert json.loads(result) == [1, 2, "three"]


# ---------------------------------------------------------------------------
# JSON null roots: None is both a parse-failure sentinel and a legit payload
# ---------------------------------------------------------------------------


def test_null_payload_is_emitted_when_schema_admits_null():
    """``json.loads("null")`` returns ``None`` — when the schema root
    admits ``"null"`` and validation passes, the emit must not be
    mistaken for a parse failure."""
    schema = {"type": ["object", "null"]}
    assert emit_repaired_json("null", schema, log_context="agent test") == "null"


def test_null_payload_returns_none_for_object_schema():
    """A parsed ``null`` that fails an object schema keeps the content
    unchanged (and must not be logged as a parse failure)."""
    assert emit_repaired_json("null", _OBJECT_SCHEMA, log_context="agent test") is None


def test_null_payload_returns_none_for_typeless_schema():
    """Typeless schemas validate ``null`` with zero errors, but the
    containers-only policy still blocks the emit."""
    assert emit_repaired_json("null", {}, log_context="agent test") is None


# ---------------------------------------------------------------------------
# _matches_schema_root edge cases
# ---------------------------------------------------------------------------


def test_root_match_handles_null_roots():
    assert _matches_schema_root(None, {"type": "null"}) is True
    assert _matches_schema_root(None, {"type": ["object", "null"]}) is True
    assert _matches_schema_root(None, _OBJECT_SCHEMA) is False
    assert _matches_schema_root(None, {}) is False


def test_root_match_infers_object_from_properties():
    schema = {"properties": {"a": {"type": "string"}}}
    assert _matches_schema_root({"a": "x"}, schema) is True
    assert _matches_schema_root(["x"], schema) is False


def test_root_match_without_declared_type_accepts_containers_only():
    assert _matches_schema_root({}, {}) is True
    assert _matches_schema_root([], {}) is True
    assert _matches_schema_root("text", {}) is False
    assert _matches_schema_root(42, {}) is False


def test_root_match_handles_type_lists_and_bool_vs_int():
    schema = {"type": ["object", "array"]}
    assert _matches_schema_root({}, schema) is True
    assert _matches_schema_root([], schema) is True
    # bool is an int subclass — must not satisfy integer/number roots
    assert _matches_schema_root(True, {"type": "integer"}) is False
    assert _matches_schema_root(True, {"type": "boolean"}) is True
    assert _matches_schema_root(1, {"type": "number"}) is True


# ---------------------------------------------------------------------------
# Schema-less coercion: output_format="json" WITHOUT an output_schema
#
# OpenAEV parses the reply of its cti.ttp_harvester and
# aev.detection_rules_generator intents with mapper.readValue and has no repair
# path: one markdown fence yields zero extracted TTPs (silently) or a 502.
# ---------------------------------------------------------------------------


def test_emit_bare_json_strips_markdown_fence():
    from app.services.structured_output import emit_bare_json

    raw = '```json\n{"predictions": {"T1059": 0.9}}\n```'
    out = emit_bare_json(raw, log_context="test")
    assert out is not None
    assert json.loads(out) == {"predictions": {"T1059": 0.9}}
    assert "```" not in out


def test_emit_bare_json_strips_surrounding_prose():
    from app.services.structured_output import emit_bare_json

    raw = (
        "Here are the rules you asked for:\n"
        '{"success": true, "spl_query": "index=win"}\n'
        "Hope that helps!"
    )
    out = emit_bare_json(raw, log_context="test")
    assert out is not None
    assert json.loads(out)["spl_query"] == "index=win"


def test_emit_bare_json_repairs_trailing_comma():
    from app.services.structured_output import emit_bare_json

    out = emit_bare_json('{"rules": [1, 2,],}', log_context="test")
    assert out is not None
    assert json.loads(out) == {"rules": [1, 2]}


def test_emit_bare_json_passes_through_already_bare_json():
    from app.services.structured_output import emit_bare_json

    out = emit_bare_json('{"a": 1}', log_context="test")
    assert out is not None
    assert json.loads(out) == {"a": 1}


def test_emit_bare_json_leaves_non_payloads_alone():
    from app.services.structured_output import emit_bare_json

    # No JSON at all, and bare scalars that merely parse: all must return None
    # so the caller keeps the original content rather than emitting something
    # worse than the prose it replaced.
    assert emit_bare_json("I could not find any TTPs.", log_context="t") is None
    assert emit_bare_json("42", log_context="t") is None
    assert emit_bare_json("null", log_context="t") is None
    assert emit_bare_json("", log_context="t") is None


# ---------------------------------------------------------------------------
# deliver_json_answer: the one rule a chat turn and an LLM Only entry apply
# ---------------------------------------------------------------------------


def test_deliver_json_answer_repairs_against_the_schema_when_there_is_one():
    from app.services.structured_output import deliver_json_answer

    raw = '```json\n{"type": "bundle", "objects": [],}\n```'
    out = deliver_json_answer(raw, "json", _OBJECT_SCHEMA, log_context="t")
    assert json.loads(out) == {"type": "bundle", "objects": []}


def test_deliver_json_answer_strips_the_wrapping_without_a_schema():
    from app.services.structured_output import deliver_json_answer

    out = deliver_json_answer('Here you go: {"a": 1}', "json", None, log_context="t")
    assert json.loads(out) == {"a": 1}


def test_deliver_json_answer_leaves_every_other_answer_as_it_came():
    from app.services.structured_output import deliver_json_answer

    fenced = '```json\n{"a": 1}\n```'
    # Not a JSON producer: its answer is not ours to rewrite.
    assert deliver_json_answer(fenced, "markdown", None, log_context="t") == fenced
    # A JSON producer whose answer yields no payload keeps it, rather than an empty one.
    assert deliver_json_answer("I found nothing.", "json", None, log_context="t") == "I found nothing."
    assert deliver_json_answer("", "json", None, log_context="t") == ""


# ---------------------------------------------------------------------------
# The outermost-block search: what the regex matched, in linear time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "block"),
    [
        ('note {"a": [1]} end', '{"a": [1]}'),
        # The first opening bracket with a closing one of its kind after it, to the LAST.
        ("a { b [1] c", "[1]"),
        ("{x} and {y}", "{x} and {y}"),
        ("no brackets", "no brackets"),
    ],
)
def test_extract_json_text_takes_the_outermost_block(text, block):
    from app.services.structured_output import extract_json_text

    assert extract_json_text(text) == block


def test_extract_json_text_is_linear_in_the_answer():
    """The regex it replaced took minutes on this. The bound is loose for slow runners."""
    import time

    from app.services.structured_output import extract_json_text

    started = time.perf_counter()
    extract_json_text("{" * 1_000_000)
    assert time.perf_counter() - started < 10.0
