"""Client secrets of an Entra app registration, through Microsoft Graph.

The password credentials of an application object
(``POST /applications/{id}/addPassword`` and ``/removePassword``), called with
an :class:`EntraClient` built from an Entra integration in app-only (API key)
mode: the platform's own credential, never a user's portal token. The provider
registers them as its :class:`~app.integrations.app_secrets.AppSecretMinter`,
which the administrator tool ``create_integration_app_secret`` reads through
the generic seam; the tool writes the secret straight into the platform and
never hands it to a model.

Graph needs the application permission ``Application.ReadWrite.All``, or
``Application.ReadWrite.OwnedBy`` with the target app among the ones the
calling app owns. Errors come back as sentences naming what is missing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.utils import format_exception_message, safe_uuid
from app.integrations.app_secrets import (
    AppRegistration,
    AppSecretMinter,
    MintedSecret,
    register_app_secret_minter,
)

from ._common import EntraClient

logger = logging.getLogger(__name__)

#: Microsoft caps a client secret's lifetime at two years.
MAX_SECRET_DAYS = 730

_PERMISSION_HINT = (
    "the credential's app needs the Microsoft Graph application permission "
    "Application.ReadWrite.All (or Application.ReadWrite.OwnedBy and ownership of "
    "the target app), with admin consent granted"
)

_NO_CREDENTIAL = (
    "Error: no app-only Microsoft Entra credential on this platform to call Microsoft Graph with. "
    "Create an Entra integration in API key (app-only) mode holding tenant_id, client_id and "
    "client_secret of an app granted the application permission Application.ReadWrite.All (or "
    "Application.ReadWrite.OwnedBy and ownership of the target app), then call again."
)


def _client(client_or_token: EntraClient | str) -> EntraClient:
    if isinstance(client_or_token, str):
        return EntraClient(client_or_token)
    return client_or_token


def _status_problem(resp: Any, err: str | None, what: str) -> str:
    status = getattr(resp, "status_code", None)
    if status == 403:
        return f"Error 403: Microsoft Graph refused to {what}: {_PERMISSION_HINT}."
    if status == 404:
        return f"Error 404: no Entra app registration found to {what} (check app_id / object_id and the tenant)."
    return err or f"Error: Microsoft Graph could not {what}."


async def read_credential(row: Any) -> tuple[str | None, str | None]:
    """The compound app-only token ``tenant_id||client_id||client_secret`` of *row*, or the error sentence."""
    from app.core.security import decrypt_value
    from app.services.vault import reveal_field, reveal_json_field

    try:
        extra = await reveal_json_field(row, "extra_config_enc") or {}
        tenant_id = str(extra.get("tenant_id") or "").strip() if isinstance(extra, dict) else ""
        client_id = (decrypt_value(row.client_id_enc) if row.client_id_enc else "").strip()
        client_secret = ((await reveal_field(row, "client_secret_enc")) or "").strip()
    except Exception as exc:
        logger.warning("Entra credential %s cannot be read: %s", row.id, format_exception_message(exc))
        return None, f"Error: the credential of integration {row.name} ({row.id}) cannot be read."
    missing = [
        label
        for label, value in (("tenant_id", tenant_id), ("client_id", client_id), ("client_secret", client_secret))
        if not value
    ]
    if missing:
        return None, f"Error: integration {row.name} ({row.id}) lacks {', '.join(missing)} for app-only access."
    return f"{tenant_id}||{client_id}||{client_secret}", None


async def find_app(
    client: EntraClient | str, *, app_id: str = "", object_id: str = ""
) -> tuple[AppRegistration | None, str | None]:
    """The app registration named by its application id or its object id."""
    client = _client(client)
    if object_id:
        if safe_uuid(object_id) is None:
            return None, "Error: object_id must be a GUID."
        path = f"/applications/{object_id}"
    elif app_id:
        if safe_uuid(app_id) is None:
            return None, "Error: app_id must be a GUID."
        path = f"/applications(appId='{app_id}')"
    else:
        return None, "Error: app_id or object_id is required."
    resp, err = await client.request(
        "get",
        path,
        "read the app registration",
        params={"$select": "id,appId,displayName"},
        expected_statuses={403, 404},
    )
    if err or resp is None:
        return None, _status_problem(resp, err, "read the app registration")
    try:
        data = resp.json()
    except ValueError:
        return None, "Error: Microsoft Graph returned a non-JSON answer for the app registration."
    if not data.get("id") or not data.get("appId"):
        return None, "Error: Microsoft Graph returned an app registration without its ids."
    return AppRegistration(app_id=data["appId"], object_id=data["id"], display_name=data.get("displayName") or ""), None


async def add_secret(
    client: EntraClient | str, app: AppRegistration, *, display_name: str, days: int
) -> tuple[MintedSecret | None, str | None]:
    """Add a client secret valid *days* days to *app*."""
    client = _client(client)
    days = max(1, min(int(days), MAX_SECRET_DAYS))
    end = (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0)
    resp, err = await client.request(
        "post",
        f"/applications/{app.object_id}/addPassword",
        "add a client secret",
        json={"passwordCredential": {"displayName": display_name, "endDateTime": end.isoformat().replace("+00:00", "Z")}},
        expected_statuses={403, 404},
    )
    if err or resp is None:
        return None, _status_problem(resp, err, "add a client secret")
    try:
        data = resp.json()
    except ValueError:
        return None, "Error: Microsoft Graph returned a non-JSON answer to addPassword."
    value = data.get("secretText") or ""
    key_id = data.get("keyId") or ""
    if not value or not key_id:
        return None, "Error: Microsoft Graph added no usable secret (no keyId or secretText in its answer)."
    return MintedSecret(
        key_id=key_id,
        value=value,
        expires_at=data.get("endDateTime") or end.isoformat(),
        display_name=data.get("displayName") or display_name,
    ), None


async def remove_secret(client: EntraClient | str, app: AppRegistration, key_id: str) -> str | None:
    """Remove the password credential *key_id* of *app*; the error sentence, ``None`` when removed."""
    client = _client(client)
    if safe_uuid(key_id) is None:
        return "Error: key_id must be a GUID."
    resp, err = await client.request(
        "post",
        f"/applications/{app.object_id}/removePassword",
        "remove a client secret",
        json={"keyId": key_id},
        expected_statuses={403, 404},
    )
    if err or resp is None:
        return _status_problem(resp, err, "remove a client secret")
    return None


def register() -> None:
    """Declare the Entra app-only credential as the platform's minter of app secrets."""
    register_app_secret_minter(
        AppSecretMinter(
            provider="entra",
            display_name="Microsoft Entra ID",
            max_days=MAX_SECRET_DAYS,
            missing_credential=_NO_CREDENTIAL,
            read_credential=read_credential,
            find_app=find_app,
            add_secret=add_secret,
            remove_secret=remove_secret,
        )
    )
