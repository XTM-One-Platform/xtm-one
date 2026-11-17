"""The security controls registry and the contract every security change follows.

``app/core/security_controls.py`` registers every security switch once, and
the Security Audit reports each with its state. The tripwires below are what
keep that true as the platform grows: a switch-shaped setting that is neither
a control nor exempted, a finding outside the category taxonomy, a
``control=`` naming nothing, or a dashboard that labels other categories than
the backend reports, each fails the build with the line to add.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import importlib
import inspect
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import app.core.config as config_module
from app.api.v1 import security_audit
from app.core import security_controls
from app.core.config import Settings
from app.core.security_controls import (
    NOT_CONTROLS,
    SECURITY_CATEGORIES,
    SECURITY_CONTROLS,
    STORED_DEFAULTS,
    UNREADABLE_ROW,
    control_statuses,
    controls_summary,
    format_value,
    get_control,
)
from app.models.agent import Agent
from app.models.platform_settings import PlatformSettings
from app.models.sso_config import SSOConfig

_ID_SHAPE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

#: Settings fields shaped like a security switch.
SWITCH_PATTERN = re.compile(
    r"^disable_|_strict($|_)|^strict_|_require_|_enforc|_fail_mode$|^trust_|_trusted_(origins|proxies)$"
    r"|_insecure_|^allow_insecure|_dcr_enabled$|_extra_allowed_modules$|_node_permission$|_verification$"
    r"|^content_security_policy_|_accepted_audiences$"
)

#: Switches no pattern catches, some added by open pull requests: each is
#: checked once the field exists.
EXTRA_SWITCHES = frozenset(
    {
        "webhook_exact_event_matching",
        "prompt_guard_model_output",
        "tool_approvals_enabled",
        "tool_approvals_platform_default",
        "client_ip_strict",
        "rate_limit_ipv6_prefix",
        "integration_event_queue_enabled",
        "cors_origins",
        "custom_tool_sandbox",
        "min_password_length",
        "jwt_expire_minutes",
        "password_reset_expire_hours",
        "invitation_expire_hours",
        "runner_max_result_bytes",
        "debug",
    }
)

_STORED_MODELS = {"platform_settings": PlatformSettings, "sso_config": SSOConfig, "agents": Agent}

_FRONTEND_LABELS = Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "settings" / "security-audit.ts"


def _config_settings() -> set[str]:
    return {name for control in SECURITY_CONTROLS if control.stored_in == "config" for name in control.setting_names}


def _statuses(**rows) -> dict[str, security_controls.ControlStatus]:
    platform = rows.get("platform_settings")
    sso = rows.get("sso_config")
    agents = rows.get("agents")
    return {status.id: status for status in control_statuses(platform, sso, agents)}


def _sso_row(**overrides) -> SimpleNamespace:
    row = {column: default for (table, column), default in STORED_DEFAULTS.items() if table == "sso_config"}
    row["registration_enabled"] = False
    row.update(overrides)
    return SimpleNamespace(**row)


# ---------------------------------------------------------------------------
# (a) Every switch is a control, or says why it is not
# ---------------------------------------------------------------------------


def test_every_switch_shaped_setting_is_a_control_or_exempted():
    fields = Settings.model_fields
    switches = sorted(name for name in fields if SWITCH_PATTERN.search(name) or name in EXTRA_SWITCHES)
    # Guards the scan itself: a pattern that matched nothing would pass on an empty list.
    assert {"trust_proxy_headers", "disable_custom_tools", "debug"} <= set(switches)
    placed = _config_settings() | set(NOT_CONTROLS)
    unplaced = [name for name in switches if name not in placed]
    assert unplaced == [], "\n\n".join(
        f"Settings.{name} looks like a security switch but is neither a control nor exempted.\n"
        "Register it in SECURITY_CONTROLS (app/core/security_controls.py):\n"
        "    SecurityControl(\n"
        '        id="<category>.<name>",\n'
        '        title="<what is enforced>",\n'
        f'        setting="{name}",\n'
        '        hardened="<the enforcing value as an administrator types it>",\n'
        f"        enforced=<when {name.upper()} counts as enforced>,\n"
        '        impact="<what enforcing it changes for users>",\n'
        "    ),\n"
        "or, if it protects nothing, exempt it in NOT_CONTROLS:\n"
        f'    "{name}": "<why it is not a security control>",'
        for name in unplaced
    )


def test_an_exempted_setting_is_not_a_control_too():
    assert sorted(set(NOT_CONTROLS) & _config_settings()) == []
    assert all(reason.strip() for reason in NOT_CONTROLS.values())


# ---------------------------------------------------------------------------
# (b) Every control is well formed
# ---------------------------------------------------------------------------


def test_control_ids_are_namespaced_by_a_known_category_and_unique():
    ids = [control.id for control in SECURITY_CONTROLS]
    assert [cid for cid in ids if not _ID_SHAPE.match(cid)] == []
    assert sorted({control.category for control in SECURITY_CONTROLS} - set(SECURITY_CATEGORIES)) == []
    assert sorted(cid for cid in set(ids) if ids.count(cid) > 1) == []


def test_every_config_control_reads_a_real_setting():
    config_controls = [control for control in SECURITY_CONTROLS if control.stored_in == "config"]
    assert [c.id for c in config_controls if len(c.setting_names) != 1] == []
    assert [c.setting for c in config_controls if c.setting not in Settings.model_fields] == []
    assert [c.id for c in config_controls if c.where] == [], "a config control is changed through its variable"


def test_every_stored_control_reads_real_columns_and_says_where_to_change_it():
    stored = [control for control in SECURITY_CONTROLS if control.stored_in != "config"]
    assert stored, "the registry holds stored controls"
    for control in stored:
        columns = _STORED_MODELS[control.stored_in].__table__.columns
        assert [name for name in control.setting_names if name not in columns] == [], control.id
        assert [name for name in control.setting_names if (control.stored_in, name) not in STORED_DEFAULTS] == []
        # A per-agent switch is changed on each agent, the others in Settings.
        page = "Agents > " if control.stored_in == "agents" else "Settings > "
        assert control.where.startswith(page), control.id


def test_every_control_states_its_hardened_value_and_impact():
    for control in SECURITY_CONTROLS:
        assert control.title.strip() and control.hardened.strip() and control.impact.strip(), control.id
        assert control.impact.rstrip().endswith("."), control.id
        assert not control.evidence or control.evidence.rstrip().endswith("."), control.id


def test_no_control_is_reported_as_deprecated():
    # The audit reports the state of supported switches: phasing one out is an
    # UPGRADING.md announcement for a released setting, not a control's text.
    for control in SECURITY_CONTROLS:
        for text in (control.title, control.hardened, control.impact, control.evidence, control.where):
            assert not re.search(r"deprecat|obsolete|future major|removal", text, re.I), control.id


def test_stored_defaults_are_the_column_defaults():
    for (table, column), default in STORED_DEFAULTS.items():
        model_column = _STORED_MODELS[table].__table__.columns[column]
        if callable(default):
            continue  # resolved by mode, below
        if table == "agents":
            # Read as how many live agents have it on: none, the column being off by default.
            assert default == 0 and model_column.default.arg is False, (table, column)
            continue
        assert model_column.default is not None and model_column.default.arg == default, (table, column)


@pytest.mark.parametrize(
    ("mode", "expected"), [("xtm_one", False), ("ai_disabled", False), ("copilot", True), ("dev", True)]
)
async def test_the_mode_resolved_registration_default_is_the_one_sso_helpers_applies(monkeypatch, mode, expected):
    from app.core import sso_helpers

    monkeypatch.setattr(config_module.settings, "platform_mode", mode)

    class _NoRow:
        async def execute(self, *_args, **_kwargs):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    resolved = await sso_helpers.get_sso_config(_NoRow())
    assert resolved.registration_enabled is expected
    assert security_controls.stored_default("sso_config", "registration_enabled") is expected


# ---------------------------------------------------------------------------
# (c) Every finding speaks the taxonomy and names registered controls
# ---------------------------------------------------------------------------


def _finding_calls() -> list[ast.Call]:
    tree = ast.parse(inspect.getsource(security_audit))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AuditFinding"
    ]


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((keyword.value for keyword in call.keywords if keyword.arg == name), None)


def test_every_finding_category_is_in_the_taxonomy():
    calls = _finding_calls()
    assert len(calls) > 50, "the scan sees the findings the audit builds"
    outside = []
    for call in calls:
        category = _keyword(call, "category")
        if not (isinstance(category, ast.Constant) and category.value in SECURITY_CATEGORIES):
            outside.append((call.lineno, ast.unparse(category) if category is not None else None))
    assert outside == [], f"(line, category) outside SECURITY_CATEGORIES: {outside}"


def test_every_control_a_finding_names_is_registered_and_literal():
    named = []
    for call in _finding_calls():
        control = _keyword(call, "control")
        if control is None:
            continue
        assert isinstance(control, ast.Constant) and isinstance(control.value, str), (
            f"security_audit.py line {call.lineno}: control= must be a literal control id"
        )
        named.append((call.lineno, control.value))
    assert len(named) > 40
    registered = {control.id for control in SECURITY_CONTROLS}
    assert [(line, cid) for line, cid in named if cid not in registered] == []


def test_the_moved_findings_left_their_old_ids_behind():
    ids = {keyword.value for call in _finding_calls() if isinstance(keyword := _keyword(call, "id"), ast.Constant)}
    assert {
        "web.cors_wildcard",
        "network.proxy_trust_missing",
        "web.public_widget_remote_images",
        "access.global_mcp_key_restricted_reach",
    } <= ids
    assert not {"infrastructure.cors_wildcard", "infrastructure.proxy_trust_missing"} & ids
    assert not {"agents.public_widget_remote_images", "mcp.global_mcp_key_restricted_reach"} & ids


def _names_an_option(call: ast.Call) -> bool:
    return any(
        isinstance(inner, ast.Call)
        and isinstance(inner.func, ast.Name)
        and inner.func.id in {"option_reference", "where_to_set"}
        for inner in ast.walk(call)
    )


def test_a_finding_that_names_a_security_option_names_its_control():
    # An option is a control (below): a finding whose text sends the
    # administrator to one reports on a control, and the Controls view links
    # the two through control=.
    unnamed = [
        f"security_audit.py line {call.lineno}: {ast.unparse(_keyword(call, 'id'))} names a security option - "
        "add control="
        for call in _finding_calls()
        if _names_an_option(call) and _keyword(call, "control") is None
    ]
    assert unnamed == []


# ---------------------------------------------------------------------------
# (c') Each check reports under its category
# ---------------------------------------------------------------------------

#: Every function of security_audit.py that builds a finding -> the category
#: each of them belongs to. A check whose findings would span several
#: categories is one check per category.
EXPECTED_CATEGORY_BY_CHECK: dict[str, str] = {
    # authentication
    "_check_authentication": "authentication",
    "_check_sso": "authentication",
    "_check_self_registration": "authentication",
    "_check_mfa": "authentication",
    "_check_sessions": "authentication",
    "_check_account_lifetimes": "authentication",
    "_check_dormant_account_automations": "authentication",
    "_check_api_keys": "authentication",
    "_check_platform_registration": "authentication",
    "_check_registered_platforms": "authentication",
    "_check_platform_token_audiences": "authentication",
    # access
    "_check_agent_tool_admin_access": "access",
    "_check_ownerless_company_managed_assignments": "access",
    "_check_ownerless_evaluations": "access",
    "_check_global_mcp_key_reach": "access",
    "_check_trigger_integration_delivery": "access",
    "_check_kb_sync_source_integrations": "access",
    "_check_shared_personal_credentials": "access",
    # encryption
    "_check_encryption": "encryption",
    "_check_webhook_configs": "encryption",
    "_check_secret_vaults": "encryption",
    "_unreadable_webhook_configs_finding": "encryption",
    # network
    "_check_proxy_trust": "network",
    "_check_relays": "network",
    "_check_ipv6_rate_limit_key": "network",
    "_check_forwarded_client_addresses": "network",
    "_check_insecure_transport": "network",
    # web
    "_check_public_widget": "web",
    "_check_public_widget_uploads": "web",
    "_check_cors": "web",
    "_check_content_security_policy": "web",
    # integrations
    "_check_integrations": "integrations",
    "_check_oauth_token_dev_origins": "integrations",
    "_unusable_webhook_secrets_finding": "integrations",
    "_check_integration_scopes": "integrations",
    "_check_oauth_token_url_policy": "integrations",
    "_check_integration_notifications": "integrations",
    "_check_unscoped_admin_app_deliveries": "integrations",
    "_check_provider_webhook_verification": "integrations",
    # agents
    "_check_agents": "agents",
    "_check_agent_output_schemas": "agents",
    "_check_tool_result_guard": "agents",
    "_check_model_output_guard": "agents",
    "_check_prompt_guards": "agents",
    "_check_nova_semantic_calibration": "agents",
    "_check_tool_approvals": "agents",
    # assignments
    "_check_assignments": "assignments",
    "_check_event_matching": "assignments",
    # channels
    "_check_channel_bots": "channels",
    "_check_teams_graph_notifications": "channels",
    # tools
    "_check_runners": "tools",
    "_check_custom_tool_sandbox": "tools",
    "_check_sandbox_features": "tools",
    "_node_permission_finding": "tools",
    "_check_tool_call_budget": "tools",
    # mcp
    "_check_mcp": "mcp",
    # llm
    "_check_llm_providers": "llm",
    "_check_trace_payloads": "llm",
    # infrastructure
    "_check_infrastructure": "infrastructure",
    "_fail_mode_findings": "infrastructure",
}


def _check_functions() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    tree = ast.parse(inspect.getsource(security_audit))
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)}


def _builds_findings(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "AuditFinding"
        for call in ast.walk(function)
    )


def _miscategorized(functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]) -> list[str]:
    wrong = []
    for name, expected in EXPECTED_CATEGORY_BY_CHECK.items():
        function = functions.get(name)
        if function is None:
            continue  # test_every_check_is_mapped names it
        for call in ast.walk(function):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == "AuditFinding"):
                continue
            category = _keyword(call, "category")
            ident = _keyword(call, "id")
            written = category.value if isinstance(category, ast.Constant) else None
            finding_id = ident.value if isinstance(ident, ast.Constant) else None
            if written == expected and (finding_id is None or finding_id.startswith(f"{expected}.")):
                continue
            name_part = (finding_id or "").rpartition(".")[2] or "<name>"
            wrong.append(
                f"security_audit.py line {call.lineno} in {name}: category={written!r}, id={finding_id!r} - "
                f'{name} reports under {expected!r}: write category="{expected}" and id="{expected}.{name_part}"'
            )
    return wrong


def test_every_mapped_category_is_in_the_taxonomy():
    assert set(EXPECTED_CATEGORY_BY_CHECK.values()) <= set(SECURITY_CATEGORIES)


def test_each_check_reports_under_its_category():
    functions = _check_functions()
    assert "_check_proxy_trust" in functions, "the scan sees the audit's checks"
    wrong = _miscategorized(functions)
    assert wrong == [], "\n".join(wrong)


def test_every_check_is_mapped_to_its_category():
    functions = _check_functions()
    building = {name for name, function in functions.items() if _builds_findings(function)}
    unmapped = sorted(building - set(EXPECTED_CATEGORY_BY_CHECK))
    assert unmapped == [], "\n".join(
        f'security_audit.py {name} builds findings: add "{name}": "<its category>" to EXPECTED_CATEGORY_BY_CHECK'
        for name in unmapped
    )
    assert sorted(set(EXPECTED_CATEGORY_BY_CHECK) - building) == [], "a mapped check that builds no finding"


def test_a_miscategorized_check_is_named_with_the_category_and_id_to_write():
    source = (
        "def _check_proxy_trust(findings):\n"
        "    findings.append(AuditFinding(id='infrastructure.reverse_proxy_open', category='infrastructure',\n"
        "                                 severity='warning', title='t', description='d'))\n"
    )
    functions = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
    assert _miscategorized(functions) == [
        "security_audit.py line 2 in _check_proxy_trust: category='infrastructure', "
        "id='infrastructure.reverse_proxy_open' - _check_proxy_trust reports under 'network': "
        'write category="network" and id="network.reverse_proxy_open"'
    ]


# ---------------------------------------------------------------------------
# (d) The dashboard labels exactly these categories
# ---------------------------------------------------------------------------


def test_the_dashboard_labels_exactly_the_categories_in_order():
    source = _FRONTEND_LABELS.read_text(encoding="utf-8")
    match = re.search(r"export const CATEGORY_LABELS = \{(?P<body>.*?)\} as const", source, re.S)
    assert match, f"CATEGORY_LABELS not found in {_FRONTEND_LABELS}"
    keys = re.findall(r"^\s*([a-z][a-z0-9_]*)\s*:", match["body"], re.M)
    assert keys == list(SECURITY_CATEGORIES)


# ---------------------------------------------------------------------------
# (e) Every Settings > Access and security option is a control
# ---------------------------------------------------------------------------


def _security_options_or_skip() -> ModuleType:
    # Skipped only while the module is absent: once it exists, a renamed
    # SECURITY_OPTIONS or security_option fails here instead of skipping.
    try:
        return importlib.import_module("app.core.security_options")
    except ModuleNotFoundError as exc:
        if exc.name != "app.core.security_options":
            raise
        pytest.skip("app.core.security_options is not on this branch")


def test_every_security_option_is_a_control():
    options = _security_options_or_skip()
    missing = sorted(set(options.SECURITY_OPTIONS) - _config_settings())
    assert missing == [], f"security options with no control in SECURITY_CONTROLS: {missing}"


def test_every_security_option_is_grouped_under_its_control_s_category():
    # One taxonomy: Settings > Access and security groups its options by the categories
    # the audit reports its controls under.
    options = _security_options_or_skip()
    wrong = [
        f'SECURITY_OPTIONS["{key}"]: group {option.group!r} - write "{control.category}", '
        f"the category of {control.id}"
        for key, option in options.SECURITY_OPTIONS.items()
        for control in security_controls.controls_reading([key])
        if control.stored_in == "config" and option.group != control.category
    ]
    assert wrong == [], "\n".join(wrong)
    assert {option.group for option in options.SECURITY_OPTIONS.values()} <= set(SECURITY_CATEGORIES)


def test_every_security_option_is_reported_as_the_platform_resolves_it(monkeypatch):
    options = _security_options_or_skip()
    monkeypatch.setattr(options, "security_option", lambda key: ("effective", key))
    view = security_controls._LiveView({})
    assert {key: view.config(key) for key in options.SECURITY_OPTIONS} == {
        key: ("effective", key) for key in options.SECURITY_OPTIONS
    }


def test_the_live_view_reads_a_security_option_through_security_option(monkeypatch):
    # Runs on every branch: a stand-in for app.core.security_options,
    # whose options an administrator may set in Settings > Access and security.
    stand_in = ModuleType("app.core.security_options")
    stand_in.SECURITY_OPTIONS = {"webhook_require_secret": object()}
    stand_in.security_option = lambda key: f"{key} as resolved"
    monkeypatch.setitem(sys.modules, "app.core.security_options", stand_in)
    security_controls._security_options.cache_clear()
    try:
        view = security_controls._LiveView({})
        assert view.config("webhook_require_secret") == "webhook_require_secret as resolved"
        assert view.config("debug") == config_module.settings.debug
    finally:
        security_controls._security_options.cache_clear()


# ---------------------------------------------------------------------------
# The report: state, default, applies
# ---------------------------------------------------------------------------


def test_a_default_install_reports_every_config_control_and_no_sso_control():
    statuses = _statuses()
    assert {c.id for c in SECURITY_CONTROLS if c.stored_in == "config"} <= set(statuses)
    # No SSO configured: the SSO controls do not apply.
    for control_id in (
        "authentication.sso_auto_admin",
        "authentication.sso_account_creation",
        "authentication.local_sign_in",
    ):
        assert control_id not in statuses
    assert "authentication.self_registration" in statuses
    assert "mcp.xtm_mcp_server" in statuses
    assert [s.id for s in control_statuses(None, None)] == [c.id for c in SECURITY_CONTROLS if c.id in statuses]


def test_a_status_carries_what_the_dashboard_shows(monkeypatch):
    monkeypatch.setattr(config_module.settings, "min_password_length", 8)
    status = _statuses()["authentication.min_password_length"]
    assert dataclasses.asdict(status) == {
        "id": "authentication.min_password_length",
        "category": "authentication",
        "title": get_control("authentication.min_password_length").title,
        "setting": "MIN_PASSWORD_LENGTH",
        "stored_in": "config",
        "where": "",
        "value": "8",
        "default": "8",
        "hardened": ">= 12",
        "state": "off",
        "opt_in": True,
        "impact": get_control("authentication.min_password_length").impact,
        "evidence": "",
        "pinned": False,
    }
    monkeypatch.setattr(config_module.settings, "min_password_length", 14)
    assert _statuses()["authentication.min_password_length"].state == "enforced"


@pytest.mark.parametrize(
    ("setting", "value", "control_id", "state"),
    [
        ("public_widget_strict_rendering", True, "web.public_widget_strict_rendering", "enforced"),
        ("public_widget_strict_rendering", False, "web.public_widget_strict_rendering", "off"),
        ("trust_proxy_headers", True, "network.trust_proxy_headers", "off"),
        ("client_ip_strict", True, "network.client_ip_strict", "enforced"),
        ("client_ip_strict", False, "network.client_ip_strict", "off"),
        # Only a /64 or a wider network stops rotation within a /64.
        ("rate_limit_ipv6_prefix", 64, "network.rate_limit_ipv6_prefix", "enforced"),
        ("rate_limit_ipv6_prefix", 48, "network.rate_limit_ipv6_prefix", "enforced"),
        ("rate_limit_ipv6_prefix", 96, "network.rate_limit_ipv6_prefix", "off"),
        ("rate_limit_ipv6_prefix", 128, "network.rate_limit_ipv6_prefix", "off"),
        ("rate_limit_fail_mode", " Closed ", "infrastructure.rate_limit_fail_mode", "enforced"),
        ("rate_limit_fail_mode", "open", "infrastructure.rate_limit_fail_mode", "off"),
        ("jwt_expire_minutes", 1440, "authentication.session_lifetime", "enforced"),
        ("jwt_expire_minutes", 1441, "authentication.session_lifetime", "off"),
        ("session_idle_timeout_minutes", 0, "authentication.session_idle_timeout", "off"),
        ("session_idle_timeout_minutes", 30, "authentication.session_idle_timeout", "enforced"),
        ("session_idle_timeout_minutes", 31, "authentication.session_idle_timeout", "off"),
        ("password_reset_expire_hours", 1, "authentication.password_reset_expiry", "enforced"),
        ("invitation_expire_hours", 168, "authentication.invitation_expiry", "enforced"),
        ("invitation_expire_hours", 169, "authentication.invitation_expiry", "off"),
        ("mcp_dcr_enabled", False, "mcp.dynamic_client_registration", "enforced"),
        (
            "oauth_token_dev_trusted_origins",
            ["http://localhost:8080"],
            "integrations.oauth_token_dev_trusted_origins",
            "off",
        ),
        ("a2a_allow_insecure_loopback", True, "mcp.a2a_insecure_loopback", "off"),
        ("credential_transport_strict", True, "network.credential_transport_strict", "enforced"),
        ("credential_transport_strict", False, "network.credential_transport_strict", "off"),
        ("runner_max_result_bytes", 0, "tools.runner_result_bound", "off"),
        ("custom_tool_node_permission", False, "tools.custom_tool_node_permission", "off"),
        (
            "code_interpreter_extra_allowed_modules",
            "socket, http.client",
            "tools.code_interpreter_extra_modules",
            "off",
        ),
        ("custom_tool_extra_allowed_modules", " , ", "tools.custom_tool_extra_modules", "enforced"),
        ("cors_origins", ["https://xtm.example.com", "*"], "web.cors_origins", "off"),
        ("debug", True, "infrastructure.debug_mode", "off"),
    ],
)
def test_the_state_follows_the_setting(monkeypatch, setting, value, control_id, state):
    monkeypatch.setattr(config_module.settings, setting, value)
    assert _statuses()[control_id].state == state


@pytest.mark.parametrize(
    ("mode", "value", "state", "default", "opt_in", "pinned"),
    [
        # XTM One pins the quotas closed: whatever QUOTA_FAIL_MODE says,
        # enforced, by default, and nothing to change.
        ("xtm_one", "open", "enforced", "closed", False, True),
        ("ai_disabled", "open", "enforced", "closed", False, True),
        ("copilot", "open", "off", "open", True, False),
        ("copilot", "closed", "enforced", "open", True, False),
        ("dev", "open", "off", "open", True, False),
    ],
)
def test_the_quota_fail_mode_control_follows_the_platform_mode(monkeypatch, mode, value, state, default, opt_in, pinned):
    monkeypatch.setattr(config_module.settings, "platform_mode", mode)
    monkeypatch.setattr(config_module.settings, "quota_fail_mode", value)
    status = _statuses()["infrastructure.quota_fail_mode"]
    assert (status.state, status.default, status.opt_in, status.pinned) == (state, default, opt_in, pinned)
    assert status.value == ("closed" if pinned else value)


def test_a_tool_call_budget_overshoot_is_monitored_only_while_there_is_a_budget(monkeypatch):
    monkeypatch.setattr(config_module.settings, "agent_loop_max_tool_calls_strict", False)
    monkeypatch.setattr(config_module.settings, "agent_loop_max_tool_calls", 0)
    assert _statuses()["tools.tool_call_budget_strict"].state == "off"
    monkeypatch.setattr(config_module.settings, "agent_loop_max_tool_calls", 50)
    status = _statuses()["tools.tool_call_budget_strict"]
    assert (status.state, status.opt_in) == ("monitoring", True)
    assert "logged and recorded on the run's trace" in status.evidence
    monkeypatch.setattr(config_module.settings, "agent_loop_max_tool_calls_strict", True)
    assert _statuses()["tools.tool_call_budget_strict"].state == "enforced"


@pytest.mark.parametrize(
    ("mode", "disabled_by_default"), [("xtm_one", True), ("ai_disabled", True), ("copilot", False), ("dev", False)]
)
def test_the_default_of_a_mode_resolved_setting_is_the_mode_s(monkeypatch, mode, disabled_by_default):
    monkeypatch.setattr(config_module.settings, "platform_mode", mode)
    monkeypatch.setattr(config_module.settings, "disable_code_interpreter", False)
    status = _statuses()["tools.code_interpreter_disabled"]
    assert status.value == "false"
    assert status.default == format_value(disabled_by_default)
    assert status.opt_in is (not disabled_by_default)


def test_an_opt_in_control_is_one_whose_default_is_off():
    statuses = _statuses()
    assert statuses["web.public_widget_strict_rendering"].opt_in is True
    assert statuses["network.trust_proxy_headers"].opt_in is False
    # Both stay as earlier releases behaved unless an operator opts in.
    assert statuses["network.client_ip_strict"].opt_in is True
    assert statuses["network.rate_limit_ipv6_prefix"].opt_in is True
    assert statuses["tools.runner_result_bound"].opt_in is False
    assert statuses["mcp.xtm_mcp_server"].opt_in is False
    # A week of invitation lifetime is the bar and the default.
    assert (
        statuses["authentication.invitation_expiry"].state,
        statuses["authentication.invitation_expiry"].opt_in,
    ) == (
        "enforced",
        False,
    )


def test_a_list_value_names_the_first_entries_and_counts_the_others(monkeypatch):
    origins = [f"https://app{i}.example.com" for i in range(5)]
    monkeypatch.setattr(config_module.settings, "cors_origins", origins)
    status = _statuses()["web.cors_origins"]
    assert status.value == "https://app0.example.com, https://app1.example.com, https://app2.example.com and 2 more"
    assert format_value([]) == "empty" and format_value("") == "empty" and format_value(None) == "unset"
    assert format_value(frozenset({"socket", "http"})) == "http, socket"


# -- stored controls ----------------------------------------------------------------


def test_sso_controls_apply_with_sso_and_read_each_enabled_method(monkeypatch):
    monkeypatch.setattr(config_module.settings, "xtm_hub_admin_group", "")
    sso = _sso_row(oidc_enabled=True, saml_enabled=True, saml_auto_admin_new_users=True, oidc_auto_create_users=False)
    statuses = _statuses(sso_config=sso)
    admin = statuses["authentication.sso_auto_admin"]
    assert (admin.state, admin.value, admin.default, admin.opt_in) == (
        "off",
        "OIDC: false, SAML: true",
        "OIDC: false, SAML: false",
        False,
    )
    assert admin.setting == "oidc_auto_admin_new_users, saml_auto_admin_new_users"
    assert admin.stored_in == "sso_config" and admin.where.startswith("Settings > Access and security > Authentication")
    creation = statuses["authentication.sso_account_creation"]
    # On for SAML: off. By default on-the-fly creation is on, so enforcing it is an opt-in.
    assert (creation.state, creation.value, creation.opt_in) == ("off", "OIDC: false, SAML: true", True)
    assert statuses["authentication.local_sign_in"].state == "off"


def test_allowed_email_domains_enforce_sso_account_creation(monkeypatch):
    monkeypatch.setattr(config_module.settings, "xtm_hub_admin_group", "")
    sso = _sso_row(oidc_enabled=True, saml_enabled=True, allowed_domains=" Example.com, corp.io ")
    creation = _statuses(sso_config=sso)["authentication.sso_account_creation"]
    # On the fly for both methods, but only at those domains.
    assert (creation.state, creation.value, creation.default, creation.opt_in) == (
        "enforced",
        "OIDC: true, SAML: true; restricted to example.com, corp.io",
        "OIDC: true, SAML: true",
        True,
    )
    assert creation.setting == "oidc_auto_create_users, saml_auto_create_users, allowed_domains"
    # A list that names no domain restricts nothing, at sign-in as here.
    blank = _statuses(sso_config=_sso_row(oidc_enabled=True, allowed_domains=" , "))
    assert (
        blank["authentication.sso_account_creation"].state,
        blank["authentication.sso_account_creation"].value,
    ) == ("off", "OIDC: true")


def test_the_xtm_hub_decides_an_oidc_sign_in(monkeypatch):
    monkeypatch.setattr(config_module.settings, "xtm_hub_admin_group", "xtm-one-admins")
    sso = _sso_row(oidc_enabled=True, oidc_auto_admin_new_users=True, oidc_auto_create_users=True)
    statuses = _statuses(sso_config=sso)
    assert statuses["authentication.sso_auto_admin"].state == "enforced"
    assert statuses["authentication.sso_auto_admin"].value == "OIDC: true (XTM Hub gate)"
    assert statuses["authentication.sso_account_creation"].state == "enforced"


def test_self_registration_applies_only_with_local_sign_in():
    assert "authentication.self_registration" not in _statuses(sso_config=_sso_row(local_auth_enabled=False))
    open_to_domains = _statuses(sso_config=_sso_row(registration_enabled=True, allowed_domains="example.com"))
    status = open_to_domains["authentication.self_registration"]
    assert (status.state, status.value) == ("off", "true (restricted to example.com)")


@pytest.mark.parametrize(("mode", "state"), [("xtm_one", "enforced"), ("copilot", "off")])
def test_a_table_without_a_row_reads_the_defaults_of_the_mode(monkeypatch, mode, state):
    monkeypatch.setattr(config_module.settings, "platform_mode", mode)
    assert _statuses()["authentication.self_registration"].state == state


def test_the_xtm_one_mcp_server_reads_platform_settings():
    statuses = _statuses(platform_settings=SimpleNamespace(xtm_mcp_enabled=True))
    status = statuses["mcp.xtm_mcp_server"]
    assert (status.state, status.value, status.setting, status.where) == (
        "off",
        "true",
        "xtm_mcp_enabled",
        "Settings > Integrations > MCP Endpoint",
    )


def test_the_trace_payloads_control_reads_the_observability_switches():
    status = _statuses()["llm.trace_payloads"]
    # Payloads are stored by default: keeping them out is a choice to make.
    assert (status.state, status.value, status.default, status.opt_in, status.stored_in) == (
        "off",
        "true",
        "true",
        True,
        "platform_settings",
    )
    assert status.setting == "trace_store_payloads, trace_storage_enabled"
    assert status.where.startswith("Settings > Usage and quotas > Observability")
    payloads_off = _statuses(platform_settings=SimpleNamespace(trace_store_payloads=False))["llm.trace_payloads"]
    assert (payloads_off.state, payloads_off.value) == ("enforced", "false")
    storage_off = _statuses(platform_settings=SimpleNamespace(trace_storage_enabled=False))["llm.trace_payloads"]
    assert (storage_off.state, storage_off.value) == ("enforced", "trace storage off")


@pytest.mark.parametrize(("storage", "payloads"), [(True, True), (True, False), (False, True), (False, False)])
def test_the_trace_payloads_control_is_enforced_exactly_when_the_pipeline_keeps_no_payload(storage, payloads):
    from datetime import UTC, datetime
    from uuid import uuid4

    from app.services.trace_switches import TraceSwitches, apply_trace_switches
    from app.services.tracing.base import TraceData

    trace = TraceData(
        id=uuid4(), source="agent-chat", name="chat", started_at=datetime.now(UTC), input="the prompt", output="the answer"
    )
    kept = apply_trace_switches(trace, [], TraceSwitches(storage_enabled=storage, store_payloads=payloads))
    keeps_payload = kept is not None and (kept[0].input, kept[0].output) != (None, None)
    row = SimpleNamespace(trace_storage_enabled=storage, trace_store_payloads=payloads)
    assert (_statuses(platform_settings=row)["llm.trace_payloads"].state == "enforced") is not keeps_payload


def test_the_tool_approval_controls_read_the_platform_switches():
    statuses = _statuses()
    # The switch is on by default: enforced, and nothing to opt in to.
    switch = statuses["agents.tool_approvals"]
    assert (switch.state, switch.value, switch.opt_in, switch.stored_in) == (
        "enforced",
        "true",
        False,
        "platform_settings",
    )
    # Agents asking by default is opt-in on every install.
    default = statuses["agents.tool_approvals_default"]
    assert (default.state, default.value, default.opt_in, default.stored_in) == (
        "off",
        "false",
        True,
        "platform_settings",
    )
    for status in (switch, default):
        assert status.where.startswith("Settings > Access and security > Tool approvals"), status.id
    opted_in = _statuses(platform_settings=SimpleNamespace(tool_approvals_enabled=True, approve_tool_calls_default=True))
    assert (
        opted_in["agents.tool_approvals_default"].state,
        opted_in["agents.tool_approvals_default"].value,
    ) == ("enforced", "true")
    off = _statuses(platform_settings=SimpleNamespace(tool_approvals_enabled=False, approve_tool_calls_default=True))
    assert off["agents.tool_approvals"].state == "off"
    # Nothing pauses while approvals are off, whatever the default says.
    assert "agents.tool_approvals_default" not in off


class _ApprovalSwitchDb:
    """Answers the approval gate's reads of platform_settings with one value."""

    def __init__(self, value: object) -> None:
        self._value = value

    def begin_nested(self):
        return contextlib.nullcontext()

    async def scalar(self, _statement):
        return self._value


