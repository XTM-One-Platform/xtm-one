"""OpenCRQ connector status tools (read-only).

* ``list_opencrq_connectors``      - every connector with its health, last
                                     sync and schedule.
* ``list_opencrq_connector_syncs`` - one connector's recent syncs, or the
                                     state of the OpenCTI live stream.
"""

from __future__ import annotations

import logging
import re

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    OFFSET_PROP,
    TENANT_PROP,
    _as_int,
    _grc_link,
    _page,
    _resolve_tenant,
    _text,
    _trpc,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

CONNECTOR_SOURCES = [
    "first",
    "nist",
    "cisa",
    "microsoft_graph",
    "openaev",
    "drata",
    "opencti",
]

#: The connector OpenCRQ reads as a live stream rather than as scheduled runs.
_STREAM_SOURCE = "opencti"

_COMPLETED_PAGE = 10

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]+")


def _slug(source: str) -> str:
    return source.replace("_", "-")


def _state_reason(c: dict) -> str:
    """Why a connector stands where it does, from the flags OpenCRQ answers.

    OpenCRQ derives the state in order, first match wins (module
    connectors/status.ts): no deployment credentials, then no tenant
    binding, then the switch, then the runs. Unknown flags give no reason.
    """
    state = c.get("state")
    stream = c.get("source") == _STREAM_SOURCE
    if state == "unavailable" and c.get("platformConfigured") is False:
        return "the OpenCRQ deployment holds no credentials for it for this tenant"
    if state == "not_configured" and c.get("tenantConfigured") is False:
        if stream:
            return "no live stream and service account selected in this tenant"
        return "no service account bound in this tenant"
    if state == "never_synced":
        if c.get("enabled") is False:
            return "switched off in this tenant"
        if not stream:
            return "configured, no sync has finished yet"
    if state == "failing":
        if stream:
            return "OpenCTI rejected the stream token"
        return "the last sync failed"
    return ""


def _missing_capability_line(stream: dict[str, object]) -> str:
    """The OpenCTI capability OpenCRQ reports its stream token lacks, or ''.

    connectors-router.ts getStreamState answers ``missingCapability`` while
    OpenCRQ holds no stream because the deployment's OpenCTI token cannot
    create it; the capability is what an OpenCTI admin grants to fix it.
    """
    capability = _CONTROL_CHARACTERS.sub(" ", str(stream.get("missingCapability") or "")).strip()
    if not capability:
        return ""
    return f"the OpenCTI token lacks the {capability[:80]} capability to create the stream"


