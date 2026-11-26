# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""#2202: the per-exposure opt-in is read on the status and switched on the live exposure only."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException

from app.api.v1 import a2a_admin
from app.models.a2a import A2AExposure


class _Result:
    def scalar_one_or_none(self) -> None:
        return None


class _DB:
    def __init__(self) -> None:
        self.commits = 0

    async def execute(self, *_a: Any, **_k: Any) -> _Result:
        return _Result()

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"may": True, "exposure": None}
    agent = SimpleNamespace(id=uuid.uuid4())

    async def _load(_db: Any, _agent_id: uuid.UUID) -> Any:
        return agent

    async def _may(_db: Any, _user: Any, _agent: Any) -> bool:
        return bool(state["may"])

    async def _live(_db: Any, _agent_id: uuid.UUID) -> Any:
        return state["exposure"]

    monkeypatch.setattr(a2a_admin, "_load_agent", _load)
    monkeypatch.setattr(a2a_admin, "user_can_expose", _may)
    monkeypatch.setattr(a2a_admin, "get_live_exposure", _live)
    monkeypatch.setattr(a2a_admin, "external_base", lambda _r: "https://xtm.example")
    return state


async def _put(enabled: bool, db: _DB) -> a2a_admin.ExposureStatus:
    status: a2a_admin.ExposureStatus = await a2a_admin.set_partner_fleet_reads(
        uuid.uuid4(),
        a2a_admin.PartnerFleetReadsUpdate(enabled=enabled),
        cast(Any, None),
        cast(Any, db),
        cast(Any, None),
    )
    return status


@pytest.mark.asyncio
async def test_status_says_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(a2a_admin, "external_base", lambda _r: "https://xtm.example")
    exposure = A2AExposure(card_token="tok", run_as_user_id=uuid.uuid4())
    status = await a2a_admin._status_of(cast(Any, None), cast(Any, _DB()), exposure)
    assert status.partner_fleet_reads is False


@pytest.mark.asyncio
async def test_switch_on_then_off_reads_back(wired: dict[str, Any]) -> None:
    exposure = A2AExposure(card_token="tok", run_as_user_id=uuid.uuid4(), partner_fleet_reads=False)
    wired["exposure"] = exposure
    db = _DB()
    assert (await _put(True, db)).partner_fleet_reads is True
    assert exposure.partner_fleet_reads is True
    assert (await _put(False, db)).partner_fleet_reads is False
    assert db.commits == 2


@pytest.mark.asyncio
async def test_no_live_exposure_is_a_404_and_nothing_is_written(wired: dict[str, Any]) -> None:
    db = _DB()
    with pytest.raises(HTTPException) as exc:
        await _put(True, db)
    assert exc.value.status_code == 404
    assert db.commits == 0


@pytest.mark.asyncio
async def test_a_caller_who_may_not_expose_gets_the_same_404(wired: dict[str, Any]) -> None:
    wired["may"] = False
    wired["exposure"] = A2AExposure(card_token="tok", run_as_user_id=uuid.uuid4())
    db = _DB()
    with pytest.raises(HTTPException) as exc:
        await _put(True, db)
    assert exc.value.status_code == 404
    assert exc.value.detail == "Agent not found"
    assert db.commits == 0
