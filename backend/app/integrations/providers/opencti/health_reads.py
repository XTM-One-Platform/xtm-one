"""Read-only OpenCTI state reads for the Platform Health Module Analyst (#101 S14a-2b).

Today's OpenCTI tools reach connectors, feeds and playbooks only through the
``manage_*`` tools, whose names classify as writes, so the analyst's allow-list
(``services/health/module_analyst.MODULE_ANALYST_TOOLS``) could not list them.
These three tools read the same objects and nothing else: no argument selects a
mutation, no action switch exists.

* ``list_opencti_connector_states``: every connector with its active flag, type,
  last update and the messages waiting in its queue (root ``connectors``, MODULES).
* ``list_opencti_feed_states``: RSS, CSV, JSON and TAXII feeds with their running
  flag and last execution (roots ``ingestion*``, INGESTION).
* ``list_opencti_playbook_states``: playbooks with their running flag and queued
  messages (root ``playbooks``, AUTOMATION).

A root the user's role cannot read answers its own error line and the others
still answer. Names, URIs and descriptions are text the platform's users wrote:
the listing is returned wrapped as untrusted content.
"""

from __future__ import annotations

import logging
from typing import Final

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

    executor.register("list_opencti_connector_states", list_opencti_connector_states)
    executor.register("list_opencti_feed_states", list_opencti_feed_states)
    executor.register("list_opencti_playbook_states", list_opencti_playbook_states)

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
    ]
