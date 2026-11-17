"""An item created ``claimed`` or ``in_progress`` is held by its creator's run.

Before, such an item recorded no claim at all: a sibling run read no holder
and judged the creating run ended, even while that run was only between
workers (handed back on a deployment, resumed with the same run id).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

import app.services.work_item as wi_service
from app.services.work_item import WorkItemOrigin, creator_claim_columns

NOW = datetime(2026, 11, 5, 6, 40, tzinfo=UTC)
AGENT = uuid.uuid4()
RUN = uuid.uuid4()


@pytest.mark.parametrize("status", ["claimed", "in_progress"])
def test_a_held_status_records_the_creator_and_its_run(status):
    columns = creator_claim_columns(status, AGENT, WorkItemOrigin(run_id=str(RUN)), NOW)
    assert columns == {"claimed_by_agent_id": AGENT, "claimed_by_run_id": RUN, "claimed_at": NOW}


@pytest.mark.parametrize("status", ["open", "blocked", "done", "cancelled"])
def test_any_other_status_records_no_claim(status):
    assert creator_claim_columns(status, AGENT, WorkItemOrigin(run_id=str(RUN)), NOW) == {}


def test_no_creator_agent_records_no_claim():
    assert creator_claim_columns("in_progress", None, WorkItemOrigin(run_id=str(RUN)), NOW) == {}


def test_outside_a_run_the_background_task_holds_it():
    task = uuid.uuid4()
    with wi_service.claims_held_by_background_task(task):
        columns = creator_claim_columns("in_progress", AGENT, None, NOW)
    assert columns["claimed_by_run_id"] == task


def test_a_chat_turn_records_the_agent_and_no_run():
    columns = creator_claim_columns("in_progress", AGENT, WorkItemOrigin(conversation_id=str(uuid.uuid4())), NOW)
    assert columns["claimed_by_agent_id"] == AGENT
    assert columns["claimed_by_run_id"] is None


def test_a_handed_back_run_reads_as_a_live_holder():
    # A run handed back by a stopping worker stays ``running`` until the next
    # worker resumes it under the same id: its claim stands meanwhile.
    holder = wi_service.HoldingRun(status="running", ended_at=None, kind="run")
    item = type("Item", (), {"status": "in_progress", "claimed_by_run_id": RUN})()
    assert wi_service.held_by_live_run(item, {RUN: holder})
