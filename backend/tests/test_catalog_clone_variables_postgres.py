"""The agent page's missing-variable notice, against real PostgreSQL.

A NON-admin installer adds a catalog agent whose persona names a
company-managed secret: the install lists it, ``GET /agents/{id}/missing-
variables`` keeps listing it until the installer gives it a value, and never
lists a variable the caller cannot see.

``integration``-marked and skipped when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.v1 import agents as agents_api
from app.api.v1 import public_catalog
from app.models.agent import Agent
from app.models.user import User
from app.models.variable import Variable
from tests.test_catalog_clone_visibility_postgres import _temp_database

pytestmark = pytest.mark.integration


def _user(role: str, name: str) -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name=name.title(),
        role=role,
    )


@pytest.mark.asyncio
async def test_the_notice_lists_an_unfilled_secret_until_it_has_a_value(monkeypatch):
    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(public_catalog, "log_audit", _noop)
    async with _temp_database("catalog_clone_vars") as session:
        async with session() as db:
            admin, installer, other = (
                _user("admin", "admin"),
                _user("user", "installer"),
                _user("user", "other"),
            )
            db.add_all([admin, installer, other])
            await db.flush()
            secret = Variable(
                user_id=admin.id,
                name="API_KEY",
                is_secret=True,
                value_enc="cipher",
                is_company_managed=True,
            )
            db.add(secret)
            await db.flush()
            agent = Agent(
                user_id=admin.id,
                name="Published",
                slug=f"published-{uuid.uuid4().hex[:8]}",
                is_company_managed=True,
                is_public_catalog=True,
                persona=f"Use {{{{var_id.{secret.id}}}}}",
            )
            db.add(agent)
            await db.commit()
            agent_id, installer_id, other_id = agent.id, installer.id, other.id

        async with session() as db:
            user = await db.get(User, installer_id)
            installed = await public_catalog.add_catalog_item_to_account(
                entity_type="agents",
                entity_id=str(agent_id),
                body=None,
                user=user,
                db=db,
                _ai_gate=None,
            )
        [needed] = installed.variables_needing_value
        assert needed.name == "API_KEY" and needed.is_secret

        async def _missing(user_id):
            async with session() as db:
                user = await db.get(User, user_id)
                return await agents_api.list_agent_missing_variables(
                    installed.id, user=user, db=db
                )

        assert [v.id for v in await _missing(installer_id)] == [needed.id]

        # Another non-admin who cannot see the agent gets a 404, not the list.
        with pytest.raises(HTTPException) as refused:
            await _missing(other_id)
        assert refused.value.status_code == 404

        # Filled in: the notice goes away.
        async with session() as db:
            copy = (
                await db.execute(select(Variable).where(Variable.id == uuid.UUID(needed.id)))
            ).scalar_one()
            assert copy.user_id == installer_id and copy.value_enc is None
            copy.value_enc = "installer-cipher"
            await db.commit()
        assert await _missing(installer_id) == []
