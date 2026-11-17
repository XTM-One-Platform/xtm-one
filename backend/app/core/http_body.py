# SPDX-License-Identifier: Apache-2.0

"""Reading a request body under a size bound."""

from __future__ import annotations

from starlette.requests import Request


async def read_bounded_body(request: Request, max_bytes: int) -> bytes | None:
    """The request body, or ``None`` once it exceeds *max_bytes*.

    The declared ``Content-Length`` is checked before anything is read, then
    the stream is counted as it arrives, so a chunked or understated body is
    cut off at the bound instead of being buffered whole.
    """
    # RFC 9110 allows optional whitespace around a field value.
    declared = request.headers.get("content-length", "").strip(" \t").lstrip("0")
    # Compared by length first: ``int()`` refuses a string of more than 4300 digits.
    if declared.isascii() and declared.isdigit() and (len(declared) > len(str(max_bytes)) or int(declared) > max_bytes):
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)
