"""A CTEM case asset row carries its module ids and names its id space (#2448).

A case asset's ``asset_id`` is the CTEM inventory id (``organization_assets.id``).
An agent that compared it with an OpenAEV asset id saw two UUIDs and wrote a
false "wrong asset" note. The row now carries ``id_space`` ``inventory`` and
``source_ids`` ``[{source, external_id}]``; a row stored before the change is
enriched from the inventory at read time; the case read tool and the loop
prompt state which id to compare.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.services import ctem_case_assets as case_assets
from app.services import ctem_case_loop as loop

NOW = datetime(2026, 11, 26, 6, 0, tzinfo=UTC)
ORG = uuid.uuid4()
WEB = uuid.uuid4()
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
    """Answers the name lookup (when *assets* is given), then the source records."""

    def __init__(self, sources: list[tuple[uuid.UUID, str, str]], assets: list[tuple[Any, ...]] | None = None):
        self.sources = sources
        self.assets = assets
        self.reads = 0

    def begin_nested(self) -> _Nested:
        return _Nested()

    def add(self, _row: object) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def execute(self, _statement: Any) -> _Result:
        self.reads += 1
        if self.assets is not None and self.reads == 1:
            return _Result(self.assets)
        return _Result(self.sources)


class _Broken(_Db):
    async def execute(self, _statement: Any) -> _Result:
        raise RuntimeError("store down")


@pytest.mark.asyncio
async def test_resolve_names_returns_the_source_records() -> None:
    db = _Db([(WEB, "openaev", OPENAEV_ID)], assets=[(WEB, "web-1.example", "web-1.example")])
    resolved = await case_assets.resolve_names(cast(Any, db), ORG, ["web-1.example"])
    assert resolved is not None
    hit = resolved["web-1.example"]
    assert hit["asset_id"] == str(WEB)
    assert hit["sources"] == ["openaev"]
    assert hit["source_ids"] == [{"source": "openaev", "external_id": OPENAEV_ID}]


@pytest.mark.asyncio
async def test_a_validation_writes_the_source_ids_and_the_reads_keep_them() -> None:
    db = _Db([(WEB, "openaev", OPENAEV_ID)], assets=[(WEB, "web-1.example", "web-1.example")])
    case = SimpleNamespace(
        id=uuid.uuid4(),
        organization_id=ORG,
        remediation_kind="upgrade",
        state="waiting_decision",
        validations=[],
        validation_plan=False,
        product_label="Example Server",
        fixed_version="2.0",
        resolution_kind=None,
    )
    await loop.record_validation(
        cast(Any, db),
        cast(Any, case),
        provider="openaev",
        kind="validation",
        ref="sim-1",
        result="pending",
        run_order=["web-1.example"],
        now=NOW,
    )
    row = case.validations[-1]["assets"][0]
    want = [{"source": "openaev", "external_id": OPENAEV_ID}]
    assert row["source_ids"] == want
    assert row["id_space"] == case_assets.ID_SPACE
    assert row["sources"] == ["openaev"]

    attached = case_assets.attached_assets(case.validations)
    assert attached[0]["source_ids"] == want
    merged = case_assets.merged_assets([], attached)
    assert merged[0]["source_ids"] == want and merged[0]["asset_id"] == str(WEB)
    page = case_assets.with_attached({"assets": [], "asset_count": 0}, attached)
    assert page["assets"][0]["source_ids"] == want
    assert page["assets"][0]["id_space"] == case_assets.ID_SPACE


@pytest.mark.asyncio
async def test_a_row_stored_before_the_change_is_enriched_at_read_time() -> None:
    old = {"asset_id": str(WEB), "name": "web-1.example", "sources": ["openaev"], "provenance": "name_match_inferred"}
    issue_row = {"name": "db-1.example", "provenance": "issue"}
    rows = [old, issue_row]
    await case_assets.enrich_source_ids(cast(Any, _Db([(WEB, "openaev", OPENAEV_ID)])), rows)
    assert old["source_ids"] == [{"source": "openaev", "external_id": OPENAEV_ID}]
    assert old["id_space"] == case_assets.ID_SPACE
    # A row with no inventory id (an issue's asset name) is left as it is.
    assert issue_row == {"name": "db-1.example", "provenance": "issue"}


@pytest.mark.asyncio
async def test_enrichment_keeps_stored_ids_and_never_fails_a_read() -> None:
    stored = {"asset_id": str(WEB), "source_ids": [{"source": "opencti", "external_id": "x"}]}
    db = _Db([(WEB, "openaev", OPENAEV_ID)])
    await case_assets.enrich_source_ids(cast(Any, db), [stored])
    assert stored["source_ids"] == [{"source": "opencti", "external_id": "x"}] and db.reads == 0

    old = {"asset_id": str(WEB)}
    await case_assets.enrich_source_ids(cast(Any, _Broken([])), [old])
    assert "source_ids" not in old and old["id_space"] == case_assets.ID_SPACE


def test_the_case_read_tool_and_the_loop_prompt_name_the_id_space() -> None:
    import inspect

    from app.agents import builtin_agents
    from app.services.builtin_tools import ctem_cases

    tool_source = inspect.getsource(ctem_cases)
    assert "asset_id is the inventory id" in tool_source
    assert "source_ids[].external_id" in tool_source
    prompt_source = inspect.getsource(builtin_agents)
    assert "compare an OpenAEV or OpenCTI id" in prompt_source
    assert "with its source_ids[].external_id, never with asset_id" in prompt_source
