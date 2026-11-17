"""What the MCP API shows of a server's credentials, and what a save keeps.

``GET /mcp-servers`` and ``GET /mcp-servers/{id}`` answer everyone who can see
a server - a ``read_only`` grantee, every user for a company-managed one - so
``_to_response`` masks a credential in its arguments and URLs with
``redact_secret_args`` / ``redact_secret_text``, the export's non-manager
projection: a token-shaped argument, a secret-named ``NAME=value`` or
``--flag=value``, the value after a secret-named flag, a URL's password and
secret query values, an ``Authorization`` scheme's token.

The edit form sends the arguments and URLs back as it read them, and
``PUT /mcp-servers/{id}`` stored them as received, so a rename replaced every
token with its mask. A masked value sent back now keeps the stored one,
a changed one is saved as typed, and a mask that cannot be put back to one
stored value is refused with nothing written. Driven through the real router on
PostgreSQL (same scratch-database recipe as
``test_request_column_limits_postgres.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import mcp_servers as mcp_servers_api
from app.core.database import get_db
from app.core.utils import mask_secret_arg, redact_secret_args
from app.models.group import Group, GroupMember
from app.models.mcp_server import MCPServer
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

TOKEN = "ghp_" + "a" * 36
#: Same first characters as ``TOKEN``, hence the same mask.
OTHER = "ghp_aa" + "b" * 34
NEW_TOKEN = "ghp_" + "c" * 36
STORED_ARGS = ["-y", "@modelcontextprotocol/server-github", "--token", TOKEN]
SHOWN_ARGS = ["-y", "@modelcontextprotocol/server-github", "--token", "***"]

#: One stdio server's arguments per masked form, with how the API shows them.
MASKED_FORMS = {
    "bare token": (["-y", "srv", TOKEN], ["-y", "srv", mask_secret_arg(TOKEN)]),
    "NAME=value": (
        ["-e", f"GITHUB_PERSONAL_ACCESS_TOKEN={TOKEN}", "ghcr.io/github/github-mcp-server"],
        ["-e", "GITHUB_PERSONAL_ACCESS_TOKEN=***", "ghcr.io/github/github-mcp-server"],
    ),
    "--flag=value": ([f"--token={TOKEN}", "--port", "1"], ["--token=***", "--port", "1"]),
    "--flag value": (
        ["--token", "glpat-short", "--client-secret", "s3cret"],
        ["--token", "***", "--client-secret", "***"],
    ),
    "URL password": (
        ["--dsn", "postgresql://app:S3cret@db/prod"],
        ["--dsn", "postgresql://app:***@db/prod"],
    ),
    "Authorization scheme": (
        ["--header", "Authorization: Bearer eyJ.payload.sig"],
        ["--header", "Authorization: Bearer ***"],
    ),
}
#: The secret each form must never show.
SECRETS = (TOKEN, "glpat-short", "s3cret", "S3cret", "eyJ.payload.sig")

REMOTE_URLS = {
    "url": "https://user:pw-s3cret@mcp.example.com/sse?token=qt0k-xyz&mode=ro",
    "authorize_url": "https://idp.example.com/authorize?client_secret=cs-s3cret",
    "token_url": "https://idp.example.com/token?client_secret=cs-s3cret",
    "registration_url": "https://idp.example.com/register?token=rg-s3cret",
}
SHOWN_URLS = {
    "url": "https://user:***@mcp.example.com/sse?token=***&mode=ro",
    "authorize_url": "https://idp.example.com/authorize?client_secret=***",
    "token_url": "https://idp.example.com/token?client_secret=***",
    "registration_url": "https://idp.example.com/register?token=***",
}

ALL_MASKED_ARGS = [arg for stored, _ in MASKED_FORMS.values() for arg in stored]
ALL_SHOWN_ARGS = [arg for _, shown in MASKED_FORMS.values() for arg in shown]


def test_the_forms_are_what_the_api_shows():
    """The tables above are ``redact_secret_args``'s output, alone and joined."""
    for stored, shown in MASKED_FORMS.values():
        assert redact_secret_args(stored) == shown
    assert redact_secret_args(ALL_MASKED_ARGS) == ALL_SHOWN_ARGS


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "mcp_masked_args",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as db:
        yield db.sessions


