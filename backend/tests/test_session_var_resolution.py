"""$collected session-variable reference resolution.

Regression coverage for the bug where a tool argument like
``$collected.rules[9].name`` was passed through to the target system VERBATIM
(creating an OpenCTI indicator literally named ``$collected.rules[9].name``)
because only the exact whole-value match (``$collected``) was resolved.
"""

import pytest

from app.tools.agent_loop import (
    _navigate_session_path,
    _resolve_session_reference,
)

_COLLECTED = {
    "rules": [
        {"name": "rule-0", "description": "desc-0", "pattern": "p0", "score": 80},
        {"name": "rule-1", "description": "desc-1", "pattern": "p1", "score": 85},
    ],
    "meta": {"count": 2},
}


# Mirrors the REAL ``collect_task_results`` output shape: items are merged into
# an insertion-ordered *dict* keyed by item id (or ``_item_N`` when an item
# carries no id) so duplicates across tasks collapse and bulk consumers like
# ``import_opencti_dashboard`` (which require ``widgets`` to be an object) keep
# working. Per-item ``[i]`` navigation must still work against this shape.
_COLLECTED_DICT = {
    "rules": {
        "_item_0": {"name": "rule-0", "description": "desc-0", "pattern": "p0", "score": 80},
        "_item_1": {"name": "rule-1", "description": "desc-1", "pattern": "p1", "score": 85},
    },
    "meta": {"count": 2},
}


@pytest.mark.parametrize(
    ("path", "ok", "value"),
    [
        (".rules[0].name", True, "rule-0"),
        (".rules[1].pattern", True, "p1"),
        (".rules[1].score", True, 85),
        (".meta.count", True, 2),
        (".rules[0]", True, {"name": "rule-0", "description": "desc-0", "pattern": "p0", "score": 80}),
        # A positional ``[i]`` resolves against a dict by insertion order too
        # (single-key ``meta`` -> index 0 is its only value).
        (".meta[0]", True, 2),
        # Out-of-range index, missing key, index into a scalar, trailing junk.
        (".rules[9].name", False, None),
        (".rules[0].missing", False, None),
        (".meta.count[0]", False, None),
        (".rules[0].name extra", False, None),
        (".rules.name", False, None),
    ],
)
def test_navigate_session_path(path, ok, value):
    assert _navigate_session_path(_COLLECTED, path) == (ok, value)


@pytest.mark.parametrize(
    ("path", "ok", "value"),
    [
        # The producer stores items as a dict; positional ``[i]`` selects the
        # i-th item by insertion order so the persona's documented
        # ``$collected.rules[i].name`` navigation resolves.
        (".rules[0].name", True, "rule-0"),
        (".rules[1].pattern", True, "p1"),
        (".rules[1].score", True, 85),
        # Explicit dict-key navigation still works alongside positional access.
        (".rules._item_0.name", True, "rule-0"),
        # Out-of-range positional index is flagged (never silently passed).
        (".rules[9].name", False, None),
    ],
)
def test_navigate_dict_collected_positional(path, ok, value):
    assert _navigate_session_path(_COLLECTED_DICT, path) == (ok, value)


def test_resolve_whole_variable():
    session = {"$collected": _COLLECTED}
    assert _resolve_session_reference("$collected", session) == (True, True, _COLLECTED)


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("$collected.rules[0].name", "rule-0"),
        ("$collected.rules[1].description", "desc-1"),
        ("$collected.rules[1].score", 85),
    ],
)
def test_resolve_path_reference(ref, expected):
    session = {"$collected": _COLLECTED}
    is_ref, ok, value = _resolve_session_reference(ref, session)
    assert (is_ref, ok, value) == (True, True, expected)


def test_resolve_path_reference_against_real_dict_shape():
    """End-to-end: the dict shape ``collect_task_results`` actually stores must
    resolve per-item indexed references (the persona's documented flow)."""
    session = {"$collected": _COLLECTED_DICT}
    assert _resolve_session_reference("$collected.rules[0].name", session) == (
        True,
        True,
        "rule-0",
    )
    assert _resolve_session_reference("$collected.rules[1].score", session) == (
        True,
        True,
        85,
    )


def test_resolve_unresolvable_path_is_flagged_not_passed_through():
    """The key bug: an unresolvable reference must report a failure so the loop
    errors the call, never injecting the literal placeholder."""
    session = {"$collected": _COLLECTED}
    is_ref, ok, value = _resolve_session_reference("$collected.rules[9].name", session)
    assert is_ref is True
    assert ok is False
    assert value is None


def test_non_reference_is_left_untouched():
    session = {"$collected": _COLLECTED}
    assert _resolve_session_reference("a normal string", session) == (False, False, None)
    # A string that merely mentions the token but isn't a clean reference.
    assert _resolve_session_reference("see $collected for details", session) == (
        False,
        False,
        None,
    )


def test_no_session_vars():
    assert _resolve_session_reference("$collected", {}) == (False, False, None)
