"""Security options: environment variable first, then Settings > Access and security.

Pins the contract every moved option follows:

0. an option the platform mode pins takes that value whatever the variable or
   the stored value says, reads locked for that reason and refuses a write -
   in XTM One, quotas always fail closed;
1. the environment variable, when set (not blank), wins and locks the field;
   a write to a locked option is refused with 403;
2. otherwise the value stored in ``platform_settings.security_options``, read
   from the per-process snapshot;
3. otherwise today's default - no deployment changes behaviour on upgrade;

plus the call sites (each moved option switched by a stored value), the API,
the write path (validation, audit, publish after commit) and a tripwire: a
registered option read straight from ``settings`` anywhere in ``app/``
bypasses the UI value.
"""

from __future__ import annotations

import ast
import os
import pathlib
import re
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.core import config
from app.core import security_options as so
from app.core.security_options import (
    SECURITY_OPTIONS,
    SOURCE_DATABASE,
    SOURCE_DEFAULT,
    SOURCE_ENVIRONMENT,
    SOURCE_PLATFORM_MODE,
    security_option,
    security_option_source,
)
from tests import app_sources

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
FRONTEND_TABLE = APP_DIR.parents[1] / "frontend/src/pages/settings/security-options.ts"


class _Environment:
    """The process environment and the ``.env`` files, and ``settings`` built from them.

    The working directory is ``<tmp>/cwd``: ``Settings`` reads ``.env`` there
    and ``../.env`` in ``<tmp>``, both written by the test only.
    """

    def __init__(self, monkeypatch, root: pathlib.Path) -> None:
        self._monkeypatch = monkeypatch
        self.cwd = root / "cwd"
        self.cwd.mkdir()
        monkeypatch.chdir(self.cwd)
        # Case-insensitive, like ``Settings``: a developer's lower-case
        # variable must not leak into a test either.
        for name in list(os.environ):
            if name.lower() in SECURITY_OPTIONS:
                monkeypatch.delenv(name)
        self.rebuild()

    def rebuild(self) -> None:
        """Read the environment again, as the process does when it builds ``settings``.

        The options, the platform mode and the record are copied onto the one
        ``settings`` object every module holds, rather than replacing it: a
        module first imported during the test must not keep another object.
        """
        fresh = config.Settings()
        self._monkeypatch.setattr(config.settings, "_environment_fields", fresh.environment_fields)
        for key in ("platform_mode", *SECURITY_OPTIONS):
            self._monkeypatch.setattr(config.settings, key, getattr(fresh, key))

    def __call__(self, name: str, value: str) -> None:
        self._monkeypatch.setenv(name, value)
        self.rebuild()

    def dotenv(self, text: str, *, parent: bool = False) -> None:
        (self.cwd.parent if parent else self.cwd).joinpath(".env").write_text(text, encoding="utf-8")
        self.rebuild()


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Set the environment the options are resolved against, with no .env of the checkout."""
    return _Environment(monkeypatch, tmp_path)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_every_option_is_a_settings_field_named_like_its_variable():
    for key, option in SECURITY_OPTIONS.items():
        assert key in config.Settings.model_fields, key
        field = config.Settings.model_fields[key]
        # An alias would make pydantic-settings read another variable name.
        assert field.alias is None and field.validation_alias is None, key
        assert option.env_var == key.upper()


def test_defaults_are_exactly_todays_defaults():
    expected = {
        "trust_proxy_headers": False,
        "client_ip_strict": False,
        "credential_transport_strict": False,
        "rate_limit_ipv6_prefix": 128,
        "rate_limit_fail_mode": "open",
        "min_password_length": 8,
        "password_reset_expire_hours": 24,
        "invitation_expire_hours": 168,
        # A week from the sign-in, no idle timeout - nobody is signed out by the upgrade.
        "jwt_expire_minutes": 10080,
        "session_idle_timeout_minutes": 0,
        # No maximum, keys may live without an expiry as before.
        "api_key_max_lifetime_days": 0,
        "mcp_dcr_enabled": True,
        "mcp_cimd_enabled": True,
        "mcp_outbound_dcr_enabled": True,
        "oauth_token_url_strict": False,
        "oauth_token_dev_trusted_origins": (),
        # Only cloud metadata addresses are refused: every MCP server that worked keeps working.
        "mcp_server_url_policy": "permissive",
        "mcp_server_trusted_origins": (),
        "a2a_client_dev_trusted_origins": (),
        "a2a_allow_insecure_loopback": False,
        "webhook_require_secret": False,
        "webhook_exact_event_matching": False,
        "integration_notifications_require_verification": False,
        "public_widget_strict_rendering": False,
        "prompt_guard_block_public_widget": False,
        "prompt_guard_block_general_assistant": False,
        "prompt_guard_model_output": "off",
        "agent_tools_strict_admin_access": False,
        # A deck's managers may give it a public link.
        "presentation_public_links_enabled": True,
        # A video's managers may give it a public link.
        "video_public_links_enabled": True,
        # A dashboard's managers may publish it as a link: off until an admin turns it on.
        "dashboard_public_links_enabled": False,
        # Agents may schedule one-off actions.
        "scheduled_work_items_enabled": True,
        # Phone notifications show their title and text.
        "phone_notification_content": "full",
        # Each person chooses whether the companion asks for a PIN.
        "companion_pin_required": False,
        # Recordings reach public addresses only, like the browser tool.
        "browser_private_targets": False,
        # The system certificate store alone.
        "web_fetch_trusted_ca_certificates": "",
        # No tool result is screened, nothing is withheld.
        "prompt_guard_scanned_tools": (),
        "prompt_guard_block_tool_results": False,
        "runner_strict_path_confinement": False,
        "code_interpreter_extra_allowed_modules": frozenset(),
        "custom_tool_extra_allowed_modules": frozenset(),
        "custom_tool_node_permission": True,
    }
    for key, value in expected.items():
        assert SECURITY_OPTIONS[key].default() == value, key
    assert set(expected) | {
        "quota_fail_mode",
        "disable_code_interpreter",
        "disable_custom_tools",
        "disable_custom_variables",
    } == set(SECURITY_OPTIONS)


@pytest.mark.parametrize(
    "mode,disabled", [("xtm_one", True), ("ai_disabled", True), ("copilot", False), ("dev", False)]
)
def test_feature_flag_defaults_still_follow_the_platform_mode(monkeypatch, mode, disabled):
    monkeypatch.setattr(config.settings, "platform_mode", mode)
    for key in ("disable_code_interpreter", "disable_custom_tools", "disable_custom_variables"):
        assert SECURITY_OPTIONS[key].default() is disabled


@pytest.mark.parametrize(
    "mode,expected", [("xtm_one", "closed"), ("ai_disabled", "closed"), ("copilot", "open"), ("dev", "open")]
)
def test_the_quota_fail_mode_is_closed_in_xtm_one_and_open_by_default_elsewhere(monkeypatch, mode, expected):
    # XTM One sells quotas with its licence: its "default" is the pin.
    monkeypatch.setattr(config.settings, "platform_mode", mode)
    assert SECURITY_OPTIONS["quota_fail_mode"].default() == expected
    assert security_option("quota_fail_mode") == expected


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_nothing_stored_nothing_set_is_the_default(env):
    assert security_option("trust_proxy_headers") is False
    assert security_option_source("trust_proxy_headers") == SOURCE_DEFAULT


def test_a_stored_value_applies_when_the_variable_is_unset(env):
    so.publish_stored({"trust_proxy_headers": True})
    assert security_option("trust_proxy_headers") is True
    assert security_option_source("trust_proxy_headers") == SOURCE_DATABASE


def test_the_variable_wins_over_the_stored_value_and_locks(env):
    so.publish_stored({"trust_proxy_headers": True})
    env("TRUST_PROXY_HEADERS", "false")
    assert security_option("trust_proxy_headers") is False
    assert security_option_source("trust_proxy_headers") == SOURCE_ENVIRONMENT
    assert so.is_environment_locked("trust_proxy_headers")


def test_a_blank_variable_counts_as_unset(env):
    env("OAUTH_TOKEN_DEV_TRUSTED_ORIGINS", "   ")
    so.publish_stored({"oauth_token_dev_trusted_origins": ("http://localhost:8080",)})
    assert not so.is_environment_locked("oauth_token_dev_trusted_origins")
    assert security_option("oauth_token_dev_trusted_origins") == ("http://localhost:8080",)


def test_a_variable_in_a_dotenv_file_locks_like_one_in_the_process(env):
    env.dotenv("WEBHOOK_REQUIRE_SECRET=true\n")
    assert so.is_environment_locked("webhook_require_secret")
    assert security_option("webhook_require_secret") is True


def test_the_variable_is_matched_case_insensitively_like_settings(env):
    env("public_widget_strict_rendering", "true")
    assert so.is_environment_locked("public_widget_strict_rendering")


def _oracle_locked_keys() -> frozenset[str]:
    """What pydantic-settings' own env and dotenv sources set, for the registered options.

    The reference the lock must match: read independently of the record, the
    process environment winning over the files, a blank value unset.
    """
    from pydantic_settings import DotEnvSettingsSource, EnvSettingsSource

    values = {**DotEnvSettingsSource(config.Settings)(), **EnvSettingsSource(config.Settings)()}
    return frozenset(
        key
        for key in SECURITY_OPTIONS
        if values.get(key) is not None and not (isinstance(values[key], str) and not values[key].strip())
    )


@pytest.mark.parametrize(
    "process,dotenv,parent,locked",
    [
        pytest.param({}, "export WEBHOOK_REQUIRE_SECRET=true\n", None, {"webhook_require_secret"}, id="export"),
        pytest.param({}, 'MIN_PASSWORD_LENGTH="12" # policy\n', None, {"min_password_length"}, id="quotes-comment"),
        pytest.param({}, "# MIN_PASSWORD_LENGTH=12\n", None, set(), id="commented-out"),
        pytest.param({}, "min_password_length=12\n", None, {"min_password_length"}, id="lower-case-in-file"),
        pytest.param(
            {}, "", "RUNNER_STRICT_PATH_CONFINEMENT=true\n", {"runner_strict_path_confinement"}, id="parent-file"
        ),
        pytest.param(
            {},
            "CUSTOM_TOOL_EXTRA_ALLOWED_MODULES=requests\n",
            "CUSTOM_TOOL_EXTRA_ALLOWED_MODULES=\n",
            set(),
            id="parent-file-wins-blank",
        ),
        pytest.param(
            {"CUSTOM_TOOL_EXTRA_ALLOWED_MODULES": ""},
            "CUSTOM_TOOL_EXTRA_ALLOWED_MODULES=requests\n",
            None,
            set(),
            id="process-blank-wins",
        ),
        pytest.param({}, "WEBHOOK_REQUIRE_SECRET\n", None, set(), id="no-value"),
        pytest.param(
            {"TRUST_PROXY_HEADERS": "true"},
            "TRUST_PROXY_HEADERS=false\n",
            None,
            {"trust_proxy_headers"},
            id="process-and-file",
        ),
    ],
)
def test_the_lock_is_exactly_what_settings_read(env, monkeypatch, process, dotenv, parent, locked):
    for name, value in process.items():
        monkeypatch.setenv(name, value)
    if parent is not None:
        env.dotenv(parent, parent=True)
    env.dotenv(dotenv)
    assert so.environment_locked_keys() == locked
    assert so.environment_locked_keys() == _oracle_locked_keys()
    for key in locked:
        so.publish_stored({key: SECURITY_OPTIONS[key].default()})
        assert security_option_source(key) == SOURCE_ENVIRONMENT


def test_a_variable_that_appears_after_settings_was_built_does_not_lock(env, monkeypatch):
    # The startup's ``load_dotenv`` of the LLM provider seed puts the .env
    # files next to the backend into ``os.environ`` after ``settings`` was
    # built from the working directory: a variable ``Settings`` never read
    # must not lock the field, or its stored value would stop applying.
    so.publish_stored({"webhook_require_secret": True})
    monkeypatch.setenv("WEBHOOK_REQUIRE_SECRET", "false")
    assert not so.is_environment_locked("webhook_require_secret")
    assert security_option("webhook_require_secret") is True


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes are POSIX")
def test_a_dotenv_fifo_locks_like_a_file(env):
    # pydantic-settings reads a ``.env`` that is a named pipe (a secret
    # manager serving it); it can be read once, so the lock is what that read saw.
    fifo = env.cwd / ".env"
    os.mkfifo(fifo)

    def _serve() -> None:
        with open(fifo, "w", encoding="utf-8") as pipe:
            pipe.write("DISABLE_CUSTOM_TOOLS=true\n")

    writer = threading.Thread(target=_serve, daemon=True)
    writer.start()
    env.rebuild()
    writer.join(timeout=10)
    assert so.is_environment_locked("disable_custom_tools")
    assert security_option("disable_custom_tools") is True


def test_the_record_belongs_to_the_settings_it_was_read_for(env, monkeypatch):
    monkeypatch.setenv("MCP_DCR_ENABLED", "false")
    built = config.Settings()
    monkeypatch.delenv("MCP_DCR_ENABLED")
    other = config.Settings()  # another instance, read without the variable
    assert "mcp_dcr_enabled" in built.environment_fields
    assert "mcp_dcr_enabled" not in other.environment_fields
    assert config.Settings.model_construct().environment_fields == frozenset()


def test_an_invalid_stored_value_falls_back_to_the_default(env, caplog):
    values = so.normalize_stored({"min_password_length": "twelve", "unknown_option": 1, "quota_fail_mode": "CLOSED"})
    assert values == {"quota_fail_mode": "closed"}
    assert "min_password_length" in caplog.text


def test_an_invalid_stored_value_is_reported_once_not_on_every_poll(env, caplog):
    for _ in range(3):
        so.normalize_stored({"min_password_length": "twelve"})
    assert caplog.text.count("min_password_length") == 1
    so.normalize_stored({"min_password_length": "thirteen"})  # another value: said again
    assert caplog.text.count("min_password_length") == 2


def test_monkeypatched_settings_still_drive_the_value_when_nothing_is_stored(env, monkeypatch):
    # Hundreds of existing tests patch ``settings.<option>``: the resolver must
    # read them exactly as the call sites used to.
    monkeypatch.setattr(config.settings, "rate_limit_fail_mode", " Closed ")
    assert security_option("rate_limit_fail_mode") == "closed"


# ---------------------------------------------------------------------------
# The platform mode's pin: in XTM One, quotas always fail closed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["xtm_one", "ai_disabled"])
def test_xtm_one_pins_quotas_closed_over_the_variable_and_the_stored_value(env, mode):
    from app.services.quota import quota_fail_closed

    env("PLATFORM_MODE", mode)
    env("QUOTA_FAIL_MODE", "open")
    so.publish_stored({"quota_fail_mode": "open"})
    assert security_option("quota_fail_mode") == "closed"
    assert quota_fail_closed() is True
    assert security_option_source("quota_fail_mode") == SOURCE_PLATFORM_MODE
    assert so.is_pinned("quota_fail_mode") and so.is_locked("quota_fail_mode")
    assert so.lock_reason("quota_fail_mode") == SOURCE_PLATFORM_MODE
    # The variable decides nothing, so it locks nothing: not "env-managed".
    assert not so.is_environment_locked("quota_fail_mode")
    assert "quota_fail_mode" not in so.environment_locked_keys()
    # Ignored, not deleted: another mode finds it.
    assert so.stored_values()["quota_fail_mode"] == "open"
    assert so.ignored_environment_value("quota_fail_mode") == "open"


@pytest.mark.parametrize("mode", ["copilot", "dev"])
def test_copilot_and_dev_keep_the_quota_fail_mode_choice(env, mode):
    from app.services.quota import quota_fail_closed

    env("PLATFORM_MODE", mode)
    assert not so.is_pinned("quota_fail_mode")
    assert security_option("quota_fail_mode") == "open"
    assert security_option_source("quota_fail_mode") == SOURCE_DEFAULT
    so.publish_stored({"quota_fail_mode": "closed"})
    assert quota_fail_closed() is True
    assert security_option_source("quota_fail_mode") == SOURCE_DATABASE
    env("QUOTA_FAIL_MODE", "open")
    assert quota_fail_closed() is False
    assert security_option_source("quota_fail_mode") == SOURCE_ENVIRONMENT
    assert so.lock_reason("quota_fail_mode") == SOURCE_ENVIRONMENT
    assert so.ignored_environment_value("quota_fail_mode") is None


def test_a_variable_the_pin_overrides_is_warned_about_once(env, caplog):
    env("PLATFORM_MODE", "xtm_one")
    env("QUOTA_FAIL_MODE", "open")
    with caplog.at_level("WARNING", logger="app.core.security_options"):
        so.log_ignored_variables()
        so.log_ignored_variables()
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and record.name == "app.core.security_options"
    ]
    assert warnings == [
        "QUOTA_FAIL_MODE=open has no effect: in XTM One, agentic work is refused while the quota "
        "store is unreachable, because quotas are part of the licence. Remove it from the environment."
    ]


@pytest.mark.parametrize(
    "mode,variable",
    [("xtm_one", "closed"), ("xtm_one", None), ("copilot", "open"), ("dev", "closed")],
)
def test_nothing_is_warned_when_the_variable_is_not_overridden(env, caplog, mode, variable):
    env("PLATFORM_MODE", mode)
    if variable is not None:
        env("QUOTA_FAIL_MODE", variable)
    with caplog.at_level("WARNING", logger="app.core.security_options"):
        so.log_ignored_variables()
    assert not [
        record
        for record in caplog.records
        if record.levelname == "WARNING" and record.name == "app.core.security_options"
    ]


def test_both_processes_report_the_ignored_variables_at_startup():
    # The web process and the worker both enforce quotas.
    for module in ("main.py", "worker.py"):
        assert "log_ignored_variables()" in (APP_DIR / module).read_text(encoding="utf-8"), module


def test_every_pin_says_why_and_forces_a_value_the_option_accepts(monkeypatch):
    pinned = [option for option in SECURITY_OPTIONS.values() if option.pinned is not None]
    assert [option.key for option in pinned] == ["quota_fail_mode"]  # guards the scan
    reasons = _frontend_table("SECURITY_OPTION_PINNED_REASONS")
    for option in pinned:
        assert option.pinned_reason and option.pinned_reason[0].islower(), option.key
        assert reasons.get(option.key), f"{option.key} has no reason in SECURITY_OPTION_PINNED_REASONS"
        for mode in ("xtm_one", "ai_disabled", "copilot", "dev"):
            monkeypatch.setattr(config.settings, "platform_mode", mode)
            value = option.pinned_value()
            if value is not None:
                assert option.normalize(option.to_wire(value)) == value, (option.key, mode)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value,expected",
    [
        ("trust_proxy_headers", True, True),
        ("client_ip_strict", True, True),
        ("rate_limit_ipv6_prefix", 64, 64),
        ("rate_limit_ipv6_prefix", 32, 32),
        ("rate_limit_fail_mode", " Closed ", "closed"),
        ("min_password_length", 12, 12),
        ("session_idle_timeout_minutes", 0, 0),  # off: the one value under the minimum
        ("session_idle_timeout_minutes", 5, 5),
        ("oauth_token_dev_trusted_origins", ["HTTP://LocalHost:8080/x"], ("http://localhost:8080",)),
        ("custom_tool_extra_allowed_modules", ["urllib.request", "requests"], frozenset({"urllib", "requests"})),
    ],
)
def test_valid_values_are_normalized(key, value, expected):
    assert SECURITY_OPTIONS[key].normalize(value) == expected


@pytest.mark.parametrize(
    "key,value",
    [
        ("trust_proxy_headers", "true"),  # never coerced
        ("trust_proxy_headers", 1),
        ("client_ip_strict", "true"),
        # The bounds the variable is held to at startup.
        ("rate_limit_ipv6_prefix", 31),
        ("rate_limit_ipv6_prefix", 129),
        ("rate_limit_ipv6_prefix", "64"),
        ("rate_limit_fail_mode", "strict"),
        ("min_password_length", 0),
        ("min_password_length", 1025),
        ("min_password_length", True),
        ("invitation_expire_hours", 721),
        # Never a lifetime that signs everyone out before it can be fixed.
        ("jwt_expire_minutes", 4),
        ("jwt_expire_minutes", 525_601),
        ("session_idle_timeout_minutes", -1),
        # Shorter than the web app takes to say a session is in use.
        ("session_idle_timeout_minutes", 1),
        ("session_idle_timeout_minutes", 4),
        ("session_idle_timeout_minutes", 525_601),
        ("session_idle_timeout_minutes", "30"),
        ("oauth_token_dev_trusted_origins", ["localhost:8080"]),  # no scheme, as at startup
        ("oauth_token_dev_trusted_origins", ["http://localhost:99999"]),  # a port that is none
        ("oauth_token_dev_trusted_origins", ["http://" + "a" * so.MAX_LIST_ENTRY_CHARS]),
        ("oauth_token_dev_trusted_origins", [f"http://h{i}" for i in range(so.MAX_LIST_ENTRIES + 1)]),
        ("custom_tool_extra_allowed_modules", ["not a module"]),
        ("custom_tool_extra_allowed_modules", [1]),
        ("custom_tool_extra_allowed_modules", {"requests": True}),
    ],
)
def test_invalid_values_are_refused(key, value):
    with pytest.raises(ValueError):
        SECURITY_OPTIONS[key].normalize(value)


@pytest.mark.parametrize("key", ["code_interpreter_extra_allowed_modules", "custom_tool_extra_allowed_modules"])
def test_a_sandbox_escape_module_can_never_be_re_enabled(key):
    from app.services._sandbox_utils import ALWAYS_BLOCKED_MODULES

    # The sandboxes drop them from the variable's list at use; typed in the
    # UI they are refused, never stored as a re-enabled module that is not.
    for module in sorted(ALWAYS_BLOCKED_MODULES):
        with pytest.raises(ValueError, match=module):
            SECURITY_OPTIONS[key].normalize(["requests", f"{module}.sub"])
    assert SECURITY_OPTIONS[key].normalize(["socket", "http.client"]) == frozenset({"socket", "http"})


# ---------------------------------------------------------------------------
# The write path and the API
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value


class _Session:
    def __init__(self, row):
        self.row = row
        self.flushed = self.committed = False

    async def execute(self, _statement):
        return _Result(self.row)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


@pytest.fixture
def audit(monkeypatch):
    entries: list[dict] = []

    async def _log_audit(_db, **kwargs):
        entries.append(kwargs)

    async def _get_platform_settings(_db):
        return None

    monkeypatch.setattr("app.services.audit.log_audit", _log_audit)
    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _get_platform_settings)
    return entries


@pytest.mark.asyncio
async def test_a_write_merges_audits_and_keeps_unknown_keys(env, audit):
    from app.services.security_options import update_security_options

    row = SimpleNamespace(security_options={"trust_proxy_headers": True, "future_option": 1})
    db = _Session(row)
    stored = await update_security_options(
        db, {"rate_limit_fail_mode": "closed", "trust_proxy_headers": None}, user=SimpleNamespace()
    )
    assert stored == {"rate_limit_fail_mode": "closed"}
    assert row.security_options == {"future_option": 1, "rate_limit_fail_mode": "closed"}
    assert db.flushed
    assert {entry["details"]["setting"] for entry in audit} == {"rate_limit_fail_mode", "trust_proxy_headers"}
    assert all(entry["entity_type"] == "security" for entry in audit)


@pytest.mark.asyncio
async def test_an_unchanged_value_writes_and_audits_nothing(env, audit):
    from app.services.security_options import update_security_options

    db = _Session(SimpleNamespace(security_options={"min_password_length": 12}))
    await update_security_options(db, {"min_password_length": 12}, user=SimpleNamespace())
    assert not db.flushed and audit == []


@pytest.mark.asyncio
async def test_a_write_to_a_locked_option_is_refused_with_403(env, audit):
    from app.api.v1.schemas.settings import SecurityOptionsUpdateRequest
    from app.api.v1.settings import update_security_options_endpoint

    env("MCP_DCR_ENABLED", "false")
    with pytest.raises(HTTPException) as refused:
        await update_security_options_endpoint(
            body=SecurityOptionsUpdateRequest(values={"mcp_dcr_enabled": True}),
            request=SimpleNamespace(),
            user=SimpleNamespace(),
            db=SimpleNamespace(),  # never touched: refused before any read
        )
    assert refused.value.status_code == 403
    assert "MCP_DCR_ENABLED" in refused.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["open", "closed", None])
async def test_a_write_to_a_pinned_option_is_refused_with_403_and_the_reason(env, audit, value):
    from app.api.v1.schemas.settings import SecurityOptionsUpdateRequest
    from app.api.v1.settings import update_security_options_endpoint

    env("PLATFORM_MODE", "xtm_one")
    with pytest.raises(HTTPException) as refused:
        await update_security_options_endpoint(
            body=SecurityOptionsUpdateRequest(values={"quota_fail_mode": value, "trust_proxy_headers": True}),
            request=SimpleNamespace(),
            user=SimpleNamespace(),
            db=SimpleNamespace(),  # never touched: refused before any read, the stored value kept
        )
    assert refused.value.status_code == 403
    assert refused.value.detail == (
        '"Quotas when their store is unreachable" (quota_fail_mode) is read-only: in XTM One, agentic work '
        "is refused while the quota store is unreachable, because quotas are part of the licence."
    )
    assert audit == []


@pytest.mark.asyncio
async def test_copilot_mode_stores_the_quota_fail_mode(env, audit):
    from app.services.security_options import update_security_options

    env("PLATFORM_MODE", "copilot")
    row = SimpleNamespace(security_options=None)
    stored = await update_security_options(_Session(row), {"quota_fail_mode": "closed"}, user=SimpleNamespace())
    assert stored == {"quota_fail_mode": "closed"}
    assert row.security_options == {"quota_fail_mode": "closed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("values", [{"no_such_option": True}, {"min_password_length": -1}])
async def test_an_unknown_option_or_an_invalid_value_is_a_422(env, audit, values):
    from app.api.v1.schemas.settings import SecurityOptionsUpdateRequest
    from app.api.v1.settings import update_security_options_endpoint

    with pytest.raises(HTTPException) as refused:
        await update_security_options_endpoint(
            body=SecurityOptionsUpdateRequest(values=values),
            request=SimpleNamespace(),
            user=SimpleNamespace(),
            db=SimpleNamespace(),
        )
    assert refused.value.status_code == 422


@pytest.mark.asyncio
async def test_the_put_commits_then_refreshes_this_process(env, audit):
    from app.api.v1.schemas.settings import SecurityOptionsUpdateRequest
    from app.api.v1.settings import update_security_options_endpoint

    db = _Session(SimpleNamespace(security_options=None))
    response = await update_security_options_endpoint(
        body=SecurityOptionsUpdateRequest(values={"webhook_require_secret": True}),
        request=SimpleNamespace(),
        user=SimpleNamespace(),
        db=db,
    )
    assert db.committed
    assert security_option("webhook_require_secret") is True
    item = next(o for o in response.options if o.key == "webhook_require_secret")
    assert (item.value, item.source, item.locked) == (True, SOURCE_DATABASE, False)


@pytest.mark.asyncio
async def test_the_get_reports_the_environment_managed_list(env, monkeypatch):
    from app.api.v1.settings import get_security_options

    async def _refresh():
        return False  # the database is unreachable: the snapshot is shown

    monkeypatch.setattr("app.services.security_options.refresh_security_options_cache", _refresh)
    env("RUNNER_STRICT_PATH_CONFINEMENT", "true")
    response = await get_security_options(user=SimpleNamespace())
    assert response.env_managed_fields == ["runner_strict_path_confinement"]
    item = next(o for o in response.options if o.key == "runner_strict_path_confinement")
    assert (item.value, item.source, item.locked, item.env_var) == (
        True,
        SOURCE_ENVIRONMENT,
        True,
        "RUNNER_STRICT_PATH_CONFINEMENT",
    )
    assert item.locked_reason == SOURCE_ENVIRONMENT
    assert next(o for o in response.options if o.key == "trust_proxy_headers").locked_reason is None


@pytest.mark.asyncio
async def test_the_get_shows_the_pinned_quota_fail_mode_locked_by_the_platform_mode(env, monkeypatch):
    from app.api.v1.settings import get_security_options

    async def _refresh():
        return False

    monkeypatch.setattr("app.services.security_options.refresh_security_options_cache", _refresh)
    env("PLATFORM_MODE", "xtm_one")
    env("QUOTA_FAIL_MODE", "open")
    so.publish_stored({"quota_fail_mode": "open"})
    response = await get_security_options(user=SimpleNamespace())
    item = next(o for o in response.options if o.key == "quota_fail_mode")
    assert (item.value, item.default, item.stored, item.source, item.locked, item.locked_reason) == (
        "closed",
        "closed",
        "open",
        SOURCE_PLATFORM_MODE,
        True,
        SOURCE_PLATFORM_MODE,
    )
    assert "quota_fail_mode" not in response.env_managed_fields


@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_previous_snapshot(env, monkeypatch):
    from app.services import security_options as service

    so.publish_stored({"rate_limit_fail_mode": "closed"})

    class _Broken:
        async def __aenter__(self):
            raise OSError("database down")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.core.database.async_session", lambda: _Broken())
    assert await service.refresh_security_options_cache() is False
    assert security_option("rate_limit_fail_mode") == "closed"


class _NoSession:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_a_poll_that_read_before_a_write_never_puts_the_old_values_back(env, monkeypatch):
    import asyncio

    from app.services import security_options as service

    started, release = asyncio.Event(), asyncio.Event()
    rows = [{"webhook_require_secret": False}]

    async def _read(_db):
        values = rows[0]  # what the row held when this read began
        started.set()
        await release.wait()
        return values

    async def _read_switches(_db):
        from app.services.trace_switches import DEFAULT_TRACE_SWITCHES

        return DEFAULT_TRACE_SWITCHES

    monkeypatch.setattr("app.core.database.async_session", lambda: _NoSession())
    monkeypatch.setattr(service, "read_stored_security_options", _read)
    monkeypatch.setattr(service, "read_trace_switches", _read_switches)
    poll = asyncio.create_task(service.refresh_security_options_cache())
    await started.wait()
    # An administrator's write commits and publishes while that read is out.
    rows[0] = {"webhook_require_secret": True}
    assert service.publish_security_options({"webhook_require_secret": True}) is True
    release.set()
    assert await poll is True
    assert security_option("webhook_require_secret") is True
    # A poll that starts after the write publishes what it reads.
    assert await service.refresh_security_options_cache() is True
    assert security_option("webhook_require_secret") is True


@pytest.mark.asyncio
async def test_the_refresh_loop_survives_an_error_and_stops_on_cancel(monkeypatch, caplog):
    import asyncio

    from app.services import security_options as service

    calls = []

    async def _refresh():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("a programming error")
        return True

    monkeypatch.setattr(service, "refresh_security_options_cache", _refresh)
    task = asyncio.create_task(service.run_security_options_refresh_loop(interval=0.01))
    while len(calls) < 3:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert caplog.text.count("Security options refresh failed") == 1


def test_a_process_that_never_read_the_database_uses_the_environment_and_default(env):
    so.reset_snapshot()
    assert not so.snapshot_loaded()
    assert security_option("mcp_dcr_enabled") is True


# ---------------------------------------------------------------------------
# The call sites: unchanged by default, switched by the stored value
# ---------------------------------------------------------------------------


def _request(headers=None, client="10.0.0.1"):
    return SimpleNamespace(headers=headers or {}, client=SimpleNamespace(host=client))


def test_trusted_proxy_headers_switch_the_client_ip(env):
    from app.core.utils import request_client_ip

    request = _request({"x-forwarded-for": "203.0.113.9"})
    assert request_client_ip(request) == "10.0.0.1"
    so.publish_stored({"trust_proxy_headers": True})
    assert request_client_ip(request) == "203.0.113.9"


def test_trusted_proxy_headers_switch_the_scheme_the_platform_believes(env):
    from app.core.utils import external_base, is_secure_transport

    request = SimpleNamespace(
        headers={"x-forwarded-proto": "https"},
        client=SimpleNamespace(host="10.0.0.1"),
        base_url="http://xtm.example.test/",
        url=SimpleNamespace(scheme="http"),
    )
    assert external_base(request) == "http://xtm.example.test"
    assert is_secure_transport(request) is False
    so.publish_stored({"trust_proxy_headers": True})
    assert external_base(request) == "https://xtm.example.test"
    assert is_secure_transport(request) is True


def test_trusted_proxy_headers_switch_the_access_log_client(env):
    from app.main import _forwarded_for

    scope = {"headers": [(b"x-forwarded-for", b"203.0.113.9, 10.0.0.2")]}
    assert _forwarded_for(scope) is None
    so.publish_stored({"trust_proxy_headers": True})
    assert _forwarded_for(scope) == "203.0.113.9"


def test_trust_proxy_headers_switched_on_in_the_ui_is_warned_about(env, monkeypatch, caplog):
    # Its variable is reported at boot (core/proxy_headers.py); the stored
    # value when it is published, once until it is switched off again.
    from app.services import security_options as service

    monkeypatch.setattr(service, "_warned", {})
    with caplog.at_level("WARNING", logger="app.services.security_options"):
        service.publish_security_options({"trust_proxy_headers": True})
        service.publish_security_options({"trust_proxy_headers": True})
    warnings = [r.getMessage() for r in caplog.records if r.name == "app.services.security_options"]
    assert len(warnings) == 1
    assert warnings[0].startswith('"Trust proxy headers" is on (Settings > Access and security). TRUST_PROXY_HEADERS takes')
    assert "deprecat" not in warnings[0].lower()
    caplog.clear()
    env("TRUST_PROXY_HEADERS", "false")  # the variable wins: the stored value applies nowhere
    monkeypatch.setattr(service, "_warned", {})
    with caplog.at_level("WARNING", logger="app.services.security_options"):
        service.publish_security_options({"trust_proxy_headers": True})
    assert [r for r in caplog.records if r.name == "app.services.security_options"] == []


def test_canonical_client_addresses_switch_the_client_ip(env):
    from app.core.utils import request_client_ip

    request = _request(client="::ffff:10.0.0.1")
    assert request_client_ip(request) == "::ffff:10.0.0.1"
    so.publish_stored({"client_ip_strict": True})
    assert request_client_ip(request) == "10.0.0.1"


def test_the_ipv6_prefix_switches_the_rate_limit_bucket(env):
    from app.core.rate_limit import client_bucket

    assert client_bucket("2001:db8:1:2::a") == "2001:db8:1:2::a"
    so.publish_stored({"rate_limit_ipv6_prefix": 64})
    assert client_bucket("2001:db8:1:2::a") == "2001:db8:1:2::/64"
    env("RATE_LIMIT_IPV6_PREFIX", "128")  # the variable wins over the stored value
    assert client_bucket("2001:db8:1:2::a") == "2001:db8:1:2::a"


@pytest.mark.asyncio
async def test_the_security_audit_reads_the_effective_ipv6_prefix(env):
    from app.api.v1 import security_audit

    findings: list = []
    await security_audit._check_ipv6_rate_limit_key(findings)
    assert [f.id for f in findings] == ["network.ipv6_rate_limit_per_address"]
    assert findings[0].recommendation.startswith(
        'Set "IPv6 rate-limit prefix length" in Settings > Access and security > Security options to 64'
    )
    so.publish_stored({"rate_limit_ipv6_prefix": 64})
    findings.clear()
    await security_audit._check_ipv6_rate_limit_key(findings)
    assert [(f.id, f.severity) for f in findings] == [("network.ipv6_rate_limit_per_prefix", "pass")]


@pytest.mark.asyncio
async def test_the_password_rules_the_login_page_reads_follow_the_stored_length(env, monkeypatch):
    from app.api.v1 import auth

    cfg = SimpleNamespace(
        local_auth_enabled=True,
        registration_enabled=False,
        allowed_domain_list=[],
        oidc_enabled=False,
        oidc_display_name="",
        saml_enabled=False,
        saml_display_name="",
    )

    async def _get_sso_config(_db):
        return cfg

    monkeypatch.setattr("app.core.sso_helpers.get_sso_config", _get_sso_config)
    assert (await auth.auth_config(db=None)).min_password_length == 8
    so.publish_stored({"min_password_length": 14})
    assert (await auth.auth_config(db=None)).min_password_length == 14


@pytest.mark.asyncio
async def test_link_lifetimes_follow_the_stored_hours(env, monkeypatch):
    from app.api.v1 import auth
    from app.services import email

    minted: list[int] = []

    async def _create_password_reset(*, user_id, created_by_id, ttl_hours, email):
        minted.append(ttl_hours)
        return "token", None

    async def _send_email(**_kwargs):
        return True

    monkeypatch.setattr(auth.token_store, "create_password_reset", _create_password_reset)
    monkeypatch.setattr(email, "is_email_configured", lambda: True)
    monkeypatch.setattr(email, "send_email", _send_email)
    await auth._dispatch_password_reset(user_id=None, email="a@example.test")
    so.publish_stored({"password_reset_expire_hours": 2, "invitation_expire_hours": 3})
    await auth._dispatch_password_reset(user_id=None, email="a@example.test")
    assert minted == [24, 2]
    assert security_option("invitation_expire_hours") == 3


@pytest.mark.asyncio
async def test_invitations_default_to_the_stored_lifetime(env, monkeypatch):
    from app.api.v1 import invitations

    minted: list[int] = []

    class _MintedError(Exception):
        pass

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(scalar_one_or_none=lambda: None)

    async def _no_pending(_email):
        return None

    async def _create_invitation(**kwargs):
        minted.append(kwargs["ttl_hours"])
        raise _MintedError

    monkeypatch.setattr(invitations.token_store, "find_invitation_by_email", _no_pending)
    monkeypatch.setattr(invitations.token_store, "create_invitation", _create_invitation)
    body = SimpleNamespace(role="user", expires_in_hours=None, email="a@example.test", send_email=False)
    admin = SimpleNamespace(id=None)
    for stored in (None, 3):
        if stored is not None:
            so.publish_stored({"invitation_expire_hours": stored})
        with pytest.raises(_MintedError):
            await invitations.create_invitation(body=body, request=SimpleNamespace(), admin=admin, db=_Db())
    body.expires_in_hours = 48  # a lifetime the caller sends still wins
    with pytest.raises(_MintedError):
        await invitations.create_invitation(body=body, request=SimpleNamespace(), admin=admin, db=_Db())
    assert minted == [168, 3, 48]


@pytest.mark.asyncio
async def test_mcp_client_registration_follows_the_stored_switch(env):
    from app.api.v1.mcp_oauth import register_client

    so.publish_stored({"mcp_dcr_enabled": False})
    with pytest.raises(HTTPException) as refused:
        await register_client(request=SimpleNamespace(), body=SimpleNamespace(), db=None)
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_outbound_mcp_registration_follows_the_stored_switch(env, monkeypatch):
    from app.services import mcp_oauth_discovery

    async def _discover(**_kwargs):
        return SimpleNamespace(
            authorize_url="https://idp.example.test/authorize",
            token_url="https://idp.example.test/token",
            registration_url="https://idp.example.test/register",
            resource=None,
            scopes=None,
        )

    async def _allowed(_url):
        return None

    monkeypatch.setattr(mcp_oauth_discovery, "discover_oauth_endpoints", _discover)
    monkeypatch.setattr("app.integrations.token_endpoint.assert_token_endpoint_allowed", _allowed)
    srv = SimpleNamespace(
        client_id_enc=None,
        authorize_url=None,
        token_url=None,
        registration_url=None,
        oauth_resource=None,
        scopes=None,
        url="https://mcp.example.test",
        oauth_metadata_url=None,
    )
    so.publish_stored({"mcp_outbound_dcr_enabled": False})
    with pytest.raises(mcp_oauth_discovery.MCPOAuthDiscoveryError, match="disabled on this platform"):
        await mcp_oauth_discovery.ensure_mcp_oauth_client(None, srv)


def test_webhook_secrets_become_required_with_the_stored_switch(env):
    from app.api.v1.webhooks import _verify_assignment_webhook_secret

    request = SimpleNamespace(headers={})
    _verify_assignment_webhook_secret(b"{}", request, None, assignment_id="a1")  # allowed, as before
    so.publish_stored({"webhook_require_secret": True})
    with pytest.raises(HTTPException) as refused:
        _verify_assignment_webhook_secret(b"{}", request, None, assignment_id="a1")
    assert refused.value.status_code == 401


def test_every_trigger_matches_exactly_with_the_stored_switch(env):
    from app.api.v1.webhooks import _assignment_matcher, _matcher_admits
    from app.models.assignment import uses_exact_event_matching

    legacy = {"provider": "github", "event": "pull_request"}

    def _runs_on_a_sibling() -> bool:
        matcher = _assignment_matcher("github", "a1", "g1", "integration_event", legacy)
        return _matcher_admits(matcher, "github", "pull_request_review.submitted")

    assert not uses_exact_event_matching(legacy) and _runs_on_a_sibling()  # as before
    so.publish_stored({"webhook_exact_event_matching": True})
    assert uses_exact_event_matching(legacy) and not _runs_on_a_sibling()
    env("WEBHOOK_EXACT_EVENT_MATCHING", "false")
    assert not uses_exact_event_matching(legacy) and _runs_on_a_sibling()


def test_notification_verification_follows_the_stored_switch(env):
    from app.services.integration_notification_auth import (
        SlackSigningSecrets,
        admin_app_scoping_required,
        check_slack_delivery,
    )

    unsigned = (SlackSigningSecrets(), b"{}", "", "")
    assert check_slack_delivery(*unsigned).compatible  # accepted, as before
    so.publish_stored({"integration_notifications_require_verification": True})
    assert admin_app_scoping_required() is True
    assert not check_slack_delivery(*unsigned).accepted
    env("INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION", "false")  # the variable wins
    assert admin_app_scoping_required() is False
    assert check_slack_delivery(*unsigned).compatible


def test_teams_channel_notifications_follow_the_same_stored_switch(env):
    from app.channels import teams

    legacy = {"changeType": "created", "subscriptionId": "sub-x", "clientState": "copilot_team-1"}
    assert teams._notification_verdict(None, legacy, "team-1") == ("unverified", "no_record")
    assert teams.verification_required() is False  # acted on, as before
    so.publish_stored({"integration_notifications_require_verification": True})
    assert teams.verification_required() is True
    env("INTEGRATION_NOTIFICATIONS_REQUIRE_VERIFICATION", "false")  # the variable wins
    assert teams.verification_required() is False


@pytest.mark.asyncio
async def test_the_widget_renders_strictly_with_the_stored_switch(env, monkeypatch):
    import json

    from app.api.v1 import public_chat

    class _Session:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    async def _resolve(_token, _db):
        return SimpleNamespace(name="Agent", description="", icon=None, web_suggested_prompts=[])

    monkeypatch.setattr(public_chat, "async_session", lambda: _Session())
    monkeypatch.setattr(public_chat, "_resolve_agent", _resolve)

    async def _strict() -> bool:
        return json.loads((await public_chat.get_public_config("token")).body)["strict_rendering"]

    assert await _strict() is False
    so.publish_stored({"public_widget_strict_rendering": True})
    assert await _strict() is True


@pytest.mark.asyncio
async def test_the_feature_switches_follow_the_stored_values(env):
    from app.api.v1.chat import _upload_offers_code_interpreter
    from app.api.v1.variables import _require_custom_variables_enabled
    from app.services.code_interpreter import execute_python

    so.publish_stored({"disable_code_interpreter": True, "disable_custom_variables": True})
    with pytest.raises(RuntimeError, match="disabled"):
        await execute_python("print(1)")
    assert await _upload_offers_code_interpreter(None, SimpleNamespace(), SimpleNamespace()) is False
    with pytest.raises(HTTPException) as refused:
        _require_custom_variables_enabled(SimpleNamespace())
    assert refused.value.status_code == 403
    so.publish_stored({"disable_custom_variables": False})
    _require_custom_variables_enabled(SimpleNamespace())


def test_admins_agent_tools_follow_the_stored_strict_access(env):
    from app.api.v1.helpers import agent_tool_admin_bypass

    admin = SimpleNamespace(is_admin=True)
    assert agent_tool_admin_bypass(admin) is True
    so.publish_stored({"agent_tools_strict_admin_access": True})
    assert agent_tool_admin_bypass(admin) is False
    assert agent_tool_admin_bypass(SimpleNamespace(is_admin=False)) is False


def test_the_security_audit_reads_the_effective_proxy_trust(env, monkeypatch):
    from app.api.v1 import security_audit

    monkeypatch.setattr(security_audit.settings, "debug", False)
    monkeypatch.setattr(security_audit.settings, "base_url", "https://xtm.example.test")
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    findings: list = []
    security_audit._check_proxy_trust(findings)
    assert [f.title for f in findings] == ["No reverse proxy is trusted to confirm HTTPS"]
    so.publish_stored({"trust_proxy_headers": True})
    findings.clear()
    security_audit._check_proxy_trust(findings)
    assert [f.id for f in findings] == ["network.forwarded_headers_trusted"]
    assert '"Trust proxy headers" (TRUST_PROXY_HEADERS) is on' in findings[0].description
    assert 'turn off "Trust proxy headers" in Settings > Access and security > Security options' in findings[0].recommendation


def test_fail_modes_switch(env):
    from app.core.rate_limit import _rate_limit_fail_closed
    from app.services.quota import quota_fail_closed

    # Copilot mode: XTM One pins the quotas closed.
    env("PLATFORM_MODE", "copilot")
    assert not _rate_limit_fail_closed() and not quota_fail_closed()
    so.publish_stored({"rate_limit_fail_mode": "closed", "quota_fail_mode": "closed"})
    assert _rate_limit_fail_closed() and quota_fail_closed()


def test_password_policy_switches(env):
    from app.core.security import check_password_policy

    check_password_policy("abc12345")
    so.publish_stored({"min_password_length": 12})
    with pytest.raises(ValueError, match="at least 12"):
        check_password_policy("abc12345")


def test_runner_confinement_switches(env):
    from app.models.runner import PERMISSION_MODE_FULL_CONTROL
    from app.services.runners import STRICT_CONFINEMENT_KEY, delivery_payload

    mode = "restricted" if PERMISSION_MODE_FULL_CONTROL != "restricted" else "custom"
    runner = SimpleNamespace(permission_mode=mode, allowed_paths=["/data"])
    assert STRICT_CONFINEMENT_KEY not in delivery_payload(runner, "read_file", {"path": "/data/a"})
    so.publish_stored({"runner_strict_path_confinement": True})
    assert delivery_payload(runner, "read_file", {"path": "/data/a"})[STRICT_CONFINEMENT_KEY] is True


@pytest.mark.asyncio
async def test_oauth_token_url_strict_switch(env):
    from app.integrations import token_endpoint

    # The default accepts a loopback token URL, as it did before the check existed.
    await token_endpoint.assert_token_endpoint_allowed("http://127.0.0.1:8080/token")
    so.publish_stored({"oauth_token_url_strict": True})
    with pytest.raises(token_endpoint.TokenEndpointRefusedError, match="loopback address"):
        await token_endpoint.assert_token_endpoint_allowed("http://127.0.0.1:8080/token")
    # The variable wins over the stored value.
    env("OAUTH_TOKEN_URL_STRICT", "false")
    await token_endpoint.assert_token_endpoint_allowed("http://127.0.0.1:8080/token")


@pytest.mark.asyncio
async def test_oauth_token_dev_origins_switch(env, monkeypatch):
    from app.integrations import token_endpoint

    so.publish_stored({"oauth_token_url_strict": True})
    with pytest.raises(token_endpoint.TokenEndpointRefusedError):
        await token_endpoint.assert_token_endpoint_allowed("http://127.0.0.1:8080/token")
    so.publish_stored({"oauth_token_url_strict": True, "oauth_token_dev_trusted_origins": ("http://127.0.0.1:8080",)})
    await token_endpoint.assert_token_endpoint_allowed("http://127.0.0.1:8080/token")


def test_a2a_dev_origins_switch(env):
    from app.a2a.client.destinations import DestinationRefusedError, assert_allowed

    with pytest.raises(DestinationRefusedError):
        assert_allowed("http://localhost:8200/card.json", resolve=False)
    so.publish_stored({"a2a_client_dev_trusted_origins": ("http://localhost:8200",)})
    assert assert_allowed("http://localhost:8200/card.json", resolve=False) == "http://localhost:8200"
    # Still an allow-list of origins: another port of the same host stays refused.
    with pytest.raises(DestinationRefusedError):
        assert_allowed("http://localhost:8201/card.json", resolve=False)


def test_a2a_dev_origins_are_validated_like_the_variable(env):
    option = SECURITY_OPTIONS["a2a_client_dev_trusted_origins"]
    assert option.normalize(["HTTP://LocalHost:8200/x"]) == ("http://localhost:8200",)
    with pytest.raises(ValueError, match="A2A_CLIENT_DEV_TRUSTED_ORIGINS"):
        option.normalize(["localhost:8200"])


def test_a2a_loopback_exemption_switches(env, monkeypatch):
    from app.core.utils import is_secure_transport

    monkeypatch.setattr(config.settings, "debug", False)
    request = SimpleNamespace(
        url=SimpleNamespace(scheme="http", path="/a2a"), client=SimpleNamespace(host="127.0.0.1"), headers={}, scope={}
    )
    assert is_secure_transport(request) is False
    so.publish_stored({"a2a_allow_insecure_loopback": True})
    assert is_secure_transport(request) is True
    # The real socket peer only: a forwarded request is never exempt.
    forwarded = SimpleNamespace(**{**vars(request), "headers": {"x-forwarded-for": "127.0.0.1"}})
    assert is_secure_transport(forwarded) is False


def test_the_strict_credential_transport_switches_without_a_restart(env, monkeypatch):
    from app.api.deps import refuses_credential_transport

    monkeypatch.setattr(config.settings, "debug", False)
    request = SimpleNamespace(
        url=SimpleNamespace(scheme="http", path="/api/v1/runners/enroll"),
        client=SimpleNamespace(host="10.0.0.5"),
        headers={},
        scope={},
    )
    # Served by default.
    assert refuses_credential_transport(request) is False
    so.publish_stored({"credential_transport_strict": True})
    assert refuses_credential_transport(request) is True
    # The variable wins over the stored value, and locks the field.
    env("CREDENTIAL_TRANSPORT_STRICT", "false")
    assert refuses_credential_transport(request) is False
    assert so.is_environment_locked("credential_transport_strict")


def test_an_a2a_origins_relaxation_saved_in_the_ui_is_logged_at_warning(env, monkeypatch, caplog):
    from app.services import security_options as service

    monkeypatch.setattr(service, "_warned", {})
    with caplog.at_level("WARNING", logger="app.services.security_options"):
        service.publish_security_options({"a2a_client_dev_trusted_origins": ("http://localhost:8200",)})
    assert "A2A outbound destination guard relaxed for http://localhost:8200" in caplog.text


def test_sandbox_modules_and_node_permission_switch(env):
    from app.services.code_interpreter import _resolve_allowed_modules
    from app.services.custom_tool_executor import _custom_tools_disabled, _resolve_custom_tool_allowed_modules

    assert _resolve_allowed_modules() == frozenset()
    assert _resolve_custom_tool_allowed_modules() == frozenset()
    so.publish_stored(
        {
            "code_interpreter_extra_allowed_modules": frozenset({"socket"}),
            "custom_tool_extra_allowed_modules": frozenset({"requests"}),
            "disable_custom_tools": not SECURITY_OPTIONS["disable_custom_tools"].default(),
        }
    )
    assert _resolve_allowed_modules() == frozenset({"socket"})
    assert _resolve_custom_tool_allowed_modules() == frozenset({"requests"})
    assert _custom_tools_disabled() is (not SECURITY_OPTIONS["disable_custom_tools"].default())


@pytest.mark.asyncio
async def test_node_permission_switches(env, monkeypatch):
    from app.services import custom_tool_executor

    monkeypatch.setattr(custom_tool_executor, "_node_permission_flags", ("--permission",))
    assert await custom_tool_executor._resolve_node_permission_flags() == ("--permission",)
    so.publish_stored({"custom_tool_node_permission": False})
    assert await custom_tool_executor._resolve_node_permission_flags() == ()


def test_audit_recommendation_names_the_field_or_only_the_locked_variable(env):
    text = so.where_to_set("webhook_require_secret", "true")
    assert text.startswith('turn on "Require webhook secrets" in Settings > Access and security > Security options')
    assert "WEBHOOK_REQUIRE_SECRET=true and restart" in text
    assert so.where_to_set("webhook_require_secret", "true", sentence_start=True).startswith("Turn on ")
    env("WEBHOOK_REQUIRE_SECRET", "false")
    assert so.where_to_set("webhook_require_secret", "true").startswith("set WEBHOOK_REQUIRE_SECRET=true and restart")


def test_audit_recommendation_says_how_each_kind_of_option_is_set(env):
    path = "in Settings > Access and security > Security options"
    assert so.where_to_set("mcp_dcr_enabled", "false").startswith(f'turn off "MCP client self-registration" {path}')
    assert so.where_to_set("rate_limit_fail_mode", "closed").startswith(
        f'set "Rate limits when Redis is unreachable" {path} to closed (or set RATE_LIMIT_FAIL_MODE=closed'
    )
    assert so.where_to_set("min_password_length", "12").startswith(f'set "Minimum password length" {path} to 12')
    # A blank variable counts as unset: only the field empties a list.
    emptied = so.where_to_set("oauth_token_dev_trusted_origins", "")
    assert emptied.startswith('empty the "Loopback token URL origins (development)" field')
    assert "OAUTH_TOKEN_DEV_TRUSTED_ORIGINS=" not in emptied
    env("OAUTH_TOKEN_DEV_TRUSTED_ORIGINS", "http://localhost:8080")
    assert so.where_to_set("oauth_token_dev_trusted_origins", "").startswith(
        "unset OAUTH_TOKEN_DEV_TRUSTED_ORIGINS and restart"
    )


def test_a_finding_names_an_option_by_its_field_and_its_variable():
    assert so.option_reference("quota_fail_mode") == '"Quotas when their store is unreachable" (QUOTA_FAIL_MODE)'


# ---------------------------------------------------------------------------
# Tripwires
# ---------------------------------------------------------------------------

#: Reads of the environment value on purpose: the boot warning says what the
#: variable holds (the stored value warns from the service when it is published).
_ALLOWED_DIRECT_READS = {
    ("main.py", "oauth_token_dev_trusted_origins"),
    # Whether that variable's list has any effect, said in the same boot log.
    ("main.py", "oauth_token_url_strict"),
    ("main.py", "a2a_client_dev_trusted_origins"),
}

#: ``Settings`` properties that parse an option's ENVIRONMENT value only: read
#: anywhere else they would bypass the value stored in Settings > Access and security.
_ENVIRONMENT_ONLY_PROPERTIES = {
    "code_interpreter_allowed_modules": "code_interpreter_extra_allowed_modules",
    "custom_tool_allowed_modules": "custom_tool_extra_allowed_modules",
}


def _direct_reads(source: str, rel: str, tree: ast.Module | None = None) -> list[str]:
    """Every attribute or ``getattr`` spelling of a registered option in *source*.

    Whatever the object is called (``settings``, ``app_settings``, ``_cfg``,
    a local alias, ``get_settings()``): the names are distinctive enough that
    every such read is one, and the few that are on purpose are listed above.
    """
    names = set(SECURITY_OPTIONS) | set(_ENVIRONMENT_ONLY_PROPERTIES)
    found = []
    for node in app_sources.walk(ast.parse(source) if tree is None else tree):
        if isinstance(node, ast.Attribute) and node.attr in names:
            name = node.attr
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"getattr", "hasattr", "setattr"}
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in names
        ):
            name = node.args[1].value
        else:
            continue
        if (rel, name) not in _ALLOWED_DIRECT_READS:
            found.append(f"{rel}:{node.lineno} {ast.unparse(node)}")
    return found


def test_no_registered_option_is_read_straight_from_settings():
    offenders = []
    for path in app_sources.python_files(APP_DIR):
        rel = path.relative_to(APP_DIR).as_posix()
        if rel not in {"core/config.py", "core/security_options.py"}:
            offenders += _direct_reads(app_sources.source(path), rel, app_sources.tree(path))
    assert offenders == [], "read these through security_option():\n" + "\n".join(offenders)


def test_the_tripwire_catches_the_spellings_it_claims():
    source = (
        "cfg.trust_proxy_headers\n"
        "get_settings().quota_fail_mode\n"
        "getattr(settings, 'webhook_require_secret', False)\n"
        "settings.custom_tool_allowed_modules\n"
        "security_option('trust_proxy_headers')\n"
    )
    assert len(_direct_reads(source, "x.py")) == 4
    assert _direct_reads("settings.oauth_token_dev_trusted_origins", "main.py") == []


DOCS_CONFIGURATION = APP_DIR.parents[1] / "docs/docs/deployment/configuration.md"


def _frontend_table(name: str) -> dict[str, str]:
    """``key: "text"`` entries of a table of ``security-options.ts``, one line or wrapped."""
    source = FRONTEND_TABLE.read_text(encoding="utf-8")
    body = re.search(rf"export const {name} = \{{(.*?)\n\}} as const;", source, re.S)
    assert body, f"{name} not found in {FRONTEND_TABLE.name}"
    return dict(re.findall(r'^\s*(\w+):\s*"((?:[^"\\]|\\.)*)"', body.group(1), re.M))


def test_the_card_the_audit_and_the_docs_name_each_option_alike():
    # A Security Audit recommendation names the field (``where_to_set``) with
    # the registry label, the card with its own table, the docs with theirs:
    # one name, or an administrator looks for a field that is not there.
    labels = _frontend_table("SECURITY_OPTION_LABELS")
    assert set(labels) == set(SECURITY_OPTIONS)
    docs = DOCS_CONFIGURATION.read_text(encoding="utf-8")
    for key, option in SECURITY_OPTIONS.items():
        assert labels[key] == option.label, key
        assert f"| {option.label} | `{option.env_var}` |" in docs, key


def test_every_registered_option_has_a_description_and_a_known_group():
    # The card's sections are the audit's categories, which the frontend
    # labels (CATEGORY_LABELS, held to SECURITY_CATEGORIES by test_security_controls.py).
    from app.core.security_controls import SECURITY_CATEGORIES

    descriptions = _frontend_table("SECURITY_OPTION_DESCRIPTIONS")
    for key, option in SECURITY_OPTIONS.items():
        assert descriptions.get(key), key
        assert option.group in SECURITY_CATEGORIES, key


def test_no_option_is_offered_as_deprecated():
    # The card offers supported switches only: one being phased out is
    # announced in UPGRADING.md for a released option, never shipped as a
    # field whose own description says it is going away.
    descriptions = _frontend_table("SECURITY_OPTION_DESCRIPTIONS")
    for key, option in SECURITY_OPTIONS.items():
        for text in (option.label, option.pinned_reason, descriptions.get(key, "")):
            assert not re.search(r"deprecat|obsolete|future major|removal", text, re.I), key


def test_the_frontend_labels_every_registered_option():
    table = FRONTEND_TABLE.read_text(encoding="utf-8")
    for key in SECURITY_OPTIONS:
        assert re.search(rf"^\s*{key}:", table, re.M), key


def test_every_finding_an_option_links_to_is_one_the_audit_reports():
    # The card links an option to the audit findings it decides by their id:
    # an id the audit no longer emits would be a link to nothing.
    table = FRONTEND_TABLE.read_text(encoding="utf-8")
    block = re.search(r"SECURITY_OPTION_FINDINGS[^=]*= \{(.*?)\n\};", table, re.S).group(1)
    linked = dict(re.findall(r"^\s*(\w+): \[([^\]]*)\]", block, re.M))
    audit = (APP_DIR / "api/v1/security_audit.py").read_text(encoding="utf-8")
    emitted = set(re.findall(r'\bid="([a-z_]+\.[a-z_0-9]+)"', audit))
    assert linked
    for key, ids in linked.items():
        assert key in SECURITY_OPTIONS, key
        for finding_id in re.findall(r'"([^"]+)"', ids):
            assert finding_id in emitted, f"{key} links to {finding_id}, which the audit does not report"


def _recommending_links() -> dict[str, str]:
    """The ``recommends`` entries of ``SECURITY_OPTION_FINDINGS``: finding id -> option key."""
    table = FRONTEND_TABLE.read_text(encoding="utf-8")
    block = re.search(r"SECURITY_OPTION_FINDINGS[^=]*= \{(.*?)\n\};", table, re.S).group(1)
    links: dict[str, str] = {}
    for key, entries in re.findall(r"^\s*(\w+): \[([^\]]*)\]", block, re.M):
        for finding_id in re.findall(r'\{\s*id:\s*"([^"]+)",\s*recommends:\s*true\s*\}', entries):
            links[finding_id] = key
    return links


def _findings_naming_an_option() -> dict[str, str]:
    """Each ``AuditFinding(...)`` whose text calls ``where_to_set``: its id -> the option key."""
    named: dict[str, str] = {}
    audit = (APP_DIR / "api/v1/security_audit.py").read_text(encoding="utf-8")
    for call in ast.walk(ast.parse(audit)):
        if not (isinstance(call, ast.Call) and getattr(call.func, "id", "") == "AuditFinding"):
            continue
        finding_id = next(
            (kw.value.value for kw in call.keywords if kw.arg == "id" and isinstance(kw.value, ast.Constant)), None
        )
        keys = {
            inner.args[0].value
            for inner in ast.walk(call)
            if isinstance(inner, ast.Call)
            and getattr(inner.func, "id", "") == "where_to_set"
            and inner.args
            and isinstance(inner.args[0], ast.Constant)
        }
        if keys:
            assert finding_id and len(keys) == 1, (finding_id, keys)
            named[finding_id] = keys.pop()
    return named


def test_every_finding_that_names_an_option_links_back_to_that_option():
    # The audit links a finding to the field its recommendation names (a
    # ``recommends`` entry of the card's one table). A recommendation reworded
    # to name an option without that mark would lose its link silently; a
    # mark on a finding that names none, or another option, would send the
    # administrator to the wrong field.
    named = _findings_naming_an_option()
    assert len(named) >= 5  # guards the scan itself
    assert _recommending_links() == named
    assert set(named.values()) <= set(SECURITY_OPTIONS)


def test_the_migration_adds_the_column_the_model_declares():
    from app.models.platform_settings import PlatformSettings

    assert "security_options" in PlatformSettings.__table__.columns
    migration = next((APP_DIR.parent / "alembic/versions").glob("sec4580opts01_*.py")).read_text(encoding="utf-8")
    assert "terminate_lock_holders" in migration and "set_lock_timeout" in migration
    assert "ADD COLUMN IF NOT EXISTS" in migration
