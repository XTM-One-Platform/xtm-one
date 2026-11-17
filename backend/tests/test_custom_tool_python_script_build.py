"""Tests for Python custom-tool script generation (input injection)."""

from __future__ import annotations

import ast

from app.services.custom_tool_executor import _build_python_script


def test_build_python_script_is_valid_python_with_bool_and_null_inputs():
    """JSON ``true`` / ``false`` / ``null`` must not leak into the script.

    A naive ``json.dumps`` splice would emit invalid Python literals and
    break every tool invoked with boolean or null inputs — including the
    workspace wrapper handoff via ``__xtm_tool_inputs__``.
    """
    script = _build_python_script(
        "print(flag, missing, argv)",
        {"flag": True, "missing": None, "argv": ["a", 1], "n": 2.5},
    )
    # Must parse as valid Python (would raise SyntaxError otherwise) and not
    # reference undefined ``true`` / ``null`` names.
    ast.parse(script)
    assert "= true" not in script
    assert "= null" not in script
    assert "__xtm_tool_inputs__" in script


def test_build_python_script_binds_each_input_variable():
    script = _build_python_script("print(city)", {"city": "Paris", "with space": 1})
    ast.parse(script)
    assert 'city = __xtm_tool_inputs__["city"]' in script
    assert 'with_space = __xtm_tool_inputs__["with space"]' in script


def test_build_python_script_handles_hostile_input_keys():
    """Keys that sanitize to invalid identifiers (digit-leading, empty,
    Python keywords) must not produce a SyntaxError — that would kill the
    whole script including the ``__xtm_tool_inputs__`` source of truth."""
    script = _build_python_script(
        "print(__xtm_tool_inputs__)",
        {"1st": "a", "": "b", "class": "c", "if": "d"},
    )
    ast.parse(script)
    assert '_1st = __xtm_tool_inputs__["1st"]' in script
    assert '_ = __xtm_tool_inputs__[""]' in script
    assert 'class_ = __xtm_tool_inputs__["class"]' in script
    assert 'if_ = __xtm_tool_inputs__["if"]' in script


def test_build_python_script_deduplicates_colliding_keys():
    """Distinct keys collapsing to the same identifier must not silently
    overwrite each other — later ones get a numeric suffix."""
    script = _build_python_script("print(a_b)", {"a b": 1, "a-b": 2, "a.b": 3})
    ast.parse(script)
    assert 'a_b = __xtm_tool_inputs__["a b"]' in script
    assert 'a_b_2 = __xtm_tool_inputs__["a-b"]' in script
    assert 'a_b_3 = __xtm_tool_inputs__["a.b"]' in script
