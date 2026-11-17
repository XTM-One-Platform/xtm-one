# SPDX-License-Identifier: Apache-2.0

"""Security options: the environment variable first, then Settings > Access and security.

Each option is a field of :class:`app.core.config.Settings` that an
administrator can also set in Settings > Access and security. :func:`security_option` is
the ONE reading every call site uses, in this order:

1. The platform mode pins it (:attr:`SecurityOption.pinned`): that value,
   whatever the environment or the database says - in XTM One, quotas always
   fail closed, because quotas are what the licence sells. The field is
   shown locked for that reason and a write to it is refused; a stored value
   is kept, not applied, so another mode finds it, and a variable naming
   another value is reported once at startup (:func:`log_ignored_variables`).
2. The environment variable - the field's name in upper case - is set, and not
   blank: its value, validated by ``Settings`` at startup like any variable.
   The field is shown locked in the UI and a write to it is refused, the same
   contract as the ``DEFAULT_*`` models (``_env_managed_ga_fields``), Langfuse
   and the env-seeded LLM providers.
3. Otherwise the value an administrator stored in
   ``platform_settings.security_options``, read from this process's snapshot -
   never from the database on the request path.
4. Otherwise ``settings.<key>``: exactly the default the platform had before
   the option reached the UI.

"Set" is what ``Settings`` itself read when ``settings`` was built
(``Settings.environment_fields``): the process environment, the ``.env``
files, with pydantic-settings' own parsing and order - never a second read,
which a later ``load_dotenv`` or a FIFO ``.env`` would make disagree. A blank
value counts as unset, as it does for the Langfuse and ``DEFAULT_*`` locks.

The snapshot is replaced by ``app/services/security_options.py``: at startup,
after an administrator's write once it is committed, and by a short poll in
every web and worker process, so every replica converges within
:data:`REFRESH_INTERVAL_SECONDS`. When the database cannot be read the last
snapshot stays; when none was ever read the environment / default applies.

Adding an option: an entry in :data:`SECURITY_OPTIONS` (its key is the
``Settings`` field, its group the category of its control), the call site
reading ``security_option("<key>")`` instead of ``settings.<key>``, its label
in the frontend table (``pages/settings/security-options.ts``, en / es / fr /
ja) and the docs line. Every option is also a security control
(``SECURITY_CONTROLS`` in ``app/core/security_controls.py``): a switch that
guards no boundary is not a security option. ``tests/test_security_options.py``
fails on a registered option still read straight from ``settings`` anywhere in
``app/``, and ``tests/test_security_controls.py`` on an option with no control
or grouped under another category than its control's.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

import app.core.config as _config
from app.core.config import (
    MAX_SCANNED_TOOLS,
    SESSION_IDLE_MINUTES_MIN,
    SESSION_MINUTES_MAX,
    Settings,
    parse_allowed_modules,
    parse_scanned_tools,
    parse_trusted_origins,
)

logger = logging.getLogger(__name__)

#: How often every web and worker process re-reads the stored values: the
#: longest a change made on another replica takes to apply here. The same
#: bound ``ai_state`` keeps for the AI switch.
REFRESH_INTERVAL_SECONDS = 5.0

#: Where a value comes from, as the API reports it. The two that lock the
#: field are also the lock's reason (``locked_reason``).
SOURCE_PLATFORM_MODE = "platform_mode"
SOURCE_ENVIRONMENT = "environment"
SOURCE_DATABASE = "database"
SOURCE_DEFAULT = "default"

#: ``tools``: tool names (``parse_scanned_tools``), a list like ``origins``.
OptionKind = Literal["bool", "choice", "int", "origins", "modules", "tools"]

#: The list kinds: typed in as entries, emptied only from the field.
LIST_KINDS = frozenset({"origins", "modules", "tools"})

#: Bounds on list values an administrator types in, far above any real use.
MAX_LIST_ENTRIES = 50
MAX_LIST_ENTRY_CHARS = 512


@dataclass(frozen=True)
class SecurityOption:
    """One option: a ``Settings`` field an administrator may also set in the UI."""

    key: str
    kind: OptionKind
    #: The category of the option's control (``SECURITY_CATEGORIES``): the
    #: card's sections are the Security audit's categories.
    group: str
    #: The field's name in Settings > Access and security > Security options, which a
    #: Security Audit recommendation uses too (:func:`where_to_set`). The
    #: card's own label table (``SECURITY_OPTION_LABELS``) and the docs table
    #: must say the same: ``tests/test_security_options.py`` compares them.
    label: str
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    #: The one value outside ``minimum`` .. ``maximum`` an ``int`` option also
    #: accepts, which turns it off (``0`` for the idle timeout), or ``None``.
    off_value: int | None = None
    #: Only for a default ``Settings`` resolves itself (``_apply_mode_defaults``).
    default_factory: Callable[[], Any] | None = None
    #: The example an invalid ``origins`` entry is answered with.
    example: str = ""
    #: The value the platform mode forces, whatever the environment or the
    #: database says, or ``None`` where the mode leaves the choice open. Read
    #: on every call, like the mode. Only where the product forbids the choice
    #: - a mode's mere default is ``default_factory`` - and never to weaken a
    #: protection. A Security Audit finding about a pinned option never
    #: recommends changing it (:func:`where_to_set`).
    pinned: Callable[[], Any] | None = None
    #: Why the platform mode pins the value, as a refused write and the
    #: startup warning say it (a lowercase clause).
    pinned_reason: str = ""

    @property
    def env_var(self) -> str:
        return self.key.upper()

    def pinned_value(self) -> Any:
        """The value the platform mode forces now, or ``None``."""
        if self.pinned is None:
            return None
        value = self.pinned()
        return None if value is None else self._coerce(value)

    def default(self) -> Any:
        """The value when neither the environment nor the database sets one: the pinned one, if any."""
        pinned = self.pinned_value()
        if pinned is not None:
            return pinned
        if self.default_factory is not None:
            return self._coerce(self.default_factory())
        return self._coerce(Settings.model_fields[self.key].get_default(call_default_factory=True))

    def from_settings(self) -> Any:
        """``settings.<key>``: the environment value, or the default when unset.

        Read off the module attribute on every call, so a test that patches
        ``settings`` (or one of its fields) is seen as the platform sees it.
        """
        return self._coerce(getattr(_config.settings, self.key))

    def _coerce(self, value: Any) -> Any:
        # The runtime type every caller receives, whichever source it came from.
        if self.kind in ("origins", "tools"):
            return tuple(value or ())
        if self.kind == "modules":
            return value if isinstance(value, frozenset) else parse_allowed_modules(value)
        if self.kind == "choice":
            return str(value or "").strip().lower()
        return value

    def normalize(self, value: Any) -> Any:
        """Validate a value an administrator submitted or the database holds.

        Raises :class:`ValueError` with a sentence fit for a 422. Nothing is
        coerced: a ``"true"`` string is not ``True``, on the settings where
        reading one as the other weakens the platform.
        """
        if self.kind == "bool":
            if isinstance(value, bool):
                return value
            raise ValueError("must be true or false")
        if self.kind == "choice":
            if isinstance(value, str) and value.strip().lower() in self.choices:
                return value.strip().lower()
            raise ValueError(f"must be one of {', '.join(self.choices)}")
        if self.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("must be a whole number")
            if value == self.off_value:
                return value
            if (self.minimum is not None and value < self.minimum) or (
                self.maximum is not None and value > self.maximum
            ):
                if self.off_value is not None:
                    raise ValueError(f"must be {self.off_value} (off), or between {self.minimum} and {self.maximum}")
                raise ValueError(f"must be between {self.minimum} and {self.maximum}")
            return value
        if self.kind == "tools":
            return tuple(parse_scanned_tools(_list_entries(value, max_entries=MAX_SCANNED_TOOLS), self.env_var))
        entries = _list_entries(value)
        if self.kind == "origins":
            return tuple(parse_trusted_origins(entries, self.env_var, self.example))
        invalid = [entry for entry in entries if not all(part.isidentifier() for part in entry.split("."))]
        if invalid:
            raise ValueError(f"{', '.join(repr(entry) for entry in invalid)} is not a Python module name")
        modules = parse_allowed_modules(",".join(entries))
        # The sandboxes drop these from any allow-list (``effective_blocked_modules``):
        # stored, they would show a module as re-enabled that never is.
        from app.services._sandbox_utils import ALWAYS_BLOCKED_MODULES

        escapes = sorted(modules & ALWAYS_BLOCKED_MODULES)
        if escapes:
            raise ValueError(f"{', '.join(escapes)} can never be re-enabled: it would let code leave the sandbox")
        return modules

    def to_wire(self, value: Any) -> Any:
        """The JSON form: the API's and the database's."""
        if self.kind in ("origins", "tools"):
            return list(value)
        if self.kind == "modules":
            return sorted(value)
        return value


def _list_entries(value: Any, *, max_entries: int = MAX_LIST_ENTRIES) -> list[str]:
    if isinstance(value, str):
        raw = value.split(",")
    elif isinstance(value, list | tuple):
        if not all(isinstance(entry, str) for entry in value):
            raise ValueError("must be a list of text entries")
        raw = list(value)
    else:
        raise ValueError("must be a list of text entries")
    entries = [entry.strip() for entry in raw if entry.strip()]
    if len(entries) > max_entries:
        raise ValueError(f"must hold at most {max_entries} entries")
    if any(len(entry) > MAX_LIST_ENTRY_CHARS for entry in entries):
        raise ValueError(f"an entry must be at most {MAX_LIST_ENTRY_CHARS} characters long")
    return entries


def _mode_disables_by_default() -> bool:
    # ``Settings._apply_mode_defaults``: dangerous features are off in XTM One
    # (and ai_disabled) mode and on in Copilot / dev mode.
    return not _config.settings.is_copilot_like


def _closed_in_xtm_one() -> str | None:
    # A license carries a quota (AGENTS.md invariant 17): in XTM One (and
    # ai_disabled, an XTM One platform with AI off), letting agentic work run
    # unmetered while the quota store cannot be read would give away what the
    # licence sells. Copilot and dev mode (``is_copilot_like``, the other half
    # of the modes) keep the choice.
    return "closed" if _config.settings.is_xtm_one else None


SECURITY_OPTIONS: Mapping[str, SecurityOption] = MappingProxyType(
    {
        option.key: option
        for option in (
            SecurityOption("trust_proxy_headers", "bool", "network", "Trust proxy headers"),
            SecurityOption("client_ip_strict", "bool", "network", "Canonical client addresses"),
            SecurityOption(
                "credential_transport_strict",
                "bool",
                "network",
                "Require HTTPS for runner enrollment and MCP tokens",
            ),
            SecurityOption(
                "rate_limit_ipv6_prefix",
                "int",
                "network",
                "IPv6 rate-limit prefix length",
                # The bounds the ``Settings`` validator holds the variable to.
                minimum=32,
                maximum=128,
            ),
            SecurityOption(
                "rate_limit_fail_mode",
                "choice",
                "infrastructure",
                "Rate limits when Redis is unreachable",
                choices=("open", "closed"),
            ),
            SecurityOption(
                "quota_fail_mode",
                "choice",
                "infrastructure",
                "Quotas when their store is unreachable",
                choices=("open", "closed"),
                pinned=_closed_in_xtm_one,
                pinned_reason=(
                    "in XTM One, agentic work is refused while the quota store is unreachable, "
                    "because quotas are part of the licence"
                ),
            ),
            SecurityOption(
                "min_password_length", "int", "authentication", "Minimum password length", minimum=1, maximum=1024
            ),
            SecurityOption(
                "password_reset_expire_hours",
                "int",
                "authentication",
                "Password reset link lifetime (hours)",
                minimum=1,
                maximum=720,
            ),
            SecurityOption(
                "invitation_expire_hours",
                "int",
                "authentication",
                "Invitation link lifetime (hours)",
                minimum=1,
                maximum=720,
            ),
            SecurityOption(
                "jwt_expire_minutes",
                "int",
                "authentication",
                "Absolute session lifetime (minutes)",
                # Five minutes at least from the UI, so an administrator cannot
                # sign everyone out, themselves included, before they can fix it.
                minimum=5,
                maximum=SESSION_MINUTES_MAX,
            ),
            SecurityOption(
                "session_idle_timeout_minutes",
                "int",
                "authentication",
                "Idle session timeout (minutes)",
                minimum=SESSION_IDLE_MINUTES_MIN,
                maximum=SESSION_MINUTES_MAX,
                off_value=0,
            ),
            SecurityOption(
                "api_key_max_lifetime_days",
                "int",
                "authentication",
                "API key maximum lifetime (days)",
                minimum=0,
                maximum=3650,
            ),
            SecurityOption("mcp_dcr_enabled", "bool", "mcp", "MCP client self-registration"),
            SecurityOption("mcp_cimd_enabled", "bool", "mcp", "MCP client metadata documents"),
            SecurityOption("mcp_outbound_dcr_enabled", "bool", "mcp", "Automatic MCP OAuth client registration"),
            SecurityOption("oauth_token_url_strict", "bool", "integrations", "Strict OAuth token URL checks"),
            SecurityOption(
                "oauth_token_dev_trusted_origins",
                "origins",
                "integrations",
                "Loopback token URL origins (development)",
                example="http://localhost:8080",
            ),
            SecurityOption(
                "mcp_server_url_policy",
                "choice",
                "mcp",
                "MCP server URL policy",
                choices=("permissive", "private_networks", "public_only"),
            ),
            SecurityOption(
                "mcp_server_trusted_origins",
                "origins",
                "mcp",
                "Trusted MCP server origins",
                example="http://mcp.internal:8080",
            ),
            SecurityOption(
                "a2a_client_dev_trusted_origins",
                "origins",
                "mcp",
                "Trusted A2A partner origins (development)",
                example="http://localhost:8200",
            ),
            SecurityOption("a2a_allow_insecure_loopback", "bool", "mcp", "Accept plain HTTP from loopback (testing)"),
            SecurityOption("webhook_require_secret", "bool", "assignments", "Require webhook secrets"),
            SecurityOption(
                "webhook_exact_event_matching", "bool", "assignments", "Exact event matching for every trigger"
            ),
            SecurityOption(
                "integration_notifications_require_verification",
                "bool",
                "integrations",
                "Require verified integration notifications",
            ),
            SecurityOption("public_widget_strict_rendering", "bool", "web", "Strict widget rendering"),
            SecurityOption(
                "prompt_guard_block_public_widget", "bool", "agents", "Prompt guards block widget messages"
            ),
            SecurityOption(
                "prompt_guard_block_general_assistant",
                "bool",
                "agents",
                "Prompt guards block General Assistant messages",
            ),
            SecurityOption(
                "prompt_guard_model_output",
                "choice",
                "agents",
                "Prompt guards on model answers",
                choices=("off", "log", "block"),
            ),
            SecurityOption("agent_tools_strict_admin_access", "bool", "access", "Strict admin access for agent tools"),
            SecurityOption("presentation_public_links_enabled", "bool", "access", "Public presentation links"),
            SecurityOption("video_public_links_enabled", "bool", "access", "Public video links"),
            SecurityOption("dashboard_public_links_enabled", "bool", "access", "Published dashboard links"),
            SecurityOption("browser_private_targets", "bool", "tools", "Browser profiles on private networks"),
            SecurityOption("scheduled_work_items_enabled", "bool", "agents", "Scheduled work items"),
            SecurityOption("companion_pin_required", "bool", "authentication", "Require a PIN on the mobile companion"),
            SecurityOption(
                "phone_notification_content",
                "choice",
                "agents",
                "Phone notification content",
                choices=("full", "minimal"),
            ),
            SecurityOption("prompt_guard_scanned_tools", "tools", "agents", "Tools screened by the prompt guard"),
            SecurityOption(
                "prompt_guard_block_tool_results", "bool", "agents", "Withhold tool results a guard rule blocks"
            ),
            SecurityOption("runner_strict_path_confinement", "bool", "tools", "Strict runner folder confinement"),
            SecurityOption(
                "disable_code_interpreter",
                "bool",
                "tools",
                "Disable the code interpreter",
                default_factory=_mode_disables_by_default,
            ),
            SecurityOption(
                "disable_custom_tools",
                "bool",
                "tools",
                "Disable custom tools",
                default_factory=_mode_disables_by_default,
            ),
            SecurityOption(
                "disable_custom_variables",
                "bool",
                "tools",
                "Disable custom variables",
                default_factory=_mode_disables_by_default,
            ),
            SecurityOption(
                "code_interpreter_extra_allowed_modules", "modules", "tools", "Code interpreter: re-enabled modules"
            ),
            SecurityOption(
                "custom_tool_extra_allowed_modules", "modules", "tools", "Custom tools: re-enabled Python modules"
            ),
            SecurityOption(
                "custom_tool_node_permission", "bool", "tools", "Node.js permission model for custom tools"
            ),
        )
    }
)


# ---------------------------------------------------------------------------
# The environment: which options it sets
# ---------------------------------------------------------------------------


def _environment_fields() -> frozenset[str]:
    # What ``Settings`` itself read when ``settings`` was built
    # (``Settings.environment_fields``): never a second read of the files or
    # of ``os.environ``, which the process may change after that build.
    return getattr(_config.settings, "environment_fields", frozenset())


def environment_locked_keys() -> frozenset[str]:
    """The options their variable decides: read-only in Settings > Access and security."""
    return frozenset(key for key in SECURITY_OPTIONS if is_environment_locked(key))


def is_environment_locked(key: str) -> bool:
    """The variable of *key* is set and wins: never over a pin, which leaves it nothing to decide."""
    return key in SECURITY_OPTIONS and key in _environment_fields() and not is_pinned(key)


# ---------------------------------------------------------------------------
# The platform mode: which options it pins
# ---------------------------------------------------------------------------


def is_pinned(key: str) -> bool:
    """The platform mode forces the value of *key* (:attr:`SecurityOption.pinned`)."""
    option = SECURITY_OPTIONS.get(key)
    return option is not None and option.pinned_value() is not None


def lock_reason(key: str) -> str | None:
    """Why *key* is read-only (:data:`SOURCE_PLATFORM_MODE`, :data:`SOURCE_ENVIRONMENT`), ``None`` when editable."""
    if is_pinned(key):
        return SOURCE_PLATFORM_MODE
    if is_environment_locked(key):
        return SOURCE_ENVIRONMENT
    return None


def is_locked(key: str) -> bool:
    """Read-only in Settings > Access and security, a write to it refused: pinned, or decided by its variable."""
    return lock_reason(key) is not None


def ignored_environment_value(key: str) -> Any:
    """What the variable of *key* sets while the platform mode pins another value, else ``None``."""
    option = SECURITY_OPTIONS[key]
    pinned = option.pinned_value()
    if pinned is None or key not in _environment_fields():
        return None
    value = option.from_settings()
    return None if value == pinned else value


def _as_variable(wire: Any) -> str:
    # A value the way the variable is written: ``true``, ``closed``, ``a,b``.
    if isinstance(wire, bool):
        return "true" if wire else "false"
    if isinstance(wire, list):
        return ",".join(wire)
    return str(wire)


# The ignored variables already reported: a second call says nothing.
_reported_ignored: set[tuple[str, str]] = set()


def log_ignored_variables() -> None:
    """Warn once, at startup, about a variable the platform mode overrides (``QUOTA_FAIL_MODE=open`` in XTM One).

    A warning, never a startup error: a deployment that set the variable
    before the pin existed keeps booting, and is told it no longer applies.
    """
    for key, option in SECURITY_OPTIONS.items():
        value = ignored_environment_value(key)
        if value is None:
            continue
        written = _as_variable(option.to_wire(value))
        if (key, written) in _reported_ignored:
            continue
        _reported_ignored.add((key, written))
        logger.warning(
            "%s=%s has no effect: %s. Remove it from the environment.",
            option.env_var,
            written,
            option.pinned_reason,
        )


# ---------------------------------------------------------------------------
# The stored values: this process's snapshot
# ---------------------------------------------------------------------------

_stored: Mapping[str, Any] = MappingProxyType({})
_stored_loaded = False
# Every read of the database and every write takes a ticket, in the order it
# starts; the snapshot only moves forward (see ``publish_stored``).
_last_ticket = 0
_published_ticket = 0
# A stored value that does not validate is reported once, not on every poll.
_reported_invalid: set[tuple[str, str]] = set()


def normalize_stored(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep the valid values of a stored mapping, normalized.

    A key no option has (an option a later release added, or one since
    removed) is ignored, and so is a value that no longer validates: that
    option then falls back to the environment / default rather than failing
    every request that reads it.
    """
    values: dict[str, Any] = {}
    if not isinstance(raw, Mapping):
        return values
    for key, value in raw.items():
        option = SECURITY_OPTIONS.get(key)
        if option is None:
            continue
        try:
            values[key] = option.normalize(value)
        except ValueError as exc:
            reported = (key, repr(value))
            if reported not in _reported_invalid:
                _reported_invalid.add(reported)
                logger.warning(
                    "Ignoring the stored value of the security option %s (%s); "
                    "the environment or the default applies until an administrator saves it again",
                    key,
                    exc,
                )
    return values


