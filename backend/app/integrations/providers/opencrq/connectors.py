"""OpenCRQ connector tools.

* ``list_opencrq_connectors``      - every connector with its health, last
                                     sync and schedule.
* ``list_opencrq_connector_syncs`` - one connector's recent syncs, or the
                                     state of the OpenCTI live stream.
* ``configure_opencrq_connector``  - bind a service account (and the OpenCTI
                                     live stream), switch the OpenCTI stream
                                     on or off, set a run schedule.
* ``run_opencrq_connector``        - sync now, reconnect the stream or reset
                                     the state.

The writes are OpenCRQ's own procedures (integrations-router.ts configure,
opencti-router.ts configure and setEnabled, connectors-router.ts setSchedule,
syncNow, reconnectStream, resetState); OpenCRQ checks the
manage-security capability on each and its refusal is returned as is.
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

#: SCHEDULE_CADENCES (module job-schedules/cadence.ts).
SCHEDULE_CADENCES = ["manual", "hourly", "every_6_hours", "every_12_hours", "daily", "weekly"]
_TIMED_CADENCES = ("daily", "weekly")

#: action -> what the call does, in words.
_RUN_ACTIONS: dict[str, str] = {
    "sync_now": "sync now",
    "reconnect_stream": "reconnect the stream at its live tail",
    "reset_state": "reset the connector state",
    "run_inventory_now": "run the full asset inventory now",
}

#: The connectors whose run reads the whole asset inventory apart from its
#: incremental reads (connectors-router.ts INVENTORY_SOURCES).
_INVENTORY_SOURCES = ("openaev",)

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


def _inventory_line(inventory: object) -> str:
    """The full inventory read of a connector (getConnector.inventory), or ''."""
    if not isinstance(inventory, dict):
        return ""
    requested = inventory.get("requestedAt")
    due = inventory.get("nextDueAt")
    line = f"Inventory: last completed {inventory.get('lastCompletedAt') or 'never'}"
    if requested:
        line += f", requested {requested} (the next run reads it)"
    elif due:
        line += f", next due {due}"
    else:
        line += ", owed to the next run"
    return line


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
        inventory = ""
        if source in _INVENTORY_SOURCES:
            # Best effort: a failed read leaves the runs as listRuns answered them.
            detail, detail_err = await _trpc(
                base_url,
                headers,
                "connectors.getConnector",
                "get connector",
                input={"source": source},
                tenant_id=tenant,
            )
            if not detail_err and isinstance(detail, dict):
                inventory = _inventory_line(detail.get("inventory"))
        if not in_progress and not completed:
            answer = f"The {source} connector has not synced yet."
            return f"{answer}\n{inventory}" if inventory else answer
        lines = [f"{source} connector syncs:"]
        if inventory:
            lines.append(inventory)
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

    async def configure_opencrq_connector(
        source: str = "",
        service_account_id: str = "",
        new_service_account_name: str = "",
        stream_id: str = "",
        enabled: bool | None = None,
        cadence: str = "",
        at_minute: int | None = None,
        weekday: int | None = None,
        time_zone: str = "",
        tenant_id: str = "",
    ) -> str:
        source = _text(source).lower()
        if source not in CONNECTOR_SOURCES:
            return "source must be one of: " + ", ".join(CONNECTOR_SOURCES) + "."
        account = _text(service_account_id)
        new_account = _text(new_service_account_name)
        stream = _text(stream_id)
        cadence = _text(cadence).lower()
        if account and new_account:
            return "Give service_account_id or new_service_account_name, not both."
        bind = bool(account or new_account)
        if source == _STREAM_SOURCE and bind and not stream:
            return "Binding opencti also needs stream_id (an OpenCTI live stream id)."
        if stream and source != _STREAM_SOURCE:
            return "stream_id applies to opencti only."
        if enabled is not None and source != _STREAM_SOURCE:
            return (
                f"OpenCRQ has no on/off switch for {source}: it runs once a service account "
                "is bound and on its schedule (cadence manual stops the scheduled runs)."
            )
        schedule: dict[str, object] | None = None
        if cadence:
            if source == _STREAM_SOURCE:
                return "opencti is a live stream and has no run schedule."
            if cadence not in SCHEDULE_CADENCES:
                return "cadence must be one of: " + ", ".join(SCHEDULE_CADENCES) + "."
            timed = cadence in _TIMED_CADENCES
            minute = _as_int(at_minute) if at_minute is not None else None
            day = _as_int(weekday) if weekday is not None else None
            zone = _text(time_zone)
            if timed and (minute is None or not 0 <= minute < 1440 or not zone):
                return f"cadence {cadence} needs at_minute (0-1439, minute of the day) and time_zone (IANA)."
            if not timed and (minute is not None or zone):
                return f"cadence {cadence} takes no at_minute or time_zone."
            if cadence == "weekly" and (day is None or not 0 <= day <= 6):
                return "cadence weekly needs weekday (0 Sunday to 6 Saturday)."
            if cadence != "weekly" and day is not None:
                return f"cadence {cadence} takes no weekday."
            schedule = {
                "source": source,
                "cadence": cadence,
                "atMinute": minute if timed else None,
                "weekday": day if cadence == "weekly" else None,
                "timeZone": zone if timed else None,
            }
        if not bind and enabled is None and schedule is None:
            return (
                "Nothing to change: give a service account (service_account_id or "
                "new_service_account_name), enabled (opencti) or a cadence."
            )
        tenant, err = await _resolve_tenant(base_url, headers, _text(tenant_id))
        if err or tenant is None:
            return err or "No OpenCRQ tenant resolved."
        done: list[str] = []

        def _partial(error: object) -> str:
            applied = f" Already applied: {'; '.join(done)}." if done else ""
            return f"{error}{applied}"

        if bind:
            choice: dict[str, object] = (
                {"newServiceAccount": {"name": new_account[:255]}} if new_account else {"serviceAccountId": account}
            )
            # OpenCTI is bound with its live stream through its own router; every
            # other connector is an INTEGRATION_SOURCES entry of integrations.configure.
            if source == _STREAM_SOURCE:
                bound, err = await _trpc(
                    base_url,
                    headers,
                    "opencti.configure",
                    "configure connector",
                    input={**choice, "streamId": stream},
                    mutation=True,
                    tenant_id=tenant,
                )
            else:
                bound, err = await _trpc(
                    base_url,
                    headers,
                    "integrations.configure",
                    "configure connector",
                    input={"source": source, **choice},
                    mutation=True,
                    tenant_id=tenant,
                )
            if err:
                return _partial(err)
            bound_id = (bound or {}).get("serviceAccountId") if isinstance(bound, dict) else None
            line = f"service account {bound_id or account or 'none'} bound"
            if new_account:
                line += f" (created as {new_account[:80]})"
            if stream:
                line += f", live stream {stream[:80]}"
            done.append(line)
        if enabled is not None:
            _, err = await _trpc(
                base_url,
                headers,
                "opencti.setEnabled",
                "switch connector",
                input={"enabled": bool(enabled)},
                mutation=True,
                tenant_id=tenant,
            )
            if err:
                return _partial(err)
            done.append("switched on" if enabled else "switched off")
        if schedule is not None:
            _, err = await _trpc(
                base_url,
                headers,
                "connectors.setSchedule",
                "set connector schedule",
                input=schedule,
                mutation=True,
                tenant_id=tenant,
            )
            if err:
                return _partial(err)
            done.append(f"schedule {cadence}")
        return f"OpenCRQ connector {source}: {'; '.join(done)}. list_opencrq_connectors reads its state now."

    async def run_opencrq_connector(source: str = "", action: str = "", tenant_id: str = "") -> str:
        source = _text(source).lower()
        if source not in CONNECTOR_SOURCES:
            return "source must be one of: " + ", ".join(CONNECTOR_SOURCES) + "."
        action = _text(action).lower()
        if action not in _RUN_ACTIONS:
            return "action must be one of: " + ", ".join(_RUN_ACTIONS) + "."
        if source == _STREAM_SOURCE and action == "sync_now":
            return "opencti is a live stream: use reconnect_stream or reset_state."
        if source != _STREAM_SOURCE and action == "reconnect_stream":
            return f"{source} runs as a job and holds no stream: use sync_now."
        if action == "run_inventory_now" and source not in _INVENTORY_SOURCES:
            return "run_inventory_now applies to: " + ", ".join(_INVENTORY_SOURCES) + "."
        tenant, err = await _resolve_tenant(base_url, headers, _text(tenant_id))
        if err or tenant is None:
            return err or "No OpenCRQ tenant resolved."
        words = _RUN_ACTIONS[action]
        payload = {"source": source}
        # One literal procedure per action: dump_opencrq_tool_procedures reads them off the source.
        if action == "sync_now":
            answer, err = await _trpc(
                base_url,
                headers,
                "connectors.syncNow",
                words,
                input=payload,
                mutation=True,
                tenant_id=tenant,
            )
        elif action == "run_inventory_now":
            answer, err = await _trpc(
                base_url,
                headers,
                "connectors.runInventoryNow",
                words,
                input=payload,
                mutation=True,
                tenant_id=tenant,
            )
        elif action == "reconnect_stream":
            answer, err = await _trpc(
                base_url,
                headers,
                "connectors.reconnectStream",
                words,
                input=payload,
                mutation=True,
                tenant_id=tenant,
            )
        else:
            answer, err = await _trpc(
                base_url,
                headers,
                "connectors.resetState",
                words,
                input=payload,
                mutation=True,
                tenant_id=tenant,
            )
        if err:
            return err
        job = answer.get("jobId") if isinstance(answer, dict) else None
        line = f"OpenCRQ connector {source}: {words} done."
        if job:
            line += f" Run id: {job}; list_opencrq_connector_syncs follows it."
        return line

    executor.register("list_opencrq_connectors", list_opencrq_connectors)
    executor.register("list_opencrq_connector_syncs", list_opencrq_connector_syncs)
    executor.register("configure_opencrq_connector", configure_opencrq_connector)
    executor.register("run_opencrq_connector", run_opencrq_connector)

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
        ToolDefinition(
            name="configure_opencrq_connector",
            description=(
                "Configure an OpenCRQ connector: bind the service account it runs as "
                "(opencti also takes its live stream), switch the OpenCTI stream on "
                "or off, or set its run schedule. Needs the manage-security capability."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": CONNECTOR_SOURCES},
                    "service_account_id": {
                        "type": "string",
                        "description": "Existing service account UUID (list_opencrq_service_accounts).",
                    },
                    "new_service_account_name": {
                        "type": "string",
                        "description": "Create the service account under this name and bind it.",
                    },
                    "stream_id": {
                        "type": "string",
                        "description": "opencti only: the OpenCTI live stream id, with the account.",
                    },
                    "enabled": {"type": "boolean", "description": "opencti only: stream on or off."},
                    "cadence": {"type": "string", "enum": SCHEDULE_CADENCES},
                    "at_minute": {
                        "type": "integer",
                        "description": "daily and weekly: minute of the day, 0-1439.",
                    },
                    "weekday": {"type": "integer", "description": "weekly: 0 Sunday to 6 Saturday."},
                    "time_zone": {"type": "string", "description": "daily and weekly: IANA zone."},
                    **TENANT_PROP,
                },
                "required": ["source"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="run_opencrq_connector",
            description=(
                "Act on an OpenCRQ connector now: sync_now, run_inventory_now (openaev: full "
                "asset re-read), reconnect_stream (opencti) or reset_state. Confirm with the "
                "user; manage-security for all but sync_now."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "source": {"type": "string", "enum": CONNECTOR_SOURCES},
                    "action": {"type": "string", "enum": list(_RUN_ACTIONS)},
                    **TENANT_PROP,
                },
                "required": ["source", "action"],
            },
            source=_SOURCE,
        ),
    ]
