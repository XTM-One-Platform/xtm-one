"""Unit tests for provider validation helpers in ``app.api.v1.settings``.

These tests exercise the pure helpers that back the
``POST /api/v1/settings/llm-providers/validate`` endpoint:

  * :func:`_classify_target_host` / :func:`_is_public_target_host`
    – the SSRF guard.
  * :func:`_is_public_target_host_async` – async DNS resolution path.
  * :func:`_normalize_probe_base_url` – URL normalization + SSRF check.
  * :func:`_build_probe_candidates` – descending dim candidate generator.
  * :func:`_suggest_smaller_dimensions` – speed/quality dim suggestions.
  * ``ValidateProviderRequest`` – the capabilities a request may name, through
    the route with the provider stubbed.

Endpoint-level tests that talk to a live server live in
``test_settings_provider_validation_integration`` (kept out of unit CI
because the SSRF guard blocks loopback addresses before any HTTP client
is instantiated).
"""

from __future__ import annotations

import asyncio
import socket
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1.settings import (
    _build_probe_candidates,
    _check_resolved_ips,
    _classify_target_host,
    _is_public_target_host,
    _is_public_target_host_async,
    _normalize_probe_base_url,
    _probe_openai_compatible_connection,
    _suggest_smaller_dimensions,
)

# ---------------------------------------------------------------------------
# SSRF guard – synchronous host classification (no DNS)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostname,allowed,needs_dns",
    [
        ("", False, False),
        ("   ", False, False),
        ("localhost", False, False),
        ("LOCALHOST", False, False),
        ("localhost.localdomain", False, False),
        ("myhost.local", False, False),
        ("127.0.0.1", False, False),
        ("10.0.0.1", False, False),
        ("172.16.0.1", False, False),
        ("192.168.1.5", False, False),
        ("169.254.169.254", False, False),  # cloud metadata endpoint
        ("::1", False, False),
        ("fe80::1", False, False),
        ("8.8.8.8", True, False),
        ("2001:4860:4860::8888", True, False),
        ("api.openai.com", True, True),  # needs DNS
    ],
)
def test_classify_target_host_decisions(
    hostname: str, allowed: bool, needs_dns: bool
) -> None:
    result_allowed, _reason, result_needs_dns = _classify_target_host(hostname)
    assert result_allowed is allowed
    assert result_needs_dns is needs_dns


# ---------------------------------------------------------------------------
# SSRF guard – synchronous DNS path
# ---------------------------------------------------------------------------


def _addr_info(ips: list[str]) -> list[tuple]:
    """Build a fake ``getaddrinfo`` return value for the given IPs."""
    return [
        (socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 0)) for ip in ips
    ]


