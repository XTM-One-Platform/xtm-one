"""#2180: a webhook delivery the assignment's secret refuses is kept for its owner to read."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.v1 import webhooks
from app.services import webhook_refusals


class _Lists:
    def __init__(self) -> None:
        self.data: dict[str, list[str]] = {}
        self.ttl: dict[str, int] = {}

    async def lpush(self, key: str, value: str) -> None:
        self.data.setdefault(key, []).insert(0, value)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        self.data[key] = self.data.get(key, [])[start : end + 1]

    async def expire(self, key: str, seconds: int) -> None:
        self.ttl[key] = seconds

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return self.data.get(key, [])[start : end + 1]


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> _Lists:
    fake = _Lists()

    async def _client() -> _Lists:
        return fake

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    return fake


def _request(headers: dict[str, str]) -> Request:
    raw = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": raw, "query_string": b""})


@pytest.mark.asyncio
async def test_a_refused_delivery_is_kept_with_its_reason_and_header_names_only(redis: _Lists) -> None:
    request = _request({"X-Webhook-Token": "wrong-value", "Content-Type": "application/json"})
    with pytest.raises(HTTPException) as refused:
        await webhooks._verify_or_record_refusal(b"{}", request, "the-secret", assignment_id="a1")
    assert refused.value.status_code == 401
    rows = await webhook_refusals.refusals("a1")
    assert len(rows) == 1
    assert rows[0]["reason"] == "Invalid webhook token"
    assert rows[0]["auth_headers"] == ["x-webhook-token"]
    stored = json.dumps(redis.data)
    assert "wrong-value" not in stored and "the-secret" not in stored
    assert redis.ttl["webhook_refusals:a1"] == webhook_refusals.REFUSALS_TTL_SECONDS


@pytest.mark.asyncio
async def test_an_accepted_delivery_records_nothing_and_the_list_is_bounded(redis: _Lists) -> None:
    await webhooks._verify_or_record_refusal(b"{}", _request({"X-Webhook-Token": "s"}), "s", assignment_id="a2")
    assert await webhook_refusals.refusals("a2") == []
    for _ in range(webhook_refusals.MAX_REFUSALS + 5):
        with pytest.raises(HTTPException):
            await webhooks._verify_or_record_refusal(b"{}", _request({}), "s", assignment_id="a2")
    rows = await webhook_refusals.refusals("a2")
    assert len(rows) == webhook_refusals.MAX_REFUSALS
    assert rows[0]["reason"].startswith("Missing webhook signature") and rows[0]["auth_headers"] == []


@pytest.mark.asyncio
async def test_redis_down_refuses_the_delivery_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _down() -> Any:
        raise ConnectionError("down")

    monkeypatch.setattr("app.core.redis.redis_client", _down)
    with pytest.raises(HTTPException) as refused:
        await webhooks._verify_or_record_refusal(b"{}", _request({}), "s", assignment_id="a3")
    assert refused.value.status_code == 401
    assert await webhook_refusals.refusals("a3") == []
