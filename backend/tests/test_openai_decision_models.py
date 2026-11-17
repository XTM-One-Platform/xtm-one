"""OpenAI's decision models, found by asking its Decisions API (spec 028).

The probe and its outcomes are ``specs/028-openai-decision-models/contracts/
openai-decision-probe.md``; the memo is its ``data-model.md``.
"""

import asyncio
import inspect
import re
from types import SimpleNamespace

import httpx
import openai
import pytest

from app.llm import decisions, openai_provider
from app.llm.openai_provider import OpenAIProvider

_URL = "https://api.openai.com/v1/decisions"
_CONTROL = re.compile(r"xtm-probe-[0-9a-f]{32}")

# The bodies OpenAI answered on 2026-10-16.
_UNKNOWN_MODEL = {
    "message": "The model `gpt-4o-mini` does not exist or you do not have access to it.",
    "type": "invalid_request_error",
    "param": None,
    "code": "model_not_found",
}
_DUPLICATE_CHOICE = {
    "message": "Choice values must be unique within the question.",
    "type": "invalid_request_error",
    "param": "questions[0].choices[1].value",
    "code": None,
}


def _error(cls, status: int, body: dict) -> openai.APIStatusError:
    request = httpx.Request("POST", _URL)
    return cls(body["message"], response=httpx.Response(status, request=request, json={"error": body}), body=body)


def _unknown() -> openai.APIStatusError:
    return _error(openai.NotFoundError, 404, _UNKNOWN_MODEL)


def _duplicate() -> openai.APIStatusError:
    return _error(openai.BadRequestError, 400, _DUPLICATE_CHOICE)


def _other(cls, status: int, param: str | None = None) -> openai.APIStatusError:
    return _error(cls, status, {"message": "refused", "type": "x", "param": param, "code": None})


class _Client:
    """``AsyncOpenAI`` as ``list_decision_models`` uses it."""

    def __init__(
        self, listed, answer, *, api_key="sk-test", base_url="https://api.openai.com/v1/", list_error=None, list_delay=0.0
    ):
        self.listed = list(listed)
        self.answer = answer
        self.api_key = api_key
        self.base_url = base_url
        self.list_error = list_error
        self.list_delay = list_delay
        self.list_cancelled = False
        self.options: dict = {}
        self.probed: list[str] = []
        self.bodies: list[dict] = []
        self.models = self

    def with_options(self, **options):
        self.options = options
        return self

    async def list(self):
        if self.list_delay:
            try:
                await asyncio.sleep(self.list_delay)
            except asyncio.CancelledError:
                self.list_cancelled = True
                raise
        if self.list_error:
            raise self.list_error
        return SimpleNamespace(data=[SimpleNamespace(id=m) for m in self.listed])

    async def post(self, path, *, cast_to, body):
        assert path == "/decisions"
        assert cast_to is object
        self.probed.append(body["model"])
        self.bodies.append(body)
        outcome = await self.answer(body["model"]) if inspect.iscoroutinefunction(self.answer) else self.answer(body["model"])
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _answers(decision_models=("gpt-dec",)):
    """OpenAI as measured: unknown models (the control included) refused as such, decision models for the duplicate."""

    def answer(model):
        return _duplicate() if model in decision_models else _unknown()

    return answer


