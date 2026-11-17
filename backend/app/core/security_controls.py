# SPDX-License-Identifier: Apache-2.0

"""Security controls: every security switch of the platform and whether it is enforced.

The Security Audit (``GET /api/v1/security-audit``, Settings > Access and security)
reports two things. FINDINGS are what its checks observed
(``app/api/v1/security_audit.py``). CONTROLS are the switches that make the
platform safer when set, each with its state:

* ``enforced`` - set to the value that protects;
* ``monitoring`` - observes and records what it would block without blocking
  it (a tool-call overshoot that is logged and traced, a policy in
  report-only mode);
* ``off`` - neither.

The contract every security change follows:

1. **One control per switch.** A ``Settings`` field (its environment variable
   is the field name in upper case), a stored ``platform_settings`` /
   ``sso_config`` column, or an ``agents`` column switched per agent (read as
   how many live agents have it on) is registered once, in
   :data:`SECURITY_CONTROLS`.
2. **A finding about a control names it**: ``AuditFinding(control="<id>")``.
3. **Ids are literal and stable**, ``<category>.<name>``, for findings and
   controls alike: a reader keys on them, never on a title.
4. **One taxonomy**, :data:`SECURITY_CATEGORIES`, for every control and every
   finding. The dashboard labels exactly these keys (``CATEGORY_LABELS`` in
   ``frontend/src/pages/settings/security-audit.ts``), and Settings > Access and security
   groups its options by them: an option's group is its control's category.
5. **One severity rubric** for findings: ``critical`` = exploitable now (a
   secret everyone knows, a dependency failure that switches protections
   off); ``warning`` = a protection is off or misconfigured while what it
   protects is in use, or a setting silently weakens one; ``info`` = off with
   nothing it protects in use, a monitoring mode, or context; ``pass`` =
   enforced, or nothing to fix. "In use" is something the audit sees the
   protection would refuse or change right now (an embedded widget, a
   secretless webhook, a token URL on loopback, a credential issued over
   plain HTTP, the answers a connected guard does not evaluate), never the
   mere existence of the switch. While in use, a protection that is off is a
   warning, one that only monitors info, one enforced a pass.
6. **No silent pass**: a check that can warn also reports its pass when what
   it protects is in use, so the report shows what is enforced.

Adding a switch: the ``Settings`` field (or the column), then a
:class:`SecurityControl` below - its id, the setting, the value that enforces
it as an administrator types it, what enforcing it changes for users and
where what it would block is recorded - and ``control=`` on every finding the
audit reports about it. A switch-shaped field that protects nothing goes to
:data:`NOT_CONTROLS` with its reason. ``tests/test_security_controls.py``
fails on a switch in neither, and gives the entry to add.

Import-light on purpose (``app.core.config``, ``app.core.email_domains`` and
the standard library only): the audit, the settings API and the tests all read
it.
"""

from __future__ import annotations

import functools
import importlib
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType, ModuleType
from typing import Any, Literal, Protocol

import app.core.config as _config
from app.core.config import Settings
from app.core.email_domains import parse_allowed_domains

StoredIn = Literal["config", "platform_settings", "sso_config", "agents"]
ControlState = Literal["enforced", "monitoring", "off"]

#: The categories of every control and every finding, in the order the
#: dashboard lists them, each with what it covers.
SECURITY_CATEGORIES: Mapping[str, str] = MappingProxyType(
    {
        "authentication": (
            "Sign-in and accounts: the admin account, SSO, passwords, sessions, invitations, "
            "MFA, accounts nobody signs in to, platform registration."
        ),
        "access": (
            "Sharing and access: company-managed and shared entities, write rules, admin reach, "
            "reads without an account, shared credentials."
        ),
        "encryption": "SECRET_KEY and the stored secrets it protects.",
        "network": "Reverse proxy trust, client addresses, rate limiting, transport, outbound requests.",
        "web": "Browser security: content security policy, framing, CORS, public widget rendering.",
        "integrations": "Credential health, webhook and notification signatures, OAuth token URLs, scopes.",
        "agents": "Guardrails, tool approvals, tool deny lists, output schemas, prompt guards.",
        "assignments": "Webhook triggers, event matching.",
        "flows": "Flow triggers and the webhooks that start them.",
        "channels": "Channel bots.",
        "tools": "Custom tools, the code interpreter, runners, tool-call budgets.",
        "mcp": "MCP endpoints, dynamic client registration, A2A.",
        "llm": "LLM providers, observability, trace payloads.",
        "infrastructure": "Redis, debug mode, what happens when a dependency fails.",
    }
)


# ---------------------------------------------------------------------------
# What a control reads
# ---------------------------------------------------------------------------


class SettingsView(Protocol):
    """The values a control's predicates read."""

    def config(self, name: str) -> Any:
        """The ``Settings`` field *name*."""
        ...

    def stored(self, table: str, column: str) -> Any:
        """The stored ``table.column``: the row's value, its default when the table has no row."""
        ...


class UnreadableRow:
    """Stands for a stored row the audit could not load: the controls it holds are left out."""

    def __repr__(self) -> str:
        return "UNREADABLE_ROW"


UNREADABLE_ROW = UnreadableRow()


class _RowUnavailableError(Exception):
    pass


def _copilot_like() -> bool:
    return _config.settings.is_copilot_like


def _off_unless_copilot_like() -> bool:
    return not _config.settings.is_copilot_like


#: Defaults ``Settings`` resolves itself (``_apply_mode_defaults``): dangerous
#: features are off in XTM One (and ai_disabled) mode, on in Copilot / dev mode.
_MODE_CONFIG_DEFAULTS: Mapping[str, Callable[[], Any]] = MappingProxyType(
    {
        "disable_code_interpreter": _off_unless_copilot_like,
        "disable_custom_tools": _off_unless_copilot_like,
        "disable_custom_variables": _off_unless_copilot_like,
    }
)

#: What a stored column reads before its table has a row: the column default,
#: or a callable for a default the platform resolves by mode.
STORED_DEFAULTS: Mapping[tuple[str, str], Any] = MappingProxyType(
    {
        ("platform_settings", "xtm_mcp_enabled"): False,
        ("platform_settings", "trace_store_payloads"): True,
        ("platform_settings", "trace_storage_enabled"): True,
        ("platform_settings", "tool_approvals_enabled"): True,
        ("platform_settings", "approve_tool_calls_default"): False,
        ("sso_config", "oidc_enabled"): False,
        ("sso_config", "saml_enabled"): False,
        ("sso_config", "oidc_auto_admin_new_users"): False,
        ("sso_config", "saml_auto_admin_new_users"): False,
        ("sso_config", "oidc_auto_create_users"): True,
        ("sso_config", "saml_auto_create_users"): True,
        ("sso_config", "local_auth_enabled"): True,
        # ``sso_helpers.get_sso_config`` with no row: self-service in Copilot / dev mode only.
        ("sso_config", "registration_enabled"): _copilot_like,
        ("sso_config", "allowed_domains"): "",
        # A per-agent column reads as how many live agents have it on.
        ("agents", "web_uploads_enabled"): 0,
    }
)


def config_default(name: str) -> Any:
    """The value of the ``Settings`` field *name* when nothing sets it, in this deployment's mode."""
    pinned = _pinned(name)
    if pinned is not None:
        return pinned
    resolver = _MODE_CONFIG_DEFAULTS.get(name)
    if resolver is not None:
        return resolver()
    return Settings.model_fields[name].get_default(call_default_factory=True)


def stored_default(table: str, column: str) -> Any:
    default = STORED_DEFAULTS[(table, column)]
    return default() if callable(default) else default


@functools.cache
def _security_options() -> ModuleType | None:
    """``app.core.security_options`` once it exists, else ``None``.

    A setting it registers can also be set in Settings > Access and security, and the
    platform then acts on ``security_option(<name>)``, never on ``settings``.
    """
    try:
        return importlib.import_module("app.core.security_options")
    except ModuleNotFoundError as exc:
        if exc.name != "app.core.security_options":
            raise
        return None


def _pinned(name: str) -> Any:
    """The value the platform mode forces on the security option *name*, else ``None``.

    Nothing an administrator sets changes it (in XTM One, quotas always fail
    closed): it is also the setting's default in that mode.
    """
    options = _security_options()
    if options is None or name not in options.SECURITY_OPTIONS:
        return None
    pinned_value = getattr(options.SECURITY_OPTIONS[name], "pinned_value", None)
    return pinned_value() if callable(pinned_value) else None


