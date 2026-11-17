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

"""A remote MCP server's OAuth access token, kept fresh while a connection uses it.

The token refresh sweep (``services/token_refresh.py``) refreshes a stored
token ahead of its expiry on every scheduler tick, but a connection opened
before that refresh holds the token it was opened with: with a 10-minute access
token, an agent run outlives it. :class:`StoredMCPOAuthToken` is the
``httpx.Auth`` hook the MCP transports take (``auth=`` of the SDK's
``streamablehttp_client`` / ``sse_client``). It sets the token on every request:

- when the token it holds is about to expire, it first asks for the current one
  (the one the sweep stored, else a refresh);
- when the server answers ``401``, it asks for a replacement of the refused
  token and replays the request once with it - unless the challenge says
  ``insufficient_scope`` (RFC 6750 §3.1): a token lacking a scope is not a
  stale one, and refreshing it would rotate the refresh token for nothing.

Both go through :func:`token_refresh.current_mcp_access_token`, which refreshes
under the server's refresh lock with the sweep's guards and bounds the refresh
a ``401`` forces. A failure there never fails the request: the hook keeps the
token it holds and the server's answer stands. When no fresh token came back
for a token about to expire (a refresh backing off, failing, or a token that
expires as soon as it is issued), the hook asks again only after
:data:`RETRY_AFTER_SECONDS`, so a refresh that cannot happen does not cost every
request a database read.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncGenerator, Generator
from datetime import datetime

import httpx

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: How long the hook waits before asking again for a token about to expire
#: when the last ask brought no fresh one.
RETRY_AFTER_SECONDS = 15.0

_INSUFFICIENT_SCOPE = re.compile(r'\berror\s*=\s*"?insufficient_scope\b', re.IGNORECASE)


def _scope_refusal(response: httpx.Response) -> bool:
    """Whether a ``401`` refuses the token for a missing scope, not for being stale."""
    return any(_INSUFFICIENT_SCOPE.search(value) for value in response.headers.get_list("www-authenticate"))


class StoredMCPOAuthToken(httpx.Auth):
    """Bearer auth for one MCP server, with the token it stores kept fresh."""

    # The request is replayed after a refresh: its body must be read first.
    requires_request_body = True

    def __init__(self, server_id: str, token: str, expires_at: datetime | None) -> None:
        self._server_id = server_id
        self._token = token
        self._expires_at = expires_at
        # A token obtained after a ``401``: refused again, it is not refreshed
        # a second time by this connection.
        self._replaced_after_refusal: str | None = None
        # ``time.monotonic()`` before which a token about to expire is not
        # asked for again (the last ask brought no fresh one).
        self._retry_after = 0.0
        self._lock = asyncio.Lock()

    @property
    def token(self) -> str:
        return self._token

    def _expires_soon(self) -> bool:
        from app.services.token_refresh import token_expires_soon

        return token_expires_soon(self._expires_at)

    def _due(self) -> bool:
        return self._expires_soon() and time.monotonic() >= self._retry_after

    async def _replace(self, refused: str | None) -> bool:
        """Swap the held token for the current one; whether it changed."""
        from app.services.token_refresh import current_mcp_access_token

        async with self._lock:
            if refused is not None and self._token != refused:
                return True  # another request of this connection replaced it
            if refused is None and not self._due():
                return False
            try:
                current = await current_mcp_access_token(self._server_id, refused=refused)
            except Exception as exc:
                logger.warning(
                    "Could not get a fresh OAuth token for MCP server %s: %s",
                    self._server_id,
                    format_exception_message(exc),
                )
                current = None
            changed = current is not None and current.value != self._token
            if changed:
                self._token = current.value
                self._expires_at = current.expires_at
            if refused is None and self._expires_soon():
                self._retry_after = time.monotonic() + RETRY_AFTER_SECONDS
            return changed

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        if self._due():
            await self._replace(None)
        sent = self._token
        request.headers["Authorization"] = f"Bearer {sent}"
        response = yield request
        if response.status_code != 401 or sent == self._replaced_after_refusal or _scope_refusal(response):
            return
        if not await self._replace(sent):
            return
        self._replaced_after_refusal = self._token
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        # The MCP transports are asynchronous; a synchronous client sends the
        # token as held.
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request