class _Redis:
    def __init__(self, *, fail=False):
        self.values: dict[str, str] = {}
        self.expiry: dict[str, int] = {}
        self.fail = fail

    async def mget(self, keys):
        if self.fail:
            raise ConnectionError("redis down")
        return [self.values.get(k) for k in keys]

    async def set(self, key, value, nx=False, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        if nx and key in self.values:
            return None
        self.values[key] = value
        self.expiry[key] = ex
        return True

    async def eval(self, script, numkeys, key, token):
        if self.fail:
            raise ConnectionError("redis down")
        assert script == decisions._RELEASE_IF_OWNER_LUA
        if self.values.get(key) == token:
            del self.values[key]

    def pipeline(self, transaction=True):
        redis = self

        class _Pipe:
            def __init__(self):
                self.ops = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def set(self, key, value, ex=None):
                self.ops.append((key, value, ex))

            async def execute(self):
                if redis.fail:
                    raise ConnectionError("redis down")
                for key, value, ex in self.ops:
                    redis.values[key] = value
                    redis.expiry[key] = ex

        return _Pipe()


_UNSETTLED_LISTING = OpenAIProvider.list_decision_models


@pytest.fixture(autouse=True)
def _settled_bookkeeping(monkeypatch):
    """Each listing returns once the round's memo and release are done, as the assertions read them."""

    async def listed(self):
        try:
            return await _UNSETTLED_LISTING(self)
        finally:
            await asyncio.gather(*list(openai_provider._decision_bookkeeping))

    monkeypatch.setattr(OpenAIProvider, "list_decision_models", listed)


@pytest.fixture
def redis(monkeypatch):
    store = _Redis()

    async def _client():
        return store

    monkeypatch.setattr("app.llm.decisions.redis_client", _client)
    return store


def _provider(client, config_id="cfg-1"):
    provider = OpenAIProvider(api_key="sk-test")
    provider.client = client
    provider.provider_config_id = config_id
    return provider


def _non_control(client):
    return [m for m in client.probed if not _CONTROL.fullmatch(m)]


# --- the probe ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_control_is_probed_first_then_each_listed_model(redis):
    client = _Client(["gpt-chat", "gpt-dec"], _answers())

    assert await _provider(client).list_decision_models() == ["gpt-dec"]

    assert _CONTROL.fullmatch(client.probed[0])
    assert sorted(client.probed[1:]) == ["gpt-chat", "gpt-dec"]
    assert client.options == {"timeout": 5.0, "max_retries": 0}
    body = client.bodies[1]
    assert body["input"] == "probe"
    (question,) = body["questions"]
    assert question["type"] == "choice"
    assert question["name"] == "label"
    assert [c["value"] for c in question["choices"]] == ["a", "a"]


@pytest.mark.asyncio
async def test_nothing_is_probed_when_the_listing_is_empty(redis):
    client = _Client([], _answers())

    assert await _provider(client).list_decision_models() == []
    assert client.probed == []


@pytest.mark.asyncio
async def test_nothing_is_probed_when_the_listing_fails(redis):
    client = _Client(["gpt-dec"], _answers(), list_error=_other(openai.InternalServerError, 500))

    assert await _provider(client).list_decision_models() == []
    assert client.probed == []


# --- the outcome table ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_model_refused_as_unknown_is_not_a_decision_model(redis):
    client = _Client(["gpt-chat"], _answers(decision_models=()))

    assert await _provider(client).list_decision_models() == []
    (key,) = redis.values
    assert redis.values[key] == "0"
    assert redis.expiry[key] == 3_600


@pytest.mark.asyncio
async def test_a_model_refused_for_the_duplicate_choice_is_a_decision_model(redis):
    client = _Client(["gpt-dec"], _answers())

    assert await _provider(client).list_decision_models() == ["gpt-dec"]
    (key,) = redis.values
    assert redis.values[key] == "1"
    assert redis.expiry[key] == 86_400


@pytest.mark.asyncio
async def test_a_model_that_answers_is_a_decision_model(redis):
    def answer(model):
        return _unknown() if _CONTROL.fullmatch(model) else {"answers": []}

    client = _Client(["gpt-dec"], answer)

    assert await _provider(client).list_decision_models() == ["gpt-dec"]
    assert list(redis.values.values()) == ["1"]


@pytest.mark.asyncio
async def test_a_rate_limit_ends_the_round_and_remembers_nothing(redis):
    models = [f"gpt-{i:02d}" for i in range(20)]

    def answer(model):
        return _unknown() if _CONTROL.fullmatch(model) else _other(openai.RateLimitError, 429)

    client = _Client(models, answer)

    assert await _provider(client).list_decision_models() == []
    assert len(_non_control(client)) < 20
    assert redis.values == {}


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(lambda: _other(openai.AuthenticationError, 401), id="401"),
        pytest.param(lambda: _other(openai.PermissionDeniedError, 403), id="403"),
        pytest.param(lambda: _other(openai.InternalServerError, 500), id="5xx"),
        pytest.param(lambda: _other(openai.BadRequestError, 400, param="input"), id="other-400"),
        pytest.param(lambda: openai.APITimeoutError(request=httpx.Request("POST", _URL)), id="timeout"),
        pytest.param(lambda: openai.APIConnectionError(request=httpx.Request("POST", _URL)), id="connection"),
    ],
)
@pytest.mark.asyncio
async def test_any_other_answer_leaves_the_model_unknown(redis, error):
    def answer(model):
        return _unknown() if _CONTROL.fullmatch(model) else error()

    client = _Client(["gpt-dec"], answer)

    assert await _provider(client).list_decision_models() == []
    assert redis.values == {}


