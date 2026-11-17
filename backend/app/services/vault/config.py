"""The vault providers this deployment knows: declared in the environment, or connected in the UI.

A provider is declared by the operator in the environment::

    SECRET_VAULT_<NAME>_TYPE=hashicorp|aws|gcp|azure|onepassword
    SECRET_VAULT_<NAME>_<SETTING>=...

or connected by an administrator in Settings > Access and security > Secret vaults,
stored in ``secret_vault_providers``. Either way ``<NAME>``
(lowercased) is the name references use: ``vault://<name>/...``, and an
environment declaration wins over a database provider of the same name.

An environment provider's credentials come from the environment or the
workload identity (a Vault token or AppRole secret id in a variable or a
mounted file, an IAM role, a GCP service account, an Azure managed identity).
A database provider's come from its encrypted ``credentials_enc`` or the
workload identity. Which settings each type reads is
:data:`PROVIDER_SETTINGS` (environment) and :data:`DATABASE_SETTINGS` /
:data:`DATABASE_CREDENTIALS` (database), plus the :data:`POLICY_SETTINGS`
every environment provider reads (:class:`VaultPolicy`); a database
provider's policy is its grants (:class:`GrantPolicy`).

Read once per process, after ``main._load_dotenv_files`` put the ``.env``
files in ``os.environ`` (like ``LLM_PROVIDER_*``).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal

from app.core.secret_refs import PROVIDER_NAME_PATTERN

ENV_PREFIX = "SECRET_VAULT_"
_TYPE_VAR_RE = re.compile(rf"^{ENV_PREFIX}(?P<name>[A-Z][A-Z0-9_]*)_TYPE$")
_NAME_RE = re.compile(PROVIDER_NAME_PATTERN)

PROVIDER_TYPES = ("hashicorp", "aws", "gcp", "azure", "onepassword")

SOURCE_ENVIRONMENT = "environment"
SOURCE_DATABASE = "database"

#: The settings each provider type reads (``SECRET_VAULT_<NAME>_<SETTING>``).
PROVIDER_SETTINGS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "hashicorp": frozenset(
            {
                "ADDRESS",
                "NAMESPACE",
                "AUTH_METHOD",
                "AUTH_MOUNT",
                "TOKEN",
                "TOKEN_FILE",
                "ROLE_ID",
                "SECRET_ID",
                "SECRET_ID_FILE",
                "ROLE",
                "JWT_FILE",
                "KV_VERSION",
                "KV_MOUNT",
                "CA_CERT_FILE",
            }
        ),
        "aws": frozenset({"REGION", "ENDPOINT_URL"}),
        "gcp": frozenset({"PROJECT"}),
        "azure": frozenset({"VAULT_URL", "CLIENT_ID", "TENANT_ID"}),
        "onepassword": frozenset({"ADDRESS", "TOKEN", "TOKEN_FILE", "CA_CERT_FILE"}),
    }
)

#: The settings a database provider of each type stores in clear (``settings``): nothing that opens the vault,
#: and no file path - a path would let the UI make the platform read a file of its own and send it to the vault.
DATABASE_SETTINGS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "hashicorp": frozenset(
            {
                "ADDRESS",
                "NAMESPACE",
                "AUTH_METHOD",
                "AUTH_MOUNT",
                "ROLE_ID",
                "KV_VERSION",
                "KV_MOUNT",
                "CA_CERT",
            }
        ),
        "aws": frozenset({"REGION", "ENDPOINT_URL", "AUTH_METHOD", "ROLE_ARN", "EXTERNAL_ID", "ACCESS_KEY_ID"}),
        "gcp": frozenset({"PROJECT", "AUTH_METHOD"}),
        "azure": frozenset({"VAULT_URL", "AUTH_METHOD", "CLIENT_ID", "TENANT_ID"}),
        "onepassword": frozenset({"ADDRESS", "AUTH_METHOD", "CA_CERT"}),
    }
)

#: The settings that open the vault, which a database provider stores encrypted (``credentials_enc``) and never serves.
DATABASE_CREDENTIALS: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "hashicorp": frozenset({"TOKEN", "SECRET_ID"}),
        "aws": frozenset({"SECRET_ACCESS_KEY"}),
        "gcp": frozenset({"SERVICE_ACCOUNT_KEY"}),
        "azure": frozenset({"CLIENT_SECRET"}),
        "onepassword": frozenset({"TOKEN"}),
    }
)

#: Settings every environment provider reads: who may write a reference to it, and the paths it may be read at.
POLICY_SETTINGS = frozenset({"WRITERS", "PATHS"})

#: Settings that open the vault: read from the environment, never shown or stored.
SECRET_SETTINGS = frozenset({"TOKEN", "SECRET_ID"})

WRITERS_ADMINS = "admins"
WRITERS_ALL = "all"
WRITERS_VALUES = (WRITERS_ADMINS, WRITERS_ALL)

GrantPrincipal = Literal["everyone", "group", "user"]
GRANT_PRINCIPALS: tuple[GrantPrincipal, ...] = ("everyone", "group", "user")


class VaultConfigError(ValueError):
    """A provider declaration the platform cannot use; the message names the variable or field to fix."""


PathPrefix = tuple[str, ...]


def parse_path_prefixes(entries: Iterable[str], label: str) -> tuple[PathPrefix, ...]:
    """Path prefixes as segments; :class:`VaultConfigError` naming *label* for one that is not a prefix."""
    prefixes: list[PathPrefix] = []
    for entry in entries:
        prefix = str(entry).strip().strip("/")
        if not prefix:
            continue
        segments = tuple(prefix.split("/"))
        if any(not segment or segment in (".", "..") for segment in segments):
            raise VaultConfigError(f"{label} holds an entry that is not a path prefix (an empty, . or .. segment)")
        if segments not in prefixes:
            prefixes.append(segments)
    return tuple(prefixes)


def path_within(path: str, prefixes: Iterable[PathPrefix]) -> bool:
    """Whether *path* is under one of *prefixes*, segment by segment (``a/b`` admits ``a/b/c``, never ``a/bc``)."""
    segments = tuple(path.strip("/").split("/"))
    return any(segments[: len(prefix)] == prefix for prefix in prefixes)


@dataclass(frozen=True)
class VaultPrincipal:
    """Who is using a vault: what decides which providers and paths they may name."""

    is_admin: bool
    user_id: str | None = None
    group_ids: frozenset[str] = frozenset()


#: The platform itself, or an administrator: every path of every usable provider.
ADMIN_PRINCIPAL = VaultPrincipal(is_admin=True)


@dataclass(frozen=True)
class VaultGrant:
    """One grant of a database provider: who, and the path prefixes it covers (none: every path)."""

    principal: GrantPrincipal
    id: str | None = None
    paths: tuple[PathPrefix, ...] = ()

    def covers(self, who: VaultPrincipal) -> bool:
        if self.principal == "everyone":
            return True
        if self.principal == "group":
            return self.id is not None and self.id in who.group_ids
        return self.id is not None and self.id == who.user_id

    def allows_path(self, path: str) -> bool:
        return not self.paths or path_within(path, self.paths)

    def to_json(self) -> dict[str, Any]:
        return {"principal": self.principal, "id": self.id, "paths": ["/".join(prefix) for prefix in self.paths]}


def parse_grants(raw: Any) -> tuple[VaultGrant, ...]:
    """The grants a stored (or submitted) list declares; :class:`VaultConfigError` for one it cannot use."""
    if raw in (None, ""):
        return ()
    if not isinstance(raw, list):
        raise VaultConfigError("grants is a list")
    grants: list[VaultGrant] = []
    for index, item in enumerate(raw):
        label = f"grants[{index}]"
        if not isinstance(item, Mapping):
            raise VaultConfigError(f"{label} is an object")
        principal = str(item.get("principal") or "").strip().lower()
        if principal not in GRANT_PRINCIPALS:
            raise VaultConfigError(f"{label}.principal is one of {', '.join(GRANT_PRINCIPALS)}")
        ident = item.get("id")
        if principal == "everyone":
            ident = None
        elif not isinstance(ident, str) or not ident.strip():
            raise VaultConfigError(f"{label}.id names the {principal}")
        paths = item.get("paths") or []
        if not isinstance(paths, list) or not all(isinstance(path, str) for path in paths):
            raise VaultConfigError(f"{label}.paths is a list of path prefixes")
        grant = VaultGrant(
            principal=principal,  # type: ignore[arg-type]
            id=ident.strip() if isinstance(ident, str) else None,
            paths=parse_path_prefixes(paths, f"{label}.paths"),
        )
        if any(existing.principal == grant.principal and existing.id == grant.id for existing in grants):
            raise VaultConfigError(f"{label} repeats a grant: give one grant per group or user, with all its paths")
        grants.append(grant)
    return tuple(grants)


@dataclass(frozen=True)
class VaultProviderConfig:
    name: str
    type: str
    # Out of repr(): an environment provider's settings hold its credentials (``SECRET_SETTINGS``).
    settings: Mapping[str, str] = field(default_factory=dict, repr=False)
    source: str = SOURCE_ENVIRONMENT
    #: A database provider's own credentials (``DATABASE_CREDENTIALS``), decrypted in memory only.
    credentials: Mapping[str, str] = field(default_factory=dict, repr=False)
    #: A database provider's row: its id, label, grants and switches.
    id: str | None = None
    display_name: str = ""
    enabled: bool = True
    grants: tuple[VaultGrant, ...] = ()
    browsable: bool = False
    cache_seconds: float | None = None

    @property
    def from_database(self) -> bool:
        return self.source == SOURCE_DATABASE

    def get(self, setting: str, default: str = "") -> str:
        return self.settings.get(setting, default)

    def secret(self, setting: str) -> str:
        """A credential: the database provider's stored one, else the (environment) setting."""
        return self.credentials.get(setting) or self.settings.get(setting, "")

    def variable(self, setting: str) -> str:
        """Where *setting* is given, for a message: the environment variable, or the field of a database provider."""
        if self.from_database:
            return f"the {setting.lower()} setting of vault provider '{self.name}'"
        return f"{ENV_PREFIX}{self.name.upper()}_{setting}"


