"""What every Pennylane domain module shares.

Pennylane Company API v2 (``https://app.pennylane.com/api/external/v2``):

* Auth: ``Authorization: Bearer <token>``. A company token reaches one
  company; a firm (accounting practice) token reaches several and names the
  company of each request in ``X-Company-Id``. The integration stores the
  token and, for a firm token, the company id (``company_id||token``).
* Scopes: each token holds ``<resource>:readonly`` / ``<resource>:all``
  scopes. A missing scope is HTTP 403; :func:`scope_hint` names the scope
  the endpoint needs, from the API's own security table.
* Lists page with ``cursor`` / ``limit`` and answer ``items``, ``has_more``
  and ``next_cursor``. Filters are a JSON list of ``{field, operator,
  value}`` in the ``filter`` query parameter (:func:`filters`).
* Rate limit: 25 requests per 5 seconds per token; a 429 carries
  ``Retry-After`` and is retried by :func:`safe_request`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    app_token_cache_key,
    format_pagination_hint,
    get_shared_rate_limiter,
    safe_request,
)
from app.tools.base import ToolDefinition

BASE_URL = "https://app.pennylane.com/api/external/v2"
APP_URL = "https://app.pennylane.com"
PROVIDER = "Pennylane"
SOURCE = "integration:pennylane"
#: Largest page Pennylane serves on most lists (trial balance and changelogs: 1,000).
MAX_PAGE = 100
DEFAULT_PAGE = 25
TIMEOUT = 30
#: Longest free text (labels, descriptions) one value shows.
MAX_TEXT = 300

_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
#: 25 requests per 5 seconds per token.
_RATE_PER_SECOND = 5.0
_BURST = 10.0
_MAX_IN_FLIGHT = 4

#: The scope an endpoint family needs (read, write), from the API's security table.
_SCOPES: tuple[tuple[str, str, str], ...] = (
    ("changelogs/customer_invoices", "customer_invoices:readonly", "customer_invoices:all"),
    ("changelogs/supplier_invoices", "supplier_invoices:readonly", "supplier_invoices:all"),
    ("changelogs/customers", "customers:readonly", "customers:all"),
    ("changelogs/suppliers", "suppliers:readonly", "suppliers:all"),
    ("changelogs/products", "products:readonly", "products:all"),
    ("changelogs/transactions", "transactions:readonly", "transactions:all"),
    ("changelogs/quotes", "quotes:readonly", "quotes:all"),
    ("changelogs/ledger", "ledger_entries:readonly", "ledger_entries:all"),
    ("exports/fecs", "exports:fec", "exports:fec"),
    ("exports/general_ledgers", "exports:gl", "exports:gl"),
    ("exports/analytical_general_ledgers", "exports:agl", "exports:agl"),
    ("customer_invoices", "customer_invoices:readonly", "customer_invoices:all"),
    ("supplier_invoices", "supplier_invoices:readonly", "supplier_invoices:all"),
    ("company_customers", "customers:readonly", "customers:all"),
    ("individual_customers", "customers:readonly", "customers:all"),
    ("customers", "customers:readonly", "customers:all"),
    ("suppliers", "suppliers:readonly", "suppliers:all"),
    ("products", "products:readonly", "products:all"),
    ("quotes", "quotes:readonly", "quotes:all"),
    ("journals", "journals:readonly", "journals:all"),
    ("ledger_accounts", "ledger_accounts:readonly", "ledger_accounts:all"),
    ("ledger_entry_lines", "ledger_entries:readonly", "ledger_entries:all"),
    ("ledger_entries", "ledger_entries:readonly", "ledger_entries:all"),
    ("trial_balance", "trial_balance:readonly", "trial_balance:readonly"),
    ("fiscal_years", "fiscal_years:readonly", "fiscal_years:readonly"),
    ("bank_accounts", "bank_accounts:readonly", "bank_accounts:all"),
    ("transactions", "transactions:readonly", "transactions:all"),
    ("category_groups", "categories:readonly", "categories:all"),
    ("categories", "categories:readonly", "categories:all"),
    ("file_attachments", "file_attachments:all", "file_attachments:all"),
)


def scope_hint(path: str, method: str) -> str:
    """The scope a request on *path* needs, or ``""`` when none is known."""
    clean = path.strip("/")
    for prefix, read, write in _SCOPES:
        if clean.startswith(prefix):
            return read if method.lower() == "get" else write
    return ""


def split_credential(token: str) -> tuple[str, str]:
    """``(company_id, api_token)`` from the stored credential."""
    raw = (token or "").strip()
    if "||" in raw:
        company, _, key = raw.partition("||")
        return company.strip(), key.strip()
    return "", raw


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------

_RELATIVE = re.compile(r"^(\d+)\s*([dwmy])$", re.IGNORECASE)


def to_date(value: Any, name: str) -> tuple[str | None, str | None]:
    """An ISO date from ``2026-01-31``, a date-time, or a window back from today (``30d``, ``6m``)."""
    raw = str(value if value is not None else "").strip()
    if not raw:
        return None, None
    match = _RELATIVE.match(raw)
    if match:
        count, unit = int(match.group(1)), match.group(2).lower()
        days = {"d": 1, "w": 7, "m": 30, "y": 365}[unit] * count
        return (datetime.now(UTC).date() - timedelta(days=days)).isoformat(), None
    try:
        return date.fromisoformat(raw[:10]).isoformat(), None
    except ValueError:
        return None, f"Error: {name} must be a date (YYYY-MM-DD) or a window such as 30d or 6m, not {raw!r}."


def id_list(value: Any) -> list[int]:
    """Integer ids from a list, a comma-separated string or one id."""
    if value is None or value == "":
        return []
    items = value if isinstance(value, list | tuple) else str(value).split(",")
    out: list[int] = []
    for item in items:
        text = str(item).strip()
        if text.isdigit():
            out.append(int(text))
    return out


def filters(*conditions: tuple[str, str, Any]) -> str | None:
    """The ``filter`` query parameter for every condition whose value is set."""
    kept = []
    for field_name, operator, value in conditions:
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)
        kept.append({"field": field_name, "operator": operator, "value": str(value)})
    return json.dumps(kept) if kept else None


def clamp(value: Any, default: int = DEFAULT_PAGE, maximum: int = MAX_PAGE) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(number, maximum))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def as_dict(value: Any) -> dict[str, Any]:
    """*value* when it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


