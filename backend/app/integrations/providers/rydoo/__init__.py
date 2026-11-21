# SPDX-License-Identifier: Apache-2.0

"""Rydoo: the whole public expense API as tools, and expense webhook events.

Rydoo is an expense management platform. The provider covers every
operation of its public API (``operations.json``: 117 operations in 18
families), one tool each, so a Finance agent can read expenses ready for
accounting by branch (legal entity), user, group or trip, download receipts,
follow reimbursements and transactions, read and maintain users, groups,
roles, approval and control flows, projects, categories, custom fields (cost
centres, GL codes), trips, guests, linked accounts, exchange rates,
e-invoices and payment methods (the ``bank_details`` family, numbers masked).

Every write (reject, mark reported or reimbursed, add an expense, create or
update a user, group, project, list item or payment method, edit an approval
flow) is built and asks a person on each call.

Credential: an API client (client id + client secret) Rydoo issues for the
company; see ``_client.py`` for auth, pacing and retries, ``_tools.py`` for
how an operation becomes a tool and ``webhooks.py`` for the events.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._client import PROVIDER, RydooClient, build_rydoo_token, parse_rydoo_token, token_from_config
from ._tools import BANK_DETAILS_GROUP, build_operation_tools, operations, sanitize
from .webhooks import EVENT_TYPES, TRIGGER_EVENTS, is_delivery, trigger_event

__all__ = [
    "BANK_DETAILS_GROUP",
    "EVENT_TYPES",
    "TRIGGER_EVENTS",
    "build_rydoo_token",
    "build_tools",
    "is_delivery",
    "operations",
    "parse_rydoo_token",
    "sanitize",
    "token_from_config",
    "trigger_event",
    "validate_connection",
]


async def validate_connection(token: str) -> tuple[bool, str]:
    """Test Connection: mint a token and list the company's branches (entities).

    Never echoes a secret.
    """
    creds = parse_rydoo_token(token)
    missing = creds.missing()
    if missing:
        return False, f"Missing {', '.join(missing)}."
    resp, err = await RydooClient(creds).request(
        "GET", "/v2/branches", "list the branches", params={"limit": 50, "isActive": True}
    )
    if err or resp is None:
        return False, (err or "No answer").removeprefix("Error: ")
    data = resp.data
    rows = (data.get("items") or data.get("data") or []) if isinstance(data, dict) else data
    rows = rows if isinstance(rows, list) else []
    names = [str(row.get("name")) for row in rows if isinstance(row, dict) and row.get("name")]
    listed = ", ".join(names[:10]) + (" ..." if len(names) > 10 else "")
    return True, f"{PROVIDER} is connected: {len(rows)} active branch(es){': ' + listed if listed else ''}"


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Every Rydoo tool for the compound ``client_id||client_secret``.

    The tools are built whatever the credential holds; a missing part is
    named at call time, so a half-configured integration says what to fix.
    """
    client = RydooClient(parse_rydoo_token(token))
    return build_operation_tools(client, executor, persist_file_fn=kwargs.get("persist_file_fn"))
