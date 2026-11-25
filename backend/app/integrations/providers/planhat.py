# SPDX-License-Identifier: Apache-2.0

"""Planhat integration — Customer Success platform.

API docs: https://www.planhat.com/developers/api/introduction
Auth: Bearer token, either an OAuth Client access token (App Center > OAuth
  Client; each person's own account, scoped to their Planhat role) or a
  Private App API Access Token (user-agnostic). Both reach the same endpoints.
Base URL: https://api.planhat.com

Planhat field reference (commonly confused):
  Company: _id, name, h (health 0-100), phase, status, mrr, arr, mr (MRR+NRR30),
           customerFrom, customerTo (renewal date), csmScore (1-5), owner (objectId),
           tags, custom, description, country, web
  Enduser:  _id, email, firstName, lastName, name (auto), companyId, position,
            phone, nps, tags, featured, primary, custom
  Conversation: _id, companyId, subject, description, snippet, type, date, starred,
                pinned, sentiment, category, custom
  Task:     _id, companyId, mainType ("task"), action (title), description,
            startTime, endTime, status, isArchived, ownerId, custom
  Opportunity: _id, companyId, title, salesStage, status (active/won/lost),
               mrr, arr, nrr, dealDate, closeDate, ownerId, custom
  License:  _id, companyId, product, mrr, arr, value, _currency, fromDate, toDate,
            renewalStatus, renewalDate, renewalDaysFromNow, fixedPeriod, custom
  Asset:    _id, companyId, name, externalId, value, status, custom
  Ticket:   _id, companyId, subject, snippet, status, source, severity, email, custom
            (stored as Conversations of type "ticket" — the /tickets endpoint
             only lists/deletes, so a single ticket is fetched via /conversations/{id})
"""

from __future__ import annotations

import json
from typing import Any

from app.core.egress import egress_client
from app.integrations.http import format_entity_list, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Planhat"
_BASE_URL = "https://api.planhat.com"

# Placeholder values LLM agents pass when they never resolved a real _id.
# Sending them to Planhat wastes a round-trip on a guaranteed 400/404
# (a 600s assignment budget burned on failing lookups).
_INVALID_ID_PLACEHOLDERS = frozenset(
    {"n/a", "na", "none", "null", "unknown", "undefined", "-"}
)


def _validate_planhat_id(
    raw_id: Any, entity: str, lookup_hint: str, tool: str
) -> tuple[str, str | None]:
    """Normalize an agent-supplied Planhat ``_id`` and fail fast on placeholders.

    Returns ``(normalized_id, None)`` when the ID is usable, or
    ``("", error_message)`` when it is missing/whitespace/a placeholder/not a
    string, so the tool can return an actionable error without spending an
    HTTP call. Tool-call arguments come straight from provider JSON without
    type coercion, so non-string values (numbers, null, objects) must be
    treated as invalid rather than crashing on ``.strip()``.
    """
    normalized = raw_id.strip() if isinstance(raw_id, str) else ""
    if not normalized or normalized.lower() in _INVALID_ID_PLACEHOLDERS:
        return "", (
            f"Error: Planhat {entity} ID is missing or invalid. "
            f"Use {lookup_hint} to find a valid {entity} _id "
            f"before calling {tool}."
        )
    return normalized, None


# ── Formatting helpers ────────────────────────────────────────────────────


def _fmt_company(c: dict) -> str:
    health = c.get("h", "?")
    mrr = c.get("mrr", 0) or 0
    phase = c.get("phase", "?")
    status = c.get("status", "?")
    csm = c.get("csmScore", "?")
    return (
        f"{c.get('name', 'Unnamed')} | Health: {health} | CSM Score: {csm} "
        f"| MRR: ${mrr:,.0f} | Phase: {phase} | Status: {status} "
        f"| ID: {c.get('_id', '')}"
    )


def _fmt_company_detail(c: dict) -> str:
    mrr = c.get("mrr", 0) or 0
    arr = c.get("arr", 0) or 0
    return (
        f"Name: {c.get('name', '')}\n"
        f"Status: {c.get('status', '?')}\n"
        f"Phase: {c.get('phase', '?')}\n"
        f"Health: {c.get('h', '?')}\n"
        f"CSM Score: {c.get('csmScore', '?')}\n"
        f"MRR: ${mrr:,.0f}\n"
        f"ARR: ${arr:,.0f}\n"
        f"Customer From: {c.get('customerFrom', 'N/A')}\n"
        f"Renewal Date: {c.get('customerTo', 'N/A')}\n"
        f"Renewal Days: {c.get('renewalDaysFromNow', 'N/A')}\n"
        f"Owner ID: {c.get('owner', 'Unassigned')}\n"
        f"Country: {c.get('country', '')}\n"
        f"Website: {c.get('web', '')}\n"
        f"Tags: {', '.join(c.get('tags', []))}\n"
        f"Description: {c.get('description', '')}\n"
        f"Custom: {c.get('custom', {})}\n"
        f"ID: {c.get('_id', '')}"
    )


def _fmt_enduser(u: dict) -> str:
    return (
        f"{u.get('name', '')} | Email: {u.get('email', '')} "
        f"| Position: {u.get('position', '?')} | NPS: {u.get('nps', '?')} "
        f"| Company: {u.get('companyName', '?')} | ID: {u.get('_id', '')}"
    )


