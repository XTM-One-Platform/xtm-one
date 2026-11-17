"""Integration log lines never carry a URL's query string or userinfo.

Some providers take a credential in the URL (``?apikey=``, ``?token=``, a
signed URL, ``user:pass@`` on a private feed). ``safe_request`` already logs
only the request path; these tests pin the other log sites that print a
request URL: the ``AutoRefreshClient`` token-refresh lines, the RSS fetch
failure and the OpenAEV tenant-scoping skip.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import httpx
import pytest

from app.integrations.http import AutoRefreshClient, safe_request
from app.integrations.providers import rss
from app.integrations.providers.openaev import _common

SECRET = "s3cr3t-query-credential"
USERINFO_SECRET = "userinfo-password"


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    # The platform's own records: httpx's per-request INFO line is silenced
    # in production (``app.core.logging`` sets it to WARNING).
    return [record.getMessage() for record in caplog.records if record.name.startswith("app.")]


def _no_secret_logged(caplog: pytest.LogCaptureFixture) -> None:
    for message in _messages(caplog):
        assert SECRET not in message
        assert USERINFO_SECRET not in message


# ---------------------------------------------------------------------------
# AutoRefreshClient: the 401/403 refresh-and-retry lines
# ---------------------------------------------------------------------------


class _RefreshThenOk:
    """Answers *status* to the first request, 200 once the new token is sent."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.seen: list[tuple[str, str]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((str(request.url), request.headers.get("Authorization", "")))
        if request.headers.get("Authorization") == "Bearer fresh":
            return httpx.Response(200, json={"ok": True}, request=request)
        return httpx.Response(self.status, request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("via_safe_request", [False, True], ids=["client.get", "safe_request"])
async def test_the_token_refresh_logs_only_the_request_path(
    caplog: pytest.LogCaptureFixture, status: int, via_safe_request: bool
) -> None:
    handler = _RefreshThenOk(status)
    refreshes: list[int] = []

    async def _refresh() -> str:
        refreshes.append(1)
        return "fresh"

    url = f"https://api.example.test/v1/items?apikey={SECRET}&page=2"
    caplog.set_level(logging.DEBUG)
    async with AutoRefreshClient(
        {"Authorization": "Bearer stale"},
        refresh_fn=_refresh,
        transport=httpx.MockTransport(handler),
    ) as client:
        if via_safe_request:
            resp, err = await safe_request(client, "get", url, "Example", "list items")
            assert err is None
        else:
            resp = await client.get(url)

    assert resp is not None and resp.status_code == 200
    assert refreshes == [1]
    assert handler.seen == [(url, "Bearer stale"), (url, "Bearer fresh")]

    refresh_records = [
        record.args
        for record in caplog.records
        if record.name == "app.integrations.http"
        and record.msg in ("Got %s from %s — attempting token refresh", "Token refreshed — retrying %s %s")
    ]
    assert refresh_records == [(status, "/v1/items"), ("GET", "/v1/items")]
    _no_secret_logged(caplog)


# ---------------------------------------------------------------------------
# RSS: a feed that fails to fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_rss_fetch_logs_the_feed_without_its_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    url = f"https://reader:{USERINFO_SECRET}@feeds.example.test/private.xml?key={SECRET}"
    transport = httpx.MockTransport(lambda request: httpx.Response(404, request=request))
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=transport) as client:
        title, entries = await rss._fetch_and_parse(client, url)

    assert (title, entries) == (url, [])
    failures = [
        record.getMessage()
        for record in caplog.records
        if record.name == rss.logger.name and record.msg.startswith("Failed to fetch RSS feed")
    ]
    assert len(failures) == 1
    assert failures[0].startswith("Failed to fetch RSS feed https://***@feeds.example.test/private.xml?***: ")
    _no_secret_logged(caplog)


# ---------------------------------------------------------------------------
# OpenAEV: a URL outside the tenant-scoped base URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_openaev_scoping_skip_logs_no_query_string(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def _fake_safe_request(client, method, url, provider, action, **kwargs):
        calls.append(url)
        return None, None

    monkeypatch.setattr(_common, "safe_request", _fake_safe_request)
    headers = _common._headers("tok")
    headers.tenant_scope = SimpleNamespace(base_url="https://aev.example.test")
    url = f"https://elsewhere.example.test/api/things?token={SECRET}"
    caplog.set_level(logging.DEBUG)

    await _common.scoped_safe_request(None, "get", url, "OpenAEV", "list things", headers=headers)

    assert calls == [url]
    skipped = [
        record.getMessage()
        for record in caplog.records
        if record.msg.startswith("OpenAEV tenant scoping skipped")
    ]
    assert skipped == [
        "OpenAEV tenant scoping skipped, URL not under https://aev.example.test: "
        "https://elsewhere.example.test/api/things?***"
    ]
    _no_secret_logged(caplog)
