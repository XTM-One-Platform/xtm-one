"""Shared fixtures of the Platform Concierge PostgreSQL tests (#8254): a scratch
database, users with every kind of access to an agent and a flow, and the
settings an administrator saves."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.flow import Flow
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.concierge.settings import ConciergeSettings, save_concierge_settings
from tests.scratch_database import ALL_TABLES, scratch_database


@asynccontextmanager
async def concierge_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "concierge", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield db.sessions


def make_user(name: str, *, admin: bool = False) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role="admin" if admin else "user",
    )


def make_agent(owner: User | None, name: str, **columns) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        user_id=owner.id if owner else None,
        name=name,
        slug=f"{name}-{uuid.uuid4().hex[:6]}",
        persona="p",
        enabled=True,
        **columns,
    )


class World:
    """Users, a group, agents and a flow, with every kind of access to them."""

    async def build(self, sessions: async_sessionmaker) -> World:
        async with sessions() as db:
            self.owner, self.admin = make_user("owner"), make_user("admin", admin=True)
            self.reader, self.configurer, self.member = make_user("reader"), make_user("configurer"), make_user("member")
            self.revoked, self.stranger = make_user("revoked"), make_user("stranger")
            db.add_all([self.owner, self.admin, self.reader, self.configurer, self.member, self.revoked, self.stranger])
            await db.flush()
            group = Group(id=uuid.uuid4(), name="SOC")
            db.add(group)
            await db.flush()
            db.add(GroupMember(group_id=group.id, user_id=self.member.id))
            shares = {
                "shared_user_ids": [str(self.reader.id), str(self.configurer.id)],
                "shared_user_permissions": {str(self.reader.id): "read_only", str(self.configurer.id): "config"},
                "shared_group_ids": [str(group.id)],
                "shared_group_permissions": {str(group.id): "config"},
            }
            self.private = make_agent(self.owner, "private", **shares)
            self.company = make_agent(self.admin, "company", is_company_managed=True)
            self.fleet = make_agent(self.admin, "experimenter", is_company_managed=True, concierge_role="experimenter")
            self.flow = Flow(id=uuid.uuid4(), user_id=self.owner.id, name="triage", graph={}, **shares)
            db.add_all([self.private, self.company, self.fleet, self.flow])
            await db.commit()
        return self



async def configure(sessions, admin: User, **values) -> None:
    async with sessions() as db:
        await save_concierge_settings(db, ConciergeSettings(**values), actor=admin)
        await db.commit()


