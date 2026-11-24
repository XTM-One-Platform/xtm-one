"""get_drata_policy_version_content pages the policy text instead of cutting it.

Regression: the tool returned ``text[:10000]`` with no total and no way to read
the rest, so a long policy was cut silently. It now answers a window with its
range, the total length and the offset of the next window.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import drata
from app.integrations.providers.drata import _common as drata_common
from app.tools.base import CallableToolExecutor, ToolCall

_TOKEN = "https://public-api.drata.com/public/v2||test-token"


class _HtmlResp:
    def __init__(self, html: str) -> None:
        self.text = html
        self.headers = {"content-type": "text/html; charset=utf-8"}
        self.content = html.encode()
        self.status_code = 200


def _patch(monkeypatch, html: str) -> None:
    async def _fake_safe_request(client, method, url, provider, action, *, expected_statuses=None, **kwargs):
        return _HtmlResp(html), None

    monkeypatch.setattr(drata_common, "safe_request", _fake_safe_request)


async def _call(args: dict) -> str:
    executor = CallableToolExecutor()
    drata.build_tools(_TOKEN, executor)
    result = await executor.execute_tool(ToolCall(id="c1", name="get_drata_policy_version_content", arguments=args))
    assert result.is_error is False
    return result.content if isinstance(result.content, str) else str(result.content)


_TEXT = "".join(f"{i % 10}" for i in range(25_000))


@pytest.mark.asyncio
async def test_long_policy_first_window_names_total_and_next_offset(monkeypatch):
    _patch(monkeypatch, f"<html><body><p>{_TEXT}</p></body></html>")
    out = await _call({"policy_id": 1, "version_id": 7})
    assert "characters 0 to 10000 of 25000" in out
    assert "offset=10000" in out
    assert out.endswith(_TEXT[:10000])


@pytest.mark.asyncio
async def test_offset_reads_the_last_window(monkeypatch):
    _patch(monkeypatch, f"<p>{_TEXT}</p>")
    out = await _call({"policy_id": 1, "version_id": 7, "offset": 20_000})
    assert "characters 20000 to 25000 of 25000" in out
    assert "more: call again" not in out
    assert out.endswith(_TEXT[20_000:])


@pytest.mark.asyncio
async def test_max_chars_is_capped(monkeypatch):
    _patch(monkeypatch, "<p>" + "a" * 60_000 + "</p>")
    out = await _call({"policy_id": 1, "version_id": 7, "max_chars": 100_000})
    assert "characters 0 to 50000 of 60000" in out


@pytest.mark.asyncio
async def test_offset_past_end_is_named(monkeypatch):
    _patch(monkeypatch, "<p>short</p>")
    out = await _call({"policy_id": 1, "version_id": 7, "offset": 99})
    assert "past the end" in out
    assert "5 characters" in out
