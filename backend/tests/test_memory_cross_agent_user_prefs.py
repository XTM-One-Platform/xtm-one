"""Tests for cross-agent user preference scoping in the memory system.

When a shared agent is used by multiple users in different languages, the
agent's per-user memory consolidation used to capture cross-agent personal
preferences (preferred language, tone, communication style) into the
agent's own memory tier.  Because that memory is loaded into every system
prompt for that agent–user pair, the agent would (e.g.) reply in French
to a user who had now switched to English.

The fix routes cross-agent personal preferences exclusively through the
platform-wide user profile (``user_memory``) and explicitly excludes them
from:

* the Redis-backed agent consolidation prompt (``agent_memory``)
* the durable PostgreSQL fact extraction prompts
  (``persistent_memory``)
* the conversation compaction's Pinned Directives prompt
  (``conversation_memory``)

These tests assert the structural guidance is present in each prompt so
the behaviour cannot regress silently.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.llm.base import ChatMessage
from app.services.conversation_memory import _COMPACTION_SYSTEM_PROMPT
from app.services.persistent_memory import (
    _extract_candidate_facts,
    extract_and_persist_facts,
)

# ---------------------------------------------------------------------------
# agent_memory consolidation prompt
# ---------------------------------------------------------------------------


def _capture_agent_memory_prompt(
    *, is_company_managed: bool, scope_id: str | None
) -> str:
    """Trigger ``consolidate_memory`` and capture the system prompt it sends.

    The function returns early without an LLM call when there are no
    short-term entries, so we seed Redis via mocks and capture the prompt
    via ``llm.chat``.
    """
    import asyncio

    from app.services import agent_memory as agent_memory_mod

    captured: dict[str, str] = {}

    async def fake_chat(*, messages, **_kwargs):
        from types import SimpleNamespace

        captured["system_prompt"] = next(
            m.content for m in messages if m.role == "system"
        )
        return SimpleNamespace(content="updated memory body")

    fake_llm = SimpleNamespaceLLM(chat=AsyncMock(side_effect=fake_chat))

    async def _run() -> None:
        # Mock the redis pipeline + load_memory so consolidation actually runs.
        fake_short_entries = [
            {
                "source": "chat",
                "source_type": "chat",
                "summary": "User asked the agent to always reply in French.",
                "timestamp": "2026-05-26T12:00:00",
            },
            {
                "source": "chat",
                "source_type": "chat",
                "summary": "User specified the OpenCTI host URL is "
                "https://corp.example.com/.",
                "timestamp": "2026-05-26T12:01:00",
            },
        ]

        async def fake_load_memory(_agent_id: str, *, scope_id: str | None = None):
            return {"short_term": fake_short_entries, "medium_term": ""}

        class _FakeRedis:
            async def get(self, *_args, **_kwargs):
                return str(agent_memory_mod.CONSOLIDATION_THRESHOLD + 1)

            async def set(self, *_args, **_kwargs):
                return None

            async def exists(self, *_keys):
                return 0

        async def fake_redis_client():
            return _FakeRedis()

        with (
            patch.object(agent_memory_mod, "load_memory", side_effect=fake_load_memory),
            patch.object(
                agent_memory_mod, "redis_client", side_effect=fake_redis_client
            ),
            patch(
                "app.services.persistent_memory.persist_memory",
                new=AsyncMock(return_value=None),
            ),
        ):
            await agent_memory_mod.consolidate_memory(
                "00000000-0000-0000-0000-000000000001",
                fake_llm,
                force=True,
                scope_id=scope_id,
                is_company_managed=is_company_managed,
            )

    asyncio.run(_run())
    return captured["system_prompt"]


class SimpleNamespaceLLM:
    """Tiny shim so the LLM-like object exposes only ``chat``."""

    def __init__(self, *, chat) -> None:
        self.chat = chat


class TestAgentMemoryConsolidationPrompt:
    """The agent-memory consolidator must refuse to capture cross-agent
    personal preferences like language."""

    def test_per_user_branch_excludes_language_preferences(self) -> None:
        prompt = _capture_agent_memory_prompt(
            is_company_managed=False, scope_id="user-uuid"
        )
        # Per-user branch must redirect cross-agent prefs to the user profile.
        assert "preferred language" in prompt.lower()
        assert "user profile" in prompt.lower()
        assert "do not capture" in prompt.lower()
        # No legacy fallback that explicitly stores communication style as
        # *agent* memory.
        assert "communication style or tone preferences observed" not in prompt

    def test_company_managed_branch_excludes_language_preferences(self) -> None:
        prompt = _capture_agent_memory_prompt(
            is_company_managed=True, scope_id=None
        )
        # The shared branch already excluded "personal communication styles".
        # The fix additionally bans the user's preferred language explicitly.
        assert "preferred language" in prompt.lower()
        assert "platform-wide user profile" in prompt.lower()


# ---------------------------------------------------------------------------
# persistent_memory fact extraction prompts
# ---------------------------------------------------------------------------


async def _capture_extract_candidate_prompt(
    *, is_company_managed: bool, has_user_scope: bool
) -> str:
    captured: dict[str, str] = {}

    async def fake_chat(*, messages, **_kwargs):
        from types import SimpleNamespace

        captured["system_prompt"] = next(
            m.content for m in messages if m.role == "system"
        )
        return SimpleNamespace(content="NONE")

    fake_llm = SimpleNamespaceLLM(chat=AsyncMock(side_effect=fake_chat))
    await _extract_candidate_facts(
        fake_llm,
        messages_text="User: please reply in French from now on.",
        is_company_managed=is_company_managed,
        has_user_scope=has_user_scope,
    )
    return captured["system_prompt"]


class TestExtractCandidateFactsPrompt:
    """The reconciliation extractor must exclude language/comm style from
    durable per-user agent facts."""

    @pytest.mark.asyncio
    async def test_user_scoped_extractor_excludes_language(self) -> None:
        prompt = await _capture_extract_candidate_prompt(
            is_company_managed=False, has_user_scope=True
        )
        assert "preferred language" in prompt.lower()
        assert "platform-wide" in prompt.lower()
        assert "not in this agent's memory" in prompt.lower()
        # Must keep the corrected-decision rewrite guidance — it's
        # orthogonal to the new exclusion.
        assert "Use subregion filter, not country" in prompt

    @pytest.mark.asyncio
    async def test_company_managed_agent_level_extractor_excludes_language(
        self,
    ) -> None:
        prompt = await _capture_extract_candidate_prompt(
            is_company_managed=True, has_user_scope=False
        )
        assert "preferred language" in prompt.lower()
        assert "platform-wide" in prompt.lower()


@pytest.mark.asyncio
async def test_extract_and_persist_facts_user_scope_prompt_excludes_language() -> (
    None
):
    """``extract_and_persist_facts`` (the legacy assignment path) must
    apply the same exclusion when a ``user_id`` is given.
    """
    captured: dict[str, str] = {}

    async def fake_chat(*, messages, **_kwargs):
        from types import SimpleNamespace

        captured["system_prompt"] = next(
            m.content for m in messages if m.role == "system"
        )
        return SimpleNamespace(content="NONE")

    fake_llm = SimpleNamespaceLLM(chat=AsyncMock(side_effect=fake_chat))
    # ``db`` is unused because the LLM returns ``NONE`` and we short-circuit
    # before any persist call.
    await extract_and_persist_facts(
        db=None,  # type: ignore[arg-type]
        llm=fake_llm,
        agent_id="00000000-0000-0000-0000-000000000001",
        messages_text="User: switch to French replies.",
        user_id="00000000-0000-0000-0000-000000000099",
        is_company_managed=False,
    )

    prompt = captured["system_prompt"]
    assert "preferred language" in prompt.lower()
    assert "platform-wide" in prompt.lower()
    assert "name, role, team" in prompt.lower()


# ---------------------------------------------------------------------------
# user_memory consolidation prompt
# ---------------------------------------------------------------------------


def test_user_memory_consolidation_prompt_captures_language() -> None:
    """The cross-agent user profile is the canonical home for language
    preference — the prompt must explicitly ask for it.
    """
    # The prompt is built inline inside ``consolidate_user_memory``;
    # capture it through the same fake-LLM technique.
    import asyncio

    from app.services import user_memory as user_memory_mod

    captured: dict[str, str] = {}

    async def fake_chat(*, messages, **_kwargs):
        from types import SimpleNamespace

        captured["system_prompt"] = next(
            m.content for m in messages if m.role == "system"
        )
        return SimpleNamespace(content="updated profile")

    fake_llm = SimpleNamespaceLLM(chat=AsyncMock(side_effect=fake_chat))

    async def fake_load_user_memory(_identity: str):
        return {
            "short_term": [
                {
                    "agent": "OCTI Agent",
                    "interaction_type": "chat",
                    "summary": "User wrote in French and asked for French replies.",
                    "timestamp": "2026-05-26T12:00:00",
                }
            ],
            "medium_term": "",
        }

    class _FakeRedis:
        async def get(self, *_args, **_kwargs):
            return str(user_memory_mod.CONSOLIDATION_THRESHOLD + 1)

        async def set(self, *_args, **_kwargs):
            return True

        async def eval(self, *_args):
            return 1

        async def exists(self, *_keys):
            return 0

    async def fake_redis_client():
        return _FakeRedis()

    async def _run() -> None:
        with (
            patch.object(
                user_memory_mod, "load_user_memory", side_effect=fake_load_user_memory
            ),
            patch.object(
                user_memory_mod, "redis_client", side_effect=fake_redis_client
            ),
        ):
            await user_memory_mod.consolidate_user_memory(
                "user-uuid", fake_llm, force=True
            )

    asyncio.run(_run())
    prompt = captured["system_prompt"]
    assert "preferred language" in prompt.lower()
    # The prompt should also frame this section as cross-agent canonical.
    assert "cross-agent" in prompt.lower()


# ---------------------------------------------------------------------------
# conversation_memory compaction prompt
# ---------------------------------------------------------------------------


class TestConversationCompactionPrompt:
    """Pinned Directives are conversation-scoped, but the prompt should
    no longer present language preferences as the canonical example —
    those are routed to the cross-agent user profile."""

    def test_does_not_use_answer_in_french_as_canonical_directive(self) -> None:
        # Language preference must NOT be presented as a sticky directive
        # example — that's what made the compactor pin language into
        # every system prompt and made the agent stuck on French.
        assert "answer in french" not in _COMPACTION_SYSTEM_PROMPT.lower()

    def test_explicitly_excludes_cross_agent_personal_preferences(self) -> None:
        # The Exclude section must mention preferred language and
        # communication style so the LLM doesn't pin them as directives.
        lowered = _COMPACTION_SYSTEM_PROMPT.lower()
        assert "preferred reply language" in lowered or "preferred language" in lowered
        assert "communication style" in lowered
        assert "platform-wide profile" in lowered or "user profile" in lowered

    def test_keeps_imperative_task_directives_examples(self) -> None:
        # We still want the LLM to capture task-scoped imperatives like
        # filter corrections — the fix must not throw the baby out.
        assert "subregion" in _COMPACTION_SYSTEM_PROMPT
        assert "currency prefix" in _COMPACTION_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Smoke import to keep the test discoverable when pytest-asyncio is missing.
# ---------------------------------------------------------------------------


def test_smoke_imports() -> None:
    """Importing the touched modules should not raise."""
    import app.services.agent_memory
    import app.services.conversation_memory
    import app.services.persistent_memory
    import app.services.user_memory

    assert app.services.agent_memory is not None
    assert app.services.conversation_memory is not None
    assert app.services.persistent_memory is not None
    assert app.services.user_memory is not None


# Make sure mypy/ruff are happy with the unused ChatMessage import — it is
# imported so future test additions in this module can re-use it without
# triggering an extra import in already-loaded modules.
_ = ChatMessage
