"""The Security Audit's tool approval findings.

``_check_tool_approvals`` reports the platform kill switch and the install
default against what the approval gate resolves (``services/hitl_whitelist.py``),
then - only while something asks - what lets a tool call run unasked: trusted
channel bots, connected platforms whose tools skip approval, users' allow-all
mode and whitelist additions that change data.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.api.v1 import security_audit
from app.core.security_controls import UNREADABLE_ROW
from app.tools.approval import POLICY_REQUIRE, POLICY_SKIP, SET_BY_ADMIN, SET_BY_DEFAULT, SET_BY_DEPLOYMENT


class _Result:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return list(self._rows)

    def scalars(self) -> _Result:
        return self

    def scalar(self):
        return self._rows[0] if self._rows else None


class _ApprovalsDb:
    """Answers each query of the check by the table it reads, and records the tables asked."""

    def __init__(
        self,
        *,
        agents: list[tuple[str, bool | None]] = (),
        bots: list[str] = (),
        registrations: list[tuple[str, bool | None, str, str]] = (),
        allow_all: int = 0,
        additions: list[str] = (),
    ) -> None:
        self._answers = {
            "agents": list(agents),
            "channel_bots": list(bots),
            "platform_registrations": list(registrations),
            "users": [allow_all],
            "hitl_whitelist_overrides": list(additions),
        }
        self.tables: list[str] = []

    async def execute(self, statement):
        table = statement.get_final_froms()[0].name
        self.tables.append(table)
        return _Result(self._answers[table])


def _platform(*, enabled: bool | None = True, default: bool | None = True) -> SimpleNamespace:
    return SimpleNamespace(tool_approvals_enabled=enabled, approve_tool_calls_default=default)


@pytest.fixture
def chat(monkeypatch):
    state = {"enabled": False}

    async def _is_chat_enabled(_db) -> bool:
        return state["enabled"]

    monkeypatch.setattr("app.api.v1.platform.is_chat_enabled", _is_chat_enabled)
    return state


async def _findings(db: _ApprovalsDb, platform) -> dict[str, security_audit.AuditFinding]:
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_tool_approvals(db, findings, platform)
    assert len({f.id for f in findings}) == len(findings)
    return {f.id: f for f in findings}


def test_the_report_runs_the_check_on_the_loaded_platform_row():
    source = inspect.getsource(security_audit.build_security_audit_report)
    assert "_check_tool_approvals(db, findings, platform_row)" in source


async def test_an_unreadable_row_reports_nothing_and_reads_nothing(chat):
    db = _ApprovalsDb(agents=[("Mail Desk", True)])
    assert await _findings(db, UNREADABLE_ROW) == {}
    assert db.tables == []


async def test_the_kill_switch_off_warns_while_an_agent_would_ask_and_reports_nothing_else(chat):
    db = _ApprovalsDb(
        agents=[("Mail Desk", True), ("Relay", None), ("Scout", False)],
        bots=["Support bot"],
        registrations=[("OpenCTI", None, POLICY_SKIP, SET_BY_ADMIN)],
        allow_all=3,
        additions=["delete_issue"],
    )
    found = await _findings(db, _platform(enabled=False, default=False))
    assert list(found) == ["agents.tool_approvals_off"]
    warning = found["agents.tool_approvals_off"]
    assert warning.severity == "warning"
    assert warning.category == "agents"
    assert warning.control == "agents.tool_approvals"
    assert "Mail Desk" in warning.description
    assert "Relay" not in warning.description
    assert "Settings > Access and security > Tool approvals" in warning.recommendation
    # Nothing asks while it is off, so no exemption is worth a read.
    assert db.tables == ["agents"]


async def test_an_agent_on_the_install_default_counts_as_asking_while_the_default_is_on(chat):
    chat["enabled"] = True
    found = await _findings(_ApprovalsDb(agents=[("Relay", None)]), _platform(enabled=False, default=True))
    description = found["agents.tool_approvals_off"].description
    assert "Relay" in description
    assert "the General Assistant" in description


async def test_the_kill_switch_off_is_context_when_nothing_would_ask(chat):
    chat["enabled"] = True
    found = await _findings(
        _ApprovalsDb(agents=[("Relay", None), ("Scout", False)]), _platform(enabled=False, default=False)
    )
    assert list(found) == ["agents.tool_approvals_off_unused"]
    info = found["agents.tool_approvals_off_unused"]
    assert info.severity == "info"
    assert info.control == "agents.tool_approvals"
    assert "Settings > Access and security > Tool approvals" in info.recommendation


async def test_no_settings_row_reads_the_opt_in_default(chat):
    """Tool approvals are opt-in: before the row exists the default is off."""
    chat["enabled"] = True
    db = _ApprovalsDb(agents=[("Mail Desk", True), ("Relay", None)])
    found = await _findings(db, None)
    assert set(found) == {"agents.tool_approvals_on", "agents.tool_approvals_default_off"}
    assert "Relay" not in found["agents.tool_approvals_on"].description
    assert "the General Assistant" in found["agents.tool_approvals_default_off"].description


async def test_an_install_that_opted_in_passes_both_controls_and_names_what_asks(chat):
    chat["enabled"] = True
    db = _ApprovalsDb(agents=[("Mail Desk", True), ("Relay", None)])
    found = await _findings(db, _platform(enabled=True, default=True))
    assert set(found) == {"agents.tool_approvals_on", "agents.tool_approvals_default_on"}
    on = found["agents.tool_approvals_on"]
    assert (on.severity, on.control) == ("pass", "agents.tool_approvals")
    assert all(name in on.description for name in ("Mail Desk", "Relay", "the General Assistant"))
    default = found["agents.tool_approvals_default_on"]
    assert (default.severity, default.control) == ("pass", "agents.tool_approvals_default")
    assert "Relay" in default.description
    assert "Mail Desk" not in default.description
    assert db.tables == ["agents", "channel_bots", "platform_registrations", "users", "hitl_whitelist_overrides"]


async def test_an_install_that_never_opted_in_is_told_about_the_agents_on_the_default(chat):
    chat["enabled"] = True
    db = _ApprovalsDb(agents=[("Relay", None)], bots=["Support bot"])
    found = await _findings(db, _platform(enabled=True, default=False))
    assert list(found) == ["agents.tool_approvals_default_off"]
    info = found["agents.tool_approvals_default_off"]
    # The opt-in baseline: the hourly notifications alert on new critical / warning findings only.
    assert info.severity == "info"
    assert info.category == "agents"
    assert info.control == "agents.tool_approvals_default"
    assert "Relay" in info.description
    assert "the General Assistant" in info.description
    assert "opt-in" in info.description
    assert "on purpose" in info.description
    assert "Settings > Access and security > Tool approvals" in info.recommendation
    # With nothing asking, a trusted bot changes nothing.
    assert db.tables == ["agents"]


async def test_an_agent_set_to_on_still_asks_under_a_default_that_is_off(chat):
    found = await _findings(
        _ApprovalsDb(agents=[("Mail Desk", True), ("Relay", None)]), _platform(enabled=True, default=False)
    )
    assert set(found) == {"agents.tool_approvals_on", "agents.tool_approvals_default_off"}
    assert "Mail Desk" in found["agents.tool_approvals_on"].description
    assert "Relay" not in found["agents.tool_approvals_on"].description
    assert "Relay" in found["agents.tool_approvals_default_off"].description


async def test_the_default_is_not_reported_when_no_agent_follows_it(chat):
    found = await _findings(_ApprovalsDb(agents=[("Mail Desk", True)]), _platform(enabled=True, default=False))
    assert list(found) == ["agents.tool_approvals_on"]


async def test_agents_set_to_off_are_context(chat):
    found = await _findings(_ApprovalsDb(agents=[("Scout", False)]), _platform())
    assert list(found) == ["agents.tool_approvals_agents_opted_out"]
    info = found["agents.tool_approvals_agents_opted_out"]
    assert (info.severity, info.control) == ("info", "agents.tool_approvals")
    assert "Scout" in info.description


async def test_what_lets_a_call_run_unasked_is_reported_while_something_asks(chat):
    db = _ApprovalsDb(
        agents=[("Mail Desk", True)],
        bots=["Support bot"],
        registrations=[
            ("OpenCTI Prod", None, POLICY_SKIP, SET_BY_ADMIN),
            ("OpenAEV", True, POLICY_SKIP, SET_BY_DEPLOYMENT),
            ("OpenCTI Lab", None, POLICY_SKIP, SET_BY_DEPLOYMENT),
            ("OpenCRQ", None, POLICY_REQUIRE, SET_BY_DEFAULT),
        ],
        allow_all=2,
        additions=["get_issue", "delete_issue"],
    )
    found = await _findings(db, _platform())
    assert set(found) == {
        "agents.tool_approvals_on",
        "agents.tool_approvals_bots_trusted",
        "agents.tool_approvals_platforms_skip",
        "agents.tool_approvals_users_allow_all",
        "agents.tool_approvals_whitelist_additions",
    }
    assert all(f.category == "agents" and f.control == "agents.tool_approvals" for f in found.values())

    bots = found["agents.tool_approvals_bots_trusted"]
    assert bots.severity == "warning"
    assert "Support bot" in bots.description
    assert "Settings > Integrations > Channels & Bots" in bots.recommendation

    platforms = found["agents.tool_approvals_platforms_skip"]
    assert platforms.severity == "warning"
    # An administrator's skip holds; a seeded skip yields to a platform that can prompt.
    assert "OpenCTI Prod" in platforms.description
    assert "OpenCTI Lab" in platforms.description
    assert "OpenAEV" not in platforms.description
    assert "OpenCRQ" not in platforms.description
    assert "Settings > Access and security > Tool approvals" in platforms.recommendation

    users = found["agents.tool_approvals_users_allow_all"]
    assert users.severity == "info"
    assert users.title.startswith("2 user(s)")

    additions = found["agents.tool_approvals_whitelist_additions"]
    assert additions.severity == "info"
    assert "delete_issue" in additions.description
    assert "get_issue" not in additions.description
    assert "Settings > Access and security > Tool approvals" in additions.recommendation


async def test_whitelisted_reads_alone_are_not_reported(chat):
    found = await _findings(_ApprovalsDb(agents=[("Mail Desk", True)], additions=["get_issue"]), _platform())
    assert list(found) == ["agents.tool_approvals_on"]


@pytest.mark.parametrize("kill_switch", [True, False], ids=["approvals-on", "env-seeded-off"])
async def test_an_upgraded_install_raises_no_alert(chat, kill_switch):
    """What the migrations leave on an existing install alerts nobody.

    Agents migrate with no switch of their own and the install default off,
    bots ask, platforms keep the seeded "require" policy and nobody is in
    allow-all mode. ``security_audit_notifications`` tells admins about new
    critical and warning findings, so an upgrade must produce none.
    """
    chat["enabled"] = True
    db = _ApprovalsDb(
        agents=[("Mail Desk", None), ("Relay", None)],
        registrations=[
            ("OpenCTI", None, POLICY_REQUIRE, SET_BY_DEFAULT),
            ("OpenAEV", False, POLICY_REQUIRE, SET_BY_DEFAULT),
        ],
    )
    found = await _findings(db, _platform(enabled=kill_switch, default=False))
    assert found
    assert {f.severity for f in found.values()} <= {"info", "pass"}