def _fmt_run(r: dict) -> str:
    line = f"- {r.get('status', '?')}, enqueued {r.get('enqueuedAt') or '?'}"
    if r.get("finishedAt"):
        line += f", finished {r['finishedAt']}"
    if r.get("processedTotal") is not None and r.get("expectedTotal") is not None:
        line += f", {r['processedTotal']}/{r['expectedTotal']} processed"
    if r.get("triggeredBy"):
        line += f", triggered by {r['triggeredBy']}"
    if r.get("errorCount"):
        line += f", {r['errorCount']} errors"
    if r.get("lastError"):
        # An upstream message, kept to one line.
        error = _CONTROL_CHARACTERS.sub(" ", str(r["lastError"])).strip()
        line += f": {error[:200]}"
    return line + f" (run id: {r.get('id')})"


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    async def list_opencrq_connectors(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, _text(tenant_id))
        if err:
            return err
        connectors, err = await _trpc(
            base_url, headers, "connectors.listConnectors", "list connectors",
            tenant_id=tenant,
        )
        if err:
            return err
        connectors = connectors or []
        if not connectors:
            return "No OpenCRQ connector."
        # listConnectors does not carry the stream's missing capability: one
        # getStreamState read adds it to the OpenCTI row. Best effort: a failed
        # read leaves the row as listConnectors answered it.
        stream_missing = ""
        if any(c.get("source") == _STREAM_SOURCE for c in connectors):
            stream, stream_err = await _trpc(
                base_url,
                headers,
                "connectors.getStreamState",
                "get connector stream state",
                input={"source": _STREAM_SOURCE},
                tenant_id=tenant or "",
            )
            if not stream_err and isinstance(stream, dict):
                stream_missing = _missing_capability_line(stream)
        lines = [f"OpenCRQ connectors ({len(connectors)}):"]
        for c in connectors:
            source = c.get("source", "?")
            line = (
                f"- {source} ({c.get('domain', '?')}, {c.get('mechanism', '?')}): "
                f"{c.get('state', '?')}"
            )
            reason = _state_reason(c)
            if source == _STREAM_SOURCE and stream_missing:
                reason = f"{reason}; {stream_missing}" if reason else stream_missing
            if reason:
                line += f" ({reason})"
            last = c.get("lastRun") or {}
            if last:
                line += (
                    f"; last sync {last.get('status', '?')}"
                    f" {last.get('finishedAt') or last.get('startedAt') or last.get('enqueuedAt') or ''}".rstrip()
                )
            if c.get("activeRun"):
                line += "; a sync is running now"
            schedule = c.get("schedule") or {}
            if schedule.get("cadence"):
                line += f"; schedule {schedule['cadence']}"
                if schedule.get("nextRunAt"):
                    line += f", next {schedule['nextRunAt']}"
            if c.get("scopeKinds"):
                line += f"; feeds {', '.join(c['scopeKinds'])}"
            lines.append(line)
            link = _grc_link(base_url, tenant, "connector", _slug(source))
            if link and c.get("state") not in ("unavailable", "not_configured"):
                lines.append(link)
        return "\n".join(lines)

    async def list_opencrq_connector_syncs(
        source: str = "", offset: int = 0, tenant_id: str = ""
    ) -> str:
        source = _text(source).lower()
        if source not in CONNECTOR_SOURCES:
            return "source must be one of: " + ", ".join(CONNECTOR_SOURCES) + "."
        offset = _as_int(offset)
        if offset is None or offset < 0:
            return "offset must be an integer >= 0."
        tenant, err = await _resolve_tenant(base_url, headers, _text(tenant_id))
        if err:
            return err
        if source == _STREAM_SOURCE:
            stream, err = await _trpc(
                base_url, headers, "connectors.getStreamState",
                "get connector stream state", input={"source": source},
                tenant_id=tenant,
            )
            if err:
                return err
            stream = stream or {}
            lines = [
                f"{source} live stream: {stream.get('connection', '?')}",
                f"  Last event: {stream.get('lastEventAt') or 'none'}",
                f"  Reached the live tail: {'yes' if stream.get('reachedLiveTail') else 'no'}",
            ]
            missing = _missing_capability_line(stream)
            if missing:
                lines.append(f"  Missing capability: {missing}")
            if stream.get("backfillPercent") is not None:
                lines.append(f"  Backfill: {stream['backfillPercent']}%")
            if stream.get("objectsRead") is not None:
                lines.append(
                    f"  Objects read: {stream['objectsRead']}, held: "
                    f"{stream.get('objectsHeld', '?')}"
                )
            return "\n".join(lines)
        runs, err = await _trpc(
            base_url, headers, "connectors.listRuns", "list connector syncs",
            input={"source": source}, tenant_id=tenant,
        )
        if err:
            return err
        runs = runs or {}
        in_progress = runs.get("inProgress") or []
        completed = runs.get("completed") or []
        if not in_progress and not completed:
            return f"The {source} connector has not synced yet."
        lines = [f"{source} connector syncs:"]
        if in_progress:
            lines.append(f"In progress ({runs.get('inProgressCount', len(in_progress))}):")
            lines += [_fmt_run(r) for r in in_progress]
        if completed:
            # OpenCRQ answers the latest 50 completed syncs and counts them all.
            shown, more = _page(completed, offset, _COMPLETED_PAGE)
            lines.append(
                f"Completed ({runs.get('completedCount', len(completed))}, latest first):"
            )
            lines += [_fmt_run(r) for r in shown]
            if more:
                lines.append(more)
        return "\n".join(lines)

    executor.register("list_opencrq_connectors", list_opencrq_connectors)
    executor.register("list_opencrq_connector_syncs", list_opencrq_connector_syncs)

    return [
        ToolDefinition(
            name="list_opencrq_connectors",
            description=(
                "List the OpenCRQ connectors (CVE knowledge from NIST, FIRST, "
                "CISA; assets and findings from Microsoft Graph; simulations "
                "from OpenAEV; compliance from Drata; threat intelligence from "
                "OpenCTI) with their health, last sync and schedule."
            ),
            input_schema={"type": "object", "properties": {**TENANT_PROP}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_connector_syncs",
            description=(
                "List an OpenCRQ connector's recent syncs (status, progress, "
                "errors), or for OpenCTI the state of its live stream. Use it "
                "to explain a failing or stale connector."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": CONNECTOR_SOURCES},
                    **OFFSET_PROP,
                    **TENANT_PROP,
                },
                "required": ["source"],
            },
            source=_SOURCE,
        ),
    ]
