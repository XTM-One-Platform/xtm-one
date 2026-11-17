"""The lab's OAuth integrations that hold a token, and its Nova rule source with a last sync (#11143).

Started by ``lab.py`` (``seed_kb_sync_sources``) with the backend's interpreter, from ``backend/``
and with the backend's environment, once ``POST /api/v1/integrations`` created the rows: a
Microsoft and a Google OAuth integration on lab-only apps (``configured``) and a company-managed
Nova integration (``connected``, whose ``on_connect`` hook created its rule source).

No API stores a token or a sync outcome without the provider round-trip, so this writes what the
OAuth callback and a rule sync write, through the backend's own models and ``encrypt_value``:

* on each OAuth row: ``access_token_enc`` (a lab-only string, never sent anywhere), no refresh
  token, an expiry a year ahead (the refresher never calls the provider), every requested scope
  granted, ``status`` ``connected``; so the knowledge base Sources tab enables its Microsoft and
  Google "Configure Source" cards and ``POST /knowledge-bases/<id>/sync-sources`` binds them;
* on the Nova rule source: ``sync_interval`` ``manual`` (the scheduler never pulls from GitHub),
  ``last_synced_at`` now, ``last_sync_status`` ``ok``; so the Rules tab renders the Rule source
  card with its schedule picker, its last sync and "Sync now".

Arguments: the Nova integration id, the JSON file the outcome is written to, then the OAuth
integration ids. Idempotent.

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
LAB_ACCESS_TOKEN = "lab-only-oauth-access-token-not-a-token"  # noqa: S105 - lab-only value


async def seed(nova_id: str, oauth_ids: list[str]) -> dict[str, object]:
    """Connect the OAuth rows and stamp a last sync on the Nova source; answer the ids."""
    import app.models  # noqa: F401 - every table, so the rows' foreign keys resolve
    from sqlalchemy import select

    from app.api.v1.integration_catalog import PROVIDER_CATALOG
    from app.core.database import async_session, engine
    from app.core.security import encrypt_value
    from app.integrations.granted_scopes import split_scopes
    from app.models.integration import Integration
    from app.models.nova_rule_source import NovaRuleSource

    async with async_session() as db:
        for integration_id in oauth_ids:
            row = await db.get(Integration, uuid.UUID(integration_id))
            if row is None:
                raise SystemExit(f"no integration {integration_id}")
            requested = split_scopes(row.scopes or PROVIDER_CATALOG[row.provider].get("scopes"))
            row.access_token_enc = encrypt_value(LAB_ACCESS_TOKEN)
            row.refresh_token_enc = None
            row.token_expires_at = datetime.now(UTC) + timedelta(days=365)
            row.granted_scopes = " ".join(requested)
            row.status = "connected"
        source = (
            await db.execute(select(NovaRuleSource).where(NovaRuleSource.integration_id == uuid.UUID(nova_id)))
        ).scalars().first()
        if source is None:
            raise SystemExit(f"the Nova integration {nova_id} has no rule source: its on_connect hook did not run")
        source.sync_interval = "manual"
        source.last_synced_at = datetime.now(UTC)
        source.last_sync_status = "ok"
        source.last_sync_error = None
        await db.commit()
        source_id = str(source.id)
    await engine.dispose()
    return {"nova": nova_id, "nova_source": source_id, "oauth": oauth_ids}


def main() -> None:
    nova_id, out, *oauth_ids = sys.argv[1:]
    Path(out).write_text(json.dumps(asyncio.run(seed(nova_id, oauth_ids))))


if __name__ == "__main__":
    main()
