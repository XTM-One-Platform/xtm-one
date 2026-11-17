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

"""Full-fidelity trace payload storage.

Trace payloads (``input`` / ``output`` / ``metadata`` on traces and spans) are
audit material: truncating them at write time destroys the only copy.  The
durable store therefore keeps every field verbatim, with one exception — a
payload above ``settings.trace_payload_offload_threshold_bytes`` is written to
object storage and replaced in the row by a small *envelope* carrying the
object name, the exact byte size, a sha256 digest and a readable preview.
Nothing is destroyed either way: the envelope is a pointer, not a truncation.

Two size boundaries, both configurable:

  * ``trace_payload_offload_threshold_bytes`` — above this, offload.  Keeps
    multi-megabyte tool results out of the Postgres primary's row set, and
    also out of the tracing queue / Redis overflow / ClickHouse insert batch,
    because the offload runs before the payload reaches any sink.
  * ``trace_payload_hard_max_bytes`` — the backstop for when object storage is
    unreachable.  Below it, an offload failure falls back to storing the
    payload inline (fidelity beats tidiness); above it the row keeps an
    envelope with ``offload_failed`` set so the loss is visible in the UI
    instead of silently swallowed.

The read side is deliberately separate.  :func:`build_display_preview` bounds a
payload for list/detail API responses *while preserving its structure*, so the
trace UI stays responsive and callers that need the bytes fetch them from the
payload endpoint.  Structure preservation is the point: the previous
implementation truncated the serialized JSON and returned an unparseable
string, which is how span ``reasoning`` vanished from the inspector — the
frontend can only read ``output.reasoning`` if ``output`` is still an object.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import UUID

from app.core.config import settings
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

# Marker key identifying an offload envelope.  Namespaced so it cannot collide
# with a provider payload field (``reasoning``, ``tool_calls``, ``content``…).
ENVELOPE_KEY = "__xtm_offload__"
ENVELOPE_VERSION = 1

# Characters of the serialized payload kept inside the envelope so the trace UI
# can show something meaningful before the user asks for the full object.
_ENVELOPE_PREVIEW_CHARS = 4_000

# Default budget for an API display preview.  Matches the legacy cap so the
# payload panes render the same amount of text they always did; the difference
# is that the structure now survives and the rest is one fetch away.
DISPLAY_PREVIEW_MAX_CHARS = 20_000

# Lists are bounded by item count as well as by string length: a payload with
# 200k tiny elements serializes huge without any single long string.
_DISPLAY_PREVIEW_MAX_ITEMS = 500

_MIN_STRING_BUDGET = 64


def _serialize(value: Any) -> str:
    return json.dumps(value, default=str, ensure_ascii=False)


def payload_size_bytes(value: Any) -> int:
    """UTF-8 byte size of ``value`` as it would be persisted."""
    if value is None:
        return 0
    try:
        return len(_serialize(value).encode("utf-8"))
    except Exception:
        return len(repr(value).encode("utf-8"))


def is_offload_envelope(value: Any) -> bool:
    return isinstance(value, dict) and value.get(ENVELOPE_KEY) == ENVELOPE_VERSION


def _truncate_marker(text: str, limit: int) -> str:
    """Truncate ``text`` using the marker format used across the trace layer."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"… [truncated {len(text) - limit} chars]"


def _build_envelope(
    *,
    serialized: str,
    size: int,
    object_name: str | None,
    offload_failed: bool = False,
    error: str | None = None,
) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        ENVELOPE_KEY: ENVELOPE_VERSION,
        "size_bytes": size,
        "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "encoding": "json",
        "preview": _truncate_marker(serialized, _ENVELOPE_PREVIEW_CHARS),
    }
    if object_name:
        envelope["object_name"] = object_name
    if offload_failed:
        envelope["offload_failed"] = True
        envelope["error"] = error or "object storage unavailable"
    return envelope


def offload_object_prefix(trace_id: UUID | str, span_id: UUID | str | None) -> str:
    """Object-storage prefix for one trace's (or span's) offloaded payloads.

    Keyed by trace so retention can delete a whole trace's objects with a
    single prefix walk when the row is purged.
    """
    base = f"traces/{trace_id}"
    return f"{base}/{span_id}" if span_id else f"{base}/trace"


