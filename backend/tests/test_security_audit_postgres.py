"""The security audit answers against the real schema.

The other security audit tests drive one check at a time with stand-ins for
its rows, or replace every check with a stub, so a check reading a column its
model does not have passes all of them: the channel bot check read
``ChannelBot.receive_channels`` (the column is ``allowed_receive_channels``),
and ``GET /api/v1/security-audit`` answered 500 on every install with a channel
bot. Here the real route runs every check, as an admin, on real PostgreSQL
holding channel bots next to an agent, an assignment, a runner and an
integration.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import security_audit
from app.core.config import settings
from app.core.database import get_db
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.channel_bot import ChannelBot
from app.models.integration import Integration
from app.models.runner import Runner
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _client(session_factory: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(security_audit.router, prefix="/api/v1/security-audit")

    async def _get_db():
        async with session_factory() as db:
            yield db

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _seed(db: AsyncSession) -> User:
    admin = User(email="admin@example.com", hashed_password="x", full_name="Admin", role="admin")
    db.add(admin)
    await db.flush()
    agent = Agent(
        name="Embedded agent",
        slug=f"a-{uuid.uuid4().hex[:8]}",
        user_id=admin.id,
        enabled=True,
        web_enabled=True,
    )
    db.add(agent)
    await db.flush()
    db.add_all(
        [
            ChannelBot(provider="slack_bot", name="Slack"),
            ChannelBot(
                provider="teams_bot",
                name="Teams",
                allowed_receive_channels=[{"id": "19:general@thread.tacv2", "name": "General"}],
            ),
            Assignment(
                agent_id=agent.id,
                user_id=admin.id,
                name="On delivery",
                trigger_type="webhook",
                trigger_config={},
                prompt="Triage the delivery.",
            ),
            Runner(
                name="Build machine",
                platform="linux",
                key_prefix=uuid.uuid4().hex[:12],
                key_hash="not-a-real-hash",
                user_id=admin.id,
                allowed_paths=["/srv/builds"],
            ),
            Integration(provider="rss", name="Feed", user_id=admin.id),
        ]
    )
    await db.commit()
    return admin


async def test_the_audit_answers_on_an_install_with_channel_bots() -> None:
    async with scratch_database(
        "security_audit",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as scratch:
        async with scratch.sessions() as db:
            admin = await _seed(db)
        async with _client(scratch.sessions, admin) as client:
            response = await client.get("/api/v1/security-audit")

    assert response.status_code == 200, response.text
    findings = {finding["id"]: finding for finding in response.json()["findings"]}
    assert findings["channels.no_anonymous_access"]["title"] == "No channel bot allows anonymous access (2 bot(s))"
    assert findings["channels.unrestricted_receive"]["title"] == "1 channel bot(s) without receive channel restrictions"
    # The admin owning the private assignment was created just now.
    assert findings["authentication.automation_owners_signed_in"]["severity"] == "pass"


async def test_the_audit_reads_output_schema_references_from_the_stored_text() -> None:
    """Only the schemas whose text holds a reference keyword are loaded, a cleared
    schema (a JSON null) is no schema, and the pass counts the others."""
    async with scratch_database(
        "security_audit_output_schemas",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as scratch:
        async with scratch.sessions() as db:
            admin = await _seed(db)
            remote = {"type": "object", "properties": {"q": {"$ref": "https://example.com/s.json"}}}
            db.add_all(
                [
                    Agent(name=name, slug=f"a-{uuid.uuid4().hex[:8]}", user_id=admin.id, enabled=enabled, output_schema=schema)
                    for name, enabled, schema in (
                        ("Remote", True, remote),
                        ("Dynamic", True, {"properties": {"q": {"$dynamicRef": "file:///etc/passwd"}}}),
                        ("Local", True, {"$defs": {"q": {}}, "properties": {"q": {"$ref": "#/$defs/q"}}}),
                        ("Plain", True, {"type": "object"}),
                        ("Disabled", False, remote),
                        ("Cleared", True, None),
                    )
                ]
            )
            await db.commit()
        async with _client(scratch.sessions, admin) as client:
            warned = (await client.get("/api/v1/security-audit")).json()["findings"]
        async with scratch.sessions() as db:
            for agent in (await db.execute(select(Agent).where(Agent.name.in_(["Remote", "Dynamic"])))).scalars():
                agent.enabled = False
            await db.commit()
        async with _client(scratch.sessions, admin) as client:
            passed = (await client.get("/api/v1/security-audit")).json()["findings"]

    findings = {finding["id"]: finding for finding in warned}
    assert findings["agents.output_schema_remote_references"]["title"] == "2 agent output schema(s) reference another document"
    assert findings["agents.output_schema_remote_references"]["description"].startswith("Dynamic, Remote:")
    assert "agents.output_schema_references_local" not in findings
    findings = {finding["id"]: finding for finding in passed}
    assert "agents.output_schema_remote_references" not in findings
    assert findings["agents.output_schema_references_local"]["severity"] == "pass"
    assert findings["agents.output_schema_references_local"]["description"].startswith("2 enabled agent(s)")


async def test_the_audit_answers_when_an_assignment_holds_a_lone_surrogate(monkeypatch) -> None:
    """A ``json`` column keeps the lone surrogate escape a client sent, and every
    PostgreSQL JSON operator refuses that row: each count reading
    ``trigger_config`` failed the whole audit. A ``\\u0000`` escape too.
    The engine no longer stores either, so the rows are written as
    the stored text a row written before it holds."""
    monkeypatch.setattr(settings, "integration_notifications_require_verification", False)
    async with scratch_database(
        "security_audit_surrogate",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as scratch:
        async with scratch.sessions() as db:
            admin = await _seed(db)
            agent_id = (await db.execute(select(Agent.id))).scalar_one()
            stored_configs = {
                "Lone surrogate": ("webhook", r'{"path": "\ud800"}'),
                "Lone surrogate secret": ("webhook", r'{"webhook_secret": "\ud800"}'),
                "NUL": ("webhook", r'{"path": "\u0000", "webhook_secret": "s3cr3t"}'),
                "Paired": ("webhook", r'{"path": "\ud83d\ude00", "webhook_secret": "s3cr3t"}'),
                "Slack": (
                    "integration_event",
                    r'{"provider": "slack", "event": "new_slack_message", "channel": "\ud800"}',
                ),
                "LinkedIn": ("integration_event", r'{"provider": "linkedin", "note": "\udc00"}'),
            }
            db.add_all(
                [
                    Assignment(
                        agent_id=agent_id,
                        user_id=admin.id,
                        name=name,
                        trigger_type=trigger_type,
                        trigger_config={},
                        prompt="Triage the delivery.",
                    )
                    for name, (trigger_type, _stored) in stored_configs.items()
                ]
            )
            await db.flush()
            for name, (_trigger_type, stored) in stored_configs.items():
                await db.execute(
                    text("UPDATE assignments SET trigger_config = CAST(:stored AS json) WHERE name = :name"),
                    {"stored": stored, "name": name},
                )
            await db.commit()
        async with _client(scratch.sessions, admin) as client:
            response = await client.get("/api/v1/security-audit")

    assert response.status_code == 200, response.text
    findings = {finding["id"]: finding for finding in response.json()["findings"]}
    assert findings["assignments.webhook_triggers"]["title"] == "5 active webhook-triggered assignment(s)"
    # The seeded one ({}) and "Lone surrogate": the others hold a secret.
    secretless = [
        finding["title"]
        for finding_id, finding in findings.items()
        if finding_id in ("assignments.webhook_without_secret", "assignments.webhook_without_secret_refused")
    ]
    assert len(secretless) == 1 and secretless[0].startswith("2 webhook-triggered assignment(s) without a")
    # The integration-event rows are counted too, read from their stored text.
    assert findings["integrations.slack_events_unverified"]["description"].startswith("1 active assignment(s)")
    assert findings["integrations.linkedin_notifications_unverified"]["description"].startswith("1 active assignment(s)")


async def test_the_audit_reads_lent_credentials_and_stored_token_urls_from_the_schema(monkeypatch) -> None:
    """The lent personal credential goes through the exposure service's reference
    lookups, the token URLs through their columns, on real PostgreSQL."""
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "oauth_token_url_strict", False)
    async with scratch_database(
        "security_audit_lent_credentials",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as scratch:
        async with scratch.sessions() as db:
            admin = await _seed(db)
            github = Integration(
                provider="github", name="Admin GitHub", auth_type="api_key", api_key_enc="enc", user_id=admin.id
            )
            local = Integration(
                provider="github",
                name="Local IdP",
                auth_type="oauth",
                token_url="http://localhost:9000/token",
                user_id=admin.id,
            )
            db.add_all([github, local])
            await db.flush()
            agent = (await db.execute(select(Agent))).scalar_one()
            agent.integration_bindings = {"github": [str(github.id)]}
            await db.commit()
        async with _client(scratch.sessions, admin) as client:
            response = await client.get("/api/v1/security-audit")

    assert response.status_code == 200, response.text
    findings = {finding["id"]: finding for finding in response.json()["findings"]}
    lent = findings["access.personal_credentials_lent"]
    assert lent["title"] == "1 personal credential(s) are used by other people"
    # The embedded agent is private: its public widget link and its webhook
    # assignments' URL are who act with the credential.
    assert (
        f"Admin GitHub (GitHub, {github.id}) of Admin: agents and what they attach - anyone with a public widget "
        "link and holders of a webhook URL" in lent["description"]
    )
    token_urls = findings["integrations.oauth_token_url_strict_off"]
    assert token_urls["severity"] == "warning"
    assert f"integration Local IdP (GitHub, {local.id}): a loopback address" in token_urls["description"]
