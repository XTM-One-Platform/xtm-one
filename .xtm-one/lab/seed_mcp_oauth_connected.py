"""The lab's OAuth MCP server in the ``connected`` state (#9999).

Started by ``lab.py`` (``seed_mcp_servers``) with the backend's interpreter, from ``backend/``
and with the backend's environment, once ``POST /api/v1/mcp-servers`` created the row (a
Streamable HTTP server on ``.lab.invalid``, ``auth_mode`` ``oauth``, disabled). Without it no
lab server is ``connected``, so the Configuration tab's token-drop warning (``willDropTokens``
in ``ConfigTab.tsx``) only renders under a ``window.fetch`` override.

No API stores a token without the provider round-trip, so this writes what the OAuth callback
writes on the row through the backend's own model and ``encrypt_value``:

* ``access_token_enc``: a lab-only string, never sent anywhere (the server is disabled and its
  URL never resolves);
* ``refresh_token_enc``: none, and an expiry a year ahead, so the refresh sweep never selects it;
* ``oauth_status``: ``connected``.

Arguments: the server id and the JSON file the outcome is written to. Idempotent.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: The stored access token: a lab-only string, never a provider token.
LAB_ACCESS_TOKEN = "lab-only-mcp-access-token-not-a-token"


async def seed(server_id: str) -> dict[str, object]:
    """Store the lab-only token on the row and mark it connected; answer its id and status."""
    import app.models  # noqa: F401 - every table, so the row's foreign keys resolve
    from app.core.database import async_session, engine
    from app.core.security import encrypt_value
    from app.models.mcp_server import MCPServer

    async with async_session() as db:
        row = await db.get(MCPServer, uuid.UUID(server_id))
        if row is None:
            raise SystemExit(f"no MCP server {server_id}")
        if row.auth_mode != "oauth" or row.enabled:
            raise SystemExit(f"MCP server {server_id} is not a disabled OAuth server")
        now = datetime.now(UTC)
        row.access_token_enc = encrypt_value(LAB_ACCESS_TOKEN)
        row.refresh_token_enc = None
        row.token_issued_at = now
        row.token_expires_at = now + timedelta(days=365)
        row.oauth_status = "connected"
        await db.commit()
    await engine.dispose()
    return {"server": server_id, "oauth_status": "connected"}


def main() -> None:
    server_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(server_id))))


if __name__ == "__main__":
    main()
