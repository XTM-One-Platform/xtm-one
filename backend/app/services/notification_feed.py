# SPDX-License-Identifier: Apache-2.0

"""The notification feed: what the bell shows, where each item leads, and what is read.

One rule for every surface that shows a person their notifications: the web
app's bell (``Header.tsx``), the mobile companion's Inbox and the push sent to
a linked phone (``services/mobile_push.py``). Each item is presented here,
with the page it opens (``route``), so the three cannot disagree on what a
notification says or where it leads.

The items come from the two sources the bell always read:

* notification-worthy assignment runs (awaiting a human, or finished with
  ``notify_on_complete``), through ``GET /assignments/runs/all``'s own query,
  visibility included;
* the person's ``Activity`` rows of the notification types
  (:data:`BELL_ACTIVITY_TYPES`), routed by their ``notification_source``;
* the work items waiting for the person (``work_item_waiting``, #9437): the
  population of the dashboard notice, one entry per item, unless the person
  turned them off (``User.notify_waiting_work_items``). Answering the item
  takes it out of that population, which clears its entry.

Read state lives on the server (``NotificationReadState``) so a phone and a
computer agree on what is unread. A run awaiting a human, and a work item
waiting for the person, is unread until it is answered, whatever was marked.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import strip_artifact_markers
from app.models.notification_read_state import NotificationReadState

#: Activity types an integration auth failure is recorded under (#3218).
AUTH_FAILURE_ACTIVITY_TYPES: tuple[str, ...] = ("assignment_auth_quarantine", "assignment_auth_failure")
#: A resource the integration keeps reporting missing, and the pause after (#2528).
STALE_REFERENCE_ACTIVITY_TYPES: tuple[str, ...] = ("assignment_stale_reference", "assignment_stale_reference_pause")
#: A chain or retry not started: its user cannot open the run it continues (#4906).
CONTINUATION_REFUSED_ACTIVITY_TYPES: tuple[str, ...] = ("assignment_continuation_refused",)

#: Every activity type the bell reads.
BELL_ACTIVITY_TYPES: tuple[str, ...] = (
    "notification",
    *AUTH_FAILURE_ACTIVITY_TYPES,
    *STALE_REFERENCE_ACTIVITY_TYPES,
    *CONTINUATION_REFUSED_ACTIVITY_TYPES,
)

#: The kinds of notification, in the frontend's ``NotificationKind``.
NOTIFICATION_KINDS: tuple[str, ...] = (
    "hitl",
    "flow_hitl",
    "run",
    "analytics_refresh",
    "flow",
    "auth_failure",
    "stale_reference",
    "continuation_refused",
    "security_audit",
    "api_key",
    "agent_account",
    "reminder",
    "agent_message",
    "run_action_request",
    "work_item_waiting",
    "builtin_tools_outside_allowlist",
)

#: Kinds that wait on the person: shown first.
URGENT_KINDS: frozenset[str] = frozenset({"hitl", "flow_hitl", "work_item_waiting"})
#: Kinds unread until answered, whatever was marked read.
UNREAD_UNTIL_ANSWERED_KINDS: frozenset[str] = frozenset({"hitl", "work_item_waiting"})
#: The id prefix of a waiting work item's entry (never a run's or an activity's id).
WORK_ITEM_WAITING_PREFIX = "work_item:"

#: How many items each source contributes (the bell's window, unchanged).
FEED_WINDOW = 20
#: Dismissed ids kept per person: past it the oldest go (they are long read).
MAX_DISMISSED_IDS = 500
#: A run summary's preview length.
SUMMARY_PREVIEW_CHARS = 200


@dataclass(frozen=True, slots=True)
class NotificationItem:
    id: str
    kind: str
    created_at: str
    route: str
    title: str
    subtitle: str | None = None
    summary: str | None = None
    critical: bool = False
    #: The run a ``hitl`` item waits on (the Inbox answers it there).
    run_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _str(metadata: dict | None, key: str) -> str | None:
    value = (metadata or {}).get(key)
    return value if isinstance(value, str) and value else None


def _internal_path(value: str | None, fallback: str) -> str:
    """A path of the web app (``internal_paths``), else ``fallback``."""
    from app.core.internal_paths import is_internal_path

    return value if value and is_internal_path(value) else fallback


def summary_preview(text: str | None, limit: int = SUMMARY_PREVIEW_CHARS) -> str:
    """A marker-free one-line preview (the frontend's ``summaryPreview``)."""
    clean = strip_artifact_markers(text or "")
    clean = " ".join(clean.split())
    return clean if len(clean) <= limit else f"{clean[:limit]}…"


def _agent_account_route(account_id: str | None, kind: str | None) -> str:
    """An agent account's replies, the waiting ones for a draft, the handed-over ones when flagged."""
    if not _is_uuid(account_id):
        return "/profile/agent-accounts"
    account = f"/profile/agent-accounts/{account_id}"
    if kind == "flagged":
        return f"{account}/decisions?group=needs_owner"
    return f"{account}/replies" if kind == "sent" else f"{account}/replies?status=pending"


def _is_uuid(value: str | None) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


def present_run(run: Any) -> NotificationItem | None:
    """A notification-worthy run as a bell item, or ``None`` (nothing to say)."""
    created = run.created_at if isinstance(run.created_at, str) else run.created_at.isoformat()
    title = run.assignment_name or "Delegation"
    if run.status == "awaiting_human":
        if not run.human_input_question:
            return None
        return NotificationItem(
            id=str(run.id),
            kind="hitl",
            created_at=created,
            route=f"/runs/{run.id}",
            title=title,
            subtitle=run.agent_name,
            summary=run.human_input_question,
            run_id=str(run.id),
        )
    preview = summary_preview(run.result_summary)
    # A summary that is mostly a marker would make an empty notification.
    if len(preview) <= 10:
        return None
    return NotificationItem(
        id=str(run.id),
        kind="run",
        created_at=created,
        route=f"/runs/{run.id}",
        title=title,
        subtitle=run.agent_name,
        summary=preview,
    )


def waiting_deadline_text(deadline: object) -> str | None:
    """``Answer by Fri 21 Nov, 09:00 UTC`` for an ask deadline; ``None`` when absent or unreadable."""
    if not isinstance(deadline, str) or not deadline.strip():
        return None
    try:
        when = datetime.fromisoformat(deadline.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    when = when.astimezone(UTC)
    return f"Answer by {when:%a} {when.day} {when:%b}, {when:%H:%M} UTC"


def present_waiting_work_item(item: dict[str, Any]) -> NotificationItem | None:
    """A work item waiting for the person as a bell entry, the item open in
    Tasks > Work Items (where the decision card answers it).

    The entry says what the decision card says, nothing else (#776): the ask's
    question as the title (``Decision needed: <title>`` without one) and its
    deadline as the summary. ``blocked_reason`` is never shown here.
    """
    item_id = item.get("id")
    if not _is_uuid(item_id):
        return None
    question = summary_preview(item.get("question"))
    title = question or f"Decision needed: {item.get('title') or 'work item'}"
    return NotificationItem(
        id=f"{WORK_ITEM_WAITING_PREFIX}{item_id}",
        kind="work_item_waiting",
        created_at=str(item.get("since") or ""),
        route=f"/tasks/work-items/{item_id}",
        title=title,
        subtitle="Waiting for your decision",
        summary=waiting_deadline_text(item.get("deadline")),
    )


def present_activity(activity: Any, *, flows_enabled: bool) -> NotificationItem | None:
    """An activity row as a bell item, or ``None`` for a row the bell does not show."""
    metadata = activity.extra_data if isinstance(getattr(activity, "extra_data", None), dict) else {}
    source = _str(metadata, "notification_source")
    activity_type = activity.activity_type
    created = activity.created_at if isinstance(activity.created_at, str) else activity.created_at.isoformat()
    base = {
        "id": str(activity.id),
        "created_at": created,
        "title": activity.title,
        "summary": activity.summary,
    }

    def item(kind: str, route: str, subtitle: str, **extra: Any) -> NotificationItem:
        return NotificationItem(kind=kind, route=route, subtitle=activity.agent_name or subtitle, **base, **extra)

    assignment_id = _str(metadata, "assignment_id")
    assignment_route = f"/assignments/{assignment_id}" if _is_uuid(assignment_id) else None
    if activity_type in AUTH_FAILURE_ACTIVITY_TYPES:
        return item("auth_failure", assignment_route or "/integrations", "Integration")
    if activity_type in STALE_REFERENCE_ACTIVITY_TYPES:
        return item("stale_reference", assignment_route or "/integrations", "Integration")
    if activity_type in CONTINUATION_REFUSED_ACTIVITY_TYPES:
        return item("continuation_refused", assignment_route or "/assignments", "Assignment")
    if activity_type != "notification":
        return None

    if source == "llm_analytics_refresh":
        return item("analytics_refresh", _internal_path(_str(metadata, "dashboard_path"), "/observability"), "System")
    if source in ("flow_hitl", "flow"):
        # The builder is hidden behind its flag: a link nobody can open is worse
        # than no notification.
        if not flows_enabled:
            return None
        flow_id = _str(metadata, "flow_id")
        run_id = _str(metadata, "flow_run_id")
        route = "/flows"
        if _is_uuid(flow_id):
            route = f"/flows/{flow_id}/builder"
            if source == "flow_hitl" and _is_uuid(run_id):
                route += f"?run={run_id}"
        return item(source, route, "Flow")
    if source == "security_audit":
        audit_path = _str(metadata, "audit_path")
        route = audit_path if audit_path and audit_path.startswith("/settings/security") else "/settings/security"
        return item(
            "security_audit",
            _internal_path(route, "/settings/security"),
            "Security audit",
            critical=_str(metadata, "severity") == "critical",
        )
    if source == "api_key":
        return item("api_key", "/profile/api-keys", "API keys")
    if source == "agent_account":
        return item(
            "agent_account",
            _agent_account_route(_str(metadata, "account_id"), _str(metadata, "notification_kind")),
            "Agent Accounts",
        )
    if source == "work_item":
        # A scheduled one-off's outcome: its run shows what happened, then the
        # conversation that scheduled it, then the operations page.
        run_id = _str(metadata, "run_id")
        conversation_id = _str(metadata, "conversation_id")
        if _is_uuid(run_id):
            route = f"/runs/{run_id}"
        elif _is_uuid(conversation_id):
            route = f"/chat?c={conversation_id}"
        else:
            route = "/operations"
        return item("reminder", route, "Reminder")
    if source == "agent":
        return item("agent_message", _internal_path(_str(metadata, "path"), "/"), "Agent")
    if source == "run_action_request":
        # Someone who cannot answer a paused run asked its owner to act: the run.
        run_id = _str(metadata, "run_id")
        return item("run_action_request", f"/runs/{run_id}" if _is_uuid(run_id) else "/operations", "Run")
    if source == "builtin_tools_outside_allowlist":
        # A release shipped tools an agent's allowlist leaves out (#12192): its
        # Tooling section, where each one is added in one click.
        return item(
            "builtin_tools_outside_allowlist", _internal_path(_str(metadata, "path"), "/agents"), "Built-in tools"
        )
    return None


def merge_items(items: list[NotificationItem]) -> list[NotificationItem]:
    """Urgent items first, then the rest by recency; one item per id."""
    seen: set[str] = set()
    urgent = [i for i in items if i.kind in URGENT_KINDS]
    rest = sorted((i for i in items if i.kind not in URGENT_KINDS), key=lambda i: i.created_at, reverse=True)
    merged: list[NotificationItem] = []
    for item in [*urgent, *rest]:
        if item.id in seen:
            continue
        seen.add(item.id)
        merged.append(item)
    return merged


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def is_unread(item: NotificationItem, *, read_at: datetime | None, dismissed: set[str]) -> bool:
    if item.kind in UNREAD_UNTIL_ANSWERED_KINDS:
        return True
    if item.id in dismissed:
        return False
    created = _parse_time(item.created_at)
    return read_at is None or (created is not None and created > read_at)


# ── Read state ───────────────────────────────────────────────────────────────


async def load_read_state(db: AsyncSession, user_id: uuid.UUID) -> tuple[datetime | None, list[str]]:
    """The read marker and the ids dismissed since, oldest first."""
    row = (
        await db.execute(select(NotificationReadState).where(NotificationReadState.user_id == user_id))
    ).scalar_one_or_none()
    if row is None:
        return None, []
    return row.read_at, [i for i in (row.dismissed_ids or []) if isinstance(i, str)]


async def save_read_state(
    db: AsyncSession, user_id: uuid.UUID, *, read_at: datetime | None, dismissed: list[str]
) -> None:
    """Upsert the person's read marker and dismissed ids (newest kept)."""
    kept = dismissed[-MAX_DISMISSED_IDS:]
    now = datetime.now(UTC)
    statement = pg_insert(NotificationReadState).values(
        user_id=user_id, read_at=read_at, dismissed_ids=kept, updated_at=now
    )
    statement = statement.on_conflict_do_update(
        index_elements=[NotificationReadState.user_id],
        set_={"read_at": read_at, "dismissed_ids": kept, "updated_at": now},
    )
    await db.execute(statement)


# ── The feed ─────────────────────────────────────────────────────────────────


async def build_feed(db: AsyncSession, user: Any, *, limit: int = FEED_WINDOW) -> dict[str, Any]:
    """The person's notifications, newest urgent first, each marked read or not."""
    from app.api.v1.activity import list_activity
    from app.api.v1.assignments import list_all_runs
    from app.core.feature_flags import is_feature_enabled
    from app.services.work_item import list_work_items_waiting_for_person

    runs = await list_all_runs(
        limit=limit,
        offset=0,
        status=None,
        search=None,
        notifications=True,
        digests=False,
        ownership=None,
        agent_id=None,
        agent_ids=None,
        parent_run_id="none",
        a2a_integration_id=None,
        scope="visible",
        user=user,
        db=db,
    )
    activity_rows = await list_activity(
        user=user, db=db, limit=limit, offset=0, activity_type=",".join(BELL_ACTIVITY_TYPES)
    )
    flows_enabled = is_feature_enabled("FLOW_BUILDER")
    items: list[NotificationItem] = [i for run in runs if (i := present_run(run)) is not None]
    items += [
        i
        for row in activity_rows
        if (i := present_activity(_ActivityView.of(row), flows_enabled=flows_enabled)) is not None
    ]
    if getattr(user, "notify_waiting_work_items", True) is not False:
        waiting = await list_work_items_waiting_for_person(user_id=str(user.id), limit=limit)
        items += [i for row in waiting if (i := present_waiting_work_item(row)) is not None]
    merged = merge_items(items)
    read_at, dismissed_list = await load_read_state(db, user.id)
    dismissed = set(dismissed_list)
    payload = []
    unread = 0
    for item in merged:
        flag = is_unread(item, read_at=read_at, dismissed=dismissed)
        unread += int(flag)
        payload.append({**item.as_dict(), "unread": flag})
    return {"items": payload, "unread_count": unread, "read_at": read_at.isoformat() if read_at else None}


@dataclass(frozen=True, slots=True)
class _ActivityView:
    """An ``ActivityResponse`` read as an activity row (its ``metadata`` is the row's ``extra_data``)."""

    id: str
    activity_type: str
    agent_name: str | None
    title: str
    summary: str | None
    extra_data: dict | None
    created_at: str

    @classmethod
    def of(cls, response: Any) -> _ActivityView:
        return cls(
            id=response.id,
            activity_type=response.activity_type,
            agent_name=response.agent_name,
            title=response.title,
            summary=response.summary,
            extra_data=response.metadata if isinstance(response.metadata, dict) else None,
            created_at=response.created_at,
        )