def test_is_public_target_host_resolves_public_ip() -> None:
    with patch("app.api.v1.settings.socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = _addr_info(["8.8.8.8"])
        allowed, reason = _is_public_target_host("api.example.com")
    assert allowed is True
    assert reason is None


def test_is_public_target_host_blocks_private_dns_resolution() -> None:
    with patch("app.api.v1.settings.socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = _addr_info(["192.168.1.10"])
        allowed, reason = _is_public_target_host("intranet.example.com")
    assert allowed is False
    assert reason is not None
    assert "private" in reason.lower() or "non-routable" in reason.lower()


def test_is_public_target_host_blocks_partial_split_horizon() -> None:
    """Any single private resolved IP must block the host (split-horizon)."""
    with patch("app.api.v1.settings.socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = _addr_info(["8.8.8.8", "10.0.0.1"])
        allowed, _reason = _is_public_target_host("api.example.com")
    assert allowed is False


def test_is_public_target_host_handles_dns_failure() -> None:
    with patch("app.api.v1.settings.socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.side_effect = OSError("nodename nor servname")
        allowed, reason = _is_public_target_host("not-a-real-host.invalid")
    assert allowed is False
    assert reason is not None
    assert "resolve" in reason.lower()


def test_is_public_target_host_handles_empty_resolution() -> None:
    with patch("app.api.v1.settings.socket.getaddrinfo") as mock_getaddrinfo:
        mock_getaddrinfo.return_value = []
        allowed, reason = _is_public_target_host("nodns.example.com")
    assert allowed is False
    assert reason is not None


def test_check_resolved_ips_rejects_invalid_literal() -> None:
    allowed, reason = _check_resolved_ips(
        [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                0,
                "",
                ("not-an-ip", 0),
            )
        ]
    )
    assert allowed is False
    assert reason is not None
    assert "invalid" in reason.lower()


# ---------------------------------------------------------------------------
# SSRF guard – async DNS path (must not block on getaddrinfo)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_public_target_host_async_uses_loop_resolver() -> None:
    """The async helper must dispatch DNS through the event loop."""

    async def fake_getaddrinfo(*_args, **_kwargs):
        return _addr_info(["8.8.8.8"])

    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo", side_effect=fake_getaddrinfo):
        allowed, reason = await _is_public_target_host_async(
            "api.example.com"
        )
    assert allowed is True
    assert reason is None


@pytest.mark.asyncio
async def test_is_public_target_host_async_blocks_private() -> None:
    async def fake_getaddrinfo(*_args, **_kwargs):
        return _addr_info(["172.16.0.1"])

    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo", side_effect=fake_getaddrinfo):
        allowed, reason = await _is_public_target_host_async(
            "intranet.example.com"
        )
    assert allowed is False
    assert reason is not None


@pytest.mark.asyncio
async def test_is_public_target_host_async_does_not_resolve_for_ip_literal() -> None:
    """IP literals must short-circuit before DNS even runs."""
    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo") as mock_getaddrinfo:
        allowed, _reason = await _is_public_target_host_async("8.8.8.8")
    assert allowed is True
    mock_getaddrinfo.assert_not_called()


@pytest.mark.asyncio
async def test_is_public_target_host_async_blocks_localhost_without_dns() -> None:
    """``localhost`` must be blocked before any DNS resolution."""
    loop = asyncio.get_running_loop()
    with patch.object(loop, "getaddrinfo") as mock_getaddrinfo:
        allowed, reason = await _is_public_target_host_async("localhost")
    assert allowed is False
    assert reason is not None
    mock_getaddrinfo.assert_not_called()


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normalize_probe_base_url_missing_base_url() -> None:
    base_url, reason = await _normalize_probe_base_url("key-only")
    assert base_url is None
    assert reason is not None
    assert "base url" in reason.lower()


@pytest.mark.asyncio
async def test_normalize_probe_base_url_rejects_bad_scheme() -> None:
    base_url, reason = await _normalize_probe_base_url(
        "key||ftp://evil.example.com"
    )
    assert base_url is None
    assert reason is not None
    assert "scheme" in reason.lower()


@pytest.mark.asyncio
async def test_normalize_probe_base_url_rejects_userinfo() -> None:
    base_url, reason = await _normalize_probe_base_url(
        "key||https://user:pass@example.com"
    )
    assert base_url is None
    assert reason is not None
    assert "userinfo" in reason.lower()


@pytest.mark.asyncio
async def test_normalize_probe_base_url_strips_fragment_and_query() -> None:
    with patch(
        "app.api.v1.settings._is_public_target_host_async",
        new=AsyncMock(return_value=(True, None)),
    ):
        base_url, reason = await _normalize_probe_base_url(
            "key||https://api.example.com/v1?token=x#frag/"
        )
    assert reason is None
    assert base_url == "https://api.example.com/v1"


@pytest.mark.asyncio
async def test_normalize_probe_base_url_propagates_ssrf_reason() -> None:
    with patch(
        "app.api.v1.settings._is_public_target_host_async",
        new=AsyncMock(return_value=(False, "Local/private hostnames blocked.")),
    ):
        base_url, reason = await _normalize_probe_base_url(
            "key||https://localhost:11434/v1"
        )
    assert base_url is None
    assert reason == "Local/private hostnames blocked."


# ---------------------------------------------------------------------------
# Probe candidate generation
# ---------------------------------------------------------------------------


def test_build_probe_candidates_descends_by_halving() -> None:
    candidates = _build_probe_candidates(max_probe=4096)
    assert candidates[0] == 4096
    # Must be strictly descending and unique.
    assert list(candidates) == sorted(candidates, reverse=True)
    assert len(candidates) == len(set(candidates))
    # Final value should respect min_probe lower bound.
    assert candidates[-1] >= 256


def test_build_probe_candidates_respects_floor() -> None:
    candidates = _build_probe_candidates(
        max_probe=200, min_probe=64, floor_probe=64
    )
    assert candidates[0] >= 64
    assert candidates[-1] >= 64


def test_build_probe_candidates_includes_lower_bound() -> None:
    candidates = _build_probe_candidates(max_probe=1536, min_probe=384)
    assert 384 in candidates


# ---------------------------------------------------------------------------
# Dimension suggestions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "detected,expected",
    [
        (0, []),
        (-5, []),
        (256, [256]),
        (1536, [1536, 1024, 512, 256]),
        (3072, [3072, 2048, 1024, 512, 256]),
    ],
)
def test_suggest_smaller_dimensions_matches_expected(
    detected: int, expected: list[int]
) -> None:
    assert _suggest_smaller_dimensions(detected) == expected


def test_suggest_smaller_dimensions_keeps_detected_first() -> None:
    result = _suggest_smaller_dimensions(1500)
    assert result[0] == 1500
    # Every subsequent entry must be strictly smaller.
    assert all(later < 1500 for later in result[1:])


def test_suggest_smaller_dimensions_respects_min_suggestion() -> None:
    result = _suggest_smaller_dimensions(1024, min_suggestion=512)
    assert all(d >= 512 for d in result)
    assert 256 not in result


@pytest.mark.asyncio
async def test_probe_connection_accepts_embed_route() -> None:
    class _Resp:
        def __init__(self, status_code: int):
            self.status_code = status_code

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            del exc_type, exc, tb
            return False

        async def get(self, url: str, headers: dict[str, str]):
            del headers
            if url.endswith("/models"):
                return _Resp(404)
            return _Resp(404)

        async def post(self, url: str, headers: dict[str, str], json: dict[str, str]):
            del headers, json
            if url.endswith("/embed"):
                return _Resp(422)
            return _Resp(404)

    with patch(
        "app.api.v1.settings._normalize_probe_base_url",
        new=AsyncMock(return_value=("https://embedder.example.com", None)),
    ), patch(
        "app.api.v1.settings.httpx.AsyncClient",
        return_value=_Client(),
    ):
        ok, message = await _probe_openai_compatible_connection(
            "none||https://embedder.example.com"
        )

    assert ok is True
    assert "/embed" in message


# ---------------------------------------------------------------------------
# Embedding-only (TEI) provider validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validate_tei_probes_regardless_of_capability() -> None:
    """``tei`` is embedding-only and exposes no model-discovery API, so its
    validation must fall back to the connectivity probe even when the request
    capability is ``reasoning`` (the generic "Add Provider" flow). Otherwise
    the provider can never be validated and therefore never saved.
    """
    from types import SimpleNamespace

    from app.api.v1.schemas.settings import ValidateProviderRequest
    from app.api.v1.settings import validate_provider_with_key

    body = ValidateProviderRequest(
        provider="tei",
        api_key="none||https://embedder.example.com",
        capability="reasoning",
    )
    with patch(
        "app.api.v1.settings._probe_openai_compatible_connection",
        new=AsyncMock(
            return_value=(
                True,
                "Endpoint is reachable and exposes embedding route "
                "https://embedder.example.com/embed (HTTP 422), even without "
                "model discovery.",
            )
        ),
    ) as mock_probe:
        resp = await validate_provider_with_key(
            body=body,
            user=SimpleNamespace(id="admin", email="admin@filigran.io"),
        )

    mock_probe.assert_awaited_once()
    assert resp.connection_ok is True
    assert resp.provider == "tei"
    # Reported as the embedding capability even though the request said reasoning.
    assert resp.capability == "embedding"
    assert resp.models_supported is False


# ---------------------------------------------------------------------------
# Request capabilities
# ---------------------------------------------------------------------------


def _single_capability_providers() -> list[tuple[str, str]]:
    from app.llm.router import PROVIDER_MAP

    return sorted(
        (name, cls.only_capability)
        for name, cls in PROVIDER_MAP.items()
        if getattr(cls, "only_capability", None)
    )


@pytest.mark.parametrize(
    "provider,capability",
    _single_capability_providers(),
    ids=[name for name, _capability in _single_capability_providers()],
)
def test_a_single_capability_provider_can_be_validated_for_its_capability(
    provider: str, capability: str
) -> None:
    """The Add Provider dialog validates such a provider for its own capability
    (``SINGLE_CAPABILITY_PROVIDERS``): a capability the request schema refuses
    makes the provider impossible to add."""
    from app.api.v1.schemas.settings import ValidateProviderRequest

    body = ValidateProviderRequest(provider=provider, api_key="key", capability=capability)
    assert body.capability == capability


def _validate_route(
    monkeypatch: pytest.MonkeyPatch, provider_cls: type, name: str = "elevenlabs"
) -> TestClient:
    from types import SimpleNamespace

    from app.api.deps import require_admin, require_ai_enabled
    from app.api.v1 import settings as settings_api

    async def _resolve(value: str) -> str:
        return value

    monkeypatch.setitem(settings_api.PROVIDER_MAP, name, provider_cls)
    monkeypatch.setattr(settings_api, "_ensure_new_provider_allowed", lambda _p: None)
    monkeypatch.setattr(settings_api, "resolve_secret_text", _resolve)
    app = FastAPI()
    app.include_router(settings_api.router, prefix="/api/v1/settings")
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(
        id="admin", email="admin@filigran.io", is_admin=True
    )
    app.dependency_overrides[require_ai_enabled] = lambda: None
    return TestClient(app)


def test_the_add_provider_dialogs_elevenlabs_request_reaches_its_speech_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The body the dialog posts for ElevenLabs, through the request schema."""

    class _Speaks:
        only_capability = "speech"

        def __init__(self, api_key: str):
            assert api_key == "sk_key||https://api.eu.residency.elevenlabs.io"

        async def list_speech_models(self) -> list[str]:
            return ["eleven_v3", "eleven_multilingual_v2"]

    response = _validate_route(monkeypatch, _Speaks).post(
        "/api/v1/settings/llm-providers/validate",
        json={
            "provider": "elevenlabs",
            "api_key": "sk_key||https://api.eu.residency.elevenlabs.io",
            "capability": "speech",
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connection_ok"] is True
    assert body["capability"] == "speech"
    assert body["models"] == ["eleven_v3", "eleven_multilingual_v2"]


def test_a_video_provider_is_validated_by_its_video_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kling AI and Seedance serve video alone: the dialog validates them for it."""

    class _Films:
        only_capability = "video"

        def __init__(self, api_key: str):
            pass

        async def list_video_models(self) -> list[str]:
            return ["kling-v3"]

    response = _validate_route(monkeypatch, _Films, "kling").post(
        "/api/v1/settings/llm-providers/validate",
        json={"provider": "kling", "api_key": "api-key-kling-x", "capability": "video"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["capability"] == "video" and response.json()["models"] == ["kling-v3"]


def test_a_refused_video_provider_key_is_said_as_the_provider_says_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.llm.video import VideoGenerationError

    class _Refuses:
        only_capability = "video"

        def __init__(self, api_key: str):
            pass

        async def list_video_models(self) -> list[str]:
            raise VideoGenerationError("Kling AI refused the API key.")

    response = _validate_route(monkeypatch, _Refuses, "kling").post(
        "/api/v1/settings/llm-providers/validate",
        json={"provider": "kling", "api_key": "api-key-kling-x", "capability": "video"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connection_ok"] is False and body["message"] == "Kling AI refused the API key."


def test_an_unknown_validation_capability_is_still_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _validate_route(monkeypatch, object).post(
        "/api/v1/settings/llm-providers/validate",
        json={"provider": "elevenlabs", "api_key": "sk_key", "capability": "telepathy"},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "capability"]


def _fails_discovery(error: str) -> type:
    class _Fails:
        def __init__(self, api_key: str):
            pass

        async def list_models_grouped(self):
            raise RuntimeError(error)

    return _Fails


def test_validation_says_why_model_discovery_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    response = _validate_route(monkeypatch, _fails_discovery("upstream said 418"), "openai").post(
        "/api/v1/settings/llm-providers/validate",
        json={"provider": "openai", "api_key": "sk-x", "capability": "reasoning"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connection_ok"] is False
    assert "upstream said 418" in body["message"]
    assert "jwks" not in body["message"]


def test_a_gateway_in_jwt_mode_is_told_to_reach_the_platform_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1 import settings as settings_api

    monkeypatch.setattr(settings_api, "xtm_issuer", lambda: "https://xtm.example.com")
    error = "Error code: 503 - {'error': {'type': 'gateway_overloaded'}}"
    response = _validate_route(monkeypatch, _fails_discovery(error), "filigran_gateway").post(
        "/api/v1/settings/llm-providers/validate",
        json={"provider": "filigran_gateway", "api_key": "https://gw.example.com", "capability": "reasoning"},
    )

    message = response.json()["message"]
    assert "gateway_overloaded" in message
    assert "https://xtm.example.com/xtm/auth/jwks" in message
    assert "gateway token" not in message


def test_a_gateway_with_a_token_has_it_redacted_and_no_jwks_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    token = "secret-gateway-token-123"
    response = _validate_route(monkeypatch, _fails_discovery(f"401 bad token {token}"), "filigran_gateway").post(
        "/api/v1/settings/llm-providers/validate",
        json={"provider": "filigran_gateway", "api_key": f"https://gw.example.com||||{token}", "capability": "reasoning"},
    )

    message = response.json()["message"]
    assert token not in message and "401 bad token ***" in message
    assert "jwks" not in message


def test_a_secret_a_vault_reference_resolved_to_is_never_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1 import settings as settings_api

    client = _validate_route(monkeypatch, _fails_discovery("bad key sk-vault-secret-1234"), "openai")

    async def _resolve(value: str) -> str:
        return "sk-vault-secret-1234"

    monkeypatch.setattr(settings_api, "resolve_secret_text", _resolve)
    response = client.post(
        "/api/v1/settings/llm-providers/validate",
        json={"provider": "openai", "api_key": "a-reference", "capability": "reasoning"},
    )

    message = response.json()["message"]
    assert "sk-vault-secret-1234" not in message and "bad key ***" in message


def test_a_failed_probe_still_says_why_discovery_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1 import settings as settings_api

    class _Fails:
        def __init__(self, api_key: str):
            pass

        async def list_embedding_models(self) -> list[str]:
            raise RuntimeError("models route answered 503")

    client = _validate_route(monkeypatch, _Fails, "litellm")
    monkeypatch.setattr(
        settings_api, "_probe_openai_compatible_connection", AsyncMock(return_value=(False, "No route found."))
    )
    response = client.post(
        "/api/v1/settings/llm-providers/validate",
        json={"provider": "litellm", "api_key": "k||https://llm.example.com", "capability": "embedding"},
    )

    message = response.json()["message"]
    assert "No route found." in message and "models route answered 503" in message


def test_resolve_models_hints_the_gateway_jwks_without_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1 import settings as settings_api

    monkeypatch.setattr(settings_api, "xtm_issuer", lambda: "")
    response = _validate_route(monkeypatch, _fails_discovery("503 gateway_overloaded"), "filigran_gateway").post(
        "/api/v1/settings/llm-providers/resolve-models",
        json={"provider": "filigran_gateway", "api_key": "https://gw.example.com"},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "503 gateway_overloaded" in detail and "its /xtm/auth/jwks endpoint" in detail
