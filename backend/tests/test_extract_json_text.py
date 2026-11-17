"""Unit tests for ``structured_output.extract_json_text``.

The compactor (and every other structured-output call site) routes through
this extractor. It must pick the real JSON payload even when the model wraps
it oddly — most importantly when an empty / prose-only ```json``` fence
precedes the real one, which previously produced a char-0 "Expecting value"
parse failure and silently dropped compacted directives.
"""

from app.services.structured_output import extract_json_text


def test_plain_json_passthrough():
    assert extract_json_text('{"a": 1}') == '{"a": 1}'


def test_single_valid_fence():
    raw = '```json\n{"a": 1}\n```'
    assert extract_json_text(raw) == '{"a": 1}'


def test_empty_first_fence_then_valid_fence():
    """The case: an empty ```json``` stub before the real fence."""
    raw = '```json\n```\n\n```json\n{"directives": ["x"], "summary": "s"}\n```'
    out = extract_json_text(raw)
    assert out.startswith("{")
    assert '"directives"' in out
    assert '"x"' in out


def test_prose_only_first_fence_then_valid_fence():
    raw = '```\nhere is your answer\n```\n```json\n{"a": 2}\n```'
    assert extract_json_text(raw) == '{"a": 2}'


def test_prose_and_json_inside_one_fence():
    raw = '```\nSure, here you go:\n{"a": 3}\n```'
    assert extract_json_text(raw) == '{"a": 3}'


def test_prose_preamble_unfenced():
    raw = 'Here is the result:\n{"a": 4}'
    assert extract_json_text(raw) == '{"a": 4}'


def test_first_valid_fence_wins_over_later():
    raw = '```json\n{"a": 1}\n```\n```json\n{"a": 2}\n```'
    assert extract_json_text(raw) == '{"a": 1}'


def test_no_json_returns_text_as_is():
    raw = "no json here at all"
    assert extract_json_text(raw) == "no json here at all"