@dataclass(frozen=True)
class VaultPolicy:
    """Who may write a reference to an environment provider, and the paths references to it may read.

    A reference is resolved with the platform's own vault identity, for whoever
    the field it sits in serves, and the integration or MCP server holding it
    may send it wherever that entity points. So by default only administrators
    write references (``WRITERS=admins``); ``all`` lets anyone who may edit a
    secret field write one. ``PATHS`` narrows the provider to a list of path
    prefixes, matched segment by segment: ``xtm-one`` admits ``xtm-one`` and
    ``xtm-one/github``, never ``xtm-one-staging/github``. It is checked when a
    reference is written and again when it is read.
    """

    writers: str = WRITERS_ADMINS
    paths: tuple[PathPrefix, ...] = ()

    def usable_by(self, who: VaultPrincipal) -> bool:
        return who.is_admin or self.writers == WRITERS_ALL

    def allows_path(self, path: str) -> bool:
        """Whether a reference to *path* may be read at all, whoever wrote it (checked on every read)."""
        return not self.paths or path_within(path, self.paths)

    def admits(self, who: VaultPrincipal, path: str) -> bool:
        return self.usable_by(who) and self.allows_path(path)

    def browsable_by(self, who: VaultPrincipal) -> bool:
        return who.is_admin

    def prefixes_for(self, who: VaultPrincipal) -> tuple[PathPrefix, ...] | None:
        """The path prefixes *who* may name (``None``: every path), or ``()`` when none."""
        if not self.usable_by(who):
            return ()
        return self.paths or None

    @property
    def open_to_everyone_everywhere(self) -> bool:
        return self.writers == WRITERS_ALL and not self.paths


