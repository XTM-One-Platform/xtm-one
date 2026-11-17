"""Sign-in, MCP and A2A, sandboxes, fail modes, traces.

Every check follows the contract of ``app/core/security_controls.py``: a
literal id, a category of the taxonomy, the severity rubric (critical =
exploitable now, warning = off or misconfigured while what it protects is in
use, info = off with nothing in use or context, pass = enforced), a pass
whenever what it protects is in use, and ``control=`` on a finding about a
switch.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.v1 import security_audit
from app.core.redis import Redis
from app.core.security_controls import STORED_DEFAULTS, UNREADABLE_ROW
from app.models.channel_bot import ChannelBot

settings = security_audit.settings


def _sso_row(**overrides) -> SimpleNamespace:
    row = {column: default for (table, column), default in STORED_DEFAULTS.items() if table == "sso_config"}
    row["registration_enabled"] = False
    row.update(overrides)
    return SimpleNamespace(**row)


def _by_id(findings: list[security_audit.AuditFinding]) -> dict[str | None, security_audit.AuditFinding]:
    return {finding.id: finding for finding in findings}


class _RowDb:
    """``execute`` answering one aggregate row, counting the queries."""

    def __init__(self, row=(), scalar=0):
        self._row = row
        self._scalar = scalar
        self.queries = 0

    async def execute(self, *_args, **_kwargs):
        self.queries += 1
        return SimpleNamespace(one=lambda: self._row, scalar=lambda: self._scalar)


# ---------------------------------------------------------------------------
# SSO
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_hub_gate(monkeypatch):
    monkeypatch.setattr(settings, "xtm_hub_admin_group", "")


async def _sso_findings(sso) -> dict[str | None, security_audit.AuditFinding]:
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_sso(findings, sso)
    return _by_id(findings)


@pytest.mark.parametrize("sso", [None, _sso_row(), UNREADABLE_ROW])
async def test_no_sso_finding_without_sso(sso):
    assert await _sso_findings(sso) == {}


async def test_anyone_the_identity_provider_signs_in_becoming_an_admin_is_critical():
    findings = await _sso_findings(_sso_row(oidc_enabled=True, oidc_auto_admin_new_users=True))
    finding = findings["authentication.sso_auto_admin_on"]
    assert finding.severity == "critical"
    assert finding.title == "Anyone who signs in through OIDC becomes an administrator"
    assert "at every sign-in, existing accounts included" in finding.description
    assert "Automatically admin users" in (finding.recommendation or "")
    assert finding.control == "authentication.sso_auto_admin"
    # Creation is on the fly too: the second warning of the pair.
    assert findings["authentication.sso_open_account_creation"].severity == "warning"


async def test_promoting_provisioned_accounts_only_is_a_warning():
    findings = await _sso_findings(
        _sso_row(saml_enabled=True, saml_auto_admin_new_users=True, saml_auto_create_users=False)
    )
    finding = findings["authentication.sso_auto_admin_on"]
    assert (finding.severity, finding.title) == (
        "warning",
        "Everyone who signs in through SAML becomes an administrator",
    )
    assert findings["authentication.sso_accounts_provisioned"].severity == "pass"


async def test_sso_that_keeps_roles_and_provisions_accounts_passes_twice():
    findings = await _sso_findings(
        _sso_row(oidc_enabled=True, saml_enabled=True, oidc_auto_create_users=False, saml_auto_create_users=False)
    )
    assert findings["authentication.sso_roles_kept"].severity == "pass"
    assert findings["authentication.sso_accounts_provisioned"].control == "authentication.sso_account_creation"
    assert "authentication.sso_auto_admin_on" not in findings


async def test_open_account_creation_recommends_the_allowed_domains():
    findings = await _sso_findings(_sso_row(oidc_enabled=True, saml_enabled=True))
    finding = findings["authentication.sso_open_account_creation"]
    assert finding.title.endswith("(OIDC and SAML)")
    assert "with no allowed email domain" in finding.description
    assert "Set Allowed email domains" in (finding.recommendation or "")
    assert "On-the-fly user creation" in (finding.recommendation or "")


async def test_account_creation_restricted_to_the_allowed_domains_passes():
    findings = await _sso_findings(
        _sso_row(oidc_enabled=True, saml_enabled=True, allowed_domains=" Example.com, corp.io ")
    )
    assert "authentication.sso_open_account_creation" not in findings
    finding = findings["authentication.sso_account_creation_restricted"]
    assert (finding.severity, finding.control) == ("pass", "authentication.sso_account_creation")
    assert finding.title == "SSO creates accounts for 2 email domain(s) only"
    assert "OIDC and SAML, for an email address at example.com, corp.io only" in finding.description


async def test_auto_admin_where_the_allowed_domains_restrict_creation_is_a_warning():
    findings = await _sso_findings(
        _sso_row(oidc_enabled=True, oidc_auto_admin_new_users=True, allowed_domains="example.com")
    )
    finding = findings["authentication.sso_auto_admin_on"]
    assert (finding.severity, finding.title) == (
        "warning",
        "Everyone who signs in through OIDC becomes an administrator",
    )
    assert "for an email address at example.com only" in finding.description
    assert "existing accounts included, whatever their email domain" in finding.description


async def test_the_xtm_hub_decides_an_oidc_sign_in(monkeypatch):
    monkeypatch.setattr(settings, "xtm_hub_admin_group", "xtm-one-admins")
    findings = await _sso_findings(_sso_row(oidc_enabled=True, oidc_auto_admin_new_users=True))
    assert "authentication.sso_auto_admin_on" not in findings
    assert "The XTM Hub decides" in findings["authentication.sso_roles_kept"].description
    assert "The XTM Hub decides" in findings["authentication.sso_accounts_provisioned"].description


async def test_the_allowed_domains_restrict_an_oidc_sign_in_the_xtm_hub_admits(monkeypatch):
    """The Hub decides who signs in through OIDC; the domains still decide who gets a new account."""
    monkeypatch.setattr(settings, "xtm_hub_admin_group", "xtm-one-admins")
    findings = await _sso_findings(_sso_row(oidc_enabled=True, oidc_auto_create_users=True, allowed_domains="example.com"))
    assert "authentication.sso_accounts_provisioned" not in findings
    finding = findings["authentication.sso_account_creation_restricted"]
    assert (finding.severity, finding.control) == ("pass", "authentication.sso_account_creation")
    assert "On-the-fly user creation is on for OIDC, for an email address at example.com only" in finding.description
    assert "The XTM Hub decides" in finding.description


async def test_password_sign_in_next_to_sso_is_context():
    findings = await _sso_findings(_sso_row(oidc_enabled=True, oidc_auto_create_users=False))
    finding = findings["authentication.local_sign_in_with_sso"]
    assert (finding.severity, finding.control) == ("info", "authentication.local_sign_in")
    assert "offboarding" in finding.description
    sso_only = await _sso_findings(_sso_row(oidc_enabled=True, oidc_auto_create_users=False, local_auth_enabled=False))
    assert "authentication.local_sign_in_with_sso" not in sso_only


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


async def _registration_findings(sso) -> dict[str | None, security_audit.AuditFinding]:
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_self_registration(findings, sso)
    return _by_id(findings)


async def test_open_self_registration_is_a_warning():
    finding = (await _registration_findings(_sso_row(registration_enabled=True)))[
        "authentication.self_registration_open"
    ]
    assert (finding.severity, finding.control) == ("warning", "authentication.self_registration")
    assert "Allow local registration" in (finding.recommendation or "")


async def test_self_registration_restricted_to_domains_is_context():
    findings = await _registration_findings(
        _sso_row(registration_enabled=True, allowed_domains=" Example.com, corp.io ")
    )
    finding = findings["authentication.self_registration_restricted"]
    assert finding.severity == "info"
    assert finding.title == "Self-registration is open to 2 email domain(s)"
    assert "example.com, corp.io" in finding.description


async def test_self_registration_off_passes_while_local_sign_in_is_on():
    findings = await _registration_findings(_sso_row())
    assert set(findings) == {"authentication.self_registration_off"}
    finding = findings["authentication.self_registration_off"]
    assert (finding.severity, finding.control) == ("pass", "authentication.self_registration")


@pytest.mark.parametrize(
    "sso",
    [_sso_row(registration_enabled=True, local_auth_enabled=False), UNREADABLE_ROW],
)
async def test_no_self_registration_finding_when_local_sign_in_is_off_or_unread(sso):
    assert await _registration_findings(sso) == {}


@pytest.mark.parametrize(("mode", "reported"), [("xtm_one", False), ("copilot", True)])
async def test_a_fresh_install_registers_per_its_mode(monkeypatch, mode, reported):
    monkeypatch.setattr(settings, "platform_mode", mode)
    assert ("authentication.self_registration_open" in await _registration_findings(None)) is reported


# ---------------------------------------------------------------------------
# MFA
# ---------------------------------------------------------------------------


async def _mfa_findings(row, sso=None) -> tuple[dict[str | None, security_audit.AuditFinding], _RowDb]:
    db = _RowDb(row)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_mfa(db, findings, sso)
    return _by_id(findings), db


async def test_administrators_without_a_second_factor_are_a_warning():
    # (local-password accounts, of them without MFA, admins, admins without MFA)
    findings, _db = await _mfa_findings((12, 5, 3, 2))
    finding = findings["authentication.admins_without_mfa"]
    assert finding.severity == "warning"
    assert finding.title == "2 of 3 administrator(s) sign in with a password and no second factor"
    local = findings["authentication.local_users_without_mfa"]
    assert (local.severity, local.title) == ("info", "5 of 12 account(s) with a password have no second factor")


async def test_every_administrator_enrolled_is_a_pass():
    findings, _db = await _mfa_findings((4, 0, 2, 0))
    assert findings["authentication.admins_mfa_enrolled"].severity == "pass"
    assert set(findings) == {"authentication.admins_mfa_enrolled"}


async def test_no_account_with_a_password_says_nothing():
    findings, db = await _mfa_findings((0, 0, 0, 0))
    assert findings == {}
    assert db.queries == 1


@pytest.mark.parametrize("sso", [_sso_row(oidc_enabled=True, local_auth_enabled=False), UNREADABLE_ROW])
async def test_mfa_coverage_is_not_counted_when_passwords_are_refused(sso):
    findings, db = await _mfa_findings((9, 9, 9, 9), sso)
    assert findings == {}
    assert db.queries == 0


async def test_the_mfa_counts_are_one_aggregate_query():
    from sqlalchemy.dialects import postgresql

    captured = []

    class _CapturingDb(_RowDb):
        async def execute(self, statement, *_args, **_kwargs):
            captured.append(statement)
            return await super().execute(statement)

    await security_audit._check_mfa(_CapturingDb((0, 0, 0, 0)), [], None)
    [statement] = captured
    sql = str(statement.compile(dialect=postgresql.dialect())).lower()
    assert sql.count("filter (where") == 3
    assert "mfa_secret is not null" in sql and "hashed_password is not null" in sql


# ---------------------------------------------------------------------------
# Password, session and link lifetimes
# ---------------------------------------------------------------------------


@pytest.fixture
def _lifetimes(monkeypatch):
    def configure(**values):
        defaults = {
            "jwt_expire_minutes": 1440,
            "min_password_length": 12,
            "password_reset_expire_hours": 1,
            "invitation_expire_hours": 72,
        }
        for name, value in {**defaults, **values}.items():
            monkeypatch.setattr(settings, name, value)

    return configure


async def _lifetime_findings(sso=None) -> dict[str | None, security_audit.AuditFinding]:
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_account_lifetimes(findings, sso)
    return _by_id(findings)


async def test_hardened_lifetimes_pass_the_password_length_only(_lifetimes):
    _lifetimes()
    findings = await _lifetime_findings()
    assert set(findings) == {"authentication.password_length_sufficient"}
    assert findings["authentication.password_length_sufficient"].severity == "pass"


async def test_the_defaults_are_reported_as_context(_lifetimes):
    _lifetimes(
        jwt_expire_minutes=10080, min_password_length=8, password_reset_expire_hours=24, invitation_expire_hours=168
    )
    findings = await _lifetime_findings()
    assert {fid: f.severity for fid, f in findings.items()} == {
        "authentication.password_length_short": "info",
        "authentication.password_reset_link_long": "info",
    }
    assert findings["authentication.password_reset_link_long"].title == "Password reset links stay valid for 1 day(s)"
    assert findings["authentication.password_length_short"].control == "authentication.min_password_length"


async def test_an_invitation_lifetime_past_a_week_is_reported(_lifetimes):
    # A week, the default, is the bar: only a longer lifetime is reported.
    _lifetimes(invitation_expire_hours=168)
    assert "authentication.invitation_link_long" not in await _lifetime_findings()
    _lifetimes(invitation_expire_hours=336)
    finding = (await _lifetime_findings())["authentication.invitation_link_long"]
    assert (finding.severity, finding.control) == ("info", "authentication.invitation_expiry")
    assert finding.title == "Invitation links stay valid for 14 day(s)"
    assert "INVITATION_EXPIRE_HOURS=168" in (finding.recommendation or "")


async def test_a_password_shorter_than_eight_is_a_warning(_lifetimes):
    _lifetimes(min_password_length=6)
    finding = (await _lifetime_findings())["authentication.password_length_short"]
    assert finding.severity == "warning"
    assert "NIST SP 800-63B" in finding.description


async def test_password_lifetimes_are_not_reported_when_passwords_are_refused(_lifetimes):
    _lifetimes(
        min_password_length=6, password_reset_expire_hours=24, invitation_expire_hours=336, jwt_expire_minutes=2000
    )
    findings = await _lifetime_findings(_sso_row(oidc_enabled=True, local_auth_enabled=False))
    # An invitation still opens an account.
    assert set(findings) == {"authentication.invitation_link_long"}


# ---------------------------------------------------------------------------
# Session lifetime and idle timeout
# ---------------------------------------------------------------------------


async def _session_findings(**values) -> dict[str | None, security_audit.AuditFinding]:
    findings: list[security_audit.AuditFinding] = []
    with pytest.MonkeyPatch.context() as patch:
        for name, value in values.items():
            patch.setattr(settings, name, value)
        await security_audit._check_sessions(findings)
    return _by_id(findings)


async def test_the_default_session_settings_are_reported_as_context():
    findings = await _session_findings(jwt_expire_minutes=10080, session_idle_timeout_minutes=0)
    assert {fid: f.severity for fid, f in findings.items()} == {
        "authentication.session_lifetime_long": "info",
        "authentication.session_idle_timeout_off": "info",
    }
    lifetime = findings["authentication.session_lifetime_long"]
    assert lifetime.title == "A sign-in lasts 7 day(s)"
    assert lifetime.control == "authentication.session_lifetime"
    assert '"Absolute session lifetime (minutes)" (JWT_EXPIRE_MINUTES)' in lifetime.description
    assert (lifetime.recommendation or "").startswith(
        'Set "Absolute session lifetime (minutes)" in Settings > Access and security > Security options to 1440'
    )
    assert "not documented" not in (lifetime.recommendation or "")
    idle = findings["authentication.session_idle_timeout_off"]
    assert idle.control == "authentication.session_idle_timeout"
    assert (idle.recommendation or "").startswith('Set "Idle session timeout (minutes)"')


async def test_hardened_session_settings_pass_and_say_the_idle_timeout_needs_redis():
    findings = await _session_findings(jwt_expire_minutes=480, session_idle_timeout_minutes=15)
    assert {fid: f.severity for fid, f in findings.items()} == {
        "authentication.session_lifetime_bounded": "pass",
        "authentication.session_idle_timeout_on": "pass",
    }
    assert findings["authentication.session_lifetime_bounded"].title == "A sign-in lasts 8 hour(s) at most"
    assert "While Redis cannot be reached" in findings["authentication.session_idle_timeout_on"].description


async def test_an_idle_timeout_past_thirty_minutes_is_reported():
    finding = (await _session_findings(session_idle_timeout_minutes=120))["authentication.session_idle_timeout_long"]
    assert (finding.severity, finding.title) == ("info", "Unused sessions end after 2 hour(s)")
    assert "NIST SP 800-63B" in (finding.recommendation or "")


async def test_the_session_settings_are_read_as_saved_in_settings_security():
    from app.core import security_options as so

    so.publish_stored({"jwt_expire_minutes": 1440, "session_idle_timeout_minutes": 30})
    findings = await _session_findings()
    assert set(findings) == {"authentication.session_lifetime_bounded", "authentication.session_idle_timeout_on"}


@pytest.mark.parametrize(
    ("idle", "reachable", "expected"),
    [
        (0, True, None),
        (0, False, None),
        (30, True, ("infrastructure.session_idle_timeout_fails_open", "info")),
        (30, False, ("infrastructure.session_idle_timeout_unenforced", "warning")),
    ],
)
def test_the_audit_says_the_idle_timeout_fails_open_without_redis(monkeypatch, idle, reachable, expected):
    monkeypatch.setattr(settings, "session_idle_timeout_minutes", idle)
    found = [
        (f.id, f.severity)
        for f in security_audit._fail_mode_findings(reachable)
        if f.id and "session_idle_timeout" in f.id
    ]
    assert found == ([expected] if expected else [])


# ---------------------------------------------------------------------------
# Authentication: the SSO row the endpoint loaded
# ---------------------------------------------------------------------------


async def test_every_active_user_able_to_sign_in_is_a_pass(monkeypatch):
    monkeypatch.setattr(settings, "admin_email", "admin@example.com")
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_authentication(_RowDb(scalar=0), findings, None)
    assert _by_id(findings)["authentication.users_have_credentials"].severity == "pass"
    findings = []
    await security_audit._check_authentication(_RowDb(scalar=2), findings, None)
    by_id = _by_id(findings)
    assert by_id["authentication.users_without_credentials"].severity == "warning"
    assert "authentication.users_have_credentials" not in by_id


async def test_the_sso_check_reads_the_loaded_row(monkeypatch):
    monkeypatch.setattr(settings, "admin_email", "admin@example.com")
    db = _RowDb(scalar=0)
    for sso, expected in (
        (_sso_row(saml_enabled=True), "authentication.sso_enabled"),
        (None, "authentication.sso_not_configured"),
        (UNREADABLE_ROW, "authentication.sso_check_skipped"),
    ):
        findings: list[security_audit.AuditFinding] = []
        await security_audit._check_authentication(db, findings, sso)
        assert expected in _by_id(findings)


# ---------------------------------------------------------------------------
# Integrations, channels, assignments
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("debug", "severity"), [(False, "warning"), (True, "info")])
async def test_an_oauth_token_development_allow_list_outside_debug_is_a_warning(monkeypatch, debug, severity):
    monkeypatch.setattr(settings, "debug", debug)
    monkeypatch.setattr(settings, "oauth_token_url_strict", True)
    monkeypatch.setattr(settings, "oauth_token_dev_trusted_origins", ["http://localhost:8080"])
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_oauth_token_dev_origins(findings)
    [finding] = findings
    assert (finding.id, finding.severity) == ("integrations.oauth_token_dev_origins_set", severity)
    assert "http://localhost:8080" in finding.description
    monkeypatch.setattr(settings, "oauth_token_dev_trusted_origins", [])
    findings = []
    await security_audit._check_oauth_token_dev_origins(findings)
    assert findings == []


class _TokenUrlsDb:
    """The stored OAuth token URLs: the integrations' query first, then the MCP servers'."""

    def __init__(self, integrations=(), servers=()):
        self._answers = [list(integrations), list(servers)]

    async def execute(self, *_args, **_kwargs):
        rows = self._answers.pop(0)
        return SimpleNamespace(all=lambda: rows)


def _integration_token_url(url: str, name: str = "Acme IdP") -> SimpleNamespace:
    return SimpleNamespace(id="i-1", name=name, provider="custom_oauth", token_url=url)


async def _token_url_findings(db) -> list[security_audit.AuditFinding]:
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_oauth_token_url_policy(db, findings)
    return findings


async def test_an_oauth_token_development_allow_list_without_the_strict_checks_relaxes_nothing(monkeypatch):
    # Without the strict checks every loopback token URL is accepted:
    # no warning about the list, and the strict checks' own finding says it has no effect.
    monkeypatch.setattr(settings, "oauth_token_url_strict", False)
    monkeypatch.setattr(settings, "oauth_token_dev_trusted_origins", ["http://localhost:8080"])
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_oauth_token_dev_origins(findings)
    assert findings == []
    [finding] = await _token_url_findings(_TokenUrlsDb())
    assert (finding.id, finding.severity) == ("integrations.oauth_token_url_strict_off", "info")
    assert "http://localhost:8080" in finding.description
    assert "no effect until the strict checks are on" in finding.description


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://localhost:9000/token", "a loopback address"),
        ("https://127.0.0.1/token", "a loopback address"),
        ("https://[::1]:8443/oauth/token", "a loopback address"),
        ("http://2130706433/token", "a loopback address"),
        ("https://169.254.10.10/token", "a link-local address"),
        ("https://0.0.0.0/token", "a reserved address"),
        ("wss://idp.example.com/token", "a wss URL"),
    ],
)
async def test_a_stored_token_url_the_strict_checks_would_refuse_makes_them_a_warning(monkeypatch, url, reason):
    # What the strict checks protect is in use: a token URL accepted only because they are off.
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(settings, "oauth_token_url_strict", False)
    monkeypatch.setattr(settings, "oauth_token_dev_trusted_origins", [])
    server = SimpleNamespace(id="m-1", name="Remote MCP", token_url=url)
    [finding] = await _token_url_findings(_TokenUrlsDb(servers=[server]))
    assert (finding.id, finding.severity, finding.control) == (
        "integrations.oauth_token_url_strict_off",
        "warning",
        "integrations.oauth_token_url_strict",
    )
    assert finding.title == "1 OAuth token URL(s) are accepted only while the strict checks are off"
    assert f"MCP server Remote MCP (m-1): {reason}" in finding.description
    assert finding.recommendation.startswith("Point each of these at an address of its identity provider")
    assert 'turn on "Strict OAuth token URL checks"' in finding.recommendation


@pytest.mark.parametrize(
    "url",
    [
        "https://oauth2.googleapis.com/token",
        "https://10.0.0.5/token",
        "https://idp.internal/token",
        "http://169.254.169.254/latest",
        "http://localhost:abc/token",
        "not a url",
    ],
)
async def test_token_urls_the_strict_checks_accept_or_every_mode_refuses_stay_context(monkeypatch, url):
    # A public or private address, a name the audit does not resolve, and what
    # the default refuses as well (a metadata address, an unreadable port, no
    # URL at all): nothing relies on the lax mode.
    monkeypatch.setattr(settings, "oauth_token_url_strict", False)
    monkeypatch.setattr(settings, "oauth_token_dev_trusted_origins", [])
    [finding] = await _token_url_findings(_TokenUrlsDb(integrations=[_integration_token_url(url)]))
    assert (finding.severity, finding.title) == ("info", "Strict OAuth token URL checks are off")


async def test_a_loopback_token_url_a_development_origin_admits_or_a_dev_build_is_context(monkeypatch):
    monkeypatch.setattr(settings, "oauth_token_url_strict", False)
    monkeypatch.setattr(settings, "oauth_token_dev_trusted_origins", ["http://localhost:9000"])
    stored = [_integration_token_url("http://localhost:9000/token")]
    [finding] = await _token_url_findings(_TokenUrlsDb(integrations=stored))
    assert finding.severity == "info"
    monkeypatch.setattr(settings, "oauth_token_dev_trusted_origins", [])
    monkeypatch.setattr(settings, "debug", True)
    [finding] = await _token_url_findings(_TokenUrlsDb(integrations=stored))
    assert finding.severity == "info"
    assert finding.title == "1 OAuth token URL(s) are accepted only while the strict checks are off"


@pytest.mark.parametrize(
    ("mode", "finding_id", "severity"),
    [
        ("off", "agents.prompt_guard_model_output_off", "warning"),
        ("log", "agents.prompt_guard_model_output_logs_only", "info"),
        ("block", "agents.prompt_guard_model_output_blocks", "pass"),
    ],
)
async def test_the_model_output_guard_reads_off_monitoring_enforced(monkeypatch, mode, finding_id, severity):
    # A connected guard: off leaves every answer unevaluated, log only monitors, block enforces.
    from app.core import security_options

    async def _connected(_db, _provider):
        return True

    monkeypatch.setattr("app.services.integration_state.any_provider_connected", _connected)
    security_options.publish_stored({"prompt_guard_model_output": mode})
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_model_output_guard(SimpleNamespace(), findings)
    [finding] = findings
    assert (finding.id, finding.severity, finding.control) == (finding_id, severity, "agents.prompt_guard_model_output")


async def test_no_model_output_finding_without_a_connected_guard(monkeypatch):
    async def _not_connected(_db, _provider):
        return False

    monkeypatch.setattr("app.services.integration_state.any_provider_connected", _not_connected)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_model_output_guard(SimpleNamespace(), findings)
    assert findings == []


async def _lent_findings(monkeypatch, lent, truncated=False) -> list[security_audit.AuditFinding]:
    from app.services import integration_exposure

    async def _lent(_db):
        return lent, truncated

    monkeypatch.setattr(integration_exposure, "lent_personal_credentials", _lent)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_shared_personal_credentials(SimpleNamespace(), findings)
    return findings


async def test_personal_credentials_other_people_act_with_are_listed_as_context(monkeypatch):
    from app.services.integration_exposure import Audience, LentCredential

    lent = [
        LentCredential(
            "i-1", "Alice GitHub", "github", "Alice", ["agent"], Audience(user_ids={1, 2}, public_link=True)
        ),
        LentCredential("i-2", "Handed over", "jira", None, ["owner", "general_assistant"], Audience(everyone=True)),
    ]
    [finding] = await _lent_findings(monkeypatch, lent, truncated=True)
    assert (finding.id, finding.category, finding.severity, finding.control) == (
        "access.personal_credentials_lent",
        "access",
        "info",
        None,
    )
    assert finding.title == "2 personal credential(s) are used by other people"
    assert (
        "Alice GitHub (GitHub, i-1) of Alice: agents and what they attach - 2 other person(s) and anyone with a "
        "public widget link" in finding.description
    )
    assert (
        "Handed over (Atlassian Jira, i-2): its owner, with an account another person connected, "
        "the General Assistant - every user" in finding.description
    )
    assert "others may be missing" in finding.description
    assert "Sharing and access" in (finding.recommendation or "")
    assert "Don't share my connected account" in (finding.recommendation or "")


async def test_no_lent_personal_credential_says_nothing(monkeypatch):
    assert await _lent_findings(monkeypatch, []) == []


def test_every_way_a_credential_is_lent_is_named():
    from app.services import integration_exposure
    from app.services import resource_exposure as engine

    # A company-managed integration's credential is the company's: never lent.
    # A prompt or a custom tool carries variables, never an integration.
    kinds = (set(engine.DIRECT_PATHS) | set(engine.CARRIER_PATHS)) - {
        engine.PATH_COMPANY,
        engine.PATH_PROMPT,
        engine.PATH_CUSTOM_TOOL,
    } | {integration_exposure.LENT_TO_OWNER}
    assert set(security_audit._LENT_THROUGH) == kinds


class _BotsDb:
    def __init__(self, bots):
        self._bots = bots

    async def execute(self, *_args, **_kwargs):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self._bots))


def _bot(**overrides) -> ChannelBot:
    # A real model instance, never a namespace: the check may only read columns ChannelBot has.
    fields = {
        "provider": "slack_bot",
        "name": "Bot",
        "allow_anonymous": False,
        "allowed_receive_channels": [{"id": "C1", "name": "general"}],
        **overrides,
    }
    return ChannelBot(**fields)


async def test_channel_bots_without_anonymous_access_pass():
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_channel_bots(_BotsDb([_bot(), _bot()]), findings)
    finding = _by_id(findings)["channels.no_anonymous_access"]
    assert (finding.severity, finding.title) == ("pass", "No channel bot allows anonymous access (2 bot(s))")
    findings = []
    await security_audit._check_channel_bots(_BotsDb([_bot(allow_anonymous=True)]), findings)
    assert "channels.no_anonymous_access" not in _by_id(findings)
    findings = []
    await security_audit._check_channel_bots(_BotsDb([]), findings)
    assert findings == []


async def test_bots_without_a_receive_whitelist_are_counted():
    unrestricted = [_bot(allowed_receive_channels=None), _bot(provider="teams_bot", allowed_receive_channels=[])]
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_channel_bots(_BotsDb([*unrestricted, _bot()]), findings)
    finding = _by_id(findings)["channels.unrestricted_receive"]
    assert (finding.severity, finding.title) == ("info", "2 channel bot(s) without receive channel restrictions")
    findings = []
    await security_audit._check_channel_bots(_BotsDb([_bot(), _bot(provider="discord_bot")]), findings)
    assert "channels.unrestricted_receive" not in _by_id(findings)


class _AssignmentsDb:
    def __init__(self, webhook_assignments: int, secretless: int):
        self._row = (webhook_assignments, secretless)

    async def execute(self, *_args, **_kwargs):
        # The rows holding a surrogate escape SQL cannot parse: none here.
        return SimpleNamespace(one=lambda: self._row, all=lambda: [])


async def test_secretless_assignments_under_the_strict_flag_refuse_rather_than_expose(monkeypatch):
    monkeypatch.setattr(settings, "webhook_require_secret", True)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_assignments(_AssignmentsDb(3, 2), findings)
    by_id = _by_id(findings)
    assert "assignments.webhook_without_secret" not in by_id
    refused = by_id["assignments.webhook_without_secret_refused"]
    assert (refused.severity, refused.control) == ("info", "assignments.webhook_require_secret")
    assert refused.title.startswith("2 ")
    monkeypatch.setattr(settings, "webhook_require_secret", False)
    findings = []
    await security_audit._check_assignments(_AssignmentsDb(3, 2), findings)
    assert _by_id(findings)["assignments.webhook_without_secret"].control == "assignments.webhook_require_secret"


# ---------------------------------------------------------------------------
# MCP and A2A
# ---------------------------------------------------------------------------


@pytest.fixture
def _mcp(monkeypatch):
    def configure(**values):
        defaults = {
            "mcp_dcr_enabled": False,
            "mcp_cimd_enabled": False,
            "a2a_client_dev_trusted_origins": [],
            "a2a_allow_insecure_loopback": False,
            "test_only_insecure_transport_peers": [],
            "debug": False,
        }
        for name, value in {**defaults, **values}.items():
            monkeypatch.setattr(settings, name, value)

    return configure


async def _mcp_findings(platform=None, registered=0) -> tuple[dict[str | None, security_audit.AuditFinding], _RowDb]:
    db = _RowDb(scalar=registered)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_mcp(db, findings, platform)
    return _by_id(findings), db


async def test_a_hardened_mcp_surface_with_no_a2a_in_use_says_nothing(_mcp):
    _mcp()
    findings, db = await _mcp_findings()
    assert findings == {}
    # The A2A partners and the live exposures, counted to know whether HTTPS is required of anything.
    assert db.queries == 2


async def test_a2a_in_use_with_no_exception_passes_both_ways(_mcp):
    _mcp()
    findings, _db = await _mcp_findings(registered=2)
    assert {fid: (f.severity, f.control) for fid, f in findings.items()} == {
        "mcp.a2a_partners_over_https": ("pass", "mcp.a2a_dev_trusted_origins"),
        "mcp.a2a_requests_over_https": ("pass", "mcp.a2a_insecure_loopback"),
    }
    partners = findings["mcp.a2a_partners_over_https"]
    assert partners.title == "The A2A client reaches its 2 partner agent(s) over HTTPS only"
    # DEBUG counts loopback as HTTPS: the inbound pass would not hold.
    _mcp(debug=True)
    findings, _db = await _mcp_findings(registered=2)
    assert set(findings) == {"mcp.a2a_partners_over_https"}


async def test_dynamic_client_registration_names_the_clients_it_registered(_mcp):
    _mcp(mcp_dcr_enabled=True)
    findings, db = await _mcp_findings(registered=7)
    finding = findings["mcp.dynamic_client_registration_on"]
    assert (finding.severity, finding.title) == ("info", "MCP clients can register themselves (7 registered)")
    assert "MCP_DCR_ENABLED=false" in (finding.recommendation or "")
    assert db.queries == 3


async def test_client_metadata_documents_are_reported_when_on(_mcp):
    _mcp(mcp_cimd_enabled=True)
    findings, db = await _mcp_findings()
    finding = findings["mcp.client_id_metadata_documents_on"]
    assert (finding.severity, finding.control) == ("info", "mcp.client_id_metadata_documents")
    assert "MCP_CIMD_ENABLED=false" in (finding.recommendation or "")
    # Nothing counted: a document client leaves no row behind.
    assert db.queries == 2


async def test_the_xtm_one_mcp_server_is_read_from_the_loaded_row(_mcp):
    _mcp()
    findings, _db = await _mcp_findings(platform=SimpleNamespace(xtm_mcp_enabled=True))
    assert findings["mcp.xtm_mcp_server_on"].control == "mcp.xtm_mcp_server"
    for platform in (None, SimpleNamespace(xtm_mcp_enabled=False), UNREADABLE_ROW):
        findings, _db = await _mcp_findings(platform=platform)
        assert "mcp.xtm_mcp_server_on" not in findings


async def test_the_a2a_development_exceptions_are_warnings(_mcp):
    _mcp(
        a2a_client_dev_trusted_origins=["http://localhost:8200"],
        a2a_allow_insecure_loopback=True,
        test_only_insecure_transport_peers=["172.18.0.0/16"],
    )
    findings, _db = await _mcp_findings()
    assert {fid: (f.severity, f.control) for fid, f in findings.items()} == {
        "mcp.a2a_dev_origins_set": ("warning", "mcp.a2a_dev_trusted_origins"),
        "mcp.a2a_loopback_counts_as_https": ("warning", "mcp.a2a_insecure_loopback"),
        "mcp.test_only_transport_peers_set": ("warning", "mcp.test_only_insecure_transport_peers"),
    }
    _mcp(a2a_client_dev_trusted_origins=["http://localhost:8200"], debug=True)
    findings, _db = await _mcp_findings()
    assert findings["mcp.a2a_dev_origins_set"].severity == "info"


# ---------------------------------------------------------------------------
# The code interpreter, custom variables, the sandboxes
# ---------------------------------------------------------------------------


@pytest.fixture
def _sandbox(monkeypatch):
    def configure(**values):
        defaults = {
            "disable_code_interpreter": True,
            "disable_custom_tools": True,
            "disable_custom_variables": True,
            "code_interpreter_extra_allowed_modules": "",
            "custom_tool_extra_allowed_modules": "",
            "custom_tool_node_permission": True,
            "custom_tool_sandbox": "auto",
        }
        for name, value in {**defaults, **values}.items():
            monkeypatch.setattr(settings, name, value)
        monkeypatch.delenv("CUSTOM_TOOL_DOCKER", raising=False)

    return configure


async def _sandbox_findings() -> dict[str | None, security_audit.AuditFinding]:
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_sandbox_features(findings)
    return _by_id(findings)


async def test_hardened_sandboxes_say_nothing(_sandbox):
    _sandbox()
    assert await _sandbox_findings() == {}


async def test_the_code_interpreter_and_custom_variables_on_are_context(_sandbox):
    _sandbox(disable_code_interpreter=False, disable_custom_variables=False)
    findings = await _sandbox_findings()
    assert {fid: (f.severity, f.control) for fid, f in findings.items()} == {
        "tools.code_interpreter_enabled": ("info", "tools.code_interpreter_disabled"),
        "tools.custom_variables_enabled": ("info", "tools.custom_variables_disabled"),
        # In use with its whole blocklist: the pass of the modules check.
        "tools.code_interpreter_modules_blocked": ("pass", "tools.code_interpreter_extra_modules"),
    }


@pytest.mark.parametrize(
    ("active", "node_id", "severity"),
    [(True, "tools.node_permission_on", "pass"), (False, "tools.node_permission_unsupported", "warning")],
)
async def test_the_plain_subprocess_sandbox_intact_passes_where_the_node_binary_holds_it(
    _sandbox, monkeypatch, active, node_id, severity
):
    from app.services import custom_tool_executor

    async def _active() -> bool:
        return active

    monkeypatch.setattr(custom_tool_executor, "node_permission_active", _active)
    _sandbox(disable_custom_tools=False, custom_tool_sandbox="subprocess")
    findings = await _sandbox_findings()
    assert {fid: (f.severity, f.control) for fid, f in findings.items()} == {
        "tools.custom_tool_modules_blocked": ("pass", "tools.custom_tool_extra_modules"),
        node_id: (severity, "tools.custom_tool_node_permission"),
    }
    assert "CUSTOM_TOOL_SANDBOX=subprocess" in findings[node_id].description
    if not active:
        assert "Node.js 24 or later" in (findings[node_id].recommendation or "")


async def test_no_pass_for_a_sandbox_nothing_runs_in(_sandbox, monkeypatch):
    from app.services import custom_tool_executor

    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(custom_tool_executor, "node_permission_active", probe)
    _sandbox(disable_custom_tools=False, custom_tool_sandbox="docker")
    assert await _sandbox_findings() == {}
    probe.assert_not_awaited()


async def test_re_enabled_modules_are_named_and_warn_while_the_sandbox_runs(_sandbox):
    _sandbox(disable_code_interpreter=False, code_interpreter_extra_allowed_modules="socket, urllib.request, pickle")
    finding = (await _sandbox_findings())["tools.code_interpreter_modules_reenabled"]
    assert finding.severity == "warning"
    assert finding.title == "2 blocked module(s) re-enabled in the code interpreter"
    assert "re-enables socket, urllib" in finding.description
    assert "pickle stay blocked" in finding.description
    # The code interpreter off: nothing uses them today.
    _sandbox(code_interpreter_extra_allowed_modules="socket")
    finding = (await _sandbox_findings())["tools.code_interpreter_modules_reenabled"]
    assert finding.severity == "info"
    assert "disabled today" in finding.description


async def test_only_always_blocked_modules_are_context(_sandbox):
    _sandbox(disable_code_interpreter=False, code_interpreter_extra_allowed_modules="subprocess")
    finding = (await _sandbox_findings())["tools.code_interpreter_modules_reenabled"]
    assert finding.severity == "info"
    assert "names only modules that can never be re-enabled" in finding.description


@pytest.mark.parametrize(
    ("mode", "custom_tools_disabled", "severity"),
    [("auto", False, "warning"), ("subprocess", False, "warning"), ("docker", False, "info"), ("auto", True, "info")],
)
async def test_custom_tool_modules_and_node_permission_warn_while_the_plain_subprocess_runs(
    _sandbox, mode, custom_tools_disabled, severity
):
    _sandbox(
        disable_custom_tools=custom_tools_disabled,
        custom_tool_sandbox=mode,
        custom_tool_extra_allowed_modules="requests",
        custom_tool_node_permission=False,
    )
    findings = await _sandbox_findings()
    modules = findings["tools.custom_tool_modules_reenabled"]
    node = findings["tools.node_permission_off"]
    assert (modules.severity, node.severity) == (severity, severity)
    assert (modules.control, node.control) == ("tools.custom_tool_extra_modules", "tools.custom_tool_node_permission")
    if severity == "warning":
        assert f"CUSTOM_TOOL_SANDBOX={mode}" in node.description


# ---------------------------------------------------------------------------
# Fail modes, debug, CORS
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "reachable", "rate", "quota"),
    [
        (
            "closed",
            True,
            ("infrastructure.rate_limit_fails_closed", "pass"),
            ("infrastructure.quota_fails_closed", "pass"),
        ),
        (
            "closed",
            False,
            ("infrastructure.rate_limit_fails_closed", "pass"),
            ("infrastructure.quota_fails_closed", "pass"),
        ),
        ("open", True, ("infrastructure.rate_limit_fails_open", "info"), ("infrastructure.quota_fails_open", "info")),
        # A dependency failure that switches protections off, right now.
        (
            "open",
            False,
            ("infrastructure.rate_limit_fails_open", "critical"),
            ("infrastructure.quota_fails_open", "warning"),
        ),
    ],
)
def test_the_fail_modes_follow_redis(monkeypatch, mode, reachable, rate, quota):
    # Copilot mode: XTM One pins the quotas closed (below).
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    monkeypatch.setattr(settings, "rate_limit_fail_mode", mode)
    monkeypatch.setattr(settings, "quota_fail_mode", mode)
    rate_finding, quota_finding = security_audit._fail_mode_findings(reachable)
    assert (rate_finding.id, rate_finding.severity) == rate
    assert (quota_finding.id, quota_finding.severity) == quota
    assert rate_finding.control == "infrastructure.rate_limit_fail_mode"
    assert quota_finding.control == "infrastructure.quota_fail_mode"


@pytest.mark.parametrize("platform_mode", ["xtm_one", "ai_disabled"])
@pytest.mark.parametrize("reachable", [True, False])
@pytest.mark.parametrize("variable", [None, "open", "closed"])
def test_xtm_one_quotas_always_fail_closed_and_the_audit_recommends_nothing(
    monkeypatch, platform_mode, reachable, variable
):
    monkeypatch.setattr(settings, "platform_mode", platform_mode)
    monkeypatch.setattr(settings, "quota_fail_mode", variable or "open")
    if variable is not None:
        monkeypatch.setattr(settings, "_environment_fields", frozenset({"quota_fail_mode"}))
    _rate, quota_finding = security_audit._fail_mode_findings(reachable)
    assert (quota_finding.id, quota_finding.severity) == ("infrastructure.quota_fails_closed", "pass")
    assert quota_finding.control == "infrastructure.quota_fail_mode"
    assert quota_finding.recommendation is None
    assert quota_finding.description.startswith("Quotas are part of the Filigran licence")
    ignored = "QUOTA_FAIL_MODE=open is set and has no effect: remove it."
    assert (ignored in quota_finding.description) is (variable == "open")


def _install_redis(monkeypatch, *, reachable: bool) -> None:
    client = MagicMock(spec=Redis)
    client.ping = AsyncMock(return_value=True) if reachable else AsyncMock(side_effect=ConnectionError("refused"))
    monkeypatch.setattr(security_audit, "redis_client", AsyncMock(return_value=client))


@pytest.mark.parametrize(
    ("debug", "expected"),
    [(False, ("infrastructure.debug_off", "pass")), (True, ("infrastructure.debug_mode", "warning"))],
)
async def test_debug_mode_is_reported_either_way(monkeypatch, debug, expected):
    _install_redis(monkeypatch, reachable=True)
    monkeypatch.setattr(settings, "debug", debug)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_infrastructure(findings)
    finding = next(f for f in findings if f.id in ("infrastructure.debug_off", "infrastructure.debug_mode"))
    assert (finding.id, finding.severity) == expected
    assert finding.control == "infrastructure.debug_mode"


async def test_an_unreachable_redis_switches_the_rate_limits_off(monkeypatch):
    _install_redis(monkeypatch, reachable=False)
    monkeypatch.setattr(settings, "rate_limit_fail_mode", "open")
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_infrastructure(findings)
    by_id = _by_id(findings)
    assert by_id["infrastructure.redis_unreachable"].severity == "critical"
    assert by_id["infrastructure.rate_limit_fails_open"].severity == "critical"


async def test_cors_is_read_from_the_settings(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.setattr(settings, "cors_origins", ["https://xtm.example.com", "*"])
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_cors(findings)
    [finding] = findings
    assert (finding.id, finding.category, finding.severity, finding.control) == (
        "web.cors_wildcard",
        "web",
        "warning",
        "web.cors_origins",
    )
    monkeypatch.setattr(settings, "cors_origins", ["https://xtm.example.com"])
    findings = []
    await security_audit._check_cors(findings)
    [finding] = findings
    assert (finding.id, finding.severity) == ("web.cors_restricted", "pass")
    assert "https://xtm.example.com" in finding.description


# ---------------------------------------------------------------------------
# Traces
# ---------------------------------------------------------------------------


async def test_traces_keep_their_payloads_by_default_as_context():
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_trace_payloads(findings, None)
    [finding] = findings
    assert (finding.id, finding.severity, finding.control) == ("llm.trace_payloads_stored", "info", "llm.trace_payloads")
    assert "for 90 day(s)" in finding.description
    assert "Store full payloads" in finding.recommendation


async def test_payloads_off_passes_the_trace_payloads_control():
    row = SimpleNamespace(trace_storage_enabled=True, trace_store_payloads=False, trace_retention_days=30)
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_trace_payloads(findings, row)
    [finding] = findings
    # The pipeline reads the switch now: it is enforced, not ignored.
    assert (finding.id, finding.severity, finding.control) == ("llm.trace_payloads_not_stored", "pass", "llm.trace_payloads")
    assert "Store full payloads is off in Settings > Usage and quotas > Observability" in finding.description
    assert "for 30 day(s)" in finding.description
    assert "does not read" not in finding.description
    assert "Langfuse export" in finding.description


async def test_storage_off_passes_the_trace_payloads_control():
    for payloads in (True, False):
        row = SimpleNamespace(trace_storage_enabled=False, trace_store_payloads=payloads, trace_retention_days=7)
        findings: list[security_audit.AuditFinding] = []
        await security_audit._check_trace_payloads(findings, row)
        [finding] = findings
        assert (finding.id, finding.severity, finding.control) == ("llm.trace_storage_off", "pass", "llm.trace_payloads")
        assert "no new trace is written" in finding.description
        assert "7-day retention" in finding.description


async def test_an_unreadable_platform_row_reports_nothing_about_traces():
    findings: list[security_audit.AuditFinding] = []
    await security_audit._check_trace_payloads(findings, UNREADABLE_ROW)
    assert findings == []


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


async def test_the_report_carries_every_control_with_its_state(monkeypatch):
    async def _nothing(*_args, **_kwargs):
        return None

    for name, check in list(vars(security_audit).items()):
        if name.startswith("_check_") and callable(check):
            monkeypatch.setattr(security_audit, name, _nothing if name != "_check_proxy_trust" else lambda _f: None)

    rows = {
        security_audit.PlatformSettings: SimpleNamespace(xtm_mcp_enabled=True),
        security_audit.SSOConfig: _sso_row(oidc_enabled=True),
    }

    async def _loaded(_db, model):
        return rows[model]

    monkeypatch.setattr(security_audit, "_load_row", _loaded)
    report = await security_audit.run_security_audit(admin=SimpleNamespace(), db=SimpleNamespace())
    controls = {control.id: control for control in report.controls}
    assert controls["mcp.xtm_mcp_server"].state == "off"
    assert controls["authentication.sso_account_creation"].state == "off"
    assert sum(report.controls_summary.values()) == len(report.controls)
    assert set(report.controls_summary) == {"enforced", "monitoring", "off"}
    dumped = report.model_dump()
    assert {"controls", "controls_summary"} <= set(dumped)
    assert set(dumped["controls"][0]) == {
        "id",
        "category",
        "title",
        "setting",
        "stored_in",
        "where",
        "value",
        "default",
        "hardened",
        "state",
        "opt_in",
        "impact",
        "evidence",
        "pinned",
    }


async def test_a_row_that_cannot_be_read_leaves_its_controls_out_of_the_report(monkeypatch):
    async def _nothing(*_args, **_kwargs):
        return None

    for name, check in list(vars(security_audit).items()):
        if name.startswith("_check_") and callable(check):
            monkeypatch.setattr(security_audit, name, _nothing if name != "_check_proxy_trust" else lambda _f: None)
    # No session at all: both reads fail, and the audit still answers.
    report = await security_audit.run_security_audit(admin=SimpleNamespace(), db=SimpleNamespace())
    ids = {control.id for control in report.controls}
    assert "web.cors_origins" in ids
    assert "mcp.xtm_mcp_server" not in ids and "authentication.self_registration" not in ids


async def test_a_stored_row_is_read_in_a_savepoint():
    class _Savepoint:
        entered = exited = 0

        async def __aenter__(self):
            _Savepoint.entered += 1

        async def __aexit__(self, *_exc):
            _Savepoint.exited += 1
            return False

    row = object()

    class _Db:
        def begin_nested(self):
            return _Savepoint()

        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalar_one_or_none=lambda: row)

    assert await security_audit._load_row(_Db(), security_audit.SSOConfig) is row
    assert (_Savepoint.entered, _Savepoint.exited) == (1, 1)

    class _FailingDb(_Db):
        async def execute(self, *_args, **_kwargs):
            raise RuntimeError("column sso_config.saml_auto_create_users does not exist")

    assert await security_audit._load_row(_FailingDb(), security_audit.SSOConfig) is UNREADABLE_ROW