async def offload_if_oversized(
    value: Any,
    *,
    trace_id: UUID | str,
    span_id: UUID | str | None,
    field: str,
) -> Any:
    """Return ``value`` unchanged, or an envelope pointing at object storage.

    Called on the async persistence path (before the payload reaches the
    tracing queue), so every downstream sink — Postgres, ClickHouse, the Redis
    overflow buffer — carries the small envelope rather than the full blob.
    """
    if value is None or is_offload_envelope(value):
        return value

    try:
        serialized = _serialize(value)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Trace payload %s is not serializable, storing repr: %s",
            field,
            format_exception_message(exc),
        )
        return value

    size = len(serialized.encode("utf-8"))
    threshold = int(settings.trace_payload_offload_threshold_bytes)
    if threshold <= 0 or size <= threshold:
        return value

    try:
        # Imported lazily: the tracing dispatcher is imported at app startup and
        # must not pull the S3 client (and its urllib3 pool) into every process
        # that merely records traces without ever offloading one.
        from app.services.file_storage import file_storage

        object_name = await file_storage.upload(
            serialized.encode("utf-8"),
            filename=f"{field}.json",
            content_type="application/json",
            prefix=offload_object_prefix(trace_id, span_id),
        )
    except Exception as exc:
        error = format_exception_message(exc)
        hard_max = int(settings.trace_payload_hard_max_bytes)
        if size <= hard_max:
            # Fidelity first: the payload still fits the durable store, so keep
            # it verbatim rather than losing audit material to an S3 outage.
            logger.warning(
                "Trace payload offload failed for %s (%d bytes), storing inline: %s",
                field,
                size,
                error,
            )
            return value
        logger.error(
            "Trace payload offload failed for %s and exceeds the hard max "
            "(%d > %d bytes) — storing envelope with the loss recorded: %s",
            field,
            size,
            hard_max,
            error,
        )
        return _build_envelope(
            serialized=serialized,
            size=size,
            object_name=None,
            offload_failed=True,
            error=error,
        )

    logger.info(
        "Offloaded trace payload %s (%d bytes) to %s",
        field,
        size,
        object_name,
    )
    return _build_envelope(serialized=serialized, size=size, object_name=object_name)


async def resolve_payload(value: Any) -> Any:
    """Return the full payload behind ``value``, fetching the object if needed.

    Non-offloaded values pass through, so callers can use this uniformly
    whether the payload was small enough to live inline or not.
    """
    if not is_offload_envelope(value):
        return value

    object_name = value.get("object_name")
    if not object_name:
        # A failed offload: the preview inside the envelope is all that exists.
        return value

    from app.services.file_storage import file_storage

    raw = await file_storage.download(str(object_name))
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        logger.warning(
            "Offloaded trace payload %s is not valid JSON, returning raw text: %s",
            object_name,
            format_exception_message(exc),
        )
        return raw.decode("utf-8", errors="replace")


async def delete_object_names(object_names: list[str]) -> int:
    """Delete offloaded payload objects by name, tolerating per-object failure.

    Used by trace deletion and the retention purge: dropping the row without
    this would orphan the object in the bucket forever.  A bucket problem never
    blocks the row deletion the caller is performing — it is logged and the
    object is left for a later sweep.
    """
    if not object_names:
        return 0

    from app.services.file_storage import file_storage

    deleted = 0
    for object_name in object_names:
        try:
            await file_storage.delete(object_name)
            deleted += 1
        except Exception as exc:
            logger.warning(
                "Failed to delete offloaded trace payload %s: %s",
                object_name,
                format_exception_message(exc),
            )
    return deleted


def trace_object_prefix(trace_id: UUID | str) -> str:
    """Root prefix under which every object one trace owns is stored."""
    return f"traces/{trace_id}/"


