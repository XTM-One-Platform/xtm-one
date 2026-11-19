"""An item released again and again by runs that did not complete it is not re-run at once.

Three inbox runs took one Developer item within two minutes: each ended with the
item in progress, no pull request named on it, so the run end released it to
``open`` and the inbox dispatched it again at once (gap
inbox-redispatch-in-progress-item-loop, 18 Nov 2026). From the second release in a
row the item waits, doubling up to a cap, and the release note tells the next run
how to name the pull request it waits on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import app.services.work_item as wi_service


def _item(payload=None):
    return SimpleNamespace(
        payload=payload or {},
        claim_notes=None,
        status="in_progress",
        blocked_on=None,
        blocked_on_refs=None,
        blocked_reason=None,
        not_before=None,
    )


def test_backoff_doubles_from_the_second_release_and_is_capped():
    assert wi_service.release_backoff(1) == timedelta(0)
    assert wi_service.release_backoff(2) == wi_service.RELEASE_BACKOFF_BASE
    assert wi_service.release_backoff(3) == wi_service.RELEASE_BACKOFF_BASE * 2
    assert wi_service.release_backoff(50) == wi_service.RELEASE_BACKOFF_MAX


def test_first_release_goes_back_at_once_with_the_hint(monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 5)
    item = _item()
    now = datetime.now(UTC)
    assert wi_service._release_unfinished(item, run_id=uuid.uuid4(), reason=None, now=now, backoff=True) is False
    assert item.status == "open" and item.not_before is None
    assert "names no pull request" in item.claim_notes[-1]["text"]
    assert "create_github_pr(work_item_id=" in item.claim_notes[-1]["text"]


def test_second_release_in_a_row_is_deferred(monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 5)
    item = _item({"_dispatch_attempts": 1})
    now = datetime.now(UTC)
    wi_service._release_unfinished(item, run_id=uuid.uuid4(), reason=None, now=now, backoff=True)
    assert item.status == "open"
    assert item.not_before == now + wi_service.RELEASE_BACKOFF_BASE
    assert "release 2 in a row" in item.claim_notes[-1]["text"]


def test_without_backoff_the_release_is_unchanged(monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 5)
    item = _item({"_dispatch_attempts": 3})
    wi_service._release_unfinished(item, run_id=uuid.uuid4(), reason=None, now=datetime.now(UTC))
    assert item.status == "open" and item.not_before is None
    assert "names no pull request" not in item.claim_notes[-1]["text"]


def test_progress_restarts_the_count_without_a_deferral(monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 5)
    item = _item({"_dispatch_attempts": 4})
    wi_service._release_unfinished(
        item, run_id=uuid.uuid4(), reason=None, now=datetime.now(UTC), progress=["o/n#1"], backoff=True
    )
    assert item.status == "open" and item.not_before is None
    assert "_dispatch_attempts" not in item.payload


def test_the_cap_still_blocks(monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 2)
    item = _item({"_dispatch_attempts": 1})
    assert wi_service._release_unfinished(item, run_id=uuid.uuid4(), reason=None, now=datetime.now(UTC), backoff=True)
    assert item.status == "blocked" and item.not_before is None
