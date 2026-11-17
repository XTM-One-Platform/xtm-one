"""Traces name the provider CONFIG that served a call, not only its type."""

from __future__ import annotations

import ast
import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import llm_proxy
from app.llm import router
from app.llm.attribution import (
    TRACE_CONFIG_ID_KEY,
    TRACE_CONFIG_LABEL_KEY,
    TRACE_DOWNGRADE_KEY,
    TRACE_FAILOVER_KEY,
    config_metadata,
    provider_config_metadata,
    record_model_downgrade,
)
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.router import AllProvidersFailedError, FailoverLLMProvider, choose_provider_config
from app.services import evaluation_engine
from app.services.langfuse_tracing import (
    get_current_trace_ctx,
    reset_current_trace_ctx,
    set_current_trace_ctx,
    start_iteration_span,
)
from app.tools.agent_loop import run_agent_loop
from app.tools.base import CompositeToolExecutor

BACKEND = Path(__file__).resolve().parents[1]


# -- building a provider -----------------------------------------------------


class _BuiltProvider(BaseLLMProvider):
    provider_name = "openai"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        return ChatResponse(content="", model=model or "", usage={})

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs):
        if False:
            yield ""

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


def _config(provider: str = "openai", **overrides) -> SimpleNamespace:
    values = {
        "id": uuid.uuid4(),
        "provider": provider,
        "api_key_enc": "enc",
        "display_name": None,
        "default_model": None,
        "available_models": ["gpt-4o"],
        "context_windows": None,
        "max_output_tokens": None,
        "is_default": False,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_provider_stamps_the_config_it_was_built_from(monkeypatch):
    monkeypatch.setattr(router, "decrypt_value", lambda _v: "key")
    monkeypatch.setitem(router.PROVIDER_MAP, "openai", _BuiltProvider)
    team_a = _config(display_name="Team A")
    team_b = _config()

    built_a = router.build_provider(team_a)
    built_b = router.build_provider(team_b)

    # Same type, two configs: the traces must be able to tell them apart.
    assert built_a.provider_name == built_b.provider_name == "openai"
    assert built_a.provider_config_id == str(team_a.id)
    assert built_b.provider_config_id == str(team_b.id)
    assert provider_config_metadata(built_a) == {
        TRACE_CONFIG_ID_KEY: str(team_a.id),
        TRACE_CONFIG_LABEL_KEY: "Team A",
    }
    # No display name: the id alone, never an empty label.
    assert provider_config_metadata(built_b) == {TRACE_CONFIG_ID_KEY: str(team_b.id)}
    # A config row used without a provider (the Anthropic pass-through) is
    # named by the same rule.
    assert config_metadata(team_a) == provider_config_metadata(built_a)
    assert config_metadata(team_b) == provider_config_metadata(built_b)


def test_a_blank_display_name_is_no_label(monkeypatch):
    """``display_name`` is stored as typed: a whitespace-only name is no name,
    never an empty-looking label, and a padded one is trimmed."""
    monkeypatch.setattr(router, "decrypt_value", lambda _v: "key")
    monkeypatch.setitem(router.PROVIDER_MAP, "openai", _BuiltProvider)
    blank = _config(display_name="   ")
    padded = _config(display_name="  Team A  ")

    assert router.build_provider(blank).provider_config_label is None
    assert provider_config_metadata(router.build_provider(blank)) == {TRACE_CONFIG_ID_KEY: str(blank.id)}
    assert config_metadata(blank) == {TRACE_CONFIG_ID_KEY: str(blank.id)}
    assert config_metadata(padded)[TRACE_CONFIG_LABEL_KEY] == "Team A"
    # A label set on an instance directly is read by the same rule.
    stub = _BuiltProvider(api_key="k")
    stub.provider_config_id, stub.provider_config_label = "some-config", " "
    assert provider_config_metadata(stub) == {TRACE_CONFIG_ID_KEY: "some-config"}


def test_a_provider_built_outside_the_router_carries_no_config():
    # An absent id is honest; a guessed one would be confidently wrong.
    assert provider_config_metadata(_BuiltProvider(api_key="k")) == {}


# -- failover ----------------------------------------------------------------


class _Failing(BaseLLMProvider):
    provider_name = "anthropic"
    provider_config_id = "primary-config"

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        raise RuntimeError("primary down")

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs) -> AsyncIterator[str]:
        if False:
            yield ""
        raise RuntimeError("primary down")

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


