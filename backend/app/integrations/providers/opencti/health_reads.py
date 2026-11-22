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
  warning flags (``settings.platform_modules``).
* ``get_opencti_platform_queues``: workers consuming and messages queued, platform
  totals only (root ``rabbitMQMetrics``, MODULES); no queue is named.
* ``list_opencti_stream_consumers``: live stream collections and their consumers'
  time lag (root ``streamCollections``; without the ``consumers`` field, the
  collections alone).
* ``get_opencti_licence``: the enterprise licence state and expiry
  (``settings.platform_enterprise_edition``; OpenCTI 6.0 or later).

A module that lacks a field answers the error line, and the checker reports the
check that needs it as ``not_run``.

A root the user's role cannot read answers its own error line and the others
still answer. Names, URIs and descriptions are text the platform's users wrote:
the listing is returned wrapped as untrusted content.
"""

from __future__ import annotations

import logging
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


def _manager_line(node: dict[str, Any]) -> str:
    enabled = "enabled" if node.get("enable") else "disabled"
    running = "running" if node.get("running") else "not running"
    warning = ", warning" if node.get("warning") else ""
    return f"- {node.get('id') or '?'} [{enabled}, {running}{warning}]"


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

    async def get_opencti_managers() -> str:
        data, err = await _gql(_MANAGERS_QUERY)
        if err:
            return f"Managers: {err}"
        nodes = [n for n in ((data.get("settings") or {}).get("platform_modules") or []) if isinstance(n, dict)]
        if not nodes:
            return "Managers: none reported by this OpenCTI."
        down = sum(1 for n in nodes if n.get("enable") and not n.get("running"))
        return f"Managers ({len(nodes)}; {down} enabled and not running):\n" + "\n".join(
            _manager_line(n) for n in nodes
        )

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

    executor.register("list_opencti_connector_states", list_opencti_connector_states)
    executor.register("list_opencti_feed_states", list_opencti_feed_states)
    executor.register("list_opencti_playbook_states", list_opencti_playbook_states)
    executor.register("list_opencti_background_tasks", list_opencti_background_tasks)
    executor.register("get_opencti_managers", get_opencti_managers)
    executor.register("get_opencti_platform_queues", get_opencti_platform_queues)
    executor.register("list_opencti_stream_consumers", list_opencti_stream_consumers)
    executor.register("get_opencti_licence", get_opencti_licence)

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
            description="Read-only: OpenCTI platform managers with their enabled, running and warning flags.",
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
    ]
