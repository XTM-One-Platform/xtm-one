"""CRUD API tests for Nova prompt-safety rules (#nova) — real PostgreSQL.

Drives the router coroutines directly against a throwaway database (same
recipe as ``test_nova_guard_postgres.py``): exercises rule-text validation on
write, the block/log action guard, three-tier visibility, the admin-only
company-managed gate, and the dry ``/validate`` endpoint. ``log_audit`` is
stubbed so the tables under test stay minimal.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.api.v1.nova_rules as nova_rules
from app.api.v1.schemas.nova_rule import (
    NovaRuleCreate,
    NovaRuleUpdate,
    NovaRuleValidateRequest,
)
from app.models.group import Group, GroupMember
from app.models.nova_rule import NovaRule
from app.models.user import User
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_TABLES = [
    User.__table__,
    Group.__table__,
    GroupMember.__table__,
    NovaRule.__table__,
]

_GOOD_RULE = (
    'rule Jailbreak { meta: category = "Jailbreak"\n'
    '  keywords: $dan = "do anything now"\n'
    "  condition: keywords.$dan }"
)


@pytest.fixture(autouse=True)
def _stub_audit(monkeypatch):
    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(nova_rules, "log_audit", _noop)


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("novaapi", tables=_TABLES) as db:
        yield db.sessions


async def _make_user(session, *, admin: bool) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{'admin' if admin else 'user'}-{uuid.uuid4().hex[:8]}@example.com",
        full_name="Admin" if admin else "Normal",
        role="admin" if admin else "user",
        hashed_password="x",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


@pytest.mark.asyncio
async def test_create_validates_rule_text_and_defaults_category():
    async with _temp_database() as sessions, sessions() as session:
        admin = await _make_user(session, admin=True)
        # category left blank → filled from the rule's meta.category.
        body = NovaRuleCreate(
            name="Jailbreak guard", rule_text=_GOOD_RULE, action="block"
        )
        resp = await nova_rules.create_nova_rule(body, request=None, user=admin, db=session)
        assert resp.category == "Jailbreak"
        assert resp.action == "block"
        assert resp.is_company_managed is False

        got = await nova_rules.get_nova_rule(resp.id, user=admin, db=session)
        assert got.rule_text == _GOOD_RULE


@pytest.mark.asyncio
async def test_create_rejects_unparseable_rule_text():
    async with _temp_database() as sessions, sessions() as session:
        admin = await _make_user(session, admin=True)
        body = NovaRuleCreate(name="Broken", rule_text="not a rule at all")
        with pytest.raises(HTTPException) as ei:
            await nova_rules.create_nova_rule(body, request=None, user=admin, db=session)
        assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_create_rejects_invalid_action():
    async with _temp_database() as sessions, sessions() as session:
        admin = await _make_user(session, admin=True)
        body = NovaRuleCreate(name="Bad", rule_text=_GOOD_RULE, action="explode")
        with pytest.raises(HTTPException) as ei:
            await nova_rules.create_nova_rule(body, request=None, user=admin, db=session)
        assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_company_managed_requires_admin():
    async with _temp_database() as sessions, sessions() as session:
        normal = await _make_user(session, admin=False)
        body = NovaRuleCreate(
            name="Platform rule", rule_text=_GOOD_RULE, is_company_managed=True
        )
        with pytest.raises(HTTPException) as ei:
            await nova_rules.create_nova_rule(
                body, request=None, user=normal, db=session
            )
        assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_company_managed_rule_visible_to_all_users():
    async with _temp_database() as sessions, sessions() as session:
        admin = await _make_user(session, admin=True)
        normal = await _make_user(session, admin=False)
        body = NovaRuleCreate(
            name="Platform rule", rule_text=_GOOD_RULE, is_company_managed=True
        )
        await nova_rules.create_nova_rule(body, request=None, user=admin, db=session)

        # A company-managed rule shows up for a normal user's listing…
        listed = await nova_rules.list_nova_rules(user=normal, db=session)
        assert [r.name for r in listed] == ["Platform rule"]
        # …but that user cannot edit it (admins only).
        rule_id = listed[0].id
        with pytest.raises(HTTPException) as ei:
            await nova_rules.update_nova_rule(
                rule_id,
                NovaRuleUpdate(enabled=False),
                request=None,
                user=normal,
                db=session,
            )
        assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_personal_rule_hidden_from_other_users():
    async with _temp_database() as sessions, sessions() as session:
        owner = await _make_user(session, admin=False)
        other = await _make_user(session, admin=False)
        body = NovaRuleCreate(name="Mine", rule_text=_GOOD_RULE)
        await nova_rules.create_nova_rule(body, request=None, user=owner, db=session)

        assert [r.name for r in await nova_rules.list_nova_rules(user=owner, db=session)] == [
            "Mine"
        ]
        assert await nova_rules.list_nova_rules(user=other, db=session) == []


@pytest.mark.asyncio
async def test_update_revalidates_rule_text_and_delete():
    async with _temp_database() as sessions, sessions() as session:
        owner = await _make_user(session, admin=False)
        created = await nova_rules.create_nova_rule(
            NovaRuleCreate(name="Mine", rule_text=_GOOD_RULE),
            request=None,
            user=owner,
            db=session,
        )
        # Bad new rule text → 422, original preserved.
        with pytest.raises(HTTPException) as ei:
            await nova_rules.update_nova_rule(
                created.id,
                NovaRuleUpdate(rule_text="garbage"),
                request=None,
                user=owner,
                db=session,
            )
        assert ei.value.status_code == 422

        # A good edit sticks; action can be flipped to log.
        updated = await nova_rules.update_nova_rule(
            created.id,
            NovaRuleUpdate(action="log", enabled=False),
            request=None,
            user=owner,
            db=session,
        )
        assert updated.action == "log"
        assert updated.enabled is False

        await nova_rules.delete_nova_rule(
            created.id, request=None, user=owner, db=session
        )
        assert await nova_rules.list_nova_rules(user=owner, db=session) == []


@pytest.mark.asyncio
async def test_validate_endpoint_reports_tiers_and_errors():
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, admin=False)
        ok = await nova_rules.validate_nova_rule(
            NovaRuleValidateRequest(rule_text=_GOOD_RULE), user=user, db=session
        )
        assert ok.valid is True
        assert ok.name == "Jailbreak"
        assert ok.keyword_count == 1
        assert "keywords" in ok.tiers and "condition" in ok.tiers

        bad = await nova_rules.validate_nova_rule(
            NovaRuleValidateRequest(rule_text="rule X { keywords: $a = \"z\" }"),
            user=user,
            db=session,
        )
        assert bad.valid is False
        assert bad.error


def test_literal_routes_are_registered_before_the_dynamic_one():
    """`/engine-status`, `/models` and `/sources` must not be swallowed by
    `/{rule_id}` — FastAPI matches in declaration order, so a literal path
    declared after the dynamic one would 422 as a malformed UUID."""
    from app.main import application

    paths = [
        r.path for r in application.routes if "/nova-rules" in getattr(r, "path", "")
    ]
    dynamic = min(i for i, p in enumerate(paths) if p.endswith("/{rule_id}"))
    for literal in ("/engine-status", "/models", "/sources"):
        first = min(i for i, p in enumerate(paths) if p.endswith(literal))
        assert first < dynamic, f"{literal} is shadowed by /{{rule_id}}"


@pytest.mark.asyncio
async def test_reserved_sync_tags_cannot_be_forged_or_dropped():
    """The `nova-source:` tags mark which sync source owns a rule and drive
    pruning. A client must not be able to set one (a hand-made rule would then
    be deleted when Nova disconnects) nor strip one (a synced rule would become
    un-prunable)."""
    async with _temp_database() as sessions, sessions() as session:
        admin = await _make_user(session, admin=True)

        # Create: a forged ownership tag is stripped, real tags survive.
        created = await nova_rules.create_nova_rule(
            NovaRuleCreate(
                name="Hand-made",
                rule_text=_GOOD_RULE,
                tags=["nova-source:deadbeef", "mine"],
            ),
            request=None,
            user=admin,
            db=session,
        )
        assert created.tags == ["mine"]

        # Simulate the sync taking ownership of a rule.
        rule = await nova_rules.get_nova_rule(created.id, user=admin, db=session)
        from sqlalchemy import select

        from app.models.nova_rule import NovaRule as _NovaRule

        row = (
            await session.execute(_select_rule(select, _NovaRule, rule.id))
        ).scalar_one()
        row.tags = ["nova-source:abc123", "mine"]
        await session.commit()

        # Update: the client cannot drop the ownership tag, nor add a new one.
        updated = await nova_rules.update_nova_rule(
            created.id,
            NovaRuleUpdate(tags=["renamed", "nova-source:forged"]),
            request=None,
            user=admin,
            db=session,
        )
        assert "nova-source:abc123" in updated.tags  # preserved
        assert "nova-source:forged" not in updated.tags  # refused
        assert "renamed" in updated.tags


def _select_rule(select, model, rule_id):
    import uuid as _uuid

    return select(model).where(model.id == _uuid.UUID(rule_id))
