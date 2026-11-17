"""A chat message referencing other conversations with @, on the real
web chat send (``run_chat_connection``) and REST ``send_message`` with the
fan-out fakes.

What a send's ``referenced_conversation_ids`` came to is
``conversation_references.resolve_referenced_conversations``'s to decide (it
is pinned against PostgreSQL in ``test_conversation_references_postgres``);
here it is stubbed, and what is pinned is what the handlers do with it: the
references are stored on the user message, the brief reaches every agent that
answers the message - the conversation's and each @mentioned one - and a
message without references is answered as before.
"""

from __future__ import annotations

import json
import uuid

import pytest

import app.services.agent_chat as agent_chat_module
from app.api.v1 import chat as chat_module
from app.services import conversation_references, mention_dispatch
from app.services.conversation_references import (
    CONVERSATION_REFS_META_KEY,
    ReferencedConversation,
    ReferenceResolution,
)
from tests.test_chat_turn_fanout import (  # noqa: F401
    _patch_general_chat,
    fake_rl,
    fanout_redis,
    ws_env,
)
from tests.test_chat_web_steering import _send
from tests.test_mention_turn import (  # noqa: F401
    B,
    _mentioned_run,
    _rest_send,
    mention_env,
)

REFERENCED = uuid.uuid4()
BRIEF = "[Conversations the user referenced in this message] (brief)"


@pytest.fixture
def references(monkeypatch):
    """Every send references REFERENCED; the resolver's calls are recorded."""
    calls: list[dict] = []
    resolution = ReferenceResolution(
        references=[
            ReferencedConversation(
                id=REFERENCED,
                title="Q3 threat report",
                key="q3-threat-report",
                is_own=True,
                owner_name=None,
                agent_name="Analyst",
                updated_at=None,
            )
        ]
    )

    async def _resolve(_db, user, raw, *, current_conversation_id):
        calls.append({"raw": raw, "current": str(current_conversation_id)})
        return resolution if raw else ReferenceResolution()

    async def _brief(_db, res):
        return BRIEF if res else None

    monkeypatch.setattr(conversation_references, "resolve_referenced_conversations", _resolve)
    monkeypatch.setattr(conversation_references, "build_reference_brief", _brief)
    return calls


def _stored_user_meta(state) -> dict:
    [kw] = [kw for role, _content, kw in state.stored if role == "user"]
    return json.loads(kw.get("metadata_json") or "{}")


async def test_a_web_send_stores_its_references_and_every_answering_agent_reads_the_brief(
    monkeypatch, mention_env, references  # noqa: F811
):
    general_calls: list[dict] = []

    async def _general(_db, _uid, _history, _content, **kwargs):
        general_calls.append(kwargs)
        return ("assistant says", None)

    _patch_general_chat(monkeypatch, _general)
    monkeypatch.setattr(agent_chat_module, "run_agent_chat", _mentioned_run(mention_env))
    await _send(
        mention_env.env,
        {
            "content": "@q3-threat-report @beta what did we decide there?",
            "mentioned_agent_ids": [B],
            "referenced_conversation_ids": [str(REFERENCED)],
        },
    )

    assert references == [{"raw": [str(REFERENCED)], "current": str(mention_env.env.conv.id)}]
    assert _stored_user_meta(mention_env)[CONVERSATION_REFS_META_KEY] == [
        {"conversation_id": str(REFERENCED), "title": "Q3 threat report", "key": "q3-threat-report"}
    ]
    # The conversation's own agent and the @mentioned one read the same brief.
    [general] = general_calls
    assert general["reference_brief"] == BRIEF
    [mentioned] = mention_env.mentioned_calls
    assert mentioned["reference_brief"] == BRIEF


async def test_a_web_send_without_references_is_answered_as_before(
    monkeypatch, mention_env, references  # noqa: F811
):
    general_calls: list[dict] = []

    async def _general(_db, _uid, _history, _content, **kwargs):
        general_calls.append(kwargs)
        return ("assistant says", None)

    monkeypatch.setattr(chat_module, "resolve_mention_targets", mention_dispatch.resolve_mention_targets)
    _patch_general_chat(monkeypatch, _general)
    await _send(mention_env.env, {"content": "what did we decide?"})

    [general] = general_calls
    assert general["reference_brief"] is None
    assert CONVERSATION_REFS_META_KEY not in _stored_user_meta(mention_env)


async def test_a_rest_send_returns_its_references_and_the_agent_reads_the_brief(
    monkeypatch, mention_env, references  # noqa: F811
):
    general_calls: list[dict] = []

    async def _general(_db, _uid, _history, _content, **kwargs):
        general_calls.append(kwargs)
        return ("assistant says", None)

    monkeypatch.setattr(chat_module, "resolve_mention_targets", mention_dispatch.resolve_mention_targets)
    _patch_general_chat(monkeypatch, _general)
    response = await _rest_send(
        mention_env.env,
        "@q3-threat-report what did we decide there?",
        referenced_conversation_ids=[str(REFERENCED)],
    )

    [general] = general_calls
    assert general["reference_brief"] == BRIEF
    assert response.user_message.conversation_refs == [
        {"conversation_id": str(REFERENCED), "title": "Q3 threat report", "key": "q3-threat-report"}
    ]
    assert _stored_user_meta(mention_env)[CONVERSATION_REFS_META_KEY][0]["conversation_id"] == str(REFERENCED)
