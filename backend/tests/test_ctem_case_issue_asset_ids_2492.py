"""A case asset row an issue names carries its inventory id and module ids (#2492).

#2448 gave the rows a validation attached an ``id_space`` and ``source_ids``;
the rows the member issues name stayed ``{name, provenance}``, so an agent
holding an OpenAEV asset id had nothing to compare. The case read and the case
page now resolve those names against the inventory at read time, in one lookup
for the page: one asset matched gets its ids, none or several stays name-only
with ``resolved: false``.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest

from app.services import ctem_case_assets as case_assets

ORG = uuid.uuid4()
HOST = uuid.uuid4()
TWIN_A = uuid.uuid4()
TWIN_B = uuid.uuid4()
OPENAEV_ID = str(uuid.uuid4())


class _Result(list[Any]):
    def all(self) -> list[Any]:
        return list(self)


class _Nested:
    async def __aenter__(self) -> _Nested:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Db:
    """Answers the name lookup first, then the source records; counts the reads."""

    def __init__(self, assets: list[tuple[Any, ...]], sources: list[tuple[uuid.UUID, str, str]]):
        self.assets = assets
        self.sources = sources
        self.reads = 0

    def begin_nested(self) -> _Nested:
        return _Nested()

    async def execute(self, _statement: Any) -> _Result:
        self.reads += 1
        return _Result(self.assets if self.reads == 1 else self.sources)


def _db() -> _Db:
    return _Db(
        assets=[
            (HOST, "host.example", "host.example"),
            (TWIN_A, "twin.example", "twin.example"),
            (TWIN_B, "Twin.example", "twin.example"),
        ],
        sources=[(HOST, "openaev", OPENAEV_ID)],
    )


@pytest.mark.asyncio
async def test_an_issue_named_host_present_once_gets_its_inventory_id_and_source_ids() -> None:
    row: dict[str, Any] = {"name": "Host.Example", "provenance": "issue"}
    await case_assets.resolve_named_rows(cast(Any, _db()), ORG, [row])
    assert row["asset_id"] == str(HOST)
    assert row["id_space"] == case_assets.ID_SPACE
    assert row["source_ids"] == [{"source": "openaev", "external_id": OPENAEV_ID}]
    assert row["sources"] == ["openaev"]
    assert row["provenance"] == "issue" and row["name"] == "Host.Example"
    assert "resolved" not in row


@pytest.mark.asyncio
async def test_a_name_matching_two_assets_or_none_stays_name_only() -> None:
    twin: dict[str, Any] = {"name": "twin.example", "provenance": "issue"}
    ghost: dict[str, Any] = {"name": "ghost.example", "provenance": "inventory"}
    await case_assets.resolve_named_rows(cast(Any, _db()), ORG, [twin, ghost])
    assert twin == {"name": "twin.example", "provenance": "issue", "resolved": False}
    assert ghost == {"name": "ghost.example", "provenance": "inventory", "resolved": False}


@pytest.mark.asyncio
async def test_attached_rows_are_unchanged_and_one_lookup_serves_the_page() -> None:
    attached: dict[str, Any] = {
        "asset_id": str(TWIN_A),
        "name": "web-1.example",
        "id_space": case_assets.ID_SPACE,
        "sources": ["opencti"],
        "source_ids": [{"source": "opencti", "external_id": "x"}],
        "provenance": "name_match_inferred",
    }
    before = dict(attached)
    rows: list[dict[str, Any]] = [
        {"name": "host.example", "provenance": "issue"},
        {"name": "twin.example", "provenance": "issue"},
        {"name": "ghost.example", "provenance": "issue"},
        attached,
    ]
    db = _db()
    await case_assets.resolve_named_rows(cast(Any, db), ORG, rows)
    assert attached == before
    # One name lookup for every row, then one source read for the matched assets: never one per row.
    assert db.reads == 2
    assert rows[0]["asset_id"] == str(HOST)


@pytest.mark.asyncio
async def test_a_store_that_cannot_be_read_leaves_the_rows_as_they_are() -> None:
    class _Broken(_Db):
        async def execute(self, _statement: Any) -> _Result:
            raise RuntimeError("store down")

    row: dict[str, Any] = {"name": "host.example", "provenance": "issue"}
    await case_assets.resolve_named_rows(cast(Any, _Broken([], [])), ORG, [row])
    assert row == {"name": "host.example", "provenance": "issue"}
    await case_assets.resolve_named_rows(cast(Any, _db()), None, [row])
    assert row == {"name": "host.example", "provenance": "issue"}


def test_the_case_read_and_the_case_page_resolve_the_issue_named_rows() -> None:
    from pathlib import Path

    root = Path(case_assets.__file__).resolve().parents[1]
    tool = (root / "services" / "builtin_tools" / "ctem_cases.py").read_text()
    page = (root / "api" / "v1" / "ctem_cases.py").read_text()
    assert "case_assets.resolve_named_rows(" in tool
    assert "case_assets.resolve_named_rows(" in page
