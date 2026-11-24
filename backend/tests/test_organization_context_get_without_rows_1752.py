"""Slice 3b of #1752: the profile API carries profile fields only.

``GET /organization-context`` and the answer of a save name how many asset and
group rows exist; the rows themselves are read through ``/assets`` and
``/asset-groups``. The payload size does not depend on the asset count.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.v1 import organization_context as api
from app.services import organization_context as oc

_ORG = uuid.uuid4()


def _profile(count: int) -> oc.OrganizationProfile:
    assets = [{"name": f"host-{i:05d}", "category": "host"} for i in range(count)]
    return oc.parse_profile(
        {"name": "Acme", "sector": "Retail", "assets_exposure": assets, "asset_groups": [{"name": "dmz"}]}
    )


def _row() -> Any:
    return SimpleNamespace(
        version=3, profile={}, organization_id=_ORG, source="form", sources=None, created_at=None, author_user_id=None
    )


async def _get(monkeypatch: pytest.MonkeyPatch, count: int) -> api.OrganizationContextResponse:
    async def current_profile(_db: Any, _org: Any = None) -> tuple[Any, oc.OrganizationProfile]:
        return _row(), _profile(count)

    async def asset_list_counts(_db: Any, _org: Any) -> dict[str, int]:
        return {"assets_exposure": count, "asset_groups": 1}

    monkeypatch.setattr(oc, "current_profile", current_profile)
    monkeypatch.setattr(oc, "asset_list_counts", asset_list_counts)
    return await api.get_organization_context(db=None, _user=None)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_answers_counts_and_no_row_list(monkeypatch: pytest.MonkeyPatch) -> None:
    answer = await _get(monkeypatch, 3)
    assert "assets_exposure" not in answer.profile
    assert "asset_groups" not in answer.profile
    assert answer.row_counts == {"assets_exposure": 3, "asset_groups": 1}
    assert answer.profile["name"] == "Acme"


@pytest.mark.asyncio
async def test_get_payload_size_does_not_grow_with_5000_assets(monkeypatch: pytest.MonkeyPatch) -> None:
    small = len(json.dumps((await _get(monkeypatch, 10)).model_dump(mode="json")))
    large = len(json.dumps((await _get(monkeypatch, 5_000)).model_dump(mode="json")))
    # Only the digits of the count differ.
    assert large - small < 10


def test_a_response_without_counts_counts_the_profile_lists() -> None:
    answer = api._response(_row(), profile=_profile(2))
    assert answer.row_counts == {"asset_groups": 1, "assets_exposure": 2}
    assert "assets_exposure" not in answer.profile
