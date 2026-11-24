"""A profile save is judged against what the client was given, not the stale version JSON (#1738).

Since #1543 ``GET /organization-context`` answers the asset rows from the
records, where a sync writes its fresh ``last_seen``. The #1367 lock compared
the submitted rows with the stored version JSON, so saving an owned domain was
refused on a synced row the admin never touched. ``PATCH`` saves top-level
fields alone, no asset row travels.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from app.api.v1 import organization_context as api
from app.services import organization_context as oc

_ORG = uuid.uuid4()
_SYNCED = {"name": "Laptop-1", "source": "intune", "external_id": "dev-1", "last_seen": "2026-11-20T00:00:00Z"}


class _Db:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None

    async def refresh(self, _row: Any) -> None:
        return None


def _store(monkeypatch: pytest.MonkeyPatch, stored: dict[str, Any], records: list[dict[str, Any]]) -> list[Any]:
    """Version 1 holds *stored*; the records hold *records* (what a sync wrote since)."""
    saved: list[Any] = []
    row = SimpleNamespace(
        version=1,
        profile=stored,
        organization_id=_ORG,
        source="form",
        sources=None,
        created_at=None,
        author_user_id=None,
    )

    async def current_version(_db: Any, _org: Any = None) -> Any:
        return saved[-1] if saved else row

    async def hydrated_profile(_db: Any, version_row: Any) -> oc.OrganizationProfile:
        profile = oc.parse_profile(version_row.profile)
        return profile.model_copy(update={"assets_exposure": [oc.AssetEntry.model_validate(r) for r in records]})

    async def save_version(_db: Any, profile: Any, **kwargs: Any) -> Any:
        new = SimpleNamespace(
            version=len(saved) + 2,
            profile=profile.model_dump(mode="json"),
            organization_id=_ORG,
            source=kwargs.get("source"),
            sources=kwargs.get("sources"),
            created_at=None,
            author_user_id=None,
        )
        saved.append(new)
        return new

    async def no_problem(_db: Any, _profile: Any) -> None:
        return None

    async def named(_db: Any, *, user_id: Any, profile: Any) -> Any:
        return profile

    monkeypatch.setattr(oc, "current_version", current_version)
    monkeypatch.setattr(oc, "hydrated_profile", hydrated_profile)
    monkeypatch.setattr(oc, "save_version", save_version)
    monkeypatch.setattr("app.services.ctem_ticketing.target_problem", no_problem)
    monkeypatch.setattr("app.services.organization_context_draft.name_saved_pirs", named)
    return saved


def _get_body(records: list[dict[str, Any]], stored: dict[str, Any]) -> dict[str, Any]:
    """What ``GET`` answered: the stored version with the asset rows of the records."""
    profile = oc.parse_profile(stored).model_copy(
        update={"assets_exposure": [oc.AssetEntry.model_validate(r) for r in records]}
    )
    return profile.model_dump(mode="json")


@pytest.mark.asyncio
async def test_a_put_of_the_get_body_passes_after_a_sync_moved_last_seen(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {"assets_exposure": [_SYNCED], "owned_domains": []}
    records = [{**_SYNCED, "last_seen": "2026-11-24T06:00:00Z"}]
    saved = _store(monkeypatch, stored, records)
    body = _get_body(records, stored)
    body["owned_domains"] = ["example.com"]
    db: Any = _Db()
    await api.save_organization_context(
        api.SaveOrganizationContextRequest(profile=body, based_on_version=1),
        db=db,
        admin=SimpleNamespace(id=uuid.uuid4()),
    )
    assert db.committed and saved[-1].profile["owned_domains"] == ["example.com"]


@pytest.mark.asyncio
async def test_a_hand_edit_of_a_synced_field_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {"assets_exposure": [_SYNCED]}
    records = [{**_SYNCED, "last_seen": "2026-11-24T06:00:00Z"}]
    saved = _store(monkeypatch, stored, records)
    body = _get_body(records, stored)
    body["assets_exposure"][0]["name"] = "Renamed by hand"
    db: Any = _Db()
    with pytest.raises(HTTPException) as exc:
        await api.save_organization_context(
            api.SaveOrganizationContextRequest(profile=body, based_on_version=1),
            db=db,
            admin=SimpleNamespace(id=uuid.uuid4()),
        )
    assert exc.value.status_code == 422 and "kept in sync from intune" in exc.value.detail
    assert saved == [] and not db.committed


@pytest.mark.asyncio
async def test_a_patch_saves_its_fields_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    stored = {"assets_exposure": [_SYNCED], "owned_domains": ["a.example"]}
    records = [{**_SYNCED, "last_seen": "2026-11-24T06:00:00Z"}]
    saved = _store(monkeypatch, stored, records)
    db: Any = _Db()
    await api.patch_organization_context(
        api.PatchOrganizationContextRequest(fields={"owned_domains": ["a.example", "b.example"]}, based_on_version=1),
        db=db,
        admin=SimpleNamespace(id=uuid.uuid4()),
    )
    assert db.committed
    assert saved[-1].profile["owned_domains"] == ["a.example", "b.example"]
    # The asset list is the stored one, untouched: no row write follows from the patch.
    assert saved[-1].profile["assets_exposure"][0]["last_seen"] == _SYNCED["last_seen"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["assets_exposure", "asset_groups", "not_a_field"])
async def test_a_patch_refuses_rows_and_unknown_fields(monkeypatch: pytest.MonkeyPatch, field: str) -> None:
    saved = _store(monkeypatch, {}, [])
    db: Any = _Db()
    with pytest.raises(HTTPException) as exc:
        await api.patch_organization_context(
            api.PatchOrganizationContextRequest(fields={field: []}),
            db=db,
            admin=SimpleNamespace(id=uuid.uuid4()),
        )
    assert exc.value.status_code == 422 and saved == []


@pytest.mark.asyncio
async def test_a_stale_patch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    saved = _store(monkeypatch, {}, [])
    with pytest.raises(HTTPException) as exc:
        await api.patch_organization_context(
            api.PatchOrganizationContextRequest(fields={"owned_domains": ["x.example"]}, based_on_version=7),
            db=_Db(),
            admin=SimpleNamespace(id=uuid.uuid4()),
        )
    assert exc.value.status_code == 409 and saved == []
