"""The run id a wake is handed reaches its claim, as text or as a UUID.

The worker schedules a wake with the run id of an orchestration payload, which
is text; the sweep resumes the runs it read from the database, which are UUIDs.
``safe_uuid`` reads text only, so a wake that parsed a UUID again claimed
nothing and no conversation was ever told. The claim itself is SQL, pinned
against PostgreSQL in ``test_assignment_run_wake_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services import assignment_run_wake as wake

RUN = uuid.UUID("33333333-3333-4333-8333-333333333333")


@pytest.fixture
def claimed(monkeypatch) -> list[uuid.UUID]:
    seen: list[uuid.UUID] = []

    async def _claim(run_id):
        seen.append(run_id)
        return None

    monkeypatch.setattr(wake, "_claim", _claim)
    return seen


@pytest.mark.parametrize("given", [RUN, str(RUN), str(RUN).upper()])
async def test_a_run_id_reaches_the_claim_as_text_or_as_a_uuid(claimed, given):
    assert await wake.wake_owed_conversation(given) is False
    assert claimed == [RUN]


async def test_a_scheduled_wake_reaches_the_claim(monkeypatch, claimed):
    monkeypatch.setattr(wake, "WAKE_GRACE_SECONDS", 0)
    wake.schedule(str(RUN))
    await asyncio.gather(*list(wake._WAKES))
    assert claimed == [RUN]


async def test_a_swept_wake_reaches_the_claim(claimed):
    await wake._wake_safely(RUN)
    assert claimed == [RUN]


async def test_what_is_no_run_id_is_never_claimed(claimed):
    assert await wake.wake_owed_conversation("anonymous") is False
    assert await wake.wake_owed_conversation(None) is False
    pending = set(wake._WAKES)
    wake.schedule(None)
    wake.schedule("anonymous")
    assert set(wake._WAKES) == pending
    assert claimed == []