# --- the control ----------------------------------------------------------


@pytest.mark.parametrize(
    "control_answer",
    [
        pytest.param(_duplicate, id="duplicate-refusal"),
        pytest.param(lambda: {"answers": []}, id="2xx"),
        pytest.param(lambda: _other(openai.AuthenticationError, 401), id="401"),
        pytest.param(lambda: _other(openai.PermissionDeniedError, 403), id="403"),
        pytest.param(lambda: _other(openai.RateLimitError, 429), id="429"),
        pytest.param(lambda: openai.APITimeoutError(request=httpx.Request("POST", _URL)), id="timeout"),
    ],
)
@pytest.mark.asyncio
async def test_a_control_not_refused_as_unknown_stops_the_round(redis, control_answer):
    def answer(model):
        return control_answer() if _CONTROL.fullmatch(model) else _duplicate()

    client = _Client(["gpt-dec", "gpt-chat"], answer)

    assert await _provider(client).list_decision_models() == []
    assert len(client.probed) == 1
    assert redis.values == {}


# --- the memo -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_second_listing_probes_nothing(redis):
    provider = _provider(_Client(["gpt-chat", "gpt-dec"], _answers()))
    await provider.list_decision_models()

    again = _Client(["gpt-chat", "gpt-dec"], _answers())
    provider.client = again

    assert await provider.list_decision_models() == ["gpt-dec"]
    assert again.probed == []


@pytest.mark.asyncio
async def test_a_newly_listed_model_is_probed_alone_with_the_control(redis):
    provider = _provider(_Client(["gpt-chat"], _answers()))
    await provider.list_decision_models()

    again = _Client(["gpt-chat", "gpt-dec"], _answers())
    provider.client = again

    assert await provider.list_decision_models() == ["gpt-dec"]
    assert _non_control(again) == ["gpt-dec"]
    assert len(again.probed) == 2


@pytest.mark.asyncio
async def test_the_memo_is_keyed_by_config_and_key(redis):
    await _provider(_Client(["gpt-dec"], _answers())).list_decision_models()
    (first,) = redis.values
    assert re.fullmatch(r"llm:decision-model:cfg-1:[0-9a-f]{12}:gpt-dec", first)

    other_key = _Client(["gpt-dec"], _answers(), api_key="sk-other")
    await _provider(other_key).list_decision_models()

    assert len(redis.values) == 2
    assert _non_control(other_key) == ["gpt-dec"]


@pytest.mark.asyncio
async def test_an_unknown_outcome_is_not_remembered(redis):
    def answer(model):
        return _unknown() if _CONTROL.fullmatch(model) else _other(openai.InternalServerError, 500)

    await _provider(_Client(["gpt-dec"], answer)).list_decision_models()

    assert redis.values == {}


@pytest.mark.asyncio
async def test_a_redis_failure_still_returns_the_probed_answer(monkeypatch):
    store = _Redis(fail=True)

    async def _client():
        return store

    monkeypatch.setattr("app.llm.decisions.redis_client", _client)

    assert await _provider(_Client(["gpt-dec"], _answers())).list_decision_models() == ["gpt-dec"]


@pytest.mark.asyncio
async def test_an_instance_without_a_config_probes_without_the_memo(redis):
    client = _Client(["gpt-dec"], _answers())

    assert await _provider(client, config_id=None).list_decision_models() == ["gpt-dec"]
    assert redis.values == {}


# --- the bounds -----------------------------------------------------------


