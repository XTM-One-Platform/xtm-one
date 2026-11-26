"""Automatic memory self-healing.

Oversized agent memory used to be truncated with a worker log instructing
operators to run memory maintenance manually (or clear the agent's memory)
— instructions no user ever sees.  These tests pin the self-healing fixes:

* ``consolidate_memory`` clamps the medium-term summary at WRITE time: an
  oversized LLM result is compressed (extra LLM pass, hard-truncate
  fallback) before it is stored, so the Redis tier can no longer accrete
  past the prompt cap and previously oversized summaries self-heal at
  their next consolidation.
* ``compress_medium_term_memory`` rewrites an already-stored oversized
  summary in place (used by the reactive self-heal pass).
* ``auto_heal_agent_memory`` runs the per-agent persistent-tier hygiene +
  medium-term compression, throttled per agent via Redis so bursts of
  assignment runs cannot stack LLM calls.
"""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import agent_memory as agent_memory_mod
from app.services import persistent_memory as persistent_memory_mod
from app.services.agent_memory import (
    _MAX_MEDIUM_TERM_PROMPT_CHARS,
    _compress_summary,
    compress_medium_term_memory,
)
from app.services.persistent_memory import auto_heal_agent_memory

AGENT_ID = "00000000-0000-0000-0000-000000000001"

