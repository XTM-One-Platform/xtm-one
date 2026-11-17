"""AWS Secrets Manager.

Settings (``SECRET_VAULT_<NAME>_*``): ``REGION`` (else the SDK's region from
the environment) and ``ENDPOINT_URL`` (a VPC endpoint, an http(s) URL with no
credentials, query or fragment: it is shown; a database provider's is held to
the destination policy of ``app.services.vault.destinations`` when it is
saved, tested and before each Secrets Manager client is built). An
environment provider's credentials are the SDK's default chain - an IRSA /
EKS Pod Identity role, an ECS task role, an instance profile - never a stored
key.

A database provider also picks its ``AUTH_METHOD``: ``default_chain`` (the
same), ``assume_role`` (``ROLE_ARN`` and an optional ``EXTERNAL_ID``, assumed
with the default chain and renewed before it expires) or ``access_key``
(``ACCESS_KEY_ID`` and the encrypted ``SECRET_ACCESS_KEY``).

A reference path is the secret's name or ARN (``vault://aws/prod/github``);
``#key`` reads one field of a JSON secret string. Listing (the secret picker)
is ``ListSecrets`` filtered by name prefix, the ``/`` of a name read as folders.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.services.vault.base import (
    MAX_PAGE_SIZE,
    REQUEST_TIMEOUT_SECONDS,
    SecretListing,
    SecretPayload,
    VaultProvider,
    child_entries,
    service_address,
)
from app.services.vault.config import VaultConfigError, VaultProviderConfig
from app.services.vault.destinations import VaultAddressRefusedError, assert_provider_addresses_allowed
from app.services.vault.errors import VaultReadError

AUTH_METHODS = ("default_chain", "assume_role", "access_key")
_NOT_FOUND = frozenset({"ResourceNotFoundException"})
_DENIED = frozenset(
    {
        "AccessDeniedException",
        "AccessDenied",
        "UnrecognizedClientException",
        "InvalidClientTokenId",
        "ExpiredTokenException",
        "InvalidSignatureException",
        "KMSAccessDeniedException",
        "SignatureDoesNotMatch",
    }
)
_BAD_REQUEST = frozenset({"InvalidParameterException", "InvalidRequestException", "ValidationException"})
#: How long an assumed role's session is requested for, and renewed this long before its end.
_ROLE_SESSION_SECONDS = 3600
_ROLE_SESSION_NAME = "xtm-one-secret-vault"


class AWSSecretsManagerProvider(VaultProvider):
    kind = "aws"

    def __init__(self, config: VaultProviderConfig) -> None:
        super().__init__(config)
        self.region = config.get("REGION")
        self.endpoint_url = service_address(config, "ENDPOINT_URL") if config.get("ENDPOINT_URL") else ""
        self.auth_method = (config.get("AUTH_METHOD") or "default_chain").lower() if config.from_database else "default_chain"
        if self.auth_method not in AUTH_METHODS:
            raise VaultConfigError(f"{config.variable('AUTH_METHOD')} is one of {', '.join(AUTH_METHODS)}")
        if self.auth_method == "assume_role" and not config.get("ROLE_ARN").startswith("arn:"):
            raise VaultConfigError(f"{config.variable('ROLE_ARN')} is the ARN of the role to assume (arn:aws:iam::...)")
        if self.auth_method == "access_key" and not (config.get("ACCESS_KEY_ID") and config.secret("SECRET_ACCESS_KEY")):
            raise VaultConfigError(
                f"{config.variable('ACCESS_KEY_ID')} and {config.variable('SECRET_ACCESS_KEY')} are required with the access_key auth method"
            )
        self._client: Any = None
        self._client_lock = asyncio.Lock()

    def describe(self) -> dict[str, str]:
        details = {**super().describe(), "auth_method": "iam_role" if self.auth_method == "default_chain" else self.auth_method}
        if self.region:
            details["region"] = self.region
        if self.endpoint_url:
            details["address"] = self.endpoint_url
        return details

    def _session(self) -> Any:
        import boto3

        if self.auth_method == "access_key":
            return boto3.session.Session(
                aws_access_key_id=self.config.get("ACCESS_KEY_ID"),
                aws_secret_access_key=self.config.secret("SECRET_ACCESS_KEY"),
                region_name=self.region or None,
            )
        session = boto3.session.Session(region_name=self.region or None)
        if self.auth_method != "assume_role":
            return session
        from botocore.credentials import RefreshableCredentials
        from botocore.session import get_session

        sts = session.client("sts", config=self._client_config())

        def _assume() -> dict[str, Any]:
            kwargs: dict[str, Any] = {
                "RoleArn": self.config.get("ROLE_ARN"),
                "RoleSessionName": _ROLE_SESSION_NAME,
                "DurationSeconds": _ROLE_SESSION_SECONDS,
            }
            if self.config.get("EXTERNAL_ID"):
                kwargs["ExternalId"] = self.config.get("EXTERNAL_ID")
            credentials = sts.assume_role(**kwargs)["Credentials"]
            return {
                "access_key": credentials["AccessKeyId"],
                "secret_key": credentials["SecretAccessKey"],
                "token": credentials["SessionToken"],
                "expiry_time": credentials["Expiration"].isoformat(),
            }

        botocore_session = get_session()
        # botocore has no public setter for credentials it renews itself before they expire.
        botocore_session._credentials = RefreshableCredentials.create_from_metadata(
            metadata=_assume(), refresh_using=_assume, method="sts-assume-role"
        )
        if self.region:
            botocore_session.set_config_variable("region", self.region)
        return boto3.session.Session(botocore_session=botocore_session)

    def _client_config(self) -> Any:
        from botocore.config import Config

        return Config(
            connect_timeout=REQUEST_TIMEOUT_SECONDS,
            read_timeout=REQUEST_TIMEOUT_SECONDS,
            retries={"max_attempts": 2, "mode": "standard"},
        )

    def _build_client(self) -> Any:
        kwargs: dict[str, Any] = {"config": self._client_config()}
        if self.region:
            kwargs["region_name"] = self.region
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        return self._session().client("secretsmanager", **kwargs)

    async def _secrets_manager(self) -> Any:
        async with self._client_lock:
            if self._client is None:
                # The SDK resolves and connects itself: a database provider's endpoint is judged before each client.
                try:
                    await assert_provider_addresses_allowed(self.config)
                except VaultAddressRefusedError:
                    raise VaultReadError("misconfigured", "the endpoint URL is not an address a vault may be reached at") from None
                # Resolving the credential chain may reach the instance metadata service, and assuming a role STS.
                self._client = await asyncio.to_thread(self._call_mapped, self._build_client)
            return self._client

    def _call_mapped(self, call: Any, *args: Any, **kwargs: Any) -> Any:
        """*call*, with what the SDK raises mapped to a :class:`VaultReadError` reason."""
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, NoRegionError

        try:
            return call(*args, **kwargs)
        except ClientError as exc:
            code = str((exc.response or {}).get("Error", {}).get("Code") or "")
            if code in _NOT_FOUND:
                raise VaultReadError("not_found", code) from None
            if code in _DENIED:
                raise VaultReadError("access_denied", code) from None
            if code in _BAD_REQUEST:
                raise VaultReadError("invalid_reference", code) from None
            raise VaultReadError("unreachable", code or "ClientError") from None
        except (NoCredentialsError, NoRegionError) as exc:
            raise VaultReadError("misconfigured", type(exc).__name__) from None
        except BotoCoreError as exc:
            raise VaultReadError("unreachable", type(exc).__name__) from None

    async def _call(self, method: str, **kwargs: Any) -> Any:
        client = await self._secrets_manager()
        return await asyncio.to_thread(self._call_mapped, getattr(client, method), **kwargs)

    async def read(self, path: str) -> SecretPayload:
        answer = await self._call("get_secret_value", SecretId=path)
        secret = answer.get("SecretString")
        if isinstance(secret, str):
            return secret
        binary = answer.get("SecretBinary")
        if isinstance(binary, bytes | bytearray):
            try:
                return bytes(binary).decode("utf-8")
            except UnicodeDecodeError:
                raise VaultReadError("invalid_response", "the binary secret is not UTF-8 text") from None
        raise VaultReadError("invalid_response", "the answer holds no secret string")

    async def authenticate(self) -> None:
        # The caller's identity, which any credential may ask for: proves the credentials before any secret is read.
        session = await asyncio.to_thread(self._call_mapped, self._session)
        sts = await asyncio.to_thread(self._call_mapped, session.client, "sts", config=self._client_config())
        await asyncio.to_thread(self._call_mapped, sts.get_caller_identity)

    async def list_secrets(self, prefix: str, *, page_token: str | None = None, page_size: int = 100) -> SecretListing:
        base = prefix.strip("/")
        kwargs: dict[str, Any] = {"MaxResults": min(max(page_size, 1), 100)}
        if base:
            kwargs["Filters"] = [{"Key": "name", "Values": [f"{base}/"]}]
        if page_token:
            kwargs["NextToken"] = page_token
        names: list[str] = []
        next_token: str | None = None
        # A page of names may fold into a few folders: read on until the page is full or the list ends.
        while len(names) < min(page_size, MAX_PAGE_SIZE):
            answer = await self._call("list_secrets", **kwargs)
            names.extend(str(item.get("Name")) for item in answer.get("SecretList") or [] if item.get("Name"))
            next_token = answer.get("NextToken") or None
            if not next_token:
                break
            kwargs["NextToken"] = next_token
        return SecretListing(entries=child_entries(base, names), next_page_token=next_token)

    async def aclose(self) -> None:
        self._client = None
