"""Tests for Notion KB sync request throttling and 429 retry behavior."""

import httpx
import pytest

from app.services.kb_sync import _NotionRequestController, _parse_retry_after_seconds


class _FakeNotionClient:
    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = responses
        self.calls: list[tuple[str, str]] = []

    async def request(self, method: str, url: str, **_: object) -> httpx.Response:
        self.calls.append((method, url))
        return self._responses.pop(0)


def _response(status_code: int, *, retry_after: str | None = None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(
        status_code=status_code,
        headers=headers,
        request=httpx.Request("GET", "https://api.notion.com/v1/test"),
        json={"ok": True},
    )


def test_parse_retry_after_seconds_numeric():
    assert _parse_retry_after_seconds("2.5") == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_notion_request_retries_429_using_retry_after(monkeypatch):
    waits: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("app.services.kb_sync.asyncio.sleep", _fake_sleep)

    client = _FakeNotionClient(
        [
            _response(429, retry_after="2"),
            _response(200),
        ]
    )
    controller = _NotionRequestController(
        requests_per_second=0,
        retry_attempts=3,
        retry_base_delay=0.5,
    )

    resp = await controller.request(client, "GET", "https://api.notion.com/v1/pages/p1")

    assert resp.status_code == 200
    assert len(client.calls) == 2
    assert waits == [2.0]


@pytest.mark.asyncio
async def test_notion_request_throttles_successive_requests(monkeypatch):
    waits: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr("app.services.kb_sync.asyncio.sleep", _fake_sleep)

    client = _FakeNotionClient([_response(200), _response(200)])
    controller = _NotionRequestController(
        requests_per_second=2,
        retry_attempts=1,
        retry_base_delay=0.1,
    )

    await controller.request(client, "GET", "https://api.notion.com/v1/pages/p1")
    await controller.request(client, "GET", "https://api.notion.com/v1/pages/p2")

    assert len(client.calls) == 2
    assert any(wait >= 0.45 for wait in waits)