class _LiveView:
    """The platform as it runs: the settings it acts on and the stored rows the audit loaded."""

    def __init__(self, rows: Mapping[str, object | None]) -> None:
        self._rows = rows

    def config(self, name: str) -> Any:
        # Off the module attributes on every read, so a test that patches
        # ``settings`` is seen as the platform sees it.
        options = _security_options()
        if options is not None and name in options.SECURITY_OPTIONS:
            return options.security_option(name)
        return getattr(_config.settings, name)

    def stored(self, table: str, column: str) -> Any:
        row = self._rows.get(table)
        if row is UNREADABLE_ROW:
            raise _RowUnavailableError(table)
        value = None if row is None else getattr(row, column, None)
        return stored_default(table, column) if value is None else value


class _DefaultView:
    """*live*, with the control's own settings at their defaults: what the control reads by default."""

    def __init__(self, live: SettingsView, control: SecurityControl) -> None:
        self._live = live
        self._own = frozenset(control.setting_names)
        self._stored_in = control.stored_in

    def config(self, name: str) -> Any:
        if self._stored_in == "config" and name in self._own:
            return config_default(name)
        return self._live.config(name)

    def stored(self, table: str, column: str) -> Any:
        if table == self._stored_in and column in self._own:
            return stored_default(table, column)
        return self._live.stored(table, column)


# ---------------------------------------------------------------------------
# The controls
# ---------------------------------------------------------------------------

Predicate = Callable[[SettingsView], bool]

#: How many entries of a list a value names; the others are counted.
_LIST_SHOWN = 3


