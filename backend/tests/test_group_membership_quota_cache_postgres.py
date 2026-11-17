"""Membership changes drop the right cached quota resolutions, on PostgreSQL.

``test_group_membership_quota_cache.py`` drives SAML group sync and the UUID-group
merge against a session stub. These run them, and a group deletion through its
route, on a scratch database: the rows really change, the unique constraint and
the cascades are PostgreSQL's own, the memberships are read back on a second
connection, and the users handed to ``quota.invalidate_membership_quota_cache``
are exactly the ones whose memberships changed.

Skips rather than fails when PostgreSQL is unreachable, matching the other
``integration``-marked tests here.
"""

from __future__ import annotations

import sys
import types
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
import app.services.quota as quota_module
from app.api import auth_sso
from app.api.auth_sso import _rename_uuid_groups, _sync_user_groups, saml_acs
from app.api.deps import AfterCommit
from app.api.v1 import groups as groups_module
from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.group import Group, GroupMember
from app.models.user import User
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


def _user(label: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{label}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=label.title(),
        role="user",
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


async def test_saml_sync_reports_exactly_the_membership_changes():
    async with _scratch("samlsync") as scratch:
        dana = _user("dana")
        async with scratch.sessions() as db:
            db.add(dana)
            analysts = Group(name="Analysts")
            db.add_all([analysts, Group(name="Responders")])
            await db.flush()
            # Given by an administrator, not by the identity provider.
            db.add(GroupMember(group_id=analysts.id, user_id=dana.id, source="manual"))
            await db.commit()

        async def _sync(names: list[str]) -> bool:
            async with scratch.sessions() as db:
                user = await db.get(User, dana.id)
                changed = await _sync_user_groups(db, user, names, auto_create=True)
                await db.commit()
                return changed

        # Asserted and already held: re-tagged, which changes no membership.
        assert await _sync(["Analysts"]) is False
        assert await _memberships(scratch.sessions, dana.id) == {"Analysts": "saml"}
        # Unchanged.
        assert await _sync(["Analysts"]) is False
        # Left one, joined an existing group and one the sync creates.
        assert await _sync(["Responders", "New Team"]) is True
        assert await _memberships(scratch.sessions, dana.id) == {
            "Responders": "saml",
            "New Team": "saml",
        }
        # No group asserted any more: every SAML membership goes.
        assert await _sync([]) is True
        assert await _memberships(scratch.sessions, dana.id) == {}
        assert await _sync([]) is False


async def test_a_uuid_group_merge_reports_the_members_it_moved():
    async with _scratch("uuidmerge") as scratch:
        entra_id = str(uuid.uuid4())
        newcomer, already, outsider = _user("newcomer"), _user("already"), _user("outsider")
        async with scratch.sessions() as db:
            db.add_all([newcomer, already, outsider])
            by_id, target, other = (
                Group(name=entra_id),
                Group(name="Security Team"),
                Group(name="Other"),
            )
            db.add_all([by_id, target, other])
            await db.flush()
            db.add_all(
                [
                    GroupMember(group_id=by_id.id, user_id=newcomer.id, source="saml"),
                    GroupMember(group_id=by_id.id, user_id=already.id, source="saml"),
                    GroupMember(group_id=target.id, user_id=already.id, source="manual"),
                    GroupMember(group_id=other.id, user_id=outsider.id, source="manual"),
                ]
            )
            await db.commit()

        moved: set[uuid.UUID] = set()
        async with scratch.sessions() as db:
            renamed = await _rename_uuid_groups(
                db, {entra_id: "Security Team"}, moved_members=moved
            )
            await db.commit()

        assert renamed == 1
        assert moved == {newcomer.id, already.id}
        assert await _memberships(scratch.sessions, newcomer.id) == {"Security Team": "saml"}
        assert await _memberships(scratch.sessions, already.id) == {"Security Team": "manual"}
        assert await _memberships(scratch.sessions, outsider.id) == {"Other": "manual"}
        async with scratch.sessions() as db:
            assert await db.scalar(select(Group.id).where(Group.name == entra_id)) is None


@pytest.mark.parametrize("had_quota", [False, True])
async def test_deleting_a_group_drops_the_resolutions_it_took_part_in(
    monkeypatch, had_quota
):
    drops: list[object] = []

    async def _drop_members(user_ids):
        drops.append(set(user_ids))

    async def _drop_all(user_id=None):
        drops.append(("every resolution", user_id))

    monkeypatch.setattr(quota_module, "invalidate_membership_quota_cache", _drop_members)
    monkeypatch.setattr(quota_module, "invalidate_quota_cache", _drop_all)

    async with _scratch("groupdel") as scratch:
        admin, first, second, outsider = (
            _user("admin"),
            _user("first"),
            _user("second"),
            _user("outsider"),
        )
        admin.role = "admin"
        async with scratch.sessions() as db:
            db.add_all([admin, first, second, outsider])
            group = Group(
                name="Analysts",
                quota_config={"agentic": {"default": 5, "period": "daily"}} if had_quota else None,
            )
            kept = Group(name="Responders")
            db.add_all([group, kept])
            await db.flush()
            db.add_all(
                [
                    GroupMember(group_id=group.id, user_id=first.id),
                    GroupMember(group_id=group.id, user_id=second.id),
                    GroupMember(group_id=kept.id, user_id=outsider.id),
                ]
            )
            await db.commit()

        after_commit = AfterCommit()
        async with scratch.sessions() as db:
            await groups_module.delete_group(
                group_id=str(group.id),
                request=SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={}),
                admin=await db.get(User, admin.id),
                db=db,
                after_commit=after_commit,
            )
        # What run_after_commit does once the route has returned.
        await after_commit.run()

        # With a quota, every cached resolution goes (a member added while the
        # group was being deleted included); without one, its members'.
        assert drops == (
            [("every resolution", None)] if had_quota else [{first.id, second.id}]
        )
        for member in (first, second):
            assert await _memberships(scratch.sessions, member.id) == {}
        assert await _memberships(scratch.sessions, outsider.id) == {"Responders": "manual"}


