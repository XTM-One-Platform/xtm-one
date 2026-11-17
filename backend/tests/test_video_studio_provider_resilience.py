"""A provider's refusal is waited out, said and counted as the provider's.

* ``provider_errors`` reads the HTTP status, the provider's own words and the
  wait it asked for off every error shape the media providers raise - Google's
  ``RetryInfo`` included.
* A shot its provider rate limited before starting it is held (the engine's
  ``defer``: no attempt spent) for the wait the provider named or a short,
  jittered back-off, and fails saying so once held too long.
* The frames a ``generate_video_clip`` answer names are files of the
  conversation, linked without being attached.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from google.genai import errors as genai_errors

from app.llm import provider_errors, video
from app.models.file_attachment import FileAttachment
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import runners

CONVERSATION = uuid.UUID("00000000-0000-0000-0000-00000000e001")
USER_ID = uuid.UUID("00000000-0000-0000-0000-00000000e002")


def _google_429(delay: str | None = "23s") -> genai_errors.ClientError:
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": []}]
    if delay is not None:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay})
    return genai_errors.ClientError(
        429,
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "message": "Quota exceeded: 10 per minute",
                "details": details,
            }
        },
    )


# ── What a refusal says ───────────────────────────────────────────────────


def test_the_status_message_and_wait_are_read_off_every_error_shape():
    google = _google_429()
    assert provider_errors.error_status(google) == 429
    assert provider_errors.api_error_message(google) == "Quota exceeded: 10 per minute"
    assert provider_errors.retry_delay(google) == 23.0
    assert provider_errors.retry_delay(_google_429("1.5s")) == 1.5
    assert provider_errors.retry_delay(_google_429(None)) is None

    openai_shaped = SimpleNamespace(
        status_code=503,
        body={"error": {"message": "overloaded"}},
        response=SimpleNamespace(headers={"retry-after": "7"}),
    )
    assert provider_errors.error_status(openai_shaped) == 503
    assert provider_errors.api_error_message(openai_shaped) == "overloaded"
    assert provider_errors.retry_delay(openai_shaped) == 7.0
    own = video.VideoAPIError(429, "slow down", retry_after=12)
    assert (provider_errors.error_status(own), provider_errors.retry_delay(own)) == (429, 12.0)
    assert provider_errors.error_status(RuntimeError("no image")) is None


def test_a_rate_limited_creation_carries_the_wait_the_provider_asked_for():
    busy = video.status_error(429, _google_429(), "veo-3.0-generate-001")
    assert isinstance(busy, video.VideoProviderBusyError)
    assert (busy.status_code, busy.retry_after) == (429, 23.0)
    assert "Quota exceeded" in str(busy)


# ── A rate-limited shot is held, not failed ───────────────────────────────


def test_the_wait_is_the_providers_else_a_short_doubling_back_off_spread_at_random():
    for _ in range(50):
        assert 23.0 <= adapters.rate_limit_wait(0, 23.0) <= 23.0 * 1.25
        assert 10.0 <= adapters.rate_limit_wait(0, None) <= 12.5
        assert 20.0 <= adapters.rate_limit_wait(1, None) <= 25.0
        # The limits are per minute: the back-off never grows past one.
        assert 60.0 <= adapters.rate_limit_wait(9, None) <= 75.0


class _Ctx:
    cancel_event = None

    def __init__(self):
        self.db = SimpleNamespace(commit=self._commit)

    async def _commit(self):
        return None


def _held(monkeypatch, outcome, *, hold=None):
    """``VideoClipAdapter.execute`` on a run that answered *outcome*; *hold* is the execution's hold record."""
    dead: list = []

    async def run(_self, _job_id, _cancel):
        return outcome

    async def dead_letter(kind, job_id, error, **_kwargs):
        dead.append((kind, job_id, error))

    monkeypatch.setattr(adapters.VideoClipAdapter, "_run", run)
    monkeypatch.setattr(adapters.wake, "schedule", lambda *_args: None)
    monkeypatch.setattr(runners, "dead_letter", dead_letter)
    payload = {"clip_id": "c1", **({"hold": hold} if hold else {})}
    execution = SimpleNamespace(payload=payload)
    return asyncio.run(adapters.VideoClipAdapter().execute(_Ctx(), execution)), dead


def test_a_shot_its_provider_rate_limited_is_deferred_for_the_wait_it_named(monkeypatch):
    result, dead = _held(monkeypatch, runners.JobOutcome("hold", "HTTP 429: Quota exceeded", wait=23.0))
    assert result.kind == "defer" and not dead
    assert result.adapter_state == adapters.RATE_LIMITED_STATE
    assert 23.0 <= result.backoff_seconds <= 28.75
    assert result.error == "HTTP 429: Quota exceeded"


def test_a_shot_held_too_long_fails_saying_so(monkeypatch):
    since = (datetime.now(UTC) - timedelta(minutes=29, seconds=30)).isoformat()
    result, dead = _held(
        monkeypatch, runners.JobOutcome("hold", "HTTP 429: Quota exceeded"), hold={"since": since, "count": 14}
    )
    assert (result.kind, result.retryable) == ("failed", False)
    assert len(dead) == 1 and dead[0][:2] == ("clip", "c1")
    assert "Rate limited for 30 min" in dead[0][2] and "past the 30 min a job waits for its provider" in dead[0][2]


def test_a_wait_past_the_bound_fails_at_once(monkeypatch):
    result, dead = _held(monkeypatch, runners.JobOutcome("hold", "HTTP 429: daily quota", wait=6 * 3600.0))
    assert result.kind == "failed" and "Rate limited for 0 min" in dead[0][2]


def test_any_other_outcome_maps_as_before(monkeypatch):
    result, dead = _held(monkeypatch, runners.JobOutcome("retry", "HTTP 503"))
    assert (result.kind, result.retryable, result.backoff_seconds) == ("failed", True, 30) and not dead


# ── The frames an answer names are files of the conversation ──────────────


def _file(**fields) -> FileAttachment:
    return FileAttachment(
        **{
            "id": uuid.uuid4(),
            "filename": "f.jpg",
            "content_type": "image/jpeg",
            "size": 3,
            "s3_object_name": "o",
            **fields,
        }
    )


def test_linked_pictures_are_copied_into_the_conversation_and_never_attached(monkeypatch):
    from app.services import file_storage as storage
    from app.services.builtin_tools import _shared
    from app.services.builtin_tools import video_studio as tool

    frame = _file(sha256="frame-bytes")
    already = _file(conversation_id=CONVERSATION)
    copy = _file(conversation_id=CONVERSATION, sha256="frame-bytes")

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [frame, already]))

    @asynccontextmanager
    async def session():
        yield _Db()

    async def no_copy(_conversation_id, _digest):
        return None

    async def persist(**_kwargs):
        return copy

    async def download(_name):
        return b"bytes"

    monkeypatch.setattr(tool, "async_session", session)
    monkeypatch.setattr(tool, "_conversation_copy", no_copy)
    monkeypatch.setattr(_shared, "_persist_generated_file", persist)
    monkeypatch.setattr(storage.file_storage, "download", download)
    shown: set[str] = set()
    linked = asyncio.run(
        tool._file_refs(
            [frame.id, already.id], conversation_id=CONVERSATION, user_id=str(USER_ID), shown=shown, show=False
        )
    )
    assert linked.copies == {str(frame.id): str(copy.id), str(already.id): str(already.id)}
    assert linked.refs == [] and linked.named == [] and shown == set()
