# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The trace of a background run the platform lost.

A background task (``spawn_background_task``) writes its agent trace when its
turn ends. A turn that never ends - its worker killed, its lease lost with no
attempt left, the orchestrator restarted - used to leave no trace at all, so
``inspect_my_traces`` / ``inspect_agent_traces`` showed nothing for the whole
window of the run and the delegating agent could not tell it ever ran.

While the turn runs, it keeps a checkpoint of its own trace: the
trace's header when the turn opens it, then each span as it ends, in Redis
under the task id (:class:`SpanRecorder`, bound by the runner with
:func:`recording_spans`). The trace store itself is written once, at the end
of the turn, as for every other trace.

When the platform itself ends such a task as ``failed`` (the sub-agent
adapter's dead letter, the orphan sweep), it closes that trace here: the
turn's own trace id, the spans the checkpoint holds, status ``error``, named
after the task id so a search on the id finds it, carrying the reason, the
failure cause and the last heartbeat the engine heard. A task with no
checkpoint (Redis unreachable, killed before its trace opened) gets the same
trace with no span, under an id derived from the task id. Either way a second
pass (a re-run of the sweep, a dead letter after the sweep) writes the same
row, never a second one. A turn that ends through its own path writes its
trace as before; its checkpoint expires unread.

Best-effort by contract, like every trace write: a tracing failure never
changes how the task ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import Coroutine, Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from app.core.utils import safe_uuid
from app.services.failure_causes import FailureCause
from app.services.tracing.base import SpanData, TraceData

logger = logging.getLogger(__name__)

#: Namespace of :func:`background_run_trace_id`.
_BACKGROUND_RUN_TRACE_NAMESPACE = uuid.UUID("6f1c3a52-3e0b-4d0e-9a51-8c2b7f0d4e11")

#: ``metadata.kind`` of a lost-run trace, so a reader can tell it from a turn's own trace.
LOST_RUN_TRACE_KIND = "background_run_lost"

#: Trace source: background tasks run as the delegating run's child runs.
_SOURCE = "assignment"

#: Redis keys of a turn's checkpoint: its trace header, then its ended spans.
_CHECKPOINT_KEY = "tracing:background-run:{task_id}"
_CHECKPOINT_SPANS_KEY = "tracing:background-run:{task_id}:spans"

#: How long a checkpoint outlives its last write: past any orphan sweep's
#: ``fail_after``, after which nobody reads it.
CHECKPOINT_TTL_SECONDS = 24 * 3600

#: The spans a checkpoint keeps (the latest), and the size of one span past
#: which its payloads are left out of the checkpoint (the turn's own trace,
#: written at its end, keeps them whole).
_CHECKPOINT_MAX_SPANS = 500
_CHECKPOINT_MAX_SPAN_CHARS = 256_000

#: Bound on one checkpoint write or read: a slow Redis never holds a turn.
_CHECKPOINT_TIMEOUT_SECONDS = 2.0

#: The span fields a checkpoint keeps (``trace_id`` is the header's).
_SPAN_FIELDS = (
    "id",
    "parent_span_id",
    "type",
    "name",
    "position",
    "started_at",
    "ended_at",
    "duration_ms",
    "time_to_first_token",
    "model",
    "input",
    "output",
    "metadata",
    "level",
    "status_message",
    "input_tokens",
    "output_tokens",
    "usage_details",
    "tool_call_id",
    "tool_arguments",
    "stop_reason",
)

#: Checkpoint writes and lost-run closes in flight, held so they are not
#: garbage-collected before they run.
_PENDING: set[asyncio.Task] = set()

_current_recorder: ContextVar[SpanRecorder | None] = ContextVar("background_run_span_recorder", default=None)


async def _redis() -> Any:
    from app.core.redis import redis_client

    return await redis_client()


def _schedule(coro: Coroutine[Any, Any, Any]) -> asyncio.Task | None:
    """Run *coro* in the background on the running loop; ``None`` (and *coro* closed) without one."""
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return None
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)
    return task


