"""Microsoft Purview eDiscovery (Premium) - cases, custodians, searches, holds.

The eDiscovery API in Microsoft Graph (``/security/cases/ediscoveryCases``)
manages the full legal-investigation lifecycle: a *case* contains *custodians*
(people whose data is in scope), *searches* (collections from Exchange /
SharePoint / Teams), *legal holds* (preservation), *review sets* (static
collected ESI), and async *operations* (the long-running jobs cases spawn).
Requires the eDiscovery roles in Microsoft Purview plus the
``eDiscovery.Read.All`` / ``eDiscovery.ReadWrite.All`` Graph permission.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import PurviewClient, dt, fmt_list, odata_quote, safe_json, trunc

SRC = "integration:purview"
_CASES = "/security/cases/ediscoveryCases"


def _case_id(value: str) -> str:
    return quote(value.strip(), safe="")


def _fmt_case(c: dict[str, Any]) -> str:
    return (
        f"- {c.get('displayName', 'Untitled')} | status: {c.get('status', '?')}\n"
        f"  id: {c.get('id', '?')} | externalId: {c.get('externalId') or '-'} | "
        f"created: {dt(c.get('createdDateTime'))}\n"
        f"  {trunc(c.get('description'), 160)}"
    )


def _fmt_custodian(c: dict[str, Any]) -> str:
    return (
        f"- {c.get('displayName') or c.get('email', '?')} <{c.get('email', '?')}>\n"
        f"  id: {c.get('id', '?')} | status: {c.get('status', '?')} | "
        f"hold: {c.get('holdStatus', '?')}"
    )


def _fmt_search(s: dict[str, Any]) -> str:
    return (
        f"- {s.get('displayName', 'Untitled')} | id: {s.get('id', '?')}\n"
        f"  query: {trunc(s.get('contentQuery'), 160) or '-'} | "
        f"created: {dt(s.get('createdDateTime'))}"
    )


def _fmt_hold(h: dict[str, Any]) -> str:
    return (
        f"- {h.get('displayName', 'Untitled')} | id: {h.get('id', '?')} | "
        f"status: {h.get('status', '?')}\n"
        f"  query: {trunc(h.get('contentQuery'), 160) or '-'}"
    )


def _fmt_review_set(r: dict[str, Any]) -> str:
    return (
        f"- {r.get('displayName', 'Untitled')} | id: {r.get('id', '?')} | "
        f"created: {dt(r.get('createdDateTime'))}"
    )


def _fmt_operation(o: dict[str, Any]) -> str:
    pct = o.get("percentProgress")
    pct_str = f" | {pct}%" if pct is not None else ""
    return (
        f"- {o.get('action', '?')} | status: {o.get('status', '?')}{pct_str}\n"
        f"  id: {o.get('id', '?')} | created: {dt(o.get('createdDateTime'))} | "
        f"completed: {dt(o.get('completedDateTime'))}"
    )


def register(
    client: PurviewClient, executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def list_purview_ediscovery_cases(
        status: str = "", top: int = 25, skiptoken: str = ""
    ) -> str:
        params: dict[str, Any] = {"$orderby": "createdDateTime desc"}
        if status:
            params["$filter"] = f"status eq '{odata_quote(status)}'"
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.graph_paged(
            _CASES, "list eDiscovery cases", top=min(top, 100), params=params
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_case,
            noun="eDiscovery case",
            skiptoken=next_tok,
            empty_msg="No eDiscovery cases found.",
        )

    async def get_purview_ediscovery_case(case_id: str) -> str:
        data, err = await client.graph_get_json(
            f"{_CASES}/{_case_id(case_id)}", "get eDiscovery case"
        )
        if err:
            return err
        return _fmt_case(data or {})

    async def create_purview_ediscovery_case(
        display_name: str, description: str = "", external_id: str = ""
    ) -> str:
        body: dict[str, Any] = {"displayName": display_name}
        if description:
            body["description"] = description
        if external_id:
            body["externalId"] = external_id
        resp, err = await client.graph_request(
            "post", _CASES, "create eDiscovery case", json=body
        )
        if err:
            return err
        c = safe_json(resp)
        return (
            f"Created eDiscovery case '{c.get('displayName', display_name)}' "
            f"(id: {c.get('id', '?')}, status: {c.get('status', 'active')})."
        )

    async def manage_purview_ediscovery_case(
        action: str,
        case_id: str,
        display_name: str = "",
        description: str = "",
        external_id: str = "",
    ) -> str:
        cid = _case_id(case_id)
        if action == "close":
            _, err = await client.graph_request(
                "post", f"{_CASES}/{cid}/close", "close eDiscovery case"
            )
            return err or (
                f"Closing eDiscovery case {case_id} (an async operation - poll "
                f"with list_purview_ediscovery_operations)."
            )
        if action == "reopen":
            _, err = await client.graph_request(
                "post", f"{_CASES}/{cid}/reopen", "reopen eDiscovery case"
            )
            return err or f"Reopening eDiscovery case {case_id}."
        if action == "update":
            patch: dict[str, Any] = {}
            if display_name:
                patch["displayName"] = display_name
            if description:
                patch["description"] = description
            if external_id:
                patch["externalId"] = external_id
            if not patch:
                return "Error: provide display_name, description, or external_id to update."
            _, err = await client.graph_request(
                "patch", f"{_CASES}/{cid}", "update eDiscovery case", json=patch
            )
            return err or f"Updated eDiscovery case {case_id}."
        if action == "delete":
            _, err = await client.graph_request(
                "delete", f"{_CASES}/{cid}", "delete eDiscovery case"
            )
            return err or (
                f"Deleted eDiscovery case {case_id}. This is permanent and cannot "
                f"be undone."
            )
        return "Error: unknown action. Valid actions: close, reopen, update, delete."

    async def list_purview_ediscovery_custodians(
        case_id: str, top: int = 50, skiptoken: str = ""
    ) -> str:
        params: dict[str, Any] = {}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.graph_paged(
            f"{_CASES}/{_case_id(case_id)}/custodians",
            "list custodians",
            top=min(top, 100),
            params=params,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_custodian,
            noun="custodian",
            skiptoken=next_tok,
            empty_msg="No custodians on this case.",
        )

    async def manage_purview_ediscovery_custodian(
        action: str, case_id: str, email: str = "", custodian_id: str = ""
    ) -> str:
        cid = _case_id(case_id)
        if action == "add":
            if not email:
                return "Error: email is required to add a custodian."
            resp, err = await client.graph_request(
                "post",
                f"{_CASES}/{cid}/custodians",
                "add custodian",
                json={"email": email},
            )
            if err:
                return err
            cust = safe_json(resp)
            return (
                f"Added custodian {email} (id: {cust.get('id', '?')}). Create the "
                f"custodian's data sources next to reference their mailbox / OneDrive."
            )
        if action in ("release", "activate"):
            if not custodian_id:
                return f"Error: custodian_id is required to {action} a custodian."
            verb = "release" if action == "release" else "activate"
            _, err = await client.graph_request(
                "post",
                f"{_CASES}/{cid}/custodians/{_case_id(custodian_id)}/{verb}",
                f"{verb} custodian",
            )
            return err or f"Requested to {verb} custodian {custodian_id} (async)."
        return "Error: unknown action. Valid actions: add, release, activate."

    async def list_purview_ediscovery_searches(
        case_id: str, top: int = 50, skiptoken: str = ""
    ) -> str:
        params: dict[str, Any] = {}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.graph_paged(
            f"{_CASES}/{_case_id(case_id)}/searches",
            "list searches",
            top=min(top, 100),
            params=params,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_search,
            noun="search",
            skiptoken=next_tok,
            empty_msg="No searches on this case.",
        )

    async def manage_purview_ediscovery_search(
        action: str,
        case_id: str,
        search_id: str = "",
        display_name: str = "",
        content_query: str = "",
        description: str = "",
    ) -> str:
        cid = _case_id(case_id)
        base = f"{_CASES}/{cid}/searches"
        if action == "create":
            if not display_name:
                return "Error: display_name is required to create a search."
            body: dict[str, Any] = {"displayName": display_name}
            if content_query:
                body["contentQuery"] = content_query
            if description:
                body["description"] = description
            resp, err = await client.graph_request(
                "post", base, "create search", json=body
            )
            if err:
                return err
            s = safe_json(resp)
            return f"Created search '{display_name}' (id: {s.get('id', '?')})."
        if not search_id:
            return f"Error: search_id is required for action '{action}'."
        sid = _case_id(search_id)
        if action == "get":
            data, err = await client.graph_get_json(f"{base}/{sid}", "get search")
            return err or _fmt_search(data or {})
        if action == "update":
            patch: dict[str, Any] = {}
            if display_name:
                patch["displayName"] = display_name
            if content_query:
                patch["contentQuery"] = content_query
            if description:
                patch["description"] = description
            if not patch:
                return "Error: provide display_name, content_query, or description."
            _, err = await client.graph_request(
                "patch", f"{base}/{sid}", "update search", json=patch
            )
            return err or f"Updated search {search_id}."
        if action == "estimate_statistics":
            _, err = await client.graph_request(
                "post",
                f"{base}/{sid}/estimateStatistics",
                "estimate search statistics",
            )
            return err or (
                f"Started statistics estimation for search {search_id} (async - "
                f"poll list_purview_ediscovery_operations for the result)."
            )
        return (
            "Error: unknown action. Valid actions: create, get, update, "
            "estimate_statistics."
        )

    async def list_purview_ediscovery_holds(
        case_id: str, top: int = 50, skiptoken: str = ""
    ) -> str:
        params: dict[str, Any] = {}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.graph_paged(
            f"{_CASES}/{_case_id(case_id)}/legalHolds",
            "list legal holds",
            top=min(top, 100),
            params=params,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_hold,
            noun="legal hold",
            skiptoken=next_tok,
            empty_msg="No legal holds on this case.",
        )

    async def list_purview_ediscovery_review_sets(
        case_id: str, top: int = 50, skiptoken: str = ""
    ) -> str:
        params: dict[str, Any] = {}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.graph_paged(
            f"{_CASES}/{_case_id(case_id)}/reviewSets",
            "list review sets",
            top=min(top, 100),
            params=params,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_review_set,
            noun="review set",
            skiptoken=next_tok,
            empty_msg="No review sets on this case.",
        )

    async def list_purview_ediscovery_operations(
        case_id: str, top: int = 25, skiptoken: str = ""
    ) -> str:
        params: dict[str, Any] = {}
        if skiptoken:
            params["$skiptoken"] = skiptoken
        items, err, next_tok = await client.graph_paged(
            f"{_CASES}/{_case_id(case_id)}/operations",
            "list case operations",
            top=min(top, 100),
            params=params,
        )
        if err:
            return err
        return fmt_list(
            items,
            _fmt_operation,
            noun="case operation",
            skiptoken=next_tok,
            empty_msg="No operations recorded on this case.",
        )

    handlers = {
        "list_purview_ediscovery_cases": list_purview_ediscovery_cases,
        "get_purview_ediscovery_case": get_purview_ediscovery_case,
        "create_purview_ediscovery_case": create_purview_ediscovery_case,
        "manage_purview_ediscovery_case": manage_purview_ediscovery_case,
        "list_purview_ediscovery_custodians": list_purview_ediscovery_custodians,
        "manage_purview_ediscovery_custodian": manage_purview_ediscovery_custodian,
        "list_purview_ediscovery_searches": list_purview_ediscovery_searches,
        "manage_purview_ediscovery_search": manage_purview_ediscovery_search,
        "list_purview_ediscovery_holds": list_purview_ediscovery_holds,
        "list_purview_ediscovery_review_sets": list_purview_ediscovery_review_sets,
        "list_purview_ediscovery_operations": list_purview_ediscovery_operations,
    }
    for name, fn in handlers.items():
        executor.register(name, fn)

    return [
        ToolDefinition(
            name="list_purview_ediscovery_cases",
            description=(
                "List Microsoft Purview eDiscovery (Premium) cases - the "
                "containers for legal investigations. Optionally filter by status "
                "(active, closing, closedWithError, etc.). Returns each case's "
                "displayName, status, id (use with the other eDiscovery tools), "
                "externalId, and created date."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by case status, e.g. 'active' or 'closed'.",
                    },
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="get_purview_ediscovery_case",
            description=(
                "Get one eDiscovery case by id, including its status and "
                "description. Use list_purview_ediscovery_cases first to find the id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string", "description": "The eDiscovery case id."}
                },
                "required": ["case_id"],
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="create_purview_ediscovery_case",
            description=(
                "Create a new eDiscovery (Premium) case. Provide a display_name "
                "and optionally a description and an external case number "
                "(external_id). Returns the new case id to add custodians, "
                "searches, and holds to."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "display_name": {"type": "string", "description": "Case name."},
                    "description": {"type": "string"},
                    "external_id": {
                        "type": "string",
                        "description": "External case / matter number for cross-reference.",
                    },
                },
                "required": ["display_name"],
            },
            source=SRC,
            priority=1,
        ),
        ToolDefinition(
            name="manage_purview_ediscovery_case",
            description=(
                "Lifecycle operations on an eDiscovery case. action is one of: "
                "'close' (close the case - async), 'reopen', 'update' (change "
                "display_name / description / external_id), or 'delete' "
                "(PERMANENT - removes the case and its contents, cannot be undone)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["close", "reopen", "update", "delete"],
                    },
                    "case_id": {"type": "string"},
                    "display_name": {"type": "string"},
                    "description": {"type": "string"},
                    "external_id": {"type": "string"},
                },
                "required": ["action", "case_id"],
            },
            source=SRC,
            priority=2,
        ),
        ToolDefinition(
            name="list_purview_ediscovery_custodians",
            description=(
                "List the custodians on an eDiscovery case - the people whose data "
                "(mailbox, OneDrive, Teams) is in scope. Returns each custodian's "
                "email, status, and hold status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {"type": "string"},
                },
                "required": ["case_id"],
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="manage_purview_ediscovery_custodian",
            description=(
                "Manage custodians on an eDiscovery case. action is one of: 'add' "
                "(add a custodian by email - their data is then held/searchable), "
                "'release' (release the custodian from the case), or 'activate' "
                "(re-activate a released custodian). 'release'/'activate' need the "
                "custodian_id from list_purview_ediscovery_custodians."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "release", "activate"],
                    },
                    "case_id": {"type": "string"},
                    "email": {
                        "type": "string",
                        "description": "Custodian SMTP address (for 'add').",
                    },
                    "custodian_id": {
                        "type": "string",
                        "description": "Custodian id (for 'release'/'activate').",
                    },
                },
                "required": ["action", "case_id"],
            },
            source=SRC,
            priority=1,
        ),
        ToolDefinition(
            name="list_purview_ediscovery_searches",
            description=(
                "List the searches (source collections) on an eDiscovery case - the "
                "queries that collect data from Exchange, SharePoint, and Teams. "
                "Returns each search's displayName, contentQuery (KQL), and id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {"type": "string"},
                },
                "required": ["case_id"],
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="manage_purview_ediscovery_search",
            description=(
                "Manage searches on an eDiscovery case. action is one of: 'create' "
                "(new search with display_name + optional content_query KQL), 'get' "
                "(one search by search_id), 'update' (change the query/name), or "
                "'estimate_statistics' (kick off an async estimate of how much data "
                "the search matches - poll list_purview_ediscovery_operations)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "get", "update", "estimate_statistics"],
                    },
                    "case_id": {"type": "string"},
                    "search_id": {"type": "string"},
                    "display_name": {"type": "string"},
                    "content_query": {
                        "type": "string",
                        "description": "KQL content query, e.g. \"subject:contract AND date>=2024-01-01\".",
                    },
                    "description": {"type": "string"},
                },
                "required": ["action", "case_id"],
            },
            source=SRC,
            priority=1,
        ),
        ToolDefinition(
            name="list_purview_ediscovery_holds",
            description=(
                "List the legal holds on an eDiscovery case (the preservation "
                "policies that lock content for litigation). Returns each hold's "
                "displayName, status, and contentQuery."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {"type": "string"},
                },
                "required": ["case_id"],
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="list_purview_ediscovery_review_sets",
            description=(
                "List the review sets on an eDiscovery case - the static collections "
                "of electronically stored information (ESI) gathered for review and "
                "export. Returns each review set's displayName and id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "top": {"type": "integer", "default": 50},
                    "skiptoken": {"type": "string"},
                },
                "required": ["case_id"],
            },
            source=SRC,
            priority=0,
        ),
        ToolDefinition(
            name="list_purview_ediscovery_operations",
            description=(
                "List the async operations (jobs) on an eDiscovery case - e.g. case "
                "close, statistics estimation, holds being applied, or data being "
                "added to a review set. Returns each operation's action, status, and "
                "percent progress. Use this to poll the result of any async "
                "eDiscovery action."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "top": {"type": "integer", "default": 25},
                    "skiptoken": {"type": "string"},
                },
                "required": ["case_id"],
            },
            source=SRC,
            priority=0,
        ),
    ]
