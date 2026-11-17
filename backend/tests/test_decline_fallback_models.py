"""A declined request is asked again of an older model of the same provider.

Claude Sonnet 5.5 and Opus 5.5 decline offensive-security work in the
``cyber`` category, and the failover chain handed it to another Claude 5.5
model, which declined it too: every OpenAEV attack-chaining cycle failed with
``AllProvidersFailedError``. A model now declares the models its declines go
to (``decline_fallback_models``): a Sonnet 5.5 decline goes to Sonnet 5, then
to Opus 4.8; an Opus 5.x or Fable 5.x decline to Opus 4.8, the model Anthropic
routes cyber declines to. They are tried on the primary's own provider, in
order, before the rest of the chain, and only for the categories another model
may answer (``cyber``, ``frontier_llm``).
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.llm import router
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, ModelDeclinedError
from app.llm.model_capabilities import decline_fallback_models
from app.llm.router import AllProvidersFailedError, FailoverLLMProvider

_SONNET = "claude-sonnet-5-5"


def _messages() -> list[ChatMessage]:
    return [ChatMessage(role="user", content="Chain the next attack step.")]


# ── What each model declares ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-sonnet-5-5", ("claude-sonnet-5", "claude-opus-4-8")),
        ("anthropic/claude-sonnet-5.5", ("claude-sonnet-5", "claude-opus-4-8")),
        ("claude-sonnet-5", ("claude-opus-4-8",)),
        ("claude-opus-5-5", ("claude-opus-4-8",)),
        ("claude-opus-5", ("claude-opus-4-8",)),
        ("claude-fable-5", ("claude-opus-4-8",)),
        ("claude-fable-5-1", ("claude-opus-4-8",)),
        ("claude-opus-4-8", ()),
        ("claude-haiku-5-5", ()),
        ("claude-sonnet-4-6", ()),
        ("gpt-6", ()),
        ("", ()),
        (None, ()),
    ],
)
def test_the_models_a_decline_goes_to(model: str | None, expected: tuple[str, ...]):
    assert decline_fallback_models(model) == expected


# ── The failover wrapper asks them ─────────────────────────────────────────


class _Provider(BaseLLMProvider):
    """Declines on the models in *declines*, answers on any other."""

    default_model = _SONNET
    context_window = 100_000

    def __init__(
        self,
        name: str = "anthropic",
        *,
        declines: dict[str, str | None] | None = None,
        error: Exception | None = None,
        errors: dict[str, Exception] | None = None,
        config_id: str | None = None,
    ) -> None:
        self.provider_name = name
        self.provider_config_id = config_id
        self.declines = declines or {}
        self.error = error
        self.errors = errors or {}
        self.calls: list[tuple[str | None, dict]] = []

    def _answer(self, model: str | None, kwargs: dict) -> str:
        self.calls.append((model, kwargs))
        if self.error is not None:
            raise self.error
        requested = model or self.default_model
        if requested in self.errors:
            raise self.errors[requested]
        if requested in self.declines:
            raise ModelDeclinedError(model=requested, category=self.declines[requested])
        return f"answered by {model}"

    async def chat(self, messages, model=None, **kwargs):
        return ChatResponse(content=self._answer(model, kwargs), model=model)

    async def chat_stream(self, messages, model=None, **kwargs):
        yield self._answer(model, kwargs)

    async def chat_stream_with_tools(self, messages, tools=None, *, on_token=None, model=None, **kwargs):
        return ChatResponse(content=self._answer(model, kwargs), model=model)

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []

    @property
    def models_called(self) -> list[str | None]:
        return [model for model, _ in self.calls]


async def _stream(wrapper: FailoverLLMProvider) -> str:
    return "".join([chunk async for chunk in wrapper.chat_stream(_messages(), model=_SONNET)])


async def _chat(wrapper: FailoverLLMProvider) -> str:
    return (await wrapper.chat(_messages(), model=_SONNET)).content


async def _chat_with_tools(wrapper: FailoverLLMProvider) -> str:
    return (await wrapper.chat_stream_with_tools(_messages(), model=_SONNET)).content


_PATHS = [
    pytest.param(_chat, id="chat"),
    pytest.param(_stream, id="chat_stream"),
    pytest.param(_chat_with_tools, id="chat_stream_with_tools"),
]


@pytest.mark.parametrize("call", _PATHS)
async def test_a_cyber_decline_of_sonnet_5_5_is_answered_by_sonnet_5(call):
    primary = _Provider(declines={_SONNET: "cyber"})
    other = _Provider("openai")
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[other])

    answer = await call(wrapper)

    assert answer == "answered by claude-sonnet-5"
    assert primary.models_called == [_SONNET, "claude-sonnet-5"]
    assert other.calls == []
    assert wrapper.last_model == "claude-sonnet-5"


@pytest.mark.parametrize("call", _PATHS)
async def test_a_decline_of_sonnet_5_too_goes_to_opus_4_8(call):
    primary = _Provider(declines={_SONNET: "cyber", "claude-sonnet-5": "cyber"})
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[])

    answer = await call(wrapper)

    assert answer == "answered by claude-opus-4-8"
    assert primary.models_called == [_SONNET, "claude-sonnet-5", "claude-opus-4-8"]
    assert wrapper.last_model == "claude-opus-4-8"


async def test_an_opus_5_5_decline_goes_to_opus_4_8():
    primary = _Provider(declines={"claude-opus-5-5": "cyber"})
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[])

    response = await wrapper.chat(_messages(), model="claude-opus-5-5")

    assert response.content == "answered by claude-opus-4-8"
    assert primary.models_called == ["claude-opus-5-5", "claude-opus-4-8"]


async def test_a_frontier_llm_decline_goes_to_the_declared_models_too():
    primary = _Provider(declines={_SONNET: "frontier_llm"})
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[])

    assert await _chat(wrapper) == "answered by claude-sonnet-5"
    assert primary.models_called == [_SONNET, "claude-sonnet-5"]


async def test_the_primary_default_model_is_the_requested_one_when_none_is_named():
    primary = _Provider(declines={_SONNET: "cyber"})
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[])

    response = await wrapper.chat(_messages())

    assert response.content == "answered by claude-sonnet-5"
    assert primary.models_called == [None, "claude-sonnet-5"]


async def test_the_rest_of_the_chain_follows_when_every_declared_model_declines():
    declines = {_SONNET: "cyber", "claude-sonnet-5": "cyber", "claude-opus-4-8": "cyber"}
    primary = _Provider(declines=declines)
    other = _Provider("openai")
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[other])

    assert await _chat(wrapper) == "answered by None"
    assert primary.models_called == [_SONNET, "claude-sonnet-5", "claude-opus-4-8"]
    assert other.models_called == [None]


async def test_a_chain_of_declines_ends_on_the_decline():
    declines = {_SONNET: "cyber", "claude-sonnet-5": "cyber", "claude-opus-4-8": "cyber"}
    wrapper = FailoverLLMProvider(primary=_Provider(declines=declines), fallbacks=[])

    with pytest.raises(AllProvidersFailedError) as caught:
        await _chat(wrapper)

    assert [type(exc) for _, exc in caught.value.failures] == [ModelDeclinedError] * 3
    assert caught.value.failures[-1][1].model == "claude-opus-4-8"


@pytest.mark.parametrize("category", ["bio", "reasoning_extraction", "general_harms", None])
async def test_a_decline_no_other_model_may_answer_is_not_handed_on(category):
    primary = _Provider(declines={_SONNET: category})
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[_Provider("openai")])

    with pytest.raises(ModelDeclinedError):
        await _chat(wrapper)

    assert primary.models_called == [_SONNET]


async def test_another_failure_of_the_primary_skips_the_declared_models():
    primary = _Provider(error=TimeoutError("read timed out"))
    other = _Provider("openai")
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[other])

    assert await _chat(wrapper) == "answered by None"
    assert primary.models_called == [_SONNET]
    assert other.models_called == [None]


async def test_a_model_without_declared_fallbacks_goes_to_the_chain():
    primary = _Provider(declines={"claude-opus-4-8": "cyber"})
    other = _Provider("openai")
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[other])

    response = await wrapper.chat(_messages(), model="claude-opus-4-8")

    assert response.content == "answered by None"
    assert primary.models_called == ["claude-opus-4-8"]
    assert other.models_called == [None]


async def test_the_admin_fallback_model_is_not_asked_twice():
    config_id = str(uuid.uuid4())
    declines = {_SONNET: "cyber", "claude-sonnet-5": "cyber", "claude-opus-4-8": "cyber"}
    primary = _Provider(declines=declines, config_id=config_id)
    other = _Provider("openai")
    wrapper = FailoverLLMProvider(
        primary=primary,
        fallbacks=[other],
        fallback_model="claude-opus-4-8",
        fallback_model_config=SimpleNamespace(id=config_id),
        prepared={config_id: primary},
    )

    await _chat(wrapper)

    assert primary.models_called == [_SONNET, "claude-sonnet-5", "claude-opus-4-8"]
    assert other.models_called == [None]


@pytest.mark.parametrize("call", _PATHS)
async def test_the_same_provider_keeps_the_structured_output_and_the_effort(call):
    primary = _Provider(declines={_SONNET: "cyber"})
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[])
    schema = {"type": "json_schema", "schema": {"type": "object"}}

    if call is _stream:
        chunks = wrapper.chat_stream(
            _messages(), model=_SONNET, structured_output=schema, reasoning_effort="high"
        )
        assert "".join([c async for c in chunks]) == "answered by claude-sonnet-5"
    else:
        method = wrapper.chat if call is _chat else wrapper.chat_stream_with_tools
        await method(_messages(), model=_SONNET, structured_output=schema, reasoning_effort="high")

    _, fallback_kwargs = primary.calls[1]
    assert fallback_kwargs["structured_output"] == schema
    assert fallback_kwargs["reasoning_effort"] == "high"


async def test_another_provider_still_gets_neither():
    primary = _Provider(error=TimeoutError("read timed out"))
    other = _Provider("openai")
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[other])

    await wrapper.chat(
        _messages(), model=_SONNET, structured_output={"type": "json_schema"}, reasoning_effort="high"
    )

    _, fallback_kwargs = other.calls[0]
    assert "structured_output" not in fallback_kwargs
    assert "reasoning_effort" not in fallback_kwargs


async def test_an_admin_fallback_strips_the_kwargs_even_on_the_primary_instance():
    # An admin fallback model is a chain target, not a decline fallback
    # (``index < declined`` is False for it), so structured_output /
    # reasoning_effort are stripped - whether or not the provider cache happens
    # to hand the admin fallback config back the SAME instance as the primary.
    config_id = str(uuid.uuid4())
    # The primary's own model fails over (not a decline); the admin fallback
    # model answers, served by this very instance (prepared == primary).
    primary = _Provider(
        config_id=config_id,
        errors={_SONNET: RuntimeError("transient 503")},
    )
    wrapper = FailoverLLMProvider(
        primary=primary,
        fallbacks=[],
        fallback_model="claude-opus-4-8",
        fallback_model_config=SimpleNamespace(id=config_id),
        prepared={config_id: primary},
    )

    answer = await wrapper.chat(
        _messages(), model=_SONNET, structured_output={"type": "json_schema"}, reasoning_effort="high"
    )

    assert answer.content == "answered by claude-opus-4-8"
    assert primary.models_called == [_SONNET, "claude-opus-4-8"]
    _, fallback_kwargs = primary.calls[1]  # the admin fallback call
    assert "structured_output" not in fallback_kwargs
    assert "reasoning_effort" not in fallback_kwargs


# ── Later calls of the run go straight to the model that answered ──────────


@pytest.mark.parametrize("call", _PATHS)
async def test_later_calls_skip_the_declined_model(call):
    primary = _Provider(declines={_SONNET: "cyber"})
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[])

    answers = [await call(wrapper) for _ in range(3)]

    # One decline on the first call; the next two go straight to the answer.
    assert answers == ["answered by claude-sonnet-5"] * 3
    assert primary.models_called == [
        _SONNET, "claude-sonnet-5", "claude-sonnet-5", "claude-sonnet-5",
    ]
    assert wrapper.last_model == "claude-sonnet-5"


@pytest.mark.parametrize("call", _PATHS)
async def test_the_run_stays_on_the_second_fallback_once_it_answers(call):
    primary = _Provider(declines={_SONNET: "cyber", "claude-sonnet-5": "cyber"})
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[])

    await call(wrapper)
    await call(wrapper)

    assert primary.models_called == [
        _SONNET, "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-8",
    ]
    assert wrapper.last_model == "claude-opus-4-8"


@pytest.mark.parametrize("call", _PATHS)
async def test_a_routed_model_that_later_declines_moves_to_the_next(call):
    primary = _Provider(declines={_SONNET: "cyber"})
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[])

    first = await call(wrapper)
    # The routed model starts declining too: the run moves on and stays there.
    primary.declines["claude-sonnet-5"] = "cyber"
    second = await call(wrapper)
    third = await call(wrapper)

    assert first == "answered by claude-sonnet-5"
    assert second == third == "answered by claude-opus-4-8"
    assert primary.models_called == [
        _SONNET,
        "claude-sonnet-5",
        "claude-sonnet-5",
        "claude-opus-4-8",
        "claude-opus-4-8",
    ]


async def test_a_transient_failure_of_the_routed_model_never_reasks_the_requested_one():
    primary = _Provider(declines={_SONNET: "cyber"})
    other = _Provider("openai")
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[other])
    await _chat(wrapper)

    # The routed model now fails transiently (not a decline): the chain moves
    # to the other provider, and the requested model is never asked again.
    primary.error = TimeoutError("read timed out")
    answer = await _chat(wrapper)

    assert answer == "answered by None"
    assert primary.models_called == [_SONNET, "claude-sonnet-5", "claude-sonnet-5"]
    assert other.models_called == [None]


async def test_a_new_run_starts_from_the_requested_model_again():
    primary = _Provider(declines={_SONNET: "cyber"})
    first_run = FailoverLLMProvider(primary=primary, fallbacks=[])
    await _chat(first_run)

    primary.calls.clear()
    second_run = FailoverLLMProvider(primary=primary, fallbacks=[])
    await _chat(second_run)

    # A fresh wrapper (a new agent run) does not inherit the first run's route.
    assert primary.models_called == [_SONNET, "claude-sonnet-5"]


async def test_a_routed_call_that_answers_names_the_fallback_on_the_trace():
    primary = _Provider(declines={_SONNET: "cyber"})
    wrapper = FailoverLLMProvider(primary=primary, fallbacks=[])
    await _chat(wrapper)

    # The second call runs on the primary instance but as the fallback model:
    # last_model must still name the model that answered, not the requested one.
    wrapper.last_model = None
    await _chat(wrapper)
    assert wrapper.last_model == "claude-sonnet-5"


# ── One provider is enough to get the wrapper ──────────────────────────────


class _Result:
    def __init__(self, configs):
        self._configs = configs

    def scalars(self):
        return self

    def all(self):
        return self._configs

    def scalar_one_or_none(self):
        return self._configs[0] if self._configs else None


class _Session:
    def __init__(self, configs):
        # The provider-configs query comes first; any later lookup (e.g. the
        # General Assistant config when no model is resolved) sees an empty
        # result rather than an error, so a model-less resolution still runs.
        self._results = [_Result(configs)]

    async def execute(self, _statement):
        return self._results.pop(0) if self._results else _Result([])


@pytest.mark.parametrize(
    ("model", "wrapped"),
    [(_SONNET, True), ("claude-opus-5-5", True), ("claude-sonnet-4-6", False)],
)
def test_a_single_anthropic_provider_is_wrapped_for_a_model_with_declared_fallbacks(
    monkeypatch, model, wrapped
):
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=[model],
            default_model=None,
        )
    ]

    async def _platform_settings(_db):
        return SimpleNamespace(
            agent_default_model=None, agent_default_provider_id=None, fallback_model=None
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _platform_settings
    )
    monkeypatch.setattr(router, "_get_cached_provider", lambda cfg: _Provider(cfg.provider))

    provider, resolved = asyncio.run(
        router.get_llm_for_user(_Session(configs), user_id=None, model_override=model)
    )

    assert resolved == model
    assert isinstance(provider, FailoverLLMProvider) is wrapped
    assert provider.provider_name == "anthropic"


def test_a_single_anthropic_provider_is_wrapped_when_no_model_is_resolved(monkeypatch):
    # No model_override and no platform/agent default: the call will run on the
    # provider's built-in default (Sonnet 5.5, which declines), so the wrapper
    # must still be created off that default rather than off a None model.
    configs = [
        SimpleNamespace(
            id=uuid.uuid4(),
            provider="anthropic",
            available_models=[_SONNET],
            default_model=None,
        )
    ]

    async def _platform_settings(_db):
        return SimpleNamespace(
            agent_default_model=None, agent_default_provider_id=None, fallback_model=None
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _platform_settings
    )
    # The built-in default of the resolved provider is Sonnet 5.5.
    monkeypatch.setattr(router, "_get_cached_provider", lambda cfg: _Provider(cfg.provider))

    provider, _ = asyncio.run(
        router.get_llm_for_user(_Session(configs), user_id=None, model_override=None)
    )

    assert isinstance(provider, FailoverLLMProvider)
    assert not provider.has_configured_fallbacks  # decline fallbacks only


# ── The LLM proxy unwraps a decline-only wrapper to the Anthropic bridge ────


def test_the_proxy_unwraps_a_decline_only_wrapper_to_reach_the_anthropic_bridge():
    # A single-Anthropic install is now wrapped for its decline fallbacks.
    # The proxy's Anthropic-bridge predicate keys on a `.client` with
    # `messages`, which the wrapper does not have - so without unwrapping it
    # would drop to the generic provider-chat path (losing top_p / stop and the
    # overflow pre-flight, and re-wrapping raw errors as AllProvidersFailedError).
    # Unwrapping a decline-only wrapper restores the bridge; a wrapper with a
    # real chain keeps its cross-provider failover.
    from app.api.v1.llm_proxy import _has_openai_sdk

    class _AnthropicLike(_Provider):
        def __init__(self):
            super().__init__("anthropic")
            self.default_model = _SONNET
            self.client = SimpleNamespace(messages=SimpleNamespace())

    def bridge_predicate(provider) -> bool:
        # The exact condition the proxy uses to pick ``_proxy_via_anthropic``.
        return (
            not _has_openai_sdk(provider)
            and hasattr(provider, "client")
            and hasattr(provider.client, "messages")
        )

    def proxy_target(provider):
        # Mirror the proxy's unwrap step for a decline-only wrapper.
        if isinstance(provider, FailoverLLMProvider) and not provider.has_configured_fallbacks:
            return provider.primary
        return provider

    primary = _AnthropicLike()

    # Decline-only wrapper (single Anthropic provider, no chain): no .client,
    # so the wrapper itself misses the bridge, but the unwrapped primary hits it.
    decline_only = FailoverLLMProvider(primary=primary, fallbacks=[])
    assert not decline_only.has_configured_fallbacks
    assert not bridge_predicate(decline_only)
    assert proxy_target(decline_only) is primary
    assert bridge_predicate(proxy_target(decline_only))

    # A wrapper with a cross-provider chain is NOT unwrapped: it keeps failover.
    with_chain = FailoverLLMProvider(primary=primary, fallbacks=[_Provider("openai")])
    assert with_chain.has_configured_fallbacks
    assert proxy_target(with_chain) is with_chain

    # A wrapper with an admin fallback model is NOT unwrapped either.
    with_admin_model = FailoverLLMProvider(
        primary=primary,
        fallbacks=[],
        fallback_model="claude-opus-4-8",
        fallback_model_config=SimpleNamespace(id=str(uuid.uuid4())),
    )
    assert with_admin_model.has_configured_fallbacks
    assert proxy_target(with_admin_model) is with_admin_model
