# SPDX-License-Identifier: Apache-2.0

"""Two-way ticketing sync for CTEM cases (#917): one contract, any case management system.

A CTEM case lives in the organization's own case management system without
leaving XTM One. The system is the one the organization context declares
(``OrganizationProfile.ticketing``: integration id, project, and the sync
settings :class:`~app.services.organization_context.TicketingSync`).

- :class:`TicketingAdapter` is the contract every provider implements:
  ``find`` (by correlation), ``create``, ``update``, ``comment``,
  ``read_status``. Jira (:class:`JiraAdapter`) and ServiceNow
  (:class:`ServiceNowAdapter`) ship here on their providers' HTTP helpers; a
  new provider registers one class in :data:`ADAPTERS`.
- :func:`ensure_ticket` is idempotent: a case carries one correlation key
  (:func:`correlation_of`), written on the ticket (Jira label, ServiceNow
  ``correlation_id``). A replayed ``ctem_case.opened``, a retried run or a
  second fleet read finds the ticket again instead of creating a second one.
- :func:`map_status` turns the system's status into a case ticket status
  (``open``, ``in_progress``, ``done``, ``cancelled``), the customer's
  ``status_map`` first, then the system's own categories.
- Every write lands on ``case.tickets`` through
  :func:`app.services.ctem_case_loop.record_ticket` with its sync record
  (``linked``, ``in_sync``, ``out_of_date`` since when, ``error`` with the
  reason and the one action that fixes it).

No model call. Design: ``dev-docs/ctem-ticketing.md``.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import OPEN_STATES, CtemCase, CtemCaseEvent
from app.services import ctem_case_loop as case_loop

logger = logging.getLogger(__name__)

#: The correlation prefix written on every ticket the sync creates.
CORRELATION_PREFIX = "xtm-ctem-"
#: What the case shows when no ticketing is declared (never an error).
NOT_CONNECTED = "No ticketing connected"
#: The settings page a case links to when no ticketing is declared or a sync failed.
SETTINGS_PATH = "/settings/organization?card=ticketing"
#: Program defaults (doc 34 section 5), overridden by ``TicketingSync.priority_map``.
DEFAULT_JIRA_PRIORITY = {"P1": "Highest", "P2": "High", "P3": "Medium"}
DEFAULT_SERVICENOW_URGENCY = {"P1": "1", "P2": "2", "P3": "3"}


#: The stable code of each fix a sync failure names (``sync.fix_code``): the UI
#: says the fix in the reader's language from it (case-ticket.tsx), never from
#: the English ``fix`` sentence, which stays for agents and logs.
FIX_CODES = (
    "check_integration",
    "reconnect_jira",
    "map_jira_status",
    "choose_servicenow_table",
    "check_servicenow_credentials",
    "relink_case",
)


class TicketingError(Exception):
    """A sync step the system refused; ``fix`` names the one action that fixes it, ``fix_code`` its stable code."""

    def __init__(
        self,
        message: str,
        fix: str = "check the ticketing integration in Settings",
        fix_code: str = "check_integration",
    ):
        super().__init__(message)
        self.fix = fix
        self.fix_code = fix_code if fix_code in FIX_CODES else "check_integration"


@dataclass(frozen=True)
class TicketDraft:
    """What a ticket carries, built from the case and the settings (:func:`draft_of`)."""

    title: str
    description: str
    correlation: str
    priority: str = ""
    due_date: str = ""
    labels: tuple[str, ...] = ()
    record_type: str = ""
    link: str = ""


@dataclass(frozen=True)
class RemoteTicket:
    """A ticket as the system reports it."""

    key: str
    url: str
    status: str
    #: The system's own status name, kept for the case display.
    external_status: str = ""


class TicketingAdapter(Protocol):
    """The contract of a ticketing provider (``dev-docs/ctem-ticketing.md`` section 2)."""

    provider: ClassVar[str]

    async def find(self, correlation: str) -> RemoteTicket | None: ...

    async def create(self, draft: TicketDraft) -> RemoteTicket: ...

    async def update(self, key: str, draft: TicketDraft, *, status: str | None = None) -> RemoteTicket: ...

    async def comment(self, key: str, text: str) -> None: ...

    async def read_status(self, key: str) -> RemoteTicket: ...


# --- Pure rules ---------------------------------------------------------------


def correlation_of(case: CtemCase) -> str:
    """The case's stable correlation key, the same on every run and replay."""
    return f"{CORRELATION_PREFIX}{uuid.UUID(str(case.id)).hex[:16]}"


def should_create(case: CtemCase, sync: Any) -> bool:
    """Whether the sync opens a ticket by itself for *case* once it passes Mobilize."""
    create_on = getattr(sync, "create_on", "mobilize_p1_p2")
    if create_on == "manual":
        return False
    wanted = ("P1",) if create_on == "mobilize_p1" else ("P1", "P2")
    return (case.severity or "") in wanted


