# SPDX-License-Identifier: Apache-2.0

"""DealHub CPQ: every documented operation of its APIs as tools.

DealHub is a configure-price-quote platform. The provider covers the Quote
API (quotes with their line items, totals, discounts, playbook answers and
approval steps, quote documents, DealRoom signers, opportunities), the
Headless API (simulate, generate, submit for approval, publish, sign
externally), the CRM API (quote and opportunity sessions, CRM account
imports), the User API, the Partner API, the Pricing API (price book) and the
Version API (versions, playbooks, product catalog and hierarchy, asynchronous
catalog requests): 52 operations, one tool each (``_operations.py``).

Read or write follows the HTTP method: every GET is a read, every other call
asks a person before it runs. Credential: the API token a CPQ administrator
generates and the account's instance host (``_client.py``).
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._client import (
    DEFAULT_HOST,
    PROVIDER,
    DealHubClient,
    build_dealhub_token,
    normalize_host,
    parse_dealhub_token,
    probe,
)
from ._operations import OPERATIONS
from ._tools import build_operation_tools

__all__ = [
    "DEFAULT_HOST",
    "OPERATIONS",
    "PROVIDER",
    "build_dealhub_token",
    "build_tools",
    "normalize_host",
    "parse_dealhub_token",
    "validate_connection",
]


async def validate_connection(token: str) -> tuple[bool, str]:
    """Test Connection and save: read the account's active users. Never echoes the token."""
    return await probe(parse_dealhub_token(token))


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Every DealHub tool for the compound ``host||token`` (a bare token: the default host).

    The tools are built whatever the credential holds; a missing token or a
    host outside dealhub.io is named at call time.
    """
    client = DealHubClient(parse_dealhub_token(token))
    return build_operation_tools(client, executor.register, persist_file_fn=kwargs.get("persist_file_fn"))
