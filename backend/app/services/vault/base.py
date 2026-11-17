"""What every vault provider implements."""

from __future__ import annotations

import abc
import ipaddress
import pathlib
import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.services.vault.config import WRITERS_ADMINS, VaultConfigError, VaultProviderConfig
from app.services.vault.errors import VaultReadError

#: A secret as a vault holds it: one value, or entries by key.
SecretPayload = str | dict[str, Any]

#: How many names one listing page holds at most.
MAX_PAGE_SIZE = 200


@dataclass(frozen=True)
class ListedSecret:
    """One name a vault listed: a secret, or a folder (a path prefix) holding more."""

    #: The last segment, as the picker shows it (``github``, ``prod/``).
    name: str
    #: The reference path: the secret's, or the folder's with a trailing ``/``.
    path: str
    folder: bool = False


@dataclass(frozen=True)
class SecretListing:
    entries: list[ListedSecret] = field(default_factory=list)
    #: Opaque: hand it back to get the next page; ``None`` on the last one.
    next_page_token: str | None = None

#: How long one request to a vault may take.
REQUEST_TIMEOUT_SECONDS = 10.0
#: How long a failed read, or a provider's failed login, is answered from memory before it is tried again.
FAILURE_RETRY_SECONDS = 15.0


class CredentialBackoff:
    """A provider's failed login (a token request, an AppRole login), answered from memory for :data:`FAILURE_RETRY_SECONDS`.

    The reads waiting on the provider's login lock would otherwise each try
    again, one after another: with a vault that does not answer, the N-th
    secret of a load waits N request timeouts instead of failing at once.
    """

    def __init__(self) -> None:
        self._failure: VaultReadError | None = None
        self._retry_at = 0.0

    def check(self) -> None:
        """Raise the remembered failure while it holds."""
        if self._failure is not None and time.monotonic() < self._retry_at:
            raise VaultReadError(self._failure.reason, self._failure.detail)

    def failed(self, exc: VaultReadError) -> None:
        self._failure, self._retry_at = exc, time.monotonic() + FAILURE_RETRY_SECONDS

    def succeeded(self) -> None:
        self._failure = None


class VaultProvider(abc.ABC):
    """One declared vault: reads a secret by its path, holds its own credentials in memory only."""

    kind: str = ""

    def __init__(self, config: VaultProviderConfig) -> None:
        self.config = config
        self.name = config.name

    @abc.abstractmethod
    async def read(self, path: str) -> SecretPayload:
        """The secret at *path*; :class:`VaultReadError` when it cannot be read."""

    async def authenticate(self) -> None:
        """Obtain the platform's credentials for this vault (a login, a token); :class:`VaultReadError` when refused."""

    async def list_secrets(self, prefix: str, *, page_token: str | None = None, page_size: int = 100) -> SecretListing:
        """The secret names and folders directly under *prefix* (``""``: the top), never a value.

        :class:`VaultReadError` when the vault refuses the listing (the picker then falls back to typing a path).
        """
        raise VaultReadError("access_denied", f"the {self.kind or 'vault'} provider does not list its secrets")

    def path_problem(self, path: str) -> str | None:
        """Why *path* can never name a secret in this vault, checked when a reference is written and read."""
        return None

    def entry_labels(self, path: str) -> dict[str, str]:
        """How the picker shows the keys of the secret at *path*, by key, when a key is not readable as is.

        Filled by the last :meth:`read` of *path*; never a value.
        """
        return {}

    def describe(self) -> dict[str, str]:
        """What may be shown about this provider: its type, where it is and its policy, never a credential."""
        details = {"name": self.name, "type": self.kind}
        if not self.config.from_database:
            details["writers"] = (self.config.get("WRITERS") or WRITERS_ADMINS).lower()
            if self.config.get("PATHS"):
                details["paths"] = self.config.get("PATHS")
        return details

    async def aclose(self) -> None:
        """Release what the provider holds (an HTTP client)."""


def read_setting_or_file(config: VaultProviderConfig, setting: str) -> str:
    """A credential: a database provider's stored one, or given inline (``<SETTING>``) or in a mounted file (``<SETTING>_FILE``).

    Re-read on every login so a file a sidecar rotates (a Vault Agent sink, a
    Kubernetes secret) is picked up without a restart.
    """
    if config.from_database:
        return config.secret(setting)
    path = config.get(f"{setting}_FILE")
    if path:
        try:
            return pathlib.Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise VaultReadError(
                "misconfigured", f"{config.variable(setting + '_FILE')} cannot be read: {exc.strerror}"
            ) from None
    return config.get(setting)


