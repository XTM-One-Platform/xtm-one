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

"""Full-fidelity media storage for trace payloads.

An agent that receives a screenshot or produces a chart has that image as part
of its input or output — it is the same audit material as the text around it.
The trace layer used to replace those blobs with the strings ``"[image]"`` and
``"[base64 image]"`` (``langfuse_tracing._strip_media_content``), which made the
image unrecoverable from the trace: the durable store held a placeholder and
there was no second copy anywhere.

Media now follows the model
:mod:`app.services.tracing.payload_offload` established for oversized payloads.
Each blob is written to object storage under the same ``traces/<trace_id>/``
prefix and replaced in the payload by a small *reference* carrying the object
name, content type, exact byte size and a sha256 digest.  Nothing is destroyed:
the reference is a pointer, and the bytes are one fetch away from the payload
endpoint.

The stripping does survive in one place, and deliberately: the **external
Langfuse export**.  The Langfuse SDK detects ``image_url`` content parts and
uploads them to its own configured media storage, so handing it real blobs
triggers upload retries against a bucket that is frequently not configured —
the original reason the stripping was introduced.  That path is
``langfuse_tracing._bound_for_langfuse``, which strips media (and truncates)
for the export only, while the durable payload keeps the reference.

Two shapes carry base64 media in this codebase, and both are handled:

  * OpenAI content parts — ``{"type": "image_url", "image_url": {"url":
    "data:image/png;base64,…"}}`` (``llm/base.py``).  The ``data:`` URI is a
    plain string, so it is matched wherever it appears rather than only under
    a known key.
  * Anthropic content blocks — ``{"type": "image"|"document", "source":
    {"type": "base64", "media_type": "image/png", "data": "<base64>"}}``
    (``llm/anthropic_provider.py``).  Here the base64 has no ``data:`` prefix
    and is identified by its position inside a ``source`` object, whose
    ``media_type`` supplies the content type.

Remote URLs (``{"source": {"type": "url", "url": "https://…"}}``) are left
exactly as they are.  They were previously replaced by ``"[image]"``, which was
itself a fidelity loss; keeping the URL costs nothing and restores it.  This
module never fetches one — an outbound request driven by traced payload content
would be both a surprise side-effect on the trace path and an SSRF vector.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
from typing import Any
from uuid import UUID

from app.core.config import settings
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

# Marker key identifying a media reference.  Namespaced exactly like
# ``payload_offload.ENVELOPE_KEY`` so it cannot collide with a provider payload
# field, and distinct from it so the two are told apart when walking a payload.
MEDIA_KEY = "__xtm_media__"
MEDIA_VERSION = 1

# ``data:<content-type>[;params];base64,<payload>``.  The content type is
# optional in the spec (defaults to text/plain) but always present in practice.
_DATA_URI_RE = re.compile(r"^data:([^;,]*)(;[^,]*)?;base64,(.*)$", re.DOTALL)

# Smallest blob worth moving to object storage.  A tiny inline icon costs less
# to keep in the row than an object plus a reference does, and a round-trip to
# render it would be slower than the bytes it saves.
_MIN_MEDIA_BYTES = 1024

# Base64 payloads shorter than this are never treated as bare media, so a short
# opaque identifier that happens to be base64-shaped is not mistaken for an
# image.  Only applies to the ``source.data`` form; ``data:`` URIs are explicit.
_MIN_BARE_BASE64_CHARS = 256

_EXTENSION_BY_CONTENT_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "image/bmp": "bmp",
    "image/tiff": "tiff",
    "application/pdf": "pdf",
}


def is_media_reference(value: Any) -> bool:
    return isinstance(value, dict) and value.get(MEDIA_KEY) == MEDIA_VERSION


def media_object_prefix(trace_id: UUID | str, span_id: UUID | str | None) -> str:
    """Object-storage prefix for one trace's (or span's) media blobs.

    Nested under the same ``traces/<trace_id>`` root as offloaded payloads so a
    purge can still drop everything a trace owns with one prefix walk, and
    suffixed with ``media`` so the two kinds are distinguishable in the bucket.
    """
    base = f"traces/{trace_id}"
    scope = str(span_id) if span_id else "trace"
    return f"{base}/{scope}/media"


def _decode_data_uri(value: str) -> tuple[str, bytes] | None:
    """Return ``(content_type, raw_bytes)`` for a base64 ``data:`` URI."""
    match = _DATA_URI_RE.match(value)
    if match is None:
        return None
    content_type = (match.group(1) or "application/octet-stream").strip()
    try:
        raw = base64.b64decode(match.group(3), validate=False)
    except (binascii.Error, ValueError):
        return None
    return content_type, raw


def _decode_bare_base64(value: str) -> bytes | None:
    """Return the bytes of a bare (prefix-less) base64 payload."""
    if len(value) < _MIN_BARE_BASE64_CHARS:
        return None
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw or None


def _build_reference(
    *,
    raw: bytes,
    content_type: str,
    object_name: str | None,
    error: str | None = None,
) -> dict[str, Any]:
    reference: dict[str, Any] = {
        MEDIA_KEY: MEDIA_VERSION,
        "content_type": content_type,
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    if object_name:
        reference["object_name"] = object_name
    else:
        reference["offload_failed"] = True
        reference["error"] = error or "object storage unavailable"
    return reference


class _MediaUploader:
    """Uploads the blobs found in one trace's payloads, de-duplicating by digest.

    The same image commonly appears in several payloads of one trace — a
    screenshot is in the LLM call's input, in the agent-loop span above it and
    in the trace input — so the digest cache keeps one object instead of three.
    """

    def __init__(self, *, trace_id: UUID | str, span_id: UUID | str | None) -> None:
        self._trace_id = trace_id
        self._span_id = span_id
        self._by_digest: dict[str, dict[str, Any]] = {}
        self.uploaded = 0

    async def reference_for(self, raw: bytes, content_type: str) -> dict[str, Any]:
        digest = hashlib.sha256(raw).hexdigest()
        cached = self._by_digest.get(digest)
        if cached is not None:
            return dict(cached)

        extension = _EXTENSION_BY_CONTENT_TYPE.get(content_type.lower(), "bin")
        try:
            # Imported lazily for the reason payload_offload documents: the
            # tracing dispatcher is imported at app startup and must not pull
            # the S3 client into processes that never offload anything.
            from app.services.file_storage import file_storage

            object_name = await file_storage.upload(
                raw,
                filename=f"media.{extension}",
                content_type=content_type,
                prefix=media_object_prefix(self._trace_id, self._span_id),
            )
        except Exception as exc:
            error = format_exception_message(exc)
            # No inline fallback, unlike an oversized payload: the blob that
            # would be kept inline is precisely the multi-megabyte base64 the
            # row cannot hold, and the reference still records the type, size
            # and digest so the loss is visible instead of silent.
            logger.warning(
                "Trace media offload failed (%s, %d bytes): %s",
                content_type,
                len(raw),
                error,
            )
            reference = _build_reference(raw=raw, content_type=content_type, object_name=None, error=error)
            self._by_digest[digest] = reference
            return dict(reference)

        self.uploaded += 1
        reference = _build_reference(raw=raw, content_type=content_type, object_name=object_name)
        self._by_digest[digest] = reference
        return dict(reference)


async def _walk(value: Any, uploader: _MediaUploader) -> Any:
    """Replace every media blob in ``value`` with a reference, in place of it.

    Returns ``value`` itself when nothing changed, all the way up the tree.  The
    overwhelming majority of trace payloads carry no media at all, and they run
    through here on every trace — rebuilding each dict and list to produce an
    equal copy would be pure allocation, and it would also break the dispatcher
    contract that an untouched payload is passed through unchanged.
    """
    if isinstance(value, str):
        decoded = _decode_data_uri(value)
        if decoded is None:
            return value
        content_type, raw = decoded
        if len(raw) < _MIN_MEDIA_BYTES:
            return value
        return await uploader.reference_for(raw, content_type)

    if isinstance(value, list):
        items = [await _walk(item, uploader) for item in value]
        if all(new is old for new, old in zip(items, value, strict=True)):
            return value
        return items

    if not isinstance(value, dict):
        return value

    if is_media_reference(value):
        return value

    out: dict[str, Any] = {}
    changed = False
    for key, item in value.items():
        # Anthropic ``source`` block: the base64 sits in ``data`` with its type
        # in the sibling ``media_type``, so it is only identifiable here.
        if key == "data" and isinstance(item, str) and str(value.get("type") or "").lower() == "base64":
            content_type = str(value.get("media_type") or "application/octet-stream")
            raw = _decode_bare_base64(item)
            if raw is not None and len(raw) >= _MIN_MEDIA_BYTES:
                out[key] = await uploader.reference_for(raw, content_type)
                changed = True
                continue
        replacement = await _walk(item, uploader)
        changed = changed or replacement is not item
        out[key] = replacement
    return out if changed else value


async def offload_media(
    value: Any,
    *,
    trace_id: UUID | str,
    span_id: UUID | str | None,
    field: str,
) -> Any:
    """Return ``value`` with its media blobs replaced by storage references.

    Runs on the async persistence path *before*
    :func:`payload_offload.offload_if_oversized`, for two reasons: the media
    blobs are usually what makes a payload oversized in the first place, so
    replacing them often keeps the payload inline entirely; and a payload that
    is still oversized afterwards gets offloaded with the references already in
    it, so its media stays individually addressable rather than being buried
    inside one opaque JSON object.

    Never raises.  A bucket failure yields a reference recording the failure —
    the trace is still written, and the gap is visible in the UI.
    """
    if value is None or not settings.trace_media_offload_enabled:
        return value

    uploader = _MediaUploader(trace_id=trace_id, span_id=span_id)
    try:
        replacement = await _walk(value, uploader)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Trace media offload raised for %s, storing the payload as-is: %s",
            field,
            format_exception_message(exc),
        )
        return value

    if uploader.uploaded:
        logger.info(
            "Offloaded %d media blob(s) from trace payload %s",
            uploader.uploaded,
            field,
        )
    return replacement


async def resolve_media(value: Any) -> tuple[bytes, str] | None:
    """Return ``(raw_bytes, content_type)`` for a media reference.

    ``None`` when ``value`` is not a media reference, or is one whose upload
    failed and therefore has no object behind it.
    """
    if not is_media_reference(value):
        return None
    object_name = value.get("object_name")
    if not object_name:
        return None

    from app.services.file_storage import file_storage

    raw = await file_storage.download(str(object_name))
    content_type = str(value.get("content_type") or "application/octet-stream")
    return raw, content_type


def collect_media_object_names(value: Any) -> list[str]:
    """Object names of every media reference nested anywhere inside ``value``.

    Unlike a payload envelope — which replaces a whole field and so sits at the
    top level — media references live deep inside the payload, wherever the
    content part was.  Deletion therefore has to walk the structure.
    """
    found: list[str] = []
    seen: set[str] = set()

    def _visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                _visit(item)
            return
        if not isinstance(node, dict):
            return
        if is_media_reference(node):
            object_name = node.get("object_name")
            if object_name and str(object_name) not in seen:
                seen.add(str(object_name))
                found.append(str(object_name))
            return
        for item in node.values():
            _visit(item)

    _visit(value)
    return found


def find_media_reference(value: Any, sha256: str) -> dict[str, Any] | None:
    """Locate the media reference with digest ``sha256`` inside ``value``.

    The digest is the lookup key used by the payload endpoint: it identifies one
    blob within a payload without exposing a raw object name to the client, so a
    caller cannot ask for an arbitrary object in the bucket.
    """
    if not sha256:
        return None

    stack: list[Any] = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, list):
            stack.extend(node)
            continue
        if not isinstance(node, dict):
            continue
        if is_media_reference(node):
            if str(node.get("sha256") or "") == sha256:
                return node
            continue
        stack.extend(node.values())
    return None
