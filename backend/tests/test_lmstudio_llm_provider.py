"""The platform LM Studio LLM provider: every request on its integration's route, or refused.

``resolve_target`` (what the integration says: server URL, token, relay group)
is replaced by a stand-in whose route's transport is an ``httpx.MockTransport``
- or, for an integration with no relay group, whose direct client is one - so
the provider's real OpenAI client, its real transport and the rewriting run
as in production and nothing leaves the process. The resolution itself, on
real rows, is in ``test_lmstudio_console_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import openai
import pytest
from fastapi import HTTPException

from app.integrations import lifecycle
from app.integrations.providers import lmstudio as client
from app.llm.base import ChatMessage
from app.llm.lmstudio_provider import LMStudioProvider
from app.llm.openai_compat import is_retryable_openai
from app.llm.router import PROVIDER_MAP, provider_instance
from app.services import lmstudio_llm_provider as llm_link

_CONFIG_ID = uuid.uuid4()
_INTEGRATION_ID = uuid.uuid4()


@dataclass
class _Route:
    handler: object
    seen: list[httpx.Request] = field(default_factory=list)

    def transport(self) -> httpx.AsyncBaseTransport:
        async def _record(request: httpx.Request) -> httpx.Response:
            await request.aread()
            self.seen.append(request)
            return self.handler(request)

        return httpx.MockTransport(_record)


def _target(route: _Route | None, *, base_url: str = "http://gpu-host:1234/lmstudio", token: str | None = "lm-token"):  # noqa: S107 - a test token
    return llm_link.Target(
        integration_id=_INTEGRATION_ID,
        route=route,  # type: ignore[arg-type]
        connection=client.Connection(base_url=base_url, token=token),
    )


@pytest.fixture(autouse=True)
def _no_cached_targets():
    llm_link.forget_target()
    yield
    llm_link.forget_target()


@pytest.fixture
def served(monkeypatch):
    """The provider of config ``_CONFIG_ID``, its integration resolving to a recording route."""
    resolved: list = []

    def _install(handler, **target):
        route = _Route(handler)

        async def _resolve(config_id):
            resolved.append(config_id)
            return _target(route, **target)

        monkeypatch.setattr(llm_link, "resolve_target", _resolve)
        return route

    _install.resolved = resolved  # type: ignore[attr-defined]
    return _install


@pytest.fixture
def direct(monkeypatch):
    """The provider of config ``_CONFIG_ID``, its integration routed nowhere: direct clients answer."""
    real_egress_client = llm_link.egress_client

    def _install(handler, **target):
        sent: list[httpx.Request] = []
        clients: list[httpx.AsyncClient] = []

        async def _record(request: httpx.Request) -> httpx.Response:
            await request.aread()
            sent.append(request)
            return handler(request)

        def _egress_client(**kwargs):
            if "transport" in kwargs:
                # The provider's own client, on the placeholder host.
                return real_egress_client(**kwargs)
            assert kwargs.pop("route") is None
            made = httpx.AsyncClient(transport=httpx.MockTransport(_record), **kwargs)
            clients.append(made)
            return made

        async def _resolve(config_id):
            return _target(None, **target)

        monkeypatch.setattr(llm_link, "egress_client", _egress_client)
        monkeypatch.setattr(llm_link, "resolve_target", _resolve)
        return sent, clients

    return _install


def _completion(text: str = "Hello") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": "openai/gpt-oss-20b",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        },
    )


def _provider() -> LMStudioProvider:
    provider = provider_instance(PROVIDER_MAP["lmstudio"], "integration", _CONFIG_ID)
    assert isinstance(provider, LMStudioProvider)
    return provider


# ---------------------------------------------------------------------------
# Where requests go
# ---------------------------------------------------------------------------


async def test_a_chat_goes_to_the_integration_s_server_through_its_relay(served):
    route = served(lambda request: _completion("Bonjour"))
    response = await _provider().chat([ChatMessage(role="user", content="Hi")], model="openai/gpt-oss-20b")

    assert response.content == "Bonjour"
    assert served.resolved == [_CONFIG_ID]
    [request] = route.seen
    # The placeholder host is replaced by the server's root, prefix included.
    assert str(request.url) == "http://gpu-host:1234/lmstudio/v1/chat/completions"
    # The integration's token, never the SDK's placeholder key.
    assert request.headers.get_list("authorization") == ["Bearer lm-token"]
    assert "lmstudio.integration.invalid" not in request.headers.get("host", "")
    assert json.loads(request.content)["model"] == "openai/gpt-oss-20b"


async def test_without_a_relay_group_a_chat_goes_to_the_server_directly(direct):
    sent, clients = direct(lambda request: _completion("Bonjour"))
    response = await _provider().chat([ChatMessage(role="user", content="Hi")], model="openai/gpt-oss-20b")

    assert response.content == "Bonjour"
    [request] = sent
    assert str(request.url) == "http://gpu-host:1234/lmstudio/v1/chat/completions"
    assert request.headers.get_list("authorization") == ["Bearer lm-token"]
    assert json.loads(request.content)["model"] == "openai/gpt-oss-20b"
    # One client per request, closed with its answer.
    assert [c.is_closed for c in clients] == [True]


async def test_without_a_relay_group_a_stream_is_carried_directly(direct):
    def _sse(request):
        body = "".join(
            f"data: {json.dumps({'id': 'c', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'm', **c})}\n\n"
            for c in (
                {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Bon"}, "finish_reason": None}]},
                {"choices": [{"index": 0, "delta": {"content": "jour"}, "finish_reason": "stop"}]},
            )
        ) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    sent, clients = direct(_sse)
    text = "".join([part async for part in _provider().chat_stream([ChatMessage(role="user", content="Hi")], model="m")])
    assert text == "Bonjour"
    assert [r.url.path for r in sent] == ["/lmstudio/v1/chat/completions"]
    assert all(c.is_closed for c in clients)


async def test_a_server_without_a_token_gets_no_authorization(served):
    route = served(lambda request: _completion(), token=None)
    await _provider().chat([ChatMessage(role="user", content="Hi")], model="m")
    assert "authorization" not in route.seen[0].headers


async def test_a_stream_is_carried_chunk_by_chunk(served):
    def _sse(request):
        chunks = [
            {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Bon"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {"content": "jour"}, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
        body = "".join(
            f"data: {json.dumps({'id': 'c', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'm', **c})}\n\n"
            for c in chunks
        ) + "data: [DONE]\n\n"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    route = served(_sse)
    text = "".join([part async for part in _provider().chat_stream([ChatMessage(role="user", content="Hi")], model="m")])
    assert text == "Bonjour"
    assert route.seen[0].url.path == "/lmstudio/v1/chat/completions"
    assert json.loads(route.seen[0].content)["stream"] is True


async def test_the_listings_read_the_native_api_types(served):
    listing = {
        "models": [
            {"type": "llm", "key": "qwen/qwen3-4b", "display_name": "Qwen3 4B", "loaded_instances": []},
            {"type": "embedding", "key": "text-embedding-nomic-embed-text-v1.5", "display_name": "Nomic", "loaded_instances": []},
            {"type": "decision", "key": "typesafe/tev1-4b", "display_name": "Tev1", "loaded_instances": []},
            {"type": "llm", "key": "openai/gpt-oss-20b", "display_name": "GPT-OSS", "loaded_instances": []},
        ]
    }
    route = served(lambda request: httpx.Response(200, stream=httpx.ByteStream(json.dumps(listing).encode())))
    provider = _provider()
    assert await provider.list_models() == ["openai/gpt-oss-20b", "qwen/qwen3-4b"]
    assert await provider.list_embedding_models() == ["text-embedding-nomic-embed-text-v1.5"]
    assert await provider.list_decision_models() == ["typesafe/tev1-4b"]
    assert {str(r.url) for r in route.seen} == {"http://gpu-host:1234/lmstudio/api/v1/models"}


# ---------------------------------------------------------------------------
# Refusals: a 424 the SDK and the platform never retry
# ---------------------------------------------------------------------------


async def test_a_provider_with_no_config_refuses_without_reading_anything(monkeypatch):
    def _no_database():
        raise AssertionError("a provider with no config reads nothing")

    monkeypatch.setattr("app.core.database.async_session", _no_database)
    provider = provider_instance(PROVIDER_MAP["lmstudio"], "integration", None)
    with pytest.raises(openai.APIStatusError) as refused:
        await provider.chat([ChatMessage(role="user", content="Hi")], model="m")
    assert refused.value.status_code == llm_link.REFUSED_STATUS
    assert "not fed by an LM Studio integration" in str(refused.value)
    assert not is_retryable_openai(refused.value)


@pytest.mark.parametrize(
    "reason",
    [client.PUBLIC_MESSAGES["no_base_url"], "Make this integration company-managed first: ..."],
)
async def test_an_integration_that_cannot_carry_the_request_refuses_it_once(monkeypatch, reason):
    calls: list = []

    async def _refuse(config_id):
        calls.append(config_id)
        raise llm_link.TargetRefusedError(reason)

    monkeypatch.setattr(llm_link, "resolve_target", _refuse)
    with pytest.raises(openai.APIStatusError) as refused:
        await _provider().chat([ChatMessage(role="user", content="Hi")], model="m")
    assert refused.value.status_code == 424
    assert refused.value.body == {"message": reason, "type": "lmstudio_unavailable"}
    # One attempt: neither the SDK nor ``retry_llm_call`` retries a 424.
    assert calls == [_CONFIG_ID]


async def test_a_refused_listing_says_why(monkeypatch):
    async def _refuse(config_id):
        raise llm_link.TargetRefusedError(client.PUBLIC_MESSAGES["no_base_url"])

    monkeypatch.setattr(llm_link, "resolve_target", _refuse)
    with pytest.raises(httpx.HTTPStatusError) as refused:
        await _provider().list_models()
    assert str(refused.value) == client.PUBLIC_MESSAGES["no_base_url"]


async def test_the_transport_answers_only_its_own_host(served):
    route = served(lambda request: _completion())
    transport = llm_link.IntegrationTransport(_CONFIG_ID)
    response = await transport.handle_async_request(httpx.Request("GET", "http://169.254.169.254/latest/meta-data"))
    assert response.status_code == 424
    assert route.seen == []
    assert served.resolved == []


async def test_an_environment_proxy_never_takes_the_request(served, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp:3128")
    route = served(lambda request: _completion())
    await _provider().chat([ChatMessage(role="user", content="Hi")], model="m")
    assert [r.url.host for r in route.seen] == ["gpu-host"]


# ---------------------------------------------------------------------------
# What a process remembers of an integration
# ---------------------------------------------------------------------------


async def test_a_target_is_reused_then_read_again_after_an_edit(monkeypatch):
    reads: list = []

    async def _read(db, config_id):
        reads.append(config_id)
        return _target(_Route(lambda r: _completion()))

    class _Session:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(llm_link, "_read_target", _read)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())

    await llm_link.resolve_target(_CONFIG_ID)
    await llm_link.resolve_target(_CONFIG_ID)
    assert reads == [_CONFIG_ID]
    # An edit of the integration, or an unlink of the config, drops it.
    llm_link.forget_target(_INTEGRATION_ID)
    await llm_link.resolve_target(_CONFIG_ID)
    llm_link.forget_target(config_id=_CONFIG_ID)
    await llm_link.resolve_target(_CONFIG_ID)
    assert reads == [_CONFIG_ID, _CONFIG_ID, _CONFIG_ID]
    # And it expires.
    monkeypatch.setattr(llm_link, "TARGET_TTL_SECONDS", 0.0)
    await llm_link.resolve_target(_CONFIG_ID)
    assert len(reads) == 4


async def test_a_refusal_is_not_remembered(monkeypatch):
    answers = iter([llm_link.TargetRefusedError("no server URL"), _target(_Route(lambda r: _completion()))])

    async def _read(db, config_id):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    class _Session:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(llm_link, "_read_target", _read)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())
    with pytest.raises(llm_link.TargetRefusedError):
        await llm_link.resolve_target(_CONFIG_ID)
    # Fixed at once: the next request reads the integration again.
    assert (await llm_link.resolve_target(_CONFIG_ID)).integration_id == _INTEGRATION_ID


# ---------------------------------------------------------------------------
# Who may feed it, and where it is created
# ---------------------------------------------------------------------------


def _integration(**overrides) -> SimpleNamespace:
    from app.core.security import encrypt_value

    values = {
        "id": uuid.uuid4(),
        "provider": "lmstudio",
        "is_company_managed": True,
        "relay_group_id": uuid.uuid4(),
        "api_key_enc": None,
        "extra_config_enc": encrypt_value(json.dumps({"base_url": "http://gpu-host:1234"})),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_only_a_company_managed_integration_with_a_server_url_feeds_the_platform():
    from app.core.security import encrypt_value

    assert llm_link.link_refusal(_integration()) is None
    assert llm_link.link_refusal(_integration(is_company_managed=False)).startswith("Make this integration company-managed")
    # Routed or not: without a relay group, its requests go to the server directly.
    assert llm_link.link_refusal(_integration(relay_group_id=None)) is None
    assert llm_link.link_refusal(_integration(extra_config_enc=encrypt_value("{}"))) == client.PUBLIC_MESSAGES["no_base_url"]
    assert llm_link.link_refusal(
        _integration(extra_config_enc=encrypt_value(json.dumps({"base_url": "ftp://x"})))
    ) == client.PUBLIC_MESSAGES["invalid_base_url"]
    assert llm_link.link_refusal(_integration(provider="openrouter")) == (
        "Only LM Studio integrations can feed the LM Studio LLM provider."
    )


def test_the_config_holds_no_credential():
    assert llm_link.FEED.stored_credential(_integration(api_key_enc="whatever")) == llm_link.STORED_CREDENTIAL


def test_settings_never_creates_it_by_hand():
    from app.api.v1.settings import _ensure_new_provider_allowed

    with pytest.raises(HTTPException) as refused:
        _ensure_new_provider_allowed("lmstudio")
    assert refused.value.status_code == 400
    _ensure_new_provider_allowed("lmstudio", from_integration=True)
    _ensure_new_provider_allowed("custom_openai")


def test_the_integrations_api_registers_its_hooks():
    import app.api.v1.integrations  # noqa: F401

    assert lifecycle._ON_UPDATE["lmstudio"] is llm_link._on_integration_update
    assert lifecycle._ON_DISCONNECT["lmstudio"] is llm_link._on_integration_removed


@pytest.mark.parametrize(
    ("sent", "base_url", "expected"),
    [
        ("http://lmstudio.integration.invalid/v1/chat/completions", "http://gpu-host:1234", "http://gpu-host:1234/v1/chat/completions"),
        ("http://lmstudio.integration.invalid/v1/models?x=1&y=%2F", "https://proxy.corp:8443/lm", "https://proxy.corp:8443/lm/v1/models?x=1&y=%2F"),
        # An escaped character of the path is carried as sent, never decoded.
        ("http://lmstudio.integration.invalid/v1/models/a%2Fb", "http://gpu-host:1234", "http://gpu-host:1234/v1/models/a%2Fb"),
    ],
)
def test_the_rewrite_keeps_the_path_and_the_query_as_sent(sent, base_url, expected):
    assert str(llm_link._rewrite(httpx.URL(sent), base_url)) == expected
