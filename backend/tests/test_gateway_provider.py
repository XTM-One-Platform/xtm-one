"""Pytest coverage for the Filigran AI Gateway provider.

Covers the XTM-JWT auth hook (per-request minting, claim shape, headers),
the credential parsing, the caller-identity context, and chat/stream/models
round-trips against an httpx MockTransport — no network, no gateway needed.
Mirrors the style of test_xtm_auth_smoke.py (real production modules).
"""

import json
import uuid

import httpx
import jwt as pyjwt
import openai
import pytest

from app.core.xtm_auth import _normalize_url, get_jwks_payload
from app.llm.base import ChatMessage, ModelDeclinedError
from app.llm.gateway_provider import (
    FiligranGatewayProvider,
    StaticTokenAuth,
    XtmJwtAuth,
    _gateway_http_client,
    _obey_retryable_false,
    parse_gateway_credential,
    redact_token,
)
from app.llm.identity import (
    LLMCallerIdentity,
    _llm_caller,
    resolve_llm_caller,
    set_llm_caller,
)
from app.llm.music import MusicGenerationError, MusicProviderBusyError, MusicReference, MusicRequest
from app.llm.openai_compat import is_retryable_openai
from app.tools.base import ToolDefinition

GATEWAY_URL = "http://gateway.local:8080"
AUD = _normalize_url(GATEWAY_URL)

USER_ID = uuid.uuid4()
EMAIL = "alice@example.com"


@pytest.fixture(autouse=True)
def _clear_caller_context():
    """Isolate the ContextVar between tests."""
    token = _llm_caller.set(None)
    yield
    _llm_caller.reset(token)


