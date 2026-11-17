"""A stopping worker hands its work back to a worker of the incoming release, once per rollout (#8819, slice 2).

Observed 5 Nov 2026: one agent run was handed back 9 times in 40 minutes. Each stopping worker deferred the run to
the head of the queue and the next worker to claim it was often another worker of the outgoing release, stopped by
the same rolling update a minute later. A head-of-queue hand-back decided while the worker stops is now held for a
running worker of the incoming release (``claim_for``), the hold Video Studio renders already had.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.core.config import settings
from app.services import orchestration_reconciler as reconciler
from app.services import worker_presence
from app.services.orchestration_adapters.base import AdapterResult


def _records(*workers: tuple[str, str, str]) -> list[dict]:
    return [
        {"worker_id": worker, "version": version, "state": state, "claims": ["assignment"]}
        for worker, version, state in workers
    ]


@pytest.fixture
def world(monkeypatch):
    """This worker runs release 1.0.0-old and was asked to stop; the workers present are what the test sets."""
    monkeypatch.setattr(settings, "app_version", "1.0.0-old")
    monkeypatch.setattr(reconciler, "worker_stopping", lambda: True)
    found: list[dict] = _records(("old-a", "1.0.0-old", "draining"), ("old-b", "1.0.0-old", "running"))

    async def present(*, at_most: int = 256):
        return found

    monkeypatch.setattr(worker_presence, "present", present)
    return found


def _handed_back() -> AdapterResult:
    return AdapterResult.defer("handed back", delay_seconds=0.0, adapter_state="handed_back", place="head")


async def test_a_hand_back_is_held_for_the_incoming_release(world):
    world += _records(("new-a", "1.0.1-new", "running"))
    result = await reconciler._to_incoming_release(_handed_back(), "assignment", "old-a")
    assert result.claim_for == ("new-a", reconciler.HANDBACK_INCOMING_HOLD_SECONDS)
    assert (result.kind, result.place, result.adapter_state) == ("defer", "head", "handed_back")


async def test_with_no_incoming_worker_the_hand_back_is_kept_from_the_outgoing_release(world):
    # Only a running worker of the same release (the new release not up yet, or a plain restart): it is kept from
    # this release's workers for a bounded time, any worker after (#8958).
    result = await reconciler._to_incoming_release(_handed_back(), "assignment", "old-a")
    assert result.claim_for is None
    assert result.not_release == ("1.0.0-old", reconciler.HANDBACK_OUTGOING_HOLD_SECONDS)
    assert reconciler._defer_options(result)["not_release"] == ("1.0.0-old", reconciler.HANDBACK_OUTGOING_HOLD_SECONDS)


async def test_an_incoming_worker_that_does_not_claim_the_adapter_is_not_chosen(world):
    world.append({"worker_id": "new-render", "version": "1.0.1-new", "state": "running", "claims": ["video_render"]})
    result = await reconciler._to_incoming_release(_handed_back(), "assignment", "old-a")
    assert result.claim_for is None and result.not_release[0] == "1.0.0-old"


async def test_a_release_that_names_no_version_holds_nothing(world, monkeypatch):
    monkeypatch.setattr(settings, "app_version", "")
    world[:] = _records(("old-a", "", "draining"))
    original = _handed_back()
    assert await reconciler._to_incoming_release(original, "assignment", "old-a") is original


async def test_a_hand_back_already_held_from_a_release_is_never_rerouted(world):
    world += _records(("new-a", "1.0.1-new", "running"))
    held = AdapterResult.defer("h", delay_seconds=0.0, adapter_state="h", place="head")
    held = dataclasses.replace(held, not_release=("1.0.0-old", 10.0))
    assert await reconciler._to_incoming_release(held, "assignment", "old-a") is held


@pytest.mark.parametrize(
    "result",
    [
        AdapterResult.completed(),
        AdapterResult.failed("boom"),
        AdapterResult.defer("waits", delay_seconds=30.0, adapter_state="held"),
        AdapterResult.defer("given", delay_seconds=0.0, adapter_state="h", place="head", claim_for=("w9", 60.0)),
    ],
)
async def test_other_results_are_never_rerouted(world, result):
    world += _records(("new-a", "1.0.1-new", "running"))
    assert await reconciler._to_incoming_release(result, "assignment", "old-a") is result


async def test_a_worker_not_stopping_reroutes_nothing(world, monkeypatch):
    world += _records(("new-a", "1.0.1-new", "running"))
    monkeypatch.setattr(reconciler, "worker_stopping", lambda: False)
    original = _handed_back()
    assert await reconciler._to_incoming_release(original, "assignment", "old-a") is original


async def test_redis_unreadable_never_keeps_the_hand_back(world, monkeypatch):
    async def broken(claiming: str, *, me: str | None = None):
        raise ConnectionError("redis down")

    monkeypatch.setattr(worker_presence, "incoming_worker", broken)
    original = _handed_back()
    assert await reconciler._to_incoming_release(original, "assignment", "old-a") is original
