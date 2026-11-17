"""Decision models through the Filigran AI Gateway (spec 028 User Story 4).

Both ways of configuring the gateway list what its ``/v1/models`` types as
``decision`` and ask OpenAI's Decisions route: ``contracts/gateway-decisions.md``.
"""

import json

import httpx
import pytest

from app.llm.custom_openai_provider import CustomOpenAIProvider
from app.llm.decisions import DECISIONS_OPENAI
from app.llm.gateway_provider import FiligranGatewayProvider, StaticTokenAuth

GATEWAY = "https://gateway.example.com"
CHOICES = {"investigate": "Worth an analyst", "noise": "Nothing to do"}
ANSWER = {
    "answers": [
        {
            "type": "choice",
            "name": "label",
            "choice": "investigate",
            "probabilities": [
                {"value": "investigate", "probability": 0.9},
                {"value": "noise", "probability": 0.1},
            ],
            "confidence": 0.85,
        }
    ],
}
LISTING = [
    {"id": "gpt-4o", "object": "model", "owned_by": "openai", "type": "chat"},
    {"id": "tev1:0.8b", "object": "model", "owned_by": "gateway", "type": "decision"},
    {"id": "openrouter/clef", "object": "model", "owned_by": "openrouter", "type": "decision"},
    {"id": "text-embed-1", "object": "model", "owned_by": "openai", "type": "embedding"},
]


class _Gateway:
    """The gateway's two routes XTM One calls here, recording what reaches them."""

    def __init__(self, listing=LISTING, *, listing_status=200):
        self.listing = listing
        self.listing_status = listing_status
        self.decisions: list[httpx.Request] = []
        self.listings = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/models":
            self.listings += 1
            if self.listing_status != 200:
                return httpx.Response(self.listing_status, json={"error": {"message": "down"}})
            return httpx.Response(200, json={"object": "list", "data": self.listing})
        if request.method == "POST" and request.url.path == "/v1/decisions":
            self.decisions.append(request)
            return httpx.Response(200, json=ANSWER)
        return httpx.Response(404, json={"error": {"message": f"no route {request.url.path}"}})


def _gateway_provider(gateway: _Gateway, *, pin: str = "") -> FiligranGatewayProvider:
    return FiligranGatewayProvider(
        api_key=f"{GATEWAY}||{pin}||static-token",
        http_client=httpx.AsyncClient(
            auth=StaticTokenAuth("static-token", pin or None),
            transport=httpx.MockTransport(gateway),
        ),
    )


def _custom_provider(gateway: _Gateway) -> CustomOpenAIProvider:
    return CustomOpenAIProvider(
        api_key=f"static-token||{GATEWAY}/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(gateway)),
    )


def _entries(entries=None, error=None):
    """``CustomOpenAIProvider._list_model_entries`` answering *entries*, or raising *error*."""

    async def listed(_self):
        if error is not None:
            raise error
        return entries

    return listed


async def _decide(provider, model="tev1:0.8b"):
    return await provider.decide_choice(model=model, state="alert", instructions="Worth an analyst?", choices=CHOICES)


# --- Filigran AI Gateway provider ------------------------------------------


@pytest.mark.asyncio
async def test_the_gateway_lists_the_models_it_types_as_decision():
    assert await _gateway_provider(_Gateway()).list_decision_models() == ["openrouter/clef", "tev1:0.8b"]


@pytest.mark.asyncio
async def test_a_model_typed_chat_and_decision_is_listed_by_both_pickers():
    both = {"id": "gpt-6-luna", "object": "model", "owned_by": "openai", "type": "chat", "types": ["chat", "decision"]}
    provider = _gateway_provider(_Gateway([*LISTING, both]))

    assert await provider.list_decision_models() == ["gpt-6-luna", "openrouter/clef", "tev1:0.8b"]
    assert await provider.list_models() == ["gpt-4o", "gpt-6-luna"]


@pytest.mark.asyncio
async def test_a_gateway_without_model_types_lists_no_decision_model():
    untyped = [{k: v for k, v in entry.items() if k != "type"} for entry in LISTING]

    assert await _gateway_provider(_Gateway(untyped)).list_decision_models() == []


@pytest.mark.asyncio
async def test_a_failing_gateway_listing_raises():
    with pytest.raises(RuntimeError):
        await _gateway_provider(_Gateway(listing_status=500)).list_decision_models()


@pytest.mark.asyncio
async def test_a_gateway_decision_names_the_listed_upstream():
    gateway = _Gateway()

    decision = await _decide(_gateway_provider(gateway), model="openrouter/clef")

    assert decision.choice == "investigate"
    (request,) = gateway.decisions
    assert request.headers["X-Target-Provider"] == "openrouter"
    body = json.loads(request.content)
    assert body["model"] == "openrouter/clef"
    assert body["input"] == "alert"
    assert body["questions"][0]["choices"][0] == {"value": "investigate", "description": "Worth an analyst"}


@pytest.mark.asyncio
async def test_a_decision_id_the_gateway_owns_names_no_upstream():
    gateway = _Gateway()

    await _decide(_gateway_provider(gateway))

    (request,) = gateway.decisions
    assert "X-Target-Provider" not in request.headers


