"""A UTF-16 surrogate in a plain text column never breaks a write (PostgreSQL).

The driver cannot encode a surrogate as UTF-8, so a body whose ``"\\ud800"``
escape landed in an agent name, a prompt or a conversation title answered 500.
These tests drive the real routes through the ASGI app against a scratch
database (the harness of ``test_null_byte_guard_postgres.py``): the write
succeeds, a lone surrogate is stored as U+FFFD and a pair as its character,
and the response says what was stored.
"""

import pytest
from sqlalchemy import text

from tests.test_null_byte_guard_postgres import _client, _stored

pytestmark = pytest.mark.integration

EMOJI = "\U0001f600"
JSON = {"content-type": "application/json"}


async def test_an_agent_name_with_a_lone_surrogate_is_created():
    from app.models.agent import Agent

    async with _client() as (client, sessions):
        r = await client.post("/api/v1/agents", content='{"name": "Triage\\ud800 desk"}', headers=JSON)

        assert r.status_code == 201, r.text
        assert r.json()["name"] == "Triage\ufffd desk"
        assert await _stored(sessions, Agent, r.json()["id"], "name") == "Triage\ufffd desk"


async def test_a_prompt_content_with_a_surrogate_pair_keeps_its_character():
    from app.models.prompt import Prompt

    async with _client() as (client, sessions):
        # Raw bytes of a pair written one half at a time: the JSON decoder reads
        # them as two surrogates, which together are one character.
        body = b'{"name": "Summary", "content": "Done \\ud83d' + b"\xed\xb8\x80" + b'"}'
        r = await client.post("/api/v1/prompts", content=body, headers=JSON)

        assert r.status_code == 201, r.text
        assert r.json()["content"] == f"Done {EMOJI}"
        assert await _stored(sessions, Prompt, r.json()["id"], "content") == f"Done {EMOJI}"


async def test_a_conversation_title_with_a_lone_surrogate_is_created():
    from app.models.chat import Conversation

    async with _client() as (client, sessions):
        created = await client.post("/api/v1/chat/conversations", content='{"title": "Q3\\udc00"}', headers=JSON)

        assert created.status_code == 201, created.text
        assert created.json()["title"] == "Q3\ufffd"
        assert await _stored(sessions, Conversation, created.json()["id"], "title") == "Q3\ufffd"


async def test_a_raw_statement_and_an_array_bind_are_repaired():
    async with _client() as (_, sessions), sessions() as s:
        assert await s.scalar(text("SELECT CAST(:v AS text)"), {"v": "a\ud800b"}) == "a\ufffdb"
        assert await s.scalar(text("SELECT CAST(:v AS text[])"), {"v": ["\ud83d\ude00", "\udc00"]}) == [
            EMOJI,
            "\ufffd",
        ]
