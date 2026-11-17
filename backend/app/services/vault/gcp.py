"""Google Cloud Secret Manager.

Settings (``SECRET_VAULT_<NAME>_*``): ``PROJECT``, the project of a secret
named without one (else the project of the credentials). An environment
provider's credentials are Application Default Credentials - GKE Workload
Identity, the attached service account - resolved by ``google-auth``, never a
stored key. A database provider may instead store a service account key
(``AUTH_METHOD=service_account_key``, the encrypted ``SERVICE_ACCOUNT_KEY``).
The key is exchanged at Google's token endpoint (:data:`TOKEN_URI`) whatever
its ``token_uri`` says, so a key edited in the UI never makes the platform post
to another address.

A reference path is ``<secret>``, ``<secret>/<version>``, or the full
``projects/<project>/secrets/<secret>[/versions/<version>]``; the version
defaults to ``latest``. ``#key`` reads one field of a JSON payload. Listing
(the secret picker) is ``secrets.list`` of the project, by name prefix.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any

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
)
from app.services.vault.config import VaultConfigError, VaultProviderConfig
from app.services.vault.errors import VaultReadError

AUTH_METHODS = ("workload_identity", "service_account_key")
#: A project id (``my-project``, ``example.com:my-project``) or number: it is a segment of the API URL.
_PROJECT_RE = re.compile(r"[a-z0-9][a-z0-9.:\-]{0,62}")

API_ROOT = "https://secretmanager.googleapis.com/v1"
#: The token endpoint a stored service account key is exchanged at, whatever the key says.
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


class _BoundedRequest:
    """google-auth's transport with each request held to :data:`REQUEST_TIMEOUT_SECONDS` (it waits 120 s by default)."""

    def __init__(self, request: Any) -> None:
        self._request = request

    def __call__(self, url: str, method: str = "GET", body: Any = None, headers: Any = None, timeout: Any = None, **kwargs: Any) -> Any:
        bounded = min(timeout, REQUEST_TIMEOUT_SECONDS) if isinstance(timeout, int | float) else REQUEST_TIMEOUT_SECONDS
        return self._request(url, method=method, body=body, headers=headers, timeout=bounded, **kwargs)


