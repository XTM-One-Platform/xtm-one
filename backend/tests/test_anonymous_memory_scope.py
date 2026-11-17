"""Anonymous conversation scopes must never reach the durable memory store.

For anonymous external-channel users, agent memory is scoped to the
CONVERSATION id (``memory_scope_id = conversation_id``) so context stays
within the conversation. That scope id is valid for the Redis tiers, but
``agent_memory_entries.user_id`` has a foreign key to ``users`` — passing a
conversation UUID there raises ``ForeignKeyViolationError`` in production,
and silently persisting agent-level instead would leak
anonymous conversation content into the shared memory every user sees.

These tests pin the two fixes:

* ``consolidate_memory(scope_is_user_id=False)`` still updates the Redis
  medium-term summary but SKIPS the PostgreSQL persist step entirely.
* The chat layer passes ``scope_is_user_id=not is_anonymous_user`` and does
  not offer the ``store_memory`` tool to anonymous sessions at all.
"""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import agent_memory as agent_memory_mod

AGENT_ID = "00000000-0000-0000-0000-000000000001"
CONVERSATION_SCOPE = "4a04edfe-c9aa-4b1c-a6f4-61d5fd721af3"  # not a user id
USER_SCOPE = "00000000-0000-0000-0000-0000000000aa"


class _FakeRedis:
    def __init__(self) -> None:
        self.set_calls: list[tuple] = []

    async def get(self, *_args, **_kwargs):
        return str(agent_memory_mod.CONSOLIDATION_THRESHOLD + 1)

    async def set(self, *args, **kwargs):
        self.set_calls.append(args)
        return None

    async def exists(self, *_keys):
        return 0


class _FakeSession:
    """Records whether the durable-persistence session was ever opened."""

    opened = False

    async def __aenter__(self):
        type(self).opened = True
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        return None


async def _run_consolidation(*, scope_id: str, scope_is_user_id: bool):
    """Run consolidate_memory with all external seams stubbed.

    Returns ``(persist_mock, fake_redis, session_opened)``.
    """
    _FakeSession.opened = False
    fake_redis = _FakeRedis()

    async def fake_load_memory(_agent_id: str, *, scope_id: str | None = None):
        return {
            "short_term": [
                {
                    "source": "chat",
                    "source_type": "chat",
                    "summary": "Anonymous slack user asked about OpenCTI cases.",
                    "timestamp": "2026-07-02T12:00:00",
                }
            ],
            "medium_term": "",
        }

    async def fake_redis_client():
        return fake_redis

    fake_llm = SimpleNamespace(
        chat=AsyncMock(return_value=SimpleNamespace(content="updated memory body"))
    )

    persist_mock = AsyncMock(return_value=None)
    with (
        patch.object(agent_memory_mod, "load_memory", side_effect=fake_load_memory),
        patch.object(agent_memory_mod, "redis_client", side_effect=fake_redis_client),
        patch("app.services.persistent_memory.persist_memory", new=persist_mock),
        patch("app.core.database.async_session", new=_FakeSession),
    ):
        await agent_memory_mod.consolidate_memory(
            AGENT_ID,
            fake_llm,
            force=True,
            scope_id=scope_id,
            scope_is_user_id=scope_is_user_id,
        )
    return persist_mock, fake_redis, _FakeSession.opened


@pytest.mark.asyncio
async def test_anonymous_scope_skips_durable_persistence():
    """scope_is_user_id=False -> Redis medium-term updated, NO DB persist."""
    persist_mock, fake_redis, session_opened = await _run_consolidation(
        scope_id=CONVERSATION_SCOPE, scope_is_user_id=False
    )

    persist_mock.assert_not_awaited()
    assert not session_opened, (
        "consolidate_memory must not even open a DB session for an "
        "anonymous conversation scope"
    )
    # The Redis medium-term summary must still be written (first set call is
    # the medium-term key; the counter reset follows).
    assert fake_redis.set_calls, "Redis medium-term summary must still be updated"


@pytest.mark.asyncio
async def test_user_scope_still_persists_durably():
    """scope_is_user_id=True keeps the pre-existing durable persist."""
    persist_mock, _fake_redis, _ = await _run_consolidation(
        scope_id=USER_SCOPE, scope_is_user_id=True
    )

    persist_mock.assert_awaited_once()
    assert persist_mock.await_args.kwargs["user_id"] == USER_SCOPE


@pytest.mark.parametrize(("is_anonymous", "scope_is_user_id"), [(True, False), (False, True)])
async def test_chat_passes_scope_is_user_id_flag(monkeypatch, is_anonymous, scope_is_user_id):
    """The chat memory write must translate anonymity into the
    scope_is_user_id flag when scheduling consolidation."""
    import asyncio

    import app.services.agent_chat as agent_chat_module

    consolidate = AsyncMock()
    monkeypatch.setattr(agent_memory_mod, "store_short_term_memory", AsyncMock(return_value=99))
    monkeypatch.setattr(agent_memory_mod, "consolidate_memory", consolidate)

    await agent_chat_module._store_chat_memories(
        "summary",
        agent_id=AGENT_ID,
        agent_name="Agent",
        user_id=CONVERSATION_SCOPE if is_anonymous else USER_SCOPE,
        scope_id=CONVERSATION_SCOPE if is_anonymous else USER_SCOPE,
        is_anonymous=is_anonymous,
        is_company_managed=False,
        store_short=True,
        store_medium=True,
        store_user=False,
        llm=object(),
        model=None,
    )
    await asyncio.sleep(0)

    assert consolidate.await_args.kwargs["scope_is_user_id"] is scope_is_user_id


def test_store_memory_tool_not_offered_to_anonymous_sessions():
    """The durable store_memory tool must be gated on an identified user —
    for anonymous sessions the scope is a conversation id that would violate
    the users FK (and anonymous memory must stay Redis-only)."""
    import app.services.agent_chat as agent_chat_module

    source = inspect.getsource(agent_chat_module)
    store_pos = source.find("build_memory_store_tool(")
    # Skip past the import to the actual call site.
    call_pos = source.find("build_memory_store_tool(", store_pos + 1)
    target = call_pos if call_pos != -1 else store_pos
    assert target != -1
    gate_pos = source.rfind("if not is_anonymous_user:", 0, target)
    assert gate_pos != -1 and target - gate_pos < 800, (
        "build_memory_store_tool must be gated on `not is_anonymous_user` "
        "in agent_chat"
    )