def text(value: Any, limit: int = MAX_TEXT) -> str:
    out = " ".join(str(value or "").split())
    return out if len(out) <= limit else out[: limit - 1] + "…"


def money(amount: Any, currency: Any = "EUR") -> str:
    """``1,234.50 EUR`` from Pennylane's decimal string."""
    if amount is None or amount == "":
        return "-"
    try:
        number = float(amount)
    except (TypeError, ValueError):
        return f"{amount} {currency or ''}".strip()
    return f"{number:,.2f} {currency or 'EUR'}"


def ref_label(ref: Any) -> str:
    """A nested reference ``{id, name|label|number, url}`` as ``name (#id)``."""
    if not isinstance(ref, dict):
        return "-" if ref in (None, "") else str(ref)
    name = ref.get("name") or ref.get("label") or ref.get("number") or ""
    ident = ref.get("id")
    if name and ident is not None:
        return f"{text(name, 80)} (#{ident})"
    return text(name, 80) if name else (f"#{ident}" if ident is not None else "-")


def categories_label(categories: Any) -> str:
    if not isinstance(categories, list) or not categories:
        return ""
    parts = []
    for cat in categories:
        if not isinstance(cat, dict):
            continue
        label = cat.get("label") or f"#{cat.get('id')}"
        weight = cat.get("weight")
        raw_group = cat.get("category_group")
        group: dict[str, Any] = raw_group if isinstance(raw_group, dict) else {}
        prefix = f"{group.get('label')}: " if group.get("label") else ""
        try:
            share = f" {float(str(weight)) * 100:.0f}%" if weight not in (None, "", "1", "1.0") else ""
        except (TypeError, ValueError):
            share = ""
        parts.append(f"{prefix}{label}{share}")
    return ", ".join(parts)