class GoogleSecretManagerProvider(VaultProvider):
    kind = "gcp"

    def __init__(self, config: VaultProviderConfig) -> None:
        super().__init__(config)
        self.project = config.get("PROJECT")
        if self.project and not _PROJECT_RE.fullmatch(self.project):
            raise VaultConfigError(f"{config.variable('PROJECT')} is a Google Cloud project id or number")
        self.auth_method = (config.get("AUTH_METHOD") or "workload_identity").lower() if config.from_database else "workload_identity"
        if self.auth_method not in AUTH_METHODS:
            raise VaultConfigError(f"{config.variable('AUTH_METHOD')} is one of {', '.join(AUTH_METHODS)}")
        self._key_info: dict[str, Any] | None = None
        if self.auth_method == "service_account_key":
            self._key_info = _service_account_info(config)
            self.project = self.project or str(self._key_info.get("project_id") or "")
        self._credentials: Any = None
        self._ambient_project: str | None = None
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._refresh_backoff = CredentialBackoff()

    def describe(self) -> dict[str, str]:
        details = {**super().describe(), "auth_method": self.auth_method}
        if self.project:
            details["project"] = self.project
        return details

    def resource_name(self, path: str) -> str:
        """The secret version a reference path names."""
        parts = path.strip("/").split("/")
        # A full name has 4 or 6 segments: a secret named "projects" keeps the short forms.
        if parts[0] == "projects" and len(parts) > 2:
            if len(parts) == 4 and parts[2] == "secrets":
                return "/".join([*parts, "versions", "latest"])
            if len(parts) == 6 and parts[2] == "secrets" and parts[4] == "versions":
                return "/".join(parts)
            raise VaultReadError(
                "invalid_reference",
                "a full name is projects/PROJECT/secrets/SECRET, optionally followed by /versions/VERSION",
            )
        project = self.project or self._ambient_project
        if not project:
            raise VaultReadError(
                "misconfigured", f"{self.config.variable('PROJECT')} is not set and the credentials name no project"
            )
        if len(parts) == 1:
            return f"projects/{project}/secrets/{parts[0]}/versions/latest"
        if len(parts) == 2:
            return f"projects/{project}/secrets/{parts[0]}/versions/{parts[1]}"
        raise VaultReadError(
            "invalid_reference", "a path is a secret name, optionally followed by /VERSION, or a full resource name"
        )

    def _refresh_credentials(self) -> str:
        import google.auth
        from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError, RefreshError, TransportError
        from google.auth.transport.requests import Request

        try:
            if self._credentials is None and self._key_info is not None:
                from google.oauth2 import service_account

                try:
                    self._credentials = service_account.Credentials.from_service_account_info(
                        self._key_info, scopes=list(_SCOPES)
                    )
                except (ValueError, KeyError) as exc:
                    raise VaultReadError("misconfigured", f"the service account key is not usable: {type(exc).__name__}") from None
            if self._credentials is None:
                self._credentials, self._ambient_project = google.auth.default(scopes=list(_SCOPES))
            if not self._credentials.valid:
                self._credentials.refresh(_BoundedRequest(Request()))
        except DefaultCredentialsError as exc:
            raise VaultReadError("misconfigured", type(exc).__name__) from None
        except RefreshError as exc:
            raise VaultReadError("access_denied", type(exc).__name__) from None
        except TransportError as exc:
            raise VaultReadError("unreachable", type(exc).__name__) from None
        except GoogleAuthError as exc:
            raise VaultReadError("misconfigured", type(exc).__name__) from None
        return str(self._credentials.token)

    async def _access_token(self) -> str:
        async with self._lock:
            if self._credentials is not None and self._credentials.valid:
                return str(self._credentials.token)
            self._refresh_backoff.check()
            try:
                # google-auth's transport is synchronous (and may reach the metadata server).
                token = await asyncio.to_thread(self._refresh_credentials)
            except VaultReadError as exc:
                self._refresh_backoff.failed(exc)
                raise
            self._refresh_backoff.succeeded()
            return token

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=False)
        return self._client

    async def authenticate(self) -> None:
        await self._access_token()

    async def list_secrets(self, prefix: str, *, page_token: str | None = None, page_size: int = 100) -> SecretListing:
        token = await self._access_token()
        project = self.project or self._ambient_project
        if not project:
            raise VaultReadError(
                "misconfigured", f"{self.config.variable('PROJECT')} is not set and the credentials name no project"
            )
        base = prefix.strip("/")
        params: dict[str, str] = {"pageSize": str(min(max(page_size, 1), MAX_PAGE_SIZE))}
        if page_token:
            params["pageToken"] = page_token
        if base:
            # Secret ids hold no "/": a prefix is a start of a name, filtered again below to be exact.
            params["filter"] = f"name:{base}"
        try:
            response = await self._http().get(
                f"{API_ROOT}/projects/{project}/secrets", params=params, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise network_failure(exc) from None
        if response.status_code != 200:
            raise http_failure(response)
        try:
            body = response.json()
        except ValueError:
            raise VaultReadError("invalid_response", "the answer is not JSON") from None
        entries = []
        for secret in body.get("secrets") or [] if isinstance(body, dict) else []:
            secret_id = str(secret.get("name") or "").rsplit("/", 1)[-1] if isinstance(secret, dict) else ""
            if secret_id and secret_id.startswith(base):
                entries.append(ListedSecret(name=secret_id, path=secret_id))
        entries.sort(key=lambda entry: entry.name.lower())
        return SecretListing(entries=entries, next_page_token=body.get("nextPageToken") or None)

    async def read(self, path: str) -> SecretPayload:
        token = await self._access_token()
        name = self.resource_name(path)
        try:
            response = await self._http().get(f"{API_ROOT}/{name}:access", headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as exc:
            raise network_failure(exc) from None
        if response.status_code != 200:
            raise http_failure(response)
        try:
            data = response.json()["payload"]["data"]
            # Strict: a corrupted payload is an invalid answer, never an empty secret.
            return base64.b64decode(data, validate=True).decode("utf-8")
        except (ValueError, KeyError, TypeError, UnicodeDecodeError):
            raise VaultReadError("invalid_response", "the answer holds no UTF-8 payload") from None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _service_account_info(config: VaultProviderConfig) -> dict[str, Any]:
    """The stored service account key, checked when the provider is saved: the JSON Google gives, of a service account."""
    raw = config.secret("SERVICE_ACCOUNT_KEY")
    if not raw:
        raise VaultConfigError(f"{config.variable('SERVICE_ACCOUNT_KEY')} is required with the service_account_key auth method")
    try:
        info = json.loads(raw)
    except ValueError:
        raise VaultConfigError(f"{config.variable('SERVICE_ACCOUNT_KEY')} is not a JSON key file") from None
    if not isinstance(info, dict) or info.get("type") != "service_account" or not info.get("private_key") or not info.get("client_email"):
        raise VaultConfigError(f"{config.variable('SERVICE_ACCOUNT_KEY')} is not the JSON key of a service account")
    project = info.get("project_id")
    if project is not None and not (isinstance(project, str) and _PROJECT_RE.fullmatch(project)):
        raise VaultConfigError(f"the project_id of {config.variable('SERVICE_ACCOUNT_KEY')} is not a Google Cloud project id")
    return {**info, "token_uri": TOKEN_URI}
