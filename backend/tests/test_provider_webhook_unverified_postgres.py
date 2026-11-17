"""The provider webhook audit counts what the route can run, on real PostgreSQL.

The unit tests read the check's SQL; here it runs against the real schema. The count per
provider groups by its label, tests the binding the way ``_load_provider_routing`` does
(``integration_bindings::jsonb ? provider``), and counts exactly the assignments the
router loads for a provider, less those the match path drops (a deactivated owner). A row
PostgreSQL cannot parse (a lone surrogate escape) is counted from its stored text
or skipped, never a failed audit.

``integration``-marked and skips when PostgreSQL is unreachable, like its neighbours; the
temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import security_audit, webhooks
from app.core.config import settings
from app.core.security import encrypt_value
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.oauth_default import OAuthDefault
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

GITHUB_ISSUES = {"provider": "github", "event": "issues"}


def _user(name: str, *, active: bool = True) -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name=name,
        role="user",
        is_active=active,
    )


def _agent(owner: User, bindings: object, *, enabled: bool = True) -> Agent:
    return Agent(
        name=f"Agent {uuid.uuid4().hex[:8]}",
        slug=f"a-{uuid.uuid4().hex[:8]}",
        user_id=owner.id,
        enabled=enabled,
        integration_bindings=bindings,
    )


def _assignment(agent: Agent, trigger_type: str, config: dict, *, enabled: bool = True) -> Assignment:
    return Assignment(
        agent_id=agent.id,
        user_id=agent.user_id,
        name=f"On {trigger_type}",
        trigger_type=trigger_type,
        trigger_config=config,
        prompt="Handle the event.",
        enabled=enabled,
    )


def _bound_to(*providers: str) -> dict:
    return {provider: [str(uuid.uuid4())] for provider in providers}


async def _store(db, table: str, column: str, row_id, stored: str) -> None:
    """Write *stored* as the column's JSON text, around the engine's sanitizing serializer."""
    await db.execute(
        text(f"UPDATE {table} SET {column} = CAST(:stored AS json) WHERE id = :id"),  # noqa: S608 - the test's own names
        {"stored": stored, "id": row_id},
    )


async def _audit(sessions) -> dict:
    async with sessions() as db:
        findings: list = []
        await security_audit._check_integration_notifications(db, findings)
    return {finding.id: finding for finding in findings}


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch):
    monkeypatch.setattr(settings, "integration_notifications_require_verification", False)
    webhooks._invalidate_provider_routing_cache()
    yield
    webhooks._invalidate_provider_routing_cache()


async def test_the_audit_counts_what_the_router_loads_less_what_the_match_path_drops() -> None:
    async with scratch_database(
        "provider_webhook_unverified",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as scratch:
        async with scratch.sessions() as db:
            owner, gone = _user("owner"), _user("gone", active=False)
            db.add_all([owner, gone])
            await db.flush()
            bound = _agent(owner, _bound_to("github", "gmail"))
            disabled = _agent(owner, _bound_to("github"), enabled=False)
            orphaned = _agent(gone, _bound_to("github"))
            unbound = _agent(owner, _bound_to("jira"))
            db.add_all([bound, disabled, orphaned, unbound])
            await db.flush()
            reached = [
                _assignment(bound, "integration_event", GITHUB_ISSUES),
                _assignment(bound, "webhook", {"path": "github"}),
            ]
            owner_deactivated = _assignment(orphaned, "integration_event", GITHUB_ISSUES)
            db.add_all(
                [
                    *reached,
                    owner_deactivated,
                    _assignment(bound, "integration_event", {"provider": "gmail", "event": "new_email"}),
                    _assignment(bound, "integration_event", GITHUB_ISSUES, enabled=False),
                    _assignment(disabled, "integration_event", GITHUB_ISSUES),
                    _assignment(unbound, "integration_event", GITHUB_ISSUES),
                    _assignment(bound, "webhook", {"path": "deploy"}),
                    _assignment(bound, "schedule", {"cron": "0 * * * *"}),
                    OAuthDefault(
                        provider="gmail",
                        client_id_enc=encrypt_value("gmail-app"),
                        client_secret_enc=encrypt_value("gmail-client-secret"),
                    ),
                ]
            )
            await db.commit()
            reached_ids = {assignment.id for assignment in reached}
            dropped_id = owner_deactivated.id

        routing = await webhooks._load_provider_routing("github")
        findings = await _audit(scratch.sessions)

    loaded = {uuid.UUID(matcher.assignment_id) for matcher in routing.matchers}
    assert loaded == reached_ids | {dropped_id}
    finding = findings["integrations.provider_webhook_unverified"]
    assert finding.description.startswith("Up to 3 active assignment(s)")
    assert f"github: {len(reached_ids)} assignment(s), no OAuth default" in finding.description
    assert "gmail: 1 assignment(s), no webhook secret on its OAuth default" in finding.description
    assert "deploy" not in finding.description and "jira" not in finding.description


async def test_a_signed_provider_is_a_pass() -> None:
    async with scratch_database(
        "provider_webhook_verified",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as scratch:
        async with scratch.sessions() as db:
            owner = _user("owner")
            db.add(owner)
            await db.flush()
            agent = _agent(owner, _bound_to("github"))
            db.add(agent)
            await db.flush()
            db.add_all(
                [
                    _assignment(agent, "integration_event", GITHUB_ISSUES),
                    OAuthDefault(
                        provider="github",
                        client_id_enc=encrypt_value("github-app"),
                        client_secret_enc=encrypt_value("github-client-secret"),
                        extra_config_enc=encrypt_value('{"webhook_secret": "s3cret"}'),
                    ),
                ]
            )
            await db.commit()
        findings = await _audit(scratch.sessions)

    assert "integrations.provider_webhook_unverified" not in findings
    assert findings["integrations.provider_webhook_verified"].severity == "pass"


async def test_rows_postgresql_cannot_parse_are_counted_from_their_text_or_skipped() -> None:
    async with scratch_database(
        "provider_webhook_surrogate",
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as scratch:
        async with scratch.sessions() as db:
            owner = _user("owner")
            db.add(owner)
            await db.flush()
            agent = _agent(owner, _bound_to("github"))
            disabled = _agent(owner, _bound_to("github"), enabled=False)
            # The router skips these bindings (its ``::jsonb`` cannot read them); so does the audit.
            unreadable_bindings = _agent(owner, {})
            # A valid surrogate pair is read by both, and counted.
            paired_bindings = _agent(owner, {"github": [str(uuid.uuid4())], "note": "\U0001f600"})
            db.add_all([agent, disabled, unreadable_bindings, paired_bindings])
            await db.flush()
            # The engine no longer stores these; the rows hold them as a row written before it does.
            unparsed = {
                # The router loads these (it reads trigger_config in Python): counted.
                _assignment(agent, "integration_event", GITHUB_ISSUES): r'{"provider": "github", "event": "issues", "note": "\ud800"}',
                _assignment(agent, "webhook", {}): r'{"path": "github", "note": "\udc00"}',
                _assignment(paired_bindings, "integration_event", GITHUB_ISSUES): r'{"provider": "github", "event": "issues", "note": "\u0000"}',
                # A path no binding names, a disabled agent, bindings the router cannot read.
                _assignment(agent, "webhook", {}): r'{"path": "\udc00"}',
                _assignment(disabled, "integration_event", GITHUB_ISSUES): r'{"provider": "github", "event": "issues", "note": "\ud800"}',
                _assignment(unreadable_bindings, "integration_event", GITHUB_ISSUES): r'{"provider": "github", "event": "issues", "note": "\ud800"}',
            }
            db.add_all(
                [
                    _assignment(agent, "integration_event", GITHUB_ISSUES),
                    _assignment(paired_bindings, "integration_event", GITHUB_ISSUES),
                    _assignment(unreadable_bindings, "integration_event", GITHUB_ISSUES),
                    *unparsed,
                ]
            )
            await db.flush()
            for assignment, stored in unparsed.items():
                await _store(db, "assignments", "trigger_config", assignment.id, stored)
            await _store(db, "agents", "integration_bindings", unreadable_bindings.id, r'{"github": ["\ud800"]}')
            await db.commit()
        findings = await _audit(scratch.sessions)

    finding = findings["integrations.provider_webhook_unverified"]
    assert finding.description.startswith("Up to 5 active assignment(s)")
    assert "github: 5 assignment(s), no OAuth default" in finding.description
