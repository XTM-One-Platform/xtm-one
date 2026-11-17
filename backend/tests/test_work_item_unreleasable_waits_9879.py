"""A wait the platform can never release is refused, and stale means "needs a look" (#9879).

* A ``blocked_on_refs`` entry of kind ``deployment`` is released when a running
  build carries its merge: only the repositories the platform's builds come from
  (``work_item_deployment_repositories``) can satisfy it, so one on any other
  repository is refused at write with the alternatives named.
* A blocked item waiting on watched references gets the longer
  ``work_item_stale_ref_wait_days`` window, an item gated by a ``not_before``
  ahead is never stale on inactivity, and one left waiting on an unobserved
  deployment (written before the refusal) is always stale, with that reason.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.services.company_managed_writes import agent_writer
from app.services.work_item_blocked_refs import (
    BlockedRefError,
    normalize_blocked_on_refs,
    refuse_unobserved_deployment_refs,
)

NOW = datetime(2026, 11, 7, 22, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_deployment_repositories", "XTM-One-Platform/xtm-one")
    monkeypatch.setattr(wi_service.settings, "work_item_stale_after_hours", 24)
    monkeypatch.setattr(wi_service.settings, "work_item_stale_ref_wait_days", 7)


def _item(**overrides):
    base = dict(
        id=uuid.uuid4(),
        status="blocked",
        due_at=None,
        not_before=None,
        blocked_on=None,
        blocked_on_refs=None,
        action_mode=None,
        claimed_by_run_id=None,
        last_activity_at=NOW - timedelta(days=2),
        created_at=NOW - timedelta(days=3),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _deployment(repo: str, **extra) -> list[dict]:
    return normalize_blocked_on_refs([{"repo": repo, "kind": "deployment", "number": 12, **extra}])


# -- The write-time refusal --------------------------------------------------------


def test_a_deployment_on_an_unobserved_repository_is_refused_with_the_alternatives():
    with pytest.raises(BlockedRefError) as err:
        refuse_unobserved_deployment_refs(_deployment("FiligranHQ/platform"))
    message = str(err.value)
    assert "filigranhq/platform" in message
    assert "workflow_run" in message
    assert "not_before" in message


def test_a_deployment_on_the_platform_repository_is_accepted_whatever_the_case():
    refuse_unobserved_deployment_refs(_deployment("xtm-one-platform/XTM-ONE"))


def test_other_reference_kinds_and_a_satisfied_deployment_are_untouched():
    refs = normalize_blocked_on_refs(
        [
            {"repo": "FiligranHQ/platform", "kind": "pull_request", "number": 3},
            {"repo": "FiligranHQ/platform", "kind": "workflow_run", "workflow": "release.yml"},
            {"repo": "FiligranHQ/platform", "kind": "deployment", "number": 4, "state": "deployed"},
        ]
    )
    refuse_unobserved_deployment_refs(refs)


@pytest.mark.asyncio
async def test_create_work_item_refuses_the_unobserved_deployment_before_any_write():
    with pytest.raises(BlockedRefError):
        await wi_service.create_work_item(
            user_id=str(uuid.uuid4()),
            title="Waits on the platform chart",
            writer=agent_writer(is_company_managed=False),
            blocked_on_refs=[{"repo": "FiligranHQ/platform", "kind": "deployment", "number": 12}],
        )


# -- Stale means "needs a look" ------------------------------------------------------


def test_a_blocked_item_waiting_on_an_open_pull_request_is_not_stale_within_the_ref_window():
    refs = normalize_blocked_on_refs([{"repo": "o/r", "kind": "pull_request", "number": 5}])
    item = _item(blocked_on_refs=refs, last_activity_at=NOW - timedelta(days=3))
    assert wi_service.work_item_stale_reason(item, NOW) is None
    assert wi_service.is_work_item_stale(item, NOW) is False


def test_a_reference_wait_quiet_past_the_ref_window_is_stale_with_its_reason():
    refs = normalize_blocked_on_refs([{"repo": "o/r", "kind": "pull_request", "number": 5}])
    item = _item(blocked_on_refs=refs, last_activity_at=NOW - timedelta(days=8))
    assert wi_service.work_item_stale_reason(item, NOW) == "ref_wait_quiet"


def test_an_item_gated_on_a_future_date_is_never_stale_on_inactivity():
    item = _item(
        blocked_reason="re-check the chart next month",
        not_before=NOW + timedelta(days=20),
        last_activity_at=NOW - timedelta(days=10),
    )
    assert wi_service.work_item_stale_reason(item, NOW) is None


def test_a_past_due_date_still_wins_over_the_gate():
    item = _item(not_before=NOW + timedelta(days=1), due_at=NOW - timedelta(hours=1))
    assert wi_service.work_item_stale_reason(item, NOW) == "past_due"


def test_a_legacy_wait_on_an_unobserved_deployment_is_stale_at_once():
    item = _item(blocked_on_refs=_deployment("FiligranHQ/platform"), last_activity_at=NOW - timedelta(minutes=5))
    assert wi_service.work_item_stale_reason(item, NOW) == "wait_never_releases"


def test_a_quiet_claim_keeps_the_hours_window():
    item = _item(status="claimed", last_activity_at=NOW - timedelta(hours=30))
    assert wi_service.work_item_stale_reason(item, NOW) == "quiet"


def test_the_ref_window_disabled_falls_back_to_the_hours_window(monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_stale_ref_wait_days", 0)
    refs = normalize_blocked_on_refs([{"repo": "o/r", "kind": "pull_request", "number": 5}])
    item = _item(blocked_on_refs=refs, last_activity_at=NOW - timedelta(days=2))
    assert wi_service.work_item_stale_reason(item, NOW) == "quiet"


def test_the_serialized_item_names_its_stale_reason():
    refs = _deployment("FiligranHQ/platform")
    item = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        owner_agent_id=None,
        created_by_agent_id=None,
        title="t",
        description=None,
        payload={},
        status="blocked",
        priority="medium",
        correlation_key=None,
        due_at=None,
        not_before=None,
        claimed_by_agent_id=None,
        claimed_at=None,
        source="agent",
        result=None,
        blocked_on=None,
        blocked_on_refs=refs,
        action_mode=None,
        last_activity_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    data = wi_service.serialize_work_item(item, now=NOW)
    assert data["is_stale"] is True
    assert data["stale_reason"] == "wait_never_releases"


def test_the_stale_sql_clause_carries_the_gate_the_ref_window_and_the_unobserved_deployment():
    sql = str(wi_service._stale_sql_clause(NOW).compile(dialect=postgresql.dialect()))
    assert "not_before" in sql
    assert "blocked_on_refs" in sql
