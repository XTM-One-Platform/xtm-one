"""Azure Key Vault, authenticated by a managed identity.

Settings (``SECRET_VAULT_<NAME>_*``): ``VAULT_URL``
(``https://<vault>.vault.azure.net``), ``CLIENT_ID`` (a user-assigned
identity or the workload identity's application) and ``TENANT_ID``.
The token comes from, in order:

* AKS Workload Identity - the federated token in ``AZURE_FEDERATED_TOKEN_FILE``
  exchanged at ``AZURE_AUTHORITY_HOST`` for ``CLIENT_ID`` / ``TENANT_ID``
  (``AZURE_CLIENT_ID`` / ``AZURE_TENANT_ID`` by default);
* App Service / Container Apps - ``IDENTITY_ENDPOINT`` + ``IDENTITY_HEADER``;
* the instance metadata service of a VM or a scale set.

No secret is stored. A database provider may instead sign in with an
application's client secret (``AUTH_METHOD=client_secret``: ``TENANT_ID``,
``CLIENT_ID`` and the encrypted ``CLIENT_SECRET``). Its ``VAULT_URL`` is a key
vault of an Azure cloud (:data:`KEY_VAULT_DOMAINS`): the token sent there is
the platform's own identity's, so a UI session never points it at another
host. The token is requested for the Key Vault of the vault's cloud; a vault on
another host, which only an environment declaration may name, gets a token
for the public cloud's. A reference path is
``<secret name>`` or ``<secret name>/<version>``; ``#key`` reads one field of a
JSON value. Listing (the secret picker) reads the secrets' properties, never a
value.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import time
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.services.vault.base import (
    MAX_PAGE_SIZE,
    REQUEST_TIMEOUT_SECONDS,
    CredentialBackoff,
    ListedSecret,
    SecretListing,
    SecretPayload,
    VaultProvider,
    http_failure,
    network_failure,
    service_address,
)
from app.services.vault.config import VaultConfigError, VaultProviderConfig
from app.services.vault.errors import VaultReadError

AUTH_METHODS = ("managed_identity", "client_secret")
KEY_VAULT_RESOURCE = "https://vault.azure.net"
#: The key vault domain of each Azure cloud (public, China, US Government, Germany): a vault is ``<name>.<domain>``.
KEY_VAULT_DOMAINS = ("vault.azure.net", "vault.azure.cn", "vault.usgovcloudapi.net", "vault.microsoftazure.de")
API_VERSION = "7.4"
_IMDS_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
_TOKEN_MARGIN_SECONDS = 60.0


class AzureKeyVaultProvider(VaultProvider):
    kind = "azure"

    def __init__(self, config: VaultProviderConfig) -> None:
        super().__init__(config)
        if not config.get("VAULT_URL").startswith("https://"):
            raise VaultConfigError(f"{config.variable('VAULT_URL')} must be the https:// URL of the key vault")
        self.vault_url = service_address(config, "VAULT_URL")
        domain = _key_vault_domain(self.vault_url)
        if config.from_database and domain is None:
            raise VaultConfigError(
                f"{config.variable('VAULT_URL')} must be the URL of an Azure key vault (https://NAME.vault.azure.net, "
                "or the key vault domain of a sovereign cloud): the platform's identity token is sent to it"
            )
        self.resource = f"https://{domain}" if domain else KEY_VAULT_RESOURCE
        self.auth_method = (config.get("AUTH_METHOD") or "managed_identity").lower() if config.from_database else "managed_identity"
        if self.auth_method not in AUTH_METHODS:
            raise VaultConfigError(f"{config.variable('AUTH_METHOD')} is one of {', '.join(AUTH_METHODS)}")
        self.client_id = config.get("CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID", "")
        self.tenant_id = config.get("TENANT_ID") or os.environ.get("AZURE_TENANT_ID", "")
        if self.auth_method == "client_secret" and not (
            config.get("TENANT_ID") and config.get("CLIENT_ID") and config.secret("CLIENT_SECRET")
        ):
            raise VaultConfigError(
                f"{config.variable('TENANT_ID')}, {config.variable('CLIENT_ID')} and "
                f"{config.variable('CLIENT_SECRET')} are required with the client_secret auth method"
            )
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = asyncio.Lock()
        self._token_backoff = CredentialBackoff()

    def describe(self) -> dict[str, str]:
        return {**super().describe(), "address": self.vault_url, "auth_method": self.auth_method}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False)
        return self._client

    async def _request_token(self) -> dict[str, Any]:
        federated = os.environ.get("AZURE_FEDERATED_TOKEN_FILE", "")
        authority = os.environ.get("AZURE_AUTHORITY_HOST", "https://login.microsoftonline.com").rstrip("/")
        try:
            if self.auth_method == "client_secret":
                response = await self._http().post(
                    f"{authority}/{quote(self.tenant_id, safe='')}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.config.secret("CLIENT_SECRET"),
                        "scope": f"{self.resource}/.default",
                    },
                )
            elif federated and self.client_id and self.tenant_id:
                assertion = await asyncio.to_thread(_read_file, federated)
                response = await self._http().post(
                    f"{authority}/{quote(self.tenant_id, safe='')}/oauth2/v2.0/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "scope": f"{self.resource}/.default",
                        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                        "client_assertion": assertion,
                    },
                )
            elif os.environ.get("IDENTITY_ENDPOINT") and os.environ.get("IDENTITY_HEADER"):
                params = {"api-version": "2019-08-01", "resource": self.resource}
                if self.client_id:
                    params["client_id"] = self.client_id
                response = await self._http().get(
                    os.environ["IDENTITY_ENDPOINT"],
                    params=params,
                    headers={"X-IDENTITY-HEADER": os.environ["IDENTITY_HEADER"]},
                )
            else:
                params = {"api-version": "2018-02-01", "resource": self.resource}
                if self.client_id:
                    params["client_id"] = self.client_id
                response = await self._http().get(_IMDS_URL, params=params, headers={"Metadata": "true"})
        except httpx.HTTPError as exc:
            raise network_failure(exc) from None
        if response.status_code != 200:
            failure = http_failure(response)
            raise VaultReadError(
                "access_denied" if response.status_code < 500 else failure.reason,
                f"managed identity token: {failure.detail}",
            )
        try:
            body = response.json()
        except ValueError:
            raise VaultReadError("invalid_response", "the identity token answer is not JSON") from None
        if not isinstance(body, dict) or not body.get("access_token"):
            raise VaultReadError("invalid_response", "the identity token answer holds no token")
        return body

    async def _access_token(self, *, rejected: str | None = None) -> str:
        """The identity's token; a new one when the current one is *rejected*, unless another read already renewed it."""
        async with self._lock:
            if self._token and self._token != rejected and time.monotonic() < self._token_expires_at:
                return self._token
            self._token_backoff.check()
            try:
                body = await self._request_token()
            except VaultReadError as exc:
                self._token_backoff.failed(exc)
                raise
            self._token_backoff.succeeded()
            try:
                lifetime = float(body.get("expires_in") or 300)
            except (TypeError, ValueError):
                lifetime = 300.0
            self._token = str(body["access_token"])
            self._token_expires_at = time.monotonic() + max(lifetime - _TOKEN_MARGIN_SECONDS, 1.0)
            return self._token

    def secret_url(self, path: str) -> str:
        parts = path.strip("/").split("/")
        if len(parts) not in (1, 2):
            raise VaultReadError("invalid_reference", "a path is a secret name, optionally followed by /VERSION")
        return f"{self.vault_url}/secrets/{'/'.join(quote(part, safe='') for part in parts)}"

    async def _get(self, url: str, token: str) -> httpx.Response:
        try:
            return await self._http().get(
                url, params={"api-version": API_VERSION}, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise network_failure(exc) from None

    async def read(self, path: str) -> SecretPayload:
        url = self.secret_url(path)
        token = await self._access_token()
        response = await self._get(url, token)
        if response.status_code == 401:
            response = await self._get(url, await self._access_token(rejected=token))
        if response.status_code != 200:
            raise http_failure(response)
        try:
            value = response.json()["value"]
        except (ValueError, KeyError, TypeError):
            raise VaultReadError("invalid_response", "the answer holds no secret value") from None
        if not isinstance(value, str):
            raise VaultReadError("invalid_response", "the secret value is not text")
        return value

    async def authenticate(self) -> None:
        await self._access_token()

    async def list_secrets(self, prefix: str, *, page_token: str | None = None, page_size: int = 100) -> SecretListing:
        base = prefix.strip("/")
        listing_root = f"{self.vault_url}/secrets"
        if page_token:
            # Azure's nextLink is a full URL: only one into this vault's own listing is followed.
            if not _same_listing(page_token, self.vault_url):
                raise VaultReadError("invalid_reference", "the page token is not a listing of this vault")
            url, params = page_token, None
        else:
            url, params = listing_root, {"api-version": API_VERSION, "maxresults": str(min(max(page_size, 1), 25))}
        token = await self._access_token()
        try:
            response = await self._http().get(url, params=params, headers={"Authorization": f"Bearer {token}"})
            if response.status_code == 401:
                token = await self._access_token(rejected=token)
                response = await self._http().get(url, params=params, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise network_failure(exc) from None
        if response.status_code != 200:
            raise http_failure(response)
        try:
            body = response.json()
        except ValueError:
            raise VaultReadError("invalid_response", "the answer is not JSON") from None
        entries = []
        for item in (body.get("value") or [])[:MAX_PAGE_SIZE] if isinstance(body, dict) else []:
            name = str(item.get("id") or "").rstrip("/").rsplit("/", 1)[-1] if isinstance(item, dict) else ""
            if name and name.startswith(base) and (item.get("attributes") or {}).get("enabled") is not False:
                entries.append(ListedSecret(name=name, path=name))
        entries.sort(key=lambda entry: entry.name.lower())
        next_link = body.get("nextLink") if isinstance(body, dict) else None
        return SecretListing(entries=entries, next_page_token=next_link if isinstance(next_link, str) and next_link else None)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _key_vault_domain(vault_url: str) -> str | None:
    """The Azure key vault domain *vault_url*'s host is a vault of (:data:`KEY_VAULT_DOMAINS`), else ``None``."""
    host = urlsplit(vault_url).hostname or ""
    return next((domain for domain in KEY_VAULT_DOMAINS if host.endswith(f".{domain}")), None)


def _same_listing(link: str, vault_url: str) -> bool:
    """Whether *link* is this vault's ``/secrets`` listing (Azure writes it with an explicit ``:443``)."""
    try:
        target, vault = urlsplit(link), urlsplit(vault_url)
        return (
            target.scheme == "https"
            and target.hostname is not None
            and target.hostname == vault.hostname
            and (target.port or 443) == (vault.port or 443)
            and target.path.rstrip("/") == "/secrets"
            and not target.username
            and not target.password
        )
    except ValueError:
        return False


def _read_file(path: str) -> str:
    try:
        return pathlib.Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VaultReadError("misconfigured", f"{path} cannot be read: {exc.strerror}") from None