def _fmt_enduser_detail(u: dict) -> str:
    return (
        f"Name: {u.get('name', '')}\n"
        f"Email: {u.get('email', '')}\n"
        f"First Name: {u.get('firstName', '')}\n"
        f"Last Name: {u.get('lastName', '')}\n"
        f"Position: {u.get('position', '')}\n"
        f"Phone: {u.get('phone', '')}\n"
        f"Company: {u.get('companyName', '')} ({u.get('companyId', '')})\n"
        f"NPS: {u.get('nps', '?')}\n"
        f"NPS Comment: {u.get('npsComment', '')}\n"
        f"Featured: {u.get('featured', False)}\n"
        f"Tags: {', '.join(u.get('tags', []))}\n"
        f"Last Touch: {u.get('lastTouch', 'N/A')}\n"
        f"Custom: {u.get('custom', {})}\n"
        f"ID: {u.get('_id', '')}"
    )


def _fmt_when(raw: Any) -> str:
    """Render a Planhat ISO timestamp as ``YYYY-MM-DD HH:MM UTC``.

    Planhat stores dates as UTC ISO strings (``2026-11-04T06:44:57.000Z``);
    a value without a time part keeps its day-level form.
    """
    if not isinstance(raw, str) or not raw:
        return ""
    if len(raw) >= 16 and raw[10] == "T":
        return f"{raw[:10]} {raw[11:16]} UTC"
    return raw[:10]


# Ticket statuses that count as closed for the ``open`` / ``closed`` filter of
# list_planhat_tickets. Planhat ticket statuses are free text synced from the
# help desk (Queued, In Progress, Waiting for customer, Closed...).
_CLOSED_TICKET_STATUSES = frozenset({"closed", "solved", "resolved"})


def _ticket_severity(t: dict) -> Any:
    """Severity of a ticket: ``custom.Severity`` first, the raw field second.

    Help-desk syncs write the severity as a custom field; the top-level
    ``severity`` is usually empty, which made every list line read ``?``.
    """
    custom = t.get("custom")
    if isinstance(custom, dict):
        for key, value in custom.items():
            if key.lower() == "severity" and value not in (None, ""):
                return value
    raw = t.get("severity")
    return raw if raw not in (None, "") else "?"


def _ticket_matches_status(t: dict, wanted: str) -> bool:
    """``open`` = not closed, ``closed`` = closed, else a case-insensitive match."""
    status = str(t.get("status") or "").strip().lower()
    if wanted == "open":
        return status not in _CLOSED_TICKET_STATUSES
    if wanted == "closed":
        return status in _CLOSED_TICKET_STATUSES
    return status == wanted


def _fmt_conversation(c: dict) -> str:
    date = _fmt_when(c.get("date"))
    subj = c.get("subject", "")
    snippet = (c.get("snippet") or c.get("description") or "")[:200]
    sentiment = c.get("sentiment", "")
    sent_tag = f" [{sentiment}]" if sentiment else ""
    return f"[{c.get('type', '?')}] {date} — {subj}{': ' + snippet if snippet else ''}{sent_tag} | ID: {c.get('_id', '')}"


def _fmt_task(t: dict) -> str:
    done = "done" if t.get("isArchived") else "open"
    action = t.get("action", "") or t.get("description", "")
    start = (t.get("startTime") or "N/A")[:10]
    return (
        f"[{done}] {action} | Status: {t.get('status', '?')} "
        f"| Due: {start} | Company: {t.get('companyName', '?')} "
        f"| ID: {t.get('_id', '')}"
    )


def _fmt_opportunity(o: dict) -> str:
    mrr = o.get("mrr", 0) or 0
    arr = o.get("arr", 0) or 0
    return (
        f"{o.get('title', 'Untitled')} | Stage: {o.get('salesStage', '?')} "
        f"| Status: {o.get('status', '?')} | MRR: ${mrr:,.0f} | ARR: ${arr:,.0f} "
        f"| Company: {o.get('companyName', '?')} | ID: {o.get('_id', '')}"
    )


def _fmt_license(lic: dict) -> str:
    mrr = lic.get("mrr", 0) or 0
    return (
        f"{lic.get('product', 'N/A')} | MRR: ${mrr:,.0f} "
        f"| From: {(lic.get('fromDate') or 'N/A')[:10]} "
        f"| To: {(lic.get('toDate') or 'open')[:10]} "
        f"| Renewal: {lic.get('renewalStatus', '?')} "
        f"| Days to Renewal: {lic.get('renewalDaysFromNow', '?')} "
        f"| Company: {lic.get('companyName', '?')} | ID: {lic.get('_id', '')}"
    )


def _fmt_asset(a: dict) -> str:
    val = a.get("value", 0) or 0
    return (
        f"{a.get('name', 'Unnamed')} | Status: {a.get('status', '?')} "
        f"| Value: ${val:,.0f} "
        f"| Company: {a.get('companyName', '?')} | ID: {a.get('_id', '')}"
    )


def _fmt_ticket(t: dict) -> str:
    return (
        f"[{t.get('status', '?')}] {t.get('subject', t.get('title', ''))} "
        f"| Source: {t.get('source', '?')} | Severity: {_ticket_severity(t)} "
        f"| Company: {t.get('companyName', '?')} | ID: {t.get('_id', '')}"
    )