@pytest.mark.parametrize("stored", [True, False, None])
async def test_the_tool_approval_controls_are_enforced_exactly_when_the_gate_asks(stored):
    from app.services.hitl_whitelist import resolve_approve_tool_calls, resolve_gate_suppression

    row = SimpleNamespace(tool_approvals_enabled=stored, approve_tool_calls_default=stored)
    statuses = _statuses(platform_settings=row)
    gate_runs = await resolve_gate_suppression(_ApprovalSwitchDb(stored), None) is None
    assert (statuses["agents.tool_approvals"].state == "enforced") is gate_runs
    if gate_runs:
        # An agent left on Default (and the General Assistant) asks exactly when the control says so.
        asks = await resolve_approve_tool_calls(_ApprovalSwitchDb(stored), None)
        assert (statuses["agents.tool_approvals_default"].state == "enforced") is asks


def test_the_widget_uploads_control_counts_the_widgets_that_allow_them():
    status = _statuses()["web.public_widget_uploads"]
    # Off on every widget by default: enforced, and nothing to opt in to.
    assert (status.state, status.value, status.default, status.opt_in, status.stored_in) == (
        "enforced",
        "off on every widget",
        "off on every widget",
        False,
        "agents",
    )
    assert status.setting == "web_uploads_enabled" and status.where.startswith("Agents > ")
    allowed = _statuses(agents=SimpleNamespace(web_uploads_enabled=3))["web.public_widget_uploads"]
    assert (allowed.state, allowed.value) == ("off", "on for 3 widget(s)")