async def list_trace_objects(trace_id: UUID | str) -> list[str]:
    """The name of every stored object one trace owns; raises when the bucket cannot be listed."""
    from app.services.file_storage import file_storage

    return list(await file_storage.list_object_names(trace_object_prefix(trace_id)))


async def delete_trace_objects(trace_ids: list[Any]) -> int:
    """Delete every stored object belonging to ``trace_ids`` by prefix sweep.

    Complements name-based deletion, which can only see what the row still
    points at.  Two things escape it:

      * media nested *inside* an offloaded payload — the row holds the payload
        envelope, and the media references live in the stored object, so no
        query over the rows can find them;
      * objects left behind by a partially-written trace, whose row never
        recorded the pointer.

    Sweeping ``traces/<trace_id>/`` catches both, and is safe because that
    prefix is owned exclusively by the one trace (both
    :func:`offload_object_prefix` and ``media_offload.media_object_prefix``
    nest under it).  Best-effort: a listing failure yields nothing to delete
    and is logged by the storage layer, never blocking the row deletion.
    """
    if not trace_ids:
        return 0

    names: list[str] = []
    for trace_id in trace_ids:
        try:
            names.extend(await list_trace_objects(trace_id))
        except Exception as exc:
            logger.warning(
                "Could not list stored objects for trace %s: %s",
                trace_id,
                format_exception_message(exc),
            )
    return await delete_object_names(names)


def _shrink(value: Any, string_budget: int, max_items: int) -> Any:
    """Recursively bound every string leaf and list length in ``value``."""
    if isinstance(value, str):
        return _truncate_marker(value, string_budget)
    if isinstance(value, list):
        head = [_shrink(item, string_budget, max_items) for item in value[:max_items]]
        hidden = len(value) - len(head)
        if hidden > 0:
            head.append(f"… [{hidden} items omitted]")
        return head
    if isinstance(value, dict):
        return {k: _shrink(v, string_budget, max_items) for k, v in value.items()}
    return value


def build_display_preview(
    value: Any,
    *,
    limit: int = DISPLAY_PREVIEW_MAX_CHARS,
) -> tuple[Any, bool]:
    """Bound ``value`` for an API response, keeping its JSON structure intact.

    Returns ``(preview, truncated)``.  An offload envelope is returned as-is
    (it is already small and the UI renders it as a "stored externally"
    placeholder with a load action).

    Unlike a truncation of the serialized form, this never turns an object into
    a string: keys such as ``reasoning`` / ``tool_calls`` / ``messages`` stay
    addressable, which is what the span inspector reads.
    """
    if value is None or is_offload_envelope(value):
        return value, False

    if payload_size_bytes(value) <= limit:
        return value, False

    if isinstance(value, str):
        return _truncate_marker(value, limit), True

    # Largest per-string budget whose serialized form still fits.  Binary
    # search keeps this to ~log2(limit) serializations of an already bounded
    # structure, and guarantees a stable result for a given payload.
    low, high = _MIN_STRING_BUDGET, limit
    best = _shrink(value, _MIN_STRING_BUDGET, _DISPLAY_PREVIEW_MAX_ITEMS)
    while low <= high:
        mid = (low + high) // 2
        candidate = _shrink(value, mid, _DISPLAY_PREVIEW_MAX_ITEMS)
        if payload_size_bytes(candidate) <= limit:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best, True


def payload_meta(value: Any, *, preview_truncated: bool) -> dict[str, Any] | None:
    """Describe what the API is returning for one payload field.

    ``None`` when the field is absent or fully inlined and untruncated — the
    common case, kept out of the response so it stays lean.
    """
    if value is None:
        return None
    offloaded = is_offload_envelope(value)
    if not offloaded and not preview_truncated:
        return None
    meta: dict[str, Any] = {
        "truncated": bool(preview_truncated) or offloaded,
        "offloaded": offloaded,
    }
    if offloaded:
        meta["size_bytes"] = value.get("size_bytes")
        meta["sha256"] = value.get("sha256")
        if value.get("offload_failed"):
            meta["offload_failed"] = True
            meta["error"] = value.get("error")
    else:
        meta["size_bytes"] = payload_size_bytes(value)
    return meta
