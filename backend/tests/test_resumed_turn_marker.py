"""The marker of a reply the platform resumed by itself.

``parent_wake.resume_cut_turn`` stores ``cut_turn_resume.resumed_from_meta``
on the reply of a resumed turn; the message API serves it as
``resumed_from`` so the transcript can say so after a reload. A reply that was
never resumed, or one whose record is malformed, carries none.
"""

from __future__ import annotations

import datetime
import json
import uuid

import pytest

from app.api.v1.chat import _build_message_response
from app.services.cut_turn_resume import CutTurn, resumed_from_meta


class _StubMessage:
    """The attributes ``_build_message_response`` reads, without a DB row."""

    def __init__(self, metadata_json: str | None):
        self.id = uuid.uuid4()
        self.conversation_id = uuid.uuid4()
        self.role = "assistant"
        self.content = "the answer"
        self.channel = "web"
        self.channel_message_id = None
        self.sequence = 3
        self.created_at = datetime.datetime.now(datetime.UTC)
        self.metadata_json = metadata_json
        self.llm_trace_id = None


def _cut(*, resumes: int, cause: str) -> CutTurn:
    return CutTurn(
        conversation_id=str(uuid.uuid4()),
        user_id=str(uuid.uuid4()),
        user_message_id=str(uuid.uuid4()),
        turn_id="turn-1",
        cause=cause,
        resumes=resumes,
        cut_at="2026-11-02T12:30:00+00:00",
    )


def test_a_resumed_reply_carries_its_marker():
    meta = resumed_from_meta(_cut(resumes=1, cause="recycle"), resumed_as=None)
    resp = _build_message_response(_StubMessage(json.dumps(meta)))
    assert resp.resumed_from is not None
    assert resp.resumed_from.cause == "recycle"
    assert resp.resumed_from.resume == 2
    assert resp.resumed_from.of == meta["resumed_from"]["of"]
    assert resp.resumed_from.cut_at == "2026-11-02T12:30:00+00:00"


def test_a_reply_never_resumed_carries_none():
    assert _build_message_response(_StubMessage(None)).resumed_from is None
    assert _build_message_response(_StubMessage(json.dumps({"tool_call_count": 3}))).resumed_from is None


@pytest.mark.parametrize("raw", ["yes", 3, ["stop"], None])
def test_a_malformed_record_carries_none(raw):
    resp = _build_message_response(_StubMessage(json.dumps({"resumed_from": raw})))
    assert resp.resumed_from is None


def test_malformed_fields_are_dropped_not_raised():
    raw = {"cause": 7, "resume": True, "of": "20", "cut_at": ""}
    resp = _build_message_response(_StubMessage(json.dumps({"resumed_from": raw})))
    assert resp.resumed_from is not None
    assert resp.resumed_from.model_dump() == {"cause": None, "resume": None, "of": None, "cut_at": None}