@pytest.mark.asyncio
async def test_a_pinned_credential_names_its_upstream_on_every_decision():
    gateway = _Gateway()

    await _decide(_gateway_provider(gateway, pin="ollama"))
    await _decide(_gateway_provider(gateway, pin="ollama"), model="openrouter/clef")

    assert [r.headers["X-Target-Provider"] for r in gateway.decisions] == ["ollama", "ollama"]


@pytest.mark.asyncio
async def test_a_model_the_gateway_does_not_list_is_not_looked_up_again_at_once():
    gateway = _Gateway()
    provider = _gateway_provider(gateway)

    await _decide(provider, model="typed-decision-id")
    await _decide(provider, model="typed-decision-id")

    assert gateway.listings == 1
    assert all("X-Target-Provider" not in r.headers for r in gateway.decisions)


@pytest.mark.asyncio
async def test_a_model_the_operator_adds_later_names_its_upstream(monkeypatch):
    from app.llm import gateway_provider

    gateway = _Gateway()
    provider = _gateway_provider(gateway)
    await _decide(provider, model="added-later")
    gateway.listing = [*LISTING, {"id": "added-later", "object": "model", "owned_by": "ollama", "type": "decision"}]
    now = gateway_provider.time.monotonic()
    monkeypatch.setattr(
        gateway_provider.time, "monotonic", lambda: now + gateway_provider._OWNER_MISS_TTL_SECONDS + 1
    )

    await _decide(provider, model="added-later")

    assert gateway.listings == 2
    assert gateway.decisions[-1].headers["X-Target-Provider"] == "ollama"


@pytest.mark.asyncio
async def test_a_failing_gateway_listing_leaves_the_decision_to_the_gateway():
    gateway = _Gateway(listing_status=500)

    decision = await _decide(_gateway_provider(gateway))

    assert decision.choice == "investigate"
    (request,) = gateway.decisions
    assert "X-Target-Provider" not in request.headers


# --- Custom OpenAI-compatible provider pointed at the gateway ---------------


@pytest.mark.asyncio
async def test_a_custom_endpoint_lists_the_models_it_types_as_decision(monkeypatch):
    monkeypatch.setattr(CustomOpenAIProvider, "_list_model_entries", _entries(LISTING))

    assert await _custom_provider(_Gateway()).list_decision_models() == ["openrouter/clef", "tev1:0.8b"]


@pytest.mark.asyncio
async def test_a_custom_endpoint_lists_a_model_typed_chat_and_decision(monkeypatch):
    both = {"id": "gpt-6-luna", "object": "model", "owned_by": "openai", "type": "chat", "types": ["chat", "decision"]}
    monkeypatch.setattr(CustomOpenAIProvider, "_list_model_entries", _entries([*LISTING, both]))

    assert await _custom_provider(_Gateway()).list_decision_models() == [
        "gpt-6-luna",
        "openrouter/clef",
        "tev1:0.8b",
    ]


@pytest.mark.asyncio
async def test_a_custom_endpoint_ignores_a_malformed_types_list(monkeypatch):
    odd = [
        {"id": "a", "type": "decision", "types": []},
        {"id": "b", "type": "decision", "types": [1]},
        {"id": "c", "type": "chat", "types": ["rerank"]},
        {"id": "d", "type": "decision", "types": "chat"},
        {"id": "e", "type": "decision", "types": {"chat": True}},
        {"id": "f", "type": "decision", "types": [""]},
    ]
    monkeypatch.setattr(CustomOpenAIProvider, "_list_model_entries", _entries(odd))

    assert await _custom_provider(_Gateway()).list_decision_models() == ["a", "b", "d", "e", "f"]


@pytest.mark.asyncio
async def test_a_custom_endpoint_without_model_types_lists_no_decision_model(monkeypatch):
    monkeypatch.setattr(CustomOpenAIProvider, "_list_model_entries", _entries([{"id": "llama-3", "object": "model"}]))

    assert await _custom_provider(_Gateway()).list_decision_models() == []


@pytest.mark.asyncio
async def test_a_failing_custom_listing_raises(monkeypatch):
    request = httpx.Request("GET", f"{GATEWAY}/v1/models")
    error = httpx.HTTPStatusError("down", request=request, response=httpx.Response(500, request=request))
    monkeypatch.setattr(CustomOpenAIProvider, "_list_model_entries", _entries(error=error))

    with pytest.raises(httpx.HTTPStatusError):
        await _custom_provider(_Gateway()).list_decision_models()


@pytest.mark.asyncio
async def test_a_custom_endpoint_decision_goes_to_the_decisions_route_naming_no_upstream():
    gateway = _Gateway()

    decision = await _decide(_custom_provider(gateway))

    assert decision.choice == "investigate"
    (request,) = gateway.decisions
    assert "X-Target-Provider" not in request.headers
    assert json.loads(request.content)["model"] == "tev1:0.8b"


def test_both_ways_of_configuring_the_gateway_speak_openai_decisions():
    assert FiligranGatewayProvider.decisions_api == DECISIONS_OPENAI
    assert CustomOpenAIProvider.decisions_api == DECISIONS_OPENAI
