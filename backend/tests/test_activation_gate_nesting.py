"""Verify the assignment activation gate nests under the workflow trace.

Box 4 of the hierarchical Langfuse tracing series: the gate used to
open a sibling top-level trace via ``trace_llm_call``.  After the
refactor it opens a child ``activation-gate`` generation under the
workflow root span via ``start_activation_gate_generation``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import assignment_engine


def _make_agent():
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Test Agent",
        slug="test-agent",
        llm_provider=None,
    )


@pytest.fixture
def stub_llm(monkeypatch):
    """Replace ``get_llm_for_user`` with a stub returning a stub LLM."""
    llm = MagicMock()
    llm.chat = AsyncMock(
        return_value=SimpleNamespace(
            content="YES",
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=1),
        )
    )
    get_llm = AsyncMock(return_value=(llm, "gpt-4o-mini"))
    monkeypatch.setattr("app.llm.router.get_llm_for_user", get_llm)
    return llm


@pytest.mark.asyncio
async def test_gate_opens_generation_under_parent_ctx(monkeypatch, stub_llm):
    sentinel_parent = {"trace": MagicMock(), "client": MagicMock()}
    captured: dict = {}

    def fake_start(parent_ctx, *, model, messages, **_):
        captured["parent_ctx"] = parent_ctx
        captured["model"] = model
        captured["messages"] = messages
        return {"trace": MagicMock(), "generation": MagicMock()}

    end_mock = MagicMock()

    monkeypatch.setattr(
        "app.services.langfuse_tracing.start_activation_gate_generation",
        fake_start,
    )
    monkeypatch.setattr("app.services.langfuse_tracing.end_generation", end_mock)

    answer = await assignment_engine._run_activation_gate(
        MagicMock(),
        agent=_make_agent(),
        user_id="user-1",
        rendered_activation="Should I run?",
        parent_trace_ctx=sentinel_parent,
    )

    assert answer == "YES"
    assert captured["parent_ctx"] is sentinel_parent
    assert captured["model"] == "gpt-4o-mini"
    assert len(captured["messages"]) == 2  # system + user
    end_mock.assert_called_once()
    end_kwargs = end_mock.call_args.kwargs
    assert end_kwargs["output"] == "YES"
    assert end_kwargs["model"] == "gpt-4o-mini"
    assert end_kwargs.get("error") is None
    # Usage details propagate to the closed generation.
    assert end_kwargs["usage"].prompt_tokens == 10
    assert end_kwargs["usage"].completion_tokens == 1


@pytest.mark.asyncio
async def test_gate_closes_with_error_on_llm_failure(monkeypatch, stub_llm):
    stub_llm.chat = AsyncMock(side_effect=RuntimeError("boom"))

    end_mock = MagicMock()
    monkeypatch.setattr(
        "app.services.langfuse_tracing.start_activation_gate_generation",
        lambda *_, **__: {"trace": MagicMock(), "generation": MagicMock()},
    )
    monkeypatch.setattr("app.services.langfuse_tracing.end_generation", end_mock)

    with pytest.raises(RuntimeError, match="boom"):
        await assignment_engine._run_activation_gate(
            MagicMock(),
            agent=_make_agent(),
            user_id="user-2",
            rendered_activation="Should I run?",
            parent_trace_ctx={"trace": MagicMock()},
        )

    end_mock.assert_called_once()
    end_kwargs = end_mock.call_args.kwargs
    assert isinstance(end_kwargs["error"], RuntimeError)
    assert str(end_kwargs["error"]) == "boom"
    # On LLM failure, no content / usage is recorded.
    assert end_kwargs["output"] is None
    assert end_kwargs["usage"] is None


@pytest.mark.asyncio
async def test_gate_degrades_gracefully_without_parent_ctx(stub_llm):
    """With ``parent_trace_ctx=None`` the real helper returns ``None``
    (no monkeypatching of the gate generation needed) and ``end_span``
    naturally no-ops on a missing ctx.
    """
    answer = await assignment_engine._run_activation_gate(
        MagicMock(),
        agent=_make_agent(),
        user_id=None,
        rendered_activation="Should I run?",
        parent_trace_ctx=None,
    )

    assert answer == "YES"


@pytest.mark.asyncio
async def test_gate_prompt_advertises_yes_no_skip(monkeypatch, stub_llm):
    """The activation gate's system prompt MUST list every accepted
    answer (``YES`` / ``NO`` / ``SKIP``).

    The caller in ``execute_assignment_run`` branches on
    ``answer.startswith("NO") or answer.startswith("SKIP")``, so the
    ``SKIP`` branch is reachable but unreachable in practice if the
    system prompt only advertises YES/NO — the model has no permission
    to emit ``SKIP``. This test pins the prompt/caller alignment so a
    future regression that narrows the prompt to ``"Answer only YES or
    NO."`` (which would make the SKIP branch effectively dead code)
    trips here.
    """
    captured: dict = {}

    def fake_start(parent_ctx, *, model, messages, **_):
        captured["messages"] = messages
        return {"trace": MagicMock(), "generation": MagicMock()}

    monkeypatch.setattr(
        "app.services.langfuse_tracing.start_activation_gate_generation",
        fake_start,
    )
    monkeypatch.setattr(
        "app.services.langfuse_tracing.end_generation", MagicMock()
    )

    await assignment_engine._run_activation_gate(
        MagicMock(),
        agent=_make_agent(),
        user_id="user-1",
        rendered_activation="Should I run?",
        parent_trace_ctx={"trace": MagicMock()},
    )

    messages = captured["messages"]
    assert len(messages) == 2
    system_prompt = messages[0].content
    # All three accepted answer tokens MUST appear in the system prompt
    # so the model has explicit permission to emit each one — the
    # caller treats both ``NO`` and ``SKIP`` as skip signals.
    for token in ("YES", "NO", "SKIP"):
        assert token in system_prompt, (
            f"activation gate system prompt MUST advertise {token!r} so the "
            f"model has permission to emit it (the caller's "
            f"``answer.startswith({token!r})`` branch becomes dead code "
            f"otherwise); got:\n{system_prompt}"
        )
