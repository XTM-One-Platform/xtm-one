"""An editor's re-point and an admin's vault reference, written at the same time, on a real PostgreSQL.

A non-admin editor may move an integration or an MCP server only while it holds
no reference they could not write. The check reads the row; without a lock, an
admin committing an admins-only reference between the editor's read and the
editor's commit would leave the reference attached to the editor's new
destination - the secret sent where the editor chose. Both update routes read
the row ``FOR UPDATE``, so the editor's edit waits for the admin's, sees the
reference, and is refused.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.deps import AfterCommit
from app.api.v1 import integrations as integration_routes
from app.api.v1 import mcp_servers as mcp_routes
from app.api.v1.schemas.integration import UpdateIntegrationRequest
from app.api.v1.schemas.mcp_server import MCPServerUpdate
from app.core.security import decrypt_json, decrypt_stored_value, encrypt_value
from app.models.integration import Integration
from app.models.mcp_server import MCPServer
from app.models.user import User
from app.services.vault.resolver import SecretResolver, set_resolver
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_vault_references import FakeVault

pytestmark = pytest.mark.integration

_REFERENCE = "vault://corp/xtm-one/acme#token"


def _user(label: str, *, role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name=label.title(),
        role=role,
    )


def _pause_the_first_edit(monkeypatch, routes) -> tuple[asyncio.Event, asyncio.Event]:
    """Stop the first edit at the route's token-URL check - its first await after its read and its
    checks - with the row in hand, until released."""
    holds_row, release = asyncio.Event(), asyncio.Event()
    real_check = routes.require_allowed_token_url
    paused: list[bool] = []

    async def _pausing_check(token_url):
        if not paused:
            paused.append(True)
            holds_row.set()
            await release.wait()
        return await real_check(token_url)

    monkeypatch.setattr(routes, "require_allowed_token_url", _pausing_check)
    return holds_row, release


async def _race(
    admin_edit: Callable[[], Awaitable[object]],
    editor_edit: Callable[[], Awaitable[object]],
    holds_row: asyncio.Event,
    release: asyncio.Event,
) -> None:
    admin = asyncio.ensure_future(admin_edit())
    await asyncio.wait_for(holds_row.wait(), timeout=10)
    editor = asyncio.ensure_future(editor_edit())
    # The editor's read waits on the admin's lock instead of reading the old row.
    await asyncio.sleep(1.0)
    assert not editor.done()

    release.set()
    await asyncio.wait_for(admin, timeout=10)
    with pytest.raises(HTTPException) as refused:
        await asyncio.wait_for(editor, timeout=10)
    assert refused.value.status_code == 403


@pytest.fixture
def corp_vault():
    set_resolver(SecretResolver({"corp": FakeVault("corp")}, {}, 300))
    yield
    set_resolver(None)


async def test_an_mcp_server_repoint_waits_for_an_admin_reference_and_is_refused(monkeypatch, corp_vault):
    holds_row, release = _pause_the_first_edit(monkeypatch, mcp_routes)
    async with scratch_database(
        "vaultrepointmcp", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        editor, admin = _user("editor"), _user("admin", role="admin")
        async with db.sessions() as seed:
            seed.add_all([editor, admin])
            await seed.flush()
            server = MCPServer(
                name="Acme",
                transport="streamable_http",
                url="https://mcp.acme.test/mcp",
                auth_mode="headers",
                user_id=editor.id,
                is_company_managed=False,
                headers_enc=encrypt_value(json.dumps({"Authorization": "Bearer literal"})),
            )
            seed.add(server)
            await seed.commit()
            server_id = str(server.id)

        async with db.sessions() as admin_session, db.sessions() as editor_session:
            await _race(
                lambda: mcp_routes.update_mcp_server(
                    server_id, MCPServerUpdate(headers={"Authorization": _REFERENCE}), None, user=admin, db=admin_session
                ),
                lambda: mcp_routes.update_mcp_server(
                    server_id, MCPServerUpdate(url="https://mcp.editor.test/mcp"), None, user=editor, db=editor_session
                ),
                holds_row,
                release,
            )

        async with db.sessions() as check:
            stored = (await check.execute(select(MCPServer).where(MCPServer.id == server.id))).scalar_one()
            assert stored.url == "https://mcp.acme.test/mcp"
            assert json.loads(decrypt_stored_value(stored.headers_enc)) == {"Authorization": _REFERENCE}


async def test_an_integration_repoint_waits_for_an_admin_reference_and_is_refused(monkeypatch, corp_vault):
    holds_row, release = _pause_the_first_edit(monkeypatch, integration_routes)
    async with scratch_database(
        "vaultrepointinteg", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        editor, admin = _user("editor"), _user("admin", role="admin")
        async with db.sessions() as seed:
            seed.add_all([editor, admin])
            await seed.flush()
            integration = Integration(
                name="GitHub",
                provider="github",
                auth_type="api_key",
                status="connected",
                user_id=editor.id,
                is_company_managed=False,
                api_key_enc=encrypt_value("literal-token"),
                extra_config_enc=encrypt_value(json.dumps({"base_url": "https://api.github.com"})),
            )
            seed.add(integration)
            await seed.commit()
            integration_id = str(integration.id)

        async with db.sessions() as admin_session, db.sessions() as editor_session:
            await _race(
                lambda: integration_routes.update_integration(
                    integration_id,
                    UpdateIntegrationRequest(api_key=_REFERENCE),
                    None,
                    user=admin,
                    db=admin_session,
                    after_commit=AfterCommit(),
                ),
                lambda: integration_routes.update_integration(
                    integration_id,
                    UpdateIntegrationRequest(extra_config={"base_url": "https://github.editor.test"}),
                    None,
                    user=editor,
                    db=editor_session,
                    after_commit=AfterCommit(),
                ),
                holds_row,
                release,
            )

        async with db.sessions() as check:
            stored = (await check.execute(select(Integration).where(Integration.id == integration.id))).scalar_one()
            assert decrypt_json(stored.extra_config_enc) == {"base_url": "https://api.github.com"}
            assert decrypt_stored_value(stored.api_key_enc) == _REFERENCE