@pytest.mark.asyncio
async def test_at_most_eight_probes_run_at_once(redis):
    in_flight = 0
    peak = 0

    async def answer(model):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _unknown()

    client = _Client([f"gpt-{i:02d}" for i in range(20)], answer)
    await _provider(client).list_decision_models()

    assert len(client.probed) == 21
    assert peak <= 8


@pytest.mark.asyncio
async def test_probes_that_never_answer_end_at_the_deadline(redis, monkeypatch):
    monkeypatch.setattr(openai_provider, "DECISION_PROBE_DEADLINE_SECONDS", 0.2)

    cancelled = []

    async def answer(model):
        if _CONTROL.fullmatch(model):
            return _unknown()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(model)
            raise

    assert await _provider(_Client(["gpt-dec"], answer)).list_decision_models() == []
    # The deadline cut the probe that never answers (no wall-clock bound: a
    # loaded runner is slower than any figure, the cancellation is not).
    assert cancelled == ["gpt-dec"]
    assert redis.values == {}


@pytest.mark.asyncio
async def test_a_listing_that_never_answers_ends_at_the_deadline(redis, monkeypatch):
    monkeypatch.setattr(openai_provider, "DECISION_PROBE_DEADLINE_SECONDS", 0.2)
    # Only the round's deadline can end the listing: its own budget is longer.
    monkeypatch.setattr(openai_provider, "DECISION_LISTING_BUDGET_SECONDS", 120.0)
    client = _Client(["gpt-dec"], _answers(), list_delay=60)

    assert await _provider(client).list_decision_models() == []
    # The deadline cancelled the listing (no wall-clock bound, see above).
    assert client.list_cancelled
    assert client.probed == []


# --- probe rounds ------------------------------------------------


@pytest.mark.asyncio
async def test_a_round_already_running_for_the_config_sends_nothing(redis):
    client = _Client(["gpt-dec"], _answers())
    provider = _provider(client)
    marker = "llm:decision-model-round:cfg-1:" + _scope(provider)
    redis.values[marker] = "1"

    assert await provider.list_decision_models() == []
    assert client.probed == []
    assert marker in redis.values


@pytest.mark.asyncio
async def test_the_round_marker_is_released_after_the_round(redis):
    await _provider(_Client(["gpt-dec"], _answers())).list_decision_models()

    assert not [key for key in redis.values if key.startswith("llm:decision-model-round:")]


@pytest.mark.asyncio
async def test_a_round_past_its_marker_leaves_the_next_rounds_marker(redis):
    client = _Client(["gpt-dec"], _answers())
    provider = _provider(client)
    marker = "llm:decision-model-round:cfg-1:" + _scope(provider)
    real_round = provider._decision_round

    async def outlived(*args):
        redis.values[marker] = "next-round"  # this round's marker expired, another round claimed it
        return await real_round(*args)

    provider._decision_round = outlived
    await provider.list_decision_models()

    assert redis.values[marker] == "next-round"


@pytest.mark.asyncio
async def test_a_slow_redis_after_the_round_neither_delays_the_listing_nor_loses_its_answers(redis, monkeypatch):
    released = asyncio.Event()
    real_remember = openai_provider.remember_decision_support

    async def slow_remember(*args):
        await released.wait()
        await real_remember(*args)

    monkeypatch.setattr(openai_provider, "remember_decision_support", slow_remember)
    provider = _provider(_Client(["gpt-dec", "gpt-4o"], _answers()))

    assert await asyncio.wait_for(_UNSETTLED_LISTING(provider), 1) == ["gpt-dec"]
    released.set()
    await asyncio.gather(*list(openai_provider._decision_bookkeeping))

    assert sorted(v for k, v in redis.values.items() if k.startswith("llm:decision-model:")) == ["0", "1"]


@pytest.mark.asyncio
async def test_the_memo_is_keyed_by_the_endpoint_too(redis):
    await _provider(_Client(["gpt-dec"], _answers())).list_decision_models()
    other = _Client(["gpt-dec"], _answers(), base_url="https://proxy.example.com/v1/")

    await _provider(other).list_decision_models()

    assert _non_control(other) == ["gpt-dec"]
    assert len([key for key in redis.values if key.startswith("llm:decision-model:")]) == 2


