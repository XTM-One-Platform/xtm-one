"""An item an inbox run takes out of ``blocked`` keeps what it waited for.

Two holes left Developer items blocked or in progress with nothing that would
ever move them (fleet watchdog, 7 Nov 2026):

- a run set a blocked item ``in_progress``: the write cleared its
  ``blocked_on_refs``, and when the run ended without completing it at the
  dispatch cap the item was parked "No release condition";
- a run that did not hold the item (a red-check or green-check run) set it
  ``in_progress``: no run held it, so no run end released it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import app.services.work_item as wi_service

PR_REF = {"repo": "owner/name", "kind": "pull_request", "number": 9581, "release_on": "merged_or_closed"}


def test_stash_keeps_only_open_pull_request_refs():
    issue = {"repo": "owner/name", "kind": "issue", "number": 3, "release_on": "closed"}
    out = wi_service.stash_resume_wait_refs({"a": 1}, [PR_REF, issue])
    assert out["a"] == 1
    assert [r["number"] for r in out[wi_service.RESUME_WAIT_REFS_KEY]] == [9581]


def test_stash_with_no_pull_request_drops_an_earlier_one():
    payload = {wi_service.RESUME_WAIT_REFS_KEY: [PR_REF]}
    assert wi_service.RESUME_WAIT_REFS_KEY not in wi_service.stash_resume_wait_refs(payload, None)


def _released_item(payload):
    return SimpleNamespace(
        payload=payload,
        claim_notes=None,
        status="in_progress",
        blocked_on=None,
        blocked_on_refs=None,
        blocked_reason=None,
    )


def test_release_at_the_cap_parks_on_the_kept_pull_request(monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 1)
    item = _released_item({wi_service.RESUME_WAIT_REFS_KEY: [PR_REF]})
    exhausted = wi_service._release_unfinished(item, run_id=uuid.uuid4(), reason=None, now=datetime.now(UTC))
    assert exhausted and item.status == "blocked"
    assert [r["number"] for r in item.blocked_on_refs] == [9581]
    assert "pull_request owner/name#9581" in item.blocked_reason
    assert "No release condition" not in item.blocked_reason


def test_release_at_the_cap_without_a_kept_wait_is_unchanged(monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 1)
    item = _released_item({})
    wi_service._release_unfinished(item, run_id=uuid.uuid4(), reason=None, now=datetime.now(UTC))
    assert item.blocked_on_refs is None and "No release condition" in item.blocked_reason


def _writer(agent_id):
    return SimpleNamespace(agent_id=agent_id)


def _unheld(**over):
    base = dict(claimed_by_agent_id=None, claimed_by_run_id=None, claimed_at=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_a_run_moving_an_unheld_item_in_progress_holds_it():
    agent, run = uuid.uuid4(), uuid.uuid4()
    item = _unheld()
    now = datetime.now(UTC)
    wi_service._hold_by_writer_run(item, "in_progress", writer=_writer(agent), writer_run_ids=(run,), now=now)
    assert item.claimed_by_agent_id == agent and item.claimed_by_run_id == run and item.claimed_at == now


def test_a_claim_of_another_run_or_agent_is_never_taken_over():
    agent, other = uuid.uuid4(), uuid.uuid4()
    held = _unheld(claimed_by_run_id=other, claimed_by_agent_id=agent)
    wi_service._hold_by_writer_run(
        held, "in_progress", writer=_writer(agent), writer_run_ids=(uuid.uuid4(),), now=datetime.now(UTC)
    )
    assert held.claimed_by_run_id == other
    foreign = _unheld(claimed_by_agent_id=other)
    wi_service._hold_by_writer_run(
        foreign, "claimed", writer=_writer(agent), writer_run_ids=(uuid.uuid4(),), now=datetime.now(UTC)
    )
    assert foreign.claimed_by_run_id is None and foreign.claimed_by_agent_id == other


def test_no_run_or_another_status_takes_no_hold():
    agent = uuid.uuid4()
    person = _unheld()
    wi_service._hold_by_writer_run(
        person, "in_progress", writer=_writer(agent), writer_run_ids=None, now=datetime.now(UTC)
    )
    assert person.claimed_by_run_id is None
    blocked = _unheld()
    wi_service._hold_by_writer_run(
        blocked, "blocked", writer=_writer(agent), writer_run_ids=(uuid.uuid4(),), now=datetime.now(UTC)
    )
    assert blocked.claimed_by_run_id is None
