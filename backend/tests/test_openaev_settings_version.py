"""``manage_openaev_settings`` action ``version`` reads a plain-text body.

OpenAEV ``GET /api/settings/version`` answers the version as a bare string
(``PlatformSettingsApi.platformVersion`` returns ``String``), so parsing the body
as JSON failed with "Extra data". The tool now reads the text and still accepts
a JSON body.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.integrations.providers.openaev import _common, build_tools
from app.integrations.providers.openaev.platform import _version_from_body
from app.tools.base import CallableToolExecutor, ToolCall

BASE = "https://demo.openaev.io"


class _TextResp:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, text: str) -> None:
        self.text = text
        self.content = text.encode()

    def json(self) -> Any:
        return json.loads(self.text)


class _Transport:
    def __init__(self, version_body: str) -> None:
        self.version_body = version_body
        self.urls: list[str] = []

    async def __call__(
        self, client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any
    ) -> tuple[_TextResp, None]:
        self.urls.append(url)
        if url == f"{BASE}/api/settings/version":
            return _TextResp(self.version_body), None
        return _TextResp("{}"), None


async def _version(monkeypatch: pytest.MonkeyPatch, body: str) -> str:
    transport = _Transport(body)
    monkeypatch.setattr(_common, "safe_request", transport)
    executor = CallableToolExecutor()
    build_tools(f"{BASE}||test-token", executor)
    result = await executor.execute_tool(
        ToolCall(id="call-v", name="manage_openaev_settings", arguments={"action": "version"})
    )
    assert f"{BASE}/api/settings/version" in transport.urls
    return str(result.content)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("3.261116.1", "3.261116.1"),
        ("  3.261116.1\n", "3.261116.1"),
        ('"3.261116.1"', "3.261116.1"),
        ('{"version": "3.261116.1"}', "3.261116.1"),
        ('{"platform_version": "3.261116.1"}', "3.261116.1"),
        ("", ""),
        ("   ", ""),
        ("null", ""),
    ],
)
def test_version_from_body(body: str, expected: str) -> None:
    assert _version_from_body(body) == expected


async def test_plain_text_version_body_is_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    content = await _version(monkeypatch, "3.261116.1")
    assert 'OpenAEV version: {"version": "3.261116.1"}' in content
    assert "Extra data" not in content


async def test_json_version_body_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    content = await _version(monkeypatch, '{"version": "3.261116.1"}')
    assert 'OpenAEV version: {"version": "3.261116.1"}' in content


async def test_empty_version_body_gives_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    content = await _version(monkeypatch, "")
    assert "without a version" in content
    assert "OpenAEV version: {" not in content
