"""OpenCRQ: re-mint the short-lived per-user JWT mid-run.

A platform-managed OpenCRQ integration authenticates with the per-user Ed25519
JWT XTM One mints for it, whose TTL is ten minutes (``_XTM_JWT_TTL_MINUTES``),
minted ONCE at tool-build time. Any run longer than that — an assessment sweep,
a CRQ simulation — used to fail auth on every call from the expiry onwards and
never recover, because the provider ignored the ``refresh_fn`` the dispatcher
already passes to ``build_tools`` and every handler opened its own bare
``httpx.AsyncClient``. This is the same defect fixed for OpenCTI, and
it reuses that fix's machinery rather than repeating it.

Unlike OpenCTI's GraphQL — which answers an expired token with ``AUTH_REQUIRED``
inside a ``200 OK`` body — tRPC returns a real HTTP 401, so the reactive half is
just ``AutoRefreshClient``'s existing 401/403 retry and no body-level trigger is
needed. ``test_trpc_error_envelope_on_200_is_not_mistaken_for_success`` pins the
assumption that a rejected token arrives as a status code, so this file fails
loudly if OpenCRQ ever starts reporting auth failures in a 2xx body instead.

The tests drive the real ``AutoRefreshClient`` / ``safe_request`` / ``_trpc``
stack with the inner ``httpx`` transport patched, so no socket is opened.
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.http import (
    JWT_PROACTIVE_REFRESH_SKEW_SECONDS,
    AutoRefreshClient,
)
from app.integrations.providers.opencrq import build_tools
from app.integrations.providers.opencrq._common import (
    _client,
    _headers,
    _resolve_tenant,
    _trpc,
)
from app.tools.base import CallableToolExecutor

BASE_URL = "https://demo.opencrq.io"
TENANT = "11111111-1111-1111-1111-111111111111"


def _jwt(seconds_from_now: float) -> str:
    """A signature-less JWT whose ``exp`` is *seconds_from_now* from now.

    ``bearer_seconds_remaining`` only peeks the ``exp`` claim (it never verifies
    the signature), so a real key is unnecessary; the ``sig`` segment is a
    placeholder to keep the three-part shape.
    """

    def _seg(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    header = _seg({"alg": "EdDSA", "typ": "JWT"})
    payload = _seg({"exp": int(time.time() + seconds_from_now)})
    return f"{header}.{payload}.sig"


def _trpc_ok(url: str, data: Any) -> httpx.Response:
    """The tRPC success envelope: ``{"result": {"data": ...}}``."""
    return httpx.Response(
        200, json={"result": {"data": data}}, request=httpx.Request("GET", url)
    )


def _unauthorized(url: str) -> httpx.Response:
    """How tRPC reports a rejected/expired bearer token: HTTP 401 + envelope."""
    return httpx.Response(
        401,
        json={
            "error": {
                "message": "UNAUTHORIZED",
                "code": -32001,
                "data": {"code": "UNAUTHORIZED", "httpStatus": 401},
            }
        },
        request=httpx.Request("GET", url),
    )


class _Remint:
    """A JWT re-minter that counts how many tokens it handed out."""

    def __init__(self, ttl: float = 600) -> None:
        self.calls = 0
        self._ttl = ttl

    async def __call__(self) -> str:
        self.calls += 1
        return _jwt(self._ttl)


# ---------------------------------------------------------------------------
# _client — the single factory
# ---------------------------------------------------------------------------


def test_client_upgrades_headers_carrying_a_reminter() -> None:
    assert isinstance(_client(_headers(_jwt(600), _Remint())), AutoRefreshClient)


def test_client_keeps_plain_client_for_a_static_api_key() -> None:
    """A static-API-key integration has no callback and is left untouched."""
    assert isinstance(_client(_headers("static-api-key")), httpx.AsyncClient)
    assert isinstance(_client({"Authorization": "Bearer k"}), httpx.AsyncClient)


def test_no_call_site_bypasses_the_client_factory() -> None:
    """Regression guard for the 15 ad-hoc clients this change funnelled.

    ``_common._client`` is the only place allowed to construct an
    ``httpx.AsyncClient`` in this provider. Without this test the next tool
    added would silently reintroduce a client pinned to the build-time JWT.
    """
    provider_dir = Path(_trpc.__module__.replace(".", "/")).parent
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted((root / provider_dir).glob("*.py")):
        if path.name == "_common.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"httpx\.AsyncClient\(", line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "construct clients via _common._client(headers, ...) so the JWT can be "
        "re-minted mid-run:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# Reactive recovery — tRPC's HTTP 401
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_recovers_when_a_later_call_is_unauthorized() -> None:
    """The reported failure shape: call 1 succeeds, call 2 gets a 401.

    Mirrors a real run — two separate tool calls sharing one headers dict, each
    opening its own client — and pins the whole contract: one re-mint, one
    retry, a successful second call, and a headers dict left holding the new
    token so every *later* call is fine too.
    """
    stale, fresh = _jwt(600), _jwt(1800)
    remints = {"n": 0}

    async def _remint() -> str:
        remints["n"] += 1
        return fresh

    headers = _headers(stale, _remint)
    seen_auth: list[str | None] = []

    async def _fake_request(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        auth = (kwargs.get("headers") or {}).get("Authorization")
        seen_auth.append(auth)
        if auth == f"Bearer {stale}" and len(seen_auth) > 1:
            return _unauthorized(url)
        return _trpc_ok(url, [{"id": "c1", "name": "AC-1"}])

    with patch.object(httpx.AsyncClient, "request", _fake_request):
        async with _client(headers) as c:
            data, err = await _trpc(
                BASE_URL, headers, "controls.list", "list controls",
                tenant_id=TENANT, client=c,
            )
        assert err is None and data == [{"id": "c1", "name": "AC-1"}]
        assert remints["n"] == 0, "a healthy first call must not re-mint"

        async with _client(headers) as c:
            data, err = await _trpc(
                BASE_URL, headers, "controls.list", "list controls",
                tenant_id=TENANT, client=c,
            )

    assert err is None, f"the retry should have succeeded, got: {err}"
    assert data == [{"id": "c1", "name": "AC-1"}]
    assert remints["n"] == 1, "exactly one re-mint"
    assert seen_auth == [
        f"Bearer {stale}",  # call 1 — fine
        f"Bearer {stale}",  # call 2 — 401
        f"Bearer {fresh}",  # call 2, retried on the new token
    ]
    assert headers["Authorization"] == f"Bearer {fresh}", (
        "the shared headers must carry the new token so later calls recover too"
    )


@pytest.mark.asyncio
async def test_the_retry_keeps_the_tenant_scope_header() -> None:
    """``x-tenant-id`` must survive the re-mint.

    ``_trpc`` copies the shared headers and adds the tenant scope; the retry
    patches only ``Authorization`` into that copy. Losing the tenant header
    would turn a recovered call into a wrong-scope (or empty) answer, which is
    worse than the auth failure it replaced.
    """
    remint = _Remint()
    headers = _headers(_jwt(600), remint)
    seen: list[dict[str, str]] = []

    async def _401_then_ok(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        sent = dict(kwargs.get("headers") or {})
        seen.append(sent)
        return _unauthorized(url) if len(seen) == 1 else _trpc_ok(url, [])

    with patch.object(httpx.AsyncClient, "request", _401_then_ok):
        async with _client(headers) as c:
            _, err = await _trpc(
                BASE_URL, headers, "controls.list", "list controls",
                tenant_id=TENANT, client=c,
            )

    assert err is None
    assert remint.calls == 1
    assert [h.get("x-tenant-id") for h in seen] == [TENANT, TENANT]


@pytest.mark.asyncio
async def test_unauthorized_is_surfaced_for_a_static_api_key() -> None:
    """With no re-minter there is nothing to retry with — report, don't retry."""
    headers = _headers("static-api-key")
    calls = {"n": 0}

    async def _always_401(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        calls["n"] += 1
        return _unauthorized(url)

    with patch.object(httpx.AsyncClient, "request", _always_401):
        async with _client(headers) as c:
            data, err = await _trpc(
                BASE_URL, headers, "controls.list", "list controls", client=c
            )

    assert data is None
    assert err is not None
    assert calls["n"] == 1, "no callback, so no retry"


@pytest.mark.asyncio
async def test_unauthorized_retries_exactly_once() -> None:
    """A token that stays rejected surfaces the error instead of looping."""
    remint = _Remint()
    headers = _headers(_jwt(600), remint)
    calls = {"n": 0}

    async def _always_401(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        calls["n"] += 1
        return _unauthorized(url)

    with patch.object(httpx.AsyncClient, "request", _always_401):
        async with _client(headers) as c:
            _, err = await _trpc(
                BASE_URL, headers, "controls.list", "list controls", client=c
            )

    assert err is not None
    assert remint.calls == 1, "one re-mint, not a re-mint per attempt"
    assert calls["n"] == 2, "the original call plus exactly one retry"


@pytest.mark.asyncio
async def test_trpc_error_envelope_on_200_is_not_mistaken_for_success() -> None:
    """Pins the assumption that lets this provider skip a body-level trigger.

    tRPC reports a rejected token as a real HTTP 401, so ``AutoRefreshClient``
    sees it and no 200-with-auth-error detection is needed (unlike OpenCTI's
    GraphQL). Should OpenCRQ ever start answering 200 with an ``UNAUTHORIZED``
    envelope, ``_trpc`` still refuses to treat it as data — this test keeps that
    true, and its docstring is the pointer to add a body-level re-mint trigger.
    """
    headers = _headers(_jwt(600), _Remint())

    async def _200_with_error(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        return httpx.Response(
            200,
            json={"error": {"message": "UNAUTHORIZED", "data": {"code": "UNAUTHORIZED"}}},
            request=httpx.Request("GET", url),
        )

    with patch.object(httpx.AsyncClient, "request", _200_with_error):
        async with _client(headers) as c:
            data, err = await _trpc(
                BASE_URL, headers, "controls.list", "list controls", client=c
            )

    assert data is None
    assert err is not None and "UNAUTHORIZED" in err


# ---------------------------------------------------------------------------
# Proactive pre-expiry re-mint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_near_expiry_token_is_reminted_before_sending() -> None:
    """The common case: no rejection round-trip at all.

    Recovering reactively still costs one failed call per expiry window, and a
    multi-call handler shares ONE client whose reactive budget is a single
    retry — so the proactive path is what keeps a long run healthy.
    """
    remint = _Remint()
    headers = _headers(_jwt(JWT_PROACTIVE_REFRESH_SKEW_SECONDS - 10), remint)
    seen_auth: list[str | None] = []

    async def _ok(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        seen_auth.append((kwargs.get("headers") or {}).get("Authorization"))
        return _trpc_ok(url, [])

    with patch.object(httpx.AsyncClient, "request", _ok):
        async with _client(headers) as c:
            _, err = await _trpc(
                BASE_URL, headers, "controls.list", "list controls", client=c
            )

    assert err is None
    assert remint.calls == 1, "one proactive re-mint before sending"
    assert len(seen_auth) == 1, "no rejection round-trip needed"
    assert seen_auth == [headers["Authorization"]], (
        "the only physical request must already carry the freshly minted token"
    )


@pytest.mark.asyncio
async def test_healthy_token_is_not_reminted() -> None:
    remint = _Remint()
    headers = _headers(_jwt(JWT_PROACTIVE_REFRESH_SKEW_SECONDS + 300), remint)

    async def _ok(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        return _trpc_ok(url, [])

    with patch.object(httpx.AsyncClient, "request", _ok):
        async with _client(headers) as c:
            _, err = await _trpc(
                BASE_URL, headers, "controls.list", "list controls", client=c
            )

    assert err is None
    assert remint.calls == 0


@pytest.mark.asyncio
async def test_a_multi_call_handler_refreshes_once_for_the_whole_burst() -> None:
    """Several ``_trpc`` calls inside one expiry window mint ONE token.

    A multi-call handler (tenant resolution + detail + related lists) shares one
    client and one headers dict. The proactive refresh is coalesced, so the
    burst costs a single re-mint rather than one per call.
    """
    remint = _Remint()
    headers = _headers(_jwt(JWT_PROACTIVE_REFRESH_SKEW_SECONDS - 10), remint)
    calls = {"n": 0}

    async def _ok(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        calls["n"] += 1
        return _trpc_ok(url, [])

    with patch.object(httpx.AsyncClient, "request", _ok):
        async with _client(headers) as c:
            for _ in range(4):
                _, err = await _trpc(
                    BASE_URL, headers, "controls.list", "list controls", client=c
                )
                assert err is None

    assert calls["n"] == 4
    assert remint.calls == 1, "one re-mint for the whole expiry window"


@pytest.mark.asyncio
async def test_trpc_opens_a_refreshing_client_when_none_is_injected() -> None:
    """A single-call handler passes no client — that path must refresh too."""
    remint = _Remint()
    headers = _headers(_jwt(600), remint)
    calls = {"n": 0}

    async def _401_then_ok(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        calls["n"] += 1
        return _unauthorized(url) if calls["n"] == 1 else _trpc_ok(url, [])

    with patch.object(httpx.AsyncClient, "request", _401_then_ok):
        # No ``client=`` — _trpc opens its own through the factory.
        _, err = await _trpc(BASE_URL, headers, "controls.list", "list controls")

    assert err is None, f"the internally-opened client must refresh too: {err}"
    assert remint.calls == 1
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_resolve_tenant_recovers_too() -> None:
    """Tenant resolution is the FIRST call of most handlers.

    If it cannot recover, the whole handler dies before reaching its real work,
    so it must go through the same refreshing path.
    """
    remint = _Remint()
    headers = _headers(_jwt(600), remint)
    calls = {"n": 0}

    async def _401_then_tenants(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _unauthorized(url)
        return _trpc_ok(url, [{"id": TENANT, "name": "Acme"}])

    with patch.object(httpx.AsyncClient, "request", _401_then_tenants):
        async with _client(headers) as c:
            tenant, err = await _resolve_tenant(BASE_URL, headers, "", client=c)

    assert err is None and tenant == TENANT
    assert remint.calls == 1


# ---------------------------------------------------------------------------
# build_tools wiring — end to end through a registered tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_tools_binds_refresh_fn_so_a_real_tool_recovers() -> None:
    """Drives a real registered handler through its own multi-call flow.

    Fails if ``build_tools`` ever stops threading ``refresh_fn`` onto the shared
    headers — the defect this change fixes.
    """
    stale, fresh = _jwt(600), _jwt(1800)
    remints = {"n": 0}

    async def _remint() -> str:
        remints["n"] += 1
        return fresh

    executor = CallableToolExecutor()
    tools = build_tools(f"{BASE_URL}||{stale}", executor, refresh_fn=_remint)
    assert any(t.name == "list_opencrq_frameworks" for t in tools)

    seen_auth: list[str | None] = []

    async def _unauthorized_then_ok(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        auth = (kwargs.get("headers") or {}).get("Authorization")
        seen_auth.append(auth)
        if auth == f"Bearer {stale}":
            return _unauthorized(url)
        if "getTenants" in url:
            return _trpc_ok(url, [{"id": TENANT, "name": "Acme"}])
        if "complianceAssessments.getFrameworks" in url:
            return _trpc_ok(
                url,
                [
                    {
                        "id": "fw-nist",
                        "key": "nist-csf-2.0",
                        "title": "NIST CSF 2.0",
                        "version": "1.2.0",
                        "oscalCatalogUuid": "720a010b-253c-4a94-bb65-cb58400966f5",
                    }
                ],
            )
        return _trpc_ok(
            url,
            [
                {
                    "id": "fw-iso",
                    "drataId": 41,
                    "slug": "iso27001-2022",
                    "tag": "ISO270012022",
                    "name": "ISO 27001:2022",
                    "description": None,
                    "isReady": False,
                    "numInScopeRequirements": 11,
                    "numReadyInScopeRequirements": 6,
                    "numInScopeControls": 9,
                    "lastReportedAt": "2026-10-14T12:56:13.932Z",
                    "derivedTotalRequirements": 12,
                    "derivedInScopeRequirements": 11,
                    "derivedReadyInScopeRequirements": 6,
                }
            ],
        )

    handler = executor._handlers["list_opencrq_frameworks"]
    with patch.object(httpx.AsyncClient, "request", _unauthorized_then_ok):
        result = await handler()

    assert "NIST CSF 2.0" in str(result) and "ISO 27001:2022" in str(result), (
        f"expected the retried calls to return rows: {result}"
    )
    assert remints["n"] == 1
    assert seen_auth[0] == f"Bearer {stale}"
    assert all(a == f"Bearer {fresh}" for a in seen_auth[1:]), (
        "every call after the re-mint must use the new token"
    )