class _Answering(BaseLLMProvider):
    provider_name = "openai"
    provider_config_id = "fallback-config"
    provider_config_label = "Backup key"

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        return ChatResponse(content="ok", model="fallback", usage={})

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs) -> AsyncIterator[str]:
        yield "ok"

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


class _Span:
    def __init__(self):
        self.updates: list[dict] = []

    def update(self, **kwargs):
        self.updates.append(kwargs)


def _trace_ctx() -> dict:
    return {
        "trace": _Span(),
        "metadata": {
            "llm_provider": "anthropic",
            TRACE_CONFIG_ID_KEY: "primary-config",
        },
    }


def _run_with_trace(ctx: dict, coro_factory):
    async def _go():
        token = set_current_trace_ctx(ctx)
        try:
            return await coro_factory()
        finally:
            reset_current_trace_ctx(token)

    return asyncio.run(_go())


def test_failover_wrapper_mirrors_the_primary_config():
    wrapper = FailoverLLMProvider(primary=_Failing(), fallbacks=[_Answering()])
    assert wrapper.provider_config_id == "primary-config"


def test_a_fallback_answer_repoints_the_trace_at_its_own_config():
    ctx = _trace_ctx()
    wrapper = FailoverLLMProvider(primary=_Failing(), fallbacks=[_Answering()])

    _run_with_trace(ctx, lambda: wrapper.chat([ChatMessage(role="user", content="hi")]))

    meta = ctx["metadata"]
    # The primary never answered: naming it would be confidently wrong.
    assert meta[TRACE_CONFIG_ID_KEY] == "fallback-config"
    assert meta[TRACE_CONFIG_LABEL_KEY] == "Backup key"
    assert meta[TRACE_FAILOVER_KEY] == [
        {"provider": "openai", "config_id": "fallback-config", "label": "Backup key"}
    ]
    # The provider TYPE is left as it was: pricing resolves costs from it.
    assert meta["llm_provider"] == "anthropic"
    assert ctx["local_metadata"][TRACE_CONFIG_ID_KEY] == "fallback-config"
    assert ctx["trace"].updates, "the Langfuse root span is updated too"


def test_a_streamed_fallback_answer_repoints_the_trace_too():
    ctx = _trace_ctx()
    wrapper = FailoverLLMProvider(primary=_Failing(), fallbacks=[_Answering()])

    async def _consume():
        return [chunk async for chunk in wrapper.chat_stream([ChatMessage(role="user", content="hi")])]

    assert _run_with_trace(ctx, _consume) == ["ok"]
    assert ctx["metadata"][TRACE_CONFIG_ID_KEY] == "fallback-config"


def test_repeated_failovers_record_each_fallback_once():
    ctx = _trace_ctx()
    wrapper = FailoverLLMProvider(primary=_Failing(), fallbacks=[_Answering()])

    async def _twice():
        await wrapper.chat([ChatMessage(role="user", content="a")])
        await wrapper.chat([ChatMessage(role="user", content="b")])

    _run_with_trace(ctx, _twice)
    assert len(ctx["metadata"][TRACE_FAILOVER_KEY]) == 1


def test_a_primary_answer_leaves_the_trace_alone():
    ctx = _trace_ctx()
    wrapper = FailoverLLMProvider(primary=_Answering(), fallbacks=[_Failing()])

    _run_with_trace(ctx, lambda: wrapper.chat([ChatMessage(role="user", content="hi")]))

    assert ctx["metadata"] == {"llm_provider": "anthropic", TRACE_CONFIG_ID_KEY: "primary-config"}
    assert ctx["trace"].updates == []