OVERSIZED = "F " * (_MAX_MEDIUM_TERM_PROMPT_CHARS // 2 + 500)  # > cap
COMPACT = "- compact fact one\n- compact fact two"


class _FakeRedis:
    """Minimal async Redis stub recording get/set/expire traffic."""

    def __init__(self, store: dict | None = None) -> None:
        self.store: dict = store or {}
        self.set_calls: list[tuple[tuple, dict]] = []
        self.expire_calls: list[tuple[str, int]] = []

    async def get(self, key):
        return self.store.get(key)

    async def set(self, *args, **kwargs):
        self.set_calls.append((args, kwargs))
        if kwargs.get("nx") and args[0] in self.store:
            return None
        self.store[args[0]] = args[1]
        return True

    async def expire(self, key, seconds):
        self.expire_calls.append((key, seconds))
        return True

    async def exists(self, *_keys):
        return 0


def _fake_llm(*responses: str):
    """LLM stub whose chat() returns *responses* in order."""
    contents = list(responses)

    async def _chat(**_kwargs):
        return SimpleNamespace(content=contents.pop(0))

    return SimpleNamespace(chat=AsyncMock(side_effect=_chat))


# ---------------------------------------------------------------------------
# _compress_summary — the shared compression primitive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compress_summary_uses_llm_result_when_compact():
    llm = _fake_llm(COMPACT)
    result = await _compress_summary(OVERSIZED, llm, agent_id=AGENT_ID)
    assert result == COMPACT
    llm.chat.assert_awaited_once()


@pytest.mark.asyncio
async def test_compress_summary_hard_truncates_when_llm_fails():
    """LLM failure must degrade to hard truncation — the returned value is
    ALWAYS within the cap so callers can store it without re-checking."""
    llm = SimpleNamespace(chat=AsyncMock(side_effect=RuntimeError("provider down")))
    result = await _compress_summary(OVERSIZED, llm, agent_id=AGENT_ID)
    assert len(result) <= _MAX_MEDIUM_TERM_PROMPT_CHARS


@pytest.mark.asyncio
async def test_compress_summary_hard_truncates_still_oversized_llm_output():
    """An LLM that ignores the 400-word instruction must not defeat the cap."""
    llm = _fake_llm("X " * (_MAX_MEDIUM_TERM_PROMPT_CHARS))
    result = await _compress_summary(OVERSIZED, llm, agent_id=AGENT_ID)
    assert len(result) <= _MAX_MEDIUM_TERM_PROMPT_CHARS


# ---------------------------------------------------------------------------
# consolidate_memory — write-time clamp
# ---------------------------------------------------------------------------


async def _run_consolidation(llm) -> _FakeRedis:
    fake_redis = _FakeRedis()

    async def fake_load_memory(_agent_id: str, *, scope_id: str | None = None):
        return {
            "short_term": [
                {
                    "source": "Triage",
                    "source_type": "assignment",
                    "summary": "Processed the inbox.",
                    "timestamp": "2026-07-11T12:00:00",
                }
            ],
            "medium_term": "",
        }

    async def fake_redis_client():
        return fake_redis

    with (
        patch.object(agent_memory_mod, "load_memory", side_effect=fake_load_memory),
        patch.object(agent_memory_mod, "redis_client", side_effect=fake_redis_client),
        patch(
            "app.services.persistent_memory.persist_memory",
            new=AsyncMock(return_value=None),
        ),
        patch("app.core.database.async_session", new=_FakeSessionFactory()),
    ):
        await agent_memory_mod.consolidate_memory(
            AGENT_ID, llm, force=True, scope_id=None, scope_is_user_id=True
        )
    return fake_redis


@pytest.mark.asyncio
async def test_consolidate_memory_clamps_oversized_summary_at_write_time():
    """An oversized consolidation result must be compressed BEFORE storage —
    the Redis medium-term tier must never hold a summary above the cap."""
    # First chat call = consolidation (oversized), second = compression.
    llm = _fake_llm(OVERSIZED, COMPACT)

    fake_redis = await _run_consolidation(llm)

    stored = fake_redis.store[agent_memory_mod._medium_term_key(AGENT_ID)]
    assert stored == COMPACT
    assert len(stored) <= _MAX_MEDIUM_TERM_PROMPT_CHARS
    assert llm.chat.await_count == 2


@pytest.mark.asyncio
async def test_consolidate_memory_stores_compact_summary_unchanged():
    """A within-cap consolidation result must be stored as-is (no extra
    LLM call, no truncation)."""
    llm = _fake_llm(COMPACT)

    fake_redis = await _run_consolidation(llm)

    stored = fake_redis.store[agent_memory_mod._medium_term_key(AGENT_ID)]
    assert stored == COMPACT
    assert llm.chat.await_count == 1


# ---------------------------------------------------------------------------
# compress_medium_term_memory — reactive compression of a stored summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compress_medium_term_memory_noop_when_within_cap():
    key = agent_memory_mod._medium_term_key(AGENT_ID)
    fake_redis = _FakeRedis({key: COMPACT})
    llm = _fake_llm()

    async def fake_redis_client():
        return fake_redis

    with patch.object(agent_memory_mod, "redis_client", side_effect=fake_redis_client):
        changed = await compress_medium_term_memory(AGENT_ID, llm)

    assert changed is False
    assert not fake_redis.set_calls
    llm.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_compress_medium_term_memory_rewrites_oversized_summary():
    key = agent_memory_mod._medium_term_key(AGENT_ID)
    fake_redis = _FakeRedis({key: OVERSIZED})
    llm = _fake_llm(COMPACT)

    async def fake_redis_client():
        return fake_redis

    with patch.object(agent_memory_mod, "redis_client", side_effect=fake_redis_client):
        changed = await compress_medium_term_memory(AGENT_ID, llm)

    assert changed is True
    assert fake_redis.store[key] == COMPACT


# ---------------------------------------------------------------------------
# auto_heal_agent_memory — reactive, throttled per-agent self-heal
# ---------------------------------------------------------------------------


class _FakeSessionFactory:
    """Async-context-manager session stub recording commit()."""

    committed = False

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        type(self).committed = True


@pytest.mark.asyncio
async def test_auto_heal_skipped_when_throttled():
    """A second heal within the throttle window must be a no-op — no DB
    session, no hygiene calls, no LLM."""
    fake_redis = _FakeRedis(
        {f"agent_mem_autoheal:{AGENT_ID}": "1", f"agent_mem_autoheal:{AGENT_ID}:scope:agent": "1"}
    )

    async def fake_redis_client():
        return fake_redis

    with (
        patch.object(
            persistent_memory_mod, "prune_old_memories", new_callable=AsyncMock
        ) as mock_prune,
        patch(
            "app.core.redis.redis_client",
            side_effect=fake_redis_client,
        ),
    ):
        ran = await auto_heal_agent_memory(AGENT_ID)

    assert ran is False
    mock_prune.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_agent_wide_hygiene_runs_once_per_window_and_each_lane_compresses():
    """Two throttles: the agent-wide persistent hygiene runs once per window,
    whichever lane fires it; each user's lane gets its own compression, once."""
    fake_redis = _FakeRedis()

    async def fake_redis_client():
        return fake_redis

    with (
        patch.object(
            persistent_memory_mod, "prune_old_memories", new_callable=AsyncMock
        ) as mock_prune,
        patch.object(persistent_memory_mod, "enforce_memory_cap", new_callable=AsyncMock),
        patch.object(persistent_memory_mod, "consolidate_agent_facts", new_callable=AsyncMock) as mock_facts,
        patch.object(persistent_memory_mod, "reembed_stale_memories", new_callable=AsyncMock),
        patch("app.core.redis.redis_client", side_effect=fake_redis_client),
        patch("app.core.database.async_session", new=_FakeSessionFactory()),
        patch(
            "app.llm.router.get_llm_for_user",
            new=AsyncMock(return_value=(SimpleNamespace(), "test-model")),
        ),
        patch(
            "app.services.agent_memory.compress_medium_term_memory",
            new_callable=AsyncMock,
        ) as mock_compress,
    ):
        assert await auto_heal_agent_memory(AGENT_ID, scope_id="user-a") is True
        assert await auto_heal_agent_memory(AGENT_ID, scope_id="user-b") is True
        assert await auto_heal_agent_memory(AGENT_ID, scope_id="user-a") is False
        # The agent-level lane has its own compression throttle too.
        assert await auto_heal_agent_memory(AGENT_ID, scope_id=None) is True
        assert await auto_heal_agent_memory(AGENT_ID, scope_id=None) is False

    mock_prune.assert_awaited_once()
    mock_facts.assert_awaited_once()
    assert [c.kwargs["scope_id"] for c in mock_compress.await_args_list] == ["user-a", "user-b", None]


@pytest.mark.asyncio
async def test_auto_heal_runs_full_hygiene_and_compression():
    """With the throttle acquired, the heal pass must run all four
    persistent-tier hygiene tasks, compress the medium-term summary, and
    commit its own session."""
    _FakeSessionFactory.committed = False
    fake_redis = _FakeRedis()

    async def fake_redis_client():
        return fake_redis

    llm = SimpleNamespace(chat=AsyncMock())
    with (
        patch.object(
            persistent_memory_mod, "prune_old_memories", new_callable=AsyncMock
        ) as mock_prune,
        patch.object(
            persistent_memory_mod, "enforce_memory_cap", new_callable=AsyncMock
        ) as mock_cap,
        patch.object(
            persistent_memory_mod, "consolidate_agent_facts", new_callable=AsyncMock
        ) as mock_facts,
        patch.object(
            persistent_memory_mod, "reembed_stale_memories", new_callable=AsyncMock
        ) as mock_reembed,
        patch(
            "app.core.redis.redis_client",
            side_effect=fake_redis_client,
        ),
        patch(
            "app.core.database.async_session",
            new=_FakeSessionFactory(),
        ),
        patch(
            "app.llm.router.get_llm_for_user",
            new=AsyncMock(return_value=(llm, "test-model")),
        ),
        patch(
            "app.services.agent_memory.compress_medium_term_memory",
            new_callable=AsyncMock,
        ) as mock_compress,
    ):
        mock_prune.return_value = 1
        mock_cap.return_value = 2
        mock_facts.return_value = 3
        mock_reembed.return_value = 0
        mock_compress.return_value = True

        ran = await auto_heal_agent_memory(AGENT_ID)

    assert ran is True
    mock_prune.assert_awaited_once()
    mock_cap.assert_awaited_once()
    mock_facts.assert_awaited_once()
    mock_reembed.assert_awaited_once()
    mock_compress.assert_awaited_once()
    assert _FakeSessionFactory.committed
    # The throttle key must have been claimed (nx set).
    assert f"agent_mem_autoheal:{AGENT_ID}" in fake_redis.store


@pytest.mark.asyncio
async def test_auto_heal_failure_shortens_throttle_ttl():
    """When the heal pass fails AFTER acquiring the throttle, the key's TTL
    must be shortened so a transient failure can retry soon instead of
    blocking remediation for the full 6h window."""
    fake_redis = _FakeRedis()

    async def fake_redis_client():
        return fake_redis

    with (
        patch.object(
            persistent_memory_mod,
            "prune_old_memories",
            new=AsyncMock(side_effect=RuntimeError("db down")),
        ),
        patch(
            "app.core.redis.redis_client",
            side_effect=fake_redis_client,
        ),
        patch(
            "app.core.database.async_session",
            new=_FakeSessionFactory(),
        ),
    ):
        ran = await auto_heal_agent_memory(AGENT_ID)

    assert ran is False
    # Both throttle keys were claimed, then their TTL shortened on failure.
    keys = [f"agent_mem_autoheal:{AGENT_ID}", f"agent_mem_autoheal:{AGENT_ID}:scope:agent"]
    assert all(key in fake_redis.store for key in keys)
    assert fake_redis.expire_calls == [
        (key, persistent_memory_mod._AUTO_HEAL_FAILURE_RETRY_SECONDS) for key in keys
    ]


@pytest.mark.asyncio
async def test_self_heal_task_named_and_exception_consumed(
    caplog: pytest.LogCaptureFixture,
):
    """The fire-and-forget task is named after the agent so failures can be
    correlated, and the done callback consumes an unexpected exception
    (broken never-raises contract) instead of letting it surface as a
    "Task exception was never retrieved" warning."""
    from app.services import assignment_helpers as assignment_helpers_mod

    agent = SimpleNamespace(id=AGENT_ID)
    caplog.set_level(logging.WARNING, logger="app.services.assignment_helpers")

    with patch(
        "app.services.persistent_memory.auto_heal_agent_memory",
        new=AsyncMock(side_effect=RuntimeError("broken contract")),
    ):
        assignment_helpers_mod._schedule_memory_self_heal(agent, None)
        tasks = list(assignment_helpers_mod._self_heal_tasks)
        assert tasks
        assert tasks[0].get_name() == f"memory-self-heal-{AGENT_ID}"
        await asyncio.gather(*tasks, return_exceptions=True)
        # Let the done callback run (scheduled via call_soon).
        await asyncio.sleep(0)

    # Strong reference dropped, exception consumed and logged with the
    # task name for correlation.
    assert not assignment_helpers_mod._self_heal_tasks
    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "self-heal task" in r.getMessage()
    ]
    assert warnings, "The consumed exception must be logged as a WARNING"
    assert f"memory-self-heal-{AGENT_ID}" in warnings[0].getMessage()
    assert "broken contract" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_auto_heal_skipped_when_redis_unavailable():
    """Redis being down must not turn into an unthrottled LLM storm — the
    pass is skipped entirely (the hourly scheduler still covers hygiene)."""

    async def broken_redis_client():
        raise ConnectionError("redis down")

    with (
        patch.object(
            persistent_memory_mod, "prune_old_memories", new_callable=AsyncMock
        ) as mock_prune,
        patch(
            "app.core.redis.redis_client",
            side_effect=broken_redis_client,
        ),
    ):
        ran = await auto_heal_agent_memory(AGENT_ID)

    assert ran is False
    mock_prune.assert_not_awaited()
