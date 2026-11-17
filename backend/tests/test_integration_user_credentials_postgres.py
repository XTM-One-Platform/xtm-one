"""Each person's own account on a shared integration, on PostgreSQL.

The session doubles of ``test_integration_user_credentials`` cannot show the
parts PostgreSQL decides: the credential loads with its integration (the
holder reads its configuration from it), its row lock, the refresh sweep's
join, the per-user template candidate's ``EXISTS``, and the cascades. Same
scratch-database recipe as ``test_token_refresh_row_lock_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.integration import Integration
from app.models.integration_user_credential import IntegrationUserCredential
from app.models.user import User
from app.services import integration_user_credentials as user_credentials
from app.services import token_refresh
from app.services.integration_tools import template_candidate_clause
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "usercred", tables=ALL_TABLES, extensions=("vector",), bind_app_sessions=False
    ) as db:
        yield db.sessions


def _user(label: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{label}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=label,
        hashed_password="x",
    )


async def _seed(sessions) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A company-managed per_user integration, its owner, a connected member, a stranger."""
    owner, member, stranger = _user("owner"), _user("member"), _user("stranger")
    async with sessions() as db:
        db.add_all([owner, member, stranger])
        await db.flush()
        integration = Integration(
            user_id=owner.id,
            provider="notion",
            auth_type="oauth",
            name="Company Notion",
            status="connected",
            is_company_managed=True,
            credential_mode="per_user",
            client_id_enc="client-id",
            token_url="https://api.notion.com/v1/oauth/token",
            access_token_enc="owner-access",
            refresh_token_enc="owner-refresh",
        )
        db.add(integration)
        await db.flush()
        credential = await user_credentials.upsert_user_credential(db, integration, member.id)
        user_credentials.store_tokens(credential, access_token="member-access", refresh_token="member-refresh")
        credential.token_expires_at = datetime.now(UTC) + timedelta(minutes=1)
        await db.commit()
        return integration.id, owner.id, member.id, stranger.id


async def test_a_credential_loads_with_its_integration_and_locks_its_own_row():
    async with _temp_database() as sessions:
        integration_id, _owner, member, _stranger = await _seed(sessions)
        async with sessions() as db:
            credential = await user_credentials.get_user_credential(db, integration_id, member)
            ref = user_credentials.holder_ref(credential)
        async with sessions() as db:
            holder = await user_credentials.load_token_holder(db, ref)
            assert isinstance(holder, IntegrationUserCredential)
            assert holder.token_url == "https://api.notion.com/v1/oauth/token"
            assert holder.client_id_enc == "client-id"
            stored = (holder.access_token_enc, holder.refresh_token_enc)
            # The row lock and the re-read are the credential's own row.
            assert await token_refresh.lock_stored_tokens(db, holder) == stored
            assert stored != ("owner-access", "owner-refresh")
            await db.commit()


async def test_the_resolver_and_the_template_candidates_on_postgres():
    async with _temp_database() as sessions:
        integration_id, owner, member, stranger = await _seed(sessions)
        async with sessions() as db:
            integration = await db.get(Integration, integration_id)
            assert await user_credentials.resolve_token_holder(db, integration, owner) is integration
            member_holder = await user_credentials.resolve_token_holder(db, integration, member)
            assert isinstance(member_holder, IntegrationUserCredential)
            assert member_holder.access_token_enc
            assert await user_credentials.resolve_token_holder(db, integration, stranger) is None

            for acting, eligible in ((member, True), (stranger, False)):
                found = await db.scalar(
                    select(Integration.id).where(
                        Integration.id == integration_id, template_candidate_clause(acting, [])
                    )
                )
                assert (found is not None) is eligible

            counts = await user_credentials.connected_user_counts(db, [integration_id])
            assert counts == {str(integration_id): 1}


async def test_the_sweep_selects_the_people_s_expiring_credentials():
    async with _temp_database() as sessions:
        integration_id, *_ = await _seed(sessions)
        selected: list[object] = []

        async def _record(holder, _http, _sem):
            selected.append(holder)
            return False

        async with sessions() as db:
            original = token_refresh._refresh_one_integration
            token_refresh._refresh_one_integration = _record
            try:
                await token_refresh.refresh_expiring_tokens(db)
            finally:
                token_refresh._refresh_one_integration = original

        credentials = [h for h in selected if isinstance(h, IntegrationUserCredential)]
        assert [str(c.integration_id) for c in credentials] == [str(integration_id)]
        assert credentials[0].token_url == "https://api.notion.com/v1/oauth/token"


async def test_credentials_go_with_their_integration_and_their_user():
    async with _temp_database() as sessions:
        integration_id, _owner, member, _stranger = await _seed(sessions)
        async with sessions() as db:
            await db.execute(delete(User).where(User.id == member))
            await db.commit()
            assert await db.scalar(select(func.count()).select_from(IntegrationUserCredential)) == 0

        integration_id, _owner, member, _stranger = await _seed(sessions)
        async with sessions() as db:
            await db.execute(delete(Integration).where(Integration.id == integration_id))
            await db.commit()
            assert await db.scalar(select(func.count()).select_from(IntegrationUserCredential)) == 0


async def test_the_people_s_own_status_and_who_still_sees_it_on_postgres():
    """The integration's holder never connected (status ``configured``, the rows
    ``u5545ucred02`` moved): a member who connected their own account is served,
    and a sharing change prunes only the people who lost sight of it, read in
    one query."""
    from app.models.group import Group, GroupMember
    from app.services.integration_credentials import users_seeing_integration

    async with _temp_database() as sessions:
        owner, by_group, by_grant, stranger = (_user(label) for label in ("owner", "by-group", "by-grant", "stranger"))
        async with sessions() as db:
            db.add_all([owner, by_group, by_grant, stranger])
            group = Group(id=uuid.uuid4(), name=f"team-{uuid.uuid4().hex[:8]}")
            db.add(group)
            await db.flush()
            db.add(GroupMember(group_id=group.id, user_id=by_group.id))
            integration = Integration(
                user_id=owner.id,
                provider="notion",
                auth_type="oauth",
                name="Personal Notion",
                status="configured",
                credential_mode="per_user",
                shared_group_ids=[str(group.id)],
                shared_user_ids=[str(by_grant.id)],
            )
            db.add(integration)
            await db.flush()
            for person in (by_group, by_grant, stranger):
                credential = await user_credentials.upsert_user_credential(db, integration, person.id)
                user_credentials.store_tokens(credential, access_token=f"{person.full_name}-access", refresh_token=None)
            await db.commit()
            integration_id = integration.id

        async with sessions() as db:
            for acting, served in ((by_group.id, True), (owner.id, False)):
                found = await db.scalar(
                    select(Integration.id).where(
                        Integration.id == integration_id, user_credentials.connected_for_clause(acting)
                    )
                )
                assert (found is not None) is served

            people = [owner.id, by_group.id, by_grant.id, stranger.id]
            assert await users_seeing_integration(db, integration_id, people) == {owner.id, by_group.id, by_grant.id}

            integration = await db.get(Integration, integration_id)
            assert await user_credentials.prune_unreachable_user_credentials(db, integration) == 1
            await db.commit()
            left = set(
                (
                    await db.execute(
                        select(IntegrationUserCredential.user_id).where(
                            IntegrationUserCredential.integration_id == integration_id
                        )
                    )
                ).scalars()
            )
            assert left == {by_group.id, by_grant.id}
