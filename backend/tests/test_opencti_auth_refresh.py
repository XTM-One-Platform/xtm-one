"""OpenCTI: re-mint the short-lived cross-platform JWT mid-run.

A platform-managed OpenCTI integration authenticates with a cross-platform
EdDSA JWT whose TTL is 10 minutes, minted ONCE at tool-build time. Every run
longer than that - i.e. every document-import or harvesting run - used to die on
``search_opencti: GraphQL error: You must be logged in to do this.`` and stay
dead, because the provider ignored the ``refresh_fn`` the dispatcher already
passes it and every handler opened its own bare ``httpx.AsyncClient``.

The fix has three parts, one test group each:

* ``build_tools`` binds ``refresh_fn`` onto ``RefreshableHeaders``, and
  ``_client`` turns that into an ``AutoRefreshClient`` - so the reactive
  401/403 backstop and the proactive pre-expiry re-mint both apply.
* ``_graphql`` / ``_graphql_upload`` additionally treat an auth-shaped
  *GraphQL* error as a refresh trigger: OpenCTI reports an expired token as
  ``AUTH_REQUIRED`` inside a ``200 OK`` body, which ``AutoRefreshClient``
  cannot see.
* every call site goes through the one ``_client`` factory, so the next tool
  added cannot regress to a pinned token.

The tests exercise the real ``AutoRefreshClient`` / ``safe_request`` stack with
the inner ``httpx`` transport patched, so no socket is opened.
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
    RefreshableHeaders,
)
from app.integrations.providers.opencti import build_tools
from app.integrations.providers.opencti._common import (
    _client,
    _graphql,
    _graphql_upload,
    _is_auth_error,
)
from app.tools.base import CallableToolExecutor

BASE_URL = "https://demo.opencti.io"

#: The literal payload OpenCTI returns for an expired/absent token: HTTP 200
#: with the failure in the GraphQL ``errors`` array, never a 401.
AUTH_REQUIRED_BODY = {
    "errors": [
        {
            "message": "You must be logged in to do this.",
            "extensions": {"code": "AUTH_REQUIRED"},
        }
    ]
}


def _jwt(seconds_from_now: float) -> str:
    """A signature-less JWT whose ``exp`` is *seconds_from_now* from now.

    ``bearer_seconds_remaining`` only peeks the ``exp`` claim (it never verifies
    the signature), so a real key is unnecessary; the ``sig`` segment is a
    placeholder to keep the three-part shape.
    """

    def _seg(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    header = _seg({"alg": "none", "typ": "JWT"})
    payload = _seg({"exp": int(time.time() + seconds_from_now)})
    return f"{header}.{payload}.sig"


def _headers(token: str, refresh_fn: Any = None) -> dict[str, str]:
    return RefreshableHeaders(
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        refresh_fn=refresh_fn,
    )


def _json_resp(url: str, body: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, json=body, request=httpx.Request("POST", url)
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
# _is_auth_error — the GraphQL-level trigger
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "errors",
    [
        pytest.param(AUTH_REQUIRED_BODY["errors"], id="message+extensions"),
        pytest.param(
            [{"message": "You must be logged in to do this."}], id="message-only"
        ),
        pytest.param(
            [{"message": "boom", "extensions": {"code": "AUTH_REQUIRED"}}],
            id="extensions-code-only",
        ),
        pytest.param(
            [{"message": "unrelated"}, {"extensions": {"code": "AUTH_REQUIRED"}}],
            id="second-of-several",
        ),
    ],
)
def test_is_auth_error_recognises_opencti_auth_failures(errors: list) -> None:
    assert _is_auth_error(errors) is True


@pytest.mark.parametrize(
    "errors",
    [
        pytest.param(
            [
                {
                    "message": "You are not allowed to do this.",
                    "extensions": {"code": "FORBIDDEN_ACCESS"},
                }
            ],
            id="forbidden-access-is-not-an-auth-error",
        ),
        pytest.param(
            [{"message": "Cannot read property 'id' of undefined"}], id="server-error"
        ),
        pytest.param([{"message": ""}], id="empty-message"),
        pytest.param(["a bare string"], id="non-dict-entry"),
        pytest.param([], id="empty-list"),
        pytest.param(None, id="missing"),
    ],
)
def test_is_auth_error_ignores_everything_else(errors: Any) -> None:
    """A capability denial must NOT trigger a re-mint.

    Re-minting a token for the same identity cannot grant a missing capability,
    so a retry would only repeat the call — and repeat any side effect it had.
    """
    assert _is_auth_error(errors) is False


# ---------------------------------------------------------------------------
# _client — the single factory
# ---------------------------------------------------------------------------


def test_client_upgrades_refreshable_headers() -> None:
    """Headers carrying a re-minter get the auto-refreshing client."""
    assert isinstance(_client(_headers(_jwt(600), _Remint())), AutoRefreshClient)


def test_client_keeps_plain_client_for_a_static_api_key() -> None:
    """A static-API-key integration has no callback and is left untouched."""
    assert isinstance(_client({"Authorization": "Bearer static-key"}), httpx.AsyncClient)
    # A RefreshableHeaders with no callback behaves like the plain dict it replaces.
    assert isinstance(_client(_headers("static-key")), httpx.AsyncClient)


def test_no_call_site_bypasses_the_client_factory() -> None:
    """Regression guard for the ~100 ad-hoc clients this change funnelled.

    ``_common._client`` is the only place allowed to construct an
    ``httpx.AsyncClient`` in this provider. Without this test the next tool
    added would silently reintroduce a client pinned to the build-time JWT.
    """
    provider_dir = Path(_graphql.__module__.replace(".", "/")).parent
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in sorted((root / provider_dir).glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"httpx\.AsyncClient\(", line) and path.name != "_common.py":
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], (
        "construct clients via _common._client(headers, ...) so the JWT can be "
        "re-minted mid-run:\n" + "\n".join(offenders)
    )


# ---------------------------------------------------------------------------
# _graphql — the GraphQL-level AUTH_REQUIRED path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_recovers_when_a_later_call_returns_auth_required() -> None:
    """The reported failure: call 1 succeeds, call 2 gets AUTH_REQUIRED.

    Mirrors a real run — two separate tool calls sharing one headers dict, each
    opening its own client — and pins the whole contract: exactly one re-mint,
    one retry, a successful second call, and a headers dict left holding the new
    token so every *later* call is fine too.
    """
    stale, fresh = _jwt(600), _jwt(1800)
    remints = {"n": 0}

    async def _remint_fresh() -> str:
        remints["n"] += 1
        return fresh

    headers = _headers(stale, _remint_fresh)
    seen_auth: list[str | None] = []

    async def _fake_request(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        auth = (kwargs.get("headers") or {}).get("Authorization")
        seen_auth.append(auth)
        if auth == f"Bearer {stale}" and len(seen_auth) > 1:
            return _json_resp(url, AUTH_REQUIRED_BODY)
        return _json_resp(url, {"data": {"stixCoreObjects": {"edges": []}}})

    with patch.object(httpx.AsyncClient, "request", _fake_request):
        async with _client(headers) as c:
            data, err = await _graphql(c, BASE_URL, headers, "query A { me { id } }")
        assert err is None and data == {"stixCoreObjects": {"edges": []}}
        assert remints["n"] == 0, "a healthy first call must not re-mint"

        async with _client(headers) as c:
            data, err = await _graphql(c, BASE_URL, headers, "query B { me { id } }")

    assert err is None, f"the retry should have succeeded, got: {err}"
    assert data == {"stixCoreObjects": {"edges": []}}
    assert remints["n"] == 1, "exactly one re-mint"
    assert seen_auth == [
        f"Bearer {stale}",  # call 1 — fine
        f"Bearer {stale}",  # call 2 — AUTH_REQUIRED
        f"Bearer {fresh}",  # call 2, retried on the new token
    ]
    assert headers["Authorization"] == f"Bearer {fresh}", (
        "the shared headers must carry the new token so later calls recover too"
    )


@pytest.mark.asyncio
async def test_auth_required_retries_exactly_once() -> None:
    """A token that stays rejected surfaces the error instead of looping."""
    remint = _Remint()
    headers = _headers(_jwt(600), remint)
    calls = {"n": 0}

    async def _always_auth_required(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        calls["n"] += 1
        return _json_resp(url, AUTH_REQUIRED_BODY)

    with patch.object(httpx.AsyncClient, "request", _always_auth_required):
        async with _client(headers) as c:
            data, err = await _graphql(c, BASE_URL, headers, "query { me { id } }")

    assert data == {}
    assert err is not None and "You must be logged in to do this." in err
    assert remint.calls == 1, "one re-mint, not a re-mint per attempt"
    assert calls["n"] == 2, "the original call plus exactly one retry"


@pytest.mark.asyncio
async def test_auth_required_is_surfaced_for_a_static_api_key() -> None:
    """With no re-minter there is nothing to retry with — report, don't retry."""
    headers = _headers("static-api-key")
    calls = {"n": 0}

    async def _auth_required(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        calls["n"] += 1
        return _json_resp(url, AUTH_REQUIRED_BODY)

    with patch.object(httpx.AsyncClient, "request", _auth_required):
        async with _client(headers) as c:
            _, err = await _graphql(c, BASE_URL, headers, "query { me { id } }")

    assert err is not None and "You must be logged in to do this." in err
    assert calls["n"] == 1, "no callback, so no retry"


@pytest.mark.asyncio
async def test_forbidden_access_is_not_retried() -> None:
    """A capability denial is reported as-is: a retry would repeat side effects."""
    remint = _Remint()
    headers = _headers(_jwt(600), remint)
    calls = {"n": 0}

    async def _forbidden(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        calls["n"] += 1
        return _json_resp(
            url,
            {
                "errors": [
                    {
                        "message": "You are not allowed to do this.",
                        "extensions": {"code": "FORBIDDEN_ACCESS"},
                    }
                ]
            },
        )

    with patch.object(httpx.AsyncClient, "request", _forbidden):
        async with _client(headers) as c:
            _, err = await _graphql(c, BASE_URL, headers, "mutation { doThing }")

    assert err is not None and "not allowed" in err
    assert remint.calls == 0
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_http_401_still_refreshes_and_retries() -> None:
    """The AutoRefreshClient backstop keeps working for a real HTTP 401.

    OpenCTI answers an expired *token* with a 200 + AUTH_REQUIRED, but a proxy
    or the gateway in front of it can still 401, so both paths must recover.
    """
    remint = _Remint()
    headers = _headers(_jwt(600), remint)
    calls = {"n": 0}

    async def _401_then_ok(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, request=httpx.Request("POST", url))
        return _json_resp(url, {"data": {"me": {"id": "u1"}}})

    with patch.object(httpx.AsyncClient, "request", _401_then_ok):
        async with _client(headers) as c:
            data, err = await _graphql(c, BASE_URL, headers, "query { me { id } }")

    assert err is None and data == {"me": {"id": "u1"}}
    assert remint.calls == 1
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Proactive pre-expiry re-mint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_near_expiry_token_is_reminted_before_sending() -> None:
    """The common case: no rejection round-trip at all.

    Recovering reactively still means one failed call per expiry window — and
    the orchestrator fires callbacks with ``asyncio.gather``, so that is a burst.
    Swapping the token out first means the peer only ever sees a valid one.
    """
    remint = _Remint()
    headers = _headers(_jwt(JWT_PROACTIVE_REFRESH_SKEW_SECONDS - 10), remint)
    seen_auth: list[str | None] = []

    async def _ok(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        seen_auth.append((kwargs.get("headers") or {}).get("Authorization"))
        return _json_resp(url, {"data": {"me": {"id": "u1"}}})

    with patch.object(httpx.AsyncClient, "request", _ok):
        async with _client(headers) as c:
            _, err = await _graphql(c, BASE_URL, headers, "query { me { id } }")

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
        return _json_resp(url, {"data": {"me": {"id": "u1"}}})

    with patch.object(httpx.AsyncClient, "request", _ok):
        async with _client(headers) as c:
            _, err = await _graphql(c, BASE_URL, headers, "query { me { id } }")

    assert err is None
    assert remint.calls == 0


# ---------------------------------------------------------------------------
# _graphql_upload — a document import is exactly the long run that expires
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_recovers_from_auth_required() -> None:
    stale, fresh = _jwt(600), _jwt(1800)

    async def _remint_fresh() -> str:
        return fresh

    headers = _headers(stale, _remint_fresh)
    seen_auth: list[str | None] = []

    async def _auth_required_then_ok(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        auth = (kwargs.get("headers") or {}).get("Authorization")
        seen_auth.append(auth)
        if auth == f"Bearer {stale}":
            return _json_resp(url, AUTH_REQUIRED_BODY)
        return _json_resp(url, {"data": {"stixCoreObjectEdit": {"importPush": {}}}})

    with patch.object(httpx.AsyncClient, "request", _auth_required_then_ok):
        async with _client(headers, timeout=120) as c:
            data, err = await _graphql_upload(
                c,
                BASE_URL,
                headers,
                "mutation Upload($file: Upload!) { importPush(file: $file) { id } }",
                {"id": "report--1"},
                "file",
                "report.pdf",
                b"%PDF-1.4",
                "application/pdf",
            )

    assert err is None, f"the retry should have succeeded, got: {err}"
    assert data == {"stixCoreObjectEdit": {"importPush": {}}}
    assert seen_auth == [f"Bearer {stale}", f"Bearer {fresh}"]
    # The Content-Type header must stay stripped on the retry too — httpx sets
    # the multipart boundary itself, and a leftover application/json breaks it.
    assert headers["Content-Type"] == "application/json"


# ---------------------------------------------------------------------------
# build_tools wiring — end to end through a registered tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_tools_binds_refresh_fn_so_search_opencti_recovers() -> None:
    """``search_opencti`` — the tool from the report — recovers mid-run.

    Drives the real registered handler, so this fails if ``build_tools`` ever
    stops threading ``refresh_fn`` onto the shared headers.
    """
    stale, fresh = _jwt(600), _jwt(1800)
    remints = {"n": 0}

    async def _remint() -> str:
        remints["n"] += 1
        return fresh

    executor = CallableToolExecutor()
    tools = build_tools(f"{BASE_URL}||{stale}", executor, refresh_fn=_remint)
    assert any(t.name == "search_opencti" for t in tools)

    seen_auth: list[str | None] = []

    async def _auth_required_then_ok(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        auth = (kwargs.get("headers") or {}).get("Authorization")
        seen_auth.append(auth)
        if auth == f"Bearer {stale}":
            return _json_resp(url, AUTH_REQUIRED_BODY)
        return _json_resp(
            url,
            {
                "data": {
                    "stixCoreObjects": {
                        "edges": [
                            {
                                "node": {
                                    "id": "malware--1",
                                    "entity_type": "Malware",
                                    "name": "Emotet",
                                }
                            }
                        ],
                        "pageInfo": {"globalCount": 1},
                    }
                }
            },
        )

    handler = executor._handlers["search_opencti"]
    with patch.object(httpx.AsyncClient, "request", _auth_required_then_ok):
        result = await handler(search="Emotet")

    assert "Emotet" in str(result), f"expected the retried search to return rows: {result}"
    assert remints["n"] == 1
    assert seen_auth == [f"Bearer {stale}", f"Bearer {fresh}"]
