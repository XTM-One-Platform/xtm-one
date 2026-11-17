"""A recording whose application did not answer waits for it instead of failing.

``runners.run_take`` queues a take whose profile's probe carries an
``http_status`` (a 5xx or a 429 past the probe's own retries) again and
answers ``hold``; the take adapter defers it, no attempt spent, for
``unanswered_wait``, and fails it with its ``profile session not checked``
message once it waited ``UNANSWERED_HOLD_SECONDS``. A probe that answered and
reads signed out still fails the take at once.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_FAILED, JOB_PENDING, JOB_RUNNING, VideoTake
from app.services.browser_profiles.signin import ProbeResult
from app.services.orchestration_adapters import video_studio as adapters
from app.services.orchestration_engine import HOLD_PAYLOAD_KEY
from app.services.video_studio import capture, jobs, runners, take_state
from app.services.video_studio.spec import validate_take_script

USER_ID = uuid.uuid4()


def _take() -> VideoTake:
    script = validate_take_script({"start_url": "https://app.example.com/", "steps": [{"action": "wait", "seconds": 1}]})
    return VideoTake(id=uuid.uuid4(), user_id=USER_ID, name="Dashboard tour", status=JOB_PENDING, script=script)


def _recording(monkeypatch, take: VideoTake, probe: ProbeResult) -> list[dict]:
    """``run_take`` on *take*, its capture stopped before step 1 on *probe*; the rows' writes, in order."""
    written: list[dict] = []

    class _Db:
        async def get(self, _model, _key):
            return take

        async def commit(self):
            return None

    @asynccontextmanager
    async def session():
        yield _Db()

    async def earlier(*_args, **_kwargs):
        return {}

    async def record(*_args, **_kwargs):
        raise capture.ProfileNotAuthenticatedError(probe)

    async def mark(_kind, _job_id, **changes):
        written.append(changes)

    async def mark_if_open(_kind, _job_id, **changes):
        written.append(changes)
        return True

    monkeypatch.setattr(runners, "async_session", session)
    monkeypatch.setattr(take_state, "load_earlier_finds", earlier)
    monkeypatch.setattr(capture, "record_take", record)
    monkeypatch.setattr(jobs, "mark", mark)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    return written


def test_an_application_that_did_not_answer_queues_the_take_again(monkeypatch):
    take = _take()
    probe = ProbeResult(authenticated=False, url="https://app.example.com/", reason="HTTP 503", http_status=503)
    written = _recording(monkeypatch, take, probe)
    outcome = asyncio.run(runners.run_take(str(take.id)))
    assert outcome.kind == "hold"
    assert (outcome.error or "").startswith("profile session not checked")
    assert take.status == JOB_RUNNING  # the run started it ...
    last = written[-1]
    # ... and gave it back to the queue, saying what it waits for: never failed, never finished.
    assert (last["status"], last["progress"], last["started_at"]) == (JOB_PENDING, 0, None)
    assert last["stage"] == "Waiting for the application to answer (HTTP 503)"
    assert all(change.get("status") != JOB_FAILED for change in written)


def test_a_session_the_application_reads_signed_out_still_fails_the_take(monkeypatch):
    take = _take()
    probe = ProbeResult(authenticated=False, url="https://app.example.com/login", reason="the sign-in form")
    written = _recording(monkeypatch, take, probe)
    outcome = asyncio.run(runners.run_take(str(take.id)))
    assert outcome.kind == "done"
    assert written[-1]["status"] == JOB_FAILED
    assert written[-1]["error"].startswith("profile not authenticated")


def _execution(held_for: float | None, count: int = 0) -> SimpleNamespace:
    payload: dict = {"take_id": str(uuid.uuid4())}
    if held_for is not None:
        since = datetime.now(UTC) - timedelta(seconds=held_for)
        payload[HOLD_PAYLOAD_KEY] = {"since": since.isoformat(), "count": count, "reason": "unanswered"}
    return SimpleNamespace(id=uuid.uuid4(), payload=payload)


def _held(monkeypatch, execution) -> tuple[object, list[tuple]]:
    ended: list[tuple] = []

    async def dead_letter(kind, job_id, error, **_kwargs):
        ended.append((kind, job_id, error))

    monkeypatch.setattr(runners, "dead_letter", dead_letter)
    monkeypatch.setattr(adapters.wake, "schedule", lambda *_a, **_k: None)
    outcome = runners.JobOutcome("hold", "profile session not checked: landed on https://app.example.com/ (HTTP 503)")
    result = asyncio.run(adapters.VideoTakeAdapter()._unanswered(execution.payload["take_id"], execution, outcome))
    return result, ended


@pytest.mark.parametrize(("count", "first"), [(0, 30.0), (1, 60.0), (2, 120.0), (6, 300.0)])
def test_an_unanswered_take_is_deferred_no_attempt_spent_its_wait_doubling(monkeypatch, count, first):
    assert adapters.unanswered_wait(count) == first
    result, ended = _held(monkeypatch, _execution(held_for=60.0, count=count))
    assert (result.kind, result.adapter_state, ended) == ("defer", adapters.UNANSWERED_STATE, [])
    assert first <= result.backoff_seconds <= first + 60.0


def test_a_take_whose_application_stays_unanswered_fails_saying_so(monkeypatch):
    result, ended = _held(monkeypatch, _execution(held_for=adapters.UNANSWERED_HOLD_SECONDS - 10.0, count=8))
    assert (result.kind, result.retryable) == ("failed", False)
    [(kind, _job, error)] = ended
    # The tool's hint reads this prefix: record again later, no new sign-in.
    assert kind == "take" and error.startswith("profile session not checked")
    assert "it did not answer for 30 min" in error