async def _user(sessions: async_sessionmaker, role: str = "user") -> User:
    async with sessions() as db:
        user = User(
            email=f"{role}-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name=role.title(),
            role=role,
        )
        db.add(user)
        await db.commit()
        return user


async def _server(sessions: async_sessionmaker, **fields) -> uuid.UUID:
    fields.setdefault("transport", "stdio")
    fields.setdefault("command", "npx")
    async with sessions() as db:
        srv = MCPServer(name=fields.pop("name", "GitHub"), **fields)
        db.add(srv)
        await db.commit()
        return srv.id


async def _owner_and_server(
    sessions: async_sessionmaker, args: list[str]
) -> tuple[User, uuid.UUID]:
    """An ordinary user and the stdio server they own."""
    user = await _user(sessions)
    return user, await _server(sessions, user_id=user.id, args=args)


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """The real MCP server router, as *user*, one session per request."""
    app = FastAPI()
    app.include_router(mcp_servers_api.router, prefix="/api/v1/mcp-servers")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    async def _current_user(db: AsyncSession = Depends(get_db)) -> User:
        return await db.get(User, user.id)

    app.dependency_overrides[deps.get_current_user] = _current_user
    app.dependency_overrides[get_db] = _get_db
    for gate in (deps.require_agentic_ai, deps.require_ai_enabled, deps.require_agentic_ai_licensed):
        app.dependency_overrides[gate] = lambda: None
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _stored(sessions: async_sessionmaker, server_id: uuid.UUID) -> MCPServer:
    async with sessions() as db:
        return await db.get(MCPServer, server_id)


async def _assert_masked_for(
    sessions: async_sessionmaker, viewer: User, server_id: uuid.UUID, *, can_manage: bool
) -> None:
    """*viewer* reads the server masked, by id and in the list."""
    async with _client(sessions, viewer) as client:
        detail = await client.get(f"/api/v1/mcp-servers/{server_id}")
        listed = await client.get("/api/v1/mcp-servers")
    assert detail.status_code == 200
    assert listed.status_code == 200
    [row] = [s for s in listed.json() if s["id"] == str(server_id)]
    for shown in (detail.json(), row):
        assert shown["can_manage"] is can_manage
        assert shown["args"] == ALL_SHOWN_ARGS
        assert {field: shown[field] for field in SHOWN_URLS} == SHOWN_URLS
    for secret in (*SECRETS, "pw-s3cret", "qt0k-xyz", "cs-s3cret", "rg-s3cret"):
        assert secret not in detail.text
        assert secret not in listed.text


# ---------------------------------------------------------------------------
# Who reads a server: every one of them reads it masked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_read_only_grantee_reads_every_credential_masked():
    async with _database() as sessions:
        owner = await _user(sessions)
        grantee = await _user(sessions)
        server_id = await _server(
            sessions,
            user_id=owner.id,
            args=ALL_MASKED_ARGS,
            **REMOTE_URLS,
            shared_user_ids=[str(grantee.id)],
            shared_user_permissions={str(grantee.id): "read_only"},
        )
        await _assert_masked_for(sessions, grantee, server_id, can_manage=False)

        # Nor can the grantee write it.
        async with _client(sessions, grantee) as client:
            refused = await client.put(
                f"/api/v1/mcp-servers/{server_id}", json={"name": "Taken"}
            )
        assert refused.status_code == 403
        assert (await _stored(sessions, server_id)).args == ALL_MASKED_ARGS


@pytest.mark.asyncio
async def test_a_read_only_group_member_reads_every_credential_masked():
    async with _database() as sessions:
        owner = await _user(sessions)
        member = await _user(sessions)
        async with sessions() as db:
            group = Group(name=f"readers-{uuid.uuid4().hex[:8]}")
            db.add(group)
            await db.flush()
            db.add(GroupMember(group_id=group.id, user_id=member.id))
            await db.commit()
        server_id = await _server(
            sessions,
            user_id=owner.id,
            args=ALL_MASKED_ARGS,
            **REMOTE_URLS,
            shared_group_ids=[str(group.id)],
            shared_group_permissions={str(group.id): "read_only"},
        )
        await _assert_masked_for(sessions, member, server_id, can_manage=False)


