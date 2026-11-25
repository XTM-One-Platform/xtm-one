"""Jira Service Management (JSM) - service desks, requests, queues, SLA,
approvals, participants, organizations and knowledge base.

Uses the Service Desk REST API (``/rest/servicedeskapi``), which paginates with
``start``/``limit`` (not ``startAt``/``maxResults``) and returns results under a
``values`` key. Customer requests support custom request-field values, discovered
via get_jira_request_type_fields.
"""

from __future__ import annotations

from typing import Any

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _SOURCE,
    body_text,
    clamp,
    comment_body,
    jsm,
    truncate,
    truncate_body,
)
from ._common import jira_request as _request

_REQUEST_STATUS = ("OPEN_REQUESTS", "CLOSED_REQUESTS", "ALL_REQUESTS")


def _account_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _approval_lines(approval: dict[str, Any]) -> list[str]:
    """One approval step: its final decision, then each approver's own decision.

    A step whose approvers come from several groups stays ``pending`` until its
    condition is met (e.g. one approver of each group), so the per-approver
    decisions are what say who has answered and who is still awaited.
    """
    lines = [
        f"  #{approval.get('id', '?')} {approval.get('name', '?')} - "
        f"final decision: {approval.get('finalDecision') or 'pending'}"
    ]
    if approval.get("canAnswerApprovalDecision") is not None:
        can = "yes" if approval.get("canAnswerApprovalDecision") else "no"
        lines.append(f"    you can answer: {can}")
    approvers = approval.get("approvers")
    if isinstance(approvers, list) and approvers:
        lines.append("    approvers:")
        for entry in approvers:
            if not isinstance(entry, dict):
                continue
            raw_person = entry.get("approver")
            person: dict[str, Any] = raw_person if isinstance(raw_person, dict) else {}
            name = person.get("displayName") or "?"
            account = person.get("accountId") or "?"
            lines.append(f"      {name} ({account}): {entry.get('approverDecision') or 'pending'}")
    return lines


def _answered_approval(issue_key: str, approval_id: str, decision: str, resp: Any) -> str:
    """What answering an approval did, read from the step Jira returns.

    The caller's decision is recorded on its own line; the step itself may stay
    pending while other approvers (another group, a quorum) have not answered.
    """
    try:
        approval = resp.json() if resp is not None else None
    except ValueError:
        approval = None
    if not isinstance(approval, dict) or not approval:
        return (
            f"Approval {approval_id} on {issue_key}: your decision '{decision}' was sent; "
            "Jira returned no step state, read it with list_approvals."
        )
    final = approval.get("finalDecision") or "pending"
    lines = [
        f"Approval {approval_id} on {issue_key}: your decision '{decision}' was recorded; "
        f"the step's final decision is {final}."
    ]
    if final == "pending":
        lines.append("  The step still waits for other approvers:")
    lines.extend(_approval_lines(approval)[1:])
    return "\n".join(lines)


