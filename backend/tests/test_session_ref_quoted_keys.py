"""Session references: quoted bracket keys and the unresolved-reference error.

A key holding a dash, dot, colon or space (a STIX ``extension-definition--UUID``
key) is addressed with ``["key"]`` or ``['key']``; the error of a reference
that does not resolve names the variable, the failing segment and the syntax.
"""

import time

import pytest

from app.tools.agent_loop import (
    _navigate_session_path,
    _resolve_session_reference,
    _session_reference_error,
)

_EXT = "extension-definition--ea279b3e-5c71-4632-ac08-831c66a786ba"
_BUNDLE = {
    "type": "bundle",
    "objects": [
        {
            "type": "report",
            "extensions": {_EXT: {"content": "<p>x</p>"}},
            "x_odd": {"a.b": 1, 'say "hi"': 2, "it's": 3, "back\\slash": 4},
        }
    ],
}
_INPUT = {"$input": _BUNDLE}


@pytest.mark.parametrize(
    "ref",
    [
        f'$input.objects[0].extensions["{_EXT}"].content',
        f"$input.objects[0].extensions['{_EXT}'].content",
    ],
)
def test_quoted_bracket_key_resolves_a_dashed_key(ref):
    assert _resolve_session_reference(ref, _INPUT) == (True, True, "<p>x</p>")


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ('.objects[0].x_odd["a.b"]', 1),
        ('.objects[0].x_odd["say \\"hi\\""]', 2),
        (".objects[0].x_odd['it\\'s']", 3),
        ('.objects[0].x_odd["it\'s"]', 3),
        ('.objects[0].x_odd["back\\\\slash"]', 4),
        ('["objects"][0]["type"]', "report"),
    ],
)
def test_quoted_key_with_a_dot_or_an_escaped_quote(path, value):
    assert _navigate_session_path(_BUNDLE, path) == (True, value)


def test_dotted_dashed_key_still_fails_and_the_error_suggests_the_bracket_form():
    ref = "$input.objects[0].extensions.extension-definition--x.content"
    assert _resolve_session_reference(ref, _INPUT) == (True, False, None)
    error = _session_reference_error(ref, "body", _INPUT)
    assert "malformed segment '.extension-definition--x'" in error
    assert '["extension-definition--x"]' in error
    assert "NOT executed" in error


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ('$input.objects[0].extensions["nope"].content', "key 'nope' not found at '$input.objects[0].extensions'"),
        ("$input.objects[5].type", "index [5] out of range at '$input.objects' (1 item(s))"),
        ('$input.objects[0].type["x"]', "'$input.objects[0].type' is a str, it has no key 'x'"),
        ('$input.objects[0].extensions["unterminated', "malformed segment"),
    ],
)
def test_unknown_key_or_index_errors_with_the_failing_segment(ref, expected):
    assert _resolve_session_reference(ref, _INPUT) == (True, False, None)
    assert expected in _session_reference_error(ref, "body", _INPUT)


def test_input_error_never_mentions_collected():
    ref = "$input.objects[9].name"
    error = _session_reference_error(ref, "name", {**_INPUT, "$collected": {"rules": []}})
    assert "Path syntax: $input.key" in error
    assert '$input["any-key"]' in error
    assert "$collected" not in error
    assert "collect_task_results" not in error


def test_collected_error_points_to_the_collect_output():
    session = {"$collected": {"rules": [{"name": "r"}]}}
    error = _session_reference_error("$collected.rules[3].name", "name", session)
    assert "index [3] out of range at '$collected.rules'" in error
    assert "collect_task_results" in error


def test_existing_collected_paths_unchanged():
    session = {"$collected": {"rules": {"_item_0": {"name": "rule-0"}, "_item_1": {"name": "rule-1"}}}}
    assert _resolve_session_reference("$collected.rules[0].name", session) == (True, True, "rule-0")
    assert _resolve_session_reference("$collected.rules[1].name", session) == (True, True, "rule-1")
    assert _resolve_session_reference('$collected.rules["_item_1"].name', session) == (True, True, "rule-1")


def test_hostile_quoted_key_is_linear():
    hostile = '.x["' + "\\a" * 20000 + "a" * 20000
    started = time.perf_counter()
    assert _navigate_session_path({"x": {}}, hostile) == (False, None)
    assert time.perf_counter() - started < 1.0
