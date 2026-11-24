"""Pennylane integration - accounting, invoicing and banking (Company API v2).

API: https://pennylane.readme.io/reference (Company API v2, ``/api/external/v2``).
Auth: a Bearer token. A company token reaches one company; a firm token
reaches several and names the company in ``X-Company-Id``: the integration
then stores ``company_id||token``.

One module per domain, each a ``register(ctx, executor)``:

* ``accounting`` - company, fiscal years, journals, ledger accounts, ledger
                   entries and lines, trial balance, FEC / GL / analytical GL
                   exports, ledger entry writes.
* ``invoices``   - customer and supplier invoices, lines, payments, matched
                   transactions, PDF download, draft creation, matching,
                   paid and payment status, accounting validation.
* ``parties``    - customers, suppliers (IBAN masked), products, quotes.
* ``banking``    - bank accounts, transactions, analytical categories and
                   categorisation, file attachments, changelogs.
* ``catalog``    - every other endpoint of the API, one tool each: bank
                   accounts and establishments, SEPA / GoCardless / Pro
                   account mandates, lettering, products, quotes, commercial
                   documents, billing subscriptions, purchase requests,
                   invoice lifecycle, webhooks.

A tool is a read when its endpoint is a GET, else a write. Writes that touch
money, bank data or mandates, send a document outside, or delete declare
``always_ask``: each call waits for a person's approval. The bank group is
:data:`BANK_DETAIL_TOOLS`.
"""

from __future__ import annotations

from typing import Any

from app.integrations.base import BaseIntegration, OAuthDefaults
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import accounting, banking, catalog, invoices, parties
from ._common import PennylaneContext
from .banking import BANK_ACCOUNT_TOOLS
from .catalog import CATALOG_BANK_TOOLS

#: Every tool that shows or changes bank accounts, establishments or mandates:
#: one named group a binding or a deny list can withhold.
BANK_DETAIL_TOOLS = BANK_ACCOUNT_TOOLS | CATALOG_BANK_TOOLS

__all__ = ["BANK_DETAIL_TOOLS", "PennylaneIntegration", "build_tools", "validate_connection"]


async def _me(token: str) -> tuple[bool, str, dict[str, Any]]:
    ctx = PennylaneContext.from_token(token)
    if not ctx.api_token:
        return False, "Missing Pennylane API token.", {}
    async with ctx.client() as client:
        data, err = await ctx.get(client, "me", action="read the token's company")
    if err:
        return False, err.removeprefix("Error: "), {}
    company = data.get("company") or {}
    scopes = sorted(str(s) for s in data.get("scopes") or [])
    return (
        True,
        f"Connected to Pennylane company '{company.get('name')}' (#{company.get('id')}) with {len(scopes)} scopes"
        + (f": {', '.join(scopes)}" if scopes else "")
        + ".",
        data,
    )


async def validate_connection(token: str) -> tuple[bool, str]:
    """``(ok, message)`` for the integration's Test button: ``GET /me``."""
    ok, message, _ = await _me(token)
    return ok, message


class PennylaneIntegration(BaseIntegration):
    provider_name = "pennylane"
    display_name = "Pennylane"
    icon = "Calculator"
    description = (
        "Connect Pennylane to read the ledger, trial balance and exports, customer and supplier "
        "invoices with their payments, bank transactions and analytics, and to draft invoices, "
        "match transactions and categorise."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        ok, _ = await validate_connection(token)
        return ok

    async def get_user_info(self, token: str) -> dict[str, Any]:
        ok, message, data = await _me(token)
        if not ok:
            return {}
        company = data.get("company") or {}
        return {
            "name": company.get("name") or "Pennylane company",
            "scopes": data.get("scopes") or [],
            "status": message,
        }


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Every Pennylane tool, for the stored credential *token* (``[company_id||]token``)."""
    ctx = PennylaneContext.from_token(token, persist_file_fn=kwargs.get("persist_file_fn"))
    tools: list[ToolDefinition] = []
    for module in (accounting, invoices, parties, banking, catalog):
        tools += module.register(ctx, executor)
    return tools