@pytest.mark.asyncio
async def test_a_cancelled_listing_leaves_no_probe_running(redis):
    started = asyncio.Event()
    running = 0

    async def answer(model):
        nonlocal running
        if _CONTROL.fullmatch(model):
            return _unknown()
        running += 1
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            running -= 1

    task = asyncio.create_task(_provider(_Client([f"gpt-{i}" for i in range(4)], answer)).list_decision_models())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert running == 0
    assert not [key for key in redis.values if key.startswith("llm:decision-model-round:")]


@pytest.mark.asyncio
async def test_a_control_that_does_not_answer_ends_within_its_own_budget(redis, monkeypatch):
    monkeypatch.setattr(openai_provider, "DECISION_CONTROL_BUDGET_SECONDS", 0.2)
    # Only the control's budget can end the round: the deadline is longer.
    monkeypatch.setattr(openai_provider, "DECISION_PROBE_DEADLINE_SECONDS", 120.0)
    cancelled = []

    async def answer(model):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.append(model)
            raise

    assert await _provider(_Client(["gpt-dec"], answer)).list_decision_models() == []
    # The control was cut by its budget, and nothing was probed after it.
    assert len(cancelled) == 1 and _CONTROL.fullmatch(cancelled[0])
    assert redis.values == {}


def _scope(provider):
    from app.core.log_once import fingerprint

    return fingerprint(provider.client.api_key, str(provider.client.base_url))


# --- whether the listing is whole (the Classify node's sweep) --------------


async def _listing(provider):
    try:
        return await provider.decision_listing()
    finally:
        await asyncio.gather(*list(openai_provider._decision_bookkeeping))


@pytest.mark.asyncio
async def test_a_finished_round_gives_the_whole_listing(redis):
    assert await _listing(_provider(_Client(["gpt-dec", "gpt-chat"], _answers()))) == (["gpt-dec"], True)


@pytest.mark.asyncio
async def test_a_remembered_listing_is_whole(redis):
    provider = _provider(_Client(["gpt-dec", "gpt-chat"], _answers()))
    await _listing(provider)

    assert await _listing(provider) == (["gpt-dec"], True)


@pytest.mark.asyncio
async def test_a_round_held_by_another_caller_gives_a_partial_listing(redis):
    provider = _provider(_Client(["gpt-dec"], _answers()))
    redis.values["llm:decision-model-round:cfg-1:" + _scope(provider)] = "other"

    assert await _listing(provider) == ([], False)


@pytest.mark.asyncio
async def test_a_failed_model_listing_gives_a_partial_listing(redis):
    client = _Client([], _answers(), list_error=openai.APIConnectionError(request=httpx.Request("GET", _URL)))

    assert await _listing(_provider(client)) == ([], False)


@pytest.mark.asyncio
async def test_a_model_left_unchecked_gives_a_partial_listing(redis):
    def answer(model):
        return _other(openai.InternalServerError, 500) if model == "gpt-flaky" else _answers()(model)

    assert await _listing(_provider(_Client(["gpt-dec", "gpt-flaky"], answer))) == (["gpt-dec"], False)


@pytest.mark.parametrize(
    ("control_answer", "whole"),
    [
        pytest.param(lambda: _other(openai.AuthenticationError, 401), True, id="401"),
        pytest.param(lambda: _other(openai.PermissionDeniedError, 403), True, id="403"),
        pytest.param(lambda: _other(openai.NotFoundError, 404), True, id="no-route"),
        pytest.param(lambda: _other(openai.RateLimitError, 429), False, id="429"),
        pytest.param(lambda: _other(openai.InternalServerError, 500), False, id="5xx"),
    ],
)
@pytest.mark.asyncio
async def test_a_control_refused_outright_settles_the_listing(redis, control_answer, whole):
    # A key that reaches no Decisions API serves no decision model.
    def answer(model):
        return control_answer() if _CONTROL.fullmatch(model) else _duplicate()

    assert await _listing(_provider(_Client(["gpt-dec"], answer))) == ([], whole)
    assert redis.values == {}
