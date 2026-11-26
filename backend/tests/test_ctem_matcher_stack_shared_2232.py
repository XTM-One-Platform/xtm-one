# SPDX-License-Identifier: Apache-2.0
"""#2232: the matcher stack is computed once per organization and shared, the inventory read is indexed."""

from __future__ import annotations

import asyncio
import random
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_case_versions as cv
from app.services import ctem_cases_source as source
from app.services import organization_context as org_context


def _sql_rule(stored: list[str], labels: list[str]) -> list[str]:
    """What the former ``product_key IN keys OR product_key LIKE '% key'`` read selected."""
    out = set()
    for label in labels:
        keys = cv.product_keys(label)
        if not keys:
            continue
        for key in stored:
            if key in keys or key.endswith(" " + keys[0]):
                out.add(key)
    return sorted(out)


def test_matching_product_keys_selects_what_the_like_read_selected() -> None:
    rng = random.Random(7)  # noqa: S311 - seeded test data, not crypto
    words = ["google", "llc", "chrome", "microsoft", "windows", "edge", "webview2", "mozilla", "firefox", "oracle"]
    stored = list({" ".join(rng.choice(words) for _ in range(rng.randint(1, 4))) for _ in range(3000)})
    labels = [
        "Google Chrome",
        "Chrome",
        "Microsoft Windows",
        "Firefox",
        "Mozilla Firefox",
        "Oracle",
        "",
        "Edge WebView2",
    ]
    assert cv.matching_product_keys(stored, labels) == _sql_rule(stored, labels)


class _CountedKey(str):
    """A stored key that counts the comparisons made on it, so linearity is measured without a clock."""

    calls = 0

    __hash__ = str.__hash__

    def __eq__(self, other: object) -> bool:
        type(self).calls += 1
        return str.__eq__(self, other)

    def __contains__(self, other: object) -> bool:
        type(self).calls += 1
        return str.__contains__(self, other)  # type: ignore[operator]

    def endswith(self, *args: Any) -> bool:
        type(self).calls += 1
        return str.endswith(self, *args)

    def startswith(self, *args: Any) -> bool:
        type(self).calls += 1
        return str.startswith(self, *args)


def test_matching_product_keys_is_linear() -> None:
    """Comparisons on stored keys stay linear in keys plus labels (a per-label scan would be keys x labels).

    Counted, never timed: a wall-clock bound failed on a loaded CI shard (3.8 s for a 2 s bound).
    """
    stored = [_CountedKey(f"vendor{i % 700} product {i}") for i in range(5000)]
    labels = [f"Product {i}" for i in range(1000)]
    _CountedKey.calls = 0
    matched = cv.matching_product_keys(stored, labels)
    assert len(matched) == 1000
    assert _CountedKey.calls <= 4 * (len(stored) + len(labels))


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


@pytest.fixture
def org(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    row = SimpleNamespace(organization_id=uuid.uuid4(), version=3)

    async def current_version(_db: Any, organization_id: Any = None) -> Any:
        return row

    monkeypatch.setattr(org_context, "current_version", current_version)
    return row


@pytest.mark.asyncio
async def test_concurrent_viewer_builds_compute_the_stack_once(
    monkeypatch: pytest.MonkeyPatch, org: SimpleNamespace
) -> None:
    redis = _FakeRedis()
    calls = 0

    async def compute(_db: Any) -> list[dict[str, Any]]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return [{"label": "Google Chrome", "versions": ["1.0"]}]

    async def client() -> Any:
        return redis

    monkeypatch.setattr(source, "_compute_matcher_stack", compute)
    monkeypatch.setattr(source, "_stack_redis", client)
    stacks = await asyncio.gather(*(source._matcher_stack(None) for _ in range(5)))  # type: ignore[arg-type]
    assert calls == 1
    assert all(s == [{"label": "Google Chrome", "versions": ["1.0"]}] for s in stacks)
    assert source._matcher_stack_key(org.organization_id, 3) in redis.data
    assert f"{source._matcher_stack_key(org.organization_id, 3)}:lock" not in redis.data


@pytest.mark.asyncio
async def test_a_build_waits_for_the_stack_another_pod_computes(
    monkeypatch: pytest.MonkeyPatch, org: SimpleNamespace
) -> None:
    redis = _FakeRedis()
    key = source._matcher_stack_key(org.organization_id, 3)
    redis.data[f"{key}:lock"] = "1"  # another pod holds the computation

    async def compute(_db: Any) -> list[dict[str, Any]]:
        raise AssertionError("the stack another pod computes must not be computed again")

    async def client() -> Any:
        return redis

    async def other_pod() -> None:
        await asyncio.sleep(0.1)
        redis.data[key] = '[{"label": "Firefox"}]'

    monkeypatch.setattr(source, "_compute_matcher_stack", compute)
    monkeypatch.setattr(source, "_stack_redis", client)
    monkeypatch.setattr(source, "_MATCHER_STACK_POLL_SECONDS", 0.02)
    stack, _ = await asyncio.gather(source._matcher_stack(None), other_pod())  # type: ignore[arg-type]
    assert stack == [{"label": "Firefox"}]


@pytest.mark.asyncio
async def test_without_redis_the_stack_is_computed(monkeypatch: pytest.MonkeyPatch, org: SimpleNamespace) -> None:
    async def compute(_db: Any) -> list[dict[str, Any]]:
        return [{"label": "Chrome"}]

    async def client() -> Any:
        return None

    monkeypatch.setattr(source, "_compute_matcher_stack", compute)
    monkeypatch.setattr(source, "_stack_redis", client)
    assert await source._matcher_stack(None) == [{"label": "Chrome"}]  # type: ignore[arg-type]