def snapshot_ticket() -> int:
    """A ticket for a read of the stored values about to start, or a write just committed."""
    global _last_ticket
    _last_ticket += 1
    return _last_ticket


def publish_stored(values: Mapping[str, Any], *, ticket: int | None = None) -> bool:
    """Replace this process's snapshot with already normalized values.

    One reference swap: a reader sees the previous snapshot or the next,
    never a mix. *ticket* is the one :func:`snapshot_ticket` gave the read
    before it started; the values are published only when nothing newer was
    - a poll that read before an administrator's write committed must not
    put the old values back after the write published the new ones. False
    when they were older and dropped.
    """
    global _stored, _stored_loaded, _published_ticket
    if ticket is None:
        ticket = snapshot_ticket()
    if ticket <= _published_ticket:
        return False
    _published_ticket = ticket
    _stored = MappingProxyType(dict(values))
    _stored_loaded = True
    return True


def reset_snapshot() -> None:
    """Back to "nothing read yet" (the environment / default applies), for tests."""
    global _stored, _stored_loaded, _published_ticket
    _stored = MappingProxyType({})
    _stored_loaded = False
    _published_ticket = _last_ticket
    _reported_invalid.clear()
    _reported_ignored.clear()


def snapshot_loaded() -> bool:
    """Whether this process has read the stored values at least once."""
    return _stored_loaded


