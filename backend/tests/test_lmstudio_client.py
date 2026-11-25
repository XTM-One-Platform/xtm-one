"""The LM Studio client: only through a relay route, bounded and typed, fixed sentences on failure.

The relay route is a stand-in whose transport is an ``httpx.MockTransport``: the
code under test goes through ``egress_client`` and the route's ``transport()``
exactly as with a real relay group, and nothing leaves the process.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest

from app.core.egress import egress_route
from app.integrations.providers import lmstudio as client

_CONNECTION = client.Connection(base_url="http://gpu-host:1234", token="lm-token")


@dataclass
class _Route:
    """A relay route whose transport answers with ``handler``, recording each request."""

    handler: object
    seen: list[httpx.Request] = field(default_factory=list)

    def transport(self) -> httpx.AsyncBaseTransport:
        def _record(request: httpx.Request) -> httpx.Response:
            self.seen.append(request)
            return self.handler(request)

        return httpx.MockTransport(_record)


def _json(payload, status: int = 200, **headers) -> httpx.Response:
    # A stream, as a server's answer is: the client reads it raw and bounded.
    return httpx.Response(
        status, stream=httpx.ByteStream(json.dumps(payload).encode()), headers={"content-type": "application/json", **headers}
    )


_LISTING = {
    "models": [
        {
            "type": "embedding",
            "publisher": "nomic-ai",
            "key": "text-embedding-nomic-embed-text-v1.5",
            "display_name": "Nomic Embed Text v1.5",
            "quantization": {"name": "Q4_K_M", "bits_per_weight": 4},
            "size_bytes": 84106624,
            "params_string": None,
            "loaded_instances": [],
            "max_context_length": 2048,
            "format": "gguf",
        },
        {
            "type": "llm",
            "publisher": "openai",
            "key": "openai/gpt-oss-20b",
            "display_name": "GPT-OSS 20B",
            "architecture": "gpt-oss",
            "quantization": {"name": "MXFP4", "bits_per_weight": 4.25},
            "size_bytes": 12109566560,
            "params_string": "20B",
            "loaded_instances": [
                {"id": "openai/gpt-oss-20b", "config": {"context_length": 8192, "flash_attention": True, "parallel": 4}}
            ],
            "max_context_length": 131072,
            "format": "gguf",
            "capabilities": {
                "vision": False,
                "trained_for_tool_use": True,
                "reasoning": {"allowed_options": ["low", "medium", "high", "turbo"], "default": "medium"},
            },
        },
        # Not a model: dropped, never echoed.
        {"type": "llm", "key": "../../etc/passwd"},
        "garbage",
    ]
}


# ---------------------------------------------------------------------------
# The server URL
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://gpu-host:1234", "http://gpu-host:1234"),
        ("  http://gpu-host:1234/  ", "http://gpu-host:1234"),
        ("http://gpu-host:1234/v1", "http://gpu-host:1234"),
        ("http://gpu-host:1234/api/v1/", "http://gpu-host:1234"),
        ("http://gpu-host:1234/api/v0", "http://gpu-host:1234"),
        ("https://proxy.corp/lmstudio/v1", "https://proxy.corp/lmstudio"),
        ("http://10.0.0.5:1234", "http://10.0.0.5:1234"),
        ("http://[fd00::5]:1234", "http://[fd00::5]:1234"),
    ],
)
def test_a_server_url_is_stored_as_its_root(raw, expected):
    assert client.normalize_base_url(raw) == expected


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (None, "no_base_url"),
        ("", "no_base_url"),
        ("   ", "no_base_url"),
        (42, "no_base_url"),
        ("gpu-host:1234", "invalid_base_url"),
        ("ftp://gpu-host:1234", "invalid_base_url"),
        ("file:///etc/passwd", "invalid_base_url"),
        ("http://user:pass@gpu-host:1234", "invalid_base_url"),
        ("http://gpu-host:1234/?x=1", "invalid_base_url"),
        ("http://gpu-host:1234/#frag", "invalid_base_url"),
        ("http://gpu host:1234", "invalid_base_url"),
        ("http://gpu-host:1234/a/../b", "invalid_base_url"),
        ("http://gpu-host:notaport", "invalid_base_url"),
        ("http://gpu-host:/", "invalid_base_url"),
        ("http://", "invalid_base_url"),
    ],
)
def test_a_server_url_that_is_not_an_http_root_is_refused(raw, kind):
    with pytest.raises(client.LMStudioError) as refused:
        client.normalize_base_url(raw)
    assert refused.value.kind == kind
    assert refused.value.public_message == client.PUBLIC_MESSAGES[kind]


@pytest.mark.parametrize(("token", "expected"), [(None, None), ("", None), ("none", None), ("Dummy", None), (" t0k ", "t0k")])
def test_the_token_is_optional(token, expected):
    assert client.make_connection("http://h:1234", token).token == expected


def test_a_token_with_a_line_break_is_refused():
    with pytest.raises(client.LMStudioError) as refused:
        client.make_connection("http://h:1234", "abc\r\nX-Injected: 1")
    assert refused.value.kind == "auth"


# ---------------------------------------------------------------------------
# Values a caller sends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("openai/gpt-oss-20b", True),
        ("qwen3-4b-2507", True),
        ("openai/gpt-oss-20b:2", True),
        ("text-embedding-nomic-embed-text-v1.5", True),
        ("", False),
        ("../secret", False),
        ("a/../b", False),
        ("model with space", False),
        ("model\nnext", False),
        ("http://evil.example/x", False),
        ("x" * 301, False),
        (None, False),
        (7, False),
    ],
)
def test_model_keys(value, valid):
    assert client.is_valid_model_key(value) is valid


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("openai/gpt-oss-20b", True),
        ("https://huggingface.co/lmstudio-community/Qwen3-4B-GGUF", True),
        ("https://hf.co/lmstudio-community/Qwen3-4B-GGUF", True),
        ("http://huggingface.co/x/y", False),
        ("https://evil.example/x/y", False),
        ("https://huggingface.co.evil.example/x", False),
        ("https://user@huggingface.co/x/y", False),
        ("https://huggingface.co:8443/x/y", False),
        ("https://huggingface.co/x y", False),
        ("https://huggingface.co/x#y", False),
        ("x" * 501, False),
    ],
)
def test_download_sources(value, valid):
    assert client.is_valid_download_source(value) is valid


@pytest.mark.parametrize(("value", "valid"), [("job_493c7c9ded", True), ("../models", False), ("job/1", False), ("", False)])
def test_job_ids(value, valid):
    assert client.is_valid_job_id(value) is valid


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


async def test_without_a_relay_route_nothing_is_sent():
    sent: list = []
    transport = httpx.MockTransport(lambda r: sent.append(r) or _json(_LISTING))
    # Even a client whose transport would answer: the module refuses first.
    with pytest.raises(client.LMStudioError) as refused:
        async with httpx.AsyncClient(transport=transport):
            await client.list_models(_CONNECTION)
    assert refused.value.kind == "not_routed"
    assert sent == []


async def test_the_listing_goes_through_the_route_with_the_token_and_is_typed():
    route = _Route(lambda request: _json(_LISTING))
    with egress_route(route):
        models = await client.list_models(_CONNECTION)

    [request] = route.seen
    assert (request.method, str(request.url)) == ("GET", "http://gpu-host:1234/api/v1/models")
    assert request.headers["authorization"] == "Bearer lm-token"
    assert request.headers["accept-encoding"] == "identity"
    assert [m["key"] for m in models] == ["openai/gpt-oss-20b", "text-embedding-nomic-embed-text-v1.5"]
    llm, embedding = models
    assert llm == {
        "key": "openai/gpt-oss-20b",
        "type": "llm",
        "display_name": "GPT-OSS 20B",
        "publisher": "openai",
        "architecture": "gpt-oss",
        "format": "gguf",
        "quantization": "MXFP4",
        "bits_per_weight": 4.25,
        "size_bytes": 12109566560,
        "params": "20B",
        "max_context_length": 131072,
        "vision": False,
        "tool_use": True,
        # An option LM Studio may add later is not passed on.
        "reasoning_options": ["low", "medium", "high"],
        "description": None,
        "loaded_instances": [{"id": "openai/gpt-oss-20b", "context_length": 8192, "flash_attention": True, "parallel": 4}],
    }
    assert (embedding["type"], embedding["loaded_instances"], embedding["tool_use"]) == ("embedding", [], False)


async def test_no_token_sends_no_authorization():
    route = _Route(lambda request: _json({"models": []}))
    with egress_route(route):
        await client.list_models(client.Connection(base_url="http://h:1234"))
    assert "authorization" not in route.seen[0].headers


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (_json({"error": "Unauthorized"}, 401), "auth"),
        (_json({"error": "Forbidden"}, 403), "auth"),
        # Only an LM Studio older than 0.4 answers the v1 listing with a 404.
        (_json({"error": "Unexpected endpoint"}, 404), "unsupported"),
        (_json({"error": {"message": "bad"}}, 400), "invalid_request"),
        (_json({"error": "boom"}, 500), "unavailable"),
        (httpx.Response(302, stream=httpx.ByteStream(b""), headers={"location": "http://169.254.169.254/"}), "invalid_response"),
        (httpx.Response(200, stream=httpx.ByteStream(b"<html>"), headers={"content-type": "text/html"}), "invalid_response"),
        (httpx.Response(200, stream=httpx.ByteStream(b"{not json"), headers={"content-type": "application/json"}), "invalid_response"),
        (
            httpx.Response(
                200,
                stream=httpx.ByteStream(b"\x1f\x8b..."),
                headers={"content-type": "application/json", "content-encoding": "gzip"},
            ),
            "invalid_response",
        ),
        (_json({"data": []}), "invalid_response"),
    ],
)
async def test_a_failure_is_one_fixed_sentence(response, kind):
    route = _Route(lambda request: response)
    with egress_route(route), pytest.raises(client.LMStudioError) as refused:
        await client.list_models(_CONNECTION)
    assert refused.value.kind == kind
    assert refused.value.public_message == client.PUBLIC_MESSAGES[kind]


async def test_a_body_over_the_cap_ends_the_transfer(monkeypatch):
    monkeypatch.setattr(client, "_MAX_BODY_BYTES", 64)
    route = _Route(lambda request: _json({"models": [{"key": "x" * 200}]}))
    with egress_route(route), pytest.raises(client.LMStudioError) as refused:
        await client.list_models(_CONNECTION)
    assert refused.value.kind == "invalid_response"


async def test_a_relay_failure_says_the_platform_s_sentence_never_the_relay_s_detail():
    from app.services.relay_transport import RELAY_ERROR_MESSAGES, relay_error

    def _fail(request):
        raise relay_error("connect_failed", "dial tcp 10.0.0.5:1234: connection refused (internal-host.corp)", request=request)

    route = _Route(_fail)
    with egress_route(route), pytest.raises(client.LMStudioError) as refused:
        await client.list_models(_CONNECTION)
    assert refused.value.kind == "relay_connect_failed"
    assert refused.value.public_message == f"{RELAY_ERROR_MESSAGES['connect_failed']}. Check the relay group on the Relays page."
    assert "internal-host" not in refused.value.public_message


async def test_an_upstream_message_reaches_the_log_only(caplog):
    route = _Route(lambda request: _json({"error": {"message": "key sk-secret-123 refused for tenant acme"}}, 400))
    with egress_route(route), pytest.raises(client.LMStudioError) as refused:
        await client.list_models(_CONNECTION)
    assert "tenant" not in refused.value.public_message
    assert "tenant acme" in refused.value.detail


# ---------------------------------------------------------------------------
# Load, unload, download
# ---------------------------------------------------------------------------


async def test_load_sends_only_the_options_given_and_waits_long():
    route = _Route(lambda request: _json({"type": "llm", "instance_id": "openai/gpt-oss-20b", "load_time_seconds": 9.1, "status": "loaded"}))
    with egress_route(route):
        loaded = await client.load_model(_CONNECTION, "openai/gpt-oss-20b", context_length=16384, flash_attention=True)

    [request] = route.seen
    assert (request.method, request.url.path) == ("POST", "/api/v1/models/load")
    assert json.loads(request.content) == {"model": "openai/gpt-oss-20b", "context_length": 16384, "flash_attention": True}
    assert request.extensions["timeout"]["read"] == 600.0
    assert loaded == {"instance_id": "openai/gpt-oss-20b", "type": "llm", "load_time_seconds": 9.1}


async def test_load_refuses_an_answer_without_an_instance():
    route = _Route(lambda request: _json({"status": "loaded", "instance_id": "../x"}))
    with egress_route(route), pytest.raises(client.LMStudioError) as refused:
        await client.load_model(_CONNECTION, "openai/gpt-oss-20b")
    assert refused.value.kind == "invalid_response"


async def test_a_bad_key_is_refused_before_anything_is_sent():
    route = _Route(lambda request: _json({}))
    with egress_route(route):
        for call in (
            client.load_model(_CONNECTION, "../x"),
            client.unload_model(_CONNECTION, "a b"),
            client.download_model(_CONNECTION, "https://evil.example/m"),
            client.download_model(_CONNECTION, "https://huggingface.co/a/b", quantization="Q4 K"),
        ):
            with pytest.raises(client.LMStudioError) as refused:
                await call
            assert refused.value.kind == "invalid_request"
        with pytest.raises(client.LMStudioError) as refused:
            await client.download_status(_CONNECTION, "../../models")
        assert refused.value.kind == "not_found"
    assert route.seen == []


async def test_unload_names_the_instance():
    route = _Route(lambda request: _json({"instance_id": "openai/gpt-oss-20b"}))
    with egress_route(route):
        assert await client.unload_model(_CONNECTION, "openai/gpt-oss-20b") == "openai/gpt-oss-20b"
    assert json.loads(route.seen[0].content) == {"instance_id": "openai/gpt-oss-20b"}


async def test_a_download_and_its_status():
    answers = iter(
        [
            _json({"job_id": "job_493c7c9ded", "status": "downloading", "total_size_bytes": 2279145003, "started_at": "2025-10-03T15:33:23.496Z"}),
            _json({"job_id": "job_493c7c9ded", "status": "completed", "total_size_bytes": 2279145003, "downloaded_bytes": 2279145003, "completed_at": "2025-10-03T15:40:00Z"}),
        ]
    )
    route = _Route(lambda request: next(answers))
    with egress_route(route):
        started = await client.download_model(_CONNECTION, "https://huggingface.co/lmstudio-community/Qwen3-4B-GGUF", quantization="Q4_K_M")
        done = await client.download_status(_CONNECTION, "job_493c7c9ded")

    first, second = route.seen
    assert json.loads(first.content) == {"model": "https://huggingface.co/lmstudio-community/Qwen3-4B-GGUF", "quantization": "Q4_K_M"}
    assert second.url.path == "/api/v1/models/download/status/job_493c7c9ded"
    assert (started["job_id"], started["status"], started["downloaded_bytes"]) == ("job_493c7c9ded", "downloading", None)
    assert (done["status"], done["downloaded_bytes"]) == ("completed", 2279145003)


async def test_an_already_downloaded_model_has_no_job():
    route = _Route(lambda request: _json({"status": "already_downloaded"}))
    with egress_route(route):
        job = await client.download_model(_CONNECTION, "openai/gpt-oss-20b")
    assert (job["job_id"], job["status"]) == (None, "already_downloaded")


async def test_an_unknown_download_status_is_refused():
    route = _Route(lambda request: _json({"status": "exploded", "job_id": "job_1"}))
    with egress_route(route), pytest.raises(client.LMStudioError) as refused:
        await client.download_status(_CONNECTION, "job_1")
    assert refused.value.kind == "invalid_response"


# ---------------------------------------------------------------------------
# Connectivity test
# ---------------------------------------------------------------------------


def _integration(*, routed: bool = True, base_url: str | None = "http://gpu-host:1234") -> SimpleNamespace:
    from app.core.security import encrypt_value

    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="lmstudio",
        relay_group_id=uuid.uuid4() if routed else None,
        api_key_enc=None,
        extra_config_enc=encrypt_value(json.dumps({"base_url": base_url} if base_url else {})),
    )


async def test_the_connection_test_is_a_registered_probe():
    from app.integrations.connectivity import has_connectivity_probe

    assert has_connectivity_probe("lmstudio")


async def test_the_connection_test_needs_a_relay_group():
    from app.integrations.connectivity import ProbeContext

    result = await client.check_connectivity(ProbeContext(provider="lmstudio", name="LM Studio", token=None, extra={}, integration=_integration(routed=False)))
    assert (result.status, result.message) == ("error", client.PUBLIC_MESSAGES["not_routed"])


async def test_the_connection_test_counts_the_models_through_the_relay():
    from app.integrations.connectivity import ProbeContext

    route = _Route(lambda request: _json(_LISTING))
    with egress_route(route):
        result = await client.check_connectivity(
            ProbeContext(provider="lmstudio", name="GPU box", token=None, extra={}, integration=_integration())
        )
    assert (result.status, result.message) == ("ok", "GPU box is connected through the relay: 2 models on the server, 1 loaded.")


async def test_the_connection_test_says_what_is_missing():
    from app.integrations.connectivity import ProbeContext

    result = await client.check_connectivity(
        ProbeContext(provider="lmstudio", name="GPU box", token=None, extra={}, integration=_integration(base_url=None))
    )
    assert (result.status, result.message) == ("error", f"GPU box: {client.PUBLIC_MESSAGES['no_base_url']}")


# ---------------------------------------------------------------------------
# The agent tool (and a dashboard's integration source)
# ---------------------------------------------------------------------------


async def test_the_models_tool_reads_through_the_route_and_answers_json():
    from app.services.tool_classification import definition_tool_type
    from app.tools.base import CallableToolExecutor, ToolCall

    executor = CallableToolExecutor()
    [definition] = client.build_tools("http://gpu-host:1234||lm-token", executor)
    assert definition.name == "list_lmstudio_models" and definition_tool_type(definition) == "read"
    route = _Route(lambda request: _json(_LISTING))
    with egress_route(route):
        every = json.loads((await executor.execute_tool(ToolCall(id="a", name=definition.name, arguments={}))).content)
        loaded = json.loads(
            (await executor.execute_tool(ToolCall(id="b", name=definition.name, arguments={"loaded_only": True}))).content
        )
    assert route.seen[0].headers["authorization"] == "Bearer lm-token"
    assert (every["count"], every["loaded"]) == (2, 1)
    assert every["models"][0] == {
        "model": "GPT-OSS 20B", "key": "openai/gpt-oss-20b", "type": "llm", "publisher": "openai",
        "architecture": "gpt-oss", "params": "20B", "quantization": "MXFP4", "size_gb": 12.11,
        "max_context_length": 131072, "loaded_instances": 1, "vision": False, "tool_use": True,
    }  # fmt: skip
    assert [m["key"] for m in loaded["models"]] == ["openai/gpt-oss-20b"]


async def test_the_models_tool_says_why_it_reads_nothing_and_needs_a_server():
    from app.tools.base import CallableToolExecutor, ToolCall

    assert client.build_tools("||lm-token", CallableToolExecutor()) == []
    executor = CallableToolExecutor()
    [definition] = client.build_tools("http://gpu-host:1234||", executor)
    # No relay route: the fixed sentence, never a call.
    answer = await executor.execute_tool(ToolCall(id="a", name=definition.name, arguments={}))
    assert answer.content.startswith("Error: ")