def test_a_failover_with_no_current_trace_is_harmless():
    wrapper = FailoverLLMProvider(primary=_Failing(), fallbacks=[_Answering()])
    response = asyncio.run(wrapper.chat([ChatMessage(role="user", content="hi")]))
    assert response.content == "ok"


def test_the_correction_reaches_the_trace_from_the_task_a_call_runs_in():
    """The agent loop runs every model call in a task of its own
    (``_race_cancellation``), which sees a copy of the context: the correction
    must still land on the trace the caller holds."""
    ctx = _trace_ctx()
    wrapper = FailoverLLMProvider(primary=_Failing(), fallbacks=[_Answering()])

    async def _in_a_task():
        return await asyncio.ensure_future(wrapper.chat([ChatMessage(role="user", content="hi")]))

    _run_with_trace(ctx, _in_a_task)
    assert ctx["metadata"][TRACE_CONFIG_ID_KEY] == "fallback-config"


# -- the trace root, not the span the agent loop made current ------------------


def _local_root() -> dict:
    """A root trace context shaped like ``trace_llm_call``'s with Langfuse off."""
    meta = {"llm_provider": "anthropic", TRACE_CONFIG_ID_KEY: "primary-config"}
    return {
        "trace": None,
        "generation": None,
        "client": None,
        "parent_ctx": None,
        "metadata": meta,
        "local_trace_id": uuid.uuid4(),
        "local_metadata": dict(meta),
        "local_model": "big-model",
        "local_spans": [],
        "local_span_counter": 0,
        "local_started_at": datetime.now(UTC),
        "local_status": "ok",
    }


def test_a_correction_made_inside_a_span_lands_on_the_trace_root():
    """The agent loop makes each iteration's span current around its model
    call: a failover and a downgrade must be written on the trace the Trace
    Explorer shows, not on that span."""
    root = _local_root()
    iteration = start_iteration_span(iteration=1, parent_ctx=root)
    wrapper = FailoverLLMProvider(primary=_Failing(), fallbacks=[_Answering()])

    async def _in_the_iteration():
        await wrapper.chat([ChatMessage(role="user", content="hi")])
        record_model_downgrade(_Answering(), from_model="big-model", to_model="cheap-model")

    _run_with_trace(iteration, _in_the_iteration)
    assert root["metadata"][TRACE_CONFIG_ID_KEY] == "fallback-config"
    assert root["local_metadata"][TRACE_FAILOVER_KEY]
    assert root["local_metadata"][TRACE_DOWNGRADE_KEY]["to_model"] == "cheap-model"
    assert root["local_model"] == "cheap-model"
    assert TRACE_CONFIG_ID_KEY not in iteration["metadata"]
    assert iteration.get("local_model") is None


def test_a_nested_agent_trace_names_its_own_config():
    """A consulted agent's trace is a root of its own under its caller's span:
    a failover in it is written there, never on the caller's trace."""
    outer = _local_root()
    caller_span = start_iteration_span(iteration=1, parent_ctx=outer)
    nested = {**_local_root(), "parent_ctx": caller_span}
    nested_iteration = start_iteration_span(iteration=1, parent_ctx=nested)
    wrapper = FailoverLLMProvider(primary=_Failing(), fallbacks=[_Answering()])

    _run_with_trace(nested_iteration, lambda: wrapper.chat([ChatMessage(role="user", content="hi")]))
    assert nested["metadata"][TRACE_CONFIG_ID_KEY] == "fallback-config"
    assert outer["metadata"][TRACE_CONFIG_ID_KEY] == "primary-config"


