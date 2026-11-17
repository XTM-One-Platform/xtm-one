"""General Assistant integration bindings follow the real visibility filter.

The three-tier filter (JSONB group / user grants, ``access_restricted``) is run
by PostgreSQL itself: the tables are created in a throwaway schema on one
connection and dropped afterwards.
"""

from __future__ import annotations

import os
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.api.v1.helpers as helpers
import app.api.v1.settings as settings_api
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.relay import RelayGroup
from app.models.user import User
from app.services.integration_credentials import visible_integration_ids

pytestmark = pytest.mark.integration

_ME = uuid.uuid4()
_COLLEAGUE = uuid.uuid4()
_MY_GROUP = str(uuid.uuid4())
_OTHER_GROUP = str(uuid.uuid4())

_RESTRICTED = {"is_company_managed": True, "access_restricted": True}

#: case -> (integration columns, visible to _ME). Whether an OAuth token may be
#: USED is the OAuth token gate's question, not this one: an unshared OAuth
#: integration the user can see is visible here.
_CASES: dict[str, tuple[dict, bool]] = {
    "own": ({"user_id": _ME}, True),
    "foreign_private_api_key": ({"auth_type": "api_key"}, False),
    "foreign_private_oauth": ({}, False),
    "acknowledged_oauth_shared_with_another_group": (
        {"shared_group_ids": [_OTHER_GROUP], "oauth_share_acknowledged": True},
        False,
    ),
    "group_shared_api_key": ({"auth_type": "api_key", "shared_group_ids": [_MY_GROUP]}, True),
    "user_granted_api_key": ({"auth_type": "api_key", "shared_user_ids": [str(_ME)]}, True),
    "company_managed": ({"is_company_managed": True}, True),
    "company_managed_restricted_to_others": (_RESTRICTED, False),
    "company_managed_restricted_to_my_group": (
        {**_RESTRICTED, "shared_group_ids": [_MY_GROUP]},
        True,
    ),
    "company_managed_restricted_to_me": ({**_RESTRICTED, "shared_user_ids": [str(_ME)]}, True),
}


def _database_url() -> str:
    return os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot"
    ).strip()


@pytest.fixture
async def session(monkeypatch):
    schema = f"ga_bindings_{uuid.uuid4().hex[:10]}"
    engine = create_async_engine(_database_url())
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")

    async def _groups(_db, user_id):
        return [_MY_GROUP] if user_id == _ME else []

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    tables = (
        User.__table__,
        Group.__table__,
        GroupMember.__table__,
        RelayGroup.__table__,
        Integration.__table__,
        GeneralAssistantConfig.__table__,
    )
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            await conn.execute(text(f'SET search_path TO "{schema}"'))
            for table in tables:
                await conn.run_sync(lambda c, t=table: t.create(c))
            db = AsyncSession(bind=conn)
            for user_id in (_ME, _COLLEAGUE):
                db.add(
                    User(id=user_id, email=f"{user_id}@example.com", full_name="u", hashed_password="x")
                )
            await db.flush()
            try:
                yield db
            finally:
                await db.close()
                await conn.rollback()
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                await conn.commit()
    finally:
        await engine.dispose()


async def _add_cases(session) -> dict[str, Integration]:
    rows = {}
    for case, (columns, _visible) in _CASES.items():
        rows[case] = Integration(
            **{
                "id": uuid.uuid4(),
                "user_id": _COLLEAGUE,
                "provider": "slack",
                "name": case,
                "auth_type": "oauth",
                "status": "connected",
                "access_token_enc": "enc",
                **columns,
            }
        )
        session.add(rows[case])
    await session.flush()
    return rows


async def test_visible_integration_ids_is_the_three_tier_rule(session):
    rows = await _add_cases(session)

    visible = await visible_integration_ids(session, _ME, [str(r.id) for r in rows.values()])

    expected = {str(rows[c].id) for c, (_cols, is_visible) in _CASES.items() if is_visible}
    assert visible == expected


@pytest.mark.parametrize("role", ["user", "admin"])
async def test_saving_refuses_an_added_integration_the_user_cannot_see(session, role):
    """An admin is held to the rule too: the picker lists what they can see."""
    rows = await _add_cases(session)
    user = SimpleNamespace(id=_ME, ga_integration_bindings=None, role=role)

    for case, (_cols, is_visible) in _CASES.items():
        requested = {"slack": [str(rows[case].id)]}
        if is_visible:
            assert await settings_api._checked_ga_integration_bindings(
                session, user, requested
            ) == requested, case
        else:
            with pytest.raises(HTTPException) as exc:
                await settings_api._checked_ga_integration_bindings(session, user, requested)
            assert exc.value.status_code == 403, case


async def test_saving_keeps_the_platform_s_bindings_the_user_cannot_see(session):
    rows = await _add_cases(session)
    hidden = str(rows["foreign_private_api_key"].id)
    session.add(GeneralAssistantConfig(integration_bindings={"slack": [hidden]}))
    await session.flush()
    user = SimpleNamespace(id=_ME, ga_integration_bindings=None, role="user")

    saved = await settings_api._checked_ga_integration_bindings(
        session, user, {"slack": [hidden]}
    )

    assert saved == {"slack": [hidden]}
