"""HashiCorp Vault: KV v1 / v2 secrets, token, AppRole or Kubernetes auth.

Settings (``SECRET_VAULT_<NAME>_*``):

* ``ADDRESS`` - the Vault address, ``https://vault.example.com:8200``.
* ``AUTH_METHOD`` - ``token`` (default), ``approle`` or ``kubernetes``.
* ``TOKEN`` / ``TOKEN_FILE`` - the token (``token``).
* ``ROLE_ID`` + ``SECRET_ID`` / ``SECRET_ID_FILE`` - the AppRole (``approle``).
* ``ROLE`` + ``JWT_FILE`` - the Vault role and the service account token
  (``kubernetes``; the pod's projected token by default).
* ``AUTH_MOUNT`` - where the auth method is mounted (``approle`` / ``kubernetes``).
* ``NAMESPACE`` - a Vault Enterprise namespace.
* ``KV_VERSION`` - ``2`` (default) or ``1``.
* ``KV_MOUNT`` - when set, reference paths are relative to it; otherwise the
  path's first segment is the mount (``secret/github``, as ``vault kv get``).
  Required with ``KV_VERSION=1``: a KV v1 read is a plain ``GET /v1/<path>``,
  so without a fixed mount a reference could name any Vault endpoint the
  platform's token may read. A mount, fixed or named by a path, is never one
  of Vault's own namespaces (:data:`RESERVED_MOUNTS`).
* ``CA_CERT_FILE`` - the CA bundle that signs the Vault certificate.

A database provider stores the same settings, a pasted PEM
``CA_CERT`` instead of the file, and its ``TOKEN`` / ``SECRET_ID`` encrypted.
It signs in with a token or an AppRole only: a Kubernetes login sends this
platform's own service account token to the address, which only the operator
chooses (a provider declared in the environment), never a UI session. Its
``ADDRESS`` is held to the destination policy of
``app.services.vault.destinations``: checked when it is saved and
tested, and on the address every connection opens to (pinned, no redirect).
Listing (the secret picker) is a KV ``LIST`` of the metadata (v2) or of the
path (v1); without ``KV_MOUNT``, the top lists the KV mounts the token sees.

A login token is kept in memory until shortly before its lease ends, and a
read refused with 403 logs in again once (a revoked or expired token), at
most once per ``FAILURE_RETRY_SECONDS``: Vault also answers 403 for a path
its policy denies. A failed login is answered from memory for as long.
"""

from __future__ import annotations

import asyncio
import pathlib
import time
from typing import Any

import httpx

from app.services.vault.base import (
    FAILURE_RETRY_SECONDS,
    REQUEST_TIMEOUT_SECONDS,
    CredentialBackoff,
    ListedSecret,
    SecretListing,
    SecretPayload,
    VaultProvider,
    http_failure,
    network_failure,
    page_entries,
    read_setting_or_file,
    service_address,
    tls_verify,
)
from app.services.vault.config import ENV_PREFIX, VaultConfigError, VaultProviderConfig
from app.services.vault.destinations import client_options
from app.services.vault.errors import VaultReadError

AUTH_METHODS = ("token", "approle", "kubernetes")
#: The methods a provider connected in the UI may sign in with.
DATABASE_AUTH_METHODS = ("token", "approle")
#: Vault's own namespaces: never a KV mount (``auth/token/lookup-self`` answers with the token itself).
RESERVED_MOUNTS = frozenset({"sys", "auth", "identity", "cubbyhole"})
DEFAULT_KUBERNETES_JWT_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/token"
#: A login token is renewed this long before its lease ends.
_LEASE_MARGIN_SECONDS = 30.0
#: How long a login token whose lease Vault does not bound is kept.
_UNBOUNDED_LEASE_SECONDS = 3600.0


