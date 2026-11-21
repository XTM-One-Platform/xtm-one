# SPDX-License-Identifier: Apache-2.0

"""Airwallex: treasury, payouts, FX, cards and reports as tools, and webhook events.

Airwallex is a global business account (multi-currency wallets, global
accounts, payouts, FX, card issuing, billing and payment acceptance). The
provider reads what a finance agent needs, one integration per Airwallex
account (legal entity) or one organisation key reaching several accounts
(``login_as`` at sign-in, ``on_behalf_of`` per call), and exports the
financial reports as files.

Money-moving tools (beneficiary, transfer, conversion, card freeze) exist but
are built only when the integration's money-movement switch is on, which it
is not by default; they ask a person on every call, and a read-only binding
withholds them anyway.

``_client.py``: credential, environments, bearer and pacing. ``_tools.py``:
the operation table and masking. ``webhooks.py``: event types and the
signature check.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._client import (
    DEFAULT_ENVIRONMENT,
    ENVIRONMENTS,
    MONEY_MOVEMENT_ENABLED,
    PROVIDER,
    AirwallexClient,
    build_airwallex_token,
    parse_airwallex_token,
    token_from_config,
)
from ._tools import build_operation_tools, operations
from .webhooks import EVENT_TYPES, SIGNATURE_HEADER, TIMESTAMP_HEADER, verify_signature

__all__ = [
    "DEFAULT_ENVIRONMENT",
    "ENVIRONMENTS",
    "EVENT_TYPES",
    "MONEY_MOVEMENT_ENABLED",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "build_airwallex_token",
    "build_tools",
    "operations",
    "parse_airwallex_token",
    "token_from_config",
    "validate_connection",
    "verify_signature",
]


async def validate_connection(token: str) -> tuple[bool, str]:
    """Test Connection: sign in and read the account. Never echoes a secret."""
    creds = parse_airwallex_token(token)
    missing = creds.missing()
    if missing:
        return False, f"Missing {', '.join(missing)}."
    resp, err = await AirwallexClient(creds).request("GET", "/api/v1/account", "read the account")
    if err or resp is None:
        return False, (err or "No answer").removeprefix("Error: ")
    info = resp.data if isinstance(resp.data, dict) else {}
    details = info.get("account_details") if isinstance(info.get("account_details"), dict) else {}
    business = details.get("business_details") if isinstance(details.get("business_details"), dict) else {}
    name = business.get("business_name") or info.get("nickname") or info.get("id") or "unknown"
    money = "money movement on" if creds.money_movement else "read only"
    return True, f"{PROVIDER} ({creds.environment}) is connected to account {name} ({money})"


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Every Airwallex tool for the compound token.

    The tools are built whatever the credential holds; a missing part is
    named at call time, so a half-configured integration says what to fix.
    """
    client = AirwallexClient(parse_airwallex_token(token))
    return build_operation_tools(client, executor, persist_file_fn=kwargs.get("persist_file_fn"))