def service_address(config: VaultProviderConfig, setting: str) -> str:
    """The http(s) address *setting* declares, without a trailing slash; :class:`VaultConfigError` otherwise.

    The address is shown (``describe()``, the startup log, the Security Audit),
    so one carrying credentials (``https://user:pw@host``), a query or a
    fragment is refused rather than masked. The error names the variable,
    never its value.
    """
    value = config.get(setting).rstrip("/")
    variable = config.variable(setting)
    try:
        parts = urlsplit(value)
    except ValueError:
        raise VaultConfigError(f"{variable} must be an http(s) URL") from None
    try:
        parts.port  # noqa: B018 - raises on a port that is not a number
    except ValueError:
        raise VaultConfigError(f"{variable} has an invalid port") from None
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise VaultConfigError(f"{variable} must be an http(s) URL")
    if parts.username is not None or parts.password is not None:
        raise VaultConfigError(f"{variable} must not carry credentials")
    if parts.query or parts.fragment:
        raise VaultConfigError(f"{variable} must not carry a query or a fragment")
    return value


def plain_http_off_loopback(address: str) -> bool:
    """Whether a provider's *address* is plain HTTP to another host: its secrets and credentials cross the network in clear."""
    parts = urlsplit(address)
    if parts.scheme != "http":
        return False
    host = (parts.hostname or "").lower()
    if host == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


def tls_verify(config: VaultProviderConfig) -> bool | ssl.SSLContext:
    """TLS verification for a provider's HTTP client: always on, against ``CA_CERT_FILE`` or a pasted ``CA_CERT`` when given.

    A CA that cannot be read or holds no certificate is the provider's
    misconfiguration (:class:`VaultReadError`), reported like any other.
    """
    ca_pem = config.get("CA_CERT") if config.from_database else ""
    if ca_pem:
        try:
            return ssl.create_default_context(cadata=ca_pem)
        except (ssl.SSLError, ValueError) as exc:
            raise VaultReadError(
                "misconfigured", f"{config.variable('CA_CERT')} is not a PEM certificate: {type(exc).__name__}"
            ) from None
    ca_file = config.get("CA_CERT_FILE")
    if not ca_file:
        return True
    try:
        return ssl.create_default_context(cafile=ca_file)
    except OSError as exc:
        raise VaultReadError(
            "misconfigured", f"{config.variable('CA_CERT_FILE')} cannot be used: {type(exc).__name__}"
        ) from None


def entry_names(payload: SecretPayload) -> list[str]:
    """The key names a secret holds (a KV secret, a JSON object), never a value; ``[]`` for a single value."""
    import json

    entries: Any = payload
    if isinstance(payload, str):
        try:
            entries = json.loads(payload)
        except ValueError:
            return []
    if not isinstance(entries, dict):
        return []
    return sorted(str(key) for key in entries)


def child_entries(prefix: str, names: list[str]) -> list[ListedSecret]:
    """The entries directly under *prefix* of a vault whose names are flat paths (``prod/db/main``): secrets and folders."""
    base = prefix.strip("/")
    base = f"{base}/" if base else ""
    seen: dict[str, ListedSecret] = {}
    for full in names:
        if not full.startswith(base) or full == base:
            continue
        rest = full[len(base):]
        head, slash, _ = rest.partition("/")
        if not head:
            continue
        if slash:
            seen.setdefault(f"{base}{head}/", ListedSecret(name=f"{head}/", path=f"{base}{head}/", folder=True))
        else:
            seen[f"{base}{head}"] = ListedSecret(name=head, path=f"{base}{head}")
    return sorted(seen.values(), key=lambda entry: (not entry.folder, entry.name.lower()))


def page_entries(entries: list[ListedSecret], page_token: str | None, page_size: int) -> SecretListing:
    """One page of a listing the vault answers whole: the token is the offset of the next page."""
    try:
        start = max(int(page_token or 0), 0)
    except ValueError:
        start = 0
    size = min(max(page_size, 1), MAX_PAGE_SIZE)
    end = start + size
    return SecretListing(entries=entries[start:end], next_page_token=str(end) if end < len(entries) else None)


def http_failure(response: httpx.Response) -> VaultReadError:
    """The reason a vault's HTTP answer refused a read, with its status for the log."""
    return status_failure(response.status_code)


def status_failure(status: int) -> VaultReadError:
    """The reason an HTTP status refusing a read stands for, with the status for the log."""
    if status == 404:
        return VaultReadError("not_found", "HTTP 404")
    if status in (401, 403):
        return VaultReadError("access_denied", f"HTTP {status}")
    if status == 400:
        return VaultReadError("invalid_reference", "HTTP 400")
    return VaultReadError("unreachable", f"HTTP {status}")


def network_failure(exc: Exception) -> VaultReadError:
    return VaultReadError("unreachable", type(exc).__name__)