def _fmt_ticket_detail(t: dict) -> str:
    body = t.get("snippet") or t.get("description") or ""
    created = t.get("createDate") or t.get("date") or "N/A"
    return (
        f"Subject: {t.get('subject', t.get('title', ''))}\n"
        f"Status: {t.get('status', '?')}\n"
        f"Open: {t.get('isOpen', '?')}\n"
        f"Source: {t.get('source', '?')}\n"
        f"Severity: {_ticket_severity(t)}\n"
        f"Type: {t.get('type', '?')}\n"
        f"Email: {t.get('email', '')}\n"
        f"Company: {t.get('companyName', '')} ({t.get('companyId', '')})\n"
        f"Created: {created}\n"
        f"Updated: {t.get('updatedAt') or 'N/A'}\n"
        f"Tags: {', '.join(t.get('tags', []))}\n"
        f"Content: {body}\n"
        f"Custom: {t.get('custom', {})}\n"
        f"ID: {t.get('_id', '')}"
    )


def _fmt_conversation_detail(c: dict) -> str:
    body = c.get("description") or c.get("snippet") or ""
    users = c.get("users") or []
    names = [
        str(u.get("name") or u.get("email") or u.get("id") or "")
        if isinstance(u, dict)
        else str(u)
        for u in users
    ]
    return (
        f"Subject: {c.get('subject', '')}\n"
        f"Type: {c.get('type', '?')}\n"
        f"Date: {_fmt_when(c.get('date')) or 'N/A'}\n"
        f"Company: {c.get('companyName', '')} ({c.get('companyId', '')})\n"
        f"Users: {', '.join(n for n in names if n)}\n"
        f"Sentiment: {c.get('sentiment', '')}\n"
        f"Content: {body}\n"
        f"ID: {c.get('_id', '')}"
    )