def register(
    base: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    api = jsm(base)

    async def list_jira_service_desks(count: int = 50) -> str:
        """List Jira Service Management service desks (id, project name, project key)."""
        resp, err = await _request(
            "get",
            f"{api}/servicedesk",
            "list service desks",
            headers=headers,
            params={"limit": clamp(count, 50)},
        )
        if err:
            return err
        desks = resp.json().get("values", [])
        if not desks:
            return "No service desks found."
        lines = [f"Service desks ({len(desks)}):"]
        for d in desks:
            lines.append(
                f"  #{d.get('id', '?')} {d.get('projectName', '?')} "
                f"(key: {d.get('projectKey', '?')})"
            )
        return "\n".join(lines)

    async def list_jira_request_types(service_desk_id: int, count: int = 50) -> str:
        """List the request types for a service desk (id, name, description)."""
        resp, err = await _request(
            "get",
            f"{api}/servicedesk/{service_desk_id}/requesttype",
            f"list request types for desk {service_desk_id}",
            headers=headers,
            params={"limit": clamp(count, 50)},
        )
        if err:
            return err
        types = resp.json().get("values", [])
        if not types:
            return f"No request types for service desk {service_desk_id}."
        lines = [f"Request types for service desk {service_desk_id} ({len(types)}):"]
        for t in types:
            lines.append(
                f"  #{t.get('id', '?')} {t.get('name', '?')} - "
                f"{truncate(t.get('description') or '', 100)}"
            )
        return "\n".join(lines)

    async def get_jira_request_type_fields(
        service_desk_id: int, request_type_id: int
    ) -> str:
        """Discover the fields (incl. custom + required + allowed values) for creating a JSM request.

        The JSM analogue of get_jira_create_fields - use it to learn the
        requestFieldValues keys (including custom field ids) and value shapes
        before create_jira_service_request.
        """
        resp, err = await _request(
            "get",
            f"{api}/servicedesk/{service_desk_id}/requesttype/{request_type_id}/field",
            f"get request type {request_type_id} fields",
            headers=headers,
        )
        if err:
            return err
        data = resp.json()
        fields = data.get("requestTypeFields", [])
        if not fields:
            return f"No fields returned for request type {request_type_id}."
        lines = [f"Fields for request type {request_type_id} ({len(fields)}):"]
        for f in fields:
            required = " (required)" if f.get("required") else ""
            lines.append(f"  {f.get('name', '?')} - id: {f.get('fieldId', '?')}{required}")
            valid = f.get("validValues") or []
            if valid:
                rendered = [str(v.get("label") or v.get("value") or v) for v in valid[:12]]
                lines.append(f"      valid values: {', '.join(rendered)}")
        if data.get("canRaiseOnBehalfOf"):
            lines.append("  (can raise on behalf of another customer)")
        return "\n".join(lines)

    async def create_jira_service_request(
        service_desk_id: int,
        request_type_id: int,
        summary: str,
        description: str = "",
        reporter_account_id: str = "",
        request_field_values: dict[str, Any] | None = None,
    ) -> str:
        """Create a customer request in JSM.

        ``request_field_values`` is a {fieldId: value} map for extra/custom
        fields (discover them with get_jira_request_type_fields).
        """
        field_values: dict[str, Any] = {"summary": summary}
        if description:
            field_values["description"] = description
        if isinstance(request_field_values, dict):
            for fid, value in request_field_values.items():
                if value is not None:
                    field_values[str(fid)] = value
        payload: dict[str, Any] = {
            "serviceDeskId": str(service_desk_id),
            "requestTypeId": str(request_type_id),
            "requestFieldValues": field_values,
        }
        if reporter_account_id:
            payload["raiseOnBehalfOf"] = reporter_account_id
        resp, err = await _request(
            "post", f"{api}/request", "create service request", headers=headers, json=payload
        )
        if err:
            return err
        req = resp.json()
        return (
            f"Service request created: {req.get('issueKey', '?')} - {summary}\n"
            f"  Status: {(req.get('currentStatus') or {}).get('status', '?')}"
        )

    async def list_jira_service_requests(
        service_desk_id: int = 0,
        request_type_id: int = 0,
        request_status: str = "OPEN_REQUESTS",
        search_term: str = "",
        count: int = 30,
    ) -> str:
        """List JSM customer requests with optional filters (status, desk, type, search)."""
        params: dict[str, Any] = {"limit": clamp(count, 30, 100)}
        if service_desk_id:
            params["serviceDeskId"] = service_desk_id
        if request_type_id:
            params["requestTypeId"] = request_type_id
        if request_status in _REQUEST_STATUS:
            params["requestStatus"] = request_status
        if search_term:
            params["searchTerm"] = search_term
        resp, err = await _request(
            "get", f"{api}/request", "list service requests", headers=headers, params=params
        )
        if err:
            return err
        requests = resp.json().get("values", [])
        if not requests:
            return "No service requests found."
        lines = [f"Service requests ({len(requests)}):"]
        for r in requests:
            status = (r.get("currentStatus") or {}).get("status", "?")
            reporter = (r.get("reporter") or {}).get("displayName", "?")
            summary = ""
            for fv in r.get("requestFieldValues", []):
                if fv.get("fieldId") == "summary":
                    summary = fv.get("value", "")
                    break
            lines.append(
                f"  {r.get('issueKey', '?')} [{status}] {summary} (reporter: {reporter})"
            )
        return "\n".join(lines)

    async def get_jira_service_request(issue_key: str) -> str:
        """Get a JSM request's details: status, reporter, field values and comments."""
        resp, err = await _request(
            "get",
            f"{api}/request/{issue_key}",
            f"get service request {issue_key}",
            headers=headers,
            params={"expand": "status,requestType,serviceDesk,comment,sla,participant"},
        )
        if err:
            return err
        req = resp.json()
        status = (req.get("currentStatus") or {}).get("status", "?")
        reporter = (req.get("reporter") or {}).get("displayName", "?")
        created = (req.get("createdDate") or {}).get("friendly", "?")
        lines = [
            f"Request: {req.get('issueKey', '?')}",
            f"  Status: {status} | Reporter: {reporter} | Created: {created}",
            f"  Service Desk: {req.get('serviceDeskId', '?')} | Type: {req.get('requestTypeId', '?')}",
        ]
        for fv in req.get("requestFieldValues", []):
            label = fv.get("label") or fv.get("fieldId", "?")
            value = fv.get("value")
            # Rich text (the request description, an ADF document) is a body,
            # not a short field value: give it the body cap.
            is_body = fv.get("fieldId") == "description" or isinstance(value, dict)
            rendered = (
                (truncate_body(body_text(value)) if is_body else truncate(body_text(value)))
                if value
                else ""
            )
            if rendered:
                lines.append(f"  {label}: {rendered}")
        comment_data = req.get("comment") or {}
        comments = comment_data.get("values", []) if isinstance(comment_data, dict) else []
        if comments:
            lines.append(f"\nComments ({len(comments)}):")
            for c in comments:
                author = (c.get("author") or {}).get("displayName", "?")
                date = (c.get("created") or {}).get("friendly", "?")
                visibility = "public" if c.get("public") else "internal"
                lines.append(
                    f"\n  --- {author} ({date}) [{visibility}] [id: {c.get('id', '?')}] ---\n  "
                    f"{comment_body(c, req.get('issueKey') or issue_key)}"
                )
        return "\n".join(lines)

    async def add_jira_service_request_comment(
        issue_key: str, body: str, is_public: bool = True
    ) -> str:
        """Add a comment to a JSM request. is_public=True is visible to the customer."""
        _, err = await _request(
            "post",
            f"{api}/request/{issue_key}/comment",
            f"add comment to request {issue_key}",
            headers=headers,
            json={"body": body, "public": is_public},
        )
        if err:
            return err
        return f"Comment added to request {issue_key} ({'public' if is_public else 'internal'})."

    async def transition_jira_service_request(
        issue_key: str, transition_id: str = "", comment: str = ""
    ) -> str:
        """List or perform customer transitions on a JSM request.

        Without transition_id, lists the available customer transitions. With
        one, performs it (optionally with a comment).
        """
        if not transition_id:
            resp, err = await _request(
                "get",
                f"{api}/request/{issue_key}/transition",
                f"list transitions for request {issue_key}",
                headers=headers,
            )
            if err:
                return err
            transitions = resp.json().get("values", [])
            if not transitions:
                return f"No customer transitions available for {issue_key}."
            lines = [f"Customer transitions for {issue_key}:"]
            for t in transitions:
                lines.append(f"  {t.get('name', '?')} (id: {t.get('id', '?')})")
            return "\n".join(lines)
        payload: dict[str, Any] = {"id": transition_id}
        if comment:
            payload["additionalComment"] = {"body": comment}
        _, err = await _request(
            "post",
            f"{api}/request/{issue_key}/transition",
            f"transition request {issue_key}",
            headers=headers,
            json=payload,
        )
        if err:
            return err
        return f"Request {issue_key} transitioned (transition {transition_id})."

    async def manage_jira_service_request(
        action: str,
        issue_key: str,
        account_ids: Any = None,
        approval_id: str = "",
        decision: str = "",
    ) -> str:
        """Manage a JSM request's participants, SLA and approvals.

        Actions: 'list_participants', 'add_participants'/'remove_participants'
        (account_ids), 'sla' (SLA status), 'list_approvals', 'answer_approval'
        (approval_id + decision: approve/decline).
        """
        action = (action or "").strip().lower()
        if not issue_key:
            return "issue_key is required."
        if action == "list_participants":
            resp, err = await _request(
                "get",
                f"{api}/request/{issue_key}/participant",
                f"list participants of {issue_key}",
                headers=headers,
            )
            if err:
                return err
            people = resp.json().get("values", [])
            if not people:
                return f"{issue_key} has no participants."
            names = [
                f"{p.get('displayName', '?')} ({p.get('accountId', '?')})" for p in people
            ]
            return f"Participants of {issue_key}: " + ", ".join(names)
        if action in ("add_participants", "remove_participants"):
            ids = _account_ids(account_ids)
            if not ids:
                return "account_ids is required."
            method = "post" if action == "add_participants" else "delete"
            _, err = await _request(
                method,
                f"{api}/request/{issue_key}/participant",
                f"{action} on {issue_key}",
                headers=headers,
                json={"accountIds": ids},
            )
            if err:
                return err
            verb = "Added" if action == "add_participants" else "Removed"
            return f"{verb} {len(ids)} participant(s) on {issue_key}."
        if action == "sla":
            resp, err = await _request(
                "get",
                f"{api}/request/{issue_key}/sla",
                f"get SLA for {issue_key}",
                headers=headers,
            )
            if err:
                return err
            slas = resp.json().get("values", [])
            if not slas:
                return f"No SLA data for {issue_key}."
            lines = [f"SLA for {issue_key}:"]
            for s in slas:
                ongoing = s.get("ongoingCycle") or {}
                breached = ongoing.get("breached")
                remaining = (ongoing.get("remainingTime") or {}).get("friendly", "")
                state = "breached" if breached else (f"{remaining} remaining" if remaining else "ok")
                lines.append(f"  {s.get('name', '?')}: {state}")
            return "\n".join(lines)
        if action == "list_approvals":
            resp, err = await _request(
                "get",
                f"{api}/request/{issue_key}/approval",
                f"list approvals for {issue_key}",
                headers=headers,
            )
            if err:
                return err
            approvals = resp.json().get("values", [])
            if not approvals:
                return f"No approvals on {issue_key}."
            lines = [f"Approvals on {issue_key}:"]
            for a in approvals:
                lines.extend(_approval_lines(a))
            return "\n".join(lines)
        if action == "answer_approval":
            if not (approval_id and decision):
                return "approval_id and decision (approve/decline) are required."
            decision_norm = decision.strip().lower()
            if decision_norm not in ("approve", "decline"):
                return "decision must be 'approve' or 'decline'."
            resp, err = await _request(
                "post",
                f"{api}/request/{issue_key}/approval/{approval_id}",
                f"answer approval {approval_id} on {issue_key}",
                headers=headers,
                json={"decision": decision_norm},
            )
            if err:
                return err
            return _answered_approval(issue_key, approval_id, decision_norm, resp)
        return (
            f"Unknown action '{action}'. Valid: list_participants, add_participants, "
            "remove_participants, sla, list_approvals, answer_approval."
        )

    async def list_jira_service_desk_queues(
        service_desk_id: int, queue_id: int = 0, count: int = 30
    ) -> str:
        """List a service desk's queues, or the issues in a specific queue (queue_id)."""
        if queue_id:
            resp, err = await _request(
                "get",
                f"{api}/servicedesk/{service_desk_id}/queue/{queue_id}/issue",
                f"list issues in queue {queue_id}",
                headers=headers,
                params={"limit": clamp(count, 30, 100)},
            )
            if err:
                return err
            issues = resp.json().get("values", [])
            if not issues:
                return f"Queue {queue_id} is empty."
            lines = [f"Issues in queue {queue_id} ({len(issues)}):"]
            for issue in issues:
                fields = issue.get("fields") or {}
                status = (fields.get("status") or {}).get("name", "?")
                lines.append(
                    f"  {issue.get('key', '?')} [{status}] {fields.get('summary', '?')}"
                )
            return "\n".join(lines)
        resp, err = await _request(
            "get",
            f"{api}/servicedesk/{service_desk_id}/queue",
            f"list queues for desk {service_desk_id}",
            headers=headers,
            params={"limit": clamp(count, 30, 100)},
        )
        if err:
            return err
        queues = resp.json().get("values", [])
        if not queues:
            return f"No queues for service desk {service_desk_id}."
        lines = [f"Queues for service desk {service_desk_id} ({len(queues)}):"]
        for q in queues:
            lines.append(
                f"  #{q.get('id', '?')} {q.get('name', '?')} "
                f"({q.get('issueCount', '?')} issues)"
            )
        lines.append("\nTip: pass queue_id to list the issues in a queue.")
        return "\n".join(lines)

    async def manage_jira_organization(
        action: str,
        organization_id: int = 0,
        name: str = "",
        account_ids: Any = None,
        count: int = 50,
    ) -> str:
        """Manage JSM organizations (customer groups).

        Actions: 'list', 'create' (name), 'delete' (organization_id),
        'list_users' (organization_id), 'add_users'/'remove_users'
        (organization_id + account_ids).
        """
        action = (action or "").strip().lower()
        if action == "list":
            resp, err = await _request(
                "get",
                f"{api}/organization",
                "list organizations",
                headers=headers,
                params={"limit": clamp(count, 50)},
            )
            if err:
                return err
            orgs = resp.json().get("values", [])
            if not orgs:
                return "No organizations found."
            lines = [f"Organizations ({len(orgs)}):"]
            for o in orgs:
                lines.append(f"  #{o.get('id', '?')} {o.get('name', '?')}")
            return "\n".join(lines)
        if action == "create":
            if not name:
                return "name is required for 'create'."
            resp, err = await _request(
                "post", f"{api}/organization", "create organization", headers=headers, json={"name": name}
            )
            if err:
                return err
            o = resp.json()
            return f"Created organization '{o.get('name')}' (id: {o.get('id')})."
        if action == "delete":
            if not organization_id:
                return "organization_id is required for 'delete'."
            _, err = await _request(
                "delete",
                f"{api}/organization/{organization_id}",
                f"delete organization {organization_id}",
                headers=headers,
            )
            if err:
                return err
            return f"Organization {organization_id} deleted."
        if action == "list_users":
            if not organization_id:
                return "organization_id is required for 'list_users'."
            resp, err = await _request(
                "get",
                f"{api}/organization/{organization_id}/user",
                f"list users in organization {organization_id}",
                headers=headers,
                params={"limit": clamp(count, 50)},
            )
            if err:
                return err
            users = resp.json().get("values", [])
            if not users:
                return f"Organization {organization_id} has no users."
            names = [
                f"{u.get('displayName', '?')} ({u.get('accountId', '?')})" for u in users
            ]
            return f"Users in organization {organization_id}: " + ", ".join(names)
        if action in ("add_users", "remove_users"):
            if not organization_id:
                return "organization_id is required."
            ids = _account_ids(account_ids)
            if not ids:
                return "account_ids is required."
            method = "post" if action == "add_users" else "delete"
            _, err = await _request(
                method,
                f"{api}/organization/{organization_id}/user",
                f"{action} on organization {organization_id}",
                headers=headers,
                json={"accountIds": ids},
            )
            if err:
                return err
            verb = "Added" if action == "add_users" else "Removed"
            return f"{verb} {len(ids)} user(s) on organization {organization_id}."
        return (
            f"Unknown action '{action}'. Valid: list, create, delete, list_users, "
            "add_users, remove_users."
        )

    async def list_jira_kb_articles(
        service_desk_id: int, query: str = "", count: int = 20
    ) -> str:
        """Search the knowledge base articles linked to a service desk."""
        params: dict[str, Any] = {"limit": clamp(count, 20, 100)}
        if query:
            params["query"] = query
        resp, err = await _request(
            "get",
            f"{api}/servicedesk/{service_desk_id}/knowledgebase/article",
            f"list KB articles for desk {service_desk_id}",
            headers=headers,
            params=params,
        )
        if err:
            return err
        articles = resp.json().get("values", [])
        if not articles:
            return f"No knowledge base articles for service desk {service_desk_id}."
        lines = [f"KB articles for service desk {service_desk_id} ({len(articles)}):"]
        for a in articles:
            lines.append(f"  {a.get('title', '?')}")
            excerpt = truncate(a.get("excerpt") or "", 160)
            if excerpt:
                lines.append(f"    {excerpt}")
        return "\n".join(lines)

    executor.register("list_jira_service_desks", list_jira_service_desks)
    executor.register("list_jira_request_types", list_jira_request_types)
    executor.register("get_jira_request_type_fields", get_jira_request_type_fields)
    executor.register("create_jira_service_request", create_jira_service_request)
    executor.register("list_jira_service_requests", list_jira_service_requests)
    executor.register("get_jira_service_request", get_jira_service_request)
    executor.register(
        "add_jira_service_request_comment", add_jira_service_request_comment
    )
    executor.register(
        "transition_jira_service_request", transition_jira_service_request
    )
    executor.register("manage_jira_service_request", manage_jira_service_request)
    executor.register("list_jira_service_desk_queues", list_jira_service_desk_queues)
    executor.register("manage_jira_organization", manage_jira_organization)
    executor.register("list_jira_kb_articles", list_jira_kb_articles)

    return [
        ToolDefinition(
            name="list_jira_service_desks",
            description="List Jira Service Management service desks (id, project name, project key).",
            input_schema={
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Max desks (max 100)", "default": 50},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_jira_request_types",
            description="List the request types for a JSM service desk. Use list_jira_service_desks for desk ids.",
            input_schema={
                "type": "object",
                "properties": {
                    "service_desk_id": {"type": "integer", "description": "Service desk id"},
                    "count": {"type": "integer", "description": "Max types (max 100)", "default": 50},
                },
                "required": ["service_desk_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_jira_request_type_fields",
            description=(
                "Discover the fields (incl. custom + required + valid values) for "
                "creating a JSM request of a given request type - the JSM analogue of "
                "get_jira_create_fields. Use the returned field ids in "
                "create_jira_service_request 'request_field_values'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "service_desk_id": {"type": "integer", "description": "Service desk id"},
                    "request_type_id": {"type": "integer", "description": "Request type id"},
                },
                "required": ["service_desk_id", "request_type_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="create_jira_service_request",
            description=(
                "Create a customer request in JSM. Use list_jira_service_desks + "
                "list_jira_request_types for ids, and get_jira_request_type_fields to "
                "discover required + custom fields. Pass extra/custom fields via "
                "'request_field_values' ({fieldId: value}). Optionally raise on behalf "
                "of another customer."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "service_desk_id": {"type": "integer", "description": "Service desk id"},
                    "request_type_id": {"type": "integer", "description": "Request type id"},
                    "summary": {"type": "string", "description": "Request summary"},
                    "description": {"type": "string", "description": "Request description", "default": ""},
                    "reporter_account_id": {"type": "string", "description": "Raise on behalf of this customer", "default": ""},
                    "request_field_values": {
                        "type": "object",
                        "description": "Map of {fieldId: value} for extra/custom request fields",
                        "additionalProperties": True,
                    },
                },
                "required": ["service_desk_id", "request_type_id", "summary"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_jira_service_requests",
            description=(
                "List JSM customer requests with optional filters: request_status "
                "(OPEN_REQUESTS/CLOSED_REQUESTS/ALL_REQUESTS), service_desk_id, "
                "request_type_id, search_term."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "service_desk_id": {"type": "integer", "description": "Filter by service desk", "default": 0},
                    "request_type_id": {"type": "integer", "description": "Filter by request type", "default": 0},
                    "request_status": {
                        "type": "string",
                        "enum": list(_REQUEST_STATUS),
                        "default": "OPEN_REQUESTS",
                    },
                    "search_term": {"type": "string", "description": "Search term", "default": ""},
                    "count": {"type": "integer", "description": "Max requests (max 100)", "default": 30},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_jira_service_request",
            description="Get a JSM request's full details: status, reporter, field values and comments.",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Request issue key (e.g. 'HELP-1')"},
                },
                "required": ["issue_key"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_jira_service_request_comment",
            description="Add a comment to a JSM request. is_public=True is visible to the customer; False is an internal note.",
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Request issue key"},
                    "body": {"type": "string", "description": "Comment text"},
                    "is_public": {"type": "boolean", "description": "Visible to customer", "default": True},
                },
                "required": ["issue_key", "body"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="transition_jira_service_request",
            description=(
                "List or perform the customer transitions on a JSM request. Call "
                "without transition_id to list available transitions; with one to "
                "perform it (optionally with a comment)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "issue_key": {"type": "string", "description": "Request issue key"},
                    "transition_id": {"type": "string", "description": "Transition id (omit to list)", "default": ""},
                    "comment": {"type": "string", "description": "Optional comment", "default": ""},
                },
                "required": ["issue_key"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="manage_jira_service_request",
            description=(
                "Manage a JSM request's participants, SLA and approvals. Actions: "
                "'list_participants', 'add_participants'/'remove_participants' "
                "(account_ids), 'sla', 'list_approvals' (each step's final decision and "
                "every approver's own decision), 'answer_approval' (approval_id + decision "
                "approve/decline; answers your decision and the step's final decision, which "
                "stays pending while approvers of other groups have not answered)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_participants",
                            "add_participants",
                            "remove_participants",
                            "sla",
                            "list_approvals",
                            "answer_approval",
                        ],
                    },
                    "issue_key": {"type": "string", "description": "Request issue key"},
                    "account_ids": {"type": "array", "items": {"type": "string"}, "description": "Account ids (participants)"},
                    "approval_id": {"type": "string", "description": "Approval id (answer_approval)", "default": ""},
                    "decision": {"type": "string", "enum": ["approve", "decline", ""], "description": "Approval decision", "default": ""},
                },
                "required": ["action", "issue_key"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_jira_service_desk_queues",
            description="List a service desk's queues (with issue counts), or the issues in a specific queue (pass queue_id).",
            input_schema={
                "type": "object",
                "properties": {
                    "service_desk_id": {"type": "integer", "description": "Service desk id"},
                    "queue_id": {"type": "integer", "description": "Queue id (omit to list queues)", "default": 0},
                    "count": {"type": "integer", "description": "Max items (max 100)", "default": 30},
                },
                "required": ["service_desk_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_jira_organization",
            description=(
                "Manage JSM organizations (customer groups). Actions: 'list', 'create' "
                "(name), 'delete' (organization_id), 'list_users' (organization_id), "
                "'add_users'/'remove_users' (organization_id + account_ids)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "create", "delete", "list_users", "add_users", "remove_users"],
                    },
                    "organization_id": {"type": "integer", "description": "Organization id", "default": 0},
                    "name": {"type": "string", "description": "Organization name (create)", "default": ""},
                    "account_ids": {"type": "array", "items": {"type": "string"}, "description": "Account ids (add/remove users)"},
                    "count": {"type": "integer", "description": "Max items (max 100)", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="list_jira_kb_articles",
            description="Search the knowledge base articles linked to a service desk (title + excerpt). Useful for self-service answers.",
            input_schema={
                "type": "object",
                "properties": {
                    "service_desk_id": {"type": "integer", "description": "Service desk id"},
                    "query": {"type": "string", "description": "Search term", "default": ""},
                    "count": {"type": "integer", "description": "Max articles (max 100)", "default": 20},
                },
                "required": ["service_desk_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
