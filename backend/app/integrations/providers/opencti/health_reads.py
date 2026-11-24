"""Read-only OpenCTI state reads for the Platform Health Module Analyst (#101 S14a-2b).

Today's OpenCTI tools reach connectors, feeds and playbooks only through the
``manage_*`` tools, whose names classify as writes, so the analyst's allow-list
(``services/health/module_analyst.MODULE_CHECKER_TOOLS``) could not list them.
These three tools read the same objects and nothing else: no argument selects a
mutation, no action switch exists.

* ``list_opencti_connector_states``: every connector with its active flag, type,
  last update and the messages waiting in its queue (root ``connectors``, MODULES).
* ``list_opencti_feed_states``: RSS, CSV, JSON and TAXII feeds with their running
  flag and last execution (roots ``ingestion*``, INGESTION).
* ``list_opencti_playbook_states``: playbooks with their running flag and queued
  messages (root ``playbooks``, AUTOMATION).

The module health reads of S15b-2 (platform-health.md 13.5), platform-wide, none
taking or returning a connector id:

* ``list_opencti_background_tasks``: the newest background tasks with their type,
  progress, completion and errors (root ``backgroundTasks``; OpenCTI 6.0 or later).
* ``get_opencti_managers``: the platform managers with their enabled, running and
  warning flags (``settings.platform_modules``), each classed stream (continuous)
  or cron (intermittent) by :data:`MANAGER_KINDS`. ``running`` is not liveness:
  OpenCTI sets it only while a node holds the manager's lock and executes its
  handler (``managerModule.ts``), so an idle cron manager reads not running between
  two cycles. Only an enabled stream manager down on every one of
  :data:`_MANAGER_READS` reads, :data:`_MANAGER_REREAD_SECONDS` apart, is named down.
* ``get_opencti_platform_queues``: workers consuming and messages queued, platform
  totals only (root ``rabbitMQMetrics``, MODULES); no queue is named.
* ``list_opencti_stream_consumers``: live stream collections and their consumers'
  time lag (root ``streamCollections``; without the ``consumers`` field, the
  collections alone).
* ``get_opencti_licence``: the enterprise licence state and expiry
  (``settings.platform_enterprise_edition``; OpenCTI 6.0 or later).
* ``get_opencti_data_health``: knowledge objects created and updated per day over
  7 days (``stixCoreObjectsTimeSeries`` on ``created_at`` and ``updated_at``),
  the newest creation time (``stixCoreObjects``), the platform-wide total
  (``stixCoreObjectsNumber``) and the newest works with errors or timed out
  (``works``, MODULES), counted platform-wide: no connector is named.
* ``get_opencti_error_rate``: activity log entries in error per day over 7 days
  (``auditsTimeSeries`` on the ``Activity`` stream, ``event_status`` error;
  SETTINGS_SECURITYACTIVITY).

A module that lacks a field answers the error line, and the checker reports the
check that needs it as ``not_run``.

A root the user's role cannot read answers its own error line and the others
still answer. Names, URIs and descriptions are text the platform's users wrote:
the listing is returned wrapped as untrusted content.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from app.services.content_safety import wrap_untrusted
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _edges, _graphql

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencti"

#: The most objects one call lists per root; the listing says when it is cut.
_LIMIT: Final = 100

_CONNECTORS_QUERY: Final = (
    "query HealthConnectors { connectors {"
    " id name active connector_type updated_at"
    " connector_queue_details { messages_number } } }"
)
_CONNECTORS_QUERY_NO_QUEUE: Final = "query HealthConnectors { connectors { id name active connector_type updated_at } }"

#: feed kind -> root field of its list query.
_FEED_ROOTS: Final[dict[str, str]] = {
    "rss": "ingestionRsss",
    "csv": "ingestionCsvs",
    "json": "ingestionJsons",
    "taxii": "ingestionTaxiis",
}

_PLAYBOOKS_QUERY: Final = (
    "query HealthPlaybooks($first: Int) { playbooks(first: $first,"
    " orderBy: name, orderMode: asc) { edges { node {"
    " id name playbook_running queue_messages } } } }"
)


_BACKGROUND_TASKS_QUERY: Final = (
    "query HealthBackgroundTasks($first: Int) { backgroundTasks(first: $first,"
    " orderBy: created_at, orderMode: desc) { edges { node {"
    " id type created_at last_execution_date completed"
    " task_expected_number task_processed_number errors { timestamp message } } } } }"
)

_MANAGERS_QUERY: Final = "query HealthManagers { settings { platform_modules { id enable running warning } } }"

#: OpenCTI manager id -> how its ``running`` flag behaves (OpenCTI master, read
#: 24 Nov 2026, ``opencti-graphql/src/manager``). ``stream``: a stream processor, or
#: the infinite-interval telemetry cron, keeps ``running`` true while a node holds
#: the lock. ``cron``: ``running`` is true only while one cycle executes, false in
#: between. An id missing here is reported ``unknown`` and never judged.
MANAGER_KINDS: Final[dict[str, str]] = {
    "RULE_ENGINE": "stream",
    "HISTORY_MANAGER": "stream",
    "SYNC_MANAGER": "stream",
    "PUBLISHER_MANAGER": "stream",
    "NOTIFICATION_MANAGER": "stream",
    "ACTIVITY_MANAGER": "stream",
    "PLAYBOOK_MANAGER": "stream",
    "TELEMETRY_MANAGER": "stream",
    "TASK_MANAGER": "cron",
    "RETENTION_MANAGER": "cron",
    "EXPIRATION_SCHEDULER": "cron",
    "INGESTION_MANAGER": "cron",
    "CONNECTOR_MANAGER": "cron",
    "GARBAGE_COLLECTION_MANAGER": "cron",
    "INDICATOR_DECAY_MANAGER": "cron",
    "FILE_INDEX_MANAGER": "cron",
    "DATA_SANITY_MANAGER": "cron",
    "EXCLUSION_LIST_CACHE_BUILD_MANAGER": "cron",
    "EXCLUSION_LIST_CACHE_SYNC_MANAGER": "cron",
    "HUB_REGISTRATION_MANAGER": "cron",
    "INGESTION_HEALTH_MANAGER": "cron",
    "PIR_MANAGER": "cron",
    "PLATFORM_USAGE_METRICS_MANAGER": "cron",
    "WORKFLOW_STATUS_CLEANUP_MANAGER": "cron",
    "XTM_ONE_REGISTRATION_MANAGER": "cron",
    "CATALOG_MANAGER": "cron",
}
#: The reads one call makes while an enabled stream manager reads not running.
_MANAGER_READS: Final = 3
#: The seconds between two of those reads (one read is a 30 s heartbeat snapshot).
_MANAGER_REREAD_SECONDS: Final = 60
_MANAGERS_RUNNING_MEANING: Final = (
    "running = a node is executing this manager's handler right now: continuous for"
    " stream managers, intermittent for cron managers (not running between two cycles"
    " is normal). A cron manager is never judged on it."
)

_QUEUES_QUERY: Final = (
    "query HealthPlatformQueues { rabbitMQMetrics { consumers overview {"
    " queue_totals { messages messages_ready messages_unacknowledged } } } }"
)

_STREAMS_QUERY: Final = (
    "query HealthStreams($first: Int) { streamCollections(first: $first) { edges { node {"
    " id name stream_live consumers { connectionId connectedAt lastEventDate timeLag } } } } }"
)
_STREAMS_QUERY_NO_CONSUMERS: Final = (
    "query HealthStreams($first: Int) { streamCollections(first: $first) { edges { node { id name stream_live } } } }"
)

_LICENCE_QUERY: Final = (
    "query HealthLicence { settings { platform_enterprise_edition {"
    " license_validated license_expired license_expiration_date license_type } } }"
)

#: The days the data health and error rate reads cover.
_DAYS: Final = 7
#: The newest works the data health read counts errors over.
_WORK_SAMPLE: Final = 200

_SERIES_QUERY: Final = (
    "query HealthDataSeries($field: String!, $startDate: DateTime!, $endDate: DateTime!) {"
    " stixCoreObjectsTimeSeries(field: $field, operation: count, startDate: $startDate,"
    ' endDate: $endDate, interval: "day") { date value } }'
)
_DATA_TOTALS_QUERY: Final = (
    "query HealthDataTotals { stixCoreObjectsNumber { total count }"
    " stixCoreObjects(first: 1, orderBy: created_at, orderMode: desc) { edges { node { created_at } } } }"
)
_WORKS_QUERY: Final = (
    "query HealthWorks($first: Int) { works(first: $first, orderBy: timestamp, orderMode: desc) {"
    " edges { node { status timestamp errors { timestamp } } } } }"
)
_ERROR_RATE_QUERY: Final = (
    "query HealthErrorRate($startDate: DateTime!, $endDate: DateTime!, $filters: FilterGroup) {"
    ' auditsTimeSeries(field: "timestamp", operation: count, startDate: $startDate,'
    ' endDate: $endDate, interval: "day", types: ["Activity"], filters: $filters) { date value } }'
)
#: Activity entries whose ``event_status`` is ``error`` (the OpenCTI audit screen's red line).
_ERROR_FILTER: Final[dict[str, Any]] = {
    "mode": "and",
    "filters": [{"key": ["event_status"], "values": ["error"], "operator": "eq"}],
    "filterGroups": [],
}

#: The newest background tasks one call reads.
_TASK_LIMIT: Final = 50
#: The errors listed per background task; the line counts the rest.
_TASK_ERRORS: Final = 3


def _feed_query(root: str) -> str:
    return (
        f"query HealthFeeds($first: Int) {{ {root}(first: $first) {{ edges {{ node {{"
        " id name uri ingestion_running last_execution_date } } } }"
    )


def _connector_line(node: dict) -> str:
    state = "active" if node.get("active") else "inactive"
    queue = (node.get("connector_queue_details") or {}).get("messages_number")
    parts = [
        f"- {node.get('name') or '?'} [{state}] type {node.get('connector_type') or '?'}",
        f"updated {node.get('updated_at') or 'never'}",
    ]
    if queue is not None:
        parts.append(f"queue {queue}")
    return ", ".join(parts) + f" (id: {node.get('id') or '?'})"


def _feed_line(kind: str, node: dict) -> str:
    state = "running" if node.get("ingestion_running") else "stopped"
    return (
        f"- {kind} {node.get('name') or '?'} [{state}], last run "
        f"{node.get('last_execution_date') or 'never'}, uri {node.get('uri') or 'none'}"
        f" (id: {node.get('id') or '?'})"
    )


def _playbook_line(node: dict) -> str:
    state = "running" if node.get("playbook_running") else "stopped"
    queued = node.get("queue_messages")
    tail = f", queue {queued}" if queued is not None else ""
    return f"- {node.get('name') or '?'} [{state}]{tail} (id: {node.get('id') or '?'})"


def _task_line(node: dict[str, Any]) -> str:
    state = "completed" if node.get("completed") else "in progress"
    done, expected = node.get("task_processed_number"), node.get("task_expected_number")
    progress = f", {done} of {expected} processed" if expected is not None else ""
    errors = [e for e in (node.get("errors") or []) if isinstance(e, dict)]
    line = (
        f"- {node.get('type') or '?'} [{state}]{progress}, created {node.get('created_at') or '?'},"
        f" last run {node.get('last_execution_date') or 'never'}, errors {len(errors)}"
        f" (id: {node.get('id') or '?'})"
    )
    shown = [f"    {e.get('timestamp') or '?'}: {e.get('message') or '?'}" for e in errors[:_TASK_ERRORS]]
    return "\n".join([line, *shown])


def manager_kind(manager_id: str) -> str:
    """``stream``, ``cron`` or ``unknown`` for an OpenCTI manager id."""
    return MANAGER_KINDS.get(manager_id, "unknown")


def _manager_line(node: dict[str, Any]) -> str:
    enabled = "enabled" if node.get("enable") else "disabled"
    running = "running" if node.get("running") else "not running"
    warning = ", warning" if node.get("warning") else ""
    kind = manager_kind(str(node.get("id") or ""))
    return f"- {node.get('id') or '?'} ({kind}) [{enabled}, {running}{warning}]"


def _stream_down(nodes: list[dict[str, Any]]) -> set[str]:
    """The enabled stream managers that read not running in *nodes*."""
    return {
        str(n.get("id"))
        for n in nodes
        if n.get("enable") and not n.get("running") and manager_kind(str(n.get("id") or "")) == "stream"
    }


def _stream_line(node: dict[str, Any]) -> str:
    state = "live" if node.get("stream_live") else "stopped"
    head = f"- {node.get('name') or '?'} [{state}] (id: {node.get('id') or '?'})"
    if "consumers" not in node:
        return head
    consumers = [c for c in (node.get("consumers") or []) if isinstance(c, dict)]
    rows = [
        f"    consumer {c.get('connectionId') or '?'}: time lag {c.get('timeLag') if c.get('timeLag') is not None else '?'},"
        f" last event {c.get('lastEventDate') or 'never'}, connected {c.get('connectedAt') or '?'}"
        for c in consumers
    ]
    return "\n".join([f"{head}, consumers {len(consumers)}", *rows])


def _window() -> dict[str, str]:
    """The start and end of the last ``_DAYS`` days, ISO 8601 in UTC."""
    end = datetime.now(UTC)
    return {"startDate": (end - timedelta(days=_DAYS)).isoformat(), "endDate": end.isoformat()}


def _series_line(label: str, series: Any) -> str:
    """One line per series: the per-day values, oldest first, and their total."""
    points = [p for p in (series or []) if isinstance(p, dict)]
    values = [int(p.get("value") or 0) for p in points]
    days = ", ".join(f"{str(p.get('date') or '?')[:10]} {v}" for p, v in zip(points, values, strict=True))
    return f"{label} per day ({sum(values)} in {len(values)} days): {days or 'none'}"


def _cut(lines: list[str]) -> list[str]:
    if len(lines) > _LIMIT:
        return [*lines[:_LIMIT], f"(first {_LIMIT} of {len(lines)} shown)"]
    return lines


def register(base_url: str, headers: dict[str, str], executor: CallableToolExecutor) -> list[ToolDefinition]:
    async def _gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    async def list_opencti_connector_states() -> str:
        data, err = await _gql(_CONNECTORS_QUERY)
        if err and "connector_queue_details" in err:
            # An OpenCTI without the queue field: the states still answer.
            data, err = await _gql(_CONNECTORS_QUERY_NO_QUEUE)
        if err:
            return f"Connectors: {err}"
        nodes = [n for n in (data.get("connectors") or []) if isinstance(n, dict)]
        if not nodes:
            return "Connectors: none registered on this OpenCTI."
        lines = _cut([_connector_line(n) for n in nodes])
        return wrap_untrusted(f"Connectors ({len(nodes)}):\n" + "\n".join(lines), "OpenCTI connectors")

    async def list_opencti_feed_states() -> str:
        sections: list[str] = []
        for kind, root in _FEED_ROOTS.items():
            data, err = await _gql(_feed_query(root), {"first": _LIMIT})
            if err:
                sections.append(f"{kind} feeds: {err}")
                continue
            nodes = _edges(data, root)
            if not nodes:
                sections.append(f"{kind} feeds: none")
                continue
            sections.append(f"{kind} feeds ({len(nodes)}):\n" + "\n".join(_feed_line(kind, n) for n in nodes))
        return wrap_untrusted("\n".join(sections), "OpenCTI feeds")

    async def list_opencti_playbook_states() -> str:
        data, err = await _gql(_PLAYBOOKS_QUERY, {"first": _LIMIT})
        if err:
            return f"Playbooks: {err}"
        nodes = _edges(data, "playbooks")
        if not nodes:
            return "Playbooks: none on this OpenCTI."
        return wrap_untrusted(
            f"Playbooks ({len(nodes)}):\n" + "\n".join(_playbook_line(n) for n in nodes), "OpenCTI playbooks"
        )

    async def list_opencti_background_tasks() -> str:
        data, err = await _gql(_BACKGROUND_TASKS_QUERY, {"first": _TASK_LIMIT})
        if err:
            return f"Background tasks: {err}"
        nodes = _edges(data, "backgroundTasks")
        if not nodes:
            return "Background tasks: none on this OpenCTI."
        running = sum(1 for n in nodes if not n.get("completed"))
        failed = sum(1 for n in nodes if n.get("errors"))
        head = f"Background tasks (newest {len(nodes)}; {running} in progress, {failed} with errors):"
        return wrap_untrusted(head + "\n" + "\n".join(_task_line(n) for n in nodes), "OpenCTI background tasks")

    async def _read_managers() -> tuple[list[dict[str, Any]], str | None]:
        data, err = await _gql(_MANAGERS_QUERY)
        if err:
            return [], err
        nodes = [n for n in ((data.get("settings") or {}).get("platform_modules") or []) if isinstance(n, dict)]
        return nodes, None

    async def get_opencti_managers() -> str:
        nodes, err = await _read_managers()
        if err:
            return f"Managers: {err}"
        if not nodes:
            return "Managers: none reported by this OpenCTI."
        first_down = _stream_down(nodes)
        down, reads = set(first_down), 1
        while down and reads < _MANAGER_READS:
            await asyncio.sleep(_MANAGER_REREAD_SECONDS)
            again, err = await _read_managers()
            if err:
                break
            down &= _stream_down(again)
            reads += 1
        streams = sum(1 for n in nodes if manager_kind(str(n.get("id") or "")) == "stream")
        lines = [
            f"Managers ({len(nodes)}: {streams} stream, {len(nodes) - streams} cron or unknown).",
            _MANAGERS_RUNNING_MEANING,
        ]
        if reads < _MANAGER_READS and down:
            lines.append(
                f"Enabled stream managers not running, re-read stopped after {reads} of"
                f" {_MANAGER_READS} reads ({err}): {', '.join(sorted(down))}; not conclusive."
            )
        elif down:
            lines.append(
                f"Enabled stream managers not running on all {_MANAGER_READS} reads,"
                f" {_MANAGER_REREAD_SECONDS} s apart: {', '.join(sorted(down))}."
            )
        else:
            lines.append(f"Enabled stream managers not running on all {_MANAGER_READS} reads: none.")
        recovered = first_down - down
        if recovered:
            lines.append(
                f"Not running on the first read, running again within {reads} reads: {', '.join(sorted(recovered))}."
            )
        return "\n".join([*lines, *(_manager_line(n) for n in nodes)])

    async def get_opencti_platform_queues() -> str:
        data, err = await _gql(_QUEUES_QUERY)
        if err:
            return f"Platform queues: {err}"
        metrics = data.get("rabbitMQMetrics") or {}
        totals = (metrics.get("overview") or {}).get("queue_totals") or {}
        return (
            "Platform queues (all queues together):"
            f" consumers {metrics.get('consumers') if metrics.get('consumers') is not None else '?'},"
            f" messages {totals.get('messages', '?')},"
            f" ready {totals.get('messages_ready', '?')},"
            f" unacknowledged {totals.get('messages_unacknowledged', '?')}"
        )

    async def list_opencti_stream_consumers() -> str:
        data, err = await _gql(_STREAMS_QUERY, {"first": _LIMIT})
        if err and "consumers" in err:
            # An OpenCTI without the consumers field: the collections still answer.
            data, err = await _gql(_STREAMS_QUERY_NO_CONSUMERS, {"first": _LIMIT})
        if err:
            return f"Streams: {err}"
        nodes = _edges(data, "streamCollections")
        if not nodes:
            return "Streams: no live stream collection on this OpenCTI."
        lines = _cut([_stream_line(n) for n in nodes])
        return wrap_untrusted(f"Stream collections ({len(nodes)}):\n" + "\n".join(lines), "OpenCTI streams")

    async def get_opencti_licence() -> str:
        data, err = await _gql(_LICENCE_QUERY)
        if err:
            return f"Licence: {err}"
        ee = (data.get("settings") or {}).get("platform_enterprise_edition") or {}
        if not ee:
            return "Licence: no enterprise licence reported by this OpenCTI."
        return (
            f"Licence: type {ee.get('license_type') or '?'},"
            f" validated {bool(ee.get('license_validated'))}, expired {bool(ee.get('license_expired'))},"
            f" expires {ee.get('license_expiration_date') or 'never'}"
        )

    async def get_opencti_data_health() -> str:
        window = _window()
        lines: list[str] = []
        for field, label in (("created_at", "Objects created"), ("updated_at", "Objects updated")):
            data, err = await _gql(_SERIES_QUERY, {"field": field, **window})
            lines.append(f"{label}: {err}" if err else _series_line(label, data.get("stixCoreObjectsTimeSeries")))
        data, err = await _gql(_DATA_TOTALS_QUERY)
        if err:
            lines.append(f"Totals: {err}")
        else:
            number = data.get("stixCoreObjectsNumber") or {}
            newest = next(iter(_edges(data, "stixCoreObjects")), {}).get("created_at")
            lines.append(f"Objects in total: {number.get('total', '?')}, newest created {newest or 'never'}")
        data, err = await _gql(_WORKS_QUERY, {"first": _WORK_SAMPLE})
        if err:
            lines.append(f"Works: {err}")
        else:
            works = _edges(data, "works")
            errored = sum(1 for w in works if w.get("errors"))
            timed_out = sum(1 for w in works if w.get("status") == "timeout")
            lines.append(f"Works (newest {len(works)}, platform-wide): {errored} with errors, {timed_out} timed out")
        return f"OpenCTI data health (last {_DAYS} days):\n" + "\n".join(lines)

    async def get_opencti_error_rate() -> str:
        data, err = await _gql(_ERROR_RATE_QUERY, {**_window(), "filters": _ERROR_FILTER})
        if err:
            return f"Error rate: {err}"
        return "OpenCTI " + _series_line("activity entries in error", data.get("auditsTimeSeries"))

    executor.register("list_opencti_connector_states", list_opencti_connector_states)
    executor.register("list_opencti_feed_states", list_opencti_feed_states)
    executor.register("list_opencti_playbook_states", list_opencti_playbook_states)
    executor.register("list_opencti_background_tasks", list_opencti_background_tasks)
    executor.register("get_opencti_managers", get_opencti_managers)
    executor.register("get_opencti_platform_queues", get_opencti_platform_queues)
    executor.register("list_opencti_stream_consumers", list_opencti_stream_consumers)
    executor.register("get_opencti_licence", get_opencti_licence)
    executor.register("get_opencti_data_health", get_opencti_data_health)
    executor.register("get_opencti_error_rate", get_opencti_error_rate)

    return [
        ToolDefinition(
            name="list_opencti_connector_states",
            description="Read-only: every OpenCTI connector with its active flag, type, last update and queued messages.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencti_feed_states",
            description="Read-only: OpenCTI RSS, CSV, JSON and TAXII feeds with running flag and last execution.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencti_playbook_states",
            description="Read-only: OpenCTI playbooks with their running flag and queued messages.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencti_background_tasks",
            description="Read-only: the newest OpenCTI background tasks with progress, completion and errors.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencti_managers",
            description="Read-only: OpenCTI platform managers with enabled, running and warning flags, classed stream or cron.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencti_platform_queues",
            description="Read-only: OpenCTI workers consuming and messages queued, platform-wide totals.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencti_stream_consumers",
            description="Read-only: OpenCTI live stream collections and their consumers' time lag.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencti_licence",
            description="Read-only: OpenCTI enterprise licence state and expiry date.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencti_data_health",
            description="Read-only: OpenCTI objects created and updated per day over 7 days, totals, newest works in error.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencti_error_rate",
            description="Read-only: OpenCTI activity log entries in error per day over the last 7 days.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
    ]