@pytest.mark.asyncio
async def test_any_user_reads_a_company_managed_server_masked():
    async with _database() as sessions:
        viewer = await _user(sessions)
        server_id = await _server(
            sessions,
            user_id=None,
            is_company_managed=True,
            args=ALL_MASKED_ARGS,
            **REMOTE_URLS,
        )
        await _assert_masked_for(sessions, viewer, server_id, can_manage=False)


@pytest.mark.asyncio
async def test_the_owner_reads_masked_too_and_a_config_grantee_save_keeps_the_secrets():
    async with _database() as sessions:
        owner = await _user(sessions)
        editor = await _user(sessions)
        server_id = await _server(
            sessions,
            user_id=owner.id,
            args=ALL_MASKED_ARGS,
            **REMOTE_URLS,
            shared_user_ids=[str(editor.id)],
            shared_user_permissions={str(editor.id): "config"},
        )
        await _assert_masked_for(sessions, owner, server_id, can_manage=True)
        await _assert_masked_for(sessions, editor, server_id, can_manage=True)

        async with _client(sessions, editor) as client:
            saved = await client.put(
                f"/api/v1/mcp-servers/{server_id}",
                json={"name": "Renamed", "args": ALL_SHOWN_ARGS, **SHOWN_URLS},
            )

        assert saved.status_code == 200
        srv = await _stored(sessions, server_id)
        assert srv.name == "Renamed"
        assert srv.args == ALL_MASKED_ARGS
        assert {field: getattr(srv, field) for field in REMOTE_URLS} == REMOTE_URLS


# ---------------------------------------------------------------------------
# What a save keeps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_each_masked_form_sent_back_as_read_keeps_the_stored_value():
    async with _database() as sessions:
        owner = await _user(sessions)
        async with _client(sessions, owner) as client:
            for form, (stored, shown) in MASKED_FORMS.items():
                server_id = await _server(sessions, user_id=owner.id, args=stored)
                read = await client.get(f"/api/v1/mcp-servers/{server_id}")
                assert read.json()["args"] == shown, form

                # What the edit form sends for a rename once the arguments
                # field was touched: every field, the arguments as shown.
                saved = await client.put(
                    f"/api/v1/mcp-servers/{server_id}",
                    json={"name": f"{form} (renamed)", "transport": "stdio",
                          "command": "npx", "args": shown},
                )
                assert saved.status_code == 200, form
                assert saved.json()["args"] == shown, form
                srv = await _stored(sessions, server_id)
                assert srv.name == f"{form} (renamed)", form
                assert srv.args == stored, form


@pytest.mark.asyncio
async def test_masked_urls_sent_back_as_read_keep_the_stored_urls(monkeypatch):
    checked: list[str | None] = []

    async def _token_url_policy(token_url):
        checked.append(token_url)

    monkeypatch.setattr(mcp_servers_api, "require_allowed_token_url", _token_url_policy)
    async with _database() as sessions:
        owner = await _user(sessions)
        server_id = await _server(
            sessions,
            user_id=owner.id,
            transport="streamable_http",
            command=None,
            auth_mode="oauth",
            **REMOTE_URLS,
        )
        async with _client(sessions, owner) as client:
            read = (await client.get(f"/api/v1/mcp-servers/{server_id}")).json()
            assert {field: read[field] for field in SHOWN_URLS} == SHOWN_URLS
            # The edit form sends every OAuth URL back as shown.
            saved = await client.put(
                f"/api/v1/mcp-servers/{server_id}",
                json={"name": "Remote", "transport": "streamable_http", **SHOWN_URLS},
            )

        assert saved.status_code == 200
        # The token endpoint policy judged the URL that is saved, not its mask.
        assert checked == [REMOTE_URLS["token_url"]]
        srv = await _stored(sessions, server_id)
        assert srv.name == "Remote"
        assert {field: getattr(srv, field) for field in REMOTE_URLS} == REMOTE_URLS