def test_the_agent_loop_reports_a_failover_on_its_trace_root():
    """End to end: the loop's own iteration spans sit between the trace and
    the model call."""
    root = _local_root()
    llm = FailoverLLMProvider(primary=_Failing(), fallbacks=[_Answering()])

    async def _run():
        return await run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="hi")],
            executor=CompositeToolExecutor(),
            max_iterations=2,
            progressive_tools=False,
        )

    response = _run_with_trace(root, _run)
    assert response.content == "ok"
    assert root["metadata"][TRACE_CONFIG_ID_KEY] == "fallback-config"
    assert root["local_metadata"][TRACE_CONFIG_ID_KEY] == "fallback-config"
    assert root["local_spans"], "the loop recorded its iteration spans under the root"


# -- a primary that serves again after a failover ------------------------------


class _FailsOnce(BaseLLMProvider):
    """A primary whose first call fails, then serves: a transient outage."""

    provider_name = "anthropic"
    provider_config_id = "primary-config"
    provider_config_label = "Main key"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("primary down")
        return ChatResponse(content="ok", model="primary", usage={})

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs) -> AsyncIterator[str]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("primary down")
        yield "ok"

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


def _hi() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="hi")]


def test_a_recovered_primary_points_the_trace_back_at_its_config():
    """A trace spans many model calls and the wrapper tries its primary first
    on each: once the primary serves again, the trace must not keep naming
    the fallback that served an earlier call."""
    ctx = _trace_ctx()
    wrapper = FailoverLLMProvider(primary=_FailsOnce(), fallbacks=[_Answering()])

    async def _two_calls():
        await wrapper.chat(_hi())
        assert ctx["metadata"][TRACE_CONFIG_ID_KEY] == "fallback-config"
        await wrapper.chat(_hi())

    _run_with_trace(ctx, _two_calls)
    meta = ctx["metadata"]
    assert meta[TRACE_CONFIG_ID_KEY] == "primary-config"
    assert meta[TRACE_CONFIG_LABEL_KEY] == "Main key"
    assert ctx["local_metadata"][TRACE_CONFIG_ID_KEY] == "primary-config"
    # The fallback that served the first call stays on record.
    assert meta[TRACE_FAILOVER_KEY] == [
        {"provider": "openai", "config_id": "fallback-config", "label": "Backup key"}
    ]


@pytest.mark.parametrize("method", ["chat_stream", "chat_stream_with_tools"])
def test_a_recovered_primary_is_named_from_its_first_streamed_output(method):
    ctx = _trace_ctx()
    wrapper = FailoverLLMProvider(primary=_FailsOnce(), fallbacks=[_Answering()])
    named_at_first_output: list[str] = []

    async def _on_token(_token: str) -> None:
        named_at_first_output.append(ctx["metadata"][TRACE_CONFIG_ID_KEY])

    async def _two_calls():
        await wrapper.chat(_hi())
        if method == "chat_stream":
            async for chunk in wrapper.chat_stream(_hi()):
                await _on_token(chunk)
        else:
            await wrapper.chat_stream_with_tools(_hi(), on_token=_on_token)

    _run_with_trace(ctx, _two_calls)
    assert named_at_first_output[0] == "primary-config"


def test_a_recovered_primary_does_not_undo_a_budget_downgrade():
    """The downgrade names the config from then on; a later call the
    old wrapper's primary serves (post-turn memory consolidation) must not
    point the trace back at it."""

    class _Cheap(_Answering):
        provider_config_id = "cheap-config"
        provider_config_label = None

    ctx = _trace_ctx()
    wrapper = FailoverLLMProvider(primary=_FailsOnce(), fallbacks=[_Answering()])

    async def _run():
        await wrapper.chat(_hi())
        record_model_downgrade(_Cheap(), from_model="big-model", to_model="cheap-model")
        await wrapper.chat(_hi())

    _run_with_trace(ctx, _run)
    assert ctx["metadata"][TRACE_CONFIG_ID_KEY] == "cheap-config"


