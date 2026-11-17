"""A resumed session that cannot reopen a sandbox service preview says why and whether a navigate will work (#9697).

The reopen of a resumed session is held to the live session's policy, which admits a started service's preview
(``sandbox_workspace/preview.py``). When the admission was gone or could not be read, the notice used to report the
outbound policy's generic private-address error and to promise that a navigate would work, which the same check
refuses. The refusal now names the case: no longer admitted (a navigate is refused too), or unreadable (read once
more before giving up; a navigate may work). Nothing else private is admitted.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.services import browser_tool
from app.services.sandbox_workspace import preview

from . import test_browser_session_resume_9408 as base
from .test_browser_session_resume_9408 import EXPRESSION, KEY, LAB, PAGE, _kept_by_another_process, _launch

# The fixtures of #9408's tests, shared: a fake Redis and a process with no session kept.
redis = base.redis
fresh_process = base.fresh_process


def _private_refused(monkeypatch) -> None:
    async def refuse(url):
        raise ValueError("URL host is unresolvable or targets a private or non-routable address")

    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", refuse)


def _admission(monkeypatch, *states: str) -> list[str]:
    """Answer *states* in order (the last one repeated); returns the URLs asked."""
    asked: list[str] = []
    answers = list(states)

    async def admission(url):
        asked.append(url)
        return answers.pop(0) if len(answers) > 1 else answers[0]

    monkeypatch.setattr(preview, "admission", admission)
    monkeypatch.setattr(browser_tool, "_PREVIEW_RECHECK_SECONDS", 0)
    return asked


async def test_a_preview_no_longer_admitted_is_said_and_no_navigate_is_promised(redis, fresh_process, monkeypatch):
    launch = _launch(monkeypatch, [])
    await _kept_by_another_process()
    _private_refused(monkeypatch)
    _admission(monkeypatch, preview.NOT_ADMITTED)

    png = await browser_tool.browser_screenshot(KEY)

    assert png == b"about:blank" and launch.page.gotos == []
    notice = browser_tool.take_notice(KEY) or ""
    assert "this sandbox service preview is no longer admitted" in notice
    assert "sandbox_service_status answers the current preview_url" in notice
    assert "A navigate to it is held to the same policy." in notice
    assert "Navigate to the page to go on." not in notice
    assert "private or non-routable" not in notice


async def test_an_unreadable_admission_is_read_once_more_and_the_page_reopens(redis, fresh_process, monkeypatch):
    launch = _launch(monkeypatch, [])
    await _kept_by_another_process()
    _private_refused(monkeypatch)
    asked = _admission(monkeypatch, preview.UNREADABLE, preview.ADMITTED)

    result = await browser_tool.browser_evaluate(KEY, EXPRESSION)

    assert result == f"JavaScript result:\n{PAGE} 1"
    assert asked[:2] == [PAGE, PAGE]
    assert launch.page.gotos[0] == PAGE
    assert "could not be opened again" not in (browser_tool.take_notice(KEY) or "")


async def test_an_admission_still_unreadable_says_a_navigate_may_work(redis, fresh_process, monkeypatch):
    launch = _launch(monkeypatch, [])
    await _kept_by_another_process()
    _private_refused(monkeypatch)
    _admission(monkeypatch, preview.UNREADABLE)

    await browser_tool.browser_screenshot(KEY)

    assert launch.page.gotos == []
    notice = browser_tool.take_notice(KEY) or ""
    assert "could not be read just now: a navigate to it may work in a moment" in notice
    assert notice.endswith("Navigate to the page to go on.")


async def test_a_navigate_to_a_preview_no_longer_admitted_says_why(redis, fresh_process, monkeypatch):
    _private_refused(monkeypatch)
    _admission(monkeypatch, preview.NOT_ADMITTED)

    answer = await browser_tool.browser_navigate(KEY, f"{LAB}/dashboard")

    assert answer.startswith("Navigation blocked by URL policy: this sandbox service preview is no longer admitted")


async def test_a_private_url_that_is_no_preview_keeps_the_generic_refusal(monkeypatch):
    _private_refused(monkeypatch)
    _admission(monkeypatch, preview.NOT_A_PREVIEW)

    with pytest.raises(ValueError, match="private or non-routable") as raised:
        await browser_tool._outbound_or_preview("http://10.0.0.5/")

    assert not isinstance(raised.value, browser_tool.PreviewRefusedError)


async def test_an_admitted_preview_passes_without_the_outbound_policy(monkeypatch):
    outbound = AsyncMock(side_effect=ValueError("never asked"))
    monkeypatch.setattr(browser_tool, "validate_outbound_url_async", outbound)
    _admission(monkeypatch, preview.ADMITTED)

    assert await browser_tool._outbound_or_preview(PAGE) == PAGE
    outbound.assert_not_awaited()


class _Redis:
    def __init__(self, live: set[str] | None = None, down: bool = False) -> None:
        self.live = live or set()
        self.down = down

    async def exists(self, key):
        if self.down:
            raise ConnectionError("redis down")
        return int(key in self.live)


@pytest.mark.parametrize(
    ("url", "client", "expected"),
    [
        ("https://example.com/", _Redis(), preview.NOT_A_PREVIEW),
        ("http://10.42.0.9:3100/x", _Redis({"sandbox_preview_origin:10.42.0.9:3100"}), preview.ADMITTED),
        ("http://10.42.0.9:3100/x", _Redis(), preview.NOT_ADMITTED),
        ("http://10.42.0.9:3100/x", _Redis(down=True), preview.UNREADABLE),
    ],
)
async def test_admission_tells_apart_why_a_url_is_not_admitted(monkeypatch, url, client, expected):
    async def redis_client():
        return client

    monkeypatch.setattr("app.core.redis.redis_client", redis_client)
    monkeypatch.setattr(preview, "_allowed_cache", {})

    assert await preview.admission(url) == expected
    assert await preview.allowed(url) is (expected == preview.ADMITTED)
