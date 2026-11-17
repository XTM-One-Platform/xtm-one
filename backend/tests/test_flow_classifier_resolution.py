"""An unpinned Classify node among several decision providers."""

import asyncio
from types import SimpleNamespace

import pytest

from app.services import flow_classifier
from app.services.flow_classifier import (
    AMBIGUOUS_PROVIDER_MESSAGE,
    UNLISTED_MODEL_MESSAGE,
    ClassifierError,
    _only_provider_listing,
)

UNANSWERED = "did not answer in full when asked for their decision models"


@pytest.fixture(autouse=True)
def _forget_resolutions():
    flow_classifier._resolved.clear()
    flow_classifier._inflight.clear()
    yield
    flow_classifier._resolved.clear()
    flow_classifier._inflight.clear()


class _Provider:
    def __init__(self, listed=(), error=None, delay=0.0, whole=True, lists=True):
        self.listed = list(listed)
        self.error = error
        self.delay = delay
        self.whole = whole
        self.lists_decision_models = lists
        self.asked = 0

    async def decision_listing(self):
        self.asked += 1
        await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.listed, self.whole


def _configs(monkeypatch, *providers):
    configs = [
        SimpleNamespace(id=f"cfg-{i}", updated_at="t0", provider=f"p{i}", display_name=None, provider_instance=p)
        for i, p in enumerate(providers)
    ]

    async def provider_for_config(config):
        return config.provider_instance

    monkeypatch.setattr("app.llm.router.provider_for_config", provider_for_config)
    return configs


@pytest.mark.asyncio
async def test_the_only_provider_listing_the_model_is_taken(monkeypatch):
    ollama, gateway = _configs(monkeypatch, _Provider(["clef-flash"]), _Provider([]))

    assert await _only_provider_listing([ollama, gateway], "clef-flash") is ollama


@pytest.mark.asyncio
async def test_several_providers_listing_the_model_ask_for_a_pin(monkeypatch):
    configs = _configs(monkeypatch, _Provider(["clef-flash"]), _Provider(["clef-flash"]))

    with pytest.raises(ClassifierError, match=AMBIGUOUS_PROVIDER_MESSAGE):
        await _only_provider_listing(configs, "clef-flash")


@pytest.mark.asyncio
async def test_a_silent_listing_counts_as_failed(monkeypatch):
    monkeypatch.setattr("app.services.flow_classifier.DECISION_LISTING_TIMEOUT_SECONDS", 0.2)
    configs = _configs(monkeypatch, _Provider(["clef-flash"]), _Provider(["clef-flash"], delay=60))

    with pytest.raises(ClassifierError, match=UNANSWERED):
        await _only_provider_listing(configs, "clef-flash")


@pytest.mark.asyncio
async def test_a_unique_answer_is_remembered_for_the_next_executions(monkeypatch):
    ollama, gateway = _configs(monkeypatch, _Provider(["clef-flash"]), _Provider([]))

    assert await _only_provider_listing([ollama, gateway], "clef-flash") is ollama
    assert await _only_provider_listing([ollama, gateway], "clef-flash") is ollama

    assert (ollama.provider_instance.asked, gateway.provider_instance.asked) == (1, 1)


@pytest.mark.asyncio
async def test_a_remembered_answer_expires(monkeypatch):
    ollama, gateway = _configs(monkeypatch, _Provider(["clef-flash"]), _Provider([]))
    await _only_provider_listing([ollama, gateway], "clef-flash")
    now = flow_classifier.time.monotonic()
    monkeypatch.setattr(flow_classifier.time, "monotonic", lambda: now + flow_classifier._RESOLVED_TTL_SECONDS + 1)

    await _only_provider_listing([ollama, gateway], "clef-flash")

    assert ollama.provider_instance.asked == 2


@pytest.mark.asyncio
async def test_an_edited_candidate_is_asked_again(monkeypatch):
    ollama, gateway = _configs(monkeypatch, _Provider(["clef-flash"]), _Provider([]))
    await _only_provider_listing([ollama, gateway], "clef-flash")
    gateway.updated_at = "t1"
    gateway.provider_instance.listed = ["clef-flash"]

    with pytest.raises(ClassifierError, match=AMBIGUOUS_PROVIDER_MESSAGE):
        await _only_provider_listing([ollama, gateway], "clef-flash")


@pytest.mark.asyncio
async def test_an_ambiguous_sweep_is_remembered_briefly(monkeypatch):
    providers = [_Provider(["clef-flash"]), _Provider(["clef-flash"])]
    configs = _configs(monkeypatch, *providers)

    for _ in range(2):
        with pytest.raises(ClassifierError, match=AMBIGUOUS_PROVIDER_MESSAGE):
            await _only_provider_listing(configs, "clef-flash")
    assert providers[0].asked == 1

    now = flow_classifier.time.monotonic()
    monkeypatch.setattr(flow_classifier.time, "monotonic", lambda: now + flow_classifier._UNRESOLVED_TTL_SECONDS + 1)
    with pytest.raises(ClassifierError, match=AMBIGUOUS_PROVIDER_MESSAGE):
        await _only_provider_listing(configs, "clef-flash")
    assert providers[0].asked == 2