def test_another_wrappers_primary_leaves_a_failover_it_did_not_make():
    class _Other(_Answering):
        provider_config_id = "other-config"

    ctx = _trace_ctx()
    agent_llm = FailoverLLMProvider(primary=_FailsOnce(), fallbacks=[_Answering()])
    other_llm = FailoverLLMProvider(primary=_Other(), fallbacks=[_Failing()])

    async def _run():
        await agent_llm.chat(_hi())
        await other_llm.chat(_hi())
        assert ctx["metadata"][TRACE_CONFIG_ID_KEY] == "fallback-config"
        await agent_llm.chat(_hi())

    _run_with_trace(ctx, _run)
    assert ctx["metadata"][TRACE_CONFIG_ID_KEY] == "primary-config"


# -- a budget downgrade ------------------------------------------------


def test_a_budget_downgrade_repoints_the_trace_at_the_model_and_config_it_moved_to():
    ctx = _trace_ctx()
    ctx["metadata"]["model"] = "big-model"
    ctx["local_model"] = "big-model"

    async def _downgrade():
        record_model_downgrade(_Answering(), from_model="big-model", to_model="cheap-model")

    _run_with_trace(ctx, _downgrade)

    meta = ctx["metadata"]
    assert meta[TRACE_CONFIG_ID_KEY] == "fallback-config"
    assert meta[TRACE_CONFIG_LABEL_KEY] == "Backup key"
    assert meta["model"] == "cheap-model"
    assert meta[TRACE_DOWNGRADE_KEY] == {
        "from_model": "big-model",
        "to_model": "cheap-model",
        "provider": "openai",
        "config_id": "fallback-config",
        "label": "Backup key",
    }
    # The type stays the one the run started on, as after a failover.
    assert meta["llm_provider"] == "anthropic"
    # The durable trace names the model the run continued on, even if the run
    # then ends on an error that names none.
    assert ctx["local_model"] == "cheap-model"
    assert ctx["local_metadata"]["model"] == "cheap-model"
    assert ctx["trace"].updates, "the Langfuse root span is updated too"


def test_a_budget_downgrade_with_no_current_trace_is_harmless():
    async def _downgrade():
        record_model_downgrade(_Answering(), from_model="big-model", to_model="cheap-model")

    asyncio.run(_downgrade())


# -- a fallback whose output was cut short -------------------------------------


class _BreaksAfterFirstChunk(_Answering):
    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs) -> AsyncIterator[str]:
        yield "par"
        raise RuntimeError("fallback dropped mid-stream")


def test_a_streamed_fallback_is_named_from_its_first_chunk():
    """The output that reached the caller came from the fallback, even though
    its stream then broke: the trace must not keep naming the primary."""
    ctx = _trace_ctx()
    wrapper = FailoverLLMProvider(primary=_Failing(), fallbacks=[_BreaksAfterFirstChunk()])
    chunks: list[str] = []

    async def _consume():
        async for chunk in wrapper.chat_stream([ChatMessage(role="user", content="hi")]):
            chunks.append(chunk)

    with pytest.raises(AllProvidersFailedError):
        _run_with_trace(ctx, _consume)
    assert chunks == ["par"]
    assert ctx["metadata"][TRACE_CONFIG_ID_KEY] == "fallback-config"


def test_a_streamed_fallback_closed_after_its_first_chunk_is_named_too():
    ctx = _trace_ctx()
    wrapper = FailoverLLMProvider(primary=_Failing(), fallbacks=[_Answering()])

    async def _first_chunk_then_close():
        stream = wrapper.chat_stream([ChatMessage(role="user", content="hi")])
        first = await anext(stream)
        await stream.aclose()
        return first

    assert _run_with_trace(ctx, _first_chunk_then_close) == "ok"
    assert ctx["metadata"][TRACE_CONFIG_ID_KEY] == "fallback-config"


class _StoppedMidAnswer(_Answering):
    """Streams one token, then waits until the turn is stopped."""

    def __init__(self) -> None:
        self.first_token = asyncio.Event()

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, **kwargs):
        await on_token("par")
        self.first_token.set()
        await asyncio.Event().wait()


