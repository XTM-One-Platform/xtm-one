# SPDX-License-Identifier: Apache-2.0

"""A request body cut off past a byte limit while the form parser reads it.

Starlette's multipart parser writes a file part to a temporary file as it
arrives, before the route can check the file's size, and imposes no bound of
its own on it. A route that reads an upload builds its form on
``Request(request.scope, BoundedReceive(request.receive, limit))``: past the
limit the parser stops with a :class:`MultiPartException` (answered like the
parser's own refusals, the part written so far closed) instead of reading the
body to its end. Used by the runner result upload and the public widget upload.
"""

from __future__ import annotations

from starlette.formparsers import MultiPartException
from starlette.types import Message, Receive


class BoundedReceive:
    """The ``receive`` of an upload, cut off past ``limit`` body bytes."""

    def __init__(self, receive: Receive, limit: int, *, refusal: str) -> None:
        self._receive = receive
        self._received = 0
        self.limit = limit
        self.refusal = refusal
        self.exceeded = False

    async def __call__(self) -> Message:
        message = await self._receive()
        if message["type"] == "http.request":
            self._received += len(message.get("body", b""))
            if self._received > self.limit:
                self.exceeded = True
                raise MultiPartException(self.refusal)
        return message
