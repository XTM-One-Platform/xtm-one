"""Bulk fleet control (#9769 S3): ``POST /fleets/bulk``.

Pause all, Resume or set the autonomy level on a selection of fleets, each fleet
under its own single-fleet rule (a refused fleet is reported, the others apply);
``all_fleets`` is the administrators' every-fleet Pause switch; ``dry_run``
counts for the confirmation dialog without writing.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api.v1 import fleets as fleets_api
from app.api.v1.schemas.fleet import FleetBulkAction
from app.services.fleet_control import FleetControlOutcome
from app.services.fleets import FleetActionRefusedError


def _user(*, admin: bool):
    return SimpleNamespace(id=uuid.uuid4(), email="u@example.com", full_name="U", is_admin=admin)


def _fleet(name: str, level: str = "full_gates", *, builtin: bool = False):
    return SimpleNamespace(id=uuid.uuid4(), name=name, autonomy_level=level, is_builtin=builtin, is_archived=False)


def _db():
    return SimpleNamespace(commit=AsyncMock(), execute=AsyncMock())


async def _run(body: FleetBulkAction, user, fleets: dict, *, db=None):
    db = db or _db()

    async def lookup(_db, _user, fleet_id, *args, **kwargs):
        fleet = fleets.get(fleet_id)
        if fleet is None:
            raise HTTPException(status_code=404, detail="Fleet not found")
        return fleet

    with (
        patch.object(fleets_api, "_controllable_fleet", AsyncMock(side_effect=lookup)),
        patch.object(fleets_api, "_manageable_fleet", AsyncMock(side_effect=lookup)),
        patch.object(fleets_api, "log_audit", AsyncMock()) as audit,
    ):
        response = await fleets_api.bulk_fleet_action(body, SimpleNamespace(), user=user, db=db)
    return response, audit, db


@pytest.mark.asyncio
async def test_bulk_pause_applies_to_each_fleet_and_reports_the_refused_one():
    a, b = _fleet("A"), _fleet("B")
    fleets = {str(a.id): a, str(b.id): b}
    body = FleetBulkAction(action="pause", fleet_ids=[str(a.id), "missing", str(b.id)])
    pause = AsyncMock(side_effect=[FleetControlOutcome(2, ("x", "y")), FleetControlOutcome(0, ())])
    with patch.object(fleets_api, "pause_fleet", pause):
        response, audit, db = await _run(body, _user(admin=True), fleets)
    assert response.changed == 2
    assert response.refused == 1
    assert [o.ok for o in response.fleets] == [True, False, True]
    assert response.fleets[1].detail == "Fleet not found"
    # One fleet row per fleet that changed, flagged bulk.
    assert audit.await_count == 1
    assert audit.await_args.kwargs["details"]["bulk"] is True
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_dry_run_counts_without_writing():
    a = _fleet("A")
    body = FleetBulkAction(action="resume", fleet_ids=[str(a.id)], dry_run=True)
    with (
        patch.object(fleets_api, "resumable_count", AsyncMock(return_value=3)),
        patch.object(fleets_api, "resume_fleet", AsyncMock()) as resume,
    ):
        response, audit, db = await _run(body, _user(admin=True), {str(a.id): a})
    assert response.dry_run and response.changed == 3
    resume.assert_not_awaited()
    audit.assert_not_awaited()
    # The route commits before answering on every path; a dry run has nothing pending.
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_bulk_raise_is_refused_for_a_non_admin_and_a_lower_passes():
    high, low = _fleet("High", "autonomous"), _fleet("Low", "full_gates")
    fleets = {str(high.id): high, str(low.id): low}
    body = FleetBulkAction(action="set_autonomy", fleet_ids=[str(high.id), str(low.id)], autonomy_level="medium")
    response, audit, _ = await _run(body, _user(admin=False), fleets)
    by_name = {o.name: o for o in response.fleets}
    assert by_name["High"].ok and by_name["High"].changed == 1
    assert high.autonomy_level == "medium"
    assert not by_name["Low"].ok
    assert low.autonomy_level == "full_gates"
    assert audit.await_args.kwargs["action"] == "autonomy_level_change"
    assert audit.await_args.kwargs["details"] == {"from": "autonomous", "to": "medium", "bulk": True}


@pytest.mark.asyncio
async def test_unchanged_level_is_not_audited():
    a = _fleet("A", "low")
    body = FleetBulkAction(action="set_autonomy", fleet_ids=[str(a.id)], autonomy_level="low")
    response, audit, _ = await _run(body, _user(admin=True), {str(a.id): a})
    assert response.changed == 0 and response.refused == 0
    audit.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_fleet_switch_is_admin_only_and_pause_only():
    with pytest.raises(HTTPException) as refused:
        await _run(FleetBulkAction(action="pause", all_fleets=True), _user(admin=False), {})
    assert refused.value.status_code == 403
    with pytest.raises(HTTPException) as refused:
        await _run(FleetBulkAction(action="resume", all_fleets=True), _user(admin=True), {})
    assert refused.value.status_code == 422


@pytest.mark.asyncio
async def test_every_fleet_switch_pauses_every_live_fleet():
    a, b = _fleet("A"), _fleet("B", builtin=True)
    db = _db()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [a.id, b.id]))
    )
    pause = AsyncMock(return_value=FleetControlOutcome(1, ("x",)))
    with patch.object(fleets_api, "pause_fleet", pause):
        response, audit, _ = await _run(
            FleetBulkAction(action="pause", all_fleets=True), _user(admin=True), {str(a.id): a, str(b.id): b}, db=db
        )
    assert response.changed == 2 and response.refused == 0
    assert pause.await_count == 2
    assert audit.await_args.kwargs["details"]["all_fleets"] is True


@pytest.mark.asyncio
async def test_empty_selection_and_missing_level_are_refused():
    with pytest.raises(HTTPException) as refused:
        await _run(FleetBulkAction(action="pause"), _user(admin=True), {})
    assert refused.value.status_code == 422
    with pytest.raises(HTTPException) as refused:
        await _run(FleetBulkAction(action="set_autonomy", fleet_ids=["x"]), _user(admin=True), {})
    assert refused.value.status_code == 422


@pytest.mark.asyncio
async def test_resume_refused_during_a_stop_is_reported_per_fleet():
    a = _fleet("A")
    with patch.object(fleets_api, "resume_fleet", AsyncMock(side_effect=FleetActionRefusedError(409, "stop"))):
        response, _, _ = await _run(
            FleetBulkAction(action="resume", fleet_ids=[str(a.id)]), _user(admin=True), {str(a.id): a}
        )
    assert response.refused == 1 and response.fleets[0].detail == "stop"