def stored_values() -> Mapping[str, Any]:
    """The normalized values in this process's snapshot."""
    return _stored


# ---------------------------------------------------------------------------
# The resolver
# ---------------------------------------------------------------------------


def security_option(key: str) -> Any:
    """The effective value of a security option: pin, environment, stored value, default.

    Synchronous and database-free, so it is safe on every hot path. A key
    that is not registered raises ``KeyError``: that is a programming error.
    """
    option = SECURITY_OPTIONS[key]
    pinned = option.pinned_value()
    if pinned is not None:
        return pinned
    if key not in _environment_fields():
        stored = _stored
        if key in stored:
            return stored[key]
    return option.from_settings()


def security_option_source(key: str) -> str:
    """Where :func:`security_option` takes the value from."""
    if key not in SECURITY_OPTIONS:
        raise KeyError(key)
    if is_pinned(key):
        return SOURCE_PLATFORM_MODE
    if key in _environment_fields():
        return SOURCE_ENVIRONMENT
    if key in _stored:
        return SOURCE_DATABASE
    return SOURCE_DEFAULT


#: Where the options are, as a recommendation names it.
SECURITY_OPTIONS_PATH = "Settings > Access and security > Security options"


def option_reference(key: str) -> str:
    """An option as a Security Audit finding names it: ``"<label>" (<VARIABLE>)``.

    Both of its spellings, since either may be the one in force: the field in
    Settings > Access and security > Security options and the variable that wins over it.
    """
    option = SECURITY_OPTIONS[key]
    return f'"{option.label}" ({option.env_var})'


