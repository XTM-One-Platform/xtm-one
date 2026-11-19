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

"""Each person's own OAuth account on a ``per_user`` MCP server.

An OAuth MCP server in ``credential_mode = "per_user"`` never lends the token
stored on its row: every call carries the acting person's own token
(``MCPServerUserCredential``), or none. This module is the one place that says
which token holder a call uses and how a holder is named by the refresh code
(``services/token_refresh.py``) and the connection's auth hook
(``services/mcp_oauth_tokens.py``). Contract: ``dev-docs/mcp-server-user-credentials.md``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.mcp_server import MCPServer
from app.models.mcp_server_user_credential import MCPServerUserCredential

SHARED = "shared"
PER_USER = "per_user"
CREDENTIAL_MODES = (SHARED, PER_USER)

#: Prefix of a person's credential in a holder key; a server's key is its id.
_USER_HOLDER_PREFIX = "user:"

MCPTokenHolder = MCPServer | MCPServerUserCredential


def is_per_user(server: Any) -> bool:
    """Whether *server* is an OAuth server whose people each connect their own account."""
    return getattr(server, "auth_mode", None) == "oauth" and getattr(server, "credential_mode", SHARED) == PER_USER


def server_of(holder: MCPTokenHolder) -> MCPServer:
    """The server a token holder belongs to: the OAuth client and endpoints live there."""
    return holder.server if isinstance(holder, MCPServerUserCredential) else holder


def mcp_holder_key(holder: MCPTokenHolder) -> str:
    """The stable name of a token holder: refresh lock, back-off and auth hook key.

    A server's key is its id, as before per-user credentials existed, so the
    Redis keys of a ``shared`` server do not change.
    """
    if isinstance(holder, MCPServerUserCredential):
        return f"{_USER_HOLDER_PREFIX}{holder.id}"
    return str(holder.id)


async def load_mcp_holder(db: AsyncSession, key: object) -> MCPTokenHolder | None:
    """The token holder *key* names (:func:`mcp_holder_key`), or ``None``."""
    text = str(key)
    if text.startswith(_USER_HOLDER_PREFIX):
        credential_id = safe_uuid(text[len(_USER_HOLDER_PREFIX) :])
        if credential_id is None:
            return None
        credential = await db.get(MCPServerUserCredential, credential_id)
        # A server switched back to ``shared`` deleted its credentials; one
        # that is no longer per_user never sends a person's token again.
        if credential is None or not is_per_user(credential.server):
            return None
        return credential
    server_id = key if isinstance(key, uuid.UUID) else safe_uuid(text)
    if server_id is None:
        return None
    return await db.get(MCPServer, server_id)


async def user_credential(
    db: AsyncSession, server_id: uuid.UUID, user_id: object
) -> MCPServerUserCredential | None:
    """*user_id*'s own credential on server *server_id*, or ``None``."""
    uid = user_id if isinstance(user_id, uuid.UUID) else safe_uuid(str(user_id or ""))
    if uid is None:
        return None
    return await db.scalar(
        select(MCPServerUserCredential).where(
            MCPServerUserCredential.server_id == server_id,
            MCPServerUserCredential.user_id == uid,
        )
    )


async def user_credentials_by_server(
    db: AsyncSession, server_ids: list[uuid.UUID], user_id: object
) -> dict[uuid.UUID, MCPServerUserCredential]:
    """*user_id*'s credentials on *server_ids*, by server id (one query)."""
    uid = user_id if isinstance(user_id, uuid.UUID) else safe_uuid(str(user_id or ""))
    if uid is None or not server_ids:
        return {}
    rows = await db.scalars(
        select(MCPServerUserCredential).where(
            MCPServerUserCredential.server_id.in_(server_ids),
            MCPServerUserCredential.user_id == uid,
        )
    )
    return {row.server_id: row for row in rows}


async def credential_for_runtime(
    db: AsyncSession, server: MCPServer, acting_user_id: object
) -> MCPServerUserCredential | None:
    """The credential a connection made for *acting_user_id* carries on *server*.

    ``None`` on a ``shared`` server (the row's own token is used) and on a
    ``per_user`` server the person never connected or with no acting person:
    the connection then sends no token, never another person's.
    """
    if not is_per_user(server):
        return None
    return await user_credential(db, server.id, acting_user_id)


async def delete_user_credentials(db: AsyncSession, server_id: uuid.UUID) -> int:
    """Delete every person's credential on *server_id* (the server left ``per_user``)."""
    result = await db.execute(
        delete(MCPServerUserCredential).where(MCPServerUserCredential.server_id == server_id)
    )
    return int(result.rowcount or 0)


def clear_server_tokens(server: MCPServer) -> None:
    """Drop the token stored on the server row: in ``per_user`` mode nobody uses it."""
    server.access_token_enc = None
    server.refresh_token_enc = None
    server.token_expires_at = None
    server.token_issued_at = None
    server.oauth_status = "disconnected"