@pytest.mark.asyncio
async def test_a_rename_sending_the_arguments_back_as_read_keeps_the_token():
    async with _database() as sessions:
        user, server_id = await _owner_and_server(sessions, STORED_ARGS)
        async with _client(sessions, user) as client:
            read = await client.get(f"/api/v1/mcp-servers/{server_id}")
            assert read.status_code == 200
            shown = read.json()["args"]
            assert shown == SHOWN_ARGS

            saved = await client.put(
                f"/api/v1/mcp-servers/{server_id}",
                json={"name": "GitHub (renamed)", "tags": ["scm"], "transport": "stdio",
                      "command": "npx", "args": shown},
            )

        assert saved.status_code == 200
        assert saved.json()["args"] == SHOWN_ARGS
        srv = await _stored(sessions, server_id)
        assert srv.name == "GitHub (renamed)"
        assert srv.args == STORED_ARGS


@pytest.mark.asyncio
async def test_an_admin_editing_another_users_server_keeps_the_token():
    async with _database() as sessions:
        _, server_id = await _owner_and_server(sessions, STORED_ARGS)
        admin = await _user(sessions, role="admin")
        assert admin.is_admin
        async with _client(sessions, admin) as client:
            shown = (await client.get(f"/api/v1/mcp-servers/{server_id}")).json()["args"]
            assert shown == SHOWN_ARGS
            saved = await client.put(
                f"/api/v1/mcp-servers/{server_id}",
                json={"name": "Shared GitHub", "args": shown},
            )

        assert saved.status_code == 200
        srv = await _stored(sessions, server_id)
        assert srv.name == "Shared GitHub"
        assert srv.args == STORED_ARGS


@pytest.mark.asyncio
async def test_an_argument_inserted_before_the_masked_token_keeps_the_token():
    async with _database() as sessions:
        user, server_id = await _owner_and_server(sessions, STORED_ARGS)
        async with _client(sessions, user) as client:
            saved = await client.put(
                f"/api/v1/mcp-servers/{server_id}",
                json={"args": ["--quiet", *SHOWN_ARGS]},
            )

        assert saved.status_code == 200
        assert (await _stored(sessions, server_id)).args == ["--quiet", *STORED_ARGS]


@pytest.mark.asyncio
async def test_a_changed_token_is_saved_as_typed():
    async with _database() as sessions:
        user, server_id = await _owner_and_server(sessions, STORED_ARGS)
        async with _client(sessions, user) as client:
            saved = await client.put(
                f"/api/v1/mcp-servers/{server_id}",
                json={"args": [*STORED_ARGS[:3], NEW_TOKEN]},
            )

        assert saved.status_code == 200
        assert saved.json()["args"] == SHOWN_ARGS
        assert (await _stored(sessions, server_id)).args == [*STORED_ARGS[:3], NEW_TOKEN]


ARGS_REFUSAL = {
    "detail": (
        "An argument is still masked and cannot be matched to exactly one "
        "stored value. Type the full value of each masked argument again."
    )
}


@pytest.mark.parametrize(
    ("stored", "sent"),
    [
        # Two tokens sharing a mask, one dropped: which one is left cannot be told.
        ([TOKEN, "-e", OTHER], ["--verbose", mask_secret_arg(TOKEN)]),
        # A ``***`` moved after another flag.
        (["--token", "glpat-short"], ["--other", "***"]),
        # A URL edited around its masked password.
        (["--dsn", "postgresql://app:S3cret@db/prod"], ["--dsn", "postgresql://app:***@db2/prod"]),
        # The mask earlier releases showed after a secret flag.
        (STORED_ARGS, [*STORED_ARGS[:3], mask_secret_arg(TOKEN)]),
    ],
    ids=["shared-mask-dropped", "moved-bare-mask", "edited-url", "old-token-mask"],
)
@pytest.mark.asyncio
async def test_a_mask_that_cannot_be_put_back_is_refused_with_nothing_written(stored, sent):
    assert redact_secret_args(stored) != stored
    async with _database() as sessions:
        user, server_id = await _owner_and_server(sessions, stored)
        async with _client(sessions, user) as client:
            refused = await client.put(
                f"/api/v1/mcp-servers/{server_id}",
                json={"name": "Renamed", "args": sent},
            )

        assert refused.status_code == 422
        assert refused.json() == ARGS_REFUSAL
        srv = await _stored(sessions, server_id)
        assert srv.name == "GitHub"
        assert srv.args == stored