#: Status categories each system reports, in order: the first name that matches wins.
_CATEGORY_STATUS: tuple[tuple[str, str], ...] = (
    ("cancel", "cancelled"),
    ("won't do", "cancelled"),
    ("reject", "cancelled"),
    ("done", "done"),
    ("resolved", "done"),
    ("closed", "done"),
    ("complete", "done"),
    ("progress", "in_progress"),
    ("hold", "in_progress"),
    ("review", "in_progress"),
)
#: Jira status category keys (``statusCategory.key``).
_JIRA_CATEGORY = {"new": "open", "indeterminate": "in_progress", "done": "done"}
#: ServiceNow ``state`` numbers read, per table: each table numbers its states its own way
#: (a change_request closes at 3 and cancels at 4, a problem resolves at 106).
_SERVICENOW_STATES: dict[str, dict[str, str]] = {
    "incident": {"1": "open", "2": "in_progress", "3": "in_progress", "6": "done", "7": "done", "8": "cancelled"},
    "change_request": {
        "-5": "open",
        "-4": "in_progress",
        "-3": "in_progress",
        "-2": "in_progress",
        "-1": "in_progress",
        "0": "in_progress",
        "3": "done",
        "4": "cancelled",
    },
    "problem": {
        "101": "open",
        "102": "in_progress",
        "103": "in_progress",
        "104": "in_progress",
        "106": "done",
        "107": "done",
    },
}
#: The ``state`` written per case ticket status, per table.
_SERVICENOW_STATE_OF: dict[str, dict[str, str]] = {
    "incident": {"open": "1", "in_progress": "2", "done": "6", "cancelled": "8"},
    "change_request": {"open": "-5", "in_progress": "-1", "done": "3", "cancelled": "4"},
    "problem": {"open": "101", "in_progress": "104", "done": "106", "cancelled": "107"},
}
#: The ServiceNow tables the sync knows; another table is refused at settings save.
SERVICENOW_TABLES: tuple[str, ...] = tuple(_SERVICENOW_STATES)


def map_status(name: str, *, status_map: dict[str, str] | None = None, category: str = "", table: str = "") -> str:
    """The case ticket status of a system status *name* (customer map, then category, then the name).

    *table* is the ServiceNow table whose state numbers *name* is read with (``incident`` when empty).
    """
    raw = (name or "").strip()
    for key, value in (status_map or {}).items():
        if key.strip().lower() == raw.lower() and value in case_loop.TICKET_STATUSES:
            return value
    if category in _JIRA_CATEGORY:
        return _JIRA_CATEGORY[category]
    states = _SERVICENOW_STATES.get(table or "incident", {})
    if raw in states:
        return states[raw]
    lowered = raw.lower()
    for needle, status in _CATEGORY_STATUS:
        if needle in lowered:
            return status
    return "open"


def draft_of(case: CtemCase, sync: Any, *, base_url: str = "", provider: str = "jira") -> TicketDraft:
    """The ticket of *case*: mapped title, description, priority, due date, labels and the link back."""
    link = f"{base_url.rstrip('/')}/xtm-one/ctem/cases/{case.id}" if base_url else ""
    severity = case.severity or ""
    custom = dict(getattr(sync, "priority_map", {}) or {})
    defaults = DEFAULT_SERVICENOW_URGENCY if provider == "servicenow" else DEFAULT_JIRA_PRIORITY
    priority = custom.get(severity) or defaults.get(severity, "")
    due = ""
    if getattr(sync, "due_date_from_sla", True) and case.sla_due_at is not None:
        due = case.sla_due_at.date().isoformat()
    lines = [
        f"CTEM case: {case.title}",
        f"Fix: {case.remediation_kind}" + (f" to {case.fixed_version}" if case.fixed_version else ""),
        f"Severity: {severity or 'not set'}",
        f"Trigger: {case.trigger_path}",
    ]
    if due:
        lines.append(f"Due (SLA): {due}")
    if link:
        lines.append(f"Case and loop trace: {link}")
    lines.append(f"Correlation: {correlation_of(case)} (keep it: XTM One finds this ticket by it)")
    labels = tuple(dict.fromkeys([*(getattr(sync, "labels", []) or []), "xtm-ctem"]))
    return TicketDraft(
        title=f"[{severity}] {case.title}"[:250] if severity else case.title[:250],
        description="\n".join(lines),
        correlation=correlation_of(case),
        priority=priority,
        due_date=due,
        labels=labels,
        record_type=getattr(sync, "record_type", "") or "",
        link=link,
    )


def target_status(case: CtemCase) -> str | None:
    """The ticket status a case state asks for, or ``None`` when the ticket leads."""
    if case.state == "resolved":
        return "done"
    if case.state == "accepted_risk":
        return "cancelled"
    return None