def test_a_row_the_audit_could_not_read_leaves_its_controls_out():
    statuses = _statuses(platform_settings=UNREADABLE_ROW, sso_config=UNREADABLE_ROW, agents=UNREADABLE_ROW)
    assert "mcp.xtm_mcp_server" not in statuses
    assert not [cid for cid in statuses if get_control(cid).stored_in != "config"]
    assert "web.cors_origins" in statuses


def test_the_summary_counts_every_state():
    statuses = control_statuses(None, None)
    summary = controls_summary(statuses)
    assert set(summary) == {"enforced", "monitoring", "off"}
    assert sum(summary.values()) == len(statuses)


# -- the custom tool sandbox is read the way the executor reads it ------------------


@pytest.mark.parametrize(
    ("configured", "legacy_docker", "expected"),
    [
        (None, "", "auto"),
        (None, "true", "docker"),
        ("auto", "true", "auto"),
        ("subprocess", "true", "subprocess"),
        ("opensandbox", "", "opensandbox"),
        (" Docker ", "", "docker"),
    ],
)
def test_the_sandbox_mode_mirrors_the_executor(monkeypatch, configured, legacy_docker, expected):
    from app.services import custom_tool_executor

    monkeypatch.delenv("CUSTOM_TOOL_SANDBOX", raising=False)
    monkeypatch.setenv("CUSTOM_TOOL_DOCKER", legacy_docker)
    fresh = Settings() if configured is None else Settings(custom_tool_sandbox=configured)
    monkeypatch.setattr(config_module, "settings", fresh)
    assert custom_tool_executor._resolve_sandbox_mode() == expected
    assert security_controls.configured_custom_tool_sandbox() == expected
    status = _statuses()["tools.custom_tool_sandbox"]
    assert status.state == ("enforced" if expected in ("docker", "opensandbox") else "off")
    assert status.default == "auto"
    if configured is None and legacy_docker:
        assert status.value == "docker (CUSTOM_TOOL_DOCKER)"
