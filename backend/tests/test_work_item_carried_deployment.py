"""An item blocked on a dev item waits for the deployment of that item's pull request (#9943).

A work item completes at its pull request's merge, before any build carries
it. The pure half: the reference the platform carries, its note, and the
closings it reacts to. The PostgreSQL half lives in
``test_work_item_dependencies_postgres.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.config import settings
from app.integrations.pull_request_events import PullRequestEvent
from app.services.work_item_blocked_refs import (
    apply_reference_event,
    carried_deployment_ref,
    carried_note,
    normalize_blocked_on_refs,
    pending_deployments,
)

NOW = datetime(2026, 11, 8, 3, 40, tzinfo=UTC)
REPO = "xtm-one-platform/xtm-one"
ITEM = "145e06f8-9134-47f5-98e3-903831b3b30e"


@pytest.fixture(autouse=True)
def _observed(monkeypatch):
    monkeypatch.setattr(settings, "work_item_deployment_repositories", "XTM-One-Platform/xtm-one", raising=False)


def test_an_observed_repository_carries_a_deployment_reference_naming_the_item():
    ref = carried_deployment_ref("XTM-One-Platform/xtm-one", 9944, ITEM)
    assert ref == {
        "repo": REPO,
        "kind": "deployment",
        "number": 9944,
        "release_on": "deployed",
        "carried_by": ITEM,
    }
    # It survives the normaliser every write goes through.
    assert normalize_blocked_on_refs([ref]) == [ref]


def test_a_repository_without_observed_deployments_carries_nothing():
    assert carried_deployment_ref("OpenCTI-Platform/opencti", 12, ITEM) is None


def test_the_note_names_the_pull_request_and_the_item():
    note = carried_note(carried_deployment_ref(REPO, 9944, ITEM), NOW)
    assert note.startswith(f"carried: waits for the deployment of {REPO}#9944, the pull request of work item {ITEM}")
    assert note.endswith("carried at 2026-11-08T03:40:00Z")


def test_the_merge_records_the_commit_and_the_reference_waits_for_a_build():
    refs = [carried_deployment_ref(REPO, 9944, ITEM)]
    event = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(9944,), merged=True, merge_sha="d458416e" + "0" * 32)
    out, released, refused = apply_reference_event(refs, event, NOW)
    assert refused == [] and released == [f"deployment of {REPO}#9944: merged d458416"]
    assert out[0]["merge_sha"].startswith("d458416e") and out[0]["carried_by"] == ITEM
    assert pending_deployments(out) == out


def test_a_carried_pull_request_closed_unmerged_is_dropped_not_surfaced():
    plain = normalize_blocked_on_refs([{"repo": REPO, "kind": "deployment", "number": 7, "release_on": "deployed"}])
    refs = [*plain, carried_deployment_ref(REPO, 9944, ITEM)]
    event = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(9944,), merged=False)
    out, released, refused = apply_reference_event(refs, event, NOW)
    assert refused == []
    assert out == plain
    assert released == [f"deployment of {REPO}#9944: closed unmerged, dropped (carried from work item {ITEM})"]

    # A reference the writer set by hand still surfaces the item.
    event = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(7,), merged=False)
    _, _, refused = apply_reference_event(plain, event, NOW)
    assert refused and "closed unmerged" in refused[0]
