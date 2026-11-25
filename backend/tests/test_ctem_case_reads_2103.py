"""Agent case reads (#2103): cases by asset, a case id prefix, an asset found by its id.

1. ``list_ctem_cases(asset=...)`` passes the asset to the viewer's list, so a duplicate check on
   one asset reads every case on it in one call, never the first page of the organization.
2. ``get_ctem_case`` takes a unique 8+ character id prefix; an ambiguous one lists the ids; a
   shorter or non-hex one is no case and the answer says what case_id takes.
3. The asset inventory text search reads an 8+ character id prefix as the start of the asset id.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_cases_store as store
from app.services import ctem_cases_view as view_of
from app.services import organization_asset_store as asset_store
from tests.test_ctem_case_tools import env  # noqa: F401 - fixture


async def test_the_list_filters_on_one_asset(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    seen: list[Any] = []
    real = view_of.list_for_viewer

    def _list(cases: Any, view: Any, **kw: Any) -> Any:
        seen.append(kw.get("asset"))
        return real(cases, view, **kw)

    monkeypatch.setattr(view_of, "list_for_viewer", _list)
    await env.call("list_ctem_cases", asset="  web-01 ")
    await env.call("list_ctem_cases")
    assert seen == ["web-01", None]


async def test_a_unique_prefix_reads_the_case(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    asked: list[str] = []

    async def _prefix(_db: Any, prefix: str, *, organization_id: Any, limit: int = 2) -> list[uuid.UUID]:
        asked.append(prefix)
        return [env.case.id]

    monkeypatch.setattr(store, "case_ids_with_prefix", _prefix)
    short = str(env.case.id)[:8]
    body = json.loads(await env.call("get_ctem_case", case_id=short))
    assert body["id"] == str(env.case.id) and asked == [short]


async def test_an_ambiguous_prefix_lists_the_ids(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    other = uuid.uuid4()

    async def _prefix(_db: Any, prefix: str, *, organization_id: Any, limit: int = 2) -> list[uuid.UUID]:
        return [env.case.id, other]

    monkeypatch.setattr(store, "case_ids_with_prefix", _prefix)
    answer = await env.call("get_ctem_case", case_id="abcdef01")
    assert "matches several cases" in answer and str(env.case.id) in answer and str(other) in answer


async def test_a_short_or_non_hex_id_is_no_case(env: SimpleNamespace) -> None:  # noqa: F811
    for value in ("abc", "not-a-case-id"):
        answer = await env.call("get_ctem_case", case_id=value)
        assert answer.startswith(f"No case {value}.") and "first 8+ characters" in answer


def test_an_id_prefix_is_eight_hex_characters_or_more() -> None:
    assert asset_store.id_prefix("ABCDEF01") == "abcdef01"
    assert asset_store.id_prefix(" 1b2c3d4e-5f ") == "1b2c3d4e-5f"
    assert asset_store.id_prefix("abcdef0") is None
    assert asset_store.id_prefix("laptop-07") is None
    assert asset_store.id_prefix("") is None


# ── the store on PostgreSQL ──────────────────────────────────────────────


@pytest.mark.integration
async def test_an_asset_is_found_by_its_id_or_id_prefix() -> None:
    from tests.test_organization_asset_store_postgres import _database, _org, _save

    host = {"name": "laptop-07", "category": "host", "source": "intune", "external_id": "d-7", "serial": "SN-007"}
    async with _database() as sessions, sessions() as session:
        org = await _org(session, "A", primary=True)
        await _save(session, org, 1, [host])
        await asset_store.ensure_built(session)
        await session.commit()
        every = await asset_store.list_assets(session, org)
        asset_id = str(every.items[0]["id"])
        whole = await asset_store.list_assets(session, org, q=asset_id)
        start = await asset_store.list_assets(session, org, q=asset_id[:8].upper())
    assert [a["name"] for a in whole.items] == ["laptop-07"]
    assert [a["name"] for a in start.items] == ["laptop-07"]
