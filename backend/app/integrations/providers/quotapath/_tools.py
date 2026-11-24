"""The QuotaPath tools: one per operation of the public API, plus derived reads.

The table ``OPERATIONS`` is the whole surface of the QuotaPath API v1 as its
OpenAPI document publishes it (https://api.quotapath.com/docs/, 18
operations): users, teams, plans, deals (CRUD and bulk), quota assignments and
values per path, payouts (all, resolved, unresolved) and the data bulk import.
Read or write follows the HTTP method: every POST / PUT / PATCH / DELETE tool
is named with a write verb (a read-only binding withholds it) and asks a person
on every call (``always_ask``), whoever the arguments name.

On top of the table, three reads computed from those answers:
``get_quotapath_commission_totals`` (commission totals by rep, team, plan, path or
month, the attainment view the API itself does not serve) and two CSV exports
(payouts, deals) delivered as files of the run.

What the public API does not expose is not faked: disputes, adjustments and
draws, payout-period approval or lock, CRM sync status and commission
statement PDFs live in the QuotaPath application only.

Commission amounts are people data: every answer names the rep and the period
it was asked for, and the summary filters by rep, team and dates server-side
of the platform before anything reaches the model.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from app.integrations.file_delivery import deliver_file
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._client import PROVIDER, SOURCE, QuotaPathClient, seg

#: The longest text a tool answers; the rest is cut with a note.
MAX_ANSWER_CHARS = 60_000

_PAGING = {
    "limit": {"type": "integer", "description": "Rows per page (QuotaPath default applies when omitted)."},
    "offset": {"type": "integer", "description": "Index of the first row to return."},
    "all_pages": {
        "type": "boolean",
        "description": "Read every page (up to 5,000 rows) instead of one.",
    },
}
_UPDATED_SINCE = {
    "updated_since": {
        "type": "integer",
        "description": "Unix timestamp in seconds: only the records updated at or after it.",
    }
}
_DEAL_VALUE = {
    "type": "object",
    "properties": {
        "value": {"type": "number", "description": "Deal amount credited on the path."},
        "path_id": {"type": "string", "description": "UUID of the plan path this value applies to."},
    },
    "required": ["value", "path_id"],
}
_DEAL_FIELDS: dict[str, Any] = {
    "name": {"type": "string", "description": "Deal name."},
    "user_email": {"type": "string", "description": "Email of the deal owner (the rep credited)."},
    "deal_values": {
        "type": "array",
        "items": _DEAL_VALUE,
        "description": "One value per plan path the deal is credited on (at least one).",
    },
    "date": {"type": "string", "description": "Deal date, YYYY-MM-DD."},
    "state": {"type": "string", "description": "Deal state (for example closed_won)."},
    "integration_id": {
        "type": "string",
        "description": "The deal's id in the source system (CRM), the upsert key with its source.",
    },
    "admin_locked": {"type": "boolean", "description": "Lock the deal against CRM sync overwrites."},
    "metadata": {
        "type": "object",
        "description": "Custom fields: date_field_1, date_field_2 (YYYY-MM-DD), number_field_1 (decimal).",
    },
}
_FREE_BODY = {
    "body": {
        "type": "object",
        "description": (
            "The JSON body QuotaPath expects for this call (its OpenAPI document publishes no schema "
            "for it): read the matching list first and mirror one of its rows."
        ),
    }
}


@dataclass(frozen=True)
class Operation:
    """One operation of the QuotaPath API and the tool that calls it."""

    name: str
    method: str
    path: str
    summary: str
    description: str
    path_params: tuple[str, ...] = ()
    query: dict[str, Any] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    paginated: bool = False
    essential: bool = False

    @property
    def write(self) -> bool:
        return self.method != "GET"


OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "list_quotapath_users",
        "GET",
        "/user/",
        "List users",
        "Users of the QuotaPath workspace: id, name, email.",
        query=dict(_PAGING),
        paginated=True,
        essential=True,
    ),
    Operation(
        "list_quotapath_teams",
        "GET",
        "/team/",
        "List teams",
        "Teams with their current members and current managers (the manager roll-up).",
        query=dict(_PAGING),
        paginated=True,
        essential=True,
    ),
    Operation(
        "list_quotapath_plans",
        "GET",
        "/plan/",
        "List compensation plans",
        "Compensation plans with their assignments (who is on the plan, from when) and paths (the plan "
        "components: quota, rate, payout rules). A path id is what quotas and deal values hang on.",
        query=dict(_PAGING),
        paginated=True,
        essential=True,
    ),
    Operation(
        "list_quotapath_deals",
        "GET",
        "/deal/",
        "List deals",
        "Deals (transactions) and their crediting per path. Filter by id, integration_id (the CRM id), "
        "path_id or user_email; no filter lists every deal.",
        query={
            "id": {"type": "string", "description": "Deal UUID."},
            "integration_id": {"type": "string", "description": "The deal's id in the source CRM."},
            "path_id": {"type": "string", "description": "Only deals credited on this plan path."},
            "user_email": {"type": "string", "description": "Only deals owned by this rep."},
            **_PAGING,
        },
        paginated=True,
        essential=True,
    ),
    Operation(
        "get_quotapath_deal",
        "GET",
        "/deal/{uuid}/",
        "Get a deal",
        "One deal by UUID, with its values per path and metadata.",
        path_params=("uuid",),
        required=("uuid",),
    ),
    Operation(
        "create_quotapath_deal",
        "POST",
        "/deal/",
        "Create a deal",
        "Create a deal credited to a rep on one or more plan paths. Changes commission results.",
        body=dict(_DEAL_FIELDS),
        required=("name", "user_email", "deal_values"),
    ),
    Operation(
        "create_quotapath_deals_bulk",
        "POST",
        "/deal/bulk/",
        "Create deals in bulk",
        "Create or upsert many deals in one call (each item takes the fields of create_quotapath_deal). "
        "Changes commission results.",
        body={
            "deals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": _DEAL_FIELDS,
                    "required": ["name", "user_email", "deal_values"],
                },
                "description": "The deals to create or upsert.",
            }
        },
        required=("deals",),
    ),
    Operation(
        "update_quotapath_deal",
        "PUT",
        "/deal/{uuid}/",
        "Replace a deal",
        "Replace a deal whole (name, owner and values required). Changes commission results.",
        path_params=("uuid",),
        body=dict(_DEAL_FIELDS),
        required=("uuid", "name", "user_email", "deal_values"),
    ),
    Operation(
        "update_quotapath_deal_fields",
        "PATCH",
        "/deal/{uuid}/",
        "Update deal fields",
        "Change some fields of a deal, the others kept. Changes commission results.",
        path_params=("uuid",),
        body=dict(_DEAL_FIELDS),
        required=("uuid",),
    ),
    Operation(
        "delete_quotapath_deal",
        "DELETE",
        "/deal/{uuid}/",
        "Delete a deal",
        "Delete a deal and its crediting. Changes commission results; cannot be undone.",
        path_params=("uuid",),
        required=("uuid",),
    ),
    Operation(
        "list_quotapath_quota_assignments",
        "GET",
        "/path/{path_uuid}/quota/assignments/",
        "List quota assignments of a path",
        "Who carries a quota on a plan path, and over which periods.",
        path_params=("path_uuid",),
        query=dict(_PAGING),
        required=("path_uuid",),
        paginated=True,
    ),
    Operation(
        "create_quotapath_quota_assignment",
        "POST",
        "/path/{path_uuid}/quota/assignments/",
        "Assign a quota on a path",
        "Add a quota assignment (a member put on the path's quota). Changes attainment and commissions.",
        path_params=("path_uuid",),
        body=dict(_FREE_BODY),
        required=("path_uuid", "body"),
    ),
    Operation(
        "list_quotapath_quota_values",
        "GET",
        "/path/{path_uuid}/quota/values/",
        "List quota values of a path",
        "The quota amounts per period of a plan path (in the workspace currency).",
        path_params=("path_uuid",),
        query=dict(_PAGING),
        required=("path_uuid",),
        paginated=True,
    ),
    Operation(
        "set_quotapath_quota_values",
        "POST",
        "/path/{path_uuid}/quota/values/",
        "Set quota values of a path",
        "Write quota amounts per period on a plan path. Changes attainment and commissions.",
        path_params=("path_uuid",),
        body=dict(_FREE_BODY),
        required=("path_uuid", "body"),
    ),
    Operation(
        "list_quotapath_payouts",
        "GET",
        "/payout/",
        "List payouts",
        "Commission earned: one row per rep, deal, plan and path with its value and date. People data: "
        "ask for the reps and period needed.",
        query={**_UPDATED_SINCE, **_PAGING},
        paginated=True,
        essential=True,
    ),
    Operation(
        "list_quotapath_resolved_payouts",
        "GET",
        "/payout/resolved/",
        "List resolved payouts",
        "Payouts resolved (paid out), per rep, deal, plan and path. People data.",
        query={**_UPDATED_SINCE, **_PAGING},
        paginated=True,
    ),
    Operation(
        "list_quotapath_unresolved_payouts",
        "GET",
        "/payout/unresolved/",
        "List unresolved payouts",
        "Payouts not yet paid (kind unresolved or partially_resolved) with the amount still owed: the "
        "commission accrual. People data.",
        query=dict(_PAGING),
        paginated=True,
        essential=True,
    ),
    Operation(
        "import_quotapath_data",
        "POST",
        "/data/bulk-import/",
        "Bulk import data",
        "Upsert records of a data type from a source (QuotaPath universal import): data is a JSON string "
        "of the rows, data_id_field the field that identifies a row. Changes commission inputs.",
        body={
            "data_type": {"type": "string", "description": "The QuotaPath data type the rows are (for example deal)."},
            "data_source": {"type": "string", "description": "Name of the source system."},
            "data_id_field": {"type": "string", "description": "Field of each row that identifies it."},
            "data": {"type": "string", "description": "The rows, as a JSON string."},
        },
        required=("data_type", "data_source", "data_id_field", "data"),
    ),
)


def operations() -> tuple[Operation, ...]:
    """The table, in its order."""
    return OPERATIONS


def render(title: str, data: Any) -> str:
    """A tool answer: the title, then the JSON, cut at ``MAX_ANSWER_CHARS``."""
    text = json.dumps(data, ensure_ascii=False, indent=1, default=str)
    if len(text) > MAX_ANSWER_CHARS:
        text = text[:MAX_ANSWER_CHARS] + "\n... (cut: narrow the request with limit/offset or a filter)"
    return f"{title}\n{text}"


def _schema(op: Operation) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for name in op.path_params:
        properties[name] = {"type": "string", "description": f"{name.replace('_', ' ')} (UUID)."}
    properties.update(op.query)
    properties.update(op.body)
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if op.required:
        schema["required"] = list(op.required)
    return schema


def _description(op: Operation) -> str:
    text = f"{op.summary} (QuotaPath {op.method} {op.path}). {op.description}"
    if op.write:
        text += " Asks a person before it runs."
    return text


def _make_handler(client: QuotaPathClient, op: Operation) -> Callable[..., Awaitable[str]]:
    action = op.summary[0].lower() + op.summary[1:]

    async def handler(**kwargs: Any) -> str:
        path = op.path
        for name in op.path_params:
            value = kwargs.get(name)
            if value is None or str(value).strip() == "":
                return f"Error: {name} is required."
            path = path.replace("{" + name + "}", seg(value))
        query = {name: kwargs[name] for name in op.query if name in kwargs and name != "all_pages"}
        payload: Any = None
        if "body" in op.body:
            payload = kwargs.get("body")
        elif op.body:
            payload = {name: kwargs[name] for name in op.body if name in kwargs}
        title = f"{PROVIDER}: {op.summary}"
        if op.paginated and kwargs.get("all_pages"):
            rows, truncated, err = await client.list_all(path, action, query)
            if err and not rows:
                return err
            note = " (first 5,000 rows)" if truncated else ""
            if err:
                note += f" (stopped: {err})"
            return render(f"{title}: {len(rows)} rows{note}", rows)
        resp, err = await client.request(op.method, path, action, params=query, body=payload)
        if err or resp is None:
            return err or f"Error: {PROVIDER} answered nothing while trying to {action}."
        if resp.data is None and resp.content:
            return f"{title}\n{resp.content[:MAX_ANSWER_CHARS].decode('utf-8', 'replace')}"
        if resp.data is None:
            return f"{title}: done."
        return render(title, resp.data)

    return handler


# -- derived reads ----------------------------------------------------------------

_PAYOUT_SOURCES = {
    "all": "/payout/",
    "resolved": "/payout/resolved/",
    "unresolved": "/payout/unresolved/",
}
_GROUPS = ("rep", "team", "plan", "path", "month", "quarter")


def _parse_day(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _team_of(teams: list[dict[str, Any]]) -> dict[str, str]:
    """Rep email -> team name, from the current members of each team."""
    out: dict[str, str] = {}
    for team in teams:
        for member in team.get("current_members") or []:
            email = member.get("email") if isinstance(member, dict) else None
            if email:
                out.setdefault(str(email).lower(), str(team.get("name") or ""))
    return out


def summarize_payouts(
    rows: list[dict[str, Any]],
    *,
    group_by: str,
    date_from: date | None,
    date_to: date | None,
    rep_emails: set[str],
    team_of: dict[str, str],
    team: str = "",
) -> dict[str, Any]:
    """Totals of payout ``value`` (and ``owed`` when present) per group, filtered."""
    totals: dict[str, dict[str, Any]] = {}
    kept = 0
    for row in rows:
        email = str(row.get("target_user_email") or "").lower()
        if rep_emails and email not in rep_emails:
            continue
        row_team = team_of.get(email, "")
        if team and row_team.lower() != team.lower():
            continue
        day = _parse_day(row.get("date") or row.get("deal_date"))
        if (date_from and (day is None or day < date_from)) or (date_to and (day is None or day > date_to)):
            continue
        if group_by == "rep":
            key = f"{row.get('target_user_name') or ''} <{email}>"
        elif group_by == "team":
            key = row_team or "(no team)"
        elif group_by == "plan":
            key = str(row.get("plan_name") or "(no plan)")
        elif group_by == "path":
            key = f"{row.get('plan_name') or ''} / {row.get('path_name') or ''}"
        elif group_by == "quarter":
            key = f"{day.year}-Q{(day.month - 1) // 3 + 1}" if day else "(no date)"
        else:
            key = day.strftime("%Y-%m") if day else "(no date)"
        bucket = totals.setdefault(key, {"value": 0.0, "owed": 0.0, "payouts": 0, "deals": set()})
        bucket["value"] += float(row.get("value") or 0)
        bucket["owed"] += float(row.get("owed") or 0)
        bucket["payouts"] += 1
        if row.get("deal_uuid"):
            bucket["deals"].add(str(row["deal_uuid"]))
        kept += 1
    groups = [
        {
            group_by: key,
            "value": round(b["value"], 2),
            **({"owed": round(b["owed"], 2)} if b["owed"] else {}),
            "payouts": b["payouts"],
            "deals": len(b["deals"]),
        }
        for key, b in sorted(totals.items())
    ]
    return {
        "group_by": group_by,
        "period": {
            "from": date_from.isoformat() if date_from else None,
            "to": date_to.isoformat() if date_to else None,
        },
        "payouts_counted": kept,
        "total_value": round(sum(g["value"] for g in groups), 2),
        "groups": groups,
    }


def _derived_tools(
    client: QuotaPathClient, executor: CallableToolExecutor, persist_file_fn: Any
) -> list[ToolDefinition]:
    async def summarize(
        group_by: str = "rep",
        status: str = "all",
        date_from: str = "",
        date_to: str = "",
        rep_emails: list[str] | None = None,
        team: str = "",
    ) -> str:
        if group_by not in _GROUPS:
            return f"Error: group_by is one of {', '.join(_GROUPS)}."
        source = _PAYOUT_SOURCES.get(status)
        if source is None:
            return f"Error: status is one of {', '.join(_PAYOUT_SOURCES)}."
        start, end = _parse_day(date_from), _parse_day(date_to)
        if (date_from and start is None) or (date_to and end is None):
            return "Error: date_from and date_to are YYYY-MM-DD."
        rows, truncated, err = await client.list_all(source, "list payouts")
        if err and not rows:
            return err
        team_of: dict[str, str] = {}
        if group_by == "team" or team:
            teams, _, terr = await client.list_all("/team/", "list teams")
            if terr and not teams:
                return terr
            team_of = _team_of(teams)
        summary = summarize_payouts(
            [r for r in rows if isinstance(r, dict)],
            group_by=group_by,
            date_from=start,
            date_to=end,
            rep_emails={e.strip().lower() for e in rep_emails or [] if e and e.strip()},
            team_of=team_of,
            team=team.strip(),
        )
        summary["status"] = status
        if truncated:
            summary["note"] = "only the first 5,000 payouts were read"
        if err:
            summary["note"] = f"stopped early: {err}"
        return render(f"{PROVIDER}: commission totals by {group_by} ({status} payouts)", summary)

    async def export(kind: str, status: str = "all") -> str | ToolResult:
        if kind == "payouts":
            source = _PAYOUT_SOURCES.get(status)
            if source is None:
                return f"Error: status is one of {', '.join(_PAYOUT_SOURCES)}."
        else:
            source = "/deal/"
        rows, truncated, err = await client.list_all(source, f"export {kind}", max_rows=50_000)
        if err and not rows:
            return err
        flat = [r for r in rows if isinstance(r, dict)]
        columns: list[str] = []
        for row in flat:
            for key in row:
                if key not in columns:
                    columns.append(key)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in flat:
            writer.writerow(
                {k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for k, v in row.items()}
            )
        stamp = datetime.now(UTC).strftime("%Y%m%d")
        label = f"{status}-payouts" if kind == "payouts" else "deals"
        details = [f"{len(flat)} {label} from QuotaPath."]
        if truncated:
            details.append("Only the first 50,000 rows were exported.")
        if err:
            details.append(f"Stopped early: {err}")
        return await deliver_file(
            data=buffer.getvalue().encode("utf-8"),
            filename=f"quotapath-{label}-{stamp}.csv",
            content_type="text/csv",
            source=PROVIDER,
            persist_file_fn=persist_file_fn,
            details=details,
        )

    async def export_payouts(status: str = "all") -> str | ToolResult:
        return await export("payouts", status)

    async def export_deals() -> str | ToolResult:
        return await export("deals")

    executor.register("get_quotapath_commission_totals", summarize)
    executor.register("export_quotapath_payouts", export_payouts)
    executor.register("export_quotapath_deals", export_deals)
    status_schema = {
        "type": "string",
        "enum": list(_PAYOUT_SOURCES),
        "description": "all payouts, resolved (paid) or unresolved (still owed: the accrual).",
    }
    return [
        ToolDefinition(
            name="get_quotapath_commission_totals",
            description=(
                "Commission totals from QuotaPath payouts, grouped by rep, team, plan, path, month or quarter, "
                "filtered by period (payout date), reps and team: the commission burden, the accrual "
                "(status unresolved) and attainment roll-ups. People data: filter to what is needed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_by": {"type": "string", "enum": list(_GROUPS)},
                    "status": status_schema,
                    "date_from": {"type": "string", "description": "First day, YYYY-MM-DD (inclusive)."},
                    "date_to": {"type": "string", "description": "Last day, YYYY-MM-DD (inclusive)."},
                    "rep_emails": {"type": "array", "items": {"type": "string"}, "description": "Only these reps."},
                    "team": {"type": "string", "description": "Only the current members of this team."},
                },
                "additionalProperties": False,
            },
            source=SOURCE,
            priority=0,
            read_only=True,
        ),
        ToolDefinition(
            name="export_quotapath_payouts",
            description="Export QuotaPath payouts (all, resolved or unresolved) as a CSV file of the run: the payout report.",
            input_schema={
                "type": "object",
                "properties": {"status": status_schema},
                "additionalProperties": False,
            },
            source=SOURCE,
            priority=1,
            read_only=True,
        ),
        ToolDefinition(
            name="export_quotapath_deals",
            description="Export every QuotaPath deal and its crediting as a CSV file of the run.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            source=SOURCE,
            priority=1,
            read_only=True,
        ),
    ]


def build_operation_tools(
    client: QuotaPathClient,
    executor: CallableToolExecutor,
    *,
    persist_file_fn: Any = None,
) -> list[ToolDefinition]:
    """A tool for every operation of the table and the derived reads, handlers registered."""
    tools: list[ToolDefinition] = []
    for op in operations():
        executor.register(op.name, _make_handler(client, op))
        tools.append(
            ToolDefinition(
                name=op.name,
                description=_description(op),
                input_schema=_schema(op),
                source=SOURCE,
                priority=0 if op.essential else (2 if op.write else 1),
                always_ask=op.write,
                read_only=not op.write,
            )
        )
    tools.extend(_derived_tools(client, executor, persist_file_fn))
    return tools
