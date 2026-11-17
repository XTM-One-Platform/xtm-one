"""The pure half of conversation references: the ``@key``, what a
request names, what a stored message holds. The database half is in
``test_conversation_references_postgres``."""

from __future__ import annotations

import json
import uuid

from app.services.conversation_references import (
    CONVERSATION_REFS_META_KEY,
    ReferencedConversation,
    ReferenceResolution,
    conversation_reference_key,
    parse_referenced_conversation_ids,
    read_conversation_refs,
    with_conversation_refs,
)


def test_a_key_is_the_title_as_a_slug():
    cid = uuid.UUID("3f2a9c00-0000-4000-8000-000000000000")
    assert conversation_reference_key("Q3 Threat Report!", cid) == "q3-threat-report"
    assert conversation_reference_key("Réunion d'équipe", cid) == "reunion-d-equipe"
    assert conversation_reference_key("脅威レポート", cid) == "conversation-3f2a9c"
    assert conversation_reference_key(None, cid) == "conversation-3f2a9c"
    long = conversation_reference_key("word " * 30, cid)
    assert len(long) <= 48 and not long.endswith("-") and long.endswith("word")


def test_a_request_names_ids_in_order_once_and_nothing_else():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert parse_referenced_conversation_ids([str(a), "junk", 3, str(b), str(a).upper()]) == [a, b]
    assert parse_referenced_conversation_ids(str(a)) == []
    assert parse_referenced_conversation_ids(None) == []
    assert len(parse_referenced_conversation_ids([str(uuid.uuid4()) for _ in range(50)])) == 20


def _resolution(cid: uuid.UUID) -> ReferenceResolution:
    return ReferenceResolution(
        references=[
            ReferencedConversation(
                id=cid, title="Q3 threat report", key="q3-threat-report", is_own=True,
                owner_name=None, agent_name=None, updated_at=None,
            )
        ]
    )


def test_the_references_join_a_message_s_metadata_and_read_back_validated():
    cid = uuid.uuid4()
    meta = json.loads(with_conversation_refs(json.dumps({"attachments": [{"file_id": "f"}]}), _resolution(cid)))
    assert meta["attachments"] == [{"file_id": "f"}]
    assert read_conversation_refs(meta) == [
        {"conversation_id": str(cid), "title": "Q3 threat report", "key": "q3-threat-report"}
    ]
    assert with_conversation_refs(None, ReferenceResolution()) is None
    assert with_conversation_refs("{}", ReferenceResolution()) == "{}"
    # A stored value that is not what the platform writes reads as nothing,
    # and a key that is not a slug is rebuilt from the title.
    assert read_conversation_refs({CONVERSATION_REFS_META_KEY: "x"}) == []
    assert read_conversation_refs(
        {CONVERSATION_REFS_META_KEY: [{"conversation_id": "nope"}, {"conversation_id": str(cid), "title": "A B", "key": "<b>"}]}
    ) == [{"conversation_id": str(cid), "title": "A B", "key": "a-b"}]


def test_references_next_to_approval_decisions_are_refused():
    """A resume of a gated turn stores no new user message, so the
    references it would carry have nowhere to be recorded and the brief nowhere
    to be read: refused, like files, rather than dropped."""
    import pytest
    from pydantic import ValidationError

    from app.api.v1.chat import SendMessageRequest

    decisions = [{"tool_call_id": "a", "decision": "approve"}]
    with pytest.raises(ValidationError, match="approval decision"):
        SendMessageRequest(decisions=decisions, referenced_conversation_ids=[str(uuid.uuid4())])
    assert SendMessageRequest(decisions=decisions, referenced_conversation_ids=[]).decisions
    assert SendMessageRequest(content="see @q3", referenced_conversation_ids=[str(uuid.uuid4())]).content
