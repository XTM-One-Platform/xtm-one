"""Tests for the OpenAEV/Symfony bean-validation error extraction in
``app.integrations.http``.

OpenAEV serializes a bean-validation 400 as a ``ValidationErrorBag``: the
top-level ``message`` stays a generic "Validation Failed" and the actionable
per-field reasons are nested under ``errors.children.<field>.errors[]``. The
extractor must walk that tree and surface ``field: reason`` so a fork/finalize
400 is self-correctable instead of opaque - and array elements must read as
``field[0]`` (the caller's own JSON shape), not a dot-joined ``field.0``.
"""

import httpx

from app.integrations.http import (
    _collect_validation_children,
    _extract_error_detail,
)


def _bag() -> dict:
    return {
        "children": {
            "action_output_parsers": {
                "children": {
                    "0": {
                        "children": {
                            "contract_output_element_rule": {
                                "errors": ["must not be blank"],
                            }
                        }
                    }
                }
            }
        }
    }


def test_collect_validation_children_uses_bracket_index_for_array_elements():
    reasons = _collect_validation_children(_bag())
    assert reasons == [
        "action_output_parsers[0].contract_output_element_rule: must not be blank"
    ]


def test_collect_validation_children_handles_top_level_errors_list():
    reasons = _collect_validation_children(
        {"children": {"action_name": {"errors": ["must not be blank"]}}}
    )
    assert reasons == ["action_name: must not be blank"]


def test_collect_validation_children_ignores_non_dict_input():
    assert _collect_validation_children("nope") == []
    assert _collect_validation_children(None) == []


def test_extract_error_detail_surfaces_symfony_validation_bag():
    resp = httpx.Response(
        400,
        json={
            "code": 400,
            "message": "Validation Failed",
            "errors": _bag(),
        },
    )
    detail = _extract_error_detail(resp)
    # The nested per-field reason is surfaced with a bracketed array index...
    assert "action_output_parsers[0].contract_output_element_rule" in detail
    assert "must not be blank" in detail
    # ...and the generic top-level message never shadows it.
    assert detail != "Validation Failed"


def test_extract_error_detail_prefers_errors_array_when_present():
    # The existing GitHub/HubSpot-style ``errors`` ARRAY path is unchanged: a
    # list still wins and the object-bag walk is only a fallback.
    resp = httpx.Response(
        422,
        json={
            "message": "Validation Failed",
            "errors": [{"message": "name is required"}],
        },
    )
    detail = _extract_error_detail(resp)
    assert "name is required" in detail