def _public_key():
    """This platform's Ed25519 public key, as PyJWT expects it."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    jwk = get_jwks_payload()["keys"][0]
    import base64

    raw = base64.urlsafe_b64decode(jwk["x"] + "==")
    return Ed25519PublicKey.from_public_bytes(raw)


def _run_auth_flow(auth: XtmJwtAuth) -> httpx.Request:
    """Run the (sync-generator) auth flow on a bare request."""
    request = httpx.Request(
        "POST",
        f"{GATEWAY_URL}/v1/chat/completions",
        headers={"Authorization": "Bearer xtm-jwt"},  # SDK placeholder
    )
    flow = auth.auth_flow(request)
    return next(flow)


class TestXtmJwtAuth:
    def test_mints_a_valid_jwt_with_spec_claims(self):
        set_llm_caller(USER_ID, EMAIL)
        signed = _run_auth_flow(XtmJwtAuth(AUD))

        bearer = signed.headers["Authorization"]
        assert bearer.startswith("Bearer ")
        token = bearer.removeprefix("Bearer ")
        # Verifies against this platform's own JWKS (what the gateway fetches).
        claims = pyjwt.decode(
            token, _public_key(), algorithms=["EdDSA"], audience=AUD
        )
        assert claims["sub"] == str(USER_ID)
        assert claims["email"] == EMAIL
        assert claims["aud"] == AUD
        assert claims["jti"]
        assert claims["exp"] > claims["iat"]
        # kid header present (the gateway resolves the JWKS key by it).
        header = pyjwt.get_unverified_header(token)
        assert header["alg"] == "EdDSA"
        assert header["kid"]

    def test_sets_attribution_headers(self):
        set_llm_caller(USER_ID, EMAIL)
        signed = _run_auth_flow(XtmJwtAuth(AUD, target_provider="anthropic"))
        assert signed.headers["X-User-Id"] == str(USER_ID)
        assert signed.headers["X-Source"] == "xtm-one"
        assert signed.headers["X-Target-Provider"] == "anthropic"

    def test_no_target_provider_header_by_default(self):
        set_llm_caller(USER_ID, EMAIL)
        signed = _run_auth_flow(XtmJwtAuth(AUD))
        assert "X-Target-Provider" not in signed.headers

    def test_token_replaces_the_jwt(self):
        set_llm_caller(USER_ID, EMAIL)
        signed = _run_auth_flow(StaticTokenAuth("static-token"))
        assert signed.headers["Authorization"] == "Bearer static-token"
        assert signed.headers["X-User-Id"] == str(USER_ID)

    def test_token_needs_no_caller_identity(self, monkeypatch):
        monkeypatch.setattr("app.llm.identity._service_identity", None)
        signed = _run_auth_flow(StaticTokenAuth("static-token"))
        assert signed.headers["Authorization"] == "Bearer static-token"
        assert "X-User-Id" not in signed.headers

    def test_each_request_gets_a_fresh_token(self):
        set_llm_caller(USER_ID, EMAIL)
        auth = XtmJwtAuth(AUD)
        t1 = _run_auth_flow(auth).headers["Authorization"]
        t2 = _run_auth_flow(auth).headers["Authorization"]
        j1 = pyjwt.decode(
            t1.removeprefix("Bearer "), _public_key(), algorithms=["EdDSA"], audience=AUD
        )
        j2 = pyjwt.decode(
            t2.removeprefix("Bearer "), _public_key(), algorithms=["EdDSA"], audience=AUD
        )
        assert j1["jti"] != j2["jti"]


class TestCallerIdentity:
    def test_context_value_wins(self):
        set_llm_caller(USER_ID, EMAIL)
        ident = resolve_llm_caller()
        assert ident.user_id == USER_ID
        assert ident.email == EMAIL

    def test_falls_back_to_service_identity(self, monkeypatch):
        service = LLMCallerIdentity(user_id=uuid.uuid4(), email="admin@filigran.io")
        monkeypatch.setattr("app.llm.identity._service_identity", service)
        assert resolve_llm_caller() is service

    def test_raises_without_any_identity(self, monkeypatch):
        monkeypatch.setattr("app.llm.identity._service_identity", None)
        with pytest.raises(RuntimeError):
            resolve_llm_caller()

    def test_non_uuid_caller_is_ignored(self):
        set_llm_caller("anonymous", "x@example.com")
        assert _llm_caller.get() is None


class TestProviderConstruction:
    def test_credential_parsing_variants(self):
        for cred, expected_base in [
            (GATEWAY_URL, GATEWAY_URL),
            (f"{GATEWAY_URL}/", GATEWAY_URL),
            (f"{GATEWAY_URL}/v1", GATEWAY_URL),
        ]:
            p = FiligranGatewayProvider(api_key=cred)
            assert p.gateway_url == expected_base
            assert p.base_url == f"{expected_base}/v1"

    def test_target_provider_suffix(self):
        p = FiligranGatewayProvider(api_key=f"{GATEWAY_URL}||anthropic")
        auth = p.client._client.auth  # the httpx client's auth hook
        assert isinstance(auth, XtmJwtAuth)
        assert auth.target_provider == "anthropic"

    @pytest.mark.parametrize(
        ("cred", "target", "token"),
        [
            (GATEWAY_URL, None, None),
            (f"{GATEWAY_URL}||anthropic", "anthropic", None),
            (f"{GATEWAY_URL}||anthropic||tok", "anthropic", "tok"),
            (f"{GATEWAY_URL}||||tok", None, "tok"),
            (f" {GATEWAY_URL}/v1/ || || tok ", None, "tok"),
            (f"{GATEWAY_URL}||anthropic||", "anthropic", None),
            (f"{GATEWAY_URL}||||a||b", None, "a||b"),
        ],
    )
    def test_parse_credential(self, cred, target, token):
        parsed = parse_gateway_credential(cred)
        assert parsed.base_url == GATEWAY_URL
        assert parsed.target_provider == target
        assert parsed.token == token

    def test_token_selects_the_static_token_auth(self):
        p = FiligranGatewayProvider(api_key=f"{GATEWAY_URL}||anthropic||tok")
        auth = p.client._client.auth
        assert isinstance(auth, StaticTokenAuth)
        assert auth.token == "tok"
        assert auth.target_provider == "anthropic"

    def test_no_token_keeps_the_jwt_auth(self):
        p = FiligranGatewayProvider(api_key=GATEWAY_URL)
        assert isinstance(p.client._client.auth, XtmJwtAuth)

    def test_registered_in_both_provider_maps(self):
        from app.api.v1.settings import PROVIDER_MAP as SETTINGS_MAP
        from app.llm.router import PROVIDER_MAP as ROUTER_MAP

        assert ROUTER_MAP["filigran_gateway"] is FiligranGatewayProvider
        assert SETTINGS_MAP["filigran_gateway"] is FiligranGatewayProvider


def _mock_provider(handler) -> FiligranGatewayProvider:
    """A provider whose HTTP layer is an httpx MockTransport."""
    return FiligranGatewayProvider(
        api_key=GATEWAY_URL,
        http_client=httpx.AsyncClient(
            auth=XtmJwtAuth(AUD),
            transport=httpx.MockTransport(handler),
        ),
    )


def _assert_gateway_auth(request: httpx.Request) -> None:
    """The gateway-side check: a verifiable EdDSA JWT with the right aud."""
    token = request.headers["Authorization"].removeprefix("Bearer ")
    claims = pyjwt.decode(token, _public_key(), algorithms=["EdDSA"], audience=AUD)
    assert claims["sub"] == str(USER_ID)


class TestTokenRedaction:
    @pytest.mark.asyncio
    async def test_list_models_never_echoes_the_token(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, text="Invalid token: s3cret-token-value")

        provider = FiligranGatewayProvider(
            api_key=f"{GATEWAY_URL}||||s3cret-token-value",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises(RuntimeError) as exc:
            await provider.list_models()

        assert "s3cret-token-value" not in str(exc.value)
        assert "***" in str(exc.value)
        assert exc.value.__cause__ is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", ["chat", "stream", "embed"])
    async def test_no_call_echoes_the_token(self, call):
        token = 's3cret"token/value'

        def handler(request: httpx.Request) -> httpx.Response:
            # Echoed raw, JSON-escaped and URL-encoded.
            return httpx.Response(
                401,
                json={"error": {"message": f"Invalid token {token}", "raw": token}},
                headers={"x-echo": "1"},
            )

        provider = FiligranGatewayProvider(
            api_key=f"{GATEWAY_URL}||||{token}",
            http_client=_gateway_http_client(
                StaticTokenAuth(token), token, transport=httpx.MockTransport(handler)
            ),
        )
        messages = [ChatMessage(role="user", content="hi")]
        with pytest.raises(Exception) as exc:
            if call == "chat":
                await provider.chat(messages, model="m")
            elif call == "stream":
                async for _ in provider.chat_stream(messages, model="m"):
                    pass
            else:
                await provider.embed("hi", model="m")

        text = repr(exc.value) + str(exc.value) + str(getattr(exc.value, "body", ""))
        assert token not in text
        assert "s3cret\\\"token" not in text  # the JSON-escaped form
        assert "***" in text

    def test_redaction_covers_the_encoded_forms(self):
        token = 'abc"def/ghi=jkl'
        assert redact_token(f'x {token} y', token) == "x *** y"
        assert redact_token('x abc\\"def/ghi=jkl y', token) == "x *** y"
        assert redact_token("x abc%22def%2Fghi%3Djkl y", token) == "x *** y"

    def test_a_short_token_is_redacted_in_its_encoded_forms_too(self):
        token = "ab/c+d="
        assert len(token) < 8
        assert redact_token("echo ab%2Fc%2Bd%3D end", token) == "echo *** end"
        assert redact_token("echo ab%2Fc%2Bd%3D and ab/c+d= end", token) == "echo *** and *** end"
        assert redact_token('echo q"r', 'q"r') == "echo ***"
        assert redact_token('echo q\\"r', 'q"r') == "echo ***"

    def test_the_credential_repr_hides_the_token(self):
        cred = parse_gateway_credential(f"{GATEWAY_URL}||anthropic||s3cret-token")
        assert "s3cret-token" not in repr(cred)
        assert "s3cret-token" not in f"{cred} {[cred]}"
        assert "***" in repr(cred)
        assert parse_gateway_credential(GATEWAY_URL).token is None
        assert "token=None" in repr(parse_gateway_credential(GATEWAY_URL))
        *_, token = cred  # still unpacks
        assert token == "s3cret-token"

    def test_a_short_token_is_replaced_as_a_whole_word_only(self):
        assert redact_token("bad token ab in cabinet", "ab") == "bad token *** in cabinet"
        assert redact_token("nothing", None) == "nothing"


class TestRetryableFalse:
    """A gateway 502 marked ``retryable: false`` is sent once (#11619)."""

    _UPSTREAM_400 = {"error": {
        "message": "upstream returned status 400 Bad Request",
        "type": "provider_error", "code": "provider_error", "retryable": False,
    }}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", ["chat", "chat_stream"])
    @pytest.mark.parametrize("token", [None, "s3cret-token-value"])
    async def test_it_is_sent_once(self, call, token):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(502, json=self._UPSTREAM_400)

        set_llm_caller(USER_ID, EMAIL)
        auth = StaticTokenAuth(token) if token else XtmJwtAuth(AUD)
        provider = FiligranGatewayProvider(
            api_key=f"{GATEWAY_URL}||||{token}" if token else GATEWAY_URL,
            http_client=_gateway_http_client(auth, token, transport=httpx.MockTransport(handler)),
        )
        messages = [ChatMessage(role="user", content="hi")]
        with pytest.raises(openai.InternalServerError) as raised:
            if call == "chat":
                await provider.chat(messages, model="gpt-6-astra")
            else:
                async for _ in provider.chat_stream(messages, model="gpt-6-astra"):
                    pass
        assert not is_retryable_openai(raised.value)
        # The owner lookup's ``/v1/models`` is not the request under test.
        assert len([c for c in calls if c.url.path.endswith("/chat/completions")]) == 1

    @pytest.mark.parametrize(
        ("body", "retryable"),
        [
            ({"error": {"code": "provider_error", "retryable": False}}, False),
            ({"code": "provider_error", "retryable": False}, False),  # the SDK's unwrapped ``body``
            ({"error": {"code": "provider_error", "retryable": True}}, True),
            ({"error": {"code": "provider_error"}}, True),
            ("<html>Bad Gateway</html>", True),
        ],
        ids=["false", "false-unwrapped", "true", "absent", "html"],
    )
    def test_only_retryable_false_stops_the_retry(self, body, retryable):
        response = httpx.Response(502, request=httpx.Request("POST", f"{GATEWAY_URL}/v1/chat/completions"))
        exc = openai.InternalServerError("upstream", response=response, body=body)
        assert is_retryable_openai(exc) is retryable

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("answer", "header"),
        [
            (httpx.Response(502, json=_UPSTREAM_400), "false"),
            (httpx.Response(502, json={"error": {"code": "provider_error", "retryable": True}}), None),
            (httpx.Response(502, json={"error": {"code": "provider_error"}}), None),
            (httpx.Response(502, text="<html>Bad Gateway</html>"), None),
        ],
        ids=["false", "true", "absent", "html"],
    )
    async def test_only_retryable_false_tells_the_sdk_not_to_resend(self, answer, header):
        await _obey_retryable_false(answer)
        assert answer.headers.get("x-should-retry") == header


