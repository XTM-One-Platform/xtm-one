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

"""The trace storage and payload switches of Settings > Usage and quotas > Observability.

``platform_settings.trace_storage_enabled`` and ``trace_store_payloads`` decide
what the native trace store (PostgreSQL, and ClickHouse when it is the store)
keeps of an LLM call:

* storage off - nothing: no trace, no span and no score is written;
* payloads off - the trace without what was said: the trace's input and
  output and every span's input, output and tool arguments are dropped, and an
  error message keeps its first :data:`PAYLOAD_OFF_ERROR_CHARS` characters,
  since a provider error can quote the request it refused. Everything else is
  kept: names (tool names included), model and model parameters, timings,
  token counts and usage (so the cost), status, level, failure cause, stop
  reason, tool call ids, metadata and tags.

Both at their default (on), a trace is written exactly as before.

The tracing pipeline reads them from this process's snapshot
(:func:`trace_switches`), never from the database. The snapshot is kept in
step the way the security options are, by the same calls: read at startup and
by the poll every web and worker process runs
(``services/security_options.refresh_security_options_cache``, every
``REFRESH_INTERVAL_SECONDS``), and published by the process that served an
administrator's change once it is committed. Until this process has read them
once, the defaults apply, as they do for the security options.

The switches act where a trace is recorded (``TracingDispatcher.record``,
before any payload reaches object storage, the in-memory queue or the Redis
overflow) and again where it is written (the dispatcher's flush and each
sink): a trace recorded before a change, still queued or spilled to Redis by
any replica, is written under the switches in force when it is written, and
the objects its dropped content pointed at are deleted
(:func:`discard_switched_off_objects`).

The Langfuse export is not governed by them: it has its own switch in the same
card and receives each call's payloads, bounded per field, while it is on.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.constants import TRUNCATE_LABEL
from app.core.security_options import snapshot_ticket
from app.core.utils import format_exception_message

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.tracing.base import SpanData, TraceData

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TraceSwitches:
    storage_enabled: bool = True
    store_payloads: bool = True


DEFAULT_TRACE_SWITCHES = TraceSwitches()

#: What an error message keeps while payloads are off: the label length the
#: Langfuse export already clips a status message to.
PAYLOAD_OFF_ERROR_CHARS = TRUNCATE_LABEL
_ERROR_CUT_MARKER = " [cut: trace payloads are not stored]"

_snapshot = DEFAULT_TRACE_SWITCHES
_loaded = False
# The ticket of the read or write that produced ``_snapshot``: the snapshot
# only moves forward (``app.core.security_options.publish_stored``).
_published_ticket = 0


def trace_switches() -> TraceSwitches:
    """The switches in force in this process: synchronous and database-free."""
    return _snapshot


def snapshot_loaded() -> bool:
    """Whether this process has read the switches at least once."""
    return _loaded


def switches_from_row(storage_enabled: bool | None, store_payloads: bool | None) -> TraceSwitches:
    """The switches a ``platform_settings`` row holds; a NULL reads as on, the column default."""
    return TraceSwitches(
        storage_enabled=True if storage_enabled is None else bool(storage_enabled),
        store_payloads=True if store_payloads is None else bool(store_payloads),
    )


async def read_trace_switches(db: AsyncSession) -> TraceSwitches:
    """The stored switches (the defaults while the singleton row does not exist)."""
    from sqlalchemy import select

    from app.models.platform_settings import PlatformSettings

    row = (
        await db.execute(
            select(PlatformSettings.trace_storage_enabled, PlatformSettings.trace_store_payloads)
            .order_by(PlatformSettings.created_at.asc(), PlatformSettings.id.asc())
            .limit(1)
        )
    ).first()
    if row is None:
        return DEFAULT_TRACE_SWITCHES
    return switches_from_row(row[0], row[1])


def publish_trace_switches(switches: TraceSwitches, *, ticket: int | None = None) -> bool:
    """Put *switches* in force in this process (after the commit that stored them).

    *ticket*: the one taken before the read that produced *switches*
    (``snapshot_ticket``); without one, they are the newest - a write
    publishing what it just committed. False when a newer publish already
    happened and these older values were dropped.
    """
    global _snapshot, _loaded, _published_ticket
    if ticket is None:
        ticket = snapshot_ticket()
    if ticket <= _published_ticket:
        return False
    _published_ticket = ticket
    if switches != _snapshot:
        logger.info(
            "Trace switches now in force in this process: storage %s, payloads %s",
            "on" if switches.storage_enabled else "off",
            "on" if switches.store_payloads else "off",
        )
    _snapshot = switches
    _loaded = True
    return True


def reset_trace_switches() -> None:
    """Back to "nothing read yet" (the defaults apply), for tests."""
    global _snapshot, _loaded, _published_ticket
    _snapshot = DEFAULT_TRACE_SWITCHES
    _loaded = False
    _published_ticket = snapshot_ticket()


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def _cut_error(text: str | None) -> str | None:
    if text is None or len(text) <= PAYLOAD_OFF_ERROR_CHARS:
        return text
    return text[:PAYLOAD_OFF_ERROR_CHARS] + _ERROR_CUT_MARKER


def _span_without_payload(span: SpanData) -> SpanData:
    status_message = _cut_error(span.status_message)
    if (
        span.input is None
        and span.output is None
        and span.tool_arguments is None
        and status_message == span.status_message
    ):
        return span
    return dataclasses.replace(span, input=None, output=None, tool_arguments=None, status_message=status_message)


def without_payloads(trace: TraceData, spans: list[SpanData]) -> tuple[TraceData, list[SpanData]]:
    """*trace* and *spans* without what was said; the very same objects when there was nothing to drop."""
    kept_spans = [_span_without_payload(span) for span in spans]
    error_message = _cut_error(trace.error_message)
    if (
        trace.input is None
        and trace.output is None
        and error_message == trace.error_message
        and all(kept is span for kept, span in zip(kept_spans, spans, strict=True))
    ):
        return trace, spans
    kept_trace = dataclasses.replace(
        trace,
        input=None,
        output=None,
        error_message=error_message,
        spans=kept_spans if trace.spans else [],
    )
    return kept_trace, kept_spans


def apply_trace_switches(
    trace: TraceData,
    spans: list[SpanData],
    switches: TraceSwitches | None = None,
) -> tuple[TraceData, list[SpanData]] | None:
    """What of *trace* the trace store may keep: ``None`` for nothing, else the trace and spans to write.

    *switches* defaults to the ones in force in this process. The objects
    passed in are never modified.
    """
    switches = switches or _snapshot
    if not switches.storage_enabled:
        return None
    if switches.store_payloads:
        return trace, spans
    return without_payloads(trace, spans)


# ---------------------------------------------------------------------------
# The objects a switched-off trace leaves behind
# ---------------------------------------------------------------------------

_PAYLOAD_FIELDS = ("input", "output", "tool_arguments")


def _stored_objects(value: Any) -> list[str]:
    """The object names a trace field points at: an offloaded payload, and the media inline in it."""
    from app.services.tracing.media_offload import collect_media_object_names
    from app.services.tracing.payload_offload import is_offload_envelope

    names = collect_media_object_names(value)
    if is_offload_envelope(value) and value.get("object_name"):
        names.append(str(value["object_name"]))
    return names


async def _reachable_objects(values: list[Any]) -> set[str] | None:
    """Every object the kept *values* still point at, the media inside an offloaded one included.

    ``None`` when an offloaded value could not be read: what it points at is then unknown.
    """
    from app.services.tracing.media_offload import collect_media_object_names
    from app.services.tracing.payload_offload import is_offload_envelope, resolve_payload

    reachable: set[str] = set()
    for value in values:
        reachable.update(_stored_objects(value))
        if is_offload_envelope(value) and value.get("object_name"):
            try:
                reachable.update(collect_media_object_names(await resolve_payload(value)))
            except Exception as exc:
                logger.warning(
                    "Could not read the offloaded object %s: %s", value.get("object_name"), format_exception_message(exc)
                )
                return None
    return reachable


async def discard_switched_off_objects(
    trace: TraceData,
    spans: list[SpanData],
    kept: tuple[TraceData, list[SpanData]] | None,
) -> int:
    """Delete the stored objects of what :func:`apply_trace_switches` dropped from *trace*.

    Only a trace recorded before the switches changed has any: a newer one is
    switched before its payloads reach object storage. *kept* is what
    :func:`apply_trace_switches` returned. Nothing of the trace is written
    (``None``): its ``traces/<id>/`` prefix is its own and no row will point
    at it, so the whole prefix goes. Its payloads are dropped: every object
    under the prefix that no kept field (the metadata) still points at goes -
    the whole prefix when none does - which also catches the media stored
    inside an offloaded payload, never visible from the row. When a kept
    offloaded field or the prefix cannot be read, only the objects the dropped
    fields name go. Best-effort and never raises: a bucket problem is logged
    and the trace is written all the same.
    """
    before: list[Any] = [trace, *spans]
    after: list[Any] = [] if kept is None else [kept[0], *kept[1]]
    if kept is not None and all(new is old for new, old in zip(after, before, strict=True)):
        return 0
    try:
        from app.services.tracing.payload_offload import (
            delete_object_names,
            delete_trace_objects,
            list_trace_objects,
        )

        if kept is None:
            if any(
                _stored_objects(getattr(holder, field, None))
                for holder in before
                for field in (*_PAYLOAD_FIELDS, "metadata")
            ):
                return await delete_trace_objects([trace.id])
            return 0
        dropped: list[str] = []
        for old, new in zip(before, after, strict=True):
            if old is new:
                continue
            for field in _PAYLOAD_FIELDS:
                if getattr(new, field, None) is None:
                    dropped.extend(_stored_objects(getattr(old, field, None)))
        if not dropped:
            return 0
        reachable = await _reachable_objects([getattr(holder, "metadata", None) for holder in after])
        if reachable is None:
            return await delete_object_names(sorted(set(dropped)))
        if not reachable:
            return await delete_trace_objects([trace.id])
        try:
            stored = await list_trace_objects(trace.id)
        except Exception as exc:
            logger.warning("Could not list the stored objects of trace %s: %s", trace.id, format_exception_message(exc))
            return await delete_object_names(sorted(set(dropped)))
        return await delete_object_names(sorted(set(stored) - reachable))
    except Exception as exc:
        logger.warning(
            "Could not delete the stored payload objects of trace %s: %s",
            getattr(trace, "id", None),
            format_exception_message(exc),
        )
        return 0
