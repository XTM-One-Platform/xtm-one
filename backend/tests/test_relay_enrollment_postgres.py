"""Concurrent relay enrollments into one group, on PostgreSQL.

Every enrollment into a group waits on the group row, whatever key it holds:
the machine match and the relay cap read the whole group, so two keys racing
must not both pass them.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.v1.relays import EnrollRequest, enroll_relay
from app.core.config import settings
from app.models.relay import Relay, RelayEnrollmentKey, RelayGroup
from app.models.user import User
from app.services.relays import generate_relay_enrollment_key, hash_key, key_display_prefix
from tests.scratch_database import scratch_database

_TABLES = [User.__table__, RelayGroup.__table__, Relay.__table__, RelayEnrollmentKey.__table__]


@pytest.fixture(autouse=True)
def _relays_on(monkeypatch):
    monkeypatch.setattr(settings, "relays_enabled", True)


async def _group_with_keys(db, *, keys: int, max_uses: int | None = None) -> tuple[uuid.UUID, list[str]]:
    group = RelayGroup(name="Paris", slug=f"paris-{uuid.uuid4().hex[:6]}", is_company_managed=True)
    db.add(group)
    await db.flush()
    raw_keys = []
    for n in range(keys):
        raw = generate_relay_enrollment_key()
        db.add(
            RelayEnrollmentKey(
                group_id=group.id,
                name=f"key {n}",
                key_prefix=key_display_prefix(raw),
                key_hash=await hash_key(raw),
                max_uses=max_uses,
            )
        )
        raw_keys.append(raw)
    await db.commit()
    return group.id, raw_keys


async def _enroll(scratch, raw_key: str, machine_id: str) -> int:
    async with scratch.sessions() as db:
        body = EnrollRequest(enrollment_key=raw_key, platform="linux", hostname=machine_id, machine_id=machine_id)
        try:
            await enroll_relay(body, db=db)
        except HTTPException as exc:
            return exc.status_code
        return 201


async def _relays_in(scratch, group_id: uuid.UUID) -> int:
    async with scratch.sessions() as db:
        return await db.scalar(select(func.count(Relay.id)).where(Relay.group_id == group_id, Relay.is_active.is_(True)))


@pytest.mark.asyncio
async def test_two_keys_racing_for_the_last_slot_of_a_group_admit_one_relay(monkeypatch):
    monkeypatch.setattr(settings, "relay_max_per_group", 1)
    async with scratch_database("relayenrollcap", tables=_TABLES) as scratch:
        async with scratch.sessions() as db:
            group_id, (first, second) = await _group_with_keys(db, keys=2)
        codes = await asyncio.gather(_enroll(scratch, first, "machine-a"), _enroll(scratch, second, "machine-b"))
        assert sorted(codes) == [201, 409]
        assert await _relays_in(scratch, group_id) == 1


@pytest.mark.asyncio
async def test_one_machine_enrolled_through_two_keys_at_once_is_one_relay(monkeypatch):
    monkeypatch.setattr(settings, "relay_max_per_group", 0)
    async with scratch_database("relayenrollmachine", tables=_TABLES) as scratch:
        async with scratch.sessions() as db:
            group_id, (first, second) = await _group_with_keys(db, keys=2)
        codes = await asyncio.gather(_enroll(scratch, first, "machine-a"), _enroll(scratch, second, "machine-a"))
        assert codes == [201, 201]
        assert await _relays_in(scratch, group_id) == 1


@pytest.mark.asyncio
async def test_a_single_use_key_used_twice_at_once_enrolls_one_relay(monkeypatch):
    monkeypatch.setattr(settings, "relay_max_per_group", 0)
    async with scratch_database("relayenrollonce", tables=_TABLES) as scratch:
        async with scratch.sessions() as db:
            group_id, (key,) = await _group_with_keys(db, keys=1, max_uses=1)
        codes = await asyncio.gather(_enroll(scratch, key, "machine-a"), _enroll(scratch, key, "machine-b"))
        assert sorted(codes) == [201, 401]
        assert await _relays_in(scratch, group_id) == 1
