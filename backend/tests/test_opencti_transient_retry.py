"""OpenCTI: a read retries a transient 502/503/504, a mutation never does.

A proxy in front of OpenCTI answering 502 (bad gateway), 503 or 504 is
usually healthy again within seconds. ``_graphql`` used to send every
document without ``retry_statuses``, so one such answer failed a read such
as ``get_opencti_platform_version`` at once and an agent concluded the
platform was down. A read now opts into ``TRANSIENT_RETRY_STATUSES``; a
mutation is never replayed, since the proxy error may hide a write that was
applied.

The real ``safe_request`` stack runs with the ``httpx`` transport patched
and ``asyncio.sleep`` stubbed, so no socket is opened and no time is spent.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations import http as integration_http
from app.integrations.http import RefreshableHeaders
from app.integrations.providers.opencti._common import (
    _client,
    _graphql,
    _is_read_document,
)

BASE_URL = "https://demo.opencti.io"


def _headers() -> RefreshableHeaders:
    return RefreshableHeaders({"Authorization": "Bearer static-api-key", "Content-Type": "application/json"})


def _resp(url: str, status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=body or {}, request=httpx.Request("POST", url))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(integration_http.asyncio, "sleep", _sleep)


@pytest.mark.parametrize(
    ("document", "is_read"),
    [
        ("query PlatformVersion { about { version } }", True),
        ("{ about { version } }", True),
        ("mutation AddLabel($v: String!) { labelAdd(input: {value: $v}) { id } }", False),
        ("  mutation { doThing }", False),
    ],
)
def test_read_documents_are_told_from_writes(document: str, is_read: bool) -> None:
    assert _is_read_document(document) is is_read


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 503, 504])
async def test_a_read_retries_a_transient_proxy_error(status: int) -> None:
    calls = {"n": 0}

    async def _flaky(self: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(url, status)
        return _resp(url, 200, {"data": {"about": {"version": "7.261119.0"}}})

    headers = _headers()
    with patch.object(httpx.AsyncClient, "request", _flaky):
        async with _client(headers) as c:
            data, err = await _graphql(c, BASE_URL, headers, "query PlatformVersion { about { version } }")

    assert err is None
    assert data == {"about": {"version": "7.261119.0"}}
    assert calls["n"] == 2, "the transient answer, then one successful retry"


@pytest.mark.asyncio
async def test_a_mutation_is_never_replayed_on_a_proxy_error() -> None:
    calls = {"n": 0}

    async def _bad_gateway(self: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls["n"] += 1
        return _resp(url, 502)

    headers = _headers()
    with patch.object(httpx.AsyncClient, "request", _bad_gateway):
        async with _client(headers) as c:
            data, err = await _graphql(
                c, BASE_URL, headers, 'mutation AddLabel { labelAdd(input: {value: "x"}) { id } }'
            )

    assert data == {}
    assert err is not None and "502" in err
    assert calls["n"] == 1, "a write behind a failing proxy may have landed"


@pytest.mark.asyncio
async def test_a_persistent_proxy_error_still_fails_the_read() -> None:
    calls = {"n": 0}

    async def _down(self: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
        calls["n"] += 1
        return _resp(url, 503)

    headers = _headers()
    with patch.object(httpx.AsyncClient, "request", _down):
        async with _client(headers) as c:
            data, err = await _graphql(c, BASE_URL, headers, "query { me { id } }")

    assert data == {}
    assert err is not None and "503" in err
    assert calls["n"] == integration_http._RATE_LIMIT_MAX_RETRIES + 1