class TestChatAndModels:
    @pytest.mark.asyncio
    async def test_chat_sends_jwt_and_parses_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/chat/completions"
            _assert_gateway_auth(request)
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "claude-sonnet-4-5",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "Hello!"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                },
            )

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        response = await provider.chat(
            [ChatMessage(role="user", content="hi")], model="claude-sonnet-4-5"
        )
        assert response.content == "Hello!"
        assert response.usage["prompt_tokens"] == 5

    @pytest.mark.asyncio
    @pytest.mark.parametrize("effort", ["high", None])
    async def test_chat_with_tools_sends_effort_none_to_gpt6(self, effort):
        """gpt-6 refuses an effort but ``none`` next to tools (#11571)."""
        sent: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"object": "list", "data": []})
            sent.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "gpt-6",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                },
            )

        set_llm_caller(USER_ID, EMAIL)
        tool = ToolDefinition(
            name="lookup", description="Look up", input_schema={"type": "object", "properties": {}}
        )
        await _mock_provider(handler).chat(
            [ChatMessage(role="user", content="hi")],
            model="gpt-6",
            tools=[tool],
            reasoning_effort=effort,
        )
        assert sent["tools"]
        assert sent["reasoning_effort"] == "none"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", ["chat", "chat_stream", "chat_stream_with_tools"])
    @pytest.mark.parametrize(
        ("model", "nudge", "roles"),
        [
            ("claude-opus-5-5", False, ["user"]),
            ("anthropic/claude-opus-5.5", False, ["user"]),
            ("gpt-5", False, ["user", "assistant"]),
            # The agent loop's nudges come after the reply; the route hoists them.
            ("claude-opus-5-5", True, ["user", "system"]),
            ("gpt-5", True, ["user", "assistant", "system"]),
        ],
    )
    async def test_a_claude_model_gets_no_trailing_assistant_turn(self, call, model, nudge, roles):
        sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            sent.extend(body["messages"])
            if body.get("stream"):
                chunk = {"id": "1", "object": "chat.completion.chunk", "created": 1, "model": model,
                         "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
                return httpx.Response(
                    200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        messages = [ChatMessage(role="user", content="hi"), ChatMessage(role="assistant", content="partial")]
        if nudge:
            messages.append(ChatMessage(role="system", content="continue"))
        if call == "chat_stream":
            async for _ in provider.chat_stream(messages, model=model):
                pass
        else:
            await getattr(provider, call)(messages, model=model)
        assert [m["role"] for m in sent] == roles

    @staticmethod
    async def _send(call: str, model: str, handler, **kwargs) -> None:
        """One request through *call*, with a system prompt, a turn and a tool."""
        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        messages = [ChatMessage(role="system", content="You are helpful."), ChatMessage(role="user", content="hi")]
        tool = ToolDefinition(name="lookup", description="Look up", input_schema={"type": "object", "properties": {}})
        if call == "chat_stream":
            async for _ in provider.chat_stream(messages, model=model, **kwargs):
                pass
        else:
            await getattr(provider, call)(messages, model=model, tools=[tool], **kwargs)

    @staticmethod
    def _recording_handler(sent: dict, model: str, owner: str = "openai"):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"object": "list", "data": [
                    {"id": model, "object": "model", "created": 0, "owned_by": owner},
                ]})
            sent.update(json.loads(request.content))
            if sent.get("stream"):
                chunk = {"id": "1", "object": "chat.completion.chunk", "created": 1, "model": model,
                         "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
                return httpx.Response(
                    200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(200, json={
                "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            })

        return handler

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", ["chat", "chat_stream", "chat_stream_with_tools"])
    @pytest.mark.parametrize("model", ["claude-opus-5-5", "anthropic/claude-opus-5.5"])
    async def test_a_claude_model_gets_the_prompt_cache_breakpoints(self, call, model):
        """Tools, system prompt and the conversation, as the Anthropic provider marks them (#11640)."""
        sent: dict = {}
        await self._send(call, model, self._recording_handler(sent, model))
        marker = {"type": "ephemeral"}
        assert sent["cache_control"] == marker
        assert sent["messages"][0] == {
            "role": "system",
            "content": [{"type": "text", "text": "You are helpful.", "cache_control": marker}],
        }
        # The turn is left as it was: the top-level marker covers it.
        assert sent["messages"][1] == {"role": "user", "content": "hi"}
        if call != "chat_stream":
            assert sent["tools"][-1]["cache_control"] == marker

    @pytest.mark.asyncio
    @pytest.mark.parametrize("found_by", ["listed owner", "pinned target"])
    async def test_an_alias_served_by_anthropic_gets_the_breakpoints(self, found_by):
        sent: dict = {}
        set_llm_caller(USER_ID, EMAIL)
        if found_by == "listed owner":
            provider = _mock_provider(self._recording_handler(sent, "default", owner="Anthropic"))
        else:
            provider = _mock_provider(self._recording_handler(sent, "default", owner="openai"))
            provider._target = "anthropic"
        await provider.chat([ChatMessage(role="user", content="hi")], model="default")
        assert sent["cache_control"] == {"type": "ephemeral"}

    @pytest.mark.asyncio
    async def test_a_failed_listing_sends_the_request_unmarked_without_waiting(self):
        """One bounded try, then the chat request (antoinegaston's review on #11645)."""
        listings: list[httpx.Request] = []
        sent: dict = {}
        record = self._recording_handler(sent, "default")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                listings.append(request)
                return httpx.Response(500, json={"error": {"message": "down"}})
            return record(request)

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        for _ in range(2):
            await provider.chat([ChatMessage(role="user", content="hi")], model="default")
        assert "cache_control" not in sent
        # No SDK retry, a short read timeout, and not asked again within the miss TTL.
        assert len(listings) == 1
        assert listings[0].extensions["timeout"]["read"] == 5.0

    def test_a_pinned_target_is_kept_lowercased(self):
        assert FiligranGatewayProvider(api_key=f"{GATEWAY_URL}||Anthropic")._target == "anthropic"

    def test_the_last_leading_system_message_is_marked_and_a_tool_result_left_a_string(self):
        from app.llm.gateway_provider import mark_claude_cache_breakpoints

        marker = {"type": "ephemeral"}
        tool_result = {"role": "tool", "tool_call_id": "c1", "content": "result"}
        params = {"model": "claude-opus-5-5", "messages": [
            {"role": "system", "content": "base"},
            {"role": "system", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
            {"role": "user", "content": "hi"},
            {"role": "system", "content": "nudge"},
            tool_result,
        ]}
        mark_claude_cache_breakpoints(params, marker)
        messages = params["messages"]
        assert messages[0] == {"role": "system", "content": "base"}
        assert messages[1]["content"] == [{"type": "text", "text": "a"}, {"type": "text", "text": "b", "cache_control": marker}]
        assert messages[3] == {"role": "system", "content": "nudge"}
        # The gateway reads a tool result only as a string.
        assert messages[4] == tool_result
        assert params["extra_body"] == {"cache_control": marker}

    def test_the_breakpoint_ends_the_stable_system_prefix(self):
        from app.llm.gateway_provider import mark_claude_cache_breakpoints
        from app.llm.system_prompt_split import SYSTEM_VOLATILE_TAIL_MARKER

        marker = {"type": "ephemeral"}
        volatile = f"\n\n{SYSTEM_VOLATILE_TAIL_MARKER}2026-11-16 09:00"
        params = {"model": "claude-opus-5-5", "messages": [
            {"role": "system", "content": "persona" + volatile},
            {"role": "user", "content": "hi"},
        ]}
        mark_claude_cache_breakpoints(params, marker)
        assert params["messages"][0]["content"] == [
            {"type": "text", "text": "persona", "cache_control": marker},
            {"type": "text", "text": volatile},
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", ["chat", "chat_stream", "chat_stream_with_tools"])
    async def test_another_model_gets_no_cache_breakpoints(self, call):
        sent: dict = {}
        await self._send(call, "gpt-5", self._recording_handler(sent, "gpt-5"))
        assert "cache_control" not in json.dumps(sent)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("disabled", ["setting", "kwarg"])
    async def test_prompt_caching_switched_off_sends_no_breakpoints(self, disabled, monkeypatch):
        from app.core.config import settings

        kwargs = {}
        if disabled == "setting":
            monkeypatch.setattr(settings, "prompt_caching_enabled", False)
        else:
            kwargs["prompt_cache"] = False
        sent: dict = {}
        await self._send("chat", "claude-opus-5-5", self._recording_handler(sent, "claude-opus-5-5"), **kwargs)
        assert "cache_control" not in json.dumps(sent)

    @pytest.mark.asyncio
    async def test_the_one_hour_ttl_is_sent(self, monkeypatch):
        from app.core.config import settings

        monkeypatch.setattr(settings, "prompt_cache_ttl", "1h")
        sent: dict = {}
        await self._send("chat", "claude-opus-5-5", self._recording_handler(sent, "claude-opus-5-5"))
        assert sent["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", ["chat", "chat_stream", "chat_stream_with_tools"])
    @pytest.mark.parametrize(
        ("model", "disable_thinking", "expected"),
        [
            ("claude-opus-5-5", True, "none"),
            ("anthropic/claude-opus-4.7", True, "none"),
            ("claude-opus-5-5", False, "high"),
            ("gpt-5", True, "high"),
            # Takes no effort at all, so nothing is sent.
            ("claude-sonnet-4-5", True, None),
        ],
    )
    async def test_disable_thinking_sends_effort_none_to_claude(
        self, call, model, disable_thinking, expected
    ):
        """``disable_thinking`` reaches Claude as ``reasoning_effort: none`` (#11655)."""
        sent: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"object": "list", "data": []})
            sent.update(json.loads(request.content))
            if sent.get("stream"):
                chunk = {"id": "1", "object": "chat.completion.chunk", "created": 1, "model": model,
                         "choices": [{"index": 0, "delta": {"content": "{}"}, "finish_reason": "stop"}]}
                return httpx.Response(
                    200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "{}"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        messages = [ChatMessage(role="user", content="hi")]
        kwargs = {"model": model, "reasoning_effort": "high", "disable_thinking": disable_thinking}
        if call == "chat_stream":
            async for _ in provider.chat_stream(messages, **kwargs):
                pass
        else:
            await getattr(provider, call)(messages, **kwargs)
        assert sent.get("reasoning_effort") == expected

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", ["chat", "chat_stream_with_tools"])
    @pytest.mark.parametrize(
        ("model", "claude"),
        [("claude-opus-5-5", True), ("anthropic/claude-opus-5.5", True), ("gpt-5", False)],
    )
    async def test_a_claude_model_gets_the_anthropic_tool_wire_form(self, call, model, claude):
        sent: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            sent.update(body)
            if body.get("stream"):
                chunk = {"id": "1", "object": "chat.completion.chunk", "created": 1, "model": model,
                         "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
                return httpx.Response(
                    200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        schema = {
            "type": "object",
            "properties": {"id": {"type": "string"}, "name": {"type": "string"}},
            "oneOf": [{"required": ["id"]}, {"required": ["name"]}],
        }
        tools = [
            ToolDefinition(name="lookup", description="old", input_schema={"type": "object"}),
            ToolDefinition(name="lookup", description="new", input_schema=schema),
        ]
        set_llm_caller(USER_ID, EMAIL)
        await getattr(_mock_provider(handler), call)(
            [ChatMessage(role="user", content="hi")], model=model, tools=tools
        )
        functions = [t["function"] for t in sent["tools"]]
        if claude:
            assert [(f["name"], f["description"]) for f in functions] == [("lookup", "new")]
            assert "oneOf" not in functions[0]["parameters"]
            assert functions[0]["parameters"]["properties"] == schema["properties"]
            assert "oneOf" in schema  # the shared definition is untouched
        else:
            assert [f["parameters"] for f in functions] == [{"type": "object"}, schema]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", ["chat", "chat_stream_with_tools"])
    @pytest.mark.parametrize(
        ("target", "listing", "anthropic"),
        [
            # An alias the listing says Anthropic owns.
            (None, {"id": "default", "object": "model", "owned_by": "Anthropic"}, True),
            (None, {"id": "default", "object": "model", "owned_by": "openai"}, False),
            # A pinned credential is decided without the listing.
            ("Anthropic", None, True),
            ("openai", None, False),
            # An unreadable listing leaves the tools raw, the chat still goes out.
            (None, None, False),
        ],
    )
    async def test_an_alias_anthropic_serves_gets_the_anthropic_tool_wire_form(
        self, call, target, listing, anthropic
    ):
        sent: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                if listing is None:
                    return httpx.Response(500, json={"error": {"message": "down"}})
                return httpx.Response(200, json={"object": "list", "data": [listing]})
            body = json.loads(request.content)
            sent.update(body)
            if body.get("stream"):
                chunk = {"id": "1", "object": "chat.completion.chunk", "created": 1, "model": "default",
                         "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
                return httpx.Response(
                    200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
                    headers={"content-type": "text/event-stream"},
                )
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "default",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                },
            )

        schema = {"type": "object", "oneOf": [{"required": ["id"]}, {"required": ["name"]}]}
        provider = FiligranGatewayProvider(
            api_key=f"{GATEWAY_URL}||{target}" if target else GATEWAY_URL,
            http_client=httpx.AsyncClient(
                auth=XtmJwtAuth(AUD), transport=httpx.MockTransport(handler)
            ),
            max_retries=0,
        )
        set_llm_caller(USER_ID, EMAIL)
        await getattr(provider, call)(
            [ChatMessage(role="user", content="hi")],
            model="default",
            tools=[ToolDefinition(name="lookup", description="d", input_schema=schema)],
        )
        assert ("oneOf" not in sent["tools"][0]["function"]["parameters"]) is anthropic

    @pytest.mark.asyncio
    async def test_chat_stream_sends_jwt(self):
        def handler(request: httpx.Request) -> httpx.Response:
            _assert_gateway_auth(request)
            chunks = [
                {"id": "1", "object": "chat.completion.chunk", "created": 1,
                 "model": "m", "choices": [{"index": 0, "delta": {"content": "Hel"},
                                            "finish_reason": None}]},
                {"id": "1", "object": "chat.completion.chunk", "created": 1,
                 "model": "m", "choices": [{"index": 0, "delta": {"content": "lo"},
                                            "finish_reason": "stop"}]},
            ]
            body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(
                200, content=body.encode(), headers={"content-type": "text/event-stream"}
            )

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        out = []
        async for chunk in provider.chat_stream(
            [ChatMessage(role="user", content="hi")], model="m"
        ):
            out.append(chunk)
        assert "".join(out) == "Hello"

    @pytest.mark.asyncio
    async def test_a_refusal_is_raised_as_a_decline_with_its_category(self):
        # The gateway answers a model refusal with ``content_filter`` and
        # ``stop_details`` on the choice.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "claude-sonnet-5-5",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": None, "refusal": "No."},
                            "finish_reason": "content_filter",
                            "stop_details": {"category": "cyber", "explanation": "Cyber harm."},
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                },
            )

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        with pytest.raises(ModelDeclinedError) as caught:
            await provider.chat(
                [ChatMessage(role="user", content="hi")], model="claude-sonnet-5-5"
            )
        assert caught.value.category == "cyber"
        assert caught.value.explanation == "Cyber harm."
        assert caught.value.usage["prompt_tokens"] == 5

    @pytest.mark.asyncio
    async def test_a_streamed_refusal_is_raised_as_a_decline_with_its_category(self):
        def handler(request: httpx.Request) -> httpx.Response:
            chunk = {"id": "1", "object": "chat.completion.chunk", "created": 1, "model": "m"}
            chunks = [
                {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "content_filter",
                                       "stop_details": {"category": "cyber"}}]},
                {**chunk, "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2,
                                                   "total_tokens": 7}},
            ]
            body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return httpx.Response(
                200, content=body.encode(), headers={"content-type": "text/event-stream"}
            )

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        with pytest.raises(ModelDeclinedError) as caught:
            async for _ in provider.chat_stream(
                [ChatMessage(role="user", content="hi")], model="m"
            ):
                pass
        assert caught.value.category == "cyber"
        assert caught.value.usage["prompt_tokens"] == 5

    @pytest.mark.asyncio
    async def test_list_models_sends_jwt_and_filters_chat(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v1/models"
            _assert_gateway_auth(request)
            return httpx.Response(
                200,
                json={"object": "list", "data": [
                    {"id": "claude-sonnet-4-5", "object": "model", "owned_by": "anthropic", "type": "chat"},
                    {"id": "gpt-4o", "object": "model", "owned_by": "openai", "type": "chat"},
                    {"id": "text-embedding-3-large", "object": "model", "owned_by": "openai", "type": "embedding"},
                ]},
            )

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        # list_models() returns chat models only, sorted; embeddings are excluded.
        assert await provider.list_models() == ["claude-sonnet-4-5", "gpt-4o"]

    @pytest.mark.asyncio
    async def test_typed_listing_and_grouping(self):
        payload = {"object": "list", "data": [
            {"id": "gpt-4o", "object": "model", "owned_by": "openai", "type": "chat"},
            {"id": "claude-sonnet-4-5", "object": "model", "owned_by": "anthropic", "type": "chat"},
            {"id": "text-embedding-3-large", "object": "model", "owned_by": "openai", "type": "embedding"},
            {"id": "dall-e-3", "object": "model", "owned_by": "openai", "type": "image"},
        ]}
        provider = _mock_provider(lambda r: httpx.Response(200, json=payload))
        set_llm_caller(USER_ID, EMAIL)
        assert await provider.list_embedding_models() == ["text-embedding-3-large"]
        assert await provider.list_image_models() == ["dall-e-3"]
        groups = await provider.list_models_grouped()
        assert groups == {"anthropic": ["claude-sonnet-4-5"], "openai": ["gpt-4o"]}

    @pytest.mark.asyncio
    async def test_embed_routes_through_gateway(self):
        def handler(request: httpx.Request) -> httpx.Response:
            _assert_gateway_auth(request)
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"object": "list", "data": [
                    {"id": "text-embedding-3-large", "object": "model",
                     "owned_by": "openai", "type": "embedding"},
                ]})
            assert request.url.path == "/v1/embeddings"
            assert request.headers["X-Target-Provider"] == "openai"
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                "model": "text-embedding-3-large",
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            })

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        vec = await provider.embed("hello", model="text-embedding-3-large")
        assert vec == [0.1, 0.2, 0.3]

    @staticmethod
    def _embedding_gateway(bodies: list[dict], *, refuse_dimensions: bool = False) -> FiligranGatewayProvider:
        """A gateway answering the size asked for (``dimensions``), else 3072."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            bodies.append(body)
            if refuse_dimensions and "dimensions" in body:
                # How the gateway reports any upstream 4xx (ai-gateway ``upstream_status_error``).
                return httpx.Response(502, json={"error": {
                    "message": "upstream returned status 400 Bad Request",
                    "type": "provider_error", "code": "provider_error", "retryable": False,
                }})
            size = body.get("dimensions", 3072)
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1] * size}],
                "model": body["model"],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            })

        set_llm_caller(USER_ID, EMAIL)
        return _mock_provider(handler)

    @pytest.mark.asyncio
    async def test_embed_sends_the_configured_dimension(self):
        bodies: list[dict] = []
        provider = self._embedding_gateway(bodies)
        provider.embedding_dimensions = 1024
        assert len(await provider.embed("hello", model="text-embedding-3-large")) == 1024
        assert bodies[0]["dimensions"] == 1024

    @pytest.mark.asyncio
    async def test_embed_without_a_configured_dimension_keeps_the_native_size(self):
        bodies: list[dict] = []
        provider = self._embedding_gateway(bodies)
        assert len(await provider.embed("hello", model="text-embedding-3-large")) == 3072
        assert "dimensions" not in bodies[0]

    @pytest.mark.asyncio
    async def test_embed_sends_no_dimension_to_a_model_that_takes_none(self):
        bodies: list[dict] = []
        provider = self._embedding_gateway(bodies)
        provider.embedding_dimensions = 1536
        assert len(await provider.embed("hello", model="text-embedding-ada-002")) == 3072
        assert "dimensions" not in bodies[0]

    @pytest.mark.asyncio
    async def test_a_refused_dimension_falls_back_to_the_native_size_once_per_model(self):
        bodies: list[dict] = []
        provider = self._embedding_gateway(bodies, refuse_dimensions=True)
        provider.client = provider.client.with_options(max_retries=0)
        provider.embedding_dimensions = 4096
        assert len(await provider.embed("hello", model="text-embedding-3-large")) == 3072
        assert len(await provider.embed("again", model="text-embedding-3-large")) == 3072
        assert [("dimensions" in b) for b in bodies] == [True, False, False]

    @pytest.mark.asyncio
    async def test_an_unrelated_upstream_400_does_not_turn_the_dimension_off(self):
        """Any upstream 400 is the same 502 (an input over the token limit, say): it is not remembered."""
        bodies: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"object": "list", "data": []})
            body = json.loads(request.content)
            bodies.append(body)
            if body["input"] == "too long":
                return httpx.Response(502, json={"error": {
                    "message": "upstream returned status 400 Bad Request", "code": "provider_error",
                }})
            size = body.get("dimensions", 3072)
            return httpx.Response(200, json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1] * size}],
                "model": body["model"],
            })

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        provider.client = provider.client.with_options(max_retries=0)
        provider.embedding_dimensions = 1024
        with pytest.raises(openai.InternalServerError):
            await provider.embed("too long", model="text-embedding-3-large")
        assert len(await provider.embed("hello", model="text-embedding-3-large")) == 1024
        assert [b.get("dimensions") for b in bodies] == [1024, None, 1024]

    @pytest.mark.asyncio
    async def test_another_upstream_failure_is_not_taken_for_a_refused_dimension(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(200, json={"object": "list", "data": []})
            return httpx.Response(502, json={"error": {
                "message": "upstream returned status 500 Internal Server Error", "code": "provider_error",
            }})

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        provider.client = provider.client.with_options(max_retries=0)
        provider.embedding_dimensions = 1024
        with pytest.raises(openai.InternalServerError):
            await provider.embed("hello", model="text-embedding-3-large")


def _listing_provider(*entries: dict) -> FiligranGatewayProvider:
    """A provider whose ``/v1/models`` answers *entries* (``owned_by`` defaults to ``acme``)."""
    data = [{"object": "model", "owned_by": "acme", **e} for e in entries]
    set_llm_caller(USER_ID, EMAIL)
    return _mock_provider(lambda r: httpx.Response(200, json={"object": "list", "data": data}))


class TestModelTypes:
    """A model is offered by the picker of every type the gateway lists for it."""

    _MULTI_TYPE = (
        {"id": "gpt-6-luna", "owned_by": "openai", "type": "chat", "types": ["chat", "decision"]},
        {"id": "acme-multi-1", "type": "chat", "types": ["chat", "embedding"]},
        {"id": "acme-draw-1", "type": "chat", "types": ["chat", "image"]},
        {"id": "acme-clip-1", "type": "video", "types": ["video"]},
        {"id": "acme-voice-1", "type": "speech", "types": ["speech"]},
        {"id": "tev1:0.8b", "type": "decision", "types": ["decision"]},
        {"id": "acme-song-1", "type": "music", "types": ["music"]},
    )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("picker", "expected"),
        [
            ("list_models", ["acme-draw-1", "acme-multi-1", "gpt-6-luna"]),
            ("list_embedding_models", ["acme-multi-1"]),
            ("list_image_models", ["acme-draw-1"]),
            ("list_video_models", ["acme-clip-1"]),
            ("list_speech_models", ["acme-voice-1"]),
            ("list_music_models", ["acme-song-1"]),
            ("list_models_grouped", {"acme": ["acme-draw-1", "acme-multi-1"], "openai": ["gpt-6-luna"]}),
        ],
    )
    async def test_each_picker_lists_the_models_of_its_types(self, picker, expected):
        provider = _listing_provider(*self._MULTI_TYPE)
        assert await getattr(provider, picker)() == expected

    @pytest.mark.asyncio
    async def test_the_offered_ids_are_every_model_whatever_its_types(self):
        listed, offered = await _listing_provider(*self._MULTI_TYPE).list_models_and_offered()
        assert listed == ["acme-draw-1", "acme-multi-1", "gpt-6-luna"]
        assert offered == sorted(e["id"] for e in self._MULTI_TYPE)

    @pytest.mark.asyncio
    async def test_a_name_classified_video_or_speech_model_is_still_listed_there(self):
        provider = _listing_provider(
            {"id": "sora-2", "type": "chat", "types": ["chat"]},
            {"id": "gpt-4o-mini-tts", "type": "chat", "types": ["chat"]},
        )
        assert await provider.list_video_models() == ["sora-2"]
        assert await provider.list_speech_models() == ["gpt-4o-mini-tts"]
        assert await provider.list_models() == ["gpt-4o-mini-tts", "sora-2"]

    @pytest.mark.asyncio
    async def test_a_listing_without_either_field_is_classified_by_id(self):
        provider = _listing_provider({"id": "text-embedding-3-small"}, {"id": "gpt-4o"})
        assert await provider.list_embedding_models() == ["text-embedding-3-small"]
        assert await provider.list_models() == ["gpt-4o"]

    @pytest.mark.asyncio
    async def test_types_that_name_nothing_usable_fall_back_to_type(self):
        provider = _listing_provider(
            {"id": "acme-empty-1", "type": "embedding", "types": []},
            {"id": "acme-number-1", "type": "embedding", "types": [123]},
            {"id": "acme-null-1", "type": "embedding", "types": None},
            {"id": "acme-str-1", "type": "embedding", "types": "chat"},
            {"id": "acme-obj-1", "type": "embedding", "types": {"chat": True}},
            {"id": "acme-blank-1", "type": "embedding", "types": [""]},
        )
        assert await provider.list_embedding_models() == [
            "acme-blank-1",
            "acme-empty-1",
            "acme-null-1",
            "acme-number-1",
            "acme-obj-1",
            "acme-str-1",
        ]
        assert await provider.list_models() == []

    @pytest.mark.asyncio
    async def test_a_type_xtm_one_does_not_know_is_ignored_and_the_others_still_count(self):
        provider = _listing_provider(
            {"id": "acme-rerank-1", "type": "rerank", "types": ["rerank"]},
            {"id": "acme-mixed-1", "type": "chat", "types": ["chat", "rerank"]},
            {"id": "acme-text-1", "type": "chat", "types": [1, "chat"]},
        )
        assert await provider.list_models() == ["acme-mixed-1", "acme-text-1"]
        for listed in (
            await provider.list_embedding_models(),
            await provider.list_image_models(),
            await provider.list_video_models(),
            await provider.list_speech_models(),
        ):
            assert "acme-rerank-1" not in listed

    @pytest.mark.asyncio
    async def test_types_are_deduplicated_in_order(self):
        provider = _listing_provider({"id": "acme-dup-1", "type": "chat", "types": ["chat", "decision", "chat"]})
        assert [m.types for m in await provider._models_with_meta()] == [("chat", "decision")]


class TestGenerateMusic:
    """``POST /v1/audio/music``: sent once, a refusal read from ``error.retryable``."""

    @staticmethod
    def _provider(answer):
        """A provider whose music route answers *answer* (a response, or an exception to raise); calls are recorded."""
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/models":
                return httpx.Response(
                    200, json={"object": "list", "data": [{"id": "lyria-3.5", "owned_by": "google", "types": ["music"]}]}
                )
            calls.append(request)
            if isinstance(answer, Exception):
                raise answer
            return answer

        set_llm_caller(USER_ID, EMAIL)
        return _mock_provider(handler), calls

    @staticmethod
    def _error(status: int, *, retryable=None, code="provider_error", message="Lyria refused the prompt"):
        error = {"message": message, "code": code}
        if retryable is not None:
            error["retryable"] = retryable
        return httpx.Response(status, json={"error": error})

    @pytest.mark.asyncio
    async def test_it_posts_the_request_and_returns_the_audio_with_its_type(self):
        provider, calls = self._provider(httpx.Response(200, content=b"ID3audio", headers={"content-type": "audio/mpeg"}))
        made = await provider.generate_music(
            MusicRequest(model="lyria-3.5", prompt=" Cinematic ", duration_seconds=90, negative_prompt="drums")
        )
        assert (made.audio, made.mime) == (b"ID3audio", "audio/mpeg")
        (call,) = calls
        assert call.url.path == "/v1/audio/music"
        assert json.loads(call.content) == {
            "model": "lyria-3.5",
            "prompt": "Cinematic",
            "duration_seconds": 90.0,
            "instrumental": True,
            "negative_prompt": "drums",
        }
        assert call.headers["X-Target-Provider"] == "google"
        assert uuid.UUID(call.headers["X-Request-Id"])
        _assert_gateway_auth(call)

    @pytest.mark.asyncio
    async def test_no_negative_prompt_is_sent_when_none_is_given(self):
        provider, calls = self._provider(httpx.Response(200, content=b"x", headers={"content-type": "audio/wav"}))
        made = await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))
        assert made.mime == "audio/wav"
        assert "negative_prompt" not in json.loads(calls[0].content)

    @pytest.mark.asyncio
    async def test_a_502_the_gateway_calls_retryable_is_asked_again_later(self):
        provider, calls = self._provider(self._error(502, retryable=True, message="Upstream overloaded"))
        with pytest.raises(MusicProviderBusyError, match="Upstream overloaded"):
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))
        assert len(calls) == 1  # the SDK does not re-send it either

    @pytest.mark.asyncio
    @pytest.mark.parametrize("retryable", [False, None])
    async def test_a_502_that_is_not_retryable_is_permanent(self, retryable):
        provider, calls = self._provider(self._error(502, retryable=retryable))
        with pytest.raises(MusicGenerationError, match="Lyria refused the prompt") as raised:
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))
        assert not isinstance(raised.value, MusicProviderBusyError)
        assert len(calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [
            httpx.Response(502, text="<html>Bad Gateway</html>"),
            httpx.Response(504, text="upstream timed out"),
            httpx.Response(500, json={"error": {"message": "boom", "code": "internal_error"}}),
            httpx.Response(503, json={"error": {"message": "down", "code": "provider_unavailable"}}),
        ],
        ids=["html-502", "proxy-504", "bare-500", "503-not-overloaded"],
    )
    async def test_a_5xx_the_gateway_does_not_call_safe_may_have_billed_and_is_permanent(self, answer):
        provider, calls = self._provider(answer)
        with pytest.raises(MusicGenerationError, match="not sent again") as raised:
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))
        assert not isinstance(raised.value, MusicProviderBusyError)
        assert len(calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "answer",
        [
            _error.__func__(429, code="rate_limited"),
            _error.__func__(503, code="gateway_overloaded"),
        ],
        ids=["429", "503-overloaded"],
    )
    async def test_a_refusal_before_any_upstream_call_is_asked_again_later(self, answer):
        provider, _ = self._provider(answer)
        with pytest.raises(MusicProviderBusyError) as raised:
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))
        assert raised.value.retry_after is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("code", ["rate_limited", "capacity_exceeded"])
    async def test_a_429_carries_the_wait_the_gateway_names(self, code):
        answer = httpx.Response(429, json={"error": {"message": "slow down", "code": code, "retry_after_seconds": 42}})
        provider, calls = self._provider(answer)
        with pytest.raises(MusicProviderBusyError, match="slow down") as raised:
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))
        assert raised.value.retry_after == 42.0
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_the_retry_after_header_stands_in_for_the_envelope(self):
        answer = httpx.Response(429, json={"error": {"message": "slow", "code": "rate_limited"}}, headers={"retry-after": "7"})
        provider, _ = self._provider(answer)
        with pytest.raises(MusicProviderBusyError) as raised:
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))
        assert raised.value.retry_after == 7.0

    @pytest.mark.asyncio
    async def test_a_quota_exceeded_429_is_permanent_and_says_when_it_resets(self):
        error = {"message": "Monthly music quota reached.", "code": "quota_exceeded", "retry_after_seconds": 7 * 3600}
        provider, calls = self._provider(httpx.Response(429, json={"error": error}))
        with pytest.raises(MusicGenerationError, match="quota is exhausted") as raised:
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))
        assert not isinstance(raised.value, MusicProviderBusyError)
        assert "Monthly music quota reached. It resets in about 7 h." in str(raised.value)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_4xx_is_a_refusal(self):
        provider, _ = self._provider(self._error(400, code="invalid_request"))
        with pytest.raises(MusicGenerationError) as raised:
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))
        assert not isinstance(raised.value, MusicProviderBusyError)

    @pytest.mark.asyncio
    async def test_the_error_kind_may_be_in_type_instead_of_code(self):
        answer = httpx.Response(502, json={"error": {"message": "slow", "type": "provider_error", "retryable": True}})
        provider, _ = self._provider(answer)
        with pytest.raises(MusicProviderBusyError):
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))

    @pytest.mark.asyncio
    async def test_a_non_audio_content_type_is_read_as_mpeg(self):
        provider, _ = self._provider(httpx.Response(200, content=b"x", headers={"content-type": "application/octet-stream"}))
        assert (await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))).mime == "audio/mpeg"

    @pytest.mark.asyncio
    async def test_the_wait_connects_fast_and_outlasts_the_gateways_own(self):
        provider, calls = self._provider(httpx.Response(200, content=b"x"))
        await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))
        timeout = calls[0].extensions["timeout"]
        assert timeout["connect"] <= 10
        assert timeout["read"] > 600

    @pytest.mark.asyncio
    async def test_a_listing_that_fails_sends_nothing_and_may_be_asked_again(self):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(500, text="down")

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        with pytest.raises(MusicProviderBusyError, match="model list"):
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))
        assert all(call.url.path != "/v1/audio/music" for call in calls)

    @pytest.mark.asyncio
    async def test_a_clip_to_continue_is_refused_before_anything_is_sent(self):
        provider, calls = self._provider(httpx.Response(200, content=b"x"))
        clip = MusicReference(audio=b"a", mime="audio/wav", seconds=5)
        with pytest.raises(MusicGenerationError, match="continue"):
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x", continue_from=clip))
        assert provider.continues_music("lyria-3.5") is False
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_call_that_timed_out_is_never_sent_again(self):
        provider, calls = self._provider(httpx.ReadTimeout("slow"))
        with pytest.raises(MusicGenerationError, match="not sent again") as raised:
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))
        assert not isinstance(raised.value, MusicProviderBusyError)
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_request_that_never_left_may_be_asked_again(self):
        provider, _ = self._provider(httpx.ConnectError("refused"))
        with pytest.raises(MusicProviderBusyError):
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))

    @pytest.mark.asyncio
    async def test_an_empty_answer_is_a_failure(self):
        provider, _ = self._provider(httpx.Response(200, content=b""))
        with pytest.raises(MusicGenerationError, match="no music"):
            await provider.generate_music(MusicRequest(model="lyria-3.5", prompt="x"))


class TestToolCap:
    """The tool cap follows the upstream serving the model: the gateway has none of its own (#11639)."""

    @pytest.mark.asyncio
    async def test_cap_follows_the_upstream_owner(self):
        provider = _listing_provider(
            {"id": "claude-opus-5-5", "owned_by": "anthropic", "type": "chat"},
            {"id": "gpt-6", "owned_by": "openai", "type": "chat"},
            {"id": "anthropic.claude-opus-5-5", "owned_by": "bedrock", "type": "chat"},
            {"id": "acme-chat", "type": "chat"},
            {"id": "llama4", "owned_by": "ollama", "type": "chat"},
        )
        assert await provider.max_tools_for("llama4") == 64
        assert await provider.max_tools_for("claude-opus-5-5") == 1024
        assert await provider.max_tools_for("gpt-6") == 128
        assert await provider.max_tools_for("anthropic.claude-opus-5-5") == 256
        assert await provider.max_tools_for("acme-chat") == 128
        assert await provider.max_tools_for("unlisted") == 128

    @pytest.mark.asyncio
    async def test_a_failed_listing_keeps_the_default_cap_and_is_not_retried(self):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(500, json={"error": {"message": "down"}})

        set_llm_caller(USER_ID, EMAIL)
        provider = _mock_provider(handler)
        assert await provider.max_tools_for("claude-opus-5-5") == 128
        assert await provider.max_tools_for("claude-opus-5-5") == 128
        # One request: no SDK retry, and the failure is remembered for a while.
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_pinned_target_sets_the_cap_without_a_listing(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("the pinned target needs no listing")

        provider = FiligranGatewayProvider(
            api_key=f"{GATEWAY_URL}||anthropic",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        assert await provider.max_tools_for("house-alias") == 1024

    @pytest.mark.asyncio
    async def test_a_pinned_target_is_read_in_any_case(self):
        provider = FiligranGatewayProvider(api_key=f"{GATEWAY_URL}||Anthropic")
        assert await provider.max_tools_for("house-alias") == 1024