@dataclass(frozen=True)
class GrantPolicy:
    """Who may use a database provider and at which paths: administrators always, then its grants.

    Grants are checked when a reference is written, when an entity holding one
    is re-pointed, and in every picker; a disabled provider is usable by nobody
    and every read of it fails.
    """

    grants: tuple[VaultGrant, ...] = ()
    browsable: bool = False
    enabled: bool = True

    def _covering(self, who: VaultPrincipal) -> list[VaultGrant]:
        return [grant for grant in self.grants if grant.covers(who)]

    def usable_by(self, who: VaultPrincipal) -> bool:
        return self.enabled and (who.is_admin or bool(self._covering(who)))

    def allows_path(self, path: str) -> bool:
        return True

    def admits(self, who: VaultPrincipal, path: str) -> bool:
        if not self.enabled:
            return False
        return who.is_admin or any(grant.allows_path(path) for grant in self._covering(who))

    def browsable_by(self, who: VaultPrincipal) -> bool:
        return self.usable_by(who) and (who.is_admin or self.browsable)

    def prefixes_for(self, who: VaultPrincipal) -> tuple[PathPrefix, ...] | None:
        if not self.enabled:
            return ()
        if who.is_admin:
            return None
        covering = self._covering(who)
        if any(not grant.paths for grant in covering):
            return None
        return tuple(prefix for grant in covering for prefix in grant.paths)

    @property
    def open_to_everyone_everywhere(self) -> bool:
        return self.enabled and any(grant.principal == "everyone" and not grant.paths for grant in self.grants)


#: The policy of a provider that is not usable: references to it are only an administrator's to write.
DEFAULT_POLICY = VaultPolicy()