@pytest.mark.asyncio
async def test_a_url_edited_around_its_mask_is_refused_with_nothing_written():
    async with _database() as sessions:
        owner = await _user(sessions)
        server_id = await _server(
            sessions, user_id=owner.id, transport="sse", command=None, url=REMOTE_URLS["url"]
        )
        async with _client(sessions, owner) as client:
            refused = await client.put(
                f"/api/v1/mcp-servers/{server_id}",
                json={"name": "Renamed", "url": "https://user:***@other.example.com/sse"},
            )

        assert refused.status_code == 422
        assert refused.json() == {
            "detail": (
                "The URL is still masked and does not match the stored one. "
                "Type the full URL again."
            )
        }
        srv = await _stored(sessions, server_id)
        assert srv.name == "GitHub"
        assert srv.url == REMOTE_URLS["url"]


@pytest.mark.asyncio
async def test_a_stored_literal_equal_to_the_tokens_mask_survives_a_rename():
    # A bare token keeps its first characters when shown, so a stored literal
    # equal to that mask reads back the same as the token.
    stored = [TOKEN, "--placeholder", mask_secret_arg(TOKEN)]
    async with _database() as sessions:
        user, server_id = await _owner_and_server(sessions, stored)
        async with _client(sessions, user) as client:
            shown = (await client.get(f"/api/v1/mcp-servers/{server_id}")).json()["args"]
            assert shown == [mask_secret_arg(TOKEN), "--placeholder", mask_secret_arg(TOKEN)]
            saved = await client.put(
                f"/api/v1/mcp-servers/{server_id}", json={"name": "Renamed", "args": shown}
            )
            refused = await client.put(
                f"/api/v1/mcp-servers/{server_id}",
                json={"name": "Renamed again", "args": shown[:2]},
            )

        assert saved.status_code == 200
        assert refused.status_code == 422
        srv = await _stored(sessions, server_id)
        assert srv.name == "Renamed"
        assert srv.args == stored


@pytest.mark.asyncio
async def test_masked_arguments_sent_back_are_not_a_repoint_of_a_retained_reference(monkeypatch):
    """The re-point check sees the restored arguments: sent back as
    read they change nothing, while a token moved elsewhere still does."""
    monkeypatch.setattr(
        mcp_servers_api,
        "retained_reference_writer_problem",
        lambda *_args, **_kwargs: "keeps a reference you could not write",
    )
    async with _database() as sessions:
        user, server_id = await _owner_and_server(sessions, STORED_ARGS)
        async with _client(sessions, user) as client:
            shown = (await client.get(f"/api/v1/mcp-servers/{server_id}")).json()["args"]
            saved = await client.put(
                f"/api/v1/mcp-servers/{server_id}", json={"name": "Renamed", "args": shown}
            )
            # The ``--token ***`` pair moved to the front: restored, it is
            # another argument list than the stored one.
            moved = await client.put(
                f"/api/v1/mcp-servers/{server_id}",
                json={"name": "Moved", "args": [*shown[2:], *shown[:2]]},
            )

        assert saved.status_code == 200
        assert moved.status_code == 403
        srv = await _stored(sessions, server_id)
        assert srv.name == "Renamed"
        assert srv.args == STORED_ARGS


@pytest.mark.asyncio
async def test_the_owner_export_carries_the_real_token():
    """The export a manager downloads is the raw row, so importing it back
    gives a working server: no mask travels through the export/import path."""
    async with _database() as sessions:
        user, server_id = await _owner_and_server(sessions, STORED_ARGS)
        async with _client(sessions, user) as client:
            exported = await client.get(f"/api/v1/mcp-servers/{server_id}/export")
            assert exported.status_code == 200
            bundle = exported.json()
            assert bundle["mcp_server"]["args"] == STORED_ARGS

            imported = await client.post("/api/v1/mcp-servers/import", json=bundle)

        assert imported.status_code == 201
        assert (await _stored(sessions, uuid.UUID(imported.json()["id"]))).args == STORED_ARGS