def format_value(value: Any) -> str:
    """A setting's value as the report shows it: the way an administrator would type it."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "unset"
    if isinstance(value, str):
        return value.strip() or "empty"
    if isinstance(value, set | frozenset | list | tuple):
        entries = sorted(value) if isinstance(value, set | frozenset) else list(value)
        if not entries:
            return "empty"
        shown = ", ".join(str(entry) for entry in entries[:_LIST_SHOWN])
        hidden = len(entries) - _LIST_SHOWN
        return f"{shown} and {hidden} more" if hidden > 0 else shown
    return str(value)


@dataclass(frozen=True)
class SecurityControl:
    """One security switch: where it is set, and when it counts as enforced."""

    #: ``<category>.<name>``, stable from one release to the next.
    id: str
    title: str
    #: The ``Settings`` field, or the stored column(s), comma-separated.
    setting: str
    #: The enforcing value as an administrator types it (``true``, ``closed``, ``>= 12``, ``empty``).
    hardened: str
    enforced: Predicate
    #: What enforcing it changes for users.
    impact: str
    stored_in: StoredIn = "config"
    #: Where a stored control is changed. A config control is changed through its environment variable.
    where: str = ""
    #: Where what enforcing it would block is recorded, to read before enforcing; empty when nothing records it.
    evidence: str = ""
    #: Observes and records what it would block, without blocking it.
    monitoring: Predicate | None = None
    #: A control that does not apply (SSO controls without SSO) is left out of the report.
    applies: Predicate | None = None
    #: The value as the report shows it; by default the setting's own value.
    display: Callable[[SettingsView], str] | None = None

    @property
    def category(self) -> str:
        return self.id.split(".", 1)[0]

    @property
    def setting_names(self) -> tuple[str, ...]:
        return tuple(name.strip() for name in self.setting.split(",") if name.strip())

    @property
    def env_var(self) -> str:
        """The variable a config control is set with; empty for a stored control."""
        return self.setting.upper() if self.stored_in == "config" else ""

    def describe(self, view: SettingsView) -> str:
        if self.display is not None:
            return self.display(view)
        if self.stored_in == "config":
            return format_value(view.config(self.setting))
        return ", ".join(format_value(view.stored(self.stored_in, name)) for name in self.setting_names)


def _is_true(name: str) -> Predicate:
    return lambda view: bool(view.config(name)) is True


def _is_false(name: str) -> Predicate:
    return lambda view: bool(view.config(name)) is False


def _is_empty(name: str) -> Predicate:
    return lambda view: not view.config(name)


def _fails_closed(name: str) -> Predicate:
    # Read the way ``core/rate_limit.py`` and ``services/quota.py`` read it.
    return lambda view: str(view.config(name) or "").strip().lower() == "closed"


def _at_least(name: str, bound: int) -> Predicate:
    return lambda view: int(view.config(name)) >= bound


def _at_most(name: str, bound: int) -> Predicate:
    return lambda view: int(view.config(name)) <= bound


def _api_key_lifetime_bounded(view: SettingsView) -> bool:
    # 0 is "no maximum", the default.
    return 0 < int(view.config("api_key_max_lifetime_days") or 0) <= 365


def _module_set(value: Any) -> frozenset[str]:
    """Top-level module names, as ``Settings.code_interpreter_allowed_modules`` parses them."""
    entries = value.split(",") if isinstance(value, str) else list(value or ())
    return frozenset(top for entry in entries if (top := str(entry).strip().split(".")[0]))


def _no_module_reenabled(name: str) -> Predicate:
    return lambda view: not _module_set(view.config(name))


# -- SSO -----------------------------------------------------------------------

_SSO_PROTOCOLS = (("oidc", "OIDC"), ("saml", "SAML"))


def _enabled_protocols(view: SettingsView) -> list[tuple[str, str]]:
    return [(key, label) for key, label in _SSO_PROTOCOLS if view.stored("sso_config", f"{key}_enabled")]


def _sso_enabled(view: SettingsView) -> bool:
    return bool(_enabled_protocols(view))


def _hub_gate(view: SettingsView) -> bool:
    """``XTM_HUB_ADMIN_GROUP``: an OIDC sign-in is admitted, and its role set, by the XTM Hub."""
    return bool(str(view.config("xtm_hub_admin_group") or "").strip())


def _hub_decides(key: str, view: SettingsView) -> bool:
    return key == "oidc" and _hub_gate(view)


def _no_protocol(column: str) -> Predicate:
    """No enabled SSO method has *column* on, the XTM Hub standing in for it on OIDC."""

    def check(view: SettingsView) -> bool:
        return not any(
            view.stored("sso_config", f"{key}_{column}")
            for key, _label in _enabled_protocols(view)
            if not _hub_decides(key, view)
        )

    return check


def _per_protocol(column: str) -> Callable[[SettingsView], str]:
    def show(view: SettingsView) -> str:
        parts = []
        for key, label in _enabled_protocols(view):
            value = format_value(view.stored("sso_config", f"{key}_{column}"))
            parts.append(f"{label}: {value} (XTM Hub gate)" if _hub_decides(key, view) else f"{label}: {value}")
        return ", ".join(parts) or "SSO off"

    return show


def _allowed_domains(view: SettingsView) -> list[str]:
    return parse_allowed_domains(view.stored("sso_config", "allowed_domains"))


def _sso_account_creation_restricted(view: SettingsView) -> bool:
    """No SSO method creates an account on the fly, or the allowed email domains restrict those it creates."""
    return _no_protocol("auto_create_users")(view) or bool(_allowed_domains(view))


def _sso_account_creation(view: SettingsView) -> str:
    shown = _per_protocol("auto_create_users")(view)
    domains = _allowed_domains(view)
    return f"{shown}; restricted to {format_value(domains)}" if domains else shown


def _self_registration(view: SettingsView) -> str:
    value = format_value(view.stored("sso_config", "registration_enabled"))
    domains = _allowed_domains(view)
    if view.stored("sso_config", "registration_enabled") and domains:
        return f"{value} (restricted to {format_value(domains)})"
    return value


# -- Web -------------------------------------------------------------------------


def _csp_mode(view: SettingsView) -> str:
    return str(view.config("content_security_policy_mode"))


def _csp_enforced(view: SettingsView) -> bool:
    # Read the way ``csp.policy_header`` reads it: any mode but off and report-only enforces.
    return _csp_mode(view) not in ("off", "report-only")


def _widget_uploads(view: SettingsView) -> str:
    widgets = int(view.stored("agents", "web_uploads_enabled") or 0)
    return f"on for {widgets} widget(s)" if widgets else "off on every widget"


def _frame_ancestors_named(view: SettingsView) -> bool:
    """No ``CONTENT_SECURITY_POLICY_FRAME_ANCESTORS`` entry admits every site."""
    # The predicate the startup warning and the audit's framing finding use;
    # imported here so this module stays light to import.
    from app.core.csp import permissive_frame_ancestor_sources

    return not permissive_frame_ancestor_sources(view.config("content_security_policy_frame_ancestors") or ())


# -- Tools -----------------------------------------------------------------------

_ISOLATED_SANDBOXES = frozenset({"docker", "opensandbox"})


def custom_tool_sandbox_mode(view: SettingsView) -> str:
    """``CUSTOM_TOOL_SANDBOX`` as ``custom_tool_executor._resolve_sandbox_mode`` resolves it.

    The deprecated ``CUSTOM_TOOL_DOCKER=true`` counts as ``docker`` while
    ``CUSTOM_TOOL_SANDBOX`` is not set at all; the default view never sets it.
    """
    mode = str(view.config("custom_tool_sandbox") or "auto").strip().lower()
    if mode not in {"auto", "docker", "subprocess", "opensandbox"}:
        mode = "auto"
    if (
        mode == "auto"
        and isinstance(view, _LiveView)
        and "custom_tool_sandbox" not in _config.settings.model_fields_set
        and os.environ.get("CUSTOM_TOOL_DOCKER", "").lower() in ("true", "1", "yes")
    ):
        return "docker"
    return mode


def _show_sandbox(view: SettingsView) -> str:
    mode = custom_tool_sandbox_mode(view)
    configured = str(view.config("custom_tool_sandbox") or "auto").strip().lower()
    return f"{mode} (CUSTOM_TOOL_DOCKER)" if mode != configured else mode


def _tools_screened(view: SettingsView) -> bool:
    return bool(view.config("prompt_guard_scanned_tools"))


def _budget_monitored(view: SettingsView) -> bool:
    # Without the strict cut, a batch past the budget still runs: the
    # overshoot is logged and recorded on the run's trace.
    return int(view.config("agent_loop_max_tool_calls") or 0) > 0


# -- LLM -------------------------------------------------------------------------


def _trace_payloads_not_stored(view: SettingsView) -> bool:
    """Read the way ``services/trace_switches.py`` reads it: no payload stored, or no trace at all."""
    return not view.stored("platform_settings", "trace_store_payloads") or not view.stored(
        "platform_settings", "trace_storage_enabled"
    )


def _trace_payloads(view: SettingsView) -> str:
    if not view.stored("platform_settings", "trace_storage_enabled"):
        return "trace storage off"
    return format_value(bool(view.stored("platform_settings", "trace_store_payloads")))


SECURITY_CONTROLS: tuple[SecurityControl, ...] = (
    # -- authentication ------------------------------------------------------
    SecurityControl(
        id="authentication.sso_auto_admin",
        title="SSO sign-ins keep their role",
        setting="oidc_auto_admin_new_users, saml_auto_admin_new_users",
        stored_in="sso_config",
        where="Settings > Access and security > Authentication (Automatically admin users)",
        hardened="false",
        enforced=_no_protocol("auto_admin_new_users"),
        applies=_sso_enabled,
        display=_per_protocol("auto_admin_new_users"),
        impact=(
            "People who sign in through SSO keep the role an administrator gave them. While it is "
            "on, everyone who signs in through that method is made an administrator, existing "
            "accounts included and at every sign-in; turning it off demotes nobody. With "
            "XTM_HUB_ADMIN_GROUP set, the XTM Hub decides the role of an OIDC sign-in instead."
        ),
        evidence="The platform log records each promotion (Auto-promoted SSO user ... to admin).",
    ),
    SecurityControl(
        id="authentication.sso_account_creation",
        title="SSO account creation restricted",
        setting="oidc_auto_create_users, saml_auto_create_users, allowed_domains",
        stored_in="sso_config",
        where="Settings > Access and security > Authentication (On-the-fly user creation, Allowed email domains)",
        hardened="false, or allowed email domains set",
        enforced=_sso_account_creation_restricted,
        applies=_sso_enabled,
        display=_sso_account_creation,
        impact=(
            "SSO no longer creates an account for everyone the identity provider authenticates. "
            "With On-the-fly user creation off, only people who already have an account, matched "
            "by SSO subject or email, can sign in through SSO; anyone else is refused until an "
            "administrator or an invitation creates the account. With allowed email domains set, "
            "SSO creates an account only for an email address, as the identity provider asserts "
            "it, at one of those domains. Existing accounts keep signing in whatever their domain. "
            "With XTM_HUB_ADMIN_GROUP set, the XTM Hub decides who may sign in through OIDC."
        ),
        evidence="The audit log records every SSO sign-in (Logged in via OIDC / SAML).",
    ),
    SecurityControl(
        id="authentication.self_registration",
        title="Self-registration off",
        setting="registration_enabled",
        stored_in="sso_config",
        where="Settings > Access and security > Authentication (Allow local registration)",
        hardened="false",
        enforced=lambda view: not view.stored("sso_config", "registration_enabled"),
        # Registration is a local sign-in: refused anyway while that is off.
        applies=lambda view: bool(view.stored("sso_config", "local_auth_enabled")),
        display=_self_registration,
        impact=(
            "Nobody can create an account from the sign-in page: accounts come from an "
            "administrator, an invitation or SSO."
        ),
        evidence="The audit log records each self-registration (Registered account ...).",
    ),
    SecurityControl(
        id="authentication.local_sign_in",
        title="Local sign-in off (SSO only)",
        setting="local_auth_enabled",
        stored_in="sso_config",
        where="Settings > Access and security > Authentication (Local Authentication)",
        hardened="false",
        enforced=lambda view: not view.stored("sso_config", "local_auth_enabled"),
        applies=_sso_enabled,
        impact=(
            "Every sign-in goes through SSO, administrators included: password sign-in and "
            "self-registration are refused (an invitation still registers its invitee). Keep an "
            "administrator who signs in through SSO before turning it off, since an identity "
            "provider outage then locks everyone out."
        ),
        evidence="The audit log records each password sign-in (login_method: local).",
    ),
    SecurityControl(
        id="authentication.min_password_length",
        title="Passwords of 12 characters or more",
        setting="min_password_length",
        hardened=">= 12",
        enforced=_at_least("min_password_length", 12),
        impact=(
            "A password set from then on (registration, invitation, password change, a local "
            "account an administrator creates) needs at least that many characters, with a "
            "letter and a digit as today. Existing passwords keep working until they change."
        ),
    ),
    SecurityControl(
        id="authentication.password_reset_expiry",
        title="Password reset links expire within an hour",
        setting="password_reset_expire_hours",
        hardened="1",
        enforced=_at_most("password_reset_expire_hours", 1),
        impact=(
            "A password reset link stops working an hour after it is sent: someone who opens the "
            "email later asks for a new one."
        ),
    ),
    SecurityControl(
        id="authentication.invitation_expiry",
        title="Invitation links expire within a week",
        setting="invitation_expire_hours",
        hardened="<= 168",
        enforced=_at_most("invitation_expire_hours", 168),
        impact=(
            "An invitation link lasts a week at most unless the administrator who creates it "
            "picks another lifetime: an invitee who waits longer needs a new one."
        ),
    ),
    SecurityControl(
        id="authentication.api_key_max_lifetime",
        title="Personal API keys expire within a year",
        setting="api_key_max_lifetime_days",
        hardened="1 to 365",
        enforced=_api_key_lifetime_bounded,
        impact=(
            "A personal API key created or given a new expiry from then on must expire within that "
            "many days, and one created without an expiry gets the longest allowed. Existing keys "
            "keep the expiry they have: the Security audit lists those that outlive the limit, for "
            "their owner to re-date or an administrator to revoke."
        ),
        evidence="Settings > Access and security > API keys lists every key with its expiry and last use.",
    ),
    SecurityControl(
        id="authentication.session_lifetime",
        title="Sessions last a day at most",
        setting="jwt_expire_minutes",
        hardened="<= 1440",
        enforced=_at_most("jwt_expire_minutes", 1440),
        impact=(
            "People sign in again at least every day, even while they use the platform (through "
            "their identity provider with SSO), and a stolen session token stops working within a "
            "day. Lowering the lifetime ends the sessions already older on their next request; "
            "raising it applies from the next sign-in. API keys and platform tokens are not "
            "sessions and are not affected."
        ),
    ),
    SecurityControl(
        id="authentication.session_idle_timeout",
        title="Unused sessions end within 30 minutes",
        setting="session_idle_timeout_minutes",
        hardened="5 to 30",
        enforced=lambda view: 0 < int(view.config("session_idle_timeout_minutes")) <= 30,
        impact=(
            "A session nobody uses for that long ends: the web app signs the person out and they "
            "sign in again. Clicking, typing, scrolling or coming back to the tab counts as use; "
            "what the web app does on its own (refreshing a page, the activity stream, an answer "
            "streaming in the chat) does not. The web app warns a minute before. While Redis "
            "cannot be reached, sessions are not ended for inactivity."
        ),
    ),
    SecurityControl(
        id="authentication.companion_pin_required",
        title="The mobile companion is locked behind a PIN",
        setting="companion_pin_required",
        hardened="true",
        enforced=_is_true("companion_pin_required"),
        impact=(
            "Everyone unlocks the mobile companion on their phone with a PIN of their own (6 to 8 "
            "digits) before it shows or approves anything: each time it opens, after half a minute "
            "in the background and after 10 minutes without use. Five wrong PINs sign the phone "
            "out. Phone notifications then carry no content and no Approve or Reject buttons. A "
            "person without a PIN chooses one the next time the companion opens; nothing changes "
            "on a computer."
        ),
    ),
    SecurityControl(
        id="authentication.platform_token_audiences",
        title="Platform tokens accepted only when addressed to BASE_URL",
        setting="xtm_auth_accepted_audiences",
        hardened="empty",
        enforced=_is_empty("xtm_auth_accepted_audiences"),
        impact=(
            "A token a registered OpenCTI, OpenAEV or OpenCRQ signs a user in with is accepted "
            "only when it is addressed to BASE_URL, XTM One's identity (published at "
            "/xtm/auth/metadata): a platform version that addresses its tokens to the internal "
            "URL it reaches XTM One on is refused until it reads that document."
        ),
        evidence="The platform logs the extra audiences it accepts at startup.",
    ),
    # -- access --------------------------------------------------------------
    SecurityControl(
        id="access.agent_tools_strict_admin_access",
        title="Admins' agents keep to their three tiers",
        setting="agent_tools_strict_admin_access",
        hardened="true",
        enforced=_is_true("agent_tools_strict_admin_access"),
        impact=(
            "An admin's agent tools (the agent builder in chat and on the XTM One MCP server, the "
            "work-items delegation) read, trigger, edit, delete or add assignments only where the "
            "admin's own, group-shared and company-managed objects reach; admins keep full access "
            "through the UI and the REST API."
        ),
    ),
    SecurityControl(
        id="access.presentation_public_links",
        title="Presentations open only to signed-in people",
        setting="presentation_public_links_enabled",
        hardened="false",
        enforced=_is_false("presentation_public_links_enabled"),
        impact=(
            "Nobody can open a presentation without an account: every public link answers as if it "
            "did not exist, at once, and neither the Share dialog nor an agent can create one. The "
            "links are kept, so turning the option back on brings back those that were not turned off."
        ),
        evidence=(
            "The audit log records each public link turned on, regenerated or off (entity "
            "presentation, details public_link)."
        ),
    ),
    SecurityControl(
        id="access.video_public_links",
        title="Videos play only for signed-in people",
        setting="video_public_links_enabled",
        hardened="false",
        enforced=_is_false("video_public_links_enabled"),
        impact=(
            "Nobody can watch or download a video without an account: every public video link "
            "answers as if it did not exist, at once, and neither the Share dialog nor an agent can "
            "create one. The links are kept, so turning the option back on brings them back."
        ),
        evidence=(
            "The audit log records each public video link turned on, regenerated, offered for "
            "download or off (entity video_project, details public_link)."
        ),
    ),
    SecurityControl(
        id="access.dashboard_public_links",
        title="Dashboards open only for signed-in people",
        setting="dashboard_public_links_enabled",
        hardened="false",
        enforced=_is_false("dashboard_public_links_enabled"),
        impact=(
            "Nobody can open a custom dashboard without an account: every published link answers "
            "as if it did not exist, at once, and none can be published. The links are kept, so "
            "turning the option back on brings them back."
        ),
        evidence=(
            "The audit log records each published link turned on, regenerated or off (entity "
            "custom_dashboard, details public_link)."
        ),
    ),
    # -- agents --------------------------------------------------------------
    SecurityControl(
        id="agents.phone_notification_content",
        title="Phone notifications show nothing on a lock screen",
        setting="phone_notification_content",
        hardened="minimal",
        enforced=lambda view: view.config("phone_notification_content") == "minimal",
        impact=(
            "A notification on a linked phone says only that something is new, with no title, no "
            "text and no Approve or Reject button: the person opens the app to read it and to "
            "answer an approval. The bell and the companion's Inbox are unchanged."
        ),
    ),
    SecurityControl(
        id="agents.scheduled_work_items",
        title="Agents run no action on their own at a later time",
        setting="scheduled_work_items_enabled",
        hardened="false",
        enforced=_is_false("scheduled_work_items_enabled"),
        impact=(
            "No agent can schedule a one-off action from a chat (\"post this tomorrow at 9:00\") "
            "and nothing already scheduled is run: every agent's scheduling tools answer that "
            "scheduling is disabled by the administrator. Items that come due meanwhile are marked "
            "missed and their creators notified; turning the option back on lets them reschedule. "
            "Recurring assignments are not affected."
        ),
        evidence=(
            "The audit log records each scheduled work item created, cancelled, rescheduled or "
            "edited (entity work_item), and each run is listed in Operations > Executions as "
            "Scheduled."
        ),
    ),
    SecurityControl(
        id="agents.prompt_guard_blocks_general_assistant",
        title="Prompt guards block General Assistant messages",
        setting="prompt_guard_block_general_assistant",
        hardened="true",
        enforced=_is_true("prompt_guard_block_general_assistant"),
        impact=(
            "While a prompt guard (Nova) is connected, a General Assistant message one of its block "
            "rules matches is refused with the guard's message before the model is called, instead "
            "of being answered. A false positive refuses a colleague's question."
        ),
        evidence=(
            "While a guard is connected, the audit log records every match (security_alert, source "
            "nova_guard, chat_surface general_assistant) with the rule's action."
        ),
    ),
    SecurityControl(
        id="agents.prompt_guard_blocks_public_widget",
        title="Prompt guards block public widget messages",
        setting="prompt_guard_block_public_widget",
        hardened="true",
        enforced=_is_true("prompt_guard_block_public_widget"),
        impact=(
            "While a prompt guard (Nova) is connected, a public chat widget message one of its block "
            "rules matches is refused with the guard's message before the model is called, instead "
            "of being answered. A false positive refuses a visitor, who has nobody to ask."
        ),
        evidence=(
            "While a guard is connected, the audit log records every match (security_alert, source "
            "nova_guard, chat_surface public_widget) with the rule's action."
        ),
    ),
    SecurityControl(
        id="agents.tool_result_guard_scope",
        title="Tool results screened by the prompt guard",
        setting="prompt_guard_scanned_tools",
        hardened="the tools that return outside content",
        enforced=_tools_screened,
        impact=(
            "While a prompt guard (Nova) is connected, it evaluates what the listed tools return to an "
            "agent, the text of the files they attach included, and records each rule match in the "
            "audit log. The evaluation runs beside the agent, so it adds no wait unless tool results a "
            "block rule matches are also withheld."
        ),
        evidence="The audit log records each rule match on a tool result (Security alert, surface tool_result).",
    ),
    SecurityControl(
        id="agents.tool_result_guard_blocking",
        title="Tool results a guard rule blocks are withheld",
        setting="prompt_guard_block_tool_results",
        hardened="true",
        enforced=lambda view: bool(view.config("prompt_guard_block_tool_results")) and _tools_screened(view),
        # Without it, a match on a screened result is recorded and the result
        # reaches the agent as it is.
        monitoring=lambda view: not view.config("prompt_guard_block_tool_results") and _tools_screened(view),
        impact=(
            "A screened tool result that a block rule matches is withheld: the agent reads a short "
            "refusal naming the tool instead, without the files the result carried. The agent then "
            "waits for the guard's verdict on each screened result before it continues."
        ),
        evidence=(
            "Until then the audit log records each match on a tool result, a block rule's included "
            "(rule_action: block, blocked: false)."
        ),
    ),
    SecurityControl(
        id="agents.prompt_guard_model_output",
        title="Prompt guards withhold model answers",
        setting="prompt_guard_model_output",
        hardened="block",
        enforced=lambda view: str(view.config("prompt_guard_model_output") or "").strip().lower() == "block",
        monitoring=lambda view: str(view.config("prompt_guard_model_output") or "").strip().lower() == "log",
        impact=(
            "While a prompt guard (Nova) is connected, every model answer is evaluated before it is "
            "delivered, and one of its block rules matches is replaced by a withheld notice: answers "
            "take the evaluation's time longer. A streamed answer (web chat, the streaming chat "
            "embedded in OpenCTI / OpenAEV, the widget's stream) has already been shown, so it is "
            "only audited."
        ),
        evidence=(
            "With log, while a guard is connected, every answer is evaluated in the background after "
            "it is delivered and each match recorded in the audit log (security_alert, surface "
            "model_output)."
        ),
    ),
    SecurityControl(
        id="agents.tool_approvals",
        title="Tool calls wait for approval",
        setting="tool_approvals_enabled",
        stored_in="platform_settings",
        where="Settings > Access and security > Tool approvals",
        hardened="true",
        enforced=lambda view: view.stored("platform_settings", "tool_approvals_enabled") is not False,
        impact=(
            "A tool call that asks first waits until a person approves it, in chat and in assignment "
            "runs, for every agent that asks before running tools. Suspended, no tool call waits for "
            "anyone and tools that change data run without asking; the tool rules, the connected "
            "platforms' policies and every personal approval are kept for when approvals resume."
        ),
        evidence=(
            "Every tool call records why it ran: while it is off, an assignment run's actions and the "
            "audit log's chat tool call entries (chat_tool_calls) carry gate_outcome "
            "auto_run_approvals_disabled."
        ),
    ),
    SecurityControl(
        id="agents.tool_approvals_default",
        title="Agents ask before running tools by default",
        setting="approve_tool_calls_default",
        stored_in="platform_settings",
        where="Settings > Access and security > Tool approvals (Every agent asks by default)",
        hardened="true",
        enforced=lambda view: view.stored("platform_settings", "approve_tool_calls_default") is True,
        # With approvals off nothing pauses, whatever the default says.
        applies=lambda view: view.stored("platform_settings", "tool_approvals_enabled") is not False,
        impact=(
            "Every agent that follows the platform default, and the General Assistant where chat is "
            "available, wait for a person to approve each tool call that asks first; an agent set to Ask "
            "or Don't ask keeps its own choice. Tool approvals are opt-in: a new install starts with it "
            "off, and an upgrade keeps the value already stored (on where an administrator, or an earlier "
            "release creating the settings, turned it on), so that no running agent starts asking on its "
            "own."
        ),
        evidence=(
            "While it is off, the calls of an agent that follows the platform default record gate_outcome "
            "auto_run_install_default_off in its assignment runs' actions; a chat turn whose gate is off "
            "adds no audit log entry."
        ),
    ),
    # -- network -------------------------------------------------------------
    SecurityControl(
        id="network.trust_proxy_headers",
        title="Forwarded headers trusted from declared proxies only",
        setting="trust_proxy_headers",
        hardened="false",
        enforced=_is_false("trust_proxy_headers"),
        impact=(
            "X-Forwarded-For and X-Forwarded-Proto count only from a proxy FORWARDED_ALLOW_IPS "
            "names, never from any peer. Declare the proxy there first: without it, rate limits "
            "bucket every caller under the proxy's address and HTTPS behind it is unconfirmed."
        ),
        evidence=(
            "The security audit's finding The audit trail records a proxy's address, not users' and "
            "the platform's once-per-process warning about it name the peer audited requests arrive "
            "from; so do the access log's client_ip and the peer an unconfirmed-transport warning "
            "names: the proxy address FORWARDED_ALLOW_IPS needs."
        ),
    ),
    SecurityControl(
        id="network.client_ip_strict",
        title="Client addresses read in canonical form",
        setting="client_ip_strict",
        hardened="true",
        enforced=_is_true("client_ip_strict"),
        impact=(
            "Every client address is keyed, logged and stored in canonical form - port, brackets "
            "and IPv6 zone index dropped, an IPv4-mapped address read as IPv4, IPv6 lowercase - so "
            "one client is one rate-limit key whatever the proxy wrote. A client whose address "
            "arrived in another spelling has its current rate-limit window restart once."
        ),
        evidence=(
            "The access log's client_ip and forwarded_for show each address in the spelling it "
            "arrives in, a port or brackets included."
        ),
    ),
    SecurityControl(
        id="network.rate_limit_ipv6_prefix",
        title="IPv6 clients rate-limited per network",
        setting="rate_limit_ipv6_prefix",
        hardened="<= 64",
        enforced=_at_most("rate_limit_ipv6_prefix", 64),
        impact=(
            "The per-IP rate limits key a native IPv6 client on its network (its /64 at 64) "
            "instead of its address, so rotating addresses within it draws no fresh bucket. Every "
            "device of one /64, a corporate IPv6 LAN included, then shares one budget."
        ),
        evidence=(
            "The access log's client_ip gives the IPv6 addresses requests arrive from, and so how "
            "many clients share a /64."
        ),
    ),
    SecurityControl(
        id="network.credential_transport_strict",
        title="Runner and MCP credentials issued over confirmed HTTPS only",
        setting="credential_transport_strict",
        hardened="true",
        enforced=_is_true("credential_transport_strict"),
        impact=(
            "Runner enrollment (POST /runners/enroll), MCP OAuth client registration (POST "
            "/mcp/oauth/register) and the MCP OAuth token endpoint (POST /mcp/oauth/token) refuse "
            "a request the platform cannot confirm arrived over HTTPS - 403 https required, the "
            "OAuth invalid_client_metadata error and the OAuth invalid_request error - instead of "
            "serving it. Behind a TLS-terminating proxy, FORWARDED_ALLOW_IPS must name it first, "
            "or every runner enrollment, MCP client registration and MCP token request through it "
            "is refused; a deployment served over plain HTTP can no longer enroll runners, "
            "register MCP clients or issue MCP tokens."
        ),
        evidence=(
            "While it is off the platform logs each of the three routes the first time it serves "
            "one over an unconfirmed transport (... served over unconfirmed transport), with the "
            "peer and the forwarding headers it saw."
        ),
    ),
    SecurityControl(
        id="network.relay_version_floor",
        title="Relays older than the supported version refused",
        setting="relay_min_supported_version",
        hardened="a relay version (e.g. 1.261002.1)",
        enforced=lambda view: bool(str(view.config("relay_min_supported_version") or "").strip()),
        impact=(
            "A relay older than the floor is refused when it connects (HTTP 426, WebSocket close "
            "4426) and asked to update: one that updates itself does so at its next check, one in "
            "a container needs a newer image. Until then its group carries no request through it."
        ),
        evidence="The security audit and each relay group's Relays tab list the relays older than the floor.",
    ),
    SecurityControl(
        id="network.secret_vault_trusted_origins",
        title="No loopback exception for secret vaults connected in the UI",
        setting="secret_vault_trusted_origins",
        hardened="empty",
        enforced=_is_empty("secret_vault_trusted_origins"),
        impact=(
            "A vault connected in Settings > Access and security > Secret vaults is never reached on a loopback "
            "address; only a vault on the platform's own host (a Vault Agent, a 1Password Connect "
            "server beside it) needs its origin listed. Link-local and cloud metadata addresses stay "
            "refused whatever the list holds, and vaults declared in the environment are not affected."
        ),
    ),
    # -- web -----------------------------------------------------------------
    SecurityControl(
        id="web.content_security_policy",
        title="Content security policy enforced",
        setting="content_security_policy_mode",
        hardened="enforce",
        enforced=_csp_enforced,
        monitoring=lambda view: _csp_mode(view) == "report-only",
        impact=(
            "Browsers refuse to run a script or load a resource the platform's pages do not need, a script "
            "injected past the output checks or added by a proxy or CDN included, and to show the pages in the "
            "iframe of a site the policy does not allow. The API reference, the Teams tab and the Office add-in "
            "stay report-only."
        ),
        evidence=(
            "In report-only mode browsers report what the policy would block: the platform logs each violation "
            "the first time it sees it (CSP violation first seen ...), and the audit counts the last 7 days per "
            "directive."
        ),
    ),
    SecurityControl(
        id="web.csp_frame_ancestors",
        title="Framing restricted to named sites",
        setting="content_security_policy_frame_ancestors",
        hardened="a list without *, a bare scheme or a wildcard host",
        enforced=lambda view: _csp_enforced(view) and _frame_ancestors_named(view),
        monitoring=lambda view: _csp_mode(view) == "report-only" and _frame_ancestors_named(view),
        impact=(
            "Once CONTENT_SECURITY_POLICY_MODE=enforce, only the platform itself, the sites listed and, behind a "
            "registration token of the operator's own, the registered platforms may show its pages in an iframe: "
            "list a portal that embeds the platform before enforcing, or it is refused."
        ),
        evidence=(
            "In report-only mode each page another site frames is reported as a frame-ancestors violation, "
            "counted in the audit's per-directive totals."
        ),
    ),
    SecurityControl(
        id="web.public_widget_strict_rendering",
        title="Public chat widget strict rendering",
        setting="public_widget_strict_rendering",
        hardened="true",
        enforced=_is_true("public_widget_strict_rendering"),
        impact=(
            "Public chat widget answers show only the images the platform serves (any other "
            "image becomes a link), open every link in a new tab, and drop inline styles, SVG, "
            "MathML and links other than http(s), mailto and tel."
        ),
    ),
    SecurityControl(
        id="web.public_widget_uploads",
        title="Public chat widget visitors cannot attach files",
        setting="web_uploads_enabled",
        stored_in="agents",
        where="Agents > each agent > Configuration > Web Exposure (Allow visitor uploads)",
        hardened="false on every widget",
        enforced=lambda view: not view.stored("agents", "web_uploads_enabled"),
        display=_widget_uploads,
        impact=(
            "Visitors of the public chat widgets, who have no account, can only type: a widget that "
            "let them attach a document or an image to a message no longer offers it. The files "
            "already attached stay readable by the agent until the retention deletes them."
        ),
        evidence=(
            "While a prompt guard is connected, the audit log records each visitor file one of its rules "
            "matched (security_alert, surface widget_upload)."
        ),
    ),
    SecurityControl(
        id="web.cors_origins",
        title="CORS restricted to named origins",
        setting="cors_origins",
        hardened="a list without *",
        enforced=lambda view: "*" not in list(view.config("cors_origins") or ()),
        impact=(
            "Only the listed origins may call the API from a browser page; a page on any other "
            "origin is refused by the browser."
        ),
    ),
    # -- integrations --------------------------------------------------------
    SecurityControl(
        id="integrations.oauth_token_url_strict",
        title="Strict checks on OAuth token URLs",
        setting="oauth_token_url_strict",
        hardened="true",
        enforced=_is_true("oauth_token_url_strict"),
        impact=(
            "Every OAuth token request (code exchange and refresh, for integrations and MCP "
            "servers) and every save of a token URL also refuses a token URL whose host is, or "
            "resolves to, a loopback, link-local, unspecified, reserved or multicast address, and "
            "any scheme but http(s); private networks stay allowed. An integration or MCP server "
            "whose token URL is on loopback stops exchanging and refreshing tokens until the URL "
            "points at another address of its identity provider or, on a development machine, "
            "its origin is listed in the loopback token URL origins."
        ),
    ),
    SecurityControl(
        id="integrations.oauth_token_dev_trusted_origins",
        title="No development exception for OAuth token URLs",
        setting="oauth_token_dev_trusted_origins",
        hardened="empty",
        enforced=_is_empty("oauth_token_dev_trusted_origins"),
        impact=(
            "Under the strict OAuth token URL checks, every OAuth token request (code exchange and "
            "refresh, for integrations and MCP servers) refuses a token URL that resolves to a "
            "loopback address; only local development against an identity provider on the same "
            "machine needs the list. Without the strict checks the list has no effect."
        ),
        evidence="The platform logs the listed origins at startup.",
    ),
    SecurityControl(
        id="integrations.notifications_require_verification",
        title="Integration notifications must prove their sender",
        setting="integration_notifications_require_verification",
        hardened="true",
        enforced=_is_true("integration_notifications_require_verification"),
        impact=(
            "A Slack event, Microsoft Graph change notification or LinkedIn notification the "
            "platform cannot verify is refused instead of accepted: a Slack integration with no "
            "signing secret stored gets its messages by polling (every 5 minutes) instead of in "
            "real time, a Graph subscription recorded without a clientState stops triggering, and "
            "the admin OAuth app's webhook and LinkedIn notifications reach only the admin app's "
            "integrations the agent owner may use (a GitHub App event only those recording its "
            "installation or, recording none, whose own token sees it; one holding no usable token "
            "receives none). A Microsoft Graph notification for a Teams bot's "
            "private or shared channel is acted on only when it carries the subscription id and the "
            "random clientState recorded for its channel, and the next maintenance pass replaces the "
            "subscriptions created before 2026-07-02 (derivable clientState) or whose clientState can "
            "no longer be decrypted, deleting each before creating its replacement: if Microsoft Graph "
            "refuses the new one, that channel's follow-ups stop until a later pass creates it."
        ),
        evidence=(
            "The platform log warns (at most once every 15 minutes per case) about each kind of "
            "delivery accepted unverified and about deliveries that reached an assignment the "
            "strict rule would leave out; the audit counts the Teams channel notifications accepted "
            "unverified in the last seven days."
        ),
    ),
    # -- agents --------------------------------------------------------------
    SecurityControl(
        id="agents.nova_llm_every_condition",
        title="Nova asks every llm: condition",
        setting="nova_llm_max_conditions",
        hardened="0",
        enforced=lambda view: int(view.config("nova_llm_max_conditions") or 0) <= 0,
        impact=(
            "While the Nova integration runs its full engine, every distinct llm: condition of the "
            "enabled rules is put to the model on each message it screens, one call per condition, "
            "as it always was. With a ceiling set, the conditions past it are not asked (those of "
            "block rules are kept first), and a rule resting on one cannot fire through that tier."
        ),
        evidence=(
            "The platform log warns once per rule set with how many llm: conditions the ceiling "
            "leaves unasked (Nova llm tier: NOVA_LLM_MAX_CONDITIONS=...), and each evaluation logs "
            "its provider calls (Nova evaluation: ...)."
        ),
    ),
    # -- assignments ---------------------------------------------------------
    SecurityControl(
        id="assignments.webhook_require_secret",
        title="Assignment webhooks need a secret",
        setting="webhook_require_secret",
        hardened="true",
        enforced=_is_true("webhook_require_secret"),
        impact=(
            "A delivery to an assignment webhook with no secret configured is refused (401) "
            "instead of run: such an assignment stops triggering until it has a secret and the "
            "sender signs with it."
        ),
        evidence=(
            "The platform log warns when an assignment without a secret receives a delivery "
            "(once per assignment and process)."
        ),
    ),
    SecurityControl(
        id="assignments.exact_event_matching",
        title="Triggers run only on the events they select",
        setting="webhook_exact_event_matching",
        hardened="true",
        enforced=_is_true("webhook_exact_event_matching"),
        # A trigger still on the prefix match logs, once a day, a delivery it
        # runs on that exact matching would skip.
        monitoring=_is_false("webhook_exact_event_matching"),
        impact=(
            "Integration-event and webhook triggers saved before exact event matching stop running "
            "on events whose name only starts with the one they select (pull_request no longer "
            "runs on pull_request_review), and a GitHub trigger on its dedicated URL runs only on "
            "its event. Triggers created or given new events since already match exactly."
        ),
        evidence=(
            "The platform log records the deliveries each such trigger runs on only by prefix "
            "(legacy event matching ran it on ..., once a day per assignment for the whole platform)."
        ),
    ),
    # -- tools ---------------------------------------------------------------
    SecurityControl(
        id="tools.code_interpreter_disabled",
        title="Code interpreter disabled",
        setting="disable_code_interpreter",
        hardened="true",
        enforced=_is_true("disable_code_interpreter"),
        impact="Agents and the General Assistant can no longer run Python in the code interpreter.",
    ),
    SecurityControl(
        id="tools.browser_profiles_public_targets_only",
        title="Browser profiles reach public addresses only",
        setting="browser_private_targets",
        hardened="false",
        enforced=_is_false("browser_private_targets"),
        impact=(
            "A browser profile's sign-in, an agent browsing signed in with it and a Video Studio recording "
            "reach public addresses only, like the agent browser tool: an application on a private network "
            "cannot be opened from the backend. Loopback, link-local and cloud metadata addresses are "
            "refused either way."
        ),
        evidence=(
            "The audit log records each browser profile created, changed, signed in, used by an agent or "
            "deleted (entity browser_profile), with the hosts it declares."
        ),
    ),
    SecurityControl(
        id="tools.custom_tools_disabled",
        title="Custom tools disabled",
        setting="disable_custom_tools",
        hardened="true",
        enforced=_is_true("disable_custom_tools"),
        impact=(
            "No custom tool (JavaScript or Python written by a user) runs anywhere: chat, assignments, flows and tests."
        ),
    ),
    SecurityControl(
        id="tools.custom_variables_disabled",
        title="Custom variables disabled",
        setting="disable_custom_variables",
        hardened="true",
        enforced=_is_true("disable_custom_variables"),
        impact="The Variables feature is refused: its API and the agent builder's variable tools.",
    ),
    SecurityControl(
        id="tools.svg_strict_rendering",
        title="SVGs reaching outside themselves are refused",
        setting="svg_strict_rendering",
        hardened="true",
        enforced=_is_true("svg_strict_rendering"),
        impact=(
            "An SVG a model is shown that declares an XML entity or references anything outside itself "
            "(a file path, a URL, another document) is not rendered at all, instead of being rendered with "
            "that reference dropped. Nothing is fetched or read from disk in either mode."
        ),
        evidence="Each SVG rendered with a reference dropped logs an INFO line: those are what it would refuse.",
    ),
    SecurityControl(
        id="tools.custom_tool_sandbox",
        title="Custom tools run only in a container",
        setting="custom_tool_sandbox",
        hardened="docker or opensandbox",
        enforced=lambda view: custom_tool_sandbox_mode(view) in _ISOLATED_SANDBOXES,
        display=_show_sandbox,
        impact=(
            "Custom tools run in the hardened Docker container or an OpenSandbox container only: "
            "when that is unreachable they fail instead of falling back to a plain subprocess."
        ),
        evidence=(
            "While custom tools run in the subprocess fallback, the platform logs a security notice "
            "once a day, and again after an upgrade or a change of sandbox mode or Node.js permission model."
        ),
    ),
    SecurityControl(
        id="tools.custom_tool_node_permission",
        title="Node.js permission model for JavaScript tools",
        setting="custom_tool_node_permission",
        hardened="true",
        enforced=_is_true("custom_tool_node_permission"),
        impact=(
            "A JavaScript custom tool run in the subprocess fallback gets no file system, child "
            "process, worker thread or native addon access (Node.js 24 or later); the network "
            "stays available."
        ),
    ),
    SecurityControl(
        id="tools.code_interpreter_extra_modules",
        title="No module re-enabled in the code interpreter",
        setting="code_interpreter_extra_allowed_modules",
        hardened="empty",
        enforced=_no_module_reenabled("code_interpreter_extra_allowed_modules"),
        impact=(
            "The code interpreter keeps its whole module blocklist: code it runs cannot open "
            "network connections (it shares the host's network)."
        ),
    ),
    SecurityControl(
        id="tools.code_interpreter_package_installs",
        title="No package installed into the code interpreter",
        setting="code_interpreter_package_allowlist",
        hardened="empty",
        enforced=_is_empty("code_interpreter_package_allowlist"),
        impact=(
            "code_interpreter installs no package into a conversation's sandbox workspace: code runs with "
            "the packages the sandbox ships. A listed package is downloaded by the server from the package "
            "index (wheels only, checked against the index's sha256) and, once imported, can import modules "
            "the sandbox blocks to the code itself."
        ),
        evidence="Each install is listed in the code_interpreter result that asked for it.",
    ),
    SecurityControl(
        id="tools.sandbox_command_declared_only",
        title="Only the checks a checkout declares run in the sandbox",
        setting="code_interpreter_command_allow_argv",
        hardened="false",
        enforced=_is_false("code_interpreter_command_allow_argv"),
        impact=(
            "sandbox_run_command runs the checks a checked-out repository declares in .xtm-one/checks.toml and "
            "nothing else: an agent cannot run a command of its own in the sandbox container. The container "
            "has no network and no credential either way."
        ),
        evidence="A refused argv names the declared checks in the tool result; every run is audited (sandbox_run_command).",
    ),
    SecurityControl(
        id="tools.custom_tool_extra_modules",
        title="No module re-enabled for Python custom tools",
        setting="custom_tool_extra_allowed_modules",
        hardened="empty",
        enforced=_no_module_reenabled("custom_tool_extra_allowed_modules"),
        impact=(
            "Python custom tools keep their whole module blocklist: in the subprocess fallback "
            "they cannot open network connections."
        ),
    ),
    SecurityControl(
        id="tools.runner_strict_path_confinement",
        title="Runner folders cannot be left through a link",
        setting="runner_strict_path_confinement",
        hardened="true",
        enforced=_is_true("runner_strict_path_confinement"),
        impact=(
            "A restricted runner refuses a file command whose path leads outside its allowed "
            "folders through a symbolic link, junction or mount point, instead of running it with "
            "a warning (runners from this release or later)."
        ),
        evidence="Until then such a command runs with a warning in its result, in the runner's command history.",
    ),
    SecurityControl(
        id="tools.runner_result_bound",
        title="Runner results bounded",
        setting="runner_max_result_bytes",
        hardened="> 0",
        enforced=lambda view: int(view.config("runner_max_result_bytes")) > 0,
        impact=(
            "A runner file read never asks for more than the bound or 256 KiB, whichever is "
            "larger (text up to 256 KiB comes back inline, which the bound does not cap), and "
            "a larger file result is refused, so an agent cannot make a runner read a large "
            "file into memory."
        ),
    ),
    SecurityControl(
        id="tools.tool_call_budget_strict",
        title="Tool-call budget cut strictly",
        setting="agent_loop_max_tool_calls_strict",
        hardened="true",
        enforced=_is_true("agent_loop_max_tool_calls_strict"),
        monitoring=_budget_monitored,
        impact=(
            "A parallel batch of tool calls that crosses a run's budget runs only the calls that "
            'still fit; the others are answered "Not executed" instead of running.'
        ),
        evidence="Each overshoot is logged and recorded on the run's trace.",
    ),
    # -- mcp -----------------------------------------------------------------
    SecurityControl(
        id="mcp.dynamic_client_registration",
        title="MCP dynamic client registration off",
        setting="mcp_dcr_enabled",
        hardened="false",
        enforced=_is_false("mcp_dcr_enabled"),
        impact=(
            "MCP clients can no longer register themselves (POST /mcp/oauth/register answers "
            "403): an MCP client such as Claude Desktop or Cursor then needs an API key. Clients "
            "registered before keep working."
        ),
        evidence="The audit counts the clients registered so far.",
    ),
    SecurityControl(
        id="mcp.client_id_metadata_documents",
        title="MCP client metadata documents off",
        setting="mcp_cimd_enabled",
        hardened="false",
        enforced=_is_false("mcp_cimd_enabled"),
        impact=(
            "A client naming its own metadata document as its client_id (claude.ai, Claude "
            "Code, VS Code) is refused at /mcp/oauth/authorize and its tokens no longer refresh: "
            "it then needs dynamic registration or an API key. The platform no longer fetches "
            "client documents."
        ),
        evidence="Each document the platform refuses is logged with its URL and the reason.",
    ),
    SecurityControl(
        id="mcp.outbound_dynamic_client_registration",
        title="No automatic registration with remote MCP servers",
        setting="mcp_outbound_dcr_enabled",
        hardened="false",
        enforced=_is_false("mcp_outbound_dcr_enabled"),
        impact=(
            "The platform no longer registers itself with a remote MCP server's authorization "
            "server: connecting an OAuth MCP server needs a client ID provisioned by hand."
        ),
    ),
    SecurityControl(
        id="mcp.server_url_policy",
        title="Remote MCP servers cannot reach internal addresses",
        setting="mcp_server_url_policy",
        hardened="private_networks or public_only",
        enforced=lambda view: str(view.config("mcp_server_url_policy") or "").strip().lower()
        in {"private_networks", "public_only"},
        impact=(
            "Saving a remote MCP server, and every connection the platform opens to one (redirects "
            "and OAuth discovery included), also refuses a URL whose host is, or resolves to, a "
            "loopback, link-local, unspecified, reserved or multicast address; public_only refuses "
            "private networks too. Cloud metadata addresses are refused in every mode. A server "
            "already saved at a refused address stops connecting until its URL changes, its "
            "origin is listed in the trusted MCP server origins, or it is reached through a relay "
            "group."
        ),
    ),
    SecurityControl(
        id="mcp.server_trusted_origins",
        title="No exception to the MCP server URL policy",
        setting="mcp_server_trusted_origins",
        hardened="empty",
        enforced=_is_empty("mcp_server_trusted_origins"),
        impact=(
            "Every remote MCP server is held to the MCP server URL policy; a listed origin is "
            "otherwise reached at any address but a cloud metadata one."
        ),
    ),
    SecurityControl(
        id="mcp.xtm_mcp_server",
        title="XTM One MCP server off",
        setting="xtm_mcp_enabled",
        stored_in="platform_settings",
        where="Settings > Integrations > MCP Endpoint",
        hardened="false",
        enforced=lambda view: not view.stored("platform_settings", "xtm_mcp_enabled"),
        impact=(
            "External MCP clients no longer reach this instance's own tools at /mcp/xtm: traces, "
            "the agent builder, knowledge search and skills, each within the caller's own rights."
        ),
    ),
    SecurityControl(
        id="mcp.a2a_dev_trusted_origins",
        title="No development exception for A2A partners",
        setting="a2a_client_dev_trusted_origins",
        hardened="empty",
        enforced=_is_empty("a2a_client_dev_trusted_origins"),
        impact=(
            "The A2A client reaches every partner over HTTPS at an address its destination "
            "guard accepts; only two deployments tested on one machine need the list."
        ),
        evidence="The platform logs the listed origins at startup.",
    ),
    SecurityControl(
        id="mcp.a2a_insecure_loopback",
        title="Loopback is not taken for HTTPS on A2A",
        setting="a2a_allow_insecure_loopback",
        hardened="false",
        enforced=_is_false("a2a_allow_insecure_loopback"),
        impact=(
            "A plain HTTP request from 127.0.0.1 or ::1 no longer counts as encrypted on the A2A "
            "surface. Behind a reverse proxy on the same host every partner request arrives from "
            "loopback, so the setting would waive HTTPS for all of them."
        ),
    ),
    SecurityControl(
        id="mcp.test_only_insecure_transport_peers",
        title="No test-only HTTPS exemption",
        setting="test_only_insecure_transport_peers",
        hardened="empty",
        enforced=_is_empty("test_only_insecure_transport_peers"),
        impact=(
            "No network is exempted from the A2A HTTPS requirement: the list exists for the end-to-end test suite only."
        ),
        evidence="The platform logs the listed networks at startup.",
    ),
    # -- llm -------------------------------------------------------------------
    SecurityControl(
        id="llm.trace_payloads",
        title="Traces keep no prompts or answers",
        setting="trace_store_payloads, trace_storage_enabled",
        stored_in="platform_settings",
        where="Settings > Usage and quotas > Observability (Store full payloads, Enable local trace storage)",
        hardened="false, or trace storage off",
        enforced=_trace_payloads_not_stored,
        display=_trace_payloads,
        impact=(
            "LLM traces no longer keep what was said: each call's prompts, answers, tool arguments "
            "and tool results are dropped, and an error message keeps its first 200 characters. "
            "Traces still show the timings, token counts, cost, model, tool names and status; with "
            "trace storage off, no trace is stored at all. It applies to the traces written from "
            "then on, on every server within seconds: traces stored before keep what they hold until "
            "the retention deletes them, and the Langfuse export, while it is on, still receives "
            "each call's payloads."
        ),
    ),
    # -- infrastructure --------------------------------------------------------
    SecurityControl(
        id="infrastructure.rate_limit_fail_mode",
        title="Rate limits fail closed",
        setting="rate_limit_fail_mode",
        hardened="closed",
        enforced=_fails_closed("rate_limit_fail_mode"),
        impact=(
            "While Redis cannot be reached, rate-limited requests (sign-in, password reset, "
            "webhooks, chat messages) are refused with a 503 instead of passing unthrottled."
        ),
        evidence="Each check skipped while Redis is unreachable is logged (Rate-limit check failed for ...; failing open).",
    ),
    SecurityControl(
        id="infrastructure.quota_fail_mode",
        title="Quotas fail closed",
        setting="quota_fail_mode",
        hardened="closed",
        enforced=_fails_closed("quota_fail_mode"),
        impact="While the quota store cannot be reached, agentic executions are refused instead of running unmetered; a due background run is held until the store answers.",
        evidence="Each execution that ran unmetered is logged (... proceeds unmetered for this check).",
    ),
    SecurityControl(
        id="infrastructure.debug_mode",
        title="Debug mode off",
        setting="debug",
        hardened="false",
        enforced=_is_false("debug"),
        impact=(
            "SQL statements are no longer echoed to the log with the tokens and encrypted values "
            "they carry, loopback no longer counts as HTTPS, password reset links are no longer "
            "written to the log when no SMTP server is set, and development-only tools are gone."
        ),
    ),
)

_CONTROLS_BY_ID: Mapping[str, SecurityControl] = MappingProxyType({c.id: c for c in SECURITY_CONTROLS})

#: Switch-shaped ``Settings`` fields that are not security controls, with why.
#: Names open pull requests add are listed ahead, so each is placed once it lands.
NOT_CONTROLS: Mapping[str, str] = MappingProxyType(
    {
        "disable_sandbox_quota_usage": "Metering: whether sandbox runs count against the usage quota.",
        "strict_binary_text_extraction": (
            "Ingestion: whether a legacy Office binary with no extractor is indexed as "
            "text or skipped; it guards no boundary."
        ),
        "channel_agent_mentions_enabled": "Product: whether a channel message may address an agent by name.",
        "integration_event_queue_enabled": (
            "Delivery: whether an integration event reaching a busy assignment is queued or skipped; "
            "it guards no boundary."
        ),
        "tool_approvals_enabled": (
            "Seed only: read once to create platform_settings.tool_approvals_enabled; "
            "that stored switch, the agents.tool_approvals control, is what the approval gate consults."
        ),
        "tool_approvals_platform_default": (
            "Seed only: read once when a platform first registers, to create its "
            "platform_registrations.approval_policy; the audit reports every connected platform "
            "whose tools run unasked (agents.tool_approvals_platforms_skip)."
        ),
        "nova_evaluation_concurrency": (
            "Latency: how many provider calls one Nova evaluation runs at once; the same calls "
            "are made and give the same verdicts whatever the value."
        ),
    }
)


def get_control(control_id: str) -> SecurityControl:
    """The registered control *control_id*; ``KeyError`` for an unknown id."""
    return _CONTROLS_BY_ID[control_id]


def stored_value(row: object | None, table: str, column: str) -> Any:
    """``table.column`` of the loaded *row* (never :data:`UNREADABLE_ROW`), its default when the table has no row."""
    return _LiveView({table: row}).stored(table, column)


def configured_custom_tool_sandbox() -> str:
    """The custom tool sandbox mode as configured: ``auto`` stays ``auto``, whether Docker answers or not."""
    return custom_tool_sandbox_mode(_LiveView({}))


def controls_reading(names: Iterable[str]) -> list[SecurityControl]:
    """The controls whose setting is one of *names*."""
    wanted = set(names)
    return [control for control in SECURITY_CONTROLS if wanted & set(control.setting_names)]


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlStatus:
    id: str
    category: str
    title: str
    #: The environment variable of a config control, the stored column(s) otherwise.
    setting: str
    stored_in: StoredIn
    where: str
    value: str
    default: str
    hardened: str
    state: ControlState
    #: Whether the default leaves it off: enforcing it is a choice to make.
    opt_in: bool
    impact: str
    evidence: str
    #: The platform mode forces its value: no administrator can change it.
    pinned: bool = False


def _status(control: SecurityControl, live: SettingsView) -> ControlStatus | None:
    if control.applies is not None and not control.applies(live):
        return None
    default = _DefaultView(live, control)
    if control.enforced(live):
        state: ControlState = "enforced"
    elif control.monitoring is not None and control.monitoring(live):
        state = "monitoring"
    else:
        state = "off"
    return ControlStatus(
        id=control.id,
        category=control.category,
        title=control.title,
        setting=control.env_var or control.setting,
        stored_in=control.stored_in,
        where=control.where,
        value=control.describe(live),
        default=control.describe(default),
        hardened=control.hardened,
        state=state,
        opt_in=not control.enforced(default),
        impact=control.impact,
        evidence=control.evidence,
        pinned=control.stored_in == "config" and _pinned(control.setting) is not None,
    )


def control_statuses(
    platform_settings: object | None, sso_config: object | None, agents: object | None = None
) -> list[ControlStatus]:
    """Every control that applies, with its state, in registry order.

    *platform_settings* and *sso_config* are the loaded rows: ``None`` for a
    table with no row yet (its defaults apply), :data:`UNREADABLE_ROW` for one
    the audit could not load (the controls it holds are left out rather than
    reported from defaults the platform may not run with). *agents* carries,
    per per-agent column, how many live agents have it on.
    """
    live = _LiveView({"platform_settings": platform_settings, "sso_config": sso_config, "agents": agents})
    statuses = []
    for control in SECURITY_CONTROLS:
        try:
            status = _status(control, live)
        except _RowUnavailableError:
            continue
        if status is not None:
            statuses.append(status)
    return statuses


def controls_summary(statuses: Iterable[ControlStatus]) -> dict[str, int]:
    """How many controls are in each state (zero included)."""
    summary = {"enforced": 0, "monitoring": 0, "off": 0}
    for status in statuses:
        summary[status.state] = summary.get(status.state, 0) + 1
    return summary
