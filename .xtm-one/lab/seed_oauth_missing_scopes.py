"""The lab's OAuth integration whose stored token lacks a requested scope (#9813).

Started by ``lab.py`` (``seed_oauth_missing_scopes``) with the backend's interpreter, from
``backend/`` and with the backend's environment, once ``POST /api/v1/integrations`` created the
row (a Google OAuth integration on a lab-only app, ``configured``). Without it no lab row holds
a token, so ``missing_scopes`` stays ``None`` on ``GET /api/v1/integrations`` and the detail
page's "Requested scopes not granted" state only renders under a ``window.fetch`` override.

No API stores a token without the provider round-trip, so this writes what the OAuth callback
writes on the row through the backend's own model and ``encrypt_value``:

* ``access_token_enc``: a lab-only string, never sent anywhere (no refresh token, an expiry a
  year ahead, so the refresher never calls Google);
* ``granted_scopes``: the scopes the row requests minus the last non-identity one;
* ``status``: ``connected``.

Arguments: the integration id and the JSON file the outcome is written to (the id and the
scopes reported missing, checked with ``granted_scopes.missing_scopes``). Idempotent.

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
LAB_ACCESS_TOKEN = "lab-only-google-access-token-not-a-token"

#: Identity scopes ``missing_scopes`` never reports: the one left out is never one of them.
IDENTITY_SCOPES = frozenset({"openid", "email", "profile", "offline_access"})


def granted_without_one(requested: list[str]) -> tuple[list[str], str]:
    """*requested* minus its last non-identity scope, and that scope."""
    dropped = next(scope for scope in reversed(requested) if scope.lower() not in IDENTITY_SCOPES)
    return [scope for scope in requested if scope != dropped], dropped


async def seed(integration_id: str) -> dict[str, object]:
    """Store the token and the partial grant on the row; answer its id and the missing scopes."""
    import app.models  # noqa: F401 - every table, so the row's foreign keys resolve
    from app.api.v1.integration_catalog import PROVIDER_CATALOG
    from app.core.database import async_session, engine
    from app.core.security import encrypt_value
    from app.integrations.granted_scopes import missing_scopes, split_scopes
    from app.models.integration import Integration

    async with async_session() as db:
        row = await db.get(Integration, uuid.UUID(integration_id))
        if row is None:
            raise SystemExit(f"no integration {integration_id}")
        requested = split_scopes(row.scopes or PROVIDER_CATALOG[row.provider].get("scopes"))
        granted, dropped = granted_without_one(requested)
        row.access_token_enc = encrypt_value(LAB_ACCESS_TOKEN)
        row.refresh_token_enc = None
        row.token_expires_at = datetime.now(UTC) + timedelta(days=365)
        row.granted_scopes = " ".join(granted)
        row.status = "connected"
        missing = missing_scopes(requested, row.granted_scopes) or []
        if dropped not in missing:
            raise SystemExit(f"missing_scopes answers {missing}, not {dropped}")
        await db.commit()
    await engine.dispose()
    return {"integration": integration_id, "missing": missing}


def main() -> None:
    integration_id, out = sys.argv[1:3]
    Path(out).write_text(json.dumps(asyncio.run(seed(integration_id))))


if __name__ == "__main__":
    main()
