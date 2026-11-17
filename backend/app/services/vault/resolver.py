"""Resolving vault references: the providers, the in-memory cache and its invalidation.

One resolver per process, built from the environment on first use; the
providers an administrator connected in the UI are applied on top by
:meth:`SecretResolver.apply_database_providers` (``services/vault/store.py``,
at startup, after each change, and every few seconds in every process), and an
environment declaration wins over a database provider of the same name. A
secret read from a vault is kept in memory for ``SECRET_VAULT_CACHE_SECONDS``
(or its provider's own cache duration) and then read again, so a rotation in
the vault is picked up within that time without a restart; :func:`invalidate`
drops it at once (an upstream service refused the credential). Concurrent
reads of one secret share one request, and a failed read - or a ``#key`` the
secret does not hold - is not retried for :data:`FAILURE_RETRY_SECONDS`, so a
broken reference does not hammer the vault on every call.

Values live in this process's memory only, and only for the cache time: each
entry is dropped when it expires, whether or not it is read again. They are
never written anywhere, never logged. Logs name the provider, the path, the key
and the vault's reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.secret_refs import SecretReference
from app.services.vault.base import (
    FAILURE_RETRY_SECONDS,
    SecretListing,
    SecretPayload,
    VaultProvider,
    plain_http_off_loopback,
)
from app.services.vault.config import (
    DEFAULT_POLICY,
    SOURCE_DATABASE,
    SOURCE_ENVIRONMENT,
    Policy,
    VaultConfigError,
    VaultDeclarations,
    VaultPrincipal,
    VaultProviderConfig,
    provider_policy,
    read_declarations,
)
from app.services.vault.errors import VaultReadError

logger = logging.getLogger(__name__)

#: How many recently logged refusals are remembered before the expired ones are dropped.
_MAX_LOGGED_REFUSALS = 1024
#: How many references whose key could not be selected are remembered at once.
_MAX_SELECTION_FAILURES = 1024


def build_provider(config: VaultProviderConfig) -> VaultProvider:
    """The provider *config* declares; :class:`VaultConfigError` naming what to fix when it cannot be used."""
    return _build_provider(config)


def _fingerprint(config: VaultProviderConfig) -> str:
    """What a database provider's client is built from: a change rebuilds it (its grants are only its policy)."""
    material = json.dumps(
        [config.type, sorted(config.settings.items()), sorted(config.credentials.items()), config.enabled],
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


@dataclass
class _DatabaseEntry:
    config: VaultProviderConfig = field(repr=False)
    fingerprint: str


def _build_provider(config: VaultProviderConfig) -> VaultProvider:
    if config.type == "hashicorp":
        from app.services.vault.hashicorp import HashiCorpVaultProvider

        return HashiCorpVaultProvider(config)
    if config.type == "aws":
        from app.services.vault.aws import AWSSecretsManagerProvider

        return AWSSecretsManagerProvider(config)
    if config.type == "gcp":
        from app.services.vault.gcp import GoogleSecretManagerProvider

        return GoogleSecretManagerProvider(config)
    if config.type == "azure":
        from app.services.vault.azure import AzureKeyVaultProvider

        return AzureKeyVaultProvider(config)
    if config.type == "onepassword":
        from app.services.vault.onepassword import OnePasswordVaultProvider

        return OnePasswordVaultProvider(config)
    raise VaultConfigError(f"unknown vault provider type {config.type!r}")


@dataclass
class _Entry:
    payload: SecretPayload | None = field(repr=False)
    failure: VaultReadError | None
    expires_at: float


class SecretResolver:
    def __init__(self, providers: Mapping[str, VaultProvider], errors: Mapping[str, str], cache_seconds: float) -> None:
        self.providers: dict[str, VaultProvider] = {}
        self.errors = dict(errors)
        self.policies: dict[str, Policy] = {}
        #: Where each known provider is declared: ``environment`` or ``database``.
        self.sources: dict[str, str] = {name: SOURCE_ENVIRONMENT for name in errors}
        for name, provider in providers.items():
            self.sources[name] = provider.config.source
            try:
                self.policies[name] = provider_policy(provider.config)
            except VaultConfigError as exc:
                self.errors[name] = str(exc)
                continue
            self.providers[name] = provider
        self.cache_seconds = max(float(cache_seconds), 0.0)
        #: The database providers applied, by name: their configuration and what their client was built from.
        self.database: dict[str, _DatabaseEntry] = {}
        #: Database providers an environment declaration of the same name hides, with why.
        self.shadowed: dict[str, str] = {}
        #: Database providers an administrator disabled: every read of them fails.
        self.disabled: set[str] = set()
        self._database_ticket = 0
        self._last_ticket = 0
        self._closing: set[asyncio.Task[None]] = set()
        self._cache: dict[tuple[str, str], _Entry] = {}
        self._inflight: dict[tuple[str, str], asyncio.Future[SecretPayload]] = {}
        self._refusals_logged: dict[tuple[str, str, str, str], float] = {}
        # A key the secret does not hold, by (provider, path, key): retried after
        # FAILURE_RETRY_SECONDS, like a failed fetch, even when nothing is cached.
        self._selection_failures: dict[tuple[str, str, str], _Entry] = {}

    def snapshot_ticket(self) -> int:
        """A ticket for a read of the database providers about to start, or a write just committed."""
        self._last_ticket += 1
        return self._last_ticket

    def apply_database_providers(
        self, configs: Iterable[VaultProviderConfig], *, ticket: int | None = None
    ) -> bool:
        """Put the database providers *configs* in force, in place of the ones applied before.

        A provider whose client settings did not change keeps its client, its
        login and its cached secrets; a changed one is rebuilt and its cache
        dropped, a removed one closed. *ticket* is the one
        :meth:`snapshot_ticket` gave the read that produced *configs*: a read
        older than the last one applied is dropped (False).
        """
        if ticket is None:
            ticket = self.snapshot_ticket()
        if ticket <= self._database_ticket:
            return False
        self._database_ticket = ticket
        wanted = {config.name: config for config in configs}
        for name in [name for name in self.database if name not in wanted]:
            self._forget_database_provider(name)
        self.shadowed = {}
        for name, config in wanted.items():
            if self.sources.get(name) == SOURCE_ENVIRONMENT:
                self.shadowed[name] = "an environment declaration of the same name takes precedence"
                continue
            fingerprint = _fingerprint(config)
            entry = self.database.get(name)
            self.policies[name] = provider_policy(config)
            if entry is not None and entry.fingerprint == fingerprint:
                entry.config = config
                self._place(name, config)
                continue
            self._forget_database_provider(name, keep_policy=True)
            self.database[name] = _DatabaseEntry(config=config, fingerprint=fingerprint)
            self.sources[name] = SOURCE_DATABASE
            self._place(name, config)
        return True

    def _place(self, name: str, config: VaultProviderConfig) -> None:
        """Make database provider *name* usable, disabled, or refused, as *config* says."""
        if not config.enabled:
            self.disabled.add(name)
            self.errors.pop(name, None)
            self._close_later(self.providers.pop(name, None))
            return
        self.disabled.discard(name)
        if name in self.providers and name not in self.errors:
            self.providers[name].config = config
            return
        try:
            self.providers[name] = _build_provider(config)
            self.errors.pop(name, None)
            logger.info("Vault provider '%s' connected in the UI is in use: type=%s", name, config.type)
        except VaultConfigError as exc:
            self.errors[name] = str(exc)
            logger.error("Vault provider '%s' connected in the UI is not usable: %s", name, exc)

    def _forget_database_provider(self, name: str, *, keep_policy: bool = False) -> None:
        if self.sources.get(name) != SOURCE_DATABASE:
            return
        self.database.pop(name, None)
        self.disabled.discard(name)
        self.errors.pop(name, None)
        if not keep_policy:
            self.policies.pop(name, None)
            self.sources.pop(name, None)
        self._close_later(self.providers.pop(name, None))
        self._drop_provider_cache(name)

    def _drop_provider_cache(self, name: str) -> None:
        for key in [key for key in self._cache if key[0] == name]:
            del self._cache[key]
        for key in [key for key in self._selection_failures if key[0] == name]:
            del self._selection_failures[key]

    def _close_later(self, provider: VaultProvider | None) -> None:
        """Close a replaced provider's client once the reads already holding it are done with it."""
        if provider is None:
            return
        try:
            task = asyncio.get_running_loop().create_task(provider.aclose())
        except RuntimeError:
            return
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def config_of(self, name: str) -> VaultProviderConfig | None:
        """The configuration of provider *name*, usable, disabled or refused; ``None`` for an unknown one."""
        provider = self.providers.get(name)
        if provider is not None:
            return provider.config
        entry = self.database.get(name)
        return entry.config if entry is not None else None

    def cache_seconds_for(self, name: str) -> float:
        config = self.config_of(name)
        if config is not None and config.cache_seconds is not None:
            return max(config.cache_seconds, 0.0)
        return self.cache_seconds

    def known_names(self) -> frozenset[str]:
        """Every provider a reference may name: usable, disabled or refused, wherever declared."""
        return frozenset(self.providers) | frozenset(self.errors) | frozenset(self.disabled)

    def usable_by(self, who: VaultPrincipal) -> list[str]:
        """The usable providers *who* may pick in a secret field, by name."""
        return sorted(name for name in self.providers if self.policy(name).usable_by(who))

    def provider_for(self, name: str) -> VaultProvider:
        """The usable provider *name*; :class:`VaultReadError` saying why when it is not."""
        provider = self.providers.get(name)
        if provider is not None:
            return provider
        if name in self.disabled:
            raise VaultReadError("provider_disabled", f"vault provider '{name}' is disabled")
        reason = "misconfigured" if name in self.errors else "provider_not_configured"
        raise VaultReadError(reason, f"vault provider '{name}' is not available")

    async def list_secrets(self, name: str, prefix: str, *, page_token: str | None, page_size: int) -> SecretListing:
        """The names under *prefix* in provider *name*: never cached, never a value."""
        return await self.provider_for(name).list_secrets(prefix, page_token=page_token, page_size=page_size)

    async def payload(self, reference: SecretReference) -> SecretPayload:
        """The whole secret *reference* points at, through the cache and the path checks of :meth:`read`."""
        provider = self.provider_for(reference.provider)
        if not self.policy(reference.provider).allows_path(reference.path):
            raise VaultReadError("path_not_allowed", "the path is outside the paths the provider allows")
        if problem := provider.path_problem(reference.path):
            raise VaultReadError("invalid_reference", problem)
        return await self._payload(provider, reference.path)

    @classmethod
    def from_declarations(cls, declarations: VaultDeclarations, cache_seconds: float) -> SecretResolver:
        providers: dict[str, VaultProvider] = {}
        errors = dict(declarations.errors)
        for name, config in declarations.providers.items():
            try:
                providers[name] = _build_provider(config)
            except VaultConfigError as exc:
                errors[name] = str(exc)
        return cls(providers, errors, cache_seconds)

    async def read(self, reference: SecretReference) -> str:
        """The value *reference* names; :class:`VaultReadError` when it cannot be read."""
        try:
            provider = self.provider_for(reference.provider)
        except VaultReadError as exc:
            raise self._refused(reference, exc) from None
        if not self.policy(reference.provider).allows_path(reference.path):
            raise self._refused(
                reference,
                VaultReadError("path_not_allowed", f"the path is outside {provider.config.variable('PATHS')}"),
            )
        if problem := provider.path_problem(reference.path):
            raise self._refused(reference, VaultReadError("invalid_reference", problem))
        selection_key = (reference.provider, reference.path, reference.key or "")
        failed = self._selection_failures.get(selection_key)
        if failed is not None and failed.failure is not None:
            if time.monotonic() < failed.expires_at:
                raise self._refused(reference, VaultReadError(failed.failure.reason, failed.failure.detail))
            del self._selection_failures[selection_key]
        payload = await self._payload(provider, reference.path)
        try:
            return select_value(payload, reference.key)
        except VaultReadError as exc:
            self._remember_selection_failure(selection_key, exc)
            raise self._refused(reference, exc) from None

    def _remember_selection_failure(self, selection_key: tuple[str, str, str], exc: VaultReadError) -> None:
        now = time.monotonic()
        if len(self._selection_failures) >= _MAX_SELECTION_FAILURES:
            self._selection_failures = {
                key: entry for key, entry in self._selection_failures.items() if entry.expires_at > now
            }
            if len(self._selection_failures) >= _MAX_SELECTION_FAILURES:
                del self._selection_failures[next(iter(self._selection_failures))]
        self._selection_failures[selection_key] = _Entry(None, exc, now + FAILURE_RETRY_SECONDS)

    def _refused(self, reference: SecretReference, exc: VaultReadError) -> VaultReadError:
        """*exc*, logged at most once per :data:`FAILURE_RETRY_SECONDS` for the same reference and reason.

        A refusal decided here (no such provider, a path outside its ``PATHS``, a
        key the secret does not hold) never reaches :meth:`_fetch`, which logs what
        the vault answered, so it is logged here.
        """
        now = time.monotonic()
        log_key = (reference.provider, reference.path, reference.key or "", exc.reason)
        if now >= self._refusals_logged.get(log_key, 0.0):
            if len(self._refusals_logged) >= _MAX_LOGGED_REFUSALS:
                self._refusals_logged = {key: until for key, until in self._refusals_logged.items() if until > now}
            self._refusals_logged[log_key] = now + FAILURE_RETRY_SECONDS
            logger.warning(
                "Vault reference could not be resolved: provider=%s path=%s key=%s reason=%s detail=%s",
                reference.provider,
                reference.path,
                reference.key or "-",
                exc.reason,
                exc.detail or "-",
            )
        return exc

    def policy(self, name: str) -> Policy:
        """Who may write a reference to provider *name*, and where it may read; administrators only for an unusable one."""
        return self.policies.get(name, DEFAULT_POLICY)

    def path_problem(self, reference: SecretReference) -> str | None:
        """Why the provider *reference* names can never read its path; ``None`` for an unusable provider."""
        provider = self.providers.get(reference.provider)
        return provider.path_problem(reference.path) if provider is not None else None

    async def _payload(self, provider: VaultProvider, path: str) -> SecretPayload:
        cache_key = (provider.name, path)
        entry = self._cache.get(cache_key)
        now = time.monotonic()
        if entry is not None and now < entry.expires_at:
            if entry.failure is not None:
                # A fresh error each time: re-raising the cached one would grow its traceback on every call.
                raise VaultReadError(entry.failure.reason, entry.failure.detail)
            if entry.payload is not None:
                return entry.payload
        task = self._inflight.get(cache_key)
        if task is None:
            # Its own task: a caller cancelled mid-read neither cancels the read the others share
            # nor leaves the secret unread for them.
            task = asyncio.ensure_future(self._fetch(provider, path))
            self._inflight[cache_key] = task
            task.add_done_callback(lambda done: self._settled(cache_key, done))
        return await asyncio.shield(task)

    async def _fetch(self, provider: VaultProvider, path: str) -> SecretPayload:
        try:
            payload = await provider.read(path)
        except Exception as exc:
            # A provider maps what it knows to a reason; anything else it raised is
            # still a failed read, reported and cached like one, never a raw error.
            failure = exc if isinstance(exc, VaultReadError) else VaultReadError("unreachable", type(exc).__name__)
            logger.warning(
                "Vault reference could not be resolved: provider=%s path=%s reason=%s detail=%s",
                provider.name,
                path,
                failure.reason,
                failure.detail or "-",
            )
            self._store((provider.name, path), _Entry(None, failure, 0.0), FAILURE_RETRY_SECONDS)
            raise failure from None
        ttl = self.cache_seconds_for(provider.name)
        # A provider replaced during the read keeps nothing the old one read.
        if ttl > 0 and self.providers.get(provider.name) is provider:
            self._store((provider.name, path), _Entry(payload, None, 0.0), ttl)
        return payload

    def _store(self, cache_key: tuple[str, str], entry: _Entry, ttl: float) -> None:
        """Cache *entry* for *ttl* seconds, then drop it, read again or not: a secret read once is not kept for good."""
        entry.expires_at = time.monotonic() + ttl
        self._cache[cache_key] = entry
        asyncio.get_running_loop().call_later(ttl, self._expire, cache_key, entry)

    def _expire(self, cache_key: tuple[str, str], entry: _Entry) -> None:
        if self._cache.get(cache_key) is entry:
            del self._cache[cache_key]

    def _settled(self, cache_key: tuple[str, str], task: asyncio.Future[SecretPayload]) -> None:
        if self._inflight.get(cache_key) is task:
            del self._inflight[cache_key]
        if not task.cancelled():
            # Retrieved here, so a read every caller stopped waiting for is not reported as unretrieved.
            task.exception()

    def invalidate(self, reference: SecretReference) -> None:
        self._cache.pop((reference.provider, reference.path), None)
        for key in [key for key in self._selection_failures if key[:2] == (reference.provider, reference.path)]:
            del self._selection_failures[key]

    def clear(self) -> None:
        self._cache.clear()
        self._selection_failures.clear()

    async def aclose(self) -> None:
        for provider in self.providers.values():
            await provider.aclose()


def select_value(payload: SecretPayload, key: str | None) -> str:
    """The value a reference reads out of a secret: the entry *key*, or the only value there is."""
    entries: Any = payload
    if isinstance(payload, str):
        if not key:
            return _non_empty(payload)
        try:
            entries = json.loads(payload)
        except ValueError:
            raise VaultReadError("not_found", "the secret is a plain string, it holds no key") from None
    if not isinstance(entries, dict):
        raise VaultReadError("not_found", "the secret holds no keys")
    if key:
        if key not in entries:
            raise VaultReadError("not_found", f"the secret has no key '{key}'")
        value = entries[key]
    elif len(entries) == 1:
        value = next(iter(entries.values()))
    else:
        raise VaultReadError("key_required", f"the secret holds {len(entries)} entries")
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise VaultReadError("invalid_response", "the selected entry is not a text value")
    return _non_empty(str(value))


def _non_empty(value: str) -> str:
    """*value* as a secret: an empty one is never a credential (a caller would fall back to another it holds)."""
    if not value:
        raise VaultReadError("invalid_response", "the selected value is empty")
    return value


_resolver: SecretResolver | None = None


def get_resolver() -> SecretResolver:
    """The process's resolver, built from the environment (the ``.env`` files included) on first use."""
    global _resolver
    if _resolver is None:
        from app.core.config import load_dotenv_files, settings

        load_dotenv_files()
        _resolver = SecretResolver.from_declarations(read_declarations(), settings.secret_vault_cache_seconds)
        for name, error in sorted(_resolver.errors.items()):
            logger.error("Vault provider '%s' is not usable: %s", name, error)
    return _resolver


def set_resolver(resolver: SecretResolver | None) -> None:
    """Replace the process's resolver (tests); ``None`` rebuilds it from the environment on next use."""
    global _resolver
    _resolver = resolver


def configured_provider_names() -> frozenset[str]:
    """The providers references may name: the declared and connected ones, refused and disabled ones included."""
    return get_resolver().known_names()


def log_vault_configuration() -> None:
    """Say at startup which vault providers are declared, and which are refused and why."""
    resolver = get_resolver()
    for name, provider in sorted(resolver.providers.items()):
        details = provider.describe()
        logger.info(
            "Vault provider '%s' declared: type=%s%s",
            name,
            details.get("type"),
            "".join(f" {key}={value}" for key, value in sorted(details.items()) if key not in ("name", "type")),
        )
        if plain_http_off_loopback(details.get("address", "")):
            logger.warning(
                "Vault provider '%s' is reached over plain HTTP: its credentials and the secrets it returns cross the network unencrypted",
                name,
            )
