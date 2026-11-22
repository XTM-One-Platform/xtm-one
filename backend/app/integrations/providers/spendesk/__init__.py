# SPDX-License-Identifier: Apache-2.0

"""Spendesk: the whole Public API as tools, multi-entity, and webhook events.

Spendesk is a spend management platform: company wallets and their loads,
card payments and transactions, requests and approvals, payables (expenses,
invoices, card payments) and their receipts, cards, members and teams,
cost centres, analytical fields, expense categories, suppliers, the chart of
accounts, accounting journals and settlements. The provider covers every
operation of its Public API (``operations.json``), one tool each, plus the
receipt and purchase order downloads, so a finance agent can reconcile the
wallet each month, chase missing receipts and prepare the bookkeeping.

Credential: a Public API client (client id + secret) of a company or of the
organisation (every entity); see ``_client.py`` for auth, entity, pacing and
idempotency, ``_tools.py`` for how an operation becomes a tool (reads by HTTP
method, writes sensitive and always asked, bank details masked) and
``webhooks.py`` for the event types and the signature check.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._client import (
    DEFAULT_ENVIRONMENT,
    ENVIRONMENTS,
    PROVIDER,
    SpendeskClient,
    build_spendesk_token,
    parse_spendesk_token,
    token_from_config,
)
from ._tools import BANK_DETAILS_GROUP, bank_details_tools, build_operation_tools, operations
from .webhooks import EVENT_TYPES, SIGNATURE_HEADER, TRIGGER_EVENTS, verify_signature

__all__ = [
    "BANK_DETAILS_GROUP",
    "DEFAULT_ENVIRONMENT",
    "ENVIRONMENTS",
    "EVENT_TYPES",
    "SIGNATURE_HEADER",
    "TRIGGER_EVENTS",
    "bank_details_tools",
    "build_spendesk_token",
    "build_tools",
    "operations",
    "parse_spendesk_token",
    "token_from_config",
    "validate_connection",
    "verify_signature",
]


async def validate_connection(token: str) -> tuple[bool, str]:
    """Test Connection: mint a token and read the organisation's entities.

    An organisation-level credential lists its companies; a company-level one
    is refused that list (403) and is checked with a read of its wallet.
    Never echoes a secret.
    """
    creds = parse_spendesk_token(token)
    missing = creds.missing()
    if missing:
        return False, f"Missing {', '.join(missing)}."
    client = SpendeskClient(creds)
    resp, err = await client.request("GET", "/v2/companies", "list the organisation's companies")
    if resp is not None and err is None:
        rows = resp.data.get("data") if isinstance(resp.data, dict) else None
        names = [str(c.get("name") or c.get("id")) for c in rows or [] if isinstance(c, dict)]
        listed = ", ".join(names[:10]) + (f" and {len(names) - 10} more" if len(names) > 10 else "")
        return True, f"{PROVIDER} ({creds.environment}) organisation access: {len(names)} entities ({listed or 'none'})"
    if err and "403" not in err:
        return False, err.removeprefix("Error: ")
    resp, err = await client.request("GET", "/v1/wallet-summary", "read the wallet summary")
    if err or resp is None:
        return False, (err or "No answer").removeprefix("Error: ")
    return True, f"{PROVIDER} ({creds.environment}) company access: the wallet summary reads"


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Every Spendesk tool for the compound ``environment||client_id||client_secret``.

    The tools are built whatever the credential holds; a missing part is
    named at call time, so a half-configured integration says what to fix.
    """
    client = SpendeskClient(parse_spendesk_token(token))
    return build_operation_tools(client, executor, persist_file_fn=kwargs.get("persist_file_fn"))
