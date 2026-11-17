"""Unit tests for the reasoning details panel metadata layer.

Covers ``_build_message_response`` deserialization of the new
``tool_call_trace``, ``is_truncated`` and ``reasoning`` fields, plus the
``_deserialize_tool_call_trace`` helper directly so malformed-entry tolerance
is asserted independently of the message-response shape.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.api.v1.chat import _build_message_response, _deserialize_tool_call_trace


def _make_message(metadata: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        role="assistant",
        content="Hello",
        channel="web",
        channel_message_id=None,
        sequence=1,
        created_at=datetime.now(UTC),
        metadata_json=json.dumps(metadata) if metadata is not None else None,
        llm_trace_id=None,
    )


# ----------------------------------------------------------------------
# _build_message_response — new fields
# ----------------------------------------------------------------------


def test_build_message_response_deserializes_tool_call_trace() -> None:
    msg = _make_message(
        {
            "tool_call_trace": [
                {
                    "name": "search_code",
                    "input": '{"query":"x"}',
                    "output": "47 matches found",
                    "success": True,
                },
                {
                    "name": "create_pr",
                    "input": "{}",
                    "output": "permission denied",
                    "success": False,
                },
            ]
        }
    )

    response = _build_message_response(msg)

    assert response.tool_call_trace is not None
    assert len(response.tool_call_trace) == 2
    assert response.tool_call_trace[0].name == "search_code"
    assert response.tool_call_trace[0].input == '{"query":"x"}'
    assert response.tool_call_trace[0].output == "47 matches found"
    assert response.tool_call_trace[0].success is True
    assert response.tool_call_trace[1].success is False


def test_build_message_response_propagates_is_truncated_true() -> None:
    msg = _make_message({"is_truncated": True})
    response = _build_message_response(msg)
    assert response.is_truncated is True


def test_build_message_response_is_truncated_false_collapses_to_none() -> None:
    """A stored ``is_truncated: false`` is indistinguishable from absence in the API."""
    msg = _make_message({"is_truncated": False})
    response = _build_message_response(msg)
    assert response.is_truncated is None


def test_build_message_response_serves_the_interrupted_marker() -> None:
    """The reply a turn cut by a server restart stores."""
    assert _build_message_response(_make_message({"interrupted": True})).interrupted is True
    assert _build_message_response(_make_message({"tool_names": ["search"]})).interrupted is None


def test_build_message_response_missing_trace_is_none() -> None:
    msg = _make_message({"tool_names": ["search"], "tool_call_count": 1})
    response = _build_message_response(msg)
    assert response.tool_call_trace is None
    assert response.is_truncated is None
    assert response.tool_names == ["search"]
    assert response.llm_trace_id is None


def test_build_message_response_includes_llm_trace_id() -> None:
    trace_id = uuid4()
    msg = _make_message({"tool_names": ["search"]})
    msg.llm_trace_id = trace_id
    response = _build_message_response(msg)
    assert response.llm_trace_id == str(trace_id)


def test_build_message_response_empty_metadata_is_none() -> None:
    msg = _make_message(None)
    response = _build_message_response(msg)
    assert response.tool_call_trace is None
    assert response.is_truncated is None


# ----------------------------------------------------------------------
# _build_message_response — reasoning metadata
# ----------------------------------------------------------------------


def test_build_message_response_passes_reasoning_string_through() -> None:
    msg = _make_message({"reasoning": "Let me check the ARR cards first."})
    response = _build_message_response(msg)
    assert response.reasoning == "Let me check the ARR cards first."


def test_build_message_response_blank_reasoning_collapses_to_none() -> None:
    """Empty or whitespace-only reasoning is indistinguishable from absence."""
    for blank in ("", "   ", "\n\t"):
        msg = _make_message({"reasoning": blank})
        response = _build_message_response(msg)
        assert response.reasoning is None


def test_build_message_response_missing_reasoning_is_none() -> None:
    msg = _make_message({"tool_names": ["search"]})
    response = _build_message_response(msg)
    assert response.reasoning is None


def test_build_message_response_non_string_reasoning_collapses_to_none() -> None:
    """Malformed metadata must not crash message serialisation."""
    for malformed in (42, ["a", "b"], {"text": "x"}, True):
        msg = _make_message({"reasoning": malformed})
        response = _build_message_response(msg)
        assert response.reasoning is None


# ----------------------------------------------------------------------
# _deserialize_tool_call_trace — malformed-entry tolerance
# ----------------------------------------------------------------------


def test_deserialize_tool_call_trace_skips_non_dict_entries() -> None:
    raw = [
        {"name": "ok", "input": "{}", "output": "fine", "success": True},
        "not a dict",
        42,
        None,
    ]
    result = _deserialize_tool_call_trace(raw)
    assert result is not None
    assert len(result) == 1
    assert result[0].name == "ok"


def test_deserialize_tool_call_trace_handles_missing_fields() -> None:
    """Missing fields default to safe values rather than raising."""
    raw = [{}]  # no name, no input, no output, no success
    result = _deserialize_tool_call_trace(raw)
    assert result is not None
    assert result[0].name == ""
    assert result[0].input == ""
    assert result[0].output == ""
    assert result[0].success is True  # default per the schema


def test_deserialize_tool_call_trace_returns_none_for_non_list() -> None:
    assert _deserialize_tool_call_trace(None) is None
    assert _deserialize_tool_call_trace({}) is None
    assert _deserialize_tool_call_trace("not a list") is None
    assert _deserialize_tool_call_trace(42) is None


def test_deserialize_tool_call_trace_returns_none_for_empty_list() -> None:
    assert _deserialize_tool_call_trace([]) is None


def test_deserialize_tool_call_trace_returns_none_when_all_entries_invalid() -> None:
    """Non-dict-only input yields no valid entries → None, not an empty list."""
    raw = ["junk", 1, None]
    assert _deserialize_tool_call_trace(raw) is None