# ── Tool builder ──────────────────────────────────────────────────────────


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # ── Company tools ─────────────────────────────────────────────────────

    async def list_planhat_companies(count: int = 20, sort: str = "-mrr") -> str:
        """List companies in Planhat with health, MRR, phase, and status."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/companies",
                _PROVIDER,
                "list companies",
                headers=headers,
                params={"limit": str(count), "sort": sort},
            )
            if err:
                return err
            return format_entity_list(resp.json(), _fmt_company, noun="company")

    async def get_planhat_company(company_id: str) -> str:
        """Get detailed information about a specific company by ID."""
        normalized_company_id, id_error = _validate_planhat_id(
            company_id,
            "company",
            "list_planhat_companies or search_planhat_companies",
            "get_planhat_company",
        )
        if id_error:
            return id_error

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/companies/{normalized_company_id}",
                _PROVIDER,
                "get company",
                headers=headers,
            )
            if err:
                return err
            return _fmt_company_detail(resp.json())

    async def search_planhat_companies(
        query: str, count: int = 15, **_kwargs: Any
    ) -> str:
        """Search companies by name. Uses the lean list endpoint for efficient lookup."""
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/leancompanies",
                _PROVIDER,
                "search companies",
                headers=headers,
                params={"limit": "500", "select": "_id,name,h,mrr,phase,status"},
            )
            if err:
                return err
            companies = resp.json()
            q = query.lower()
            matches = [c for c in companies if q in (c.get("name") or "").lower()]
            return format_entity_list(
                matches[:count],
                _fmt_company,
                noun="company",
                total=len(matches),
                empty_msg=f"No companies matching '{query}'.",
            )

    async def create_planhat_company(
        name: str,
        status: str = "prospect",
        phase: str = "",
        description: str = "",
        external_id: str = "",
    ) -> str:
        """Create a new company in Planhat."""
        payload: dict[str, Any] = {"name": name, "status": status}
        if phase:
            payload["phase"] = phase
        if description:
            payload["description"] = description
        if external_id:
            payload["externalId"] = external_id
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/companies",
                _PROVIDER,
                "create company",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            data = resp.json()
            return f"Company '{name}' created. ID: {data.get('_id', '')}"

    async def update_planhat_company(company_id: str, data_json: str) -> str:
        """Update a company's fields (name, phase, status, tags, custom, etc.) via JSON."""
        try:
            data = json.loads(data_json)
        except ValueError:
            return "Error: data_json must be valid JSON."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "put",
                f"{_BASE_URL}/companies/{company_id}",
                _PROVIDER,
                "update company",
                headers=headers,
                json=data,
            )
            if err:
                return err
            return f"Company {company_id} updated."

    # ── Enduser tools ─────────────────────────────────────────────────────

    async def list_planhat_endusers(company_id: str = "", count: int = 20) -> str:
        """List end users (contacts). Optionally filter by company ID."""
        params: dict[str, str] = {"limit": str(count)}
        if company_id:
            params["companyId"] = company_id
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/endusers",
                _PROVIDER,
                "list endusers",
                headers=headers,
                params=params,
            )
            if err:
                return err
            return format_entity_list(resp.json(), _fmt_enduser, noun="enduser")

    async def get_planhat_enduser(enduser_id: str) -> str:
        """Get detailed information about a specific enduser by ID."""
        normalized_enduser_id, id_error = _validate_planhat_id(
            enduser_id,
            "enduser",
            "list_planhat_endusers",
            "get_planhat_enduser",
        )
        if id_error:
            return id_error

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/endusers/{normalized_enduser_id}",
                _PROVIDER,
                "get enduser",
                headers=headers,
            )
            if err:
                return err
            return _fmt_enduser_detail(resp.json())

    async def create_planhat_enduser(
        email: str,
        company_id: str,
        first_name: str = "",
        last_name: str = "",
        position: str = "",
        phone: str = "",
    ) -> str:
        """Create an enduser (contact) in Planhat. Requires email and companyId."""
        payload: dict[str, Any] = {"email": email, "companyId": company_id}
        if first_name:
            payload["firstName"] = first_name
        if last_name:
            payload["lastName"] = last_name
        if position:
            payload["position"] = position
        if phone:
            payload["phone"] = phone
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/endusers",
                _PROVIDER,
                "create enduser",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            data = resp.json()
            return f"Enduser '{data.get('name', email)}' created. ID: {data.get('_id', '')}"

    async def update_planhat_enduser(enduser_id: str, data_json: str) -> str:
        """Update an enduser's fields (name, position, tags, custom, etc.) via JSON."""
        try:
            data = json.loads(data_json)
        except ValueError:
            return "Error: data_json must be valid JSON."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "put",
                f"{_BASE_URL}/endusers/{enduser_id}",
                _PROVIDER,
                "update enduser",
                headers=headers,
                json=data,
            )
            if err:
                return err
            return f"Enduser {enduser_id} updated."

    # ── Conversation/Note tools ───────────────────────────────────────────

    async def list_planhat_conversations(company_id: str = "", count: int = 15) -> str:
        """List conversations (notes, emails, calls, tickets) optionally filtered by company."""
        params: dict[str, str] = {"limit": str(count), "sort": "-date"}
        if company_id:
            params["companyId"] = company_id
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/conversations",
                _PROVIDER,
                "list conversations",
                headers=headers,
                params=params,
            )
            if err:
                return err
            return format_entity_list(
                resp.json(), _fmt_conversation, noun="conversation"
            )

    async def create_planhat_note(
        company_id: str, description: str, subject: str = "", note_type: str = "note"
    ) -> str:
        """Create a conversation/note for a company. Type can be 'note' or a custom touch type."""
        payload: dict[str, Any] = {
            "companyId": company_id,
            "description": description,
            "type": note_type,
        }
        if subject:
            payload["subject"] = subject
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/conversations",
                _PROVIDER,
                "create note",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            data = resp.json()
            return f"Note created for company {company_id}. ID: {data.get('_id', '')}"

    # ── Task tools ────────────────────────────────────────────────────────

    async def list_planhat_tasks(company_id: str = "", count: int = 20) -> str:
        """List tasks. Optionally filter by company ID.

        For a full cross-company survey in a single call use
        ``list_all_planhat_tasks`` instead — iterating per company exhausts
        the tool-call budget on large workspaces.
        """
        params: dict[str, str] = {"limit": str(count), "sort": "-startTime"}
        if company_id:
            params["companyId"] = company_id
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/tasks",
                _PROVIDER,
                "list tasks",
                headers=headers,
                params=params,
            )
            if err:
                return err
            return format_entity_list(resp.json(), _fmt_task, noun="task")

    async def list_all_planhat_tasks(
        include_done: bool = False, count: int = 500
    ) -> str:
        """Fetch ALL Planhat tasks across all companies in a single call.

        Use this instead of calling ``list_planhat_tasks`` per company — that
        requires O(N companies) tool calls and exhausts the iteration budget
        in bulk-fetch assignments.  A single call returns tasks from
        the entire Planhat workspace, grouped by company name.

        ``include_done``  – when False (default) archived tasks are filtered
                            out client-side (the /tasks endpoint has no
                            archived-status filter); pass True to include them.
        ``count``         – maximum tasks fetched from the API (default 500,
                            max 2000).  The limit applies BEFORE the archived
                            filter, so fewer open tasks may be returned.
        """
        count = max(1, min(count, 2000))
        params: dict[str, str] = {"limit": str(count), "sort": "-startTime"}
        async with egress_client(timeout=60) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/tasks",
                _PROVIDER,
                "list all tasks",
                headers=headers,
                params=params,
            )
            if err:
                return err
            # Parse inside the client context, matching every other tool in
            # this module — relying on httpx's eager body buffering to read
            # after close would break if safe_request ever streamed responses.
            tasks = resp.json()
        if not isinstance(tasks, list):
            tasks = []
        fetched = len(tasks)
        # The API limit is applied before the client-side archived filter, so
        # hitting it means older tasks (including open ones) were cut off.
        truncation = (
            f"\n\n[Note: only the {fetched} most recent tasks were fetched "
            "(count limit reached); older tasks may exist — raise count "
            "(max 2000) for a fuller survey.]"
            if fetched >= count
            else ""
        )
        if not include_done:
            tasks = [t for t in tasks if not t.get("isArchived")]
        if not tasks:
            empty = "No open tasks found." if not include_done else "No tasks found."
            return empty + truncation

        # Group by company name for readability.
        by_company: dict[str, list[dict]] = {}
        for t in tasks:
            company = t.get("companyName") or t.get("companyId") or "Unknown"
            by_company.setdefault(company, []).append(t)

        sections: list[str] = []
        for company, ctasks in sorted(by_company.items()):
            header = f"{company} ({len(ctasks)} task{'s' if len(ctasks) != 1 else ''}):"
            # Re-inject the resolved company label so _fmt_task never renders
            # "Company: ?" for tasks that only carry a companyId.
            body = "\n".join(
                f"  {_fmt_task({**t, 'companyName': company})}" for t in ctasks
            )
            sections.append(f"{header}\n{body}")

        label = "open" if not include_done else "total"
        summary = (
            f"Found {len(tasks)} {label} task{'s' if len(tasks) != 1 else ''} "
            f"across {len(by_company)} "
            f"{'company' if len(by_company) == 1 else 'companies'}:"
        )
        return summary + "\n\n" + "\n\n".join(sections) + truncation

    async def create_planhat_task(
        company_id: str,
        action: str,
        description: str = "",
        start_time: str = "",
        status: str = "In-progress",
    ) -> str:
        """Create a task for a company. 'action' is the task title (e.g. 'Send Invoice')."""
        payload: dict[str, Any] = {
            "companyId": company_id,
            "mainType": "task",
            "action": action,
            "status": status,
        }
        if description:
            payload["description"] = description
        if start_time:
            payload["startTime"] = start_time
        else:
            payload["noSpecificTime"] = True
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/tasks",
                _PROVIDER,
                "create task",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            data = resp.json()
            return f"Task '{action}' created for company {company_id}. ID: {data.get('_id', '')}"

    async def update_planhat_task(task_id: str, data_json: str) -> str:
        """Update a task's fields (action, status, isArchived, description, etc.) via JSON.

        To mark a task as done, set {"isArchived": true}."""
        try:
            data = json.loads(data_json)
        except ValueError:
            return "Error: data_json must be valid JSON."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "put",
                f"{_BASE_URL}/tasks/{task_id}",
                _PROVIDER,
                "update task",
                headers=headers,
                json=data,
            )
            if err:
                return err
            return f"Task {task_id} updated."

    # ── Opportunity tools ─────────────────────────────────────────────────

    async def list_planhat_opportunities(company_id: str = "", count: int = 15) -> str:
        """List sales opportunities with title, stage, status, and revenue."""
        params: dict[str, str] = {"limit": str(count)}
        if company_id:
            params["companyId"] = company_id
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/opportunities",
                _PROVIDER,
                "list opportunities",
                headers=headers,
                params=params,
            )
            if err:
                return err
            return format_entity_list(resp.json(), _fmt_opportunity, noun="opportunity")

    async def create_planhat_opportunity(
        company_id: str,
        title: str,
        sales_stage: str = "",
        mrr: float = 0,
        nrr: float = 0,
        status: str = "active",
    ) -> str:
        """Create a sales opportunity for a company."""
        payload: dict[str, Any] = {
            "companyId": company_id,
            "title": title,
            "status": status,
        }
        if sales_stage:
            payload["salesStage"] = sales_stage
        if mrr:
            payload["mrr"] = mrr
        if nrr:
            payload["nrr"] = nrr
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/opportunities",
                _PROVIDER,
                "create opportunity",
                headers=headers,
                json=payload,
            )
            if err:
                return err
            data = resp.json()
            return f"Opportunity '{title}' created. ID: {data.get('_id', '')}"

    async def update_planhat_opportunity(opportunity_id: str, data_json: str) -> str:
        """Update an opportunity's fields (salesStage, status, mrr, title, etc.) via JSON.

        Status can be: 'active', 'won', or 'lost'."""
        try:
            data = json.loads(data_json)
        except ValueError:
            return "Error: data_json must be valid JSON."
        async with egress_client(timeout=30) as client:
            _, err = await safe_request(
                client,
                "put",
                f"{_BASE_URL}/opportunities/{opportunity_id}",
                _PROVIDER,
                "update opportunity",
                headers=headers,
                json=data,
            )
            if err:
                return err
            return f"Opportunity {opportunity_id} updated."

    # ── License tools ─────────────────────────────────────────────────────

    async def list_planhat_licenses(company_id: str = "", count: int = 20) -> str:
        """List licenses (subscriptions) with MRR, renewal status, and dates."""
        params: dict[str, str] = {"limit": str(count)}
        if company_id:
            params["companyId"] = company_id
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/licenses",
                _PROVIDER,
                "list licenses",
                headers=headers,
                params=params,
            )
            if err:
                return err
            return format_entity_list(resp.json(), _fmt_license, noun="license")

    # ── Asset tools ───────────────────────────────────────────────────────

    async def list_planhat_assets(company_id: str = "", count: int = 20) -> str:
        """List assets (product instances, subscriptions). Optionally filter by company."""
        params: dict[str, str] = {"limit": str(count)}
        if company_id:
            params["companyId"] = company_id
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/assets",
                _PROVIDER,
                "list assets",
                headers=headers,
                params=params,
            )
            if err:
                return err
            return format_entity_list(resp.json(), _fmt_asset, noun="asset")

    # ── Ticket tools ──────────────────────────────────────────────────────

    async def list_planhat_tickets(
        company_id: str = "", status: str = "", count: int = 20
    ) -> str:
        """List support tickets. Optionally filter by company and/or status.

        The /tickets endpoint ignores a ``status`` query parameter, so the
        filter runs here on a wider page and ``count`` applies after it.
        """
        wanted = status.strip().lower() if isinstance(status, str) else ""
        page = min(max(count * 10, 200), 2000) if wanted else count
        params: dict[str, str] = {"limit": str(page)}
        if company_id:
            params["companyId"] = company_id
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/tickets",
                _PROVIDER,
                "list tickets",
                headers=headers,
                params=params,
            )
            if err:
                return err
            tickets = resp.json()
            if wanted and isinstance(tickets, list):
                tickets = [
                    t
                    for t in tickets
                    if isinstance(t, dict) and _ticket_matches_status(t, wanted)
                ][:count]
            return format_entity_list(tickets, _fmt_ticket, noun="ticket")

    async def get_planhat_ticket(ticket_id: str) -> str:
        """Get a single support ticket by its ID.

        Planhat stores tickets as conversations of type "ticket", and the
        dedicated /tickets endpoint only supports listing and deleting — there
        is no /tickets/{id} read route. A single ticket is therefore retrieved
        via the conversations endpoint using the same _id that
        list_planhat_tickets returns.
        """
        normalized_ticket_id, id_error = _validate_planhat_id(
            ticket_id,
            "ticket",
            "list_planhat_tickets",
            "get_planhat_ticket",
        )
        if id_error:
            return id_error

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/conversations/{normalized_ticket_id}",
                _PROVIDER,
                "get ticket",
                headers=headers,
            )
            if err:
                return err
            return _fmt_ticket_detail(resp.json())

    async def get_planhat_conversation(conversation_id: str) -> str:
        """Get one conversation by its ID: full body and send time (UTC)."""
        normalized_id, id_error = _validate_planhat_id(
            conversation_id,
            "conversation",
            "list_planhat_conversations",
            "get_planhat_conversation",
        )
        if id_error:
            return id_error

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/conversations/{normalized_id}",
                _PROVIDER,
                "get conversation",
                headers=headers,
            )
            if err:
                return err
            return _fmt_conversation_detail(resp.json())

    # ── Register all tools ────────────────────────────────────────────────

    executor.register("list_planhat_companies", list_planhat_companies)
    executor.register("get_planhat_company", get_planhat_company)
    executor.register("search_planhat_companies", search_planhat_companies)
    executor.register("create_planhat_company", create_planhat_company)
    executor.register("update_planhat_company", update_planhat_company)
    executor.register("list_planhat_endusers", list_planhat_endusers)
    executor.register("get_planhat_enduser", get_planhat_enduser)
    executor.register("create_planhat_enduser", create_planhat_enduser)
    executor.register("update_planhat_enduser", update_planhat_enduser)
    executor.register("list_planhat_conversations", list_planhat_conversations)
    executor.register("create_planhat_note", create_planhat_note)
    executor.register("list_planhat_tasks", list_planhat_tasks)
    executor.register("list_all_planhat_tasks", list_all_planhat_tasks)
    executor.register("create_planhat_task", create_planhat_task)
    executor.register("update_planhat_task", update_planhat_task)
    executor.register("list_planhat_opportunities", list_planhat_opportunities)
    executor.register("create_planhat_opportunity", create_planhat_opportunity)
    executor.register("update_planhat_opportunity", update_planhat_opportunity)
    executor.register("list_planhat_licenses", list_planhat_licenses)
    executor.register("list_planhat_assets", list_planhat_assets)
    executor.register("list_planhat_tickets", list_planhat_tickets)
    executor.register("get_planhat_ticket", get_planhat_ticket)
    executor.register("get_planhat_conversation", get_planhat_conversation)

    return [
        # ── Company (5) ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_planhat_companies",
            description="List companies in Planhat with health score (0-100), CSM score, MRR, phase, and status.",
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Number of companies to return (max 2000)",
                    },
                    "sort": {
                        "type": "string",
                        "default": "-mrr",
                        "description": "Sort field. Prefix with - for descending. Common: -mrr, -h, name, -lastActive",
                    },
                },
                "required": [],
            },
            source="integration:planhat",
        ),
        ToolDefinition(
            name="get_planhat_company",
            description="Get detailed info about a specific Planhat company including health, MRR, ARR, renewal date, owner, tags, and custom fields.",
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "Planhat company _id",
                    }
                },
                "required": ["company_id"],
            },
            source="integration:planhat",
        ),
        ToolDefinition(
            name="search_planhat_companies",
            description="Search companies by name in Planhat.",
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search text (case-insensitive name match)",
                    },
                    "count": {
                        "type": "integer",
                        "default": 15,
                        "description": "Max results to return",
                    },
                },
                "required": ["query"],
            },
            source="integration:planhat",
        ),
        ToolDefinition(
            name="create_planhat_company",
            description="Create a new company in Planhat.",
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Company name (required)",
                    },
                    "status": {
                        "type": "string",
                        "enum": ["prospect", "coming", "customer", "canceled", "lost"],
                        "default": "prospect",
                    },
                    "phase": {
                        "type": "string",
                        "description": "Lifecycle phase (must match a phase in Planhat)",
                        "default": "",
                    },
                    "description": {
                        "type": "string",
                        "description": "Company description",
                        "default": "",
                    },
                    "external_id": {
                        "type": "string",
                        "description": "External system ID",
                        "default": "",
                    },
                },
                "required": ["name"],
            },
            source="integration:planhat",
            priority=1,
        ),
        ToolDefinition(
            name="update_planhat_company",
            description="Update a company's fields (name, phase, status, tags, custom fields, etc.) via a JSON object.",
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "Planhat company _id",
                    },
                    "data_json": {
                        "type": "string",
                        "description": 'JSON object with fields to update, e.g. {"phase": "Onboarding", "tags": ["enterprise"]}',
                    },
                },
                "required": ["company_id", "data_json"],
            },
            source="integration:planhat",
            priority=1,
        ),
        # ── Enduser (4) ─────────────────────────────────────────────────
        ToolDefinition(
            name="list_planhat_endusers",
            description="List end users (contacts) with name, email, position, and NPS. Optionally filter by company.",
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "Company ID to filter by (optional)",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Number of endusers to return (max 2000)",
                    },
                },
                "required": [],
            },
            source="integration:planhat",
        ),
        ToolDefinition(
            name="get_planhat_enduser",
            description="Get detailed info about a specific enduser (contact) including NPS, tags, custom fields.",
            input_schema={
                "type": "object",
                "properties": {
                    "enduser_id": {
                        "type": "string",
                        "description": "Planhat enduser _id",
                    }
                },
                "required": ["enduser_id"],
            },
            source="integration:planhat",
            priority=1,
        ),
        ToolDefinition(
            name="create_planhat_enduser",
            description="Create an enduser (contact) in Planhat. Requires email and company ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "Email address (required)",
                    },
                    "company_id": {
                        "type": "string",
                        "description": "Planhat company ID (required)",
                    },
                    "first_name": {
                        "type": "string",
                        "description": "First name",
                        "default": "",
                    },
                    "last_name": {
                        "type": "string",
                        "description": "Last name",
                        "default": "",
                    },
                    "position": {
                        "type": "string",
                        "description": "Role/position, e.g. 'CFO'",
                        "default": "",
                    },
                    "phone": {
                        "type": "string",
                        "description": "Phone number",
                        "default": "",
                    },
                },
                "required": ["email", "company_id"],
            },
            source="integration:planhat",
            priority=1,
        ),
        ToolDefinition(
            name="update_planhat_enduser",
            description="Update an enduser's fields (name, position, tags, phone, custom, etc.) via a JSON object.",
            input_schema={
                "type": "object",
                "properties": {
                    "enduser_id": {
                        "type": "string",
                        "description": "Planhat enduser _id",
                    },
                    "data_json": {
                        "type": "string",
                        "description": "JSON object with fields to update",
                    },
                },
                "required": ["enduser_id", "data_json"],
            },
            source="integration:planhat",
            priority=1,
        ),
        # ── Conversations / Notes (2) ────────────────────────────────────
        ToolDefinition(
            name="list_planhat_conversations",
            description=(
                "List conversations (notes, emails, calls, tickets) sorted by date, "
                "each with its send time (UTC) and a 200-character preview. "
                "Optionally filter by company. Read one in full with "
                "get_planhat_conversation."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "Company ID to filter by (optional)",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "default": 15,
                        "description": "Number of conversations to return (max 2000)",
                    },
                },
                "required": [],
            },
            source="integration:planhat",
            priority=1,
        ),
        ToolDefinition(
            name="create_planhat_note",
            description="Create a conversation/note for a Planhat company. Type defaults to 'note' but can be a custom touch type.",
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "Company ID (required)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Note content/body (required)",
                    },
                    "subject": {
                        "type": "string",
                        "description": "Note subject/title",
                        "default": "",
                    },
                    "note_type": {
                        "type": "string",
                        "description": "Conversation type: 'note' or a custom touch type name",
                        "default": "note",
                    },
                },
                "required": ["company_id", "description"],
            },
            source="integration:planhat",
        ),
        # ── Tasks (4) ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_planhat_tasks",
            description=(
                "List tasks with title, status, and due date. Optionally filter by company. "
                "For a full cross-company survey in a single call use "
                "``list_all_planhat_tasks`` instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "Company ID to filter by (optional)",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Number of tasks to return (max 10000)",
                    },
                },
                "required": [],
            },
            source="integration:planhat",
            priority=1,
        ),
        ToolDefinition(
            name="list_all_planhat_tasks",
            description=(
                "Fetch ALL Planhat tasks across all companies in a single call. "
                "Use this instead of calling list_planhat_tasks per company — "
                "iterating per company exhausts the tool-call budget on large workspaces. "
                "Results are grouped by company name. "
                "Pass include_done=true to include archived/completed tasks."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "include_done": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include archived/done tasks (default: false = open tasks only)",
                    },
                    "count": {
                        "type": "integer",
                        "default": 500,
                        "description": (
                            "Maximum tasks fetched from the API (default 500, "
                            "max 2000). Applied before the open-task filter, "
                            "so fewer open tasks may be returned."
                        ),
                    },
                },
                "required": [],
            },
            source="integration:planhat",
            priority=1,
        ),
        ToolDefinition(
            name="create_planhat_task",
            description="Create a task for a Planhat company. The 'action' field is the task title.",
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "Company ID (required)",
                    },
                    "action": {
                        "type": "string",
                        "description": "Task title, e.g. 'Send Invoice', 'Schedule QBR' (required)",
                    },
                    "description": {
                        "type": "string",
                        "description": "Task description/details",
                        "default": "",
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Due date/time in ISO format (optional)",
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "description": "Task status, e.g. 'In-progress', 'Done'",
                        "default": "In-progress",
                    },
                },
                "required": ["company_id", "action"],
            },
            source="integration:planhat",
            priority=1,
        ),
        ToolDefinition(
            name="update_planhat_task",
            description='Update a task\'s fields via JSON. Set {"isArchived": true} to mark done. Fields: action, description, status, startTime, isArchived.',
            input_schema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Planhat task _id"},
                    "data_json": {
                        "type": "string",
                        "description": 'JSON object with fields to update, e.g. {"isArchived": true}',
                    },
                },
                "required": ["task_id", "data_json"],
            },
            source="integration:planhat",
            priority=1,
        ),
        # ── Opportunities (3) ───────────────────────────────────────────
        ToolDefinition(
            name="list_planhat_opportunities",
            description="List sales opportunities with title, stage, status, and revenue (MRR/ARR).",
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "Company ID to filter by (optional)",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "default": 15,
                        "description": "Number of opportunities to return (max 2000)",
                    },
                },
                "required": [],
            },
            source="integration:planhat",
            priority=1,
        ),
        ToolDefinition(
            name="create_planhat_opportunity",
            description="Create a sales opportunity for a company with title, stage, and revenue values.",
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "Company ID (required)",
                    },
                    "title": {
                        "type": "string",
                        "description": "Opportunity title (required)",
                    },
                    "sales_stage": {
                        "type": "string",
                        "description": "Sales stage, e.g. 'Pitched', 'Negotiation'",
                        "default": "",
                    },
                    "mrr": {
                        "type": "number",
                        "description": "Monthly recurring revenue value",
                        "default": 0,
                    },
                    "nrr": {
                        "type": "number",
                        "description": "Non-recurring revenue value",
                        "default": 0,
                    },
                    "status": {
                        "type": "string",
                        "enum": ["active", "won", "lost"],
                        "default": "active",
                    },
                },
                "required": ["company_id", "title"],
            },
            source="integration:planhat",
            priority=1,
        ),
        ToolDefinition(
            name="update_planhat_opportunity",
            description="Update an opportunity's fields via JSON. Fields: title, salesStage, status (active/won/lost), mrr, arr, nrr, closeDate.",
            input_schema={
                "type": "object",
                "properties": {
                    "opportunity_id": {
                        "type": "string",
                        "description": "Planhat opportunity _id",
                    },
                    "data_json": {
                        "type": "string",
                        "description": 'JSON object with fields to update, e.g. {"status": "won", "salesStage": "Closed won"}',
                    },
                },
                "required": ["opportunity_id", "data_json"],
            },
            source="integration:planhat",
            priority=1,
        ),
        # ── Licenses (1) ────────────────────────────────────────────────
        ToolDefinition(
            name="list_planhat_licenses",
            description="List licenses (subscriptions) with MRR, product, renewal status, and dates. Optionally filter by company.",
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "Company ID to filter by (optional)",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Number of licenses to return (max 2000)",
                    },
                },
                "required": [],
            },
            source="integration:planhat",
        ),
        # ── Assets (1) ──────────────────────────────────────────────────
        ToolDefinition(
            name="list_planhat_assets",
            description="List assets (product instances) with name, value, and status. Optionally filter by company.",
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "Company ID to filter by (optional)",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Number of assets to return (max 2000)",
                    },
                },
                "required": [],
            },
            source="integration:planhat",
        ),
        # ── Tickets and one conversation read (3) ─────────────────────
        ToolDefinition(
            name="list_planhat_tickets",
            description=(
                "List support tickets with status, subject, source, and severity. "
                "Filter by company and/or status: 'open' = any status other than "
                "Closed, Solved or Resolved; 'closed' = those; any other value "
                "(e.g. 'Queued', 'In Progress', 'Waiting for customer') matches "
                "the status exactly, case-insensitive. count applies after the filter."
            ),
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "company_id": {
                        "type": "string",
                        "description": "Company ID to filter by (optional)",
                        "default": "",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by ticket status: 'open', 'closed', or an exact status such as 'Queued'",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "default": 20,
                        "description": "Number of tickets to return (max 10000)",
                    },
                },
                "required": [],
            },
            source="integration:planhat",
        ),
        ToolDefinition(
            name="get_planhat_ticket",
            description=(
                "Get full details of a single support ticket by its ID "
                "(the _id returned by list_planhat_tickets), including status, "
                "source, severity, content, company, custom fields, creation "
                "date and last update (updatedAt; Planhat exposes no status "
                "history)."
            ),
            priority=1,
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_id": {
                        "type": "string",
                        "description": "Planhat ticket _id (as returned by list_planhat_tickets)",
                    }
                },
                "required": ["ticket_id"],
            },
            source="integration:planhat",
        ),
        ToolDefinition(
            name="get_planhat_conversation",
            description=(
                "Get one conversation (note, email, call, ticket) by its ID (the "
                "_id returned by list_planhat_conversations): the full body, "
                "subject, type, send time (UTC), company and users."
            ),
            priority=1,
            input_schema={
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "Planhat conversation _id (as returned by list_planhat_conversations)",
                    }
                },
                "required": ["conversation_id"],
            },
            source="integration:planhat",
        ),
    ]
