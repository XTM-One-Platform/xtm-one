"""Regression tests for the additive failure-envelope on SendMessageResponse.

Context: on an Enterprise Edition inherited from a connected EE platform (the
``ee_platform`` agentic source, included allowance) a Filigran AI extraction
can be refused (licence/quota). The refusal used to reach OpenCTI's import-doc
as an HTTP 200 with the real reason buried inside the assistant text, so the
connector reported a misleading "empty response". The messages endpoint now
carries a machine-readable ``error`` / ``error_code`` envelope.

These tests pin the two guarantees that matter:
  1. The change is *non-breaking*: a success payload (no error fields) still
     validates and both fields default to ``None``.
  2. When populated, the envelope round-trips and HTTP status is unaffected
     (the model has no status field — the caller keeps returning 200).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.api.v1.chat import MessageResponse, SendMessageResponse


def _message(role: str, content: str) -> MessageResponse:
    return MessageResponse(
        id=str(uuid.uuid4()),
        role=role,
        content=content,
        channel="web",
        channel_message_id=None,
        sequence=1,
        created_at=datetime.now(UTC).isoformat(),
        attachments=[],
        llm_trace_id=None,
    )


def test_success_payload_omits_error_envelope():
    """A normal response builds without error fields — additive/non-breaking."""
    resp = SendMessageResponse(
        user_message=_message("user", "hi"),
        assistant_message=_message("assistant", "hello"),
        conversation_id=str(uuid.uuid4()),
    )
    assert resp.error is None
    assert resp.error_code is None
    dumped = resp.model_dump()
    assert dumped["error"] is None
    assert dumped["error_code"] is None


def test_error_envelope_round_trips():
    """A refused extraction carries the real cause without changing status."""
    detail = "Enterprise licence required for this feature."
    resp = SendMessageResponse(
        user_message=_message("user", "extract"),
        assistant_message=_message("assistant", detail),
        conversation_id=str(uuid.uuid4()),
        error=detail,
        error_code="extraction_failed",
    )
    assert resp.error == detail
    assert resp.error_code == "extraction_failed"
    # No status field exists on the model: the endpoint keeps returning 200,
    # so existing OpenCTI/connector versions are unaffected.
    assert "status" not in resp.model_dump()
