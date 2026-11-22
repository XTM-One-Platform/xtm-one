# SPDX-License-Identifier: Apache-2.0

"""Odoo: accounting reads, reports, exports and guarded writes, multi-company.

One integration is one Odoo database signed in as one user with an API key
(``base_url||database||login||api_key[||company_ids]``); every tool takes the
companies it acts for (``allowed_company_ids``), so one row serves every
company the user can see.

``_client.py``: the external API (JSON-2, JSON-RPC fallback) and fault
mapping. ``_render.py``: masking and bounded answers. ``_reports.py``: trial
balance, general ledger, aged balances, FEC rows and file builders.
``_tools.py``: the tools. ``events.py``: polling for posted entries, vendor
bills and payments.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._client import PROVIDER, OdooClient, OdooCredential, OdooError
from ._tools import BANK_DETAIL_TOOLS, build_operation_tools

__all__ = ["BANK_DETAIL_TOOLS", "OdooCredential", "build_tools", "validate_connection"]


async def validate_connection(token: str) -> tuple[bool, str]:
    """Test Connection: the server version, then the user and its companies."""
    cred = OdooCredential.parse(token)
    missing = cred.missing()
    if missing:
        return False, f"Missing {missing}."
    try:
        async with OdooClient(credential=cred) as cli:
            info = await cli.version()
            ctx = await cli.call("res.users", "context_get", action="read the signed-in user")
            companies = await cli.search_read("res.company", [], ["name"], order="id", action="list the companies")
            transport = cli.transport
    except OdooError as exc:
        return False, str(exc).removeprefix("Error: ")
    version = info.get("server_version") or "unknown version"
    names = ", ".join(f"{c.get('name')} (#{c.get('id')})" for c in companies[:10])
    more = f" and {len(companies) - 10} more" if len(companies) > 10 else ""
    tz = ctx.get("tz") if isinstance(ctx, dict) else None
    return True, (
        f"{PROVIDER} {version} ({'JSON-2' if transport == 'json2' else 'JSON-RPC'}) on database {cred.database} "
        f"as {cred.login}{f' ({tz})' if tz else ''}; {len(companies)} companies visible: {names}{more}."
    )


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Every Odoo tool; a missing credential part is named at call time."""
    return build_operation_tools(token, executor, persist_file_fn=kwargs.get("persist_file_fn"))
