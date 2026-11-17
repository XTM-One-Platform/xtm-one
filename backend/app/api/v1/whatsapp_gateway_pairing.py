# SPDX-License-Identifier: Apache-2.0

"""WhatsApp gateway pairing card - ``GET /integrations/{id}/whatsapp/pairing``.

Answers the Pairing card of a ``whatsapp`` integration on the linked-device gateway
backend (``backend = linked_device_gateway``): the live pairing state, the login
QR while the state is ``qr``, and the gateway webhook path with its secret.

Who sees what:

* the pairing state: everyone who can open the integration (an integration the caller
  cannot see is unknown, 404; one that is not a gateway ``whatsapp`` integration, 404);
* the QR and the webhook path with its ``token``: only a caller who may manage the
  integration (``can_manage``). Scanning the QR links a phone to the integration's
  number, and the token lets anyone post inbound messages into its store.

The gateway is read through the same client the agent tools use
(``GatewayClient.health`` / ``login_qr`` and ``pairing_state``), through the
integration's relay group when it has one, so the card and the tools never disagree.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import can_manage, get_user_group_ids, get_user_integration
from app.core.database import get_db
from app.core.egress import egress_route
from app.core.utils import format_exception_message
from app.integrations.providers.whatsapp_gateway import (
    BACKEND_GATEWAY,
    DEFAULT_VENDOR,
    GATEWAY_CLIENTS,
    WHAPI_BASE_URL,
    GatewayClient,
)
from app.models.integration import Integration
from app.models.user import User
from app.services.relays import integration_egress_route
from app.services.vault import reveal_field, reveal_json_field

router = APIRouter()

PROVIDER = "whatsapp"
SECRET_FIELD = "webhook_secret"  # noqa: S105 - the extra field's name, not a secret
WEBHOOK_PATH = "/api/channels/whatsapp/gateway/{integration_id}"
_NOT_FOUND = "Integration not found"

PairingState = Literal["connected", "qr", "banned", "sync_error", "starting", "unknown"]
#: Why the card could not read the gateway, as a code the frontend turns into a translated
#: sentence; ``error`` keeps the raw detail for a tooltip.
PairingErrorCode = Literal["gateway_unreachable", "no_channel_token", "token_unreadable", "unknown_vendor"]


class PairingResponse(BaseModel):
    """The Pairing card of a gateway ``whatsapp`` integration."""

    vendor: str
    #: The pairing state (``WhapiClient.pairing_state`` mapping); ``unknown`` when the gateway did not answer.
    state: PairingState
    #: Why the gateway could not be read, as a code; ``None`` when it answered.
    error_code: PairingErrorCode | None = None
    #: The raw detail behind ``error_code`` (English, for a tooltip); ``None`` when it answered.
    error: str | None = None
    #: Whether the caller may manage the integration: the QR and the webhook path are for them only.
    can_manage: bool = False
    #: The login QR as a ``data:image/png;base64,...`` URI, only while ``state`` is ``qr`` and for a manager.
    qr: str | None = None
    #: ``/api/channels/whatsapp/gateway/<id>?token=<secret>`` for a manager; ``None`` otherwise.
    webhook_path: str | None = None
    #: Whether a webhook secret is set (inbound deliveries are refused without one).
    has_webhook_secret: bool = False


async def _extra(integration: Integration) -> dict[str, Any]:
    """*integration*'s extra config, each vault reference in it resolved (#6080)."""
    if not integration.extra_config_enc:
        return {}
    try:
        extra = await reveal_json_field(integration, "extra_config_enc")
    except Exception:  # an unreadable config or secret is a config without a gateway
        return {}
    return extra if isinstance(extra, dict) else {}


async def _load(db: AsyncSession, user: User, integration_id: str) -> tuple[Integration, dict[str, Any], bool]:
    """The gateway ``whatsapp`` integration, its extra config and whether *user* may manage it."""
    group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(db, user.id, integration_id, group_ids, is_admin=user.is_admin)
    if integration.provider != PROVIDER:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    extra = await _extra(integration)
    if str(extra.get("backend") or "").strip() != BACKEND_GATEWAY:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return integration, extra, can_manage(user, integration, group_ids)


def qr_data_uri(data: Any) -> str | None:
    """The login QR of a gateway answer as a PNG data URI, ``None`` when it carries none."""
    encoded = str(data.get("base64") or "").strip() if isinstance(data, dict) else ""
    if not encoded:
        return None
    if encoded.startswith("data:"):
        return encoded
    return f"data:image/png;base64,{encoded}"


async def read_pairing(client: GatewayClient, *, with_qr: bool) -> tuple[PairingState, str | None, str | None]:
    """``(state, qr, error)`` from the gateway: the QR only when asked and the state is ``qr``."""
    health, err = await client.health()
    if err:
        return "unknown", None, err
    state: PairingState = client.pairing_state(health)  # type: ignore[assignment]
    if state != "qr" or not with_qr:
        return state, None, None
    data, err = await client.login_qr()
    if err:
        return state, None, err
    return state, qr_data_uri(data), None


@router.get("/{integration_id}/whatsapp/pairing", response_model=PairingResponse)
async def get_whatsapp_pairing(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PairingResponse:
    """The live pairing state of the gateway, its login QR and the webhook path for a manager."""
    integration, extra, manages = await _load(db, user, integration_id)
    vendor = str(extra.get("gateway_vendor") or "").strip() or DEFAULT_VENDOR
    secret = str(extra.get(SECRET_FIELD) or "")
    response = PairingResponse(vendor=vendor, state="unknown", can_manage=manages, has_webhook_secret=bool(secret))
    if manages and secret:
        response.webhook_path = f"{WEBHOOK_PATH.format(integration_id=integration.id)}?token={secret}"
    client_cls = GATEWAY_CLIENTS.get(vendor)
    if client_cls is None:
        response.error_code, response.error = "unknown_vendor", f"Unknown gateway vendor '{vendor}'."
        return response
    try:
        token = (await reveal_field(integration, "api_key_enc") or "").strip()
    except Exception as exc:
        response.error_code = "token_unreadable"
        response.error = f"The channel token could not be read: {format_exception_message(exc)}"
        return response
    if not token:
        response.error_code, response.error = "no_channel_token", "No channel token in API key."
        return response
    base_url = str(extra.get("base_url") or "").strip() or WHAPI_BASE_URL
    client = client_cls(base_url, token)
    route = await integration_egress_route(db, integration)
    with egress_route(route):
        state, qr, error = await read_pairing(client, with_qr=manages)
    response.state, response.qr, response.error = state, qr, error
    if error:
        response.error_code = "gateway_unreachable"
    return response