def _when(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def needs_reopen(case: CtemCase, ticket: dict[str, Any]) -> bool:
    """Whether a closed ticket must reopen: the case is open again after the sync closed it,
    or a re-test failed after the ticket reached done.

    A ticket done while its case waits for the re-test is not reopened: that is
    the normal path (ticket done, re-test asked, the case closes on its result).
    """
    if case.state not in OPEN_STATES or ticket.get("status") not in ("done", "cancelled"):
        return False
    if (ticket.get("sync") or {}).get("closed_by_case"):
        return True
    closed_at = _when(ticket.get("at"))
    for validation in reversed(list(case.validations or [])):
        if not isinstance(validation, dict) or validation.get("result") != "failed":
            continue
        failed_at = _when(validation.get("at"))
        return failed_at is not None and (closed_at is None or failed_at > closed_at)
    return False


def managed_ticket(case: CtemCase) -> dict[str, Any] | None:
    """The ticket the sync manages on *case* (the newest one carrying a sync record)."""
    for ticket in reversed(list(case.tickets or [])):
        if isinstance(ticket, dict) and ticket.get("sync"):
            return ticket
    return None


def _sync_record(state: str, now: datetime, adapter: Any, correlation: str, **extra: Any) -> dict[str, Any]:
    return {
        "state": state,
        "since": now.isoformat(),
        "provider": getattr(adapter, "provider", ""),
        "correlation": correlation,
        **extra,
    }


# --- Sync steps (the caller commits) ------------------------------------------


@dataclass
class SyncOutcome:
    """What one sync step did."""

    created: bool = False
    updated: bool = False
    retest_requested: bool = False
    error: str = ""
    notes: list[str] = field(default_factory=list)
    #: The step :func:`sync_case` took: ``created``, ``pushed``, ``pulled`` or ``none``.
    action: str = "none"


async def ensure_ticket(
    db: AsyncSession,
    case: CtemCase,
    adapter: TicketingAdapter,
    sync: Any,
    *,
    integration: str,
    base_url: str = "",
    now: datetime | None = None,
) -> SyncOutcome:
    """Give *case* its ticket, once: the managed ticket, else one found by correlation, else a new one."""
    now = now or datetime.now(UTC)
    outcome = SyncOutcome()
    if managed_ticket(case) is not None:
        outcome.notes.append("already linked")
        return outcome
    draft = draft_of(case, sync, base_url=base_url, provider=adapter.provider)
    try:
        remote = await adapter.find(draft.correlation)
        if remote is None:
            remote = await adapter.create(draft)
            outcome.created = True
    except TicketingError as exc:
        outcome.error = str(exc)
        logger.warning("CTEM ticket for case %s not created: %s", case.id, exc)
        return outcome
    case_loop.record_ticket(
        db,
        case,
        integration=integration,
        key=remote.key,
        url=remote.url or None,
        status=remote.status,
        now=now,
        sync=_sync_record("in_sync", now, adapter, draft.correlation, external_status=remote.external_status),
    )
    return outcome


async def push_case_state(
    db: AsyncSession,
    case: CtemCase,
    adapter: TicketingAdapter,
    sync: Any,
    *,
    base_url: str = "",
    now: datetime | None = None,
) -> SyncOutcome:
    """Carry a case change to its ticket: fields, and the status the case state asks for."""
    now = now or datetime.now(UTC)
    outcome = SyncOutcome()
    ticket = managed_ticket(case)
    if ticket is None:
        return outcome
    wanted = target_status(case)
    reopen = needs_reopen(case, ticket)
    correlation = (ticket.get("sync") or {}).get("correlation") or correlation_of(case)
    draft = draft_of(case, sync, base_url=base_url, provider=adapter.provider)
    try:
        if reopen and getattr(sync, "on_reopen", "reopen") == "linked":
            linked = await adapter.create(draft)
            await adapter.comment(linked.key, f"Reopened from {ticket.get('key')}: the re-test failed.")
            case_loop.record_ticket(
                db,
                case,
                integration=str(ticket.get("integration")),
                key=linked.key,
                url=linked.url or None,
                status=linked.status,
                now=now,
                sync=_sync_record("in_sync", now, adapter, correlation, external_status=linked.external_status),
            )
            outcome.created = True
            return outcome
        remote = await adapter.update(str(ticket["key"]), draft, status="open" if reopen else wanted)
    except TicketingError as exc:
        _mark(db, case, ticket, adapter, "error", now, error=str(exc), fix=exc.fix, fix_code=exc.fix_code)
        outcome.error = str(exc)
        return outcome
    case_loop.record_ticket(
        db,
        case,
        integration=str(ticket.get("integration")),
        key=str(ticket["key"]),
        url=remote.url or ticket.get("url"),
        status=remote.status,
        now=now,
        sync=_sync_record(
            "in_sync",
            now,
            adapter,
            correlation,
            external_status=remote.external_status,
            closed_by_case=bool(wanted) and not reopen,
        ),
    )
    outcome.updated = True
    return outcome


async def pull_ticket_status(
    db: AsyncSession,
    case: CtemCase,
    adapter: TicketingAdapter,
    sync: Any,
    *,
    remote: RemoteTicket | None = None,
    now: datetime | None = None,
) -> SyncOutcome:
    """Read the ticket's status (or take the one an inbound event carried) onto the case.

    A ticket that reaches ``done`` while the case is still worked asks for the
    re-test (``retest_requested``) when ``done_starts_retest``: the case closes
    on the re-test, never on the ticket.
    """
    now = now or datetime.now(UTC)
    outcome = SyncOutcome()
    ticket = managed_ticket(case)
    if ticket is None:
        return outcome
    correlation = (ticket.get("sync") or {}).get("correlation") or correlation_of(case)
    try:
        remote = remote or await adapter.read_status(str(ticket["key"]))
    except TicketingError as exc:
        _mark(
            db,
            case,
            ticket,
            adapter,
            "out_of_date",
            now,
            error=str(exc),
            fix=exc.fix,
            fix_code=exc.fix_code,
            keep_since=True,
        )
        outcome.error = str(exc)
        return outcome
    before = ticket.get("status")
    case_loop.record_ticket(
        db,
        case,
        integration=str(ticket.get("integration")),
        key=str(ticket["key"]),
        url=remote.url or ticket.get("url"),
        status=remote.status,
        now=now,
        sync=_sync_record(
            "in_sync",
            now,
            adapter,
            correlation,
            external_status=remote.external_status,
            closed_by_case=bool((ticket.get("sync") or {}).get("closed_by_case"))
            and remote.status in ("done", "cancelled"),
        ),
    )
    outcome.updated = before != remote.status
    if (
        outcome.updated
        and remote.status == "done"
        and case.state in OPEN_STATES
        and getattr(sync, "done_starts_retest", True)
    ):
        outcome.retest_requested = True
    return outcome


def _mark(
    db: AsyncSession,
    case: CtemCase,
    ticket: dict[str, Any],
    adapter: Any,
    state: str,
    now: datetime,
    *,
    error: str = "",
    fix: str = "",
    fix_code: str = "check_integration",
    keep_since: bool = False,
) -> None:
    """Write a sync state that is not ``in_sync`` (the since of a running out-of-date is kept)."""
    previous = dict(ticket.get("sync") or {})
    since = previous.get("since") if keep_since and previous.get("state") == state else now.isoformat()
    record = {
        **previous,
        "state": state,
        "since": since or now.isoformat(),
        "provider": getattr(adapter, "provider", previous.get("provider", "")),
        "error": error[:300],
        "fix": fix[:200],
        "fix_code": fix_code,
    }
    case_loop.record_ticket(
        db,
        case,
        integration=str(ticket.get("integration")),
        key=str(ticket["key"]),
        url=ticket.get("url"),
        status=str(ticket.get("status") or "open"),
        now=now,
        sync=record,
    )


# --- Adapters ----------------------------------------------------------------


class JiraAdapter:
    """Jira Cloud on the provider's REST v3 helpers (``integrations/providers/jira/_common.py``)."""

    provider: ClassVar[str] = "jira"

    def __init__(self, base: str, headers: dict[str, str], project: str, *, status_map: dict[str, str] | None = None):
        self.base = base
        self.headers = headers
        self.project = project
        self.status_map = status_map or {}

    def _browse(self, key: str) -> str:
        return f"{self.base}/browse/{key}" if key else ""

    async def _call(self, method: str, path: str, action: str, **kwargs: Any) -> Any:
        from app.integrations.providers.jira._common import api3, jira_request

        resp, err = await jira_request(method, f"{api3(self.base)}/{path}", action, headers=self.headers, **kwargs)
        if err or resp is None:
            raise TicketingError(
                err or f"Jira did not answer to {action}",
                fix="reconnect the Jira integration",
                fix_code="reconnect_jira",
            )
        return resp.json() if resp.content else {}

    def _remote(self, issue: dict[str, Any]) -> RemoteTicket:
        status = ((issue.get("fields") or {}).get("status")) or {}
        name = str(status.get("name") or "")
        category = str((status.get("statusCategory") or {}).get("key") or "")
        key = str(issue.get("key") or "")
        return RemoteTicket(
            key=key,
            url=self._browse(key),
            status=map_status(name, status_map=self.status_map, category=category),
            external_status=name,
        )

    async def find(self, correlation: str) -> RemoteTicket | None:
        body = {"jql": f'labels = "{correlation}"', "fields": ["status"], "maxResults": 1}
        data = await self._call("post", "search/jql", "find the CTEM ticket", json=body)
        issues = data.get("issues") or []
        return self._remote(issues[0]) if issues else None

    async def create(self, draft: TicketDraft) -> RemoteTicket:
        from app.integrations.providers.jira._common import text_to_adf

        fields: dict[str, Any] = {
            "project": {"key": self.project},
            "summary": draft.title,
            "issuetype": {"name": draft.record_type or "Task"},
            "description": text_to_adf(draft.description),
            "labels": [*draft.labels, draft.correlation],
        }
        if draft.priority:
            fields["priority"] = {"name": draft.priority}
        if draft.due_date:
            fields["duedate"] = draft.due_date
        created = await self._call(
            "post", "issue", f"create the CTEM ticket in {self.project}", json={"fields": fields}
        )
        key = str(created.get("key") or "")
        if not key:
            raise TicketingError("Jira created no issue key")
        return RemoteTicket(key=key, url=self._browse(key), status="open", external_status="")

    async def update(self, key: str, draft: TicketDraft, *, status: str | None = None) -> RemoteTicket:
        fields: dict[str, Any] = {"summary": draft.title}
        if draft.priority:
            fields["priority"] = {"name": draft.priority}
        if draft.due_date:
            fields["duedate"] = draft.due_date
        await self._call("put", f"issue/{key}", f"update {key}", json={"fields": fields})
        if status:
            await self._transition(key, status)
        return await self.read_status(key)

    async def _transition(self, key: str, status: str) -> None:
        data = await self._call("get", f"issue/{key}/transitions", f"read the transitions of {key}")
        for transition in data.get("transitions") or []:
            to = transition.get("to") or {}
            mapped = map_status(
                str(to.get("name") or ""),
                status_map=self.status_map,
                category=str((to.get("statusCategory") or {}).get("key") or ""),
            )
            if mapped == status:
                await self._call(
                    "post", f"issue/{key}/transitions", f"move {key}", json={"transition": {"id": transition["id"]}}
                )
                return
        raise TicketingError(
            f"no Jira transition of {key} leads to {status}",
            fix="map a Jira status to this case status in the ticketing sync settings",
            fix_code="map_jira_status",
        )

    async def comment(self, key: str, text: str) -> None:
        from app.integrations.providers.jira._common import text_to_adf

        await self._call("post", f"issue/{key}/comment", f"comment on {key}", json={"body": text_to_adf(text)})

    async def read_status(self, key: str) -> RemoteTicket:
        data = await self._call("get", f"issue/{key}", f"read {key}", params={"fields": "status"})
        return self._remote(data)


class ServiceNowAdapter:
    """ServiceNow on the Table API helpers (``integrations/providers/servicenow/_common.py``)."""

    provider: ClassVar[str] = "servicenow"

    def __init__(
        self,
        base: str,
        headers: dict[str, str],
        table: str = "incident",
        *,
        group: str = "",
        status_map: dict[str, str] | None = None,
    ):
        self.base = base
        self.headers = headers
        self.table = table or "incident"
        if self.table not in SERVICENOW_TABLES:
            raise TicketingError(
                f"the ServiceNow table {self.table!r} has no ticketing sync",
                fix=f"choose one of {', '.join(SERVICENOW_TABLES)} in the ticketing settings",
                fix_code="choose_servicenow_table",
            )
        self.group = group
        self.status_map = status_map or {}

    async def _call(self, method: str, url: str, action: str, **kwargs: Any) -> Any:
        from app.integrations.providers.servicenow._common import result_of, sn_request

        resp, err = await sn_request(method, url, action, headers=self.headers, **kwargs)
        if err or resp is None:
            raise TicketingError(
                err or f"ServiceNow did not answer to {action}",
                fix="check the ServiceNow credentials",
                fix_code="check_servicenow_credentials",
            )
        return result_of(resp)

    def _remote(self, rec: dict[str, Any]) -> RemoteTicket:
        from app.integrations.providers.servicenow._common import record_url

        state = str(rec.get("state") or "")
        sys_id = str(rec.get("sys_id") or "")
        return RemoteTicket(
            key=str(rec.get("number") or sys_id),
            url=record_url(self.base, self.table, sys_id),
            status=map_status(state, status_map=self.status_map, table=self.table),
            external_status=state,
        )

    def _url(self, sys_id: str = "") -> str:
        from app.integrations.providers.servicenow._common import table_url

        return table_url(self.base, self.table, sys_id)

    async def _row(self, query: str, action: str) -> dict[str, Any] | None:
        rows = await self._call(
            "get",
            self._url(),
            action,
            params={"sysparm_query": query, "sysparm_limit": 1, "sysparm_fields": "sys_id,number,state"},
        )
        return rows[0] if isinstance(rows, list) and rows else None

    async def find(self, correlation: str) -> RemoteTicket | None:
        row = await self._row(f"correlation_id={correlation}", "find the CTEM ticket")
        return self._remote(row) if row else None

    def _body(self, draft: TicketDraft) -> dict[str, Any]:
        body: dict[str, Any] = {
            "short_description": draft.title[:160],
            "description": draft.description,
            "correlation_id": draft.correlation,
            "correlation_display": "XTM One CTEM",
        }
        if draft.priority:
            body["urgency"] = draft.priority
        if draft.due_date:
            body["due_date"] = f"{draft.due_date} 23:59:59"
        if self.group:
            body["assignment_group"] = self.group
        if draft.labels:
            body["category"] = draft.labels[0]
        return body

    async def create(self, draft: TicketDraft) -> RemoteTicket:
        rec = await self._call("post", self._url(), "create the CTEM ticket", json=self._body(draft))
        if not isinstance(rec, dict) or not rec.get("sys_id"):
            raise TicketingError("ServiceNow created no record")
        return self._remote(rec)

    async def _sys_id(self, key: str) -> str:
        row = await self._row(f"number={key}", f"resolve {key}")
        if not row:
            raise TicketingError(
                f"ServiceNow has no {self.table} {key}",
                fix="relink the case to an existing record",
                fix_code="relink_case",
            )
        return str(row["sys_id"])

    async def update(self, key: str, draft: TicketDraft, *, status: str | None = None) -> RemoteTicket:
        body = self._body(draft)
        body.pop("correlation_id", None)
        if status:
            body["state"] = _SERVICENOW_STATE_OF[self.table][status]
        rec = await self._call("patch", self._url(await self._sys_id(key)), f"update {key}", json=body)
        return self._remote(rec if isinstance(rec, dict) else {"number": key})

    async def comment(self, key: str, text: str) -> None:
        await self._call("patch", self._url(await self._sys_id(key)), f"note on {key}", json={"work_notes": text})

    async def read_status(self, key: str) -> RemoteTicket:
        row = await self._row(f"number={key}", f"read {key}")
        if not row:
            raise TicketingError(
                f"ServiceNow has no {self.table} {key}",
                fix="relink the case to an existing record",
                fix_code="relink_case",
            )
        return self._remote(row)


# --- The scheduled pass (services/autonomous.py, after the case sweep) --------

#: Raised when a ticket reaches done on a case still worked: the fleets start the re-test.
CASE_RETEST_REQUESTED = "ctem_case.retest_requested"
#: Cases one pass syncs at most.
SYNC_BATCH = 200


#: The cadence of the ticketing pass when ``CTEM_TICKETING_SYNC_SECONDS`` is not set.
DEFAULT_SYNC_SECONDS = 300


def sync_seconds() -> int:
    """``CTEM_TICKETING_SYNC_SECONDS`` (at least 60), else every five minutes."""
    import os

    try:
        value = int(os.environ.get("CTEM_TICKETING_SYNC_SECONDS") or DEFAULT_SYNC_SECONDS)
    except ValueError:
        value = DEFAULT_SYNC_SECONDS
    return max(60, value)


async def sync_due() -> bool:
    """Take the sync slot for this cadence window; True when this tick runs it (or Redis is unavailable)."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        return bool(await client.set("ctem:ticketing:sync", "1", nx=True, ex=sync_seconds()))
    except Exception:  # no Redis: every tick syncs; the sync is idempotent
        return True


async def sync_cases(
    db: AsyncSession,
    *,
    events: case_loop.PendingEvents | None = None,
    base_url: str = "",
    now: datetime | None = None,
) -> dict[str, int]:
    """Sync every case of the primary organization with its ticketing target. The caller commits.

    Pulls the status of every managed ticket (ServiceNow has no push: this is
    its inbound) and pushes the case state to the ticket when they disagree.
    It never opens a ticket: that is the Mobilization role's gated call
    (``sync_one`` with ``create``, gate ``ctem.mobilize``), so no case gets a
    ticket without its Mobilize decision. No ticketing declared: nothing to
    do, counted as ``not_connected``.
    """
    from sqlalchemy import select

    from app.services import organization_context as oc

    now = now or datetime.now(UTC)
    counts = {"created": 0, "pulled": 0, "pushed": 0, "retest": 0, "errors": 0, "not_connected": 0}
    row = await oc.current_version(db)
    if row is None:
        counts["not_connected"] = 1
        return counts
    profile = oc.parse_profile(row.profile)
    adapter = await adapter_for(db, profile)
    if isinstance(adapter, str):
        counts["not_connected"] = 1
        return counts
    sync = profile.ticketing.sync
    integration = profile.ticketing.integration
    cases = (
        (
            await db.execute(
                select(CtemCase)
                .where(CtemCase.organization_id == row.organization_id)
                .order_by(CtemCase.updated_at.desc())
                .limit(SYNC_BATCH)
            )
        )
        .scalars()
        .all()
    )
    for case in cases:
        outcome = await sync_case(
            db, case, adapter, sync, integration=integration, base_url=base_url, events=events, now=now
        )
        if outcome.action == "created":
            counts["created"] += int(outcome.created)
        elif outcome.action == "pushed":
            counts["pushed"] += int(outcome.updated or outcome.created)
        elif outcome.action == "pulled":
            counts["pulled"] += 1
        counts["errors"] += int(bool(outcome.error))
        counts["retest"] += int(outcome.retest_requested)
    return counts


async def sync_case(
    db: AsyncSession,
    case: CtemCase,
    adapter: TicketingAdapter,
    sync: Any,
    *,
    integration: str,
    base_url: str = "",
    events: case_loop.PendingEvents | None = None,
    now: datetime | None = None,
    create: bool = False,
) -> SyncOutcome:
    """One sync step on *case*, the one the scheduled pass and the Mobilization tool share. The caller commits.

    No managed ticket: create it only on *create*, the Mobilization role's
    call that the gate ``ctem.mobilize`` holds (an open case only); otherwise
    say whether the settings ask one for this case. A case state the ticket
    must follow: push it. Otherwise pull the ticket's status, and a ticket
    that reached done queues the re-test.
    """
    now = now or datetime.now(UTC)
    ticket = managed_ticket(case)
    if ticket is None:
        if not (create and case.state in OPEN_STATES):
            due = case.state in OPEN_STATES and should_create(case, sync)
            note = "the settings ask a ticket once the case passes Mobilize" if due else "no ticket asked for this case"
            return SyncOutcome(notes=[note])
        outcome = await ensure_ticket(db, case, adapter, sync, integration=integration, base_url=base_url, now=now)
        outcome.action = "created"
        _frame_if_moved(events, case, outcome, now)
        return outcome
    wanted = target_status(case)
    if (wanted and ticket.get("status") != wanted) or needs_reopen(case, ticket):
        outcome = await push_case_state(db, case, adapter, sync, base_url=base_url, now=now)
        outcome.action = "pushed"
        _frame_if_moved(events, case, outcome, now)
        return outcome
    outcome = await pull_ticket_status(db, case, adapter, sync, now=now)
    outcome.action = "pulled"
    if outcome.retest_requested:
        _raise_retest(events, case, now, db=db)
    _frame_if_moved(events, case, outcome, now)
    return outcome


def _frame_if_moved(
    events: case_loop.PendingEvents | None, case: CtemCase, outcome: SyncOutcome, now: datetime
) -> None:
    """Raise the live ``ctem_case_update`` frame when a sync step changed the case's ticket (CTEM-19-UI, #1032)."""
    if events is not None and (outcome.created or outcome.updated or outcome.error or outcome.retest_requested):
        events.frame(case, "ticket", now)


async def sync_one(
    db: AsyncSession,
    case: CtemCase,
    *,
    create: bool = False,
    events: case_loop.PendingEvents | None = None,
    base_url: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    """Sync one case now with the declared ticketing target, for the Mobilization role's tool. The caller commits.

    Answers what was done and the case's ticket with its sync state. No
    ticketing declared: ``connected`` false with the reason and the settings
    link, never an error.
    """
    from app.services import organization_context as oc

    row = await oc.current_version(db)
    if row is None or row.organization_id != case.organization_id:
        return {"connected": False, "reason": NOT_CONNECTED, "settings": SETTINGS_PATH}
    profile = oc.parse_profile(row.profile)
    adapter = await adapter_for(db, profile)
    if isinstance(adapter, str):
        return {"connected": False, "reason": adapter, "settings": SETTINGS_PATH}
    outcome = await sync_case(
        db,
        case,
        adapter,
        profile.ticketing.sync,
        integration=profile.ticketing.integration,
        base_url=base_url,
        events=events,
        now=now,
        create=create,
    )
    return {
        "connected": True,
        "action": outcome.action,
        "created": outcome.created,
        "retest_requested": outcome.retest_requested,
        "error": outcome.error,
        "notes": outcome.notes,
        "ticket": managed_ticket(case),
    }


def _raise_retest(
    events: case_loop.PendingEvents | None, case: CtemCase, now: datetime, *, db: AsyncSession | None = None
) -> None:
    """Queue ``ctem_case.retest_requested`` once per ticket change (dedupe key: case and ticket time).

    With *db*, the case history also records it (``retest_requested``, CTEM-19-UI #1032), so the
    case page says the re-test started because the ticket reached done.
    """
    ticket = managed_ticket(case) or {}
    if db is not None:
        db.add(
            CtemCaseEvent(
                case_id=case.id,
                actor="system",
                kind="retest_requested",
                at=now,
                evidence=f"{ticket.get('integration')}:{ticket.get('key')}",
            )
        )
    if events is None:
        return
    events.add(
        CASE_RETEST_REQUESTED,
        {**case_loop.case_data(case), "ticket_integration": ticket.get("integration"), "ticket_key": ticket.get("key")},
        f"{CASE_RETEST_REQUESTED}:{case.id}:{ticket.get('at', now.isoformat())}",
    )


# --- Inbound status events (Jira pushes; ServiceNow is polled by sync_cases) ----


@dataclass(frozen=True)
class _InboundSource:
    """Stands for the adapter when the status arrived on an event: only ``provider`` is read."""

    provider: str


async def apply_inbound_status(
    db: AsyncSession,
    *,
    integration_id: str,
    key: str,
    status_name: str,
    category: str = "",
    provider: str = "jira",
    events: case_loop.PendingEvents | None = None,
    now: datetime | None = None,
) -> SyncOutcome | None:
    """Apply a ticket status an inbound event carried to the case that manages that ticket.

    Only the declared ticketing integration counts; a ticket no case manages
    answers ``None``. The status goes through the customer's ``status_map``
    like a pull, so the case moves the moment the ticket does, and ``done``
    asks for the re-test once. The caller commits and publishes *events*.
    """
    from sqlalchemy import select

    from app.services import organization_context as oc

    now = now or datetime.now(UTC)
    key = (key or "").strip()
    if not key or not integration_id:
        return None
    row = await oc.current_version(db)
    if row is None:
        return None
    profile = oc.parse_profile(row.profile)
    if (profile.ticketing.integration or "").strip() != str(integration_id):
        return None
    sync = profile.ticketing.sync
    cases = (
        (
            await db.execute(
                select(CtemCase)
                .where(CtemCase.organization_id == row.organization_id)
                .order_by(CtemCase.updated_at.desc())
                .limit(SYNC_BATCH)
            )
        )
        .scalars()
        .all()
    )
    for case in cases:
        ticket = managed_ticket(case)
        if ticket is None or str(ticket.get("key")) != key or str(ticket.get("integration")) != str(integration_id):
            continue
        # The case row locked until the caller's commit (#2373): a concurrent tool or loop write
        # of the same case waits, and the ticket moves the state the case is really in.
        case = await case_loop.locked_case(db, case.id) or case
        remote = RemoteTicket(
            key=key,
            url=str(ticket.get("url") or ""),
            status=map_status(
                status_name,
                status_map=dict(getattr(sync, "status_map", {}) or {}),
                category=category,
                table=(getattr(sync, "record_type", "") or "incident") if provider == "servicenow" else "",
            ),
            external_status=status_name,
        )
        # The status is given (``remote``): pull_ticket_status reads only the adapter's provider name.
        source = cast(TicketingAdapter, _InboundSource(provider))
        outcome = await pull_ticket_status(db, case, source, sync, remote=remote, now=now)
        if outcome.retest_requested:
            _raise_retest(events, case, now, db=db)
        _frame_if_moved(events, case, outcome, now)
        return outcome
    return None


async def apply_jira_delivery(integration_id: str, payload: Any) -> SyncOutcome | None:
    """Move the case of a Jira issue a verified webhook delivery carries (``jira_webhooks``).

    Never raises: a failure is logged and the next ``sync_cases`` pull applies the status.
    """
    from app.core.database import async_session

    issue = payload.get("issue") if isinstance(payload, dict) else None
    if not isinstance(issue, dict):
        return None
    raw_fields = issue.get("fields")
    fields: dict[str, Any] = raw_fields if isinstance(raw_fields, dict) else {}
    raw_status = fields.get("status")
    status: dict[str, Any] = raw_status if isinstance(raw_status, dict) else {}
    name = str(status.get("name") or "")
    if not name:
        return None
    category = str((status.get("statusCategory") or {}).get("key") or "")
    pending = case_loop.PendingEvents()
    try:
        async with async_session() as db:
            outcome = await apply_inbound_status(
                db,
                integration_id=str(integration_id),
                key=str(issue.get("key") or ""),
                status_name=name,
                category=category,
                events=pending,
            )
            if outcome is None:
                return None
            await db.commit()
        await case_loop.publish_pending(pending)
        return outcome
    except Exception:
        logger.warning("CTEM ticket status from a Jira delivery left to the sync pull", exc_info=True)
        return None


async def ticketing_target(db: AsyncSession) -> dict[str, Any]:
    """Whether a ticketing target is declared, for the case screens (CTEM-19-UI, #1032).

    Reads the profile only (no credential, no remote call): ``connected`` false
    carries the reason and the settings link the screens show, never an error.
    """
    from app.services import organization_context as oc

    row = await oc.current_version(db)
    integration = ""
    if row is not None:
        target = getattr(oc.parse_profile(row.profile), "ticketing", None)
        integration = (getattr(target, "integration", "") or "").strip()
    if not integration:
        return {"connected": False, "reason": NOT_CONNECTED, "settings": SETTINGS_PATH}
    return {"connected": True, "settings": SETTINGS_PATH}


#: The providers the sync knows, by integration provider key. A new ticketing
#: provider adds its adapter here and documents it in dev-docs/ctem-ticketing.md.
ADAPTERS: dict[str, type] = {"jira": JiraAdapter, "servicenow": ServiceNowAdapter}


async def adapter_for(db: AsyncSession, profile: Any) -> TicketingAdapter | str:
    """The adapter of the declared ticketing target, or the reason there is none (shown on the case)."""
    from app.models.integration import Integration
    from app.services.integration_tools import _get_token

    target = getattr(profile, "ticketing", None)
    integration_id = (getattr(target, "integration", "") or "").strip()
    project = (getattr(target, "project", "") or "").strip()
    if not integration_id:
        return NOT_CONNECTED
    try:
        integration = await db.get(Integration, uuid.UUID(integration_id))
    except ValueError:
        return f"the ticketing target {integration_id!r} is not an integration id"
    if integration is None:
        return "the declared ticketing integration no longer exists"
    if integration.provider not in ADAPTERS:
        return f"{integration.provider} has no ticketing sync yet"
    sync = getattr(target, "sync", None)
    status_map = dict(getattr(sync, "status_map", {}) or {})
    record_type = getattr(sync, "record_type", "") or ""
    owner = str(integration.user_id) if integration.user_id else None
    token = await _get_token(db, integration_id, integration.provider, user_id=owner, integ=integration)
    if not token:
        return "no credential could be resolved for the ticketing integration"
    if integration.provider == "jira":
        from app.integrations.providers.jira._common import jira_headers, parse_jira_token

        base, access = parse_jira_token(token)
        if not base or not access:
            return "the Jira integration has no site or token"
        return JiraAdapter(base, jira_headers(access), project, status_map=status_map)
    from app.integrations.providers.servicenow._common import build_servicenow_headers, parse_servicenow_token

    base, user, password = parse_servicenow_token(token)
    if not base:
        return "the ServiceNow integration has no instance URL"
    if (record_type or "incident") not in SERVICENOW_TABLES:
        return f"the ServiceNow table {record_type!r} has no ticketing sync"
    return ServiceNowAdapter(
        base,
        build_servicenow_headers(user, password),
        record_type or "incident",
        group=project,
        status_map=status_map,
    )


async def target_problem(db: AsyncSession, profile: Any) -> str | None:
    """Why the profile's ticketing settings cannot be saved, or ``None`` (the settings save calls it).

    A ServiceNow target takes only a table the sync maps (``SERVICENOW_TABLES``): the
    state numbers differ per table, so another table would read and write wrong states.
    """
    from app.models.integration import Integration

    target = getattr(profile, "ticketing", None)
    integration_id = (getattr(target, "integration", "") or "").strip()
    record_type = (getattr(getattr(target, "sync", None), "record_type", "") or "").strip()
    if not integration_id or not record_type:
        return None
    try:
        integration = await db.get(Integration, uuid.UUID(integration_id))
    except ValueError:
        return None
    if integration is None or integration.provider != "servicenow" or record_type in SERVICENOW_TABLES:
        return None
    return (
        f"ticketing.sync.record_type: the ServiceNow table {record_type!r} has no ticketing sync; "
        f"use one of {', '.join(SERVICENOW_TABLES)}"
    )
