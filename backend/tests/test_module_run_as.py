"""Who XTM One acts as in OpenCTI, OpenAEV and OpenCRQ (#2650): ``services/module_run_as``."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.services import module_run_as
from app.services import organization_context_sync as osync


def _user(*, admin: bool = False, active: bool = True, name: str = "Ada"):
    return SimpleNamespace(
        id=uuid.uuid4(), full_name=name, email=f"{name.lower()}@example.com", is_admin=admin, is_active=active
    )


class _Db:
    def __init__(self, *users):
        self.users = {u.id: u for u in users}

    async def get(self, _model, key):
        return self.users.get(key)


@pytest.mark.asyncio
async def test_a_missing_choice_is_refused_naming_the_module():
    actor = _user(admin=True)
    with pytest.raises(module_run_as.RunAsRefusedError) as refused:
        await module_run_as.resolve(_Db(actor), actor=actor, raw=None, providers=["openaev"])
    assert refused.value.status == 400 and "OpenAEV" in str(refused.value)


@pytest.mark.asyncio
async def test_a_person_who_is_not_an_administrator_only_chooses_themself():
    actor, other = _user(), _user(name="Bob")
    db = _Db(actor, other)
    with patch("app.services.fleet_run_as.missing_providers", AsyncMock(return_value=[])):
        assert await module_run_as.resolve(db, actor=actor, raw=str(actor.id), providers=["opencti"]) == actor.id
        with pytest.raises(module_run_as.RunAsRefusedError) as refused:
            await module_run_as.resolve(db, actor=actor, raw=str(other.id), providers=["opencti"])
    assert refused.value.status == 403


@pytest.mark.asyncio
async def test_an_administrator_picks_an_active_user_with_the_module_connected():
    admin, other, gone = _user(admin=True), _user(name="Bob"), _user(name="Gone", active=False)
    db = _Db(admin, other, gone)
    with patch("app.services.fleet_run_as.missing_providers", AsyncMock(return_value=[])):
        assert await module_run_as.resolve(db, actor=admin, raw=str(other.id), providers=["opencti"]) == other.id
        with pytest.raises(module_run_as.RunAsRefusedError) as inactive:
            await module_run_as.resolve(db, actor=admin, raw=str(gone.id), providers=["opencti"])
    assert inactive.value.status == 422
    with (
        patch("app.services.fleet_run_as.missing_providers", AsyncMock(return_value=["opencti"])),
        pytest.raises(module_run_as.RunAsRefusedError) as unconnected,
    ):
        await module_run_as.resolve(db, actor=admin, raw=str(other.id), providers=["opencti"])
    assert unconnected.value.status == 400


@pytest.mark.asyncio
async def test_candidates_list_the_caller_first_and_the_unconnected_disabled():
    admin, bob, carl = _user(admin=True, name="Zed"), _user(name="Bob"), _user(name="Carl")

    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return [bob, carl, admin]

    db = SimpleNamespace(execute=AsyncMock(return_value=_Rows()))

    async def _missing(_db, user_id, providers):
        return providers if user_id == carl.id else []

    with patch("app.services.fleet_run_as.missing_providers", _missing):
        rows = await module_run_as.candidates(db, actor=admin, provider="opencti")
    assert [r.id for r in rows] == [admin.id, bob.id, carl.id]
    assert rows[0].is_self and rows[2].selectable is False and rows[2].reason == module_run_as.NO_INTEGRATION

    with patch("app.services.fleet_run_as.missing_providers", AsyncMock(return_value=[])):
        own = await module_run_as.candidates(db, actor=bob, provider="opencti")
    assert [r.id for r in own] == [bob.id]
    with pytest.raises(module_run_as.RunAsRefusedError):
        await module_run_as.candidates(db, actor=admin, provider="github")


@pytest.mark.asyncio
async def test_a_module_sync_reads_as_its_chosen_user_and_never_falls_back():
    member = _user(name="Bob")
    db = _Db(member)
    sync = SimpleNamespace(source="opencti", owner_user_id=member.id)
    assert await osync._owner(db, sync) == (member, "")
    unset = SimpleNamespace(source="openaev", owner_user_id=None)
    owner, reason = await osync._owner(db, unset)
    assert owner is None and reason == "Choose who this sync runs as."
    # Any other source keeps the administrator rule.
    other = SimpleNamespace(source="csv", owner_user_id=member.id)
    owner, reason = await osync._owner(db, other)
    assert owner is None and "no longer an administrator" in reason