def test_a_fallback_stopped_after_its_first_token_is_named():
    """A turn cancelled while the fallback streams its answer (the user's
    stop, a timeout) never reaches the return that records a served call."""
    ctx = _trace_ctx()
    fallback = _StoppedMidAnswer()
    wrapper = FailoverLLMProvider(primary=_Failing(), fallbacks=[fallback])
    tokens: list[str] = []

    async def _on_token(token: str) -> None:
        tokens.append(token)

    async def _stopped():
        call = asyncio.ensure_future(
            wrapper.chat_stream_with_tools([ChatMessage(role="user", content="hi")], on_token=_on_token)
        )
        await fallback.first_token.wait()
        call.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await call

    _run_with_trace(ctx, _stopped)
    assert tokens == ["par"], "the caller still receives every token"
    assert ctx["metadata"][TRACE_CONFIG_ID_KEY] == "fallback-config"
    assert len(ctx["metadata"][TRACE_FAILOVER_KEY]) == 1


# -- choosing one config among several of a type -----------------------------


def test_choose_provider_config_prefers_the_owner_of_the_model():
    old = _config("anthropic", available_models=["claude-opus-4-8"])
    new = _config(
        "anthropic",
        available_models=["claude-sonnet-5"],
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    other = _config("openai")
    configs = [other, new, old]

    assert choose_provider_config(configs, "anthropic", "claude-opus-4-8") is old
    assert choose_provider_config(configs, "anthropic", "claude-sonnet-5") is new


def test_choose_provider_config_falls_back_to_default_then_newest():
    base = datetime(2026, 1, 1, tzinfo=UTC)
    older_default = _config("anthropic", available_models=[], is_default=True, created_at=base)
    newest = _config("anthropic", available_models=[], created_at=base + timedelta(days=30))

    assert choose_provider_config([newest, older_default], "anthropic") is older_default
    older_default.is_default = False
    assert choose_provider_config([newest, older_default], "anthropic") is newest
    assert choose_provider_config([newest], "openai") is None


def test_choose_provider_config_does_not_guess_an_owner_from_the_model_prefix():
    """Every candidate is of the one provider, so prefix inference cannot pick
    between them: an unlisted ``claude-*`` model goes to the default config,
    not merely to the newest."""
    base = datetime(2026, 1, 1, tzinfo=UTC)
    default = _config("anthropic", available_models=["claude-opus-4-8"], is_default=True, created_at=base)
    newest = _config("anthropic", available_models=["claude-sonnet-5"], created_at=base + timedelta(days=30))

    assert choose_provider_config([newest, default], "anthropic", "claude-next-6") is default
    default.is_default = False
    assert choose_provider_config([newest, default], "anthropic", "claude-next-6") is newest


# -- Anthropic pass-through --------------------------------------------------


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _Scalars(self._rows)

    def scalar_one_or_none(self):  # what the old lookup called
        if len(self._rows) > 1:
            raise AssertionError("MultipleResultsFound")
        return self._rows[0] if self._rows else None


class _Db:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        return _Result(self._rows)


def test_anthropic_pass_through_picks_one_of_several_anthropic_configs():
    """Two Anthropic configs (an env-seeded row and a UI-created one) used to
    raise MultipleResultsFound on every /v1/messages request: a 500."""
    env_row = _config("anthropic", available_models=["claude-opus-4-8"])
    ui_row = _config(
        "anthropic",
        available_models=["claude-sonnet-5"],
        display_name="UI key",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    db = _Db([env_row, ui_row])

    assert asyncio.run(llm_proxy._get_anthropic_config(db, "claude-opus-4-8")) is env_row
    assert asyncio.run(llm_proxy._get_anthropic_config(db, "claude-sonnet-5")) is ui_row
    assert asyncio.run(llm_proxy._get_anthropic_config(db, None)) is ui_row


def test_anthropic_pass_through_still_503s_without_an_anthropic_config():
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(llm_proxy._get_anthropic_config(_Db([]), "claude-sonnet-5"))
    assert exc_info.value.status_code == 503


# -- every trace writer names the config ------------------------------------


@pytest.mark.parametrize("stream_fails", [False, True])
def test_the_llm_only_evaluation_trace_names_its_provider_config(monkeypatch, stream_fails):
    seen: list[object] = []

    class _Model(_Answering):
        provider_config_id = "eval-config"
        provider_config_label = "Eval key"

        async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
            seen.append(get_current_trace_ctx())
            return ChatResponse(content="answer", model=model or "", usage={})

        async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs):
            seen.append(get_current_trace_ctx())
            if stream_fails:
                raise RuntimeError("streaming unsupported")
            yield "answer"

    trace_kwargs: dict = {}
    trace = {"metadata": {}}

    async def _trace_llm_call(_db, **kwargs):
        trace_kwargs.update(kwargs)
        return trace

    async def _resolve(_db, model_id, **_kwargs):
        return _Model(), model_id

    monkeypatch.setattr(evaluation_engine, "_get_llm_provider_and_model", _resolve)
    monkeypatch.setattr("app.services.langfuse_tracing.trace_llm_call", _trace_llm_call)
    monkeypatch.setattr("app.services.langfuse_tracing.end_trace", lambda *_a, **_kw: None)
    agent = SimpleNamespace(
        id=uuid.uuid4(), name="Analyst", slug="analyst", persona="p", temperature=None,
        output_format=None, output_schema=None,
    )

    async def _run():
        result = await evaluation_engine._execute_agent_llm_only(None, agent, "gpt-4o", "hi")
        return result, get_current_trace_ctx()

    (output, *_rest), current_after = asyncio.run(_run())

    assert output == "answer"
    assert trace_kwargs["metadata"] == {
        "execution_mode": "llm_only",
        "llm_provider": "openai",
        TRACE_CONFIG_ID_KEY: "eval-config",
        TRACE_CONFIG_LABEL_KEY: "Eval key",
    }
    # Current while the model answers (and for the non-streamed retry), so a
    # failover could re-point it; reset once the call is over.
    assert seen and all(ctx is trace for ctx in seen)
    assert current_after is None


# Traces that wrap other traces: their model calls run in the child traces of
# the agents they call, which name the config themselves.
_WRAPPER_TRACES = ("llm-proxy-agent", "flow-run:")


def _called_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _literal_head(node: ast.expr | None) -> str:
    """The literal text a ``name=`` value starts with (an f-string's head)."""
    if isinstance(node, ast.JoinedStr) and node.values:
        node = node.values[0]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def test_every_trace_around_an_llm_call_names_the_provider_config():
    """Counted per trace, not per file: a second trace in a file that already
    stamps one must not ride on the first (the LLM-only evaluation trace did)."""
    writers: set[str] = set()
    unstamped: list[str] = []
    for path in sorted((BACKEND / "app").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "trace_llm_call" not in source:
            continue
        tree = ast.parse(source)
        openers = {"trace_llm_call"} | {
            alias.asname
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == "trace_llm_call" and alias.asname
        }
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        traces = [
            call
            for call in calls
            if _called_name(call) in openers
            and not _literal_head(
                next((kw.value for kw in call.keywords if kw.arg == "name"), None)
            ).startswith(_WRAPPER_TRACES)
        ]
        if not traces:
            continue
        relative = path.relative_to(BACKEND).as_posix()
        writers.add(relative)
        stamps = sum(_called_name(call) in {"provider_config_metadata", "config_metadata"} for call in calls)
        if stamps < len(traces):
            unstamped.append(f"{relative}: {len(traces)} trace(s), {stamps} config stamp(s)")

    assert not unstamped, unstamped
    # The scan found the writers it exists for, so it cannot pass on nothing.
    assert writers >= {
        "app/services/agent_chat.py",
        "app/api/v1/public_chat.py",
        "app/services/assignment_engine.py",
        "app/services/general_chat.py",
        "app/services/evaluation_engine.py",
        "app/api/v1/llm_proxy.py",
    }