def line(parts: Iterable[tuple[str, Any]]) -> str:
    """``key: value | key: value`` for the values that are set."""
    return " | ".join(f"{k}: {v}" for k, v in parts if v not in (None, "", "-", []))


@dataclass
class Page:
    items: list[dict[str, Any]]
    next_cursor: str | None = None


def page_footer(page: Page) -> str:
    return format_pagination_hint(page.next_cursor, param_name="cursor")


# ---------------------------------------------------------------------------
# Context: HTTP and paging
# ---------------------------------------------------------------------------


def _error_detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return text(resp.text, 200)
    if isinstance(body, dict):
        message = body.get("error") or body.get("message") or body.get("errors") or ""
        details = body.get("details")
        if isinstance(message, list | dict):
            message = json.dumps(message)[:300]
        if details:
            message = f"{message} ({json.dumps(details)[:300]})" if message else json.dumps(details)[:300]
        return text(message, 400)
    return text(str(body), 200)


@dataclass
class PennylaneContext:
    api_token: str
    company_id: str = ""
    persist_file_fn: Any = None
    _caches: dict[str, dict[Any, Any]] = field(default_factory=dict)

    @classmethod
    def from_token(cls, token: str, **kwargs: Any) -> PennylaneContext:
        company, key = split_credential(token)
        return cls(api_token=key, company_id=company, **kwargs)

    def client(self) -> Any:
        return egress_client(timeout=TIMEOUT)

    def headers(self, *, json_body: bool = True) -> dict[str, str]:
        out = dict(_HEADERS) if json_body else {"Accept": "application/json"}
        out["Authorization"] = f"Bearer {self.api_token}"
        if self.company_id:
            out["X-Company-Id"] = self.company_id
        return out

    def _limiter(self) -> Any:
        return get_shared_rate_limiter(
            f"pennylane:{app_token_cache_key('pennylane', self.api_token)}",
            rate=_RATE_PER_SECOND,
            capacity=_BURST,
            max_concurrency=_MAX_IN_FLIGHT,
        )

    async def request(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | list[dict[str, Any]] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        action: str = "",
    ) -> tuple[Any, str | None]:
        """``(answer, None)`` or ``(None, "Error: ...")``; 204 answers ``{}``."""
        if not self.api_token:
            return None, "Error: no Pennylane API token is configured on this integration."
        action = action or f"{method.upper()} {path}"
        kwargs: dict[str, Any] = {}
        if params:
            kwargs["params"] = {k: v for k, v in params.items() if v not in (None, "")}
        if files:
            kwargs["files"] = files
            kwargs["data"] = data or {}
            kwargs["headers"] = self.headers(json_body=False)
        else:
            kwargs["headers"] = self.headers()
            if body is not None:
                kwargs["json"] = body
        retry = TRANSIENT_RETRY_STATUSES if method.lower() == "get" else None
        async with self._limiter().slot():
            resp, err = await safe_request(
                client,
                method,
                f"{BASE_URL}/{path.lstrip('/')}",
                PROVIDER,
                action,
                expected_statuses={400, 401, 403, 404, 422},
                retry_statuses=retry,
                **kwargs,
            )
        if resp is None:
            return None, err or f"Error: Pennylane did not answer {action}."
        if resp.status_code == 401:
            return None, (
                "Error: Pennylane rejected the API token (401: missing, revoked or expired). "
                "Generate a new token in Pennylane > Settings > Connectivity > Developers and "
                "update the integration."
            )
        if resp.status_code == 403:
            scope = scope_hint(path, method)
            need = f" The token needs the '{scope}' scope." if scope else ""
            return None, f"Error: Pennylane refused {action} (403: scope missing).{need} {_error_detail(resp)}".rstrip()
        if resp.status_code == 404:
            return None, f"Error: Pennylane found nothing for {action} (404). {_error_detail(resp)}".rstrip()
        if resp.status_code in (400, 422):
            return None, f"Error: Pennylane refused {action} ({resp.status_code}): {_error_detail(resp)}"
        if err:
            return None, err
        if resp.status_code == 204 or not resp.content:
            return {}, None
        try:
            return resp.json(), None
        except ValueError:
            return None, f"Error: Pennylane answered {action} with something that is not JSON."

    async def get(
        self, client: httpx.AsyncClient, path: str, params: dict[str, Any] | None = None, action: str = ""
    ) -> tuple[Any, str | None]:
        return await self.request(client, "get", path, params=params, action=action)

    async def page(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        limit: int = DEFAULT_PAGE,
        cursor: str = "",
        action: str = "",
    ) -> tuple[Page, str | None]:
        """One page, and the error that emptied it (the page is then empty)."""
        query = dict(params or {})
        query["limit"] = limit
        if cursor:
            query["cursor"] = cursor
        data, err = await self.get(client, path, query, action)
        if err:
            return Page(items=[]), err
        if isinstance(data, list):
            return Page(items=[i for i in data if isinstance(i, dict)]), None
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return Page(items=[]), f"Error: Pennylane answered {action or path} without an items list."
        nxt = data.get("next_cursor") if data.get("has_more") else None
        return Page(items=[i for i in items if isinstance(i, dict)], next_cursor=nxt or None), None

    async def all_items(
        self, client: httpx.AsyncClient, path: str, *, params: dict[str, Any] | None = None, cap: int = 1000
    ) -> tuple[list[dict[str, Any]] | None, str | None, bool]:
        """Every item of a list up to *cap*: ``(items, error, capped)``."""
        out: list[dict[str, Any]] = []
        cursor = ""
        while True:
            page, err = await self.page(client, path, params=params, limit=MAX_PAGE, cursor=cursor)
            if err or page is None:
                return None, err, False
            out.extend(page.items)
            if not page.next_cursor:
                return out, None, False
            if len(out) >= cap:
                return out[:cap], None, True
            cursor = page.next_cursor

    async def names(self, client: httpx.AsyncClient, kind: str) -> dict[int, str]:
        """``id -> label`` of journals, ledger accounts or bank accounts, read once per run."""
        cache = self._caches.get(kind)
        if cache is not None:
            return cache
        items, _, _ = await self.all_items(client, kind, cap=2000)
        cache = {}
        for item in items or []:
            ident = item.get("id")
            if ident is None:
                continue
            if kind == "ledger_accounts":
                cache[int(ident)] = f"{item.get('number', '')} {text(item.get('label'), 60)}".strip()
            elif kind == "journals":
                cache[int(ident)] = f"{item.get('code', '')} {text(item.get('label'), 60)}".strip()
            else:
                cache[int(ident)] = text(item.get("name") or item.get("label"), 60)
        self._caches[kind] = cache
        return cache


