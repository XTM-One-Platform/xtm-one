"""The fleet About checklist derived from the configuration and checked live per member (#10258 S1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services import fleet_about
from app.services.fleet_about import (
    MET,
    NOT_MET,
    PROFILE_LABELS,
    REASON_CODES,
    TO_CONFIRM,
    UNREADABLE,
    agent_account_state,
    can_open,
    integration_state,
    live_prerequisites,
    mcp_state,
    reason_text,
)

NOW = datetime(2026, 11, 9, 2, 0, tzinfo=UTC)


def _integration(**kw):
    base = {
        "status": "connected",
        "token_expires_at": None,
        "refresh_token_enc": None,
        "provider": "github",
        "name": "[Sam] GitHub",
    }
    return SimpleNamespace(**{**base, **kw})


def test_integration_state_reads_the_bound_row():
    assert integration_state(_integration(), now=NOW) == (MET, "", {})
    assert integration_state(None, now=NOW)[:2] == (NOT_MET, "integration_deleted")
    assert integration_state(_integration(status="disconnected"), now=NOW)[1] == "integration_disconnected"
    # A raw status is never shown: "configured" reads as not signed in, an unknown one as not connected.
    assert integration_state(_integration(status="configured"), now=NOW)[1] == "integration_not_signed_in"
    assert integration_state(_integration(status="weird"), now=NOW)[1] == "integration_not_connected"
    expired = _integration(token_expires_at=NOW - timedelta(days=6))
    assert integration_state(expired, now=NOW) == (NOT_MET, "token_expired", {"date": "2026-11-03"})
    # A refresh token renews an expired access token: still met.
    assert (
        integration_state(_integration(token_expires_at=NOW - timedelta(days=1), refresh_token_enc="x"), now=NOW)[0]
        == MET
    )


def test_agent_account_state_says_why():
    ok = SimpleNamespace(enabled=True, integration_id=uuid.uuid4(), status="active", last_error=None)
    assert agent_account_state(ok) == (MET, "", {})
    off = SimpleNamespace(**{**vars(ok), "enabled": False})
    assert agent_account_state(off) == (NOT_MET, "account_switched_off", {})
    paused = SimpleNamespace(**{**vars(ok), "status": "paused", "last_error": "token revoked"})
    assert agent_account_state(paused) == (NOT_MET, "account_paused", {"detail": "token revoked"})
    assert reason_text(*agent_account_state(paused)[1:]) == "paused: token revoked"
    assert agent_account_state(SimpleNamespace(**{**vars(ok), "status": "setup"}))[0] == NOT_MET
    assert agent_account_state(SimpleNamespace(**{**vars(ok), "integration_id": None}))[0] == NOT_MET


def test_mcp_state():
    assert mcp_state(SimpleNamespace(enabled=True, auth_mode="headers", oauth_status="disconnected")) == (MET, "", {})
    assert mcp_state(SimpleNamespace(enabled=True, auth_mode="oauth", oauth_status="disconnected"))[0] == NOT_MET
    assert mcp_state(SimpleNamespace(enabled=False, auth_mode="headers", oauth_status="connected"))[0] == NOT_MET


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Db:
    """Answers the one direct query of :func:`live_prerequisites`: the agent accounts."""

    def __init__(self, accounts):
        self.accounts = accounts

    async def execute(self, _query):
        return _Result(self.accounts)


async def test_a_complete_fleet_is_all_met_grouped_per_integration_and_tied_to_members(monkeypatch):
    from app.models.agent import Agent
    from app.models.integration import Integration
    from app.models.knowledge_base import KnowledgeBase
    from app.models.mcp_server import MCPServer

    lead, dev, outside = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    gh, kb = uuid.uuid4(), uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)

    def agent(aid, **kw):
        base = {
            "id": aid,
            "name": "A",
            "enabled": True,
            "integration_bindings": {},
            "integration_template_bindings": [],
            "handover_agent_ids": [],
            "knowledge_base_ids": [],
            "mcp_server_ids": [],
        }
        return SimpleNamespace(**{**base, **kw})

    agents = {
        lead: agent(
            lead,
            integration_bindings={"github": [str(gh)]},
            handover_agent_ids=[str(dev), str(outside)],
            knowledge_base_ids=[str(kb)],
        ),
        dev: agent(dev, integration_bindings={"github": [str(gh)]}),
        outside: agent(outside, name="Legal entry"),
    }
    tables = {
        Agent: agents,
        Integration: {gh: _integration()},
        KnowledgeBase: {kb: SimpleNamespace(name="Working")},
        MCPServer: {},
    }

    async def rows(_db, model, ids):
        return {i: tables.get(model, {})[i] for i in ids if i in tables.get(model, {})}

    async def visible(_db, model, ids, _user, _groups):
        return set(ids)

    async def members(_db, _fleet_id):
        return []

    async def counts(_db, _fleet):
        return SimpleNamespace(total=2, enabled=1)

    async def profile(_db, _user, items, **_kw):
        return [SimpleNamespace(check=i["check"], label=i["label"], fix_route=i["fix_route"], met=True) for i in items]

    monkeypatch.setattr(fleet_about, "_rows", rows)
    monkeypatch.setattr(fleet_about, "_visible_ids", visible)
    monkeypatch.setattr(fleet_about, "evaluate_prerequisites", profile)
    monkeypatch.setattr("app.services.fleets.fleet_members", members)
    monkeypatch.setattr("app.services.fleet_views.home_member_ids", lambda _rows, _orch: [lead, dev])
    monkeypatch.setattr("app.services.fleet_views.fleet_automation_counts", counts)

    account = SimpleNamespace(
        id=uuid.uuid4(),
        name="Outlook mailbox",
        provider="microsoft_outlook",
        agent_id=lead,
        user_id=user.id,
        enabled=True,
        integration_id=uuid.uuid4(),
        status="active",
        last_error=None,
    )
    documented = [
        {"check": "integration:github", "label": "GitHub connected", "fix_route": "/integrations"},
        {"check": "profile:sector", "label": "Sector", "fix_route": "/settings/organization/profile"},
        {"check": "manual", "label": "One", "fix_route": ""},
        {"check": "manual", "label": "Two", "fix_route": ""},
        {"check": "manual", "label": "Three", "fix_route": ""},
    ]
    fleet = SimpleNamespace(id=uuid.uuid4(), orchestrator_agent_id=lead)
    lines = await live_prerequisites(_Db([account]), user, fleet, documented, group_ids=[], now=NOW)

    checks = [line.check for line in lines]
    # One line for the GitHub integration, both members on it; the AI's provider line is replaced.
    assert checks.count(f"binding:{gh}") == 1
    assert "integration:github" not in checks
    github = next(line for line in lines if line.check == f"binding:{gh}")
    assert github.member_ids == [str(lead), str(dev)]
    assert github.label == "GitHub ([Sam] GitHub)" and github.provider == "github"
    assert github.fix_route == f"/integrations/{gh}"
    # The handover to a home member is not a prerequisite; the one outside the fleet is.
    assert f"handover:{dev}" not in checks
    assert f"handover:{outside}" in checks
    assert f"agent_account:{account.id}" in checks and f"kb:{kb}" in checks
    assert "automations:enabled" in checks and "profile:sector" in checks
    # Manual lines: at most two, to confirm, never failing.
    manual = [line for line in lines if line.kind == "manual"]
    assert [line.label for line in manual] == ["One", "Two"]
    assert all(line.state == TO_CONFIRM for line in manual)
    assert all(line.state == MET for line in lines if line.kind != "manual")
    # Labels the platform writes travel as catalog keys; a name stays as written.
    handover = next(line for line in lines if line.check == f"handover:{outside}")
    assert (handover.label_key, handover.label_params) == ("Handover to {name}", {"name": "Legal entry"})
    assert handover.label == "Handover to Legal entry"
    sector = next(line for line in lines if line.check == "profile:sector")
    assert sector.label_key == PROFILE_LABELS["profile:sector"]
    assert github.label_key == ""
    # A non-admin may not open an administrator's page: the profile Fix is not offered.
    assert sector.can_fix is False and github.can_fix is True


def test_can_open_gates_admin_pages():
    admin, peer = SimpleNamespace(is_admin=True), SimpleNamespace(is_admin=False)
    assert can_open("/settings/organization/profile", admin) is True
    assert can_open("/settings/organization/profile", peer) is False
    assert can_open("/integrations/1", peer) is True
    assert can_open("", admin) is False


def test_every_reason_code_has_its_plain_english():
    for code in REASON_CODES:
        assert reason_text(code, {"date": "2026-11-03"}), code
    assert reason_text("token_expired", {"date": "2026-11-03"}) == "token expired 2026-11-03"
    assert reason_text("") == ""


async def test_a_broken_binding_is_not_met_and_an_unshared_one_is_unreadable_never_met(monkeypatch):
    from app.models.agent import Agent
    from app.models.integration import Integration

    lead = uuid.uuid4()
    broken, hidden = uuid.uuid4(), uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    lead_agent = SimpleNamespace(
        id=lead,
        name="Lead",
        enabled=True,
        integration_bindings={"slack": [str(broken)], "jira": [str(hidden)]},
        integration_template_bindings=[],
        handover_agent_ids=[],
        knowledge_base_ids=[],
        mcp_server_ids=[],
    )
    tables = {
        Agent: {lead: lead_agent},
        Integration: {
            broken: _integration(status="error", provider="slack", name="Slack"),
            hidden: _integration(provider="jira", name="Jira"),
        },
    }

    async def rows(_db, model, ids):
        return {i: tables.get(model, {})[i] for i in ids if i in tables.get(model, {})}

    async def visible(_db, model, ids, _user, _groups):
        return {i for i in ids if i != hidden}

    async def members(_db, _fleet_id):
        return []

    async def counts(_db, _fleet):
        return SimpleNamespace(total=0, enabled=0)

    monkeypatch.setattr(fleet_about, "_rows", rows)
    monkeypatch.setattr(fleet_about, "_visible_ids", visible)
    monkeypatch.setattr("app.services.fleets.fleet_members", members)
    monkeypatch.setattr("app.services.fleet_views.home_member_ids", lambda _rows, _orch: [lead])
    monkeypatch.setattr("app.services.fleet_views.fleet_automation_counts", counts)

    fleet = SimpleNamespace(id=uuid.uuid4(), orchestrator_agent_id=lead)
    lines = {line.check: line for line in await live_prerequisites(_Db([]), user, fleet, [], group_ids=[], now=NOW)}
    assert lines[f"binding:{broken}"].state == NOT_MET
    assert lines[f"binding:{broken}"].reason_code == "integration_error"
    assert lines[f"binding:{broken}"].reason == "in error: reconnect it"
    assert lines[f"binding:{broken}"].can_fix is True
    assert lines[f"binding:{broken}"].member_ids == [str(lead)]
    assert lines[f"binding:{hidden}"].state == UNREADABLE
    assert lines[f"binding:{hidden}"].fix_route == ""
    assert lines[f"binding:{hidden}"].reason_code == "not_shared"
    assert lines[f"binding:{hidden}"].can_fix is False
    # No automation at all: no automations line.
    assert "automations:enabled" not in lines
