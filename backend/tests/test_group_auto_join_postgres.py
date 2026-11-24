"""Single sign-on into the groups marked "auto join" (#1901), on PostgreSQL.

``services/group_auto_join.join_auto_join_groups`` runs at every OIDC and SAML
sign-in. These drive it, the SAML mapping beside it and the group update route
on a scratch database: the unique constraint, the conflict skip and the audit
rows are PostgreSQL's own, read back on a second connection.

Skips rather than fails when PostgreSQL is unreachable, matching the other
``integration``-marked tests here.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.auth_sso import _sync_user_groups
from app.api.deps import AfterCommit
from app.api.v1 import groups as groups_module
from app.models.audit_log import AuditLog
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.group_auto_join import join_auto_join_groups
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _scratch(prefix: str):
    return scratch_database(
        prefix,
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    )


def _user(label: str, role: str = "user") -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{label}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=label.title(),
        role=role,
    )


async def _memberships(sessions, user_id: uuid.UUID) -> dict[str, str]:
    """Group name -> membership source, read on a connection of its own."""
    async with sessions() as db:
        rows = await db.execute(
            select(Group.name, GroupMember.source)
            .join(GroupMember, GroupMember.group_id == Group.id)
            .where(GroupMember.user_id == user_id)
        )
        return dict(rows.all())


async def _audit(sessions, **where) -> list[AuditLog]:
    async with sessions() as db:
        q = select(AuditLog)
        for column, value in where.items():
            q = q.where(getattr(AuditLog, column) == value)
        return list((await db.execute(q)).scalars().all())


async def test_a_sign_in_joins_every_auto_join_group_once_and_audits_each():
    async with _scratch("autojoin") as scratch:
        dana = _user("dana")
        async with scratch.sessions() as db:
            db.add(dana)
            db.add_all(
                [
                    Group(name="Everyone", auto_join=True),
                    Group(name="Analysts", auto_join=True),
                    Group(name="Responders"),
                ]
            )
            await db.commit()

        async with scratch.sessions() as db:
            user = await db.get(User, dana.id)
            joined = await join_auto_join_groups(db, user, provider="OIDC")
            await db.commit()
        assert len(joined) == 2
        assert await _memberships(scratch.sessions, dana.id) == {"Everyone": "auto_join", "Analysts": "auto_join"}
        rows = await _audit(scratch.sessions, user_id=dana.id, action="bind")
        assert sorted(r.entity_name for r in rows) == ["Analysts", "Everyone"]
        assert {r.user_email for r in rows} == {"system:sso-sign-in"}
        assert all("OIDC sign-in" in r.summary for r in rows)

        # The next sign-in: nothing added, nothing audited.
        async with scratch.sessions() as db:
            user = await db.get(User, dana.id)
            assert await join_auto_join_groups(db, user, provider="SAML") == set()
            await db.commit()
        assert len(await _audit(scratch.sessions, user_id=dana.id, action="bind")) == 2


async def test_a_member_already_in_the_group_keeps_their_membership_as_it_is():
    async with _scratch("autojoinkeep") as scratch:
        dana = _user("dana")
        async with scratch.sessions() as db:
            everyone = Group(name="Everyone", auto_join=True)
            db.add_all([dana, everyone])
            await db.flush()
            db.add(GroupMember(group_id=everyone.id, user_id=dana.id, source="manual"))
            await db.commit()

        async with scratch.sessions() as db:
            user = await db.get(User, dana.id)
            assert await join_auto_join_groups(db, user, provider="OIDC") == set()
            await db.commit()
        assert await _memberships(scratch.sessions, dana.id) == {"Everyone": "manual"}
        assert await _audit(scratch.sessions, user_id=dana.id, action="bind") == []


async def test_no_auto_join_group_writes_nothing():
    async with _scratch("autojoinnone") as scratch:
        dana = _user("dana")
        async with scratch.sessions() as db:
            db.add_all([dana, Group(name="Responders")])
            await db.commit()
        async with scratch.sessions() as db:
            user = await db.get(User, dana.id)
            assert await join_auto_join_groups(db, user, provider="SAML") == set()
            await db.commit()
        assert await _memberships(scratch.sessions, dana.id) == {}


async def test_the_saml_mapping_never_removes_or_takes_over_an_auto_join_membership():
    async with _scratch("autojoinsaml") as scratch:
        dana = _user("dana")
        async with scratch.sessions() as db:
            db.add_all([dana, Group(name="Everyone", auto_join=True), Group(name="Analysts")])
            await db.commit()
        async with scratch.sessions() as db:
            user = await db.get(User, dana.id)
            await join_auto_join_groups(db, user, provider="SAML")
            await db.commit()

        # Assertions naming no group, other groups, then the auto-join group too.
        for names in ([], ["Analysts"], ["Everyone", "Analysts"], ["Analysts"]):
            async with scratch.sessions() as db:
                user = await db.get(User, dana.id)
                await _sync_user_groups(db, user, names, auto_create=False)
                await db.commit()
            assert (await _memberships(scratch.sessions, dana.id))["Everyone"] == "auto_join"
        assert await _memberships(scratch.sessions, dana.id) == {"Everyone": "auto_join", "Analysts": "saml"}


async def test_auto_join_turned_on_after_the_mapping_keeps_the_saml_membership() -> None:
    """The mapping wrote a "saml" row, then an admin turned auto join on: an
    assertion that no longer names the group never removes it."""
    async with _scratch("autojoinlate") as scratch:
        dana = _user("dana")
        async with scratch.sessions() as db:
            everyone = Group(name="Everyone")
            db.add_all([dana, everyone, Group(name="Analysts")])
            await db.flush()
            db.add(GroupMember(group_id=everyone.id, user_id=dana.id, source="saml"))
            await db.commit()
        async with scratch.sessions() as db:
            group = (await db.execute(select(Group).where(Group.name == "Everyone"))).scalar_one()
            group.auto_join = True
            await db.commit()

        for names in (["Analysts"], []):
            async with scratch.sessions() as db:
                user = await db.get(User, dana.id)
                assert await join_auto_join_groups(db, user, provider="SAML") == set()
                await _sync_user_groups(db, user, names, auto_create=False)
                await db.commit()
            assert (await _memberships(scratch.sessions, dana.id))["Everyone"] == "saml"


async def test_the_toggle_is_written_and_audited_with_its_previous_value():
    async with _scratch("autojointoggle") as scratch:
        admin = _user("admin", role="admin")
        async with scratch.sessions() as db:
            group = Group(name="Everyone")
            db.add_all([admin, group])
            await db.commit()

        request = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
        async with scratch.sessions() as db:
            answer = await groups_module.update_group(
                group_id=str(group.id),
                body=groups_module.UpdateGroupRequest(auto_join=True),
                request=request,
                admin=await db.get(User, admin.id),
                db=db,
                after_commit=AfterCommit(),
            )
        assert answer.auto_join is True
        async with scratch.sessions() as db:
            assert (await db.get(Group, group.id)).auto_join is True
            detail = await groups_module.get_group(group_id=str(group.id), admin=await db.get(User, admin.id), db=db)
        assert detail.auto_join is True
        rows = await _audit(scratch.sessions, entity_id=group.id, action="update")
        assert len(rows) == 1
        assert rows[0].summary == "Switched auto join on for group 'Everyone'"
        assert rows[0].details["auto_join"] is True
        assert rows[0].details["previous_auto_join"] is False