def _dumps(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"))


def _span_payload(span: dict[str, Any]) -> str:
    """One ended span as the checkpoint keeps it: payloads left out past the size bound."""
    data = {name: span.get(name) for name in _SPAN_FIELDS}
    text = _dumps(data)
    if len(text) > _CHECKPOINT_MAX_SPAN_CHARS:
        for name in ("input", "output", "tool_arguments"):
            data[name] = {"omitted": "over the checkpoint size bound"} if data.get(name) is not None else None
        text = _dumps(data)
    return text


class SpanRecorder:
    """The checkpoint of one background turn's trace: its header at open, each span as it ends.

    Bound once, to the first trace opened under it (the turn's own; a consult
    or a transfer inside the turn opens its own trace, which is not recorded).
    Writes are queued in order and never raise.
    """

    def __init__(self, task_id: Any) -> None:
        self.task_id = str(task_id)
        self.trace_id: uuid.UUID | None = None
        self._last: asyncio.Task | None = None

    def bind(
        self,
        *,
        trace_id: Any,
        name: str | None,
        started_at: datetime | None,
        parent_trace_id: Any = None,
        model: str | None = None,
    ) -> bool:
        """Record the turn's trace as opened; ``False`` when already bound or *trace_id* is unusable."""
        trace_uuid = safe_uuid(str(trace_id)) if trace_id else None
        if self.trace_id is not None or trace_uuid is None:
            return False
        self.trace_id = trace_uuid
        header = {
            "trace_id": str(trace_uuid),
            "name": name,
            "started_at": (started_at or datetime.now(UTC)).isoformat(),
            "parent_trace_id": str(parent_trace_id) if parent_trace_id else None,
            "model": model,
        }
        self._write(self._open(_dumps(header)))
        return True

    def record_span(self, span: dict[str, Any]) -> None:
        """Add one ended span of the turn's trace to the checkpoint."""
        if self.trace_id is None:
            return
        try:
            payload = _span_payload(span)
        except Exception:
            logger.debug("Background run %s: span not checkpointed", self.task_id, exc_info=True)
            return
        self._write(self._push(payload))

    async def _open(self, header: str) -> None:
        redis = await _redis()
        # A re-run of the task opens a new trace: the spans of the attempt
        # it replaces are not its own.
        await redis.delete(_CHECKPOINT_SPANS_KEY.format(task_id=self.task_id))
        await redis.set(_CHECKPOINT_KEY.format(task_id=self.task_id), header, ex=CHECKPOINT_TTL_SECONDS)

    async def _push(self, payload: str) -> None:
        redis = await _redis()
        key = _CHECKPOINT_SPANS_KEY.format(task_id=self.task_id)
        await redis.rpush(key, payload)
        await redis.ltrim(key, -_CHECKPOINT_MAX_SPANS, -1)
        await redis.expire(key, CHECKPOINT_TTL_SECONDS)

    def _write(self, coro: Coroutine[Any, Any, None]) -> None:
        previous = self._last

        async def _in_order() -> None:
            if previous is not None:
                await asyncio.wait([previous])
            try:
                await asyncio.wait_for(coro, timeout=_CHECKPOINT_TIMEOUT_SECONDS)
            except Exception:
                logger.debug("Background run %s: checkpoint write failed", self.task_id, exc_info=True)

        task = _schedule(_in_order())
        if task is None:
            coro.close()
        else:
            self._last = task


def current_span_recorder() -> SpanRecorder | None:
    """The recorder of the background turn running in this context, if any."""
    return _current_recorder.get()


@contextlib.contextmanager
def recording_spans(task_id: Any) -> Iterator[SpanRecorder]:
    """Checkpoint the trace of the background turn run inside this block."""
    recorder = SpanRecorder(task_id)
    token = _current_recorder.set(recorder)
    try:
        yield recorder
    finally:
        _current_recorder.reset(token)


def _span_from_payload(raw: Any, trace_id: uuid.UUID) -> SpanData | None:
    try:
        data = json.loads(raw)
        span_id = safe_uuid(str(data.get("id") or ""))
        started = _as_aware(data.get("started_at"))
        if span_id is None or started is None:
            return None
        metadata = data.get("metadata")
        return SpanData(
            id=span_id,
            trace_id=trace_id,
            type=str(data.get("type") or "SPAN"),
            name=str(data.get("name") or "span"),
            position=int(data.get("position") or 0),
            started_at=started,
            ended_at=_as_aware(data.get("ended_at")),
            duration_ms=data.get("duration_ms"),
            time_to_first_token=data.get("time_to_first_token"),
            parent_span_id=safe_uuid(data.get("parent_span_id")),
            model=data.get("model"),
            input=data.get("input"),
            output=data.get("output"),
            metadata=metadata if isinstance(metadata, dict) else {},
            level=str(data.get("level") or "DEFAULT"),
            status_message=data.get("status_message"),
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            usage_details=data.get("usage_details"),
            tool_call_id=data.get("tool_call_id"),
            tool_arguments=data.get("tool_arguments"),
            stop_reason=data.get("stop_reason"),
        )
    except Exception:
        return None


async def read_checkpoint(task_id: Any) -> tuple[dict[str, Any], list[SpanData]] | None:
    """The checkpointed trace header and spans of *task_id*'s turn, or ``None``."""
    redis = await _redis()
    raw = await redis.get(_CHECKPOINT_KEY.format(task_id=task_id))
    if not raw:
        return None
    header = json.loads(raw)
    trace_id = safe_uuid(str(header.get("trace_id") or "")) if isinstance(header, dict) else None
    if trace_id is None:
        return None
    spans: list[SpanData] = []
    seen: set[uuid.UUID] = set()
    for item in await redis.lrange(_CHECKPOINT_SPANS_KEY.format(task_id=task_id), 0, -1) or []:
        span = _span_from_payload(item, trace_id)
        if span is not None and span.id not in seen:
            seen.add(span.id)
            spans.append(span)
    return header, spans


def background_run_trace_id(task_id: Any) -> uuid.UUID:
    """The id of the lost-run trace of *task_id*: stable across passes."""
    return uuid.uuid5(_BACKGROUND_RUN_TRACE_NAMESPACE, f"sub_agent_task:{task_id}")


def lost_run_trace_name(task_id: Any) -> str:
    """The trace name, which carries the task id the delegating agent searches by."""
    return f"background task {task_id} (lost)"


def _as_aware(value: Any) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def build_lost_run_trace(
    task: Any,
    *,
    reason: str,
    failure_cause: str = FailureCause.ENGINE_ABANDONED.value,
    last_heartbeat_at: Any = None,
    attempt: int | None = None,
    now: datetime | None = None,
    checkpoint: dict[str, Any] | None = None,
) -> TraceData:
    """The ``error`` trace of a background *task* the platform ended as failed.

    *checkpoint* is the header its turn recorded when it opened its trace
    (:func:`read_checkpoint`): the trace then closes that one, under its id.
    """
    now = now or datetime.now(UTC)
    task_id = str(getattr(task, "id", "") or "")
    metadata_json = getattr(task, "metadata_json", None) or {}
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    turn_trace_id = safe_uuid(str(checkpoint.get("trace_id") or ""))
    started = (
        (_as_aware(checkpoint.get("started_at")) if turn_trace_id else None)
        or _as_aware(getattr(task, "started_at", None))
        or _as_aware(getattr(task, "created_at", None))
        or now
    )
    if started > now:
        started = now
    heartbeat = _as_aware(last_heartbeat_at)
    metadata: dict[str, Any] = {
        "kind": LOST_RUN_TRACE_KIND,
        "task_id": task_id,
        "reason": reason,
        "last_heartbeat_at": heartbeat.isoformat() if heartbeat else None,
    }
    if attempt:
        metadata["attempt"] = int(attempt)
    parent_agent_id = getattr(task, "parent_agent_id", None)
    if parent_agent_id:
        metadata["parent_agent_id"] = str(parent_agent_id)
    if turn_trace_id:
        metadata["turn_trace_name"] = checkpoint.get("name")
    # A resumed attempt lost in its turn keeps its link: the trace that
    # closes it is searchable on ``resumed_from`` like the one it opened.
    from app.services.background_checkpoint import resumed_trace_meta

    resumed = resumed_trace_meta(task_id, metadata_json if isinstance(metadata_json, dict) else None)
    if resumed:
        metadata.update(resumed)
    return TraceData(
        id=turn_trace_id or background_run_trace_id(task_id),
        parent_trace_id=safe_uuid(checkpoint.get("parent_trace_id")) if turn_trace_id else None,
        model=checkpoint.get("model") if turn_trace_id else None,
        source=_SOURCE,
        name=lost_run_trace_name(task_id),
        started_at=started,
        ended_at=now,
        duration_ms=max((now - started).total_seconds() * 1000.0, 0.0),
        status="error",
        level="ERROR",
        user_id=safe_uuid(getattr(task, "user_id", None)),
        agent_id=safe_uuid(getattr(task, "target_agent_id", None)),
        conversation_id=safe_uuid(getattr(task, "conversation_id", None)),
        assignment_run_id=safe_uuid(metadata_json.get("assignment_run_id")),
        metadata=metadata,
        input=getattr(task, "task_description", None),
        error_message=reason,
        failure_cause=failure_cause,
        tags=["background-task", LOST_RUN_TRACE_KIND],
    )


#: The task fields a lost-run trace reads, copied before the caller's
#: transaction commits (an ORM row expires on commit).
_TASK_FIELDS = (
    "id",
    "user_id",
    "parent_agent_id",
    "target_agent_id",
    "conversation_id",
    "task_description",
    "started_at",
    "created_at",
    "metadata_json",
)


async def close_lost_background_run(
    task: Any,
    *,
    reason: str,
    failure_cause: str = FailureCause.ENGINE_ABANDONED.value,
    last_heartbeat_at: Any = None,
    attempt: int | None = None,
    now: datetime | None = None,
) -> uuid.UUID | None:
    """Close the trace of lost *task* with the spans its checkpoint holds; its id, or ``None`` on failure."""
    try:
        checkpoint: dict[str, Any] | None = None
        spans: list[SpanData] = []
        try:
            read = await asyncio.wait_for(read_checkpoint(task.id), timeout=_CHECKPOINT_TIMEOUT_SECONDS)
        except Exception:
            logger.debug("Lost background task %s: checkpoint unreadable", getattr(task, "id", None), exc_info=True)
            read = None
        if read is not None:
            checkpoint, spans = read
        trace = build_lost_run_trace(
            task,
            reason=reason,
            failure_cause=failure_cause,
            last_heartbeat_at=last_heartbeat_at,
            attempt=attempt,
            now=now,
            checkpoint=checkpoint,
        )
        for span in spans:
            span.trace_id = trace.id
        from app.services.tracing.dispatcher import submit_trace_for_flush

        submit_trace_for_flush(trace, spans)
        return trace.id
    except Exception:
        logger.warning("Could not record the trace of lost background task %s", getattr(task, "id", None), exc_info=True)
        return None


def record_lost_background_run(
    task: Any,
    *,
    reason: str,
    failure_cause: str = FailureCause.ENGINE_ABANDONED.value,
    last_heartbeat_at: Any = None,
    attempt: int | None = None,
    now: datetime | None = None,
) -> bool:
    """Queue the close of lost *task*'s trace (:func:`close_lost_background_run`); ``False`` when it could not be."""
    try:
        snapshot = SimpleNamespace(**{name: getattr(task, name, None) for name in _TASK_FIELDS})
        snapshot.metadata_json = dict(snapshot.metadata_json or {})
        return (
            _schedule(
                close_lost_background_run(
                    snapshot,
                    reason=reason,
                    failure_cause=failure_cause,
                    last_heartbeat_at=last_heartbeat_at,
                    attempt=attempt,
                    now=now,
                )
            )
            is not None
        )
    except Exception:
        logger.warning("Could not record the trace of lost background task %s", getattr(task, "id", None), exc_info=True)
        return False