def definition(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: Iterable[str] = (),
    *,
    priority: int = 1,
    read_only: bool = False,
    always_ask: bool = False,
) -> ToolDefinition:
    """One Pennylane ``ToolDefinition``: 0 core reads, 1 others, 2 long tail and sensitive writes.

    *always_ask*: each call waits for a person's approval (money, bank data,
    mandates, a document sent outside, a deletion).
    """
    return ToolDefinition(
        name=name,
        description=description,
        input_schema={"type": "object", "properties": properties, "required": list(required)},
        source=SOURCE,
        priority=priority,
        read_only=read_only,
        always_ask=always_ask,
    )


def id_schema(what: str) -> dict[str, Any]:
    return {"type": "integer", "description": f"Pennylane {what} id"}


def ids_schema(what: str) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "integer"}, "description": f"Only these {what} ids"}


DATE_FROM = {"type": "string", "description": "From this date (YYYY-MM-DD or a window back from today: 30d, 6m)"}
DATE_TO = {"type": "string", "description": "Up to this date included (YYYY-MM-DD)"}
LIMIT = {"type": "integer", "description": f"Records per page (1-{MAX_PAGE})", "default": DEFAULT_PAGE}
CURSOR = {"type": "string", "description": "Cursor of the next page, from the previous answer"}
