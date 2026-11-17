"""Two chat turns of one agent share a byte-identical cacheable system prefix.

The Anthropic provider caches the system prompt up to its clock line
(``split_system_prompt``). A section that changes from one call to the next and
sits before that line moves the cut on every call: the second run of an agent
called twice through the API reads only the tool schemas and the persona from
the cache. Every per-call section (message context, account brief, thread and
reply-mode blocks, page context) is written after the clock line (#11971).

Driven through ``run_agent_chat`` on PostgreSQL with the model side stubbed
(``test_memory_quarantine_chat_postgres._system_prompt``).
"""

from __future__ import annotations

import pytest

import tests.test_memory_quarantine_chat_postgres as chat_prompt
from app.llm.system_prompt_split import SYSTEM_VOLATILE_TAIL_MARKER, split_system_prompt

pytestmark = pytest.mark.integration


def _call(conversation_id: str, message_id: str, brief: str) -> dict:
    return {
        "platform": "teams",
        "channel_type": "personal",
        "is_direct_message": True,
        "conversation_id": conversation_id,
        "message_id": message_id,
        "chat_id": f"19:{conversation_id}@thread.v2",
        "account_brief": brief,
        "response_policy": "always",
    }


@pytest.fixture
def _clocked(monkeypatch):
    """The helper's frozen clock, written with the marker that opens the tail."""
    clocks = iter(["Sunday at 04:14 UTC", "Sunday at 04:15 UTC"])
    real_stub = chat_prompt._stub_turn

    def _stub(mp):
        measured = real_stub(mp)
        mp.setattr(
            "app.services.builtin_tools.prompting.format_datetime_prompt",
            lambda *_a, **_k: f"\n{SYSTEM_VOLATILE_TAIL_MARKER}{next(clocks)}",
        )
        return measured

    monkeypatch.setattr(chat_prompt, "_stub_turn", _stub)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_clocked")
async def test_two_calls_with_different_contexts_share_the_stable_prefix(monkeypatch):
    first = await chat_prompt._system_prompt(
        monkeypatch, memory=True, context=_call("conv-aaaa1111", "msg-0001", "Speaks for the CTI desk, run one.")
    )
    second = await chat_prompt._system_prompt(
        monkeypatch, memory=True, context=_call("conv-bbbb2222", "msg-0002", "Speaks for the CTI desk, run two.")
    )

    stable_1, volatile_1 = split_system_prompt(first)
    stable_2, volatile_2 = split_system_prompt(second)
    assert volatile_1 and volatile_2
    assert stable_1 == stable_2
    assert stable_1.startswith(chat_prompt._PERSONA)
    assert "── Agent ──" in stable_1
    for per_call in ("conv-aaaa1111", "msg-0001", "run one", "── Message Context ──", "── Account ──", "MUST respond"):
        assert per_call not in stable_1, per_call
        assert per_call in volatile_1, per_call
    assert "conv-bbbb2222" in volatile_2 and "run two" in volatile_2
