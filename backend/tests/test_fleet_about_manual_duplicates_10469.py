"""A documented manual prerequisite that repeats a checked line is dropped (#10469 S1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

from app.services import fleet_about
from app.services.fleet_about import TO_CONFIRM, covered_by_configuration, live_prerequisites

NOW = datetime(2026, 11, 9, 18, 0, tzinfo=UTC)


def test_covered_by_configuration_matches_whole_subjects_only():
    subjects = {"[Outlook] Samuel Hassine (CEO Office)", "ceo-office@filigran.io", "GitHub", "kb", "Default"}
    assert covered_by_configuration("Service account ceo-office@filigran.io", subjects)
    assert covered_by_configuration(
        "Outlook agent account '[Outlook] Samuel Hassine (CEO Office)' bound to the Assistant", subjects
    )
    assert covered_by_configuration("GitHub connected for the team", subjects)
    # A subject inside a longer word or address is not a match; short and generic subjects never match.
    assert not covered_by_configuration("Write to someone-ceo-office@filigran.io.example", {"ceo-office@filigran.io"})
    assert not covered_by_configuration("A contract signed with the vendor", subjects)
    assert not covered_by_configuration("The kb owner briefed", subjects)
    assert not covered_by_configuration("Default escalation contact briefed", subjects)
    # The automations line is derived: a manual line about switching them on repeats it.
    assert covered_by_configuration("An administrator switches each automation on", set(), automations=True)
    assert not covered_by_configuration("An administrator switches each automation on", set())


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Db:
    def __init__(self, accounts):
        self.accounts = accounts

    async def execute(self, _query):
        return _Result(self.accounts)


async def test_duplicates_of_agent_account_integration_and_handover_are_dropped(monkeypatch):
    from app.models.agent import Agent
    from app.models.integration import Integration
    from app.models.user import User

    lead, legal = uuid.uuid4(), uuid.uuid4()
    teams, service = uuid.uuid4(), uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    lead_agent = SimpleNamespace(
        id=lead,
        name="Assistant",
        enabled=True,
        user_id=None,
        integration_bindings={"microsoft": [str(teams)]},
        integration_template_bindings=[],
        handover_agent_ids=[str(legal)],
        knowledge_base_ids=[],
        mcp_server_ids=[],
    )
    tables = {
        Agent: {lead: lead_agent, legal: SimpleNamespace(id=legal, name="[Legal] Counsel", enabled=True)},
        Integration: {
            teams: SimpleNamespace(
                status="connected",
                token_expires_at=None,
                refresh_token_enc=None,
                provider="microsoft",
                name="[CEO Office] Microsoft",
                user_id=service,
                oauth_token_owner_id=None,
            )
        },
        User: {service: SimpleNamespace(id=service, email="ceo-office@filigran.io")},
    }

    async def rows(_db, model, ids):
        return {i: tables.get(model, {})[i] for i in ids if i in tables.get(model, {})}

    async def visible(_db, _model, ids, _user, _groups):
        return set(ids)

    async def members(_db, _fleet_id):
        return []

    async def counts(_db, _fleet):
        return SimpleNamespace(total=0, enabled=0)

    monkeypatch.setattr(fleet_about, "_rows", rows)
    monkeypatch.setattr(fleet_about, "_visible_ids", visible)
    monkeypatch.setattr("app.services.fleets.fleet_members", members)
    monkeypatch.setattr("app.services.fleet_views.home_member_ids", lambda _rows, _orch: [lead])
    monkeypatch.setattr("app.services.fleet_views.fleet_automation_counts", counts)

    account = SimpleNamespace(
        id=uuid.uuid4(),
        name="[Outlook] Samuel Hassine (CEO Office)",
        account_email="samuel@example.com",
        provider="microsoft_outlook",
        agent_id=lead,
        user_id=user.id,
        enabled=True,
        integration_id=uuid.uuid4(),
        status="active",
        last_error=None,
    )
    documented = [
        # The integration's identity (its owner's email), by the agent account name, by the handover target.
        {"check": "manual", "label": "Service account ceo-office@filigran.io", "fix_route": ""},
        {"check": "manual", "label": "Outlook agent account '[Outlook] Samuel Hassine (CEO Office)'", "fix_route": ""},
        {"check": "manual", "label": "Handover to [Legal] Counsel agreed", "fix_route": ""},
        {"check": "manual", "label": "Mailbox samuel@example.com delegated", "fix_route": ""},
        {"check": "manual", "label": "The [CEO Office] Microsoft app approved", "fix_route": ""},
        {"check": "manual", "label": "Data processing agreement signed", "fix_route": ""},
    ]
    fleet = SimpleNamespace(id=uuid.uuid4(), orchestrator_agent_id=lead)
    lines = await live_prerequisites(_Db([account]), user, fleet, documented, group_ids=[], now=NOW)

    manual = [line for line in lines if line.kind == "manual"]
    # Only what no system can read survives, still to confirm.
    assert [line.label for line in manual] == ["Data processing agreement signed"]
    assert manual[0].state == TO_CONFIRM


async def test_a_fleet_whose_manual_lines_all_repeat_checked_ones_shows_none(monkeypatch):
    lead = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    lead_agent = SimpleNamespace(
        id=lead,
        name="Lead",
        enabled=True,
        integration_bindings={},
        integration_template_bindings=[],
        handover_agent_ids=[],
        knowledge_base_ids=[],
        mcp_server_ids=[],
    )

    async def rows(_db, model, ids):
        from app.models.agent import Agent

        return {lead: lead_agent} if model is Agent and lead in ids else {}

    async def visible(_db, _model, ids, _user, _groups):
        return set(ids)

    async def members(_db, _fleet_id):
        return []

    async def counts(_db, _fleet):
        return SimpleNamespace(total=3, enabled=1)

    monkeypatch.setattr(fleet_about, "_rows", rows)
    monkeypatch.setattr(fleet_about, "_visible_ids", visible)
    monkeypatch.setattr("app.services.fleets.fleet_members", members)
    monkeypatch.setattr("app.services.fleet_views.home_member_ids", lambda _rows, _orch: [lead])
    monkeypatch.setattr("app.services.fleet_views.fleet_automation_counts", counts)

    documented = [{"check": "manual", "label": "An administrator switches each automation on", "fix_route": ""}]
    fleet = SimpleNamespace(id=uuid.uuid4(), orchestrator_agent_id=lead)
    lines = await live_prerequisites(_Db([]), user, fleet, documented, group_ids=[], now=NOW)
    assert not [line for line in lines if line.kind == "manual"]
    assert [line.check for line in lines] == ["automations:enabled"]