Policy = VaultPolicy | GrantPolicy


def provider_policy(config: VaultProviderConfig) -> Policy:
    """The policy of *config*; :class:`VaultConfigError` for a value it cannot use."""
    if config.from_database:
        return GrantPolicy(grants=config.grants, browsable=config.browsable, enabled=config.enabled)
    writers = (config.get("WRITERS") or WRITERS_ADMINS).lower()
    if writers not in WRITERS_VALUES:
        raise VaultConfigError(f"{config.variable('WRITERS')} is one of {', '.join(WRITERS_VALUES)}")
    return VaultPolicy(writers=writers, paths=parse_path_prefixes(config.get("PATHS").split(","), config.variable("PATHS")))


@dataclass(frozen=True)
class VaultDeclarations:
    """What the environment declares: the usable providers and the declarations refused, by name."""

    providers: Mapping[str, VaultProviderConfig]
    errors: Mapping[str, str]


def read_declarations(environ: Mapping[str, str] | None = None) -> VaultDeclarations:
    """Every ``SECRET_VAULT_<NAME>_TYPE`` declaration, with the settings of its type."""
    env = os.environ if environ is None else environ
    providers: dict[str, VaultProviderConfig] = {}
    errors: dict[str, str] = {}
    for variable, raw_type in sorted(env.items()):
        match = _TYPE_VAR_RE.match(variable)
        if not match:
            continue
        name = match["name"].lower()
        kind = (raw_type or "").strip().lower()
        if not _NAME_RE.fullmatch(name):
            errors[name] = f"{variable}: a provider name is a letter then up to 31 letters, digits or _"
            continue
        if kind not in PROVIDER_SETTINGS:
            # The value is never repeated: it is logged and shown in the Security Audit.
            errors[name] = f"{variable} is not a known type: it is one of {', '.join(sorted(PROVIDER_SETTINGS))}"
            continue
        prefix = f"{ENV_PREFIX}{match['name']}_"
        settings = {
            setting: value.strip()
            for setting in PROVIDER_SETTINGS[kind] | POLICY_SETTINGS
            if (value := env.get(prefix + setting)) is not None and value.strip()
        }
        providers[name] = VaultProviderConfig(name=name, type=kind, settings=MappingProxyType(settings))
    return VaultDeclarations(providers=MappingProxyType(providers), errors=MappingProxyType(errors))


def database_config(
    *,
    name: str,
    type: str,
    settings: Mapping[str, Any] | None,
    credentials: Mapping[str, Any] | None,
    id: str | None = None,
    display_name: str = "",
    enabled: bool = True,
    grants: Any = None,
    browsable: bool = False,
    cache_seconds: int | None = None,
) -> VaultProviderConfig:
    """A database provider's configuration from its row (or a draft); :class:`VaultConfigError` for one it cannot use.

    Settings and credentials are keyed by lowercase setting name; each type
    keeps only the ones it reads (:data:`DATABASE_SETTINGS`,
    :data:`DATABASE_CREDENTIALS`).
    """
    if not _NAME_RE.fullmatch(name or ""):
        raise VaultConfigError("name is a lowercase letter then up to 31 lowercase letters, digits or _")
    if type not in DATABASE_SETTINGS:
        raise VaultConfigError(f"type is one of {', '.join(PROVIDER_TYPES)}")
    if cache_seconds is not None and (not isinstance(cache_seconds, int) or isinstance(cache_seconds, bool) or not 0 <= cache_seconds <= 86400):
        raise VaultConfigError("cache_seconds is a whole number of seconds between 0 and 86400")

    def _keep(values: Mapping[str, Any] | None, allowed: frozenset[str]) -> MappingProxyType[str, str]:
        kept: dict[str, str] = {}
        for key, value in (values or {}).items():
            setting = str(key).upper()
            if setting in allowed and value is not None and str(value).strip():
                kept[setting] = str(value).strip() if setting not in ("CA_CERT", "SERVICE_ACCOUNT_KEY") else str(value)
        return MappingProxyType(kept)

    return VaultProviderConfig(
        name=name,
        type=type,
        settings=_keep(settings, DATABASE_SETTINGS[type]),
        credentials=_keep(credentials, DATABASE_CREDENTIALS[type]),
        source=SOURCE_DATABASE,
        id=id,
        display_name=display_name or name,
        enabled=enabled,
        grants=parse_grants(grants),
        browsable=browsable,
        cache_seconds=float(cache_seconds) if cache_seconds is not None else None,
    )