def where_to_set(key: str, env_value: str, *, sentence_start: bool = False) -> str:
    """How an administrator gives an option a value, for a Security Audit recommendation.

    *env_value* is the value as the variable is written (``true``, ``closed``,
    ``12``); an empty one empties a list option, which only its field can do,
    since a blank variable counts as unset. Names the field the way the card
    does (:attr:`SecurityOption.label`), and the variable as the alternative
    that wins - or only the variable when it is set, since it then pins the
    field. The Security audit links such a finding to the field
    (``SECURITY_OPTION_FINDINGS`` in the frontend). Never for an option the
    platform mode pins (:func:`is_pinned`): nothing an administrator sets
    changes it.
    """
    option = SECURITY_OPTIONS[key]
    name = f'"{option.label}"'
    locked = is_environment_locked(key)
    if option.kind in LIST_KINDS and not env_value:
        text = (
            f"unset {option.env_var} and restart (the variable locks the {name} field in "
            f"{SECURITY_OPTIONS_PATH}), and leave that field empty"
            if locked
            else f"empty the {name} field in {SECURITY_OPTIONS_PATH} (and leave {option.env_var} unset)"
        )
    elif locked:
        text = (
            f"set {option.env_var}={env_value} and restart (the variable is set, so the "
            f"{name} field in {SECURITY_OPTIONS_PATH} is locked)"
        )
    else:
        if option.kind == "bool":
            state = "on" if env_value.strip().lower() in {"true", "1", "yes", "on"} else "off"
            action = f"turn {state} {name} in {SECURITY_OPTIONS_PATH}"
        else:
            action = f"set {name} in {SECURITY_OPTIONS_PATH} to {env_value}"
        text = f"{action} (or set {option.env_var}={env_value} and restart: the variable wins and locks the field)"
    return text[0].upper() + text[1:] if sentence_start else text


def describe_security_options() -> list[dict[str, Any]]:
    """Every option with its value, default, source and lock, for the API.

    ``stored`` stays what the database holds, applied or not: a pinned option
    keeps its stored value for the mode that lets it apply.
    """
    described = []
    stored = _stored
    for key, option in SECURITY_OPTIONS.items():
        reason = lock_reason(key)
        described.append(
            {
                "key": key,
                "env_var": option.env_var,
                "kind": option.kind,
                "group": option.group,
                "choices": list(option.choices),
                "minimum": option.minimum,
                "maximum": option.maximum,
                "off_value": option.off_value,
                "value": option.to_wire(security_option(key)),
                "default": option.to_wire(option.default()),
                "stored": option.to_wire(stored[key]) if key in stored else None,
                "source": security_option_source(key),
                "locked": reason is not None,
                "locked_reason": reason,
            }
        )
    return described
