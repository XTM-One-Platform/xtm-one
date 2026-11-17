"""One platform User-Agent on every outbound request that names the platform.

``PLATFORM_USER_AGENT`` in ``app/integrations/http.py`` is the only place the
string is written. It carries the contact URL public hosts ask for, and no
copy of it is left behind: the RSS tools once announced a misspelt
``FiligramCopilot/1.0`` and the website KB sync a bare ``FiligranCopilot/1.0``.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.integrations.http import PLATFORM_USER_AGENT

_BACKEND_DIR = Path(__file__).resolve().parents[1]
_APP_DIR = _BACKEND_DIR / "app"

#: The headless browser deliberately announces an identity of its own.
_BROWSER_TOOL_USER_AGENT = "FiligranCopilot/1.0 (Browser Tool)"

#: The platform's product token with its version and optional comment - what a
#: hand-written copy of the User-Agent looks like, the misspelling included.
_PRODUCT_TOKEN = re.compile(rb"Filigra[nm]Copilot/[^\s\"']*(?: \([^()\"'\n]*\))?", re.IGNORECASE)


def _app_files() -> Iterator[Path]:
    for path in sorted(_APP_DIR.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def _answer_with(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Route every ``httpx.AsyncClient`` the code under test opens through *handler*."""
    real = httpx.AsyncClient

    def _client(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)


# ── Tripwires ─────────────────────────────────────────────────────────────


def test_the_misspelt_filigram_product_name_never_comes_back():
    hits = [
        path.relative_to(_BACKEND_DIR).as_posix()
        for path in _app_files()
        if b"filigramcopilot" in path.read_bytes().lower()
    ]
    assert hits == [], f"'FiligramCopilot' is a misspelling of 'FiligranCopilot': {hits}"


def test_the_platform_user_agent_is_written_out_only_where_it_is_defined():
    found: list[tuple[str, int, str]] = []
    for path in _app_files():
        if path.suffix == ".md":  # documentation may quote it; only code and data send it
            continue
        data = path.read_bytes()
        for match in _PRODUCT_TOKEN.finditer(data):
            line = data.count(b"\n", 0, match.start()) + 1
            value = match.group(0).decode("utf-8", "replace")
            found.append((path.relative_to(_BACKEND_DIR).as_posix(), line, value))

    allowed = [
        ("app/integrations/http.py", PLATFORM_USER_AGENT),
        ("app/services/browser_tool.py", _BROWSER_TOOL_USER_AGENT),
    ]
    stray = [f"{rel}:{line}: {value}" for rel, line, value in found if (rel, value) not in allowed]
    assert not stray, (
        "Import PLATFORM_USER_AGENT from app.integrations.http instead of writing "
        "the User-Agent out by hand:\n" + "\n".join(stray)
    )
    assert sorted((rel, value) for rel, _line, value in found) == sorted(allowed)


def test_web_fetch_reexports_the_one_definition_and_it_says_how_to_reach_the_operator():
    from app.services import web_fetch

    assert web_fetch.USER_AGENT is PLATFORM_USER_AGENT
    assert PLATFORM_USER_AGENT.startswith("FiligranCopilot/")
    assert "(+https://filigran.io)" in PLATFORM_USER_AGENT


def test_the_keyless_public_api_providers_announce_the_platform_user_agent():
    from app.integrations.providers.ransomware_live import build_ransomlive_headers
    from app.integrations.providers.vuln_intel import build_vulnintel_headers

    assert build_vulnintel_headers(None)["User-Agent"] == PLATFORM_USER_AGENT
    assert build_vulnintel_headers("nvd-api-key")["User-Agent"] == PLATFORM_USER_AGENT
    assert build_ransomlive_headers(None)["User-Agent"] == PLATFORM_USER_AGENT


# ── On the wire ───────────────────────────────────────────────────────────

_FEED = (
    b'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>Advisories</title>'
    b"<item><title>Patch Tuesday</title><link>https://feeds.test/patch</link>"
    b"<description>Monthly fixes</description></item></channel></rss>"
)


@pytest.mark.asyncio
async def test_every_rss_tool_announces_the_platform_user_agent(monkeypatch):
    from app.integrations.providers import rss
    from app.tools.base import CallableToolExecutor, ToolCall

    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent"))
        return httpx.Response(200, headers={"content-type": "application/rss+xml"}, content=_FEED)

    _answer_with(monkeypatch, handler)
    executor = CallableToolExecutor()
    tools = rss.build_tools("https://feeds.test/a.xml\nhttps://feeds.test/b.xml", executor)
    calls = {
        "fetch_rss_feeds": ({}, "Patch Tuesday"),
        "search_rss_entries": ({"query": "patch"}, "Patch Tuesday"),
        "list_rss_feeds": ({}, "Advisories"),
    }
    assert sorted(tool.name for tool in tools) == sorted(calls)

    for name, (arguments, expected) in calls.items():
        result = await executor.execute_tool(ToolCall(id=name, name=name, arguments=arguments))
        assert not result.is_error, result.content
        assert expected in result.content, result.content

    assert seen == [PLATFORM_USER_AGENT] * (2 * len(calls))


@pytest.mark.asyncio
async def test_the_website_kb_sync_announces_the_platform_user_agent_on_every_page(monkeypatch):
    from app.services import kb_sync

    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("user-agent")))
        if request.url.path == "/":
            return httpx.Response(200, headers={"content-type": "text/html"}, text='<a href="/guide">Guide</a>')
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="plain")

    cleanup_db = SimpleNamespace(get=AsyncMock(return_value=None), commit=AsyncMock())

    @contextlib.asynccontextmanager
    async def _session():
        yield cleanup_db

    _answer_with(monkeypatch, handler)
    monkeypatch.setattr(kb_sync, "_WEBSITE_REQUEST_DELAY", 0.0)
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())
    monkeypatch.setattr("app.services.web_fetch.extract_from_html", lambda _html, url: ("", {}))
    monkeypatch.setattr("app.core.database.async_session", _session)

    synced = await kb_sync.sync_website(
        db=SimpleNamespace(),
        kb=SimpleNamespace(id=uuid.uuid4(), name="docs"),
        url="https://docs.test",
        max_pages=5,
        max_depth=1,
    )

    assert synced == []
    assert seen == [("/", PLATFORM_USER_AGENT), ("/guide", PLATFORM_USER_AGENT)]
