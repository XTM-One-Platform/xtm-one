"""A schedule update that omits the cron, against a real PostgreSQL.

``PUT /assignments/{id}`` replaces ``trigger_config`` wholesale and the request
schema cannot see the stored row, so a new config without a ``cron`` on a
scheduled assignment used to drop the cron: the scheduler skips a schedule with
no cron silently, and it never ran again. The route now carries the stored cron
forward (every other key as sent), so the request still succeeds and the
schedule keeps running. The owner here is an ordinary (non-admin) user. Same
scratch-database recipe as ``test_provisioning_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.models.assignment import Assignment
from app.services import autonomous
from tests.test_assignment_update_explicit_null_postgres import _put, _seed, _stored
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration

_CRON = "0 9 * * 1"
_SCHEDULE = {"trigger_type": "schedule", "trigger_config": {"cron": _CRON}}


async def _scheduler_fires(sessionmaker, monkeypatch) -> list[object]:
    """Run the real scheduler tick; return the cron of every run it triggers."""
    fired: list[object] = []

    async def _trigger(**kwargs):
        fired.append(kwargs["trigger_context"]["cron"])
        return uuid.uuid4()

    async def _no_active_run(_db, _assignment_id):
        return False

    monkeypatch.setattr(autonomous, "trigger_assignment", _trigger)
    monkeypatch.setattr(autonomous, "has_active_run", _no_active_run)
    async with sessionmaker() as db:
        await autonomous._process_scheduled_assignments(db)
    return fired


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"trigger_config": {"timezone": "UTC"}},
        # An explicit null type is "no change", like an omitted one.
        {"trigger_type": None, "trigger_config": {"timezone": "UTC"}},
    ],
)
async def test_a_timezone_only_update_keeps_the_cron_and_the_schedule_runs(monkeypatch, payload):
    async with _temp_database("asg_sched") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker, **_SCHEDULE)
        assert not user.is_admin

        await _put(sessionmaker, user, assignment_id, payload)

        stored = await _stored(sessionmaker, assignment_id)
        assert (stored.trigger_type, stored.trigger_config) == (
            "schedule",
            {"timezone": "UTC", "cron": _CRON},
        )
        assert await _scheduler_fires(sessionmaker, monkeypatch) == [_CRON]


@pytest.mark.asyncio
async def test_the_two_step_clear_succeeds_and_ends_as_a_manual_assignment(monkeypatch):
    async with _temp_database("asg_sched") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker, **_SCHEDULE)

        await _put(sessionmaker, user, assignment_id, {"trigger_config": {}})
        stored = await _stored(sessionmaker, assignment_id)
        assert (stored.trigger_type, stored.trigger_config) == ("schedule", {"cron": _CRON})

        await _put(sessionmaker, user, assignment_id, {"trigger_type": "manual"})
        stored = await _stored(sessionmaker, assignment_id)
        assert stored.trigger_type == "manual"
        # The key a type-only switch has always left behind: never read for a
        # non-schedule assignment, so the scheduler does not pick it up.
        assert stored.trigger_config == {"cron": _CRON}
        assert await _scheduler_fires(sessionmaker, monkeypatch) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"trigger_config": {"cron": "nope"}},
        {"name": "Renamed", "trigger_config": {"cron": "*/15 * * 1"}},
        {"trigger_type": "schedule", "trigger_config": {"cron": ""}},
        # A cron key sent null or empty is a value, never "keep the stored one".
        {"trigger_config": {"cron": None}},
        {"trigger_config": {"timezone": "UTC", "cron": ""}},
    ],
)
async def test_an_explicit_invalid_cron_is_still_refused_and_nothing_is_saved(payload):
    async with _temp_database("asg_sched") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker, **_SCHEDULE)

        with pytest.raises(ValidationError, match="cron"):
            await _put(sessionmaker, user, assignment_id, payload)

        stored = await _stored(sessionmaker, assignment_id)
        assert (stored.name, stored.trigger_config) == ("Digest", {"cron": _CRON})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{"trigger_type": "schedule"}, {"trigger_type": "schedule", "trigger_config": {"timezone": "UTC"}}],
)
async def test_switching_to_schedule_without_a_cron_keeps_the_answer_of_main(payload):
    """Nothing to carry from a manual assignment: refused by the request schema,
    as before this change."""
    async with _temp_database("asg_sched") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker)

        with pytest.raises(ValidationError, match="cron"):
            await _put(sessionmaker, user, assignment_id, payload)

        stored = await _stored(sessionmaker, assignment_id)
        assert (stored.trigger_type, stored.trigger_config) == ("manual", {})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "payload", "saved_type", "saved_config"),
    [
        # A new valid cron replaces the stored one.
        (_SCHEDULE, {"trigger_config": {"cron": "30 6 * * *"}}, "schedule", {"cron": "30 6 * * *"}),
        # Edits without trigger fields.
        (_SCHEDULE, {"name": "Renamed"}, "schedule", {"cron": _CRON}),
        (_SCHEDULE, {"enabled": False}, "schedule", {"cron": _CRON}),
        # Leaving schedule in the same call: nothing is carried. Any explicit
        # type is the one saved, an empty or unknown one included, as the
        # create route saves it.
        (_SCHEDULE, {"trigger_type": "manual", "trigger_config": {}}, "manual", {}),
        (_SCHEDULE, {"trigger_type": "", "trigger_config": {"timezone": "UTC"}},
         "", {"timezone": "UTC"}),
        (_SCHEDULE, {"trigger_type": "custom", "trigger_config": {"timezone": "UTC"}},
         "custom", {"timezone": "UTC"}),
        # A stored null cron is no cron: nothing to carry.
        ({"trigger_type": "schedule", "trigger_config": {"cron": None}},
         {"trigger_config": {"timezone": "UTC"}}, "schedule", {"timezone": "UTC"}),
        # A non-schedule assignment's config is saved as sent.
        ({"trigger_type": "manual", "trigger_config": {}}, {"trigger_config": {"note": "x"}},
         "manual", {"note": "x"}),
        # A legacy schedule whose cron is broken keeps it (the scheduler pauses
        # it visibly) instead of dropping it into a silent skip.
        ({"trigger_type": "schedule", "trigger_config": {"cron": "*/15 * * 1"}},
         {"trigger_config": {"timezone": "UTC"}}, "schedule", {"timezone": "UTC", "cron": "*/15 * * 1"}),
    ],
)
async def test_other_edits_are_saved_as_before(stored, payload, saved_type, saved_config):
    async with _temp_database("asg_sched") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker, **stored)

        await _put(sessionmaker, user, assignment_id, payload)

        row = await _stored(sessionmaker, assignment_id)
        assert (row.trigger_type, row.trigger_config) == (saved_type, saved_config)


@pytest.mark.asyncio
async def test_a_concurrent_switch_to_schedule_is_seen_before_the_carry_forward():
    """Another writer holds the row while switching it to a schedule. The route's
    config without a cron must carry that committed cron, not read the manual
    snapshot it would have seen without the row lock and drop the cron."""
    async with _temp_database("asg_sched") as sessionmaker:
        user, assignment_id = await _seed(sessionmaker)

        async with sessionmaker() as other:
            row = (
                await other.execute(
                    select(Assignment).where(Assignment.id == assignment_id).with_for_update()
                )
            ).scalar_one()
            row.trigger_type = "schedule"
            row.trigger_config = {"cron": _CRON}
            await other.flush()
            put = asyncio.create_task(
                _put(sessionmaker, user, assignment_id, {"trigger_config": {"timezone": "UTC"}})
            )
            await asyncio.sleep(1)
            assert not put.done()
            await other.commit()

        await asyncio.wait_for(put, timeout=30)

        stored = await _stored(sessionmaker, assignment_id)
        assert (stored.trigger_type, stored.trigger_config) == (
            "schedule",
            {"timezone": "UTC", "cron": _CRON},
        )


@pytest.mark.asyncio
async def test_a_webhook_config_edit_still_mints_its_secret():
    async with _temp_database("asg_sched") as sessionmaker:
        user, assignment_id = await _seed(
            sessionmaker, trigger_type="webhook", trigger_config={"path": "hook"}
        )

        await _put(sessionmaker, user, assignment_id, {"trigger_config": {"path": "other"}})

        config = (await _stored(sessionmaker, assignment_id)).trigger_config
        assert config["path"] == "other" and config["webhook_secret"]
