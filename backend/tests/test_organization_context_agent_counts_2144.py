"""#2144: the category sizes an agent reads name what the paged read answers.

``get_organization_context(category="*")`` and the prompt summary counted
``software`` from the profile's hand-typed rows and ``assets_exposure`` from the
asset records, while the paged reads answer from the Software entities and the
merged inventory: 73 stated against 7948 paged, 2049 against 1789.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.organization_asset import OrganizationAsset, OrganizationSoftware
from app.services import organization_context as oc
from app.services.organization_context_injection import category_counts

_ORG = uuid.uuid4()


class _Db:
    """Answers ``COUNT`` reads by the model counted."""

    def __init__(self, held: dict[Any, int]) -> None:
        self.held = held

    async def execute(self, statement: Any) -> Any:
        text = str(statement)
        count = next((n for model, n in self.held.items() if f"FROM {model.__tablename__}" in text), 0)
        return SimpleNamespace(scalar=lambda: count)


def _records(monkeypatch: pytest.MonkeyPatch, counts: dict[str, int] | None) -> None:
    async def asset_list_counts(_db: Any, _org: Any) -> dict[str, int] | None:
        return counts

    monkeypatch.setattr(oc, "asset_list_counts", asset_list_counts)


def _profile() -> oc.OrganizationProfile:
    software = [{"product": f"tool-{i}"} for i in range(73)]
    return oc.parse_profile({"name": "Acme", "software": software})


@pytest.mark.asyncio
async def test_sizes_come_from_the_stores_the_paged_read_answers_from(monkeypatch: pytest.MonkeyPatch) -> None:
    _records(monkeypatch, {"assets_exposure": 2049, "asset_groups": 4})
    db = _Db({OrganizationAsset: 1789, OrganizationSoftware: 7948})
    counts = await oc.agent_list_counts(db, _ORG)  # type: ignore[arg-type]
    assert counts == {"assets_exposure": 1789, "asset_groups": 4, "software": 7948}
    sizes = category_counts(_profile(), asset_counts=counts)
    assert sizes["software"] == 7948
    assert sizes["assets_exposure"] == 1789
    assert sizes["asset_groups"] == 4


@pytest.mark.asyncio
async def test_empty_stores_keep_the_record_and_profile_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    _records(monkeypatch, {"assets_exposure": 3, "asset_groups": 1})
    counts = await oc.agent_list_counts(_Db({}), _ORG)  # type: ignore[arg-type]
    assert counts == {"assets_exposure": 3, "asset_groups": 1}
    assert category_counts(_profile(), asset_counts=counts)["software"] == 73


@pytest.mark.asyncio
async def test_no_organization_and_nothing_imported_answer_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _records(monkeypatch, None)
    assert await oc.agent_list_counts(_Db({}), None) is None  # type: ignore[arg-type]
    assert await oc.agent_list_counts(_Db({}), _ORG) is None  # type: ignore[arg-type]
