"""Unit tests for chat message feedback response enrichment."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.api.v1.chat import _build_message_response


def _make_message() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        role="assistant",
        content="Hello",
        channel="web",
        channel_message_id=None,
        sequence=1,
        created_at=datetime.now(UTC),
        metadata_json='{"tool_names":["search_web"],"tool_call_count":1}',
        llm_trace_id=None,
    )


def test_build_message_response_includes_user_feedback():
    msg = _make_message()
    feedback = SimpleNamespace(rating="negative", comment="Wrong answer")

    response = _build_message_response(msg, feedback)

    assert response.user_feedback is not None
    assert response.user_feedback.rating == "negative"
    assert response.user_feedback.comment == "Wrong answer"


def test_build_message_response_without_feedback_is_null():
    msg = _make_message()

    response = _build_message_response(msg)

    assert response.user_feedback is None
