"""QuotaPath (quotapath.com), the sales commission management platform.

The whole public API v1 (https://api.quotapath.com/docs/), one tool per
operation (``_tools.OPERATIONS``): users, teams with their managers,
compensation plans and their paths, deals and their crediting (list, read,
create, bulk create, replace, update, delete), quota assignments and values
per path, payouts (all, resolved, unresolved) and the data bulk import. On top:
``get_quotapath_commission_totals`` (commission totals by rep, team, plan,
path, month or quarter) and CSV exports of payouts and deals.

Auth: a workspace API key, sent as ``Authorization: Token <key>``; Test
Connection reads the users list. Writes are named with a write verb and ask a
person on every call. Events by polling (``events``): no webhook in the API.

Not in the public API, so not offered: disputes, adjustments and draws,
payout-period approval or lock, CRM sync status, commission statement PDFs.
Commission amounts are people data.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._client import PROVIDER, QuotaPathClient
from ._tools import build_operation_tools, operations

__all__ = ["build_tools", "operations", "validate_connection"]


async def validate_connection(api_key: str) -> tuple[bool, str]:
    """Test Connection: read the users list. Never echoes the key."""
    client = QuotaPathClient(api_key)
    resp, err = await client.request("GET", "/user/", "read the users", params={"limit": 1})
    if err or resp is None:
        return False, err or f"{PROVIDER} answered nothing"
    data = resp.data
    count = data.get("count") if isinstance(data, dict) else (len(data) if isinstance(data, list) else None)
    users = f"{count} user(s)" if count is not None else "its users"
    return True, f"{PROVIDER} is connected: the key reads {users} of the workspace"


def build_tools(token: str, executor: CallableToolExecutor, **kwargs: Any) -> list[ToolDefinition]:
    """Every QuotaPath tool for the API key; a missing key is named at call time."""
    return build_operation_tools(QuotaPathClient(token), executor, persist_file_fn=kwargs.get("persist_file_fn"))
