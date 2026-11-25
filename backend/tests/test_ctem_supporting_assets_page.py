# SPDX-License-Identifier: Apache-2.0

"""UX r1 B3 and B4 on #1995: the supporting asset list is read once per signal version, then sorted and paged."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_scope_gate_store as store


def _rows(n: int) -> list[dict[str, Any]]:
    return [
        {
            "asset": f"ws-{i:05d}",
            "software": "Windows",
            "through": "Workstations" if i % 2 else "All laptops",
            "through_kind": "asset_group",
            "source": "rule",
            "roles": ["stores"],
        }
        for i in range(n)
    ]


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The inventory index reads of the store, counted; the reach itself answers 10,000 assets."""
    calls: list[str] = []
    store._supporting_cache.clear()

    class Index:
        truncated = False

        def supporting(self, refs: list[str], keys: list[str], key: str) -> list[dict[str, Any]]:
            return _rows(10_000)

    async def profile(db: Any, organization_id: Any) -> None:
        return None

    async def index(db: Any, organization_id: Any) -> Index:
        calls.append(str(organization_id))
        return Index()

    monkeypatch.setattr(store, "_profile", profile)
    monkeypatch.setattr(store, "_primary_assets", index)
    monkeypatch.setattr(store, "facts_of", lambda row: None)
    monkeypatch.setattr(
        store.gate,
        "resolve",
        lambda profile, facts, _: SimpleNamespace(software_refs=["Windows"], products=[], keys=[]),
    )
    yield calls
    store._supporting_cache.clear()


def _signal(updated: datetime) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4(), updated_at=updated)


async def test_pages_searches_and_sorts_read_the_inventory_once(reads: list[str]) -> None:
    row = _signal(datetime(2026, 11, 25, tzinfo=UTC))
    first = await store.supporting_assets(None, row, "Employee workstations", offset=0, limit=10)
    assert first["total"] == 10_000 and first["assets"][0]["asset"] == "ws-00000"
    desc = await store.supporting_assets(None, row, "employee workstations", sort="asset", direction="desc", limit=10)
    assert [a["asset"] for a in desc["assets"][:2]] == ["ws-09999", "ws-09998"]
    assert desc["sort"] == "asset" and desc["direction"] == "desc"
    through = await store.supporting_assets(None, row, "employee workstations", sort="through", limit=3)
    assert {a["through"] for a in through["assets"]} == {"All laptops"}
    found = await store.supporting_assets(None, row, "employee workstations", query="ws-0500", offset=5000, limit=10)
    assert found["total"] == 10 and found["assets"] == []
    assert reads == [str(row.organization_id)]


async def test_an_unknown_sort_falls_back_to_the_asset_name(reads: list[str]) -> None:
    row = _signal(datetime(2026, 11, 25, tzinfo=UTC))
    page = await store.supporting_assets(None, row, "employee workstations", sort="roles", limit=1)
    assert page["sort"] == "asset" and page["assets"][0]["asset"] == "ws-00000"


async def test_a_re_decided_signal_reads_again(reads: list[str]) -> None:
    row = _signal(datetime(2026, 11, 25, tzinfo=UTC))
    await store.supporting_assets(None, row, "employee workstations", limit=1)
    row.updated_at = datetime(2026, 11, 25, 1, tzinfo=UTC)
    await store.supporting_assets(None, row, "employee workstations", limit=1)
    assert len(reads) == 2


def test_the_cache_keeps_at_most_its_bound() -> None:
    store._supporting_cache.clear()
    for n in range(store.SUPPORTING_CACHE_MAX + 5):
        store._supporting_keep(("o", str(n), "v", "k"), [], False)
    assert len(store._supporting_cache) == store.SUPPORTING_CACHE_MAX
    assert ("o", "0", "v", "k") not in store._supporting_cache
    store._supporting_cache.clear()
