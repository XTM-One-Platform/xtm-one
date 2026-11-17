# SPDX-License-Identifier: Apache-2.0

"""Zendesk integration - remediation and incident tickets.

Zendesk Support through the REST API v2:

  - Tickets: search, list, read with comments, create, update (status,
    priority, assignee, tags, a public reply or an internal note), delete and
    restore a deleted one; the ticket fields of the account.
  - Users: search, read, the organizations of one, create, update, delete.
  - Organizations: list, read, create, update, delete.

Authentication is an API token for an agent's email: HTTP Basic with
``<email>/token`` as the user name and the token as the password. The account
is ``https://<subdomain>.zendesk.com``: only the subdomain is configured, so the
host is always a Zendesk one.

Token format: ``subdomain||email||api_token`` (assembled by ``_get_token``).
"""

from __future__ import annotations

import base64
import re
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    clip_text,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Zendesk"
_SOURCE = "integration:zendesk"

_TIMEOUT = 30
_DEFAULT_LIMIT = 25
_MAX_LIMIT = 100

_SUBDOMAIN_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_EMAIL_RE = re.compile(r"[^@\s:]{1,64}@[^@\s:]{1,255}\.[^@\s:]{2,63}")

TICKET_STATUSES = ("new", "open", "pending", "hold", "solved", "closed")
_WRITABLE_STATUSES = ("new", "open", "pending", "hold", "solved")
TICKET_PRIORITIES = ("low", "normal", "high", "urgent")
TICKET_TYPES = ("problem", "incident", "question", "task")
_USER_ROLES = ("end-user", "agent", "admin")


def zendesk_subdomain(value: Any) -> str | None:
    """The account subdomain in *value* (``acme``, ``acme.zendesk.com`` or its URL)."""
    text = str(value or "").strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if text.endswith(".zendesk.com"):
        text = text[: -len(".zendesk.com")]
    return text if _SUBDOMAIN_RE.fullmatch(text) else None


def zendesk_api(subdomain: str) -> str:
    return f"https://{subdomain}.zendesk.com/api/v2"


def build_zendesk_headers(email: str, api_token: str) -> dict[str, str]:
    """The one header builder, shared with the connectivity test."""
    raw = f"{(email or '').strip()}/token:{(api_token or '').strip()}".encode()
    return {
        "Authorization": f"Basic {base64.b64encode(raw).decode()}",
        "Accept": "application/json",
    }


def parse_zendesk_token(token: str) -> tuple[str | None, str, str]:
    """``(subdomain, email, api_token)``; subdomain ``None`` when it is not one."""
    subdomain, email, api_token = parse_compound_token(token or "", 3, greedy_last=True)
    email = email.strip()
    return (
        zendesk_subdomain(subdomain),
        email if _EMAIL_RE.fullmatch(email) else "",
        api_token.strip(),
    )


def _clean(value: Any, limit: int = 200) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return clip_text(text, limit) if text else "-"


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _limit(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(n, _MAX_LIMIT))


def _id(value: Any) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _tags(value: Any) -> list[str]:
    items = value if isinstance(value, list) else str(value or "").replace(",", " ").split()
    return [re.sub(r"\s+", "_", str(t).strip())[:80] for t in items if str(t).strip()]


def _format_ticket(t: dict[str, Any], *, detail: bool = False) -> str:
    lines = [
        f"#{t.get('id', '?')} {_clean(t.get('subject'))} [{_clean(t.get('status'), 20)}]",
        f"  Priority: {_clean(t.get('priority'), 20)} | Type: {_clean(t.get('type'), 20)}"
        f" | Assignee id: {t.get('assignee_id') or '-'} | Requester id: {t.get('requester_id') or '-'}",
    ]
    if t.get("tags"):
        lines.append(f"  Tags: {', '.join(_clean(x, 60) for x in t['tags'])}")
    if detail:
        lines.append(f"  Created: {_clean(t.get('created_at'), 40)} | Updated: {_clean(t.get('updated_at'), 40)}")
        if t.get("description"):
            lines.append("  Description:\n" + clip_text(str(t["description"]), 4000))
    return "\n".join(lines)