def _saml_identity_provider(monkeypatch, *, subject: str, email: str) -> None:
    """A validated assertion for *subject*, with group mapping on."""
    import app.core.sso_helpers as sso_helpers

    class _Auth:
        def __init__(self, req, saml_settings):
            pass

        def process_response(self):
            pass

        def get_errors(self):
            return []

        def get_last_error_reason(self):
            return None

        def is_authenticated(self):
            return True

        def get_attributes(self):
            return {"email": [email], "groups": ["Analysts"]}

        def get_nameid(self):
            return subject

    fake_module = types.ModuleType("onelogin.saml2.auth")
    fake_module.OneLogin_Saml2_Auth = _Auth
    monkeypatch.setitem(sys.modules, "onelogin", types.ModuleType("onelogin"))
    monkeypatch.setitem(sys.modules, "onelogin.saml2", types.ModuleType("onelogin.saml2"))
    monkeypatch.setitem(sys.modules, "onelogin.saml2.auth", fake_module)
    cfg = SimpleNamespace(
        saml_enabled=True,
        saml_auto_admin_new_users=False,
        saml_auto_create_users=True,
        saml_groups_mapping_enabled=True,
        saml_groups_auto_create=True,
        allowed_domain_list=[],
    )

    async def _cfg(_db):
        return cfg

    async def _saml_settings(_cfg):
        return {}

    monkeypatch.setattr(sso_helpers, "get_sso_config", _cfg)
    monkeypatch.setattr(auth_sso, "_build_saml_settings_from_cfg", _saml_settings)
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")
    monkeypatch.setattr(settings, "xtm_hub_admin_group", "")
    monkeypatch.setattr(settings, "xtm_hub_platform_id", "")


async def test_a_sign_in_losing_a_membership_race_still_signs_in(monkeypatch):
    """Two sign-ins of one account both add its new SAML membership: the second
    to flush hits the unique constraint, rolls back and signs in with its
    audit row alone - the account it goes on with is reloaded, not the one the
    rollback expired."""
    subject = f"idp|{uuid.uuid4().hex[:8]}"
    drops: list[set] = []

    async def _drop(user_ids):
        drops.append(set(user_ids))

    monkeypatch.setattr(quota_module, "invalidate_membership_quota_cache", _drop)

    async with _scratch("samlrace") as scratch:
        dana = _user("dana")
        dana.sso_provider, dana.sso_subject = "saml", subject
        async with scratch.sessions() as db:
            analysts = Group(name="Analysts")
            db.add_all([dana, analysts])
            await db.commit()
        _saml_identity_provider(monkeypatch, subject=subject, email=dana.email)

        async def _sync_after_the_other_sign_in(db, user, group_names, auto_create):
            # This sign-in read the memberships before the other one committed
            # the same new membership: its insert collides at the next flush.
            async with scratch.sessions() as other:
                other.add(GroupMember(group_id=analysts.id, user_id=user.id, source="saml"))
                await other.commit()
            db.add(GroupMember(group_id=analysts.id, user_id=user.id, source="saml"))
            return True

        monkeypatch.setattr(auth_sso, "_sync_user_groups", _sync_after_the_other_sign_in)

        class _Request:
            query_params: dict = {}
            headers: dict = {}
            client = SimpleNamespace(host="203.0.113.7")

            async def form(self):
                return {"SAMLResponse": "opaque"}

        async with scratch.sessions() as db:
            response = await saml_acs(_Request(), db=db)

        assert response.status_code == 303
        assert "#token=" in response.headers["location"]
        assert await _memberships(scratch.sessions, dana.id) == {"Analysts": "saml"}
        async with scratch.sessions() as db:
            logins = await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.user_id == dana.id, AuditLog.action == "login")
            )
        assert logins == 1
        # Harmless when the membership was the other sign-in's: one DEL.
        assert drops == [{dana.id}]
