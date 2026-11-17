"""The Supabase database access probe on a real PostgreSQL: who may probe, and with which settings.

Runs the real route of ``app/api/v1/supabase_console.py`` against an isolated
temporary database; the Management API probe and the database login are
replaced by recording fakes, so nothing leaves the process.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from fastapi import HTTPException

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import supabase_console as console
from app.api.v1.integrations import EXTRA_CONFIG_MASK
from app.api.v1.schemas.supabase import SupabaseProbeRequest
from app.core.security import encrypt_value
from app.integrations.providers import supabase as client
from app.integrations.providers import supabase_postgres as pg
from app.models.integration import Integration
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_REF = "abcdefghijklmnopqrst"
_PAT = "sbp_0123456789abcdef"
_POOLER = "aws-0-eu-west-1.pooler.supabase.com"
_STORED = {
    "project_ref": _REF,
    "db_role": "agent_reader",
    "db_password": "stored-password",
    "db_host": _POOLER,
    "db_functions": "public.get_document",
}


class _Fakes:
    def __init__(self, monkeypatch) -> None:
        self.management: list[tuple[str, str]] = []
        self.logins: list[tuple[str, str, str]] = []

        async def probe_management(access_token: str, project_ref: str) -> dict[str, Any]:
            self.management.append((access_token, project_ref))
            return {
                "projects": [{"ref": _REF, "name": "Production", "region": "eu-west-1", "status": "ACTIVE_HEALTHY"}],
                "roles": [{"name": "agent_reader", "bypass_rls": False}, {"name": "etl", "bypass_rls": True}],
                "connections": [{"host": _POOLER, "port": 5432, "mode": "session"}],
                "errors": {},
            }

        async def session(access, project_ref, work, *, database=None, allow_rls_bypass=False):
            assert allow_rls_bypass is True  # the probe reports a bypass, it does not refuse it
            self.logins.append((access.role, access.password, project_ref))
            return await work(_Connection())

        monkeypatch.setattr(client, "probe_management", probe_management)
        monkeypatch.setattr(pg, "session", session)


class _Connection:
    async def fetchrow(self, sql: str, *_args: Any) -> dict:
        assert sql == pg.ROLE_ATTRIBUTES_SQL
        return {"name": "agent_reader", "superuser": False, "bypass_rls": False, "create_role": False}

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        if sql == pg.DATABASES_SQL:
            return [{"name": "postgres"}]
        if sql == pg.EXECUTABLE_FUNCTIONS_SQL:
            assert args == (list(pg.SYSTEM_SCHEMAS),)
            return [
                {
                    "schema": "public",
                    "name": "get_document",
                    "signature": "p_document_id uuid",
                    "returns": "text",
                    "returns_set": False,
                    "security_definer": True,
                    "volatility": "s",
                    "comment": None,
                    "args": "[]",
                }
            ]
        if sql == pg.READABLE_RELATIONS_SQL:
            return [{"schema": "public", "name": "documents", "kind": "r", "rls_enabled": True}]
        raise AssertionError(sql)


def _user(label: str, *, role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{label}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name=label.title(),
        role=role,
    )


def _integration(owner: User, *, provider: str = "supabase", **sharing) -> Integration:
    return Integration(
        id=uuid.uuid4(),
        user_id=owner.id,
        provider=provider,
        auth_type="api_key",
        name="Supabase",
        status="connected",
        is_company_managed=False,
        api_key_enc=encrypt_value(_PAT),
        extra_config_enc=encrypt_value(json.dumps(_STORED)),
        **sharing,
    )


async def _status(coro) -> int:
    try:
        await coro
    except HTTPException as exc:
        return exc.status_code
    return 200


async def test_the_probe_follows_the_manage_rule_and_offers_the_choices(monkeypatch):
    fakes = _Fakes(monkeypatch)
    async with (
        scratch_database("supabaseprobe", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True) as db,
        db.sessions() as session,
    ):
        owner, admin = _user("owner"), _user("admin", role="admin")
        editor, reader, stranger = _user("editor"), _user("reader"), _user("stranger")
        session.add_all([owner, admin, editor, reader, stranger])
        await session.flush()
        integration = _integration(
            owner,
            shared_user_ids=[str(editor.id), str(reader.id)],
            shared_user_permissions={str(editor.id): "config", str(reader.id): "read_only"},
        )
        other = _integration(owner, provider="openrouter")
        session.add_all([integration, other])
        await session.commit()

        def _probe(user, body=None, target=integration):
            return console.probe_supabase_database_access(
                str(target.id), body or SupabaseProbeRequest(), user=user, db=session
            )

        for user in (owner, admin, editor):
            response = await _probe(user)
            payload = response.model_dump(by_alias=True)
            assert [project["ref"] for project in payload["projects"]] == [_REF]
            assert payload["roles"] == [{"name": "agent_reader", "bypass_rls": False}, {"name": "etl", "bypass_rls": True}]
            assert payload["databases"] == ["postgres"]
            assert payload["role_bypasses_rls"] is False
            assert payload["functions"][0]["schema"] == "public"
            assert payload["readable_relations"] == [
                {"schema": "public", "name": "documents", "kind": "r", "rls_enabled": True}
            ]
            assert payload["errors"] == {}
        # The stored token and settings, the stored password.
        assert fakes.management[-1] == (_PAT, _REF)
        assert fakes.logins[-1] == ("agent_reader", "stored-password", _REF)

        # A read-only grantee sees the integration, not its database: 403. A
        # stranger does not see it at all, nor is another provider probed: 404.
        assert await _status(_probe(reader)) == 403
        assert await _status(_probe(stranger)) == 404
        assert await _status(_probe(owner, target=other)) == 404
        logins = len(fakes.logins)

        # The mask keeps the stored password; a typed one is tried instead.
        await _probe(owner, SupabaseProbeRequest(db_password=EXTRA_CONFIG_MASK))
        assert fakes.logins[-1] == ("agent_reader", "stored-password", _REF)
        await _probe(owner, SupabaseProbeRequest(db_role="reporting", db_password="typed"))
        assert fakes.logins[-1] == ("reporting", "typed", _REF)
        assert len(fakes.logins) == logins + 2

        # The stored password goes only to the role, host and project it was
        # saved for: another one, without a typed password, is never tried.
        for change in (
            {"db_role": "reporting"},
            {"db_host": "aws-0-us-east-1.pooler.supabase.com"},
            {"project_ref": "zyxwvutsrqponmlkjihg"},
        ):
            for body in (SupabaseProbeRequest(**change), SupabaseProbeRequest(**change, db_password=EXTRA_CONFIG_MASK)):
                response = await _probe(owner, body)
                assert response.errors["database"].startswith("Type the role's password")
        assert len(fakes.logins) == logins + 2
        # The same target spelt differently keeps it.
        await _probe(owner, SupabaseProbeRequest(db_host=_POOLER.upper(), project_ref=f"https://{_REF}.supabase.co"))
        assert fakes.logins[-1] == ("agent_reader", "stored-password", _REF)

        # An unusable setting is reported, never tried.
        response = await _probe(owner, SupabaseProbeRequest(db_host="evil.example.com"))
        assert response.functions is None
        assert response.errors["database"].startswith("The database role is unusable: the database host must be")
        assert len(fakes.logins) == logins + 3


async def test_a_body_field_the_probe_does_not_know_is_a_422():
    with pytest.raises(ValueError):
        SupabaseProbeRequest.model_validate({"db_functions": "public.f"})


def test_the_direct_read_check_counts_a_column_grant():
    assert "has_any_column_privilege(c.oid, 'SELECT')" in pg.READABLE_RELATIONS_SQL
    assert "has_table_privilege" not in pg.READABLE_RELATIONS_SQL