@pytest.mark.asyncio
async def test_a_whole_listing_without_the_model_leaves_the_provider_that_cannot_list(monkeypatch):
    # The gateway declares its decision models; OpenRouter's typed ids are listed nowhere.
    openrouter, gateway = _configs(monkeypatch, _Provider(lists=False), _Provider(["tev1:0.8b"]))

    assert await _only_provider_listing([openrouter, gateway], "typesafe/jev-1.13") is openrouter
    assert openrouter.provider_instance.asked == 0


@pytest.mark.asyncio
async def test_several_providers_that_cannot_list_ask_for_a_pin(monkeypatch):
    configs = _configs(monkeypatch, _Provider(lists=False), _Provider(lists=False), _Provider(["tev1:0.8b"]))

    with pytest.raises(ClassifierError, match=AMBIGUOUS_PROVIDER_MESSAGE):
        await _only_provider_listing(configs, "typesafe/jev-1.13")


@pytest.mark.asyncio
async def test_a_failed_listing_is_named_and_remembered_briefly(monkeypatch):
    # A local Ollama that is down may be the provider serving the model.
    failing = _Provider(error=RuntimeError("down"))
    configs = _configs(monkeypatch, failing, _Provider([]))

    for _ in range(2):
        with pytest.raises(ClassifierError, match=UNANSWERED):
            await _only_provider_listing(configs, "clef-flash")
    assert failing.asked == 1

    now = flow_classifier.time.monotonic()
    monkeypatch.setattr(flow_classifier.time, "monotonic", lambda: now + flow_classifier._UNRESOLVED_TTL_SECONDS + 1)
    with pytest.raises(ClassifierError, match=UNANSWERED):
        await _only_provider_listing(configs, "clef-flash")
    assert failing.asked == 2


@pytest.mark.asyncio
async def test_the_message_names_the_providers_that_did_not_answer(monkeypatch):
    configs = _configs(monkeypatch, _Provider(error=RuntimeError("down")), _Provider([]))
    configs[0].display_name = "Local Ollama"

    with pytest.raises(ClassifierError, match=f"{UNANSWERED}.*: Local Ollama\\. Pick"):
        await _only_provider_listing(configs, "clef-flash")


@pytest.mark.asyncio
async def test_a_partial_listing_counts_as_failed_even_beside_a_reporting_provider(monkeypatch):
    # OpenAI mid-round may serve the id a gateway reports.
    configs = _configs(monkeypatch, _Provider([], whole=False), _Provider(["gpt-6-luna"]))

    with pytest.raises(ClassifierError, match=f"{UNANSWERED}.*: p0\\."):
        await _only_provider_listing(configs, "gpt-6-luna")


@pytest.mark.asyncio
async def test_a_partial_listing_reporting_the_model_still_reports_it(monkeypatch):
    openai, gateway = _configs(monkeypatch, _Provider(["gpt-6-luna"], whole=False), _Provider(["tev1:0.8b"]))

    assert await _only_provider_listing([openai, gateway], "gpt-6-luna") is openai


@pytest.mark.asyncio
async def test_an_endpoint_declaring_no_decision_model_is_no_candidate(monkeypatch):
    # OpenRouter beside a vLLM endpoint: as before the endpoint spoke decisions.
    openrouter, vllm = _configs(monkeypatch, _Provider(lists=False), _Provider([]))

    assert await _only_provider_listing([openrouter, vllm], "typesafe/jev-1.13") is openrouter


@pytest.mark.asyncio
async def test_providers_whose_whole_listings_omit_the_model_leave_no_candidate(monkeypatch):
    # A local Ollama serving tev1 only, beside a vLLM endpoint declaring nothing.
    configs = _configs(monkeypatch, _Provider(["tev1:0.8b"]), _Provider([]))

    with pytest.raises(ClassifierError, match=UNLISTED_MODEL_MESSAGE):
        await _only_provider_listing(configs, "other-model")


@pytest.mark.asyncio
async def test_concurrent_executions_share_one_sweep(monkeypatch):
    ollama, gateway = _configs(monkeypatch, _Provider(["clef-flash"], delay=0.05), _Provider([]))

    found = await asyncio.gather(*(_only_provider_listing([ollama, gateway], "clef-flash") for _ in range(5)))

    assert found == [ollama] * 5
    assert ollama.provider_instance.asked == 1
    assert flow_classifier._inflight == {}


@pytest.mark.asyncio
async def test_a_provider_reporting_the_model_beside_a_failed_listing_is_not_taken(monkeypatch):
    # A local Ollama that is down may serve the id a gateway reports.
    configs = _configs(monkeypatch, _Provider(error=RuntimeError("down")), _Provider(["clef-flash"]))

    with pytest.raises(ClassifierError, match=UNANSWERED):
        await _only_provider_listing(configs, "clef-flash")


@pytest.mark.asyncio
async def test_the_remembered_answers_are_bounded(monkeypatch):
    monkeypatch.setattr(flow_classifier, "_MAX_RESOLVED", 3)
    ollama, gateway = _configs(monkeypatch, _Provider([f"m{i}" for i in range(5)]), _Provider([]))

    for i in range(5):
        await _only_provider_listing([ollama, gateway], f"m{i}")

    assert sorted(model for _, model in flow_classifier._resolved) == ["m2", "m3", "m4"]
