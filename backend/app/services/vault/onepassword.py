"""1Password: the item fields a 1Password Connect server's token sees.

Settings (``SECRET_VAULT_<NAME>_*``):

* ``ADDRESS`` - the 1Password Connect server, ``https://connect.example.com``
  (its REST API is ``{ADDRESS}/v1``).
* ``TOKEN`` / ``TOKEN_FILE`` - a Connect access token, granted the vaults the
  platform may read.
* ``CA_CERT_FILE`` - the CA bundle that signs the Connect server's certificate.

A database provider stores the same settings, a pasted PEM ``CA_CERT``
instead of the file, and its ``TOKEN`` encrypted.

A reference path is ``<vault>/<item>``: each segment an id (what the picker
writes, so a renamed vault or item keeps working) or a name that fits the
reference syntax. The key is a field id (``username``, ``password``, a custom
or one-time password field's id); the picker shows each field's label
(:meth:`OnePasswordVaultProvider.entry_labels`). A one-time password field
reads as its seed (the ``otpauth://`` URI) and the platform computes the codes
(``app.core.totp``): a code read now would be stale in the cache, and a code
never reaches an agent this way. The picker lists the vaults as folders, then
their items.

Reads go through the Connect client the 1Password integration's tools use
(``app.integrations.onepassword_connect``), on this provider's own HTTP client:
verified TLS, no redirect, never a relay (a relay would see the token and every
secret). A database provider's ``ADDRESS`` is held to the destination policy of
``app.services.vault.destinations``, on every connection too.
"""

from __future__ import annotations

import asyncio

import httpx

from app.integrations.onepassword_connect import ConnectAPI, ConnectFailure, field_label, item_fields, sid
from app.services.vault.base import (
    REQUEST_TIMEOUT_SECONDS,
    ListedSecret,
    SecretListing,
    SecretPayload,
    VaultProvider,
    page_entries,
    read_setting_or_file,
    service_address,
    status_failure,
    tls_verify,
)
from app.services.vault.config import VaultConfigError, VaultProviderConfig
from app.services.vault.destinations import client_options
from app.services.vault.errors import VaultReadError

#: How many items' field labels are remembered for the picker.
_MAX_LABELLED_ITEMS = 512


def _read_failure(failure: ConnectFailure) -> VaultReadError:
    """The reason a Connect read failed, as every vault provider names it."""
    if failure.kind == "http" and failure.status is not None:
        return status_failure(failure.status)
    if failure.kind == "network":
        return VaultReadError("unreachable", "the Connect server did not answer")
    if failure.kind in ("not_json", "shape"):
        return VaultReadError("invalid_response", "the answer is not the 1Password Connect API")
    if failure.kind == "ambiguous":
        return VaultReadError("invalid_reference", "the name matches several vaults or items: use the id")
    return VaultReadError("not_found", "no such vault or item")


class OnePasswordVaultProvider(VaultProvider):
    kind = "onepassword"

    def __init__(self, config: VaultProviderConfig) -> None:
        super().__init__(config)
        self.address = service_address(config, "ADDRESS")
        if config.from_database and not config.secret("TOKEN"):
            raise VaultConfigError(f"{config.variable('TOKEN')} is required")
        self._client: httpx.AsyncClient | None = None
        self._labels: dict[str, dict[str, str]] = {}

    def describe(self) -> dict[str, str]:
        return {**super().describe(), "address": self.address}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
                **client_options(self.config, tls_verify(self.config)),
            )
        return self._client

    async def _api(self) -> ConnectAPI:
        token = await asyncio.to_thread(read_setting_or_file, self.config, "TOKEN")
        if not token:
            raise VaultReadError("misconfigured", f"{self.config.variable('TOKEN')} is not set")
        return ConnectAPI(self._http(), self.address, token, max_total_seconds=REQUEST_TIMEOUT_SECONDS)

    def path_problem(self, path: str) -> str | None:
        if len(path.strip("/").split("/")) != 2:
            return "a 1Password reference path names a vault then an item, separated by a slash (vault id/item id)"
        return None

    async def authenticate(self) -> None:
        _vaults, failure = await (await self._api()).list_vaults()
        if failure:
            raise _read_failure(failure)

    async def read(self, path: str) -> SecretPayload:
        if problem := self.path_problem(path):
            raise VaultReadError("invalid_reference", problem)
        vault_ref, item_ref = path.strip("/").split("/")
        _vault, item, failure = await (await self._api()).resolve_vault_and_item(vault_ref, item_ref)
        if failure:
            raise _read_failure(failure)
        values: dict[str, str] = {}
        labels: dict[str, str] = {}
        for field in item_fields(item):
            ident, value = sid(field.get("id")), field.get("value")
            if not ident or value is None or value == "":
                continue
            values[ident] = str(value)
            labels[ident] = field_label(field)
        self._remember_labels(path, labels)
        if not values:
            raise VaultReadError("not_found", "the item has no field with a value the token may read")
        return values

    def _remember_labels(self, path: str, labels: dict[str, str]) -> None:
        key = path.strip("/")
        self._labels.pop(key, None)
        if len(self._labels) >= _MAX_LABELLED_ITEMS:
            del self._labels[next(iter(self._labels))]
        self._labels[key] = labels

    def entry_labels(self, path: str) -> dict[str, str]:
        return dict(self._labels.get(path.strip("/"), {}))

    async def list_secrets(self, prefix: str, *, page_token: str | None = None, page_size: int = 100) -> SecretListing:
        base = prefix.strip("/")
        api = await self._api()
        if not base:
            vaults, failure = await api.list_vaults()
            if failure:
                raise _read_failure(failure)
            entries = [
                ListedSecret(name=f"{sid(vault.get('name')) or sid(vault.get('id'))}/", path=f"{sid(vault.get('id'))}/", folder=True)
                for vault in vaults
                if sid(vault.get("id"))
            ]
        elif "/" in base:
            return SecretListing()
        else:
            vault, failure = await api.resolve_vault(base)
            if failure or vault is None:
                raise _read_failure(failure or ConnectFailure("", "not_found"))
            items, failure = await api.list_items(sid(vault.get("id")))
            if failure:
                raise _read_failure(failure)
            entries = [
                ListedSecret(name=sid(item.get("title")) or sid(item.get("id")), path=f"{base}/{sid(item.get('id'))}")
                for item in items
                if sid(item.get("id"))
            ]
        entries.sort(key=lambda entry: entry.name.lower())
        return page_entries(entries, page_token, page_size)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
