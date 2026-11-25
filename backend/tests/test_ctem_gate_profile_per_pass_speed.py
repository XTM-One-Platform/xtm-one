# SPDX-License-Identifier: Apache-2.0

"""A pass of the CTEM scope gate over many new signals stays cheap on a large software store.

Each signal recorded by a pass (evidence health, the listeners) ran the gate, and
every gate call rebuilt the whole software profile on the web event loop, with a
quadratic scan in ``matcher_entries``: a case-opening drain stalled the loop for
seconds at a time. The profile is now built once per session and organization,
the match index is linear and the build runs off the loop.

The tests count work, never the wall clock: a CI runner's load made a time
bound flaky (merge_group run 45613256967, 3.69 s against 2.0 s, #2140).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_scope_gate as gate
from app.services import ctem_scope_gate_store as store
from app.services import organization_software_store as software_store


def _software(count: int) -> list[dict[str, Any]]:
    return [
        {"vendor": f"vendor-{i % 97}", "product": f"product-{i}", "cpe": "", "versions": [f"{i}.0"]}
        for i in range(count)
    ]


class _CountedKey(str):
    """A matcher key that counts its equality comparisons (a dict hit costs one, a scan one per entry)."""

    compared = 0

    def __eq__(self, other: object) -> bool:
        _CountedKey.compared += 1
        return str.__eq__(self, other)

    __hash__ = str.__hash__


def test_matcher_entries_is_linear_in_the_software_count(monkeypatch: pytest.MonkeyPatch) -> None:
    size = 4_000
    key = software_store._key
    monkeypatch.setattr(software_store, "_key", lambda value: _CountedKey(key(value)))
    _CountedKey.compared = 0
    software = _software(size)
    # Vendor-less typed rows take the lookup that used to scan every entry.
    typed = [{"vendor": "", "product": f"product-{i}", "cpe": "cpe:x", "version": ""} for i in range(0, size, 2)]
    entries = software_store.matcher_entries(software, typed)
    assert len(entries) == size
    # A typed row of a listed software gives its CPE, never a second entry.
    assert entries[0]["cpe"] == "cpe:x" and entries[1]["cpe"] == ""
    # A few key comparisons per row: a scan of the entries per row would make about size * size / 2.
    rows = len(software) + len(typed)
    assert _CountedKey.compared > 0, "the counting key must reach the match index"
    assert _CountedKey.compared <= 4 * rows, f"{_CountedKey.compared} key comparisons for {rows} rows"


def test_a_vendorless_row_joins_the_one_listed_software_of_that_product() -> None:
    one = software_store.matcher_entries([{"vendor": "acme", "product": "Tool", "versions": []}], [{"product": "tool"}])
    assert len(one) == 1 and one[0]["vendor"] == "acme"
    two = software_store.matcher_entries(
        [{"vendor": "acme", "product": "Tool", "versions": []}, {"vendor": "other", "product": "Tool", "versions": []}],
        [{"product": "tool"}],
    )
    assert len(two) == 3


class _Db:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}

    def begin_nested(self) -> Any:
        db = self

        class _Nested:
            async def __aenter__(self) -> Any:
                return db

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Nested()


@pytest.mark.asyncio
async def test_one_pass_over_many_signals_reads_the_profile_once(monkeypatch: pytest.MonkeyPatch) -> None:
    org = uuid.uuid4()
    reads: list[uuid.UUID] = []

    async def _profile(db: Any, organization_id: uuid.UUID) -> Any:
        reads.append(organization_id)
        return SimpleNamespace(software=[])

    async def _links(db: Any, organization_id: uuid.UUID) -> Any:
        return object()

    async def _scenarios(db: Any, row: Any) -> Any:
        return None

    async def _related(db: Any, row: Any) -> list[Any]:
        return []

    def _decide(profile: Any, facts: Any, **kw: Any) -> Any:
        return SimpleNamespace(state="confirmed", record={})

    monkeypatch.setattr(store, "_profile", _profile)
    monkeypatch.setattr(store, "_primary_assets", _links)
    monkeypatch.setattr(store, "_scenarios_for", _scenarios)
    monkeypatch.setattr(store, "related_signals", _related)
    monkeypatch.setattr(store, "facts_of", lambda row: None)
    monkeypatch.setattr(gate, "decide", _decide)

    db = _Db()
    rows = [SimpleNamespace(id=uuid.uuid4(), organization_id=org, state="new", decision=None) for _ in range(500)]
    for row in rows:
        await store.evaluate(db, row)  # type: ignore[arg-type]
    # The cost of a pass is the profile build: one per pass, whatever the runner's speed.
    assert reads == [org], "the profile must be built once per pass, not once per signal"
    assert {row.state for row in rows} == {"confirmed"}