def _format_user(u: dict[str, Any]) -> str:
    return (
        f"- {_clean(u.get('name'), 120)} (id: {u.get('id', '?')}): {_clean(u.get('email'), 120)}, "
        f"role {_clean(u.get('role'), 20)}, organization id {u.get('organization_id') or '-'}"
        + (", suspended" if u.get("suspended") else "")
    )


def _format_org(o: dict[str, Any]) -> str:
    domains = ", ".join(_clean(d, 80) for d in o.get("domain_names") or [])
    return f"- {_clean(o.get('name'), 120)} (id: {o.get('id', '?')})" + (f": {domains}" if domains else "")


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    subdomain, email, api_token = parse_zendesk_token(token)
    if not subdomain or not email or not api_token:
        return []
    api = zendesk_api(subdomain)
    headers = build_zendesk_headers(email, api_token)

    async def _get(path: str, action: str, params: Any = None):
        async with egress_client(timeout=_TIMEOUT) as client:
            return await safe_request(
                client, "get", f"{api}{path}", _PROVIDER, action,
                headers=headers, params=params, retry_statuses=TRANSIENT_RETRY_STATUSES,
            )

    async def _write(method: str, path: str, action: str, body: dict | None = None):
        async with egress_client(timeout=_TIMEOUT) as client:
            return await safe_request(client, method, f"{api}{path}", _PROVIDER, action, headers=headers, json=body)

    def _bad_id(name: str) -> str:
        return f"Error: {name} must be a numeric Zendesk id."

    # -- Tickets ----------------------------------------------------------

    async def search_zendesk_tickets(
        query: str = "", status: str = "", tags: list[str] | None = None,
        limit: int = _DEFAULT_LIMIT, page: int = 1,
    ) -> str:
        if status and status not in TICKET_STATUSES:
            return f"Error: status must be one of {', '.join(TICKET_STATUSES)}."
        terms = ["type:ticket"]
        if status:
            terms.append(f"status:{status}")
        terms += [f"tags:{t}" for t in _tags(tags)]
        if query:
            terms.append(" ".join(str(query).split())[:500])
        try:
            page_no = max(1, min(int(page or 1), 1000))
        except (TypeError, ValueError):
            page_no = 1
        n = _limit(limit)
        params = {"query": " ".join(terms), "sort_by": "updated_at", "sort_order": "desc", "per_page": n, "page": page_no}
        resp, err = await _get("/search.json", "search tickets", params)
        if err:
            return err
        data = _json(resp)
        tickets = [t for t in data.get("results") or [] if isinstance(t, dict)]
        if not tickets:
            return "No tickets found."
        out = f"Found {data.get('count', len(tickets))} ticket(s), showing {len(tickets)}:\n"
        out += "\n".join(_format_ticket(t) for t in tickets)
        if data.get("next_page"):
            out += f"\n[More results: pass page={page_no + 1}]"
        return out

    async def get_zendesk_ticket(ticket_id: int, include_comments: bool = True) -> str:
        tid = _id(ticket_id)
        if tid is None:
            return _bad_id("ticket_id")
        resp, err = await _get(f"/tickets/{tid}.json", f"get ticket {tid}")
        if err:
            return err
        out = _format_ticket(_json(resp).get("ticket") or {}, detail=True)
        if include_comments:
            out += "\n" + await list_zendesk_ticket_comments(tid)
        return out

    async def list_zendesk_ticket_comments(ticket_id: int, limit: int = _DEFAULT_LIMIT) -> str:
        tid = _id(ticket_id)
        if tid is None:
            return _bad_id("ticket_id")
        resp, err = await _get(
            f"/tickets/{tid}/comments.json", f"list comments of ticket {tid}",
            {"page[size]": _limit(limit), "sort": "-created_at"},
        )
        if err:
            return err
        comments = [c for c in _json(resp).get("comments") or [] if isinstance(c, dict)]
        if not comments:
            return f"Ticket {tid} has no comments."
        lines = [
            f"- {_clean(c.get('created_at'), 40)} author {c.get('author_id', '?')}"
            f" ({'public' if c.get('public') else 'internal'}): {_clean(c.get('plain_body') or c.get('body'), 2000)}"
            for c in comments
        ]
        return f"Comments on #{tid}, newest first ({len(comments)}):\n" + "\n".join(lines)

    async def create_zendesk_ticket(
        subject: str, description: str, priority: str = "", type: str = "", tags: list[str] | None = None,
        requester_email: str = "", assignee_id: int | None = None, group_id: int | None = None,
        external_id: str = "",
    ) -> str:
        head = " ".join(str(subject or "").split())
        text = str(description or "").strip()
        if not head or not text:
            return "Error: subject and description are required."
        ticket: dict[str, Any] = {"subject": head[:255], "comment": {"body": text[:65000]}}
        if priority:
            if priority not in TICKET_PRIORITIES:
                return f"Error: priority must be one of {', '.join(TICKET_PRIORITIES)}."
            ticket["priority"] = priority
        if type:
            if type not in TICKET_TYPES:
                return f"Error: type must be one of {', '.join(TICKET_TYPES)}."
            ticket["type"] = type
        if tags:
            ticket["tags"] = _tags(tags)
        if requester_email:
            if not _EMAIL_RE.fullmatch(str(requester_email).strip()):
                return "Error: requester_email must be an email address."
            ticket["requester"] = {"email": str(requester_email).strip()}
        for key, value in (("assignee_id", assignee_id), ("group_id", group_id)):
            if value is not None:
                if _id(value) is None:
                    return _bad_id(key)
                ticket[key] = _id(value)
        if external_id:
            ticket["external_id"] = str(external_id)[:255]
        resp, err = await _write("post", "/tickets.json", f"create ticket {head[:60]!r}", {"ticket": ticket})
        if err:
            return err
        return "Created ticket:\n" + _format_ticket(_json(resp).get("ticket") or {})

    async def update_zendesk_ticket(
        ticket_id: int, status: str = "", priority: str = "", subject: str = "",
        assignee_id: int | None = None, group_id: int | None = None, tags: list[str] | None = None,
        comment: str = "", public: bool = False,
    ) -> str:
        tid = _id(ticket_id)
        if tid is None:
            return _bad_id("ticket_id")
        ticket: dict[str, Any] = {}
        if status:
            if status not in _WRITABLE_STATUSES:
                return f"Error: status must be one of {', '.join(_WRITABLE_STATUSES)}."
            ticket["status"] = status
        if priority:
            if priority not in TICKET_PRIORITIES:
                return f"Error: priority must be one of {', '.join(TICKET_PRIORITIES)}."
            ticket["priority"] = priority
        if subject:
            ticket["subject"] = " ".join(str(subject).split())[:255]
        for key, value in (("assignee_id", assignee_id), ("group_id", group_id)):
            if value is not None:
                if _id(value) is None:
                    return _bad_id(key)
                ticket[key] = _id(value)
        if tags is not None:
            ticket["tags"] = _tags(tags)
        if comment:
            # Internal note unless public: a reply to the requester is a choice.
            ticket["comment"] = {"body": str(comment)[:65000], "public": bool(public)}
        if not ticket:
            return "Error: nothing to change."
        resp, err = await _write("put", f"/tickets/{tid}.json", f"update ticket {tid}", {"ticket": ticket})
        if err:
            return err
        return "Updated ticket:\n" + _format_ticket(_json(resp).get("ticket") or {})

    async def delete_zendesk_ticket(ticket_id: int) -> str:
        tid = _id(ticket_id)
        if tid is None:
            return _bad_id("ticket_id")
        _resp, err = await _write("delete", f"/tickets/{tid}.json", f"delete ticket {tid}")
        return err or f"Deleted ticket {tid} (restorable for 30 days with restore_zendesk_ticket)."

    async def restore_zendesk_ticket(ticket_id: int) -> str:
        tid = _id(ticket_id)
        if tid is None:
            return _bad_id("ticket_id")
        _resp, err = await _write("put", f"/deleted_tickets/{tid}/restore.json", f"restore ticket {tid}")
        return err or f"Restored ticket {tid}."

    async def list_zendesk_ticket_fields() -> str:
        resp, err = await _get("/ticket_fields.json", "list ticket fields", {"page[size]": _MAX_LIMIT})
        if err:
            return err
        fields = [f for f in _json(resp).get("ticket_fields") or [] if isinstance(f, dict)]
        if not fields:
            return "No ticket fields found."
        lines = [
            f"- {_clean(f.get('title'), 120)} (id: {f.get('id', '?')}): type {_clean(f.get('type'), 30)}"
            + (", required" if f.get("required") else "")
            + ("" if f.get("active", True) else ", inactive")
            for f in fields
        ]
        return f"Ticket fields ({len(fields)}):\n" + "\n".join(lines)

    async def get_zendesk_ticket_field(field_id: int) -> str:
        fid = _id(field_id)
        if fid is None:
            return _bad_id("field_id")
        resp, err = await _get(f"/ticket_fields/{fid}.json", f"get ticket field {fid}")
        if err:
            return err
        f = _json(resp).get("ticket_field") or {}
        options = ", ".join(
            f"{_clean(o.get('name'), 60)}={_clean(o.get('value'), 60)}"
            for o in f.get("custom_field_options") or [] if isinstance(o, dict)
        )
        return (
            f"{_clean(f.get('title'), 120)} (id: {f.get('id', '?')}): type {_clean(f.get('type'), 30)}"
            + (f"\n  Options: {options}" if options else "")
            + (f"\n  Description: {_clean(f.get('description'), 500)}" if f.get("description") else "")
        )

    # -- Users ------------------------------------------------------------

    async def search_zendesk_users(query: str = "", role: str = "", limit: int = _DEFAULT_LIMIT) -> str:
        if role and role not in _USER_ROLES:
            return f"Error: role must be one of {', '.join(_USER_ROLES)}."
        n = _limit(limit)
        if query:
            path, params = "/users/search.json", {"query": str(query)[:200], "per_page": n}
        else:
            path, params = "/users.json", {"page[size]": n}
        if role:
            params["role"] = role
        resp, err = await _get(path, "search users", params)
        if err:
            return err
        users = [u for u in _json(resp).get("users") or [] if isinstance(u, dict)]
        if not users:
            return "No users found."
        return f"Users ({len(users)}):\n" + "\n".join(_format_user(u) for u in users)

    async def get_zendesk_user(user_id: int) -> str:
        uid = _id(user_id)
        if uid is None:
            return _bad_id("user_id")
        resp, err = await _get(f"/users/{uid}.json", f"get user {uid}")
        if err:
            return err
        return _format_user(_json(resp).get("user") or {})[2:]

    async def list_zendesk_user_organizations(user_id: int) -> str:
        uid = _id(user_id)
        if uid is None:
            return _bad_id("user_id")
        resp, err = await _get(f"/users/{uid}/organizations.json", f"list organizations of user {uid}")
        if err:
            return err
        orgs = [o for o in _json(resp).get("organizations") or [] if isinstance(o, dict)]
        if not orgs:
            return f"User {uid} belongs to no organization."
        return f"Organizations of user {uid} ({len(orgs)}):\n" + "\n".join(_format_org(o) for o in orgs)

    async def create_zendesk_user(name: str, email: str, role: str = "end-user", organization_id: int | None = None) -> str:
        who = " ".join(str(name or "").split())
        if not who:
            return "Error: name is required."
        if not _EMAIL_RE.fullmatch(str(email or "").strip()):
            return "Error: email must be an email address."
        if role not in _USER_ROLES:
            return f"Error: role must be one of {', '.join(_USER_ROLES)}."
        user: dict[str, Any] = {"name": who[:255], "email": str(email).strip(), "role": role}
        if organization_id is not None:
            if _id(organization_id) is None:
                return _bad_id("organization_id")
            user["organization_id"] = _id(organization_id)
        resp, err = await _write("post", "/users.json", f"create user {who[:60]!r}", {"user": user})
        if err:
            return err
        return "Created user " + _format_user(_json(resp).get("user") or {})[2:]

    async def update_zendesk_user(
        user_id: int, name: str = "", email: str = "", role: str = "", organization_id: int | None = None,
        suspended: bool | None = None,
    ) -> str:
        uid = _id(user_id)
        if uid is None:
            return _bad_id("user_id")
        user: dict[str, Any] = {}
        if name:
            user["name"] = " ".join(str(name).split())[:255]
        if email:
            if not _EMAIL_RE.fullmatch(str(email).strip()):
                return "Error: email must be an email address."
            user["email"] = str(email).strip()
        if role:
            if role not in _USER_ROLES:
                return f"Error: role must be one of {', '.join(_USER_ROLES)}."
            user["role"] = role
        if organization_id is not None:
            if _id(organization_id) is None:
                return _bad_id("organization_id")
            user["organization_id"] = _id(organization_id)
        if suspended is not None:
            user["suspended"] = bool(suspended)
        if not user:
            return "Error: nothing to change."
        resp, err = await _write("put", f"/users/{uid}.json", f"update user {uid}", {"user": user})
        if err:
            return err
        return "Updated user " + _format_user(_json(resp).get("user") or {})[2:]

    async def delete_zendesk_user(user_id: int) -> str:
        uid = _id(user_id)
        if uid is None:
            return _bad_id("user_id")
        _resp, err = await _write("delete", f"/users/{uid}.json", f"delete user {uid}")
        return err or f"Deleted user {uid}."

    # -- Organizations ----------------------------------------------------

    async def list_zendesk_organizations(name: str = "", limit: int = _DEFAULT_LIMIT) -> str:
        if name:
            path, params = "/organizations/autocomplete.json", {"name": str(name)[:100]}
        else:
            path, params = "/organizations.json", {"page[size]": _limit(limit)}
        resp, err = await _get(path, "list organizations", params)
        if err:
            return err
        orgs = [o for o in _json(resp).get("organizations") or [] if isinstance(o, dict)]
        if not orgs:
            return "No organizations found."
        return f"Organizations ({len(orgs)}):\n" + "\n".join(_format_org(o) for o in orgs)

    async def get_zendesk_organization(organization_id: int) -> str:
        oid = _id(organization_id)
        if oid is None:
            return _bad_id("organization_id")
        resp, err = await _get(f"/organizations/{oid}.json", f"get organization {oid}")
        if err:
            return err
        o = _json(resp).get("organization") or {}
        return _format_org(o)[2:] + (f"\n  Notes: {_clean(o.get('notes'), 1000)}" if o.get("notes") else "")

    async def create_zendesk_organization(name: str, domain_names: list[str] | None = None, notes: str = "") -> str:
        title = " ".join(str(name or "").split())
        if not title:
            return "Error: name is required."
        org: dict[str, Any] = {"name": title[:255]}
        if domain_names:
            org["domain_names"] = [str(d).strip()[:255] for d in domain_names if str(d).strip()]
        if notes:
            org["notes"] = str(notes)[:65000]
        resp, err = await _write("post", "/organizations.json", f"create organization {title[:60]!r}", {"organization": org})
        if err:
            return err
        return "Created organization " + _format_org(_json(resp).get("organization") or {})[2:]

    async def update_zendesk_organization(
        organization_id: int, name: str = "", domain_names: list[str] | None = None, notes: str | None = None
    ) -> str:
        oid = _id(organization_id)
        if oid is None:
            return _bad_id("organization_id")
        org: dict[str, Any] = {}
        if name:
            org["name"] = " ".join(str(name).split())[:255]
        if domain_names is not None:
            org["domain_names"] = [str(d).strip()[:255] for d in domain_names if str(d).strip()]
        if notes is not None:
            org["notes"] = str(notes)[:65000]
        if not org:
            return "Error: nothing to change."
        resp, err = await _write("put", f"/organizations/{oid}.json", f"update organization {oid}", {"organization": org})
        if err:
            return err
        return "Updated organization " + _format_org(_json(resp).get("organization") or {})[2:]

    async def delete_zendesk_organization(organization_id: int) -> str:
        oid = _id(organization_id)
        if oid is None:
            return _bad_id("organization_id")
        _resp, err = await _write("delete", f"/organizations/{oid}.json", f"delete organization {oid}")
        return err or f"Deleted organization {oid}."

    handlers = {
        "search_zendesk_tickets": search_zendesk_tickets,
        "get_zendesk_ticket": get_zendesk_ticket,
        "list_zendesk_ticket_comments": list_zendesk_ticket_comments,
        "create_zendesk_ticket": create_zendesk_ticket,
        "update_zendesk_ticket": update_zendesk_ticket,
        "delete_zendesk_ticket": delete_zendesk_ticket,
        "restore_zendesk_ticket": restore_zendesk_ticket,
        "list_zendesk_ticket_fields": list_zendesk_ticket_fields,
        "get_zendesk_ticket_field": get_zendesk_ticket_field,
        "search_zendesk_users": search_zendesk_users,
        "get_zendesk_user": get_zendesk_user,
        "list_zendesk_user_organizations": list_zendesk_user_organizations,
        "create_zendesk_user": create_zendesk_user,
        "update_zendesk_user": update_zendesk_user,
        "delete_zendesk_user": delete_zendesk_user,
        "list_zendesk_organizations": list_zendesk_organizations,
        "get_zendesk_organization": get_zendesk_organization,
        "create_zendesk_organization": create_zendesk_organization,
        "update_zendesk_organization": update_zendesk_organization,
        "delete_zendesk_organization": delete_zendesk_organization,
    }
    for tool_name, fn in handlers.items():
        executor.register(tool_name, fn)

    def _s(description: str, **extra: Any) -> dict:
        return {"type": "string", "description": description, **extra}

    def _i(description: str) -> dict:
        return {"type": "integer", "description": description}

    tags_p = {"type": "array", "items": {"type": "string"}, "description": "Tags"}
    limit_p = _i(f"Most items returned (default {_DEFAULT_LIMIT}, max {_MAX_LIMIT})")
    ticket_p = _i("Ticket number")
    user_p = _i("User id")
    org_p = _i("Organization id")
    domains_p = {"type": "array", "items": {"type": "string"}, "description": "Email domains of the organization"}

    def _tool(name: str, description: str, properties: dict, required: list[str]) -> ToolDefinition:
        return ToolDefinition(
            name=name,
            description=description,
            input_schema={"type": "object", "properties": properties, "required": required},
            source=_SOURCE,
        )

    return [
        _tool(
            "search_zendesk_tickets",
            "Search Zendesk tickets, most recently updated first, by status, tags and free text "
            "(Zendesk search syntax accepted, e.g. 'priority:urgent assignee:none').",
            {
                "query": _s("Text or Zendesk search terms"),
                "status": _s("Ticket status", enum=list(TICKET_STATUSES)),
                "tags": tags_p,
                "limit": limit_p,
                "page": _i("Page, from the previous answer's hint"),
            },
            [],
        ),
        _tool("get_zendesk_ticket", "Read a Zendesk ticket with its description and comments.",
              {"ticket_id": ticket_p, "include_comments": {"type": "boolean", "description": "Default true"}},
              ["ticket_id"]),
        _tool("list_zendesk_ticket_comments", "List a Zendesk ticket's comments, newest first, public and internal.",
              {"ticket_id": ticket_p, "limit": limit_p}, ["ticket_id"]),
        _tool(
            "create_zendesk_ticket",
            "Open a Zendesk ticket, e.g. a remediation request for an exposure. external_id ties it to "
            "the finding it tracks.",
            {
                "subject": _s("Subject"),
                "description": _s("First comment: what was found and what to do"),
                "priority": _s("Priority", enum=list(TICKET_PRIORITIES)),
                "type": _s("Type", enum=list(TICKET_TYPES)),
                "tags": tags_p,
                "requester_email": _s("Requester's email (default: the integration's agent)"),
                "assignee_id": _i("Agent id"),
                "group_id": _i("Group id"),
                "external_id": _s("Id of the finding in another system"),
            },
            ["subject", "description"],
        ),
        _tool(
            "update_zendesk_ticket",
            "Change a Zendesk ticket's status, priority, subject, assignee, group or tags, and add a "
            "comment: an internal note by default, a reply to the requester with public=true.",
            {
                "ticket_id": ticket_p,
                "status": _s("New status", enum=list(_WRITABLE_STATUSES)),
                "priority": _s("Priority", enum=list(TICKET_PRIORITIES)),
                "subject": _s("New subject"),
                "assignee_id": _i("Agent id"),
                "group_id": _i("Group id"),
                "tags": {**tags_p, "description": "Replace every tag"},
                "comment": _s("Comment text"),
                "public": {"type": "boolean", "description": "Send the comment to the requester (default false: internal note)"},
            },
            ["ticket_id"],
        ),
        _tool("delete_zendesk_ticket", "Delete a Zendesk ticket (restorable for 30 days).",
              {"ticket_id": ticket_p}, ["ticket_id"]),
        _tool("restore_zendesk_ticket", "Restore a deleted Zendesk ticket.", {"ticket_id": ticket_p}, ["ticket_id"]),
        _tool("list_zendesk_ticket_fields", "List the ticket fields of the Zendesk account, custom ones included.", {}, []),
        _tool("get_zendesk_ticket_field", "Read a Zendesk ticket field and its options.",
              {"field_id": _i("Ticket field id")}, ["field_id"]),
        _tool(
            "search_zendesk_users",
            "Find Zendesk users by name or email (or list them), optionally by role.",
            {"query": _s("Name or email"), "role": _s("Role", enum=list(_USER_ROLES)), "limit": limit_p},
            [],
        ),
        _tool("get_zendesk_user", "Read a Zendesk user.", {"user_id": user_p}, ["user_id"]),
        _tool("list_zendesk_user_organizations", "List the organizations a Zendesk user belongs to.",
              {"user_id": user_p}, ["user_id"]),
        _tool(
            "create_zendesk_user",
            "Create a Zendesk user.",
            {"name": _s("Name"), "email": _s("Email"), "role": _s("Role (default end-user)", enum=list(_USER_ROLES)),
             "organization_id": org_p},
            ["name", "email"],
        ),
        _tool(
            "update_zendesk_user",
            "Change a Zendesk user's name, email, role or organization, or suspend them.",
            {"user_id": user_p, "name": _s("Name"), "email": _s("Email"), "role": _s("Role", enum=list(_USER_ROLES)),
             "organization_id": org_p, "suspended": {"type": "boolean"}},
            ["user_id"],
        ),
        _tool("delete_zendesk_user", "Delete a Zendesk user.", {"user_id": user_p}, ["user_id"]),
        _tool("list_zendesk_organizations", "List Zendesk organizations, or find them by name.",
              {"name": _s("Start of the organization name"), "limit": limit_p}, []),
        _tool("get_zendesk_organization", "Read a Zendesk organization.", {"organization_id": org_p}, ["organization_id"]),
        _tool("create_zendesk_organization", "Create a Zendesk organization.",
              {"name": _s("Name"), "domain_names": domains_p, "notes": _s("Notes")}, ["name"]),
        _tool("update_zendesk_organization", "Change a Zendesk organization's name, domains or notes.",
              {"organization_id": org_p, "name": _s("Name"), "domain_names": domains_p, "notes": _s("Notes")},
              ["organization_id"]),
        _tool("delete_zendesk_organization", "Delete a Zendesk organization.",
              {"organization_id": org_p}, ["organization_id"]),
    ]