class HashiCorpVaultProvider(VaultProvider):
    kind = "hashicorp"

    def __init__(self, config: VaultProviderConfig) -> None:
        super().__init__(config)
        self.address = service_address(config, "ADDRESS")
        self.auth_method = (config.get("AUTH_METHOD") or "token").lower()
        if self.auth_method not in AUTH_METHODS:
            raise VaultConfigError(f"{config.variable('AUTH_METHOD')} is one of {', '.join(AUTH_METHODS)}")
        if config.from_database and self.auth_method not in DATABASE_AUTH_METHODS:
            raise VaultConfigError(
                f"{config.variable('AUTH_METHOD')} is one of {', '.join(DATABASE_AUTH_METHODS)}: the kubernetes "
                "method sends this platform's service account token to the vault address, so it is declared in "
                f"the environment ({ENV_PREFIX}NAME_AUTH_METHOD=kubernetes)"
            )
        if self.auth_method == "approle" and not config.get("ROLE_ID"):
            raise VaultConfigError(f"{config.variable('ROLE_ID')} is required with the approle auth method")
        if self.auth_method == "kubernetes" and not config.get("ROLE"):
            raise VaultConfigError(f"{config.variable('ROLE')} is required with the kubernetes auth method")
        if config.from_database and self.auth_method == "token" and not config.secret("TOKEN"):
            raise VaultConfigError(f"{config.variable('TOKEN')} is required with the token auth method")
        if config.from_database and self.auth_method == "approle" and not config.secret("SECRET_ID"):
            raise VaultConfigError(f"{config.variable('SECRET_ID')} is required with the approle auth method")
        kv_version = config.get("KV_VERSION") or "2"
        if kv_version not in ("1", "2"):
            raise VaultConfigError(f"{config.variable('KV_VERSION')} is 1 or 2")
        self.kv_version = int(kv_version)
        self.kv_mount = config.get("KV_MOUNT").strip("/")
        if self.kv_version == 1 and not self.kv_mount:
            raise VaultConfigError(f"{config.variable('KV_MOUNT')} is required with KV_VERSION=1")
        if self.kv_mount and any(segment in ("", ".", "..") for segment in self.kv_mount.split("/")):
            raise VaultConfigError(f"{config.variable('KV_MOUNT')} is not a mount path")
        if self.kv_mount and _is_reserved(self.kv_mount):
            raise VaultConfigError(f"{config.variable('KV_MOUNT')} names one of Vault's own namespaces")
        self.namespace = config.get("NAMESPACE")
        self._client: httpx.AsyncClient | None = None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._login_lock = asyncio.Lock()
        self._login_backoff = CredentialBackoff()
        #: Until then a refused read does not log in again: Vault answers 403 for a
        #: path its policy denies too, which a new token does not change.
        self._relogin_after = 0.0

    def describe(self) -> dict[str, str]:
        return {
            **super().describe(),
            "address": self.address,
            "auth_method": self.auth_method,
            "kv_version": str(self.kv_version),
        }

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.address,
                timeout=REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
                **client_options(self.config, tls_verify(self.config)),
            )
        return self._client

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {"X-Vault-Request": "true"}
        if self.namespace:
            headers["X-Vault-Namespace"] = self.namespace
        if token:
            headers["X-Vault-Token"] = token
        return headers

    def path_problem(self, path: str) -> str | None:
        if self.kv_mount:
            return None
        mount, _, rest = path.strip("/").partition("/")
        if _is_reserved(mount):
            return f"'{mount}' is one of Vault's own namespaces, not a KV mount"
        if not rest:
            return "the path names a KV mount and no secret"
        return None

    def api_path(self, path: str) -> str:
        """The KV API path of a reference path: ``secret/github`` -> ``/v1/secret/data/github`` (v2)."""
        if problem := self.path_problem(path):
            raise VaultReadError("invalid_reference", problem)
        if self.kv_mount:
            mount, rest = self.kv_mount, path.strip("/")
        else:
            mount, _, rest = path.strip("/").partition("/")
        middle = "data/" if self.kv_version == 2 else ""
        return f"/v1/{mount}/{middle}{rest}"

    async def _login_token(self, *, rejected: str | None = None) -> str:
        """The token to present; a new login when the current one is *rejected*, unless another read already did it.

        A refused token is replaced at most once per :data:`FAILURE_RETRY_SECONDS`
        (the same token comes back otherwise), and a failed login is answered
        from memory for as long (:class:`CredentialBackoff`).
        """
        if self.auth_method == "token":
            token = await asyncio.to_thread(read_setting_or_file, self.config, "TOKEN")
            if not token:
                raise VaultReadError("misconfigured", f"{self.config.variable('TOKEN')} is not set")
            return token
        async with self._login_lock:
            now = time.monotonic()
            if self._token and now < self._token_expires_at and (
                self._token != rejected or now < self._relogin_after
            ):
                return self._token
            self._login_backoff.check()
            self._token = None
            try:
                token, lifetime = await self._login()
            except VaultReadError as exc:
                self._login_backoff.failed(exc)
                raise
            self._login_backoff.succeeded()
            self._token = token
            self._token_expires_at = time.monotonic() + max(lifetime - _LEASE_MARGIN_SECONDS, 1.0)
            if rejected:
                self._relogin_after = time.monotonic() + FAILURE_RETRY_SECONDS
            return token

    async def _login(self) -> tuple[str, float]:
        """A new token from the AppRole or Kubernetes login, and how long it lives."""
        if self.auth_method == "approle":
            mount = self.config.get("AUTH_MOUNT") or "approle"
            secret_id = await asyncio.to_thread(read_setting_or_file, self.config, "SECRET_ID")
            body = {"role_id": self.config.get("ROLE_ID"), "secret_id": secret_id}
        else:
            mount = self.config.get("AUTH_MOUNT") or "kubernetes"
            jwt_file = self.config.get("JWT_FILE") or DEFAULT_KUBERNETES_JWT_FILE
            body = {"role": self.config.get("ROLE"), "jwt": await asyncio.to_thread(_read_jwt, jwt_file)}
        try:
            response = await self._http().post(f"/v1/auth/{mount.strip('/')}/login", json=body, headers=self._headers())
        except httpx.HTTPError as exc:
            raise network_failure(exc) from None
        if response.status_code != 200:
            failure = http_failure(response)
            raise VaultReadError(
                "access_denied" if failure.reason in ("access_denied", "invalid_reference") else failure.reason,
                f"{self.auth_method} login: {failure.detail}",
            )
        auth = _json(response).get("auth") or {}
        token = auth.get("client_token")
        if not isinstance(token, str) or not token:
            raise VaultReadError("invalid_response", f"{self.auth_method} login returned no client token")
        lease = auth.get("lease_duration")
        return token, float(lease) if isinstance(lease, int | float) and lease > 0 else _UNBOUNDED_LEASE_SECONDS

    async def _get(self, api_path: str, token: str) -> httpx.Response:
        try:
            return await self._http().get(api_path, headers=self._headers(token))
        except httpx.HTTPError as exc:
            raise network_failure(exc) from None

    async def read(self, path: str) -> SecretPayload:
        api_path = self.api_path(path)
        token = await self._login_token()
        response = await self._get(api_path, token)
        if response.status_code == 403 and self.auth_method != "token":
            renewed = await self._login_token(rejected=token)
            if renewed != token:
                response = await self._get(api_path, renewed)
        if response.status_code != 200:
            raise http_failure(response)
        data = _json(response).get("data")
        if self.kv_version == 2:
            data = data.get("data") if isinstance(data, dict) else None
        if not isinstance(data, dict):
            raise VaultReadError("invalid_response", "the answer holds no secret data (a deleted version?)")
        return data

    async def _authorized_get(self, api_path: str, params: dict[str, str] | None = None) -> httpx.Response:
        """A GET with the platform's token, logging in again once on a 403 like :meth:`read`."""
        token = await self._login_token()
        try:
            response = await self._http().get(api_path, params=params, headers=self._headers(token))
            if response.status_code == 403 and self.auth_method != "token":
                renewed = await self._login_token(rejected=token)
                if renewed != token:
                    response = await self._http().get(api_path, params=params, headers=self._headers(renewed))
        except httpx.HTTPError as exc:
            raise network_failure(exc) from None
        return response

    async def authenticate(self) -> None:
        if self.auth_method != "token":
            await self._login_token()
            return
        # A static token is checked by Vault itself: lookup-self answers 403 for one it does not know.
        response = await self._authorized_get("/v1/auth/token/lookup-self")
        if response.status_code != 200:
            raise http_failure(response)

    async def mounts(self) -> list[dict[str, str]]:
        """The KV mounts the platform's token sees, with their version: offered when the mount is chosen."""
        response = await self._authorized_get("/v1/sys/internal/ui/mounts")
        if response.status_code == 200:
            secret = (_json(response).get("data") or {}).get("secret")
        else:
            response = await self._authorized_get("/v1/sys/mounts")
            if response.status_code != 200:
                raise http_failure(response)
            body = _json(response)
            secret = body.get("data") if isinstance(body.get("data"), dict) else body
        found: list[dict[str, str]] = []
        for path, mount in sorted((secret or {}).items()) if isinstance(secret, dict) else ():
            if not isinstance(mount, dict) or mount.get("type") not in ("kv", "generic"):
                continue
            options = mount.get("options") if isinstance(mount.get("options"), dict) else {}
            version = str((options or {}).get("version") or "1")
            mount_path = str(path).strip("/")
            if mount_path and not _is_reserved(mount_path):
                found.append({"path": mount_path, "version": version if version in ("1", "2") else "1"})
        return found

    async def list_secrets(self, prefix: str, *, page_token: str | None = None, page_size: int = 100) -> SecretListing:
        base = prefix.strip("/")
        if not self.kv_mount and not base:
            entries = [
                ListedSecret(name=f"{mount['path']}/", path=f"{mount['path']}/", folder=True)
                for mount in await self.mounts()
            ]
            return page_entries(entries, page_token, page_size)
        if self.kv_mount:
            mount, rest = self.kv_mount, base
            shown = f"{base}/" if base else ""
        else:
            mount, _, rest = base.partition("/")
            if _is_reserved(mount):
                raise VaultReadError("invalid_reference", f"'{mount}' is one of Vault's own namespaces, not a KV mount")
            shown = f"{base}/"
        middle = "metadata/" if self.kv_version == 2 else ""
        api_path = f"/v1/{mount}/{middle}{rest}".rstrip("/") + "/"
        response = await self._authorized_get(api_path, params={"list": "true"})
        if response.status_code == 404:
            return SecretListing()
        if response.status_code != 200:
            raise http_failure(response)
        keys = (_json(response).get("data") or {}).get("keys") or []
        entries = []
        for key in keys if isinstance(keys, list) else []:
            if not isinstance(key, str) or not key.strip("/"):
                continue
            folder = key.endswith("/")
            entries.append(ListedSecret(name=key, path=f"{shown}{key}", folder=folder))
        entries.sort(key=lambda entry: (not entry.folder, entry.name.lower()))
        return page_entries(entries, page_token, page_size)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _is_reserved(mount: str) -> bool:
    return mount.split("/", 1)[0].lower() in RESERVED_MOUNTS


def _read_jwt(jwt_file: str) -> str:
    try:
        return pathlib.Path(jwt_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise VaultReadError(
            "misconfigured", f"the service account token {jwt_file} cannot be read: {exc.strerror}"
        ) from None


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        raise VaultReadError("invalid_response", "the answer is not JSON") from None
    if not isinstance(body, dict):
        raise VaultReadError("invalid_response", "the answer is not a JSON object")
    return body
