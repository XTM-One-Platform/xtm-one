# SPDX-License-Identifier: Apache-2.0

"""OpenAEV simulation (exercise) lifecycle + scenario lifecycle tools.

* ``manage_openaev_simulation`` - simulation/exercise CRUD, duplicate, status
  control (start/pause/stop), start-date, results, and team/player membership
  (``ExerciseApi``).
* ``manage_openaev_scenario`` - scenario lifecycle gaps: launch a running
  simulation, schedule recurrence, duplicate, healthchecks, and team/player
  membership (``ScenarioApi``). Scenario create/update/delete remain in the
  core tools.
* ``export_openaev_scenario`` - export a scenario to a portable ``.zip``
  archive (the artifact a user imports on another platform).
* ``import_openaev_scenario`` - import a scenario from such an archive and
  report ``missingActions`` (the injector capabilities the target lacks).

Simulations EXECUTE against live assets - launch/status are real actions.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

from app.core.egress import egress_client
from app.integrations.file_delivery import (
    FetchError,
    PersistFileFn,
    deliver_file,
    fetch_file,
    read_pipe_file,
)
from app.integrations.http import safe_request
from app.services.content_safety import wrap_untrusted
from app.tools.base import (
    CallableToolExecutor,
    FileContext,
    ToolDefinition,
    ToolResult,
)

from ._common import (
    _PROVIDER,
    _aev_link,
    _api,
    _ensure_fresh_token,
    _require_response,
    _scope_path,
    _search_body,
    _simulation_inject_status_lines,
    _simulation_state_lines,
    _with_scope_hint,
    findings_fate,
    json_answer,
    keep_findings_params,
    keep_findings_schema,
    simulation_results_answer,
)
from .read_scope import area_scope, count_text, with_scope

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"
_EXERCISE_STATUSES = ("SCHEDULED", "RUNNING", "PAUSED", "FINISHED", "CANCELED")

#: Largest scenario export this tool downloads or imports.
_MAX_SCENARIO_BYTES = 25 * 1024 * 1024


def _as_bool(value: object) -> bool:
    """A real boolean for a flag that may arrive as the string ``"false"``.

    ``bool("false")`` is ``True``, so a model that passes the OpenAEV query
    literal ``"false"`` would otherwise flip the flag on. Only ``True`` or a
    case-insensitive ``"true"`` is truthy here.
    """
    return value is True or str(value).strip().lower() == "true"


def _fmt_exercise(e: dict, base_url: str = "") -> str:
    eid = e.get("exercise_id", "N/A")
    lines = [
        f"[Simulation] {e.get('exercise_name', '?')}",
        f"  ID: {eid}",
        f"  status: {e.get('exercise_status', '?')}",
    ]
    if e.get("exercise_category"):
        lines.append(f"  category: {e.get('exercise_category')}")
    if e.get("exercise_severity"):
        lines.append(f"  severity: {e.get('exercise_severity')}")
    lines.extend(_simulation_state_lines(e))
    link = _aev_link(base_url, "exercise", eid)
    if link:
        lines.append(link)
    return "\n".join(lines)


def register(
    base_url: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    persist_file_fn: PersistFileFn | None = None,
) -> list[ToolDefinition]:
    def _exercise_input(
        name: str,
        subtitle: str,
        description: str,
        category: str,
        main_focus: str,
        severity: str,
        tags: list[str] | None,
    ) -> dict:
        body: dict = {}
        if name:
            body["exercise_name"] = name
        if subtitle:
            body["exercise_subtitle"] = subtitle
        if description:
            body["exercise_description"] = description
        if category:
            body["exercise_category"] = category
        if main_focus:
            body["exercise_main_focus"] = main_focus
        if severity:
            body["exercise_severity"] = severity
        if tags is not None:
            body["exercise_tags"] = tags
        return body

    async def manage_openaev_simulation(
        action: str = "",
        exercise_id: str = "",
        name: str = "",
        subtitle: str = "",
        description: str = "",
        category: str = "",
        main_focus: str = "",
        severity: str = "",
        tags: list[str] | None = None,
        status: str = "",
        start_date: str = "",
        team_id: str = "",
        team_ids: list[str] | None = None,
        player_ids: list[str] | None = None,
        search: str = "",
        limit: int = 20,
        keep_findings: bool = True,
    ) -> str:
        action = (action or "").strip().lower()

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/exercises/search",
                "search simulations",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                # OpenAEV answers an empty page to a grant-limited identity (#1444).
                scope, cause = await area_scope(headers, "assessment")
                return with_scope(
                    "No simulations found" + (": " + count_text(0, scope) if scope != "full" else "") + ".",
                    scope,
                    cause,
                )
            return f"Simulations ({len(items)}):\n" + "\n".join(
                _fmt_exercise(e, base_url) for e in items
            )

        if action == "get":
            if not exercise_id:
                return "exercise_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/exercises/{exercise_id}",
                "get simulation",
            )
            if err:
                return err
            text = _fmt_exercise(_require_response(resp).json(), base_url)
            rows, rows_err = await _api(
                base_url,
                headers,
                "get",
                f"/api/exercises/{exercise_id}/injects/results",
                "read simulation inject statuses",
            )
            if rows_err:
                return text + "\n  Inject statuses: not read (" + rows_err.strip() + ")"
            statuses = _simulation_inject_status_lines(_require_response(rows).json())
            return "\n".join([text, *statuses])

        if action == "create":
            if not name:
                return "name is required for action 'create'."
            body = _exercise_input(
                name, subtitle, description, category, main_focus, severity, tags
            )
            if start_date:
                body["exercise_start_date"] = start_date
            resp, err = await _api(
                base_url, headers, "post", "/api/exercises", "create simulation",
                json=body,
            )
            if err:
                return err
            e = resp.json()
            return f"Created simulation '{e.get('exercise_name')}' (id: {e.get('exercise_id')})."

        if action == "update":
            if not exercise_id:
                return "exercise_id is required for action 'update'."
            body = _exercise_input(
                name, subtitle, description, category, main_focus, severity, tags
            )
            if not body:
                return "Provide at least one field to update."
            resp, err = await _api(
                base_url, headers, "put", f"/api/exercises/{exercise_id}",
                "update simulation", json=body,
            )
            if err:
                return err
            return f"Updated simulation {exercise_id}."

        if action == "delete":
            if not exercise_id:
                return "exercise_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/exercises/{exercise_id}",
                "delete simulation", params=keep_findings_params(keep_findings),
            )
            if err:
                return err
            return f"Deleted simulation {exercise_id}. " + findings_fate(keep_findings, "simulation")

        if action == "duplicate":
            if not exercise_id:
                return "exercise_id is required for action 'duplicate'."
            resp, err = await _api(
                base_url, headers, "post", f"/api/exercises/{exercise_id}",
                "duplicate simulation",
            )
            if err:
                return err
            e = resp.json()
            return f"Duplicated simulation - new id: {e.get('exercise_id')}."

        if action == "status":
            if not exercise_id or not status:
                return (
                    "exercise_id and status are required for 'status'. "
                    f"Valid statuses: {', '.join(_EXERCISE_STATUSES)}."
                )
            status_u = status.strip().upper()
            if status_u not in _EXERCISE_STATUSES:
                return f"status must be one of {', '.join(_EXERCISE_STATUSES)}."
            resp, err = await _api(
                base_url, headers, "put", f"/api/exercises/{exercise_id}/status",
                "set simulation status", json={"exercise_status": status_u},
            )
            if err:
                return err
            return f"Simulation {exercise_id} status set to {status_u}."

        if action == "set_start_date":
            if not exercise_id or not start_date:
                return "exercise_id and start_date (ISO 8601) are required."
            _, err = await _api(
                base_url, headers, "put",
                f"/api/exercises/{exercise_id}/start-date",
                "set simulation start date",
                json={"exercise_start_date": start_date},
            )
            if err:
                return err
            return f"Simulation {exercise_id} start date set to {start_date}."

        if action == "results":
            if not exercise_id:
                return "exercise_id is required for action 'results'."
            return await simulation_results_answer(_api, base_url, headers, exercise_id)

        if action in ("add_teams", "remove_teams"):
            # `team_ids=[]` is meaningful for add_teams (maps to /teams/replace,
            # i.e. clear membership), so only reject a missing list, not empty.
            if not exercise_id or team_ids is None:
                return f"exercise_id and team_ids are required for '{action}'."
            verb = "replace" if action == "add_teams" else "remove"
            _, err = await _api(
                base_url, headers, "put",
                f"/api/exercises/{exercise_id}/teams/{verb}",
                "update simulation teams", json={"exercise_teams": team_ids},
            )
            if err:
                return err
            return f"{'Set' if verb == 'replace' else 'Removed'} teams on simulation {exercise_id}."

        if action in ("enable_players", "disable_players"):
            if not exercise_id or not team_id or not player_ids:
                return (
                    f"exercise_id, team_id and player_ids are required for '{action}'."
                )
            verb = "enable" if action == "enable_players" else "disable"
            _, err = await _api(
                base_url, headers, "put",
                f"/api/exercises/{exercise_id}/teams/{team_id}/players/{verb}",
                "update simulation team players",
                json={"exercise_team_players": player_ids},
            )
            if err:
                return err
            return f"{verb.capitalize()}d {len(player_ids)} players on team {team_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "delete, duplicate, status, set_start_date, results, add_teams, "
            "remove_teams, enable_players, disable_players."
        )

    async def manage_openaev_scenario(
        action: str = "",
        scenario_id: str = "",
        recurrence_cron: str = "",
        recurrence_start: str = "",
        recurrence_end: str = "",
        team_id: str = "",
        team_ids: list[str] | None = None,
        player_ids: list[str] | None = None,
    ) -> str:
        action = (action or "").strip().lower()
        if not scenario_id:
            return "scenario_id is required."

        if action == "launch":
            resp, err = await _api(
                base_url, headers, "post",
                f"/api/scenarios/{scenario_id}/exercise/running",
                "launch scenario",
            )
            if err:
                return err
            launched = resp.json() if resp is not None else None
            launched = launched if isinstance(launched, dict) else {}
            # OpenAEV answers {simulation_id} since its launch-with-targets
            # change; older builds answered the whole exercise.
            sim_id = launched.get("simulation_id") or launched.get("exercise_id")
            if not sim_id:
                return (
                    f"Launched scenario {scenario_id}, but OpenAEV returned no "
                    "simulation id; list the scenario's simulations to find it."
                )
            name = launched.get("exercise_name")
            status = launched.get("exercise_status")
            if "simulation_id" in launched and (not name or not status):
                sim_resp, sim_err = await _api(
                    base_url,
                    headers,
                    "get",
                    f"/api/exercises/{sim_id}",
                    "get simulation",
                )
                sim = sim_resp.json() if not sim_err and sim_resp is not None else None
                if isinstance(sim, dict):
                    name = name or sim.get("exercise_name")
                    status = status or sim.get("exercise_status")
            return (
                f"Launched scenario {scenario_id}. Running simulation created: "
                f"{name or '(name not read)'} (id: {sim_id}, status: "
                f"{status or 'not read'})."
            )

        if action == "schedule_recurrence":
            if not recurrence_cron:
                return "recurrence_cron is required for 'schedule_recurrence'."
            body: dict = {"scenario_recurrence": recurrence_cron}
            if recurrence_start:
                body["scenario_recurrence_start"] = recurrence_start
            if recurrence_end:
                body["scenario_recurrence_end"] = recurrence_end
            _, err = await _api(
                base_url, headers, "put",
                f"/api/scenarios/{scenario_id}/recurrence",
                "schedule scenario recurrence", json=body,
            )
            if err:
                return err
            return f"Scheduled recurrence for scenario {scenario_id}."

        if action == "duplicate":
            resp, err = await _api(
                base_url, headers, "post", f"/api/scenarios/{scenario_id}",
                "duplicate scenario",
            )
            if err:
                return err
            s = resp.json()
            return f"Duplicated scenario - new id: {s.get('scenario_id')}."

        if action == "healthcheck":
            resp, err = await _api(
                base_url, headers, "get",
                f"/api/scenarios/{scenario_id}/healthchecks",
                "scenario healthchecks",
            )
            if err:
                return err
            return f"Scenario {scenario_id} healthchecks:\n{json_answer(resp.json(), 2000)}"

        if action in ("add_teams", "remove_teams"):
            # `team_ids=[]` is meaningful for add_teams (maps to /teams/replace,
            # i.e. clear membership), so only reject a missing list, not empty.
            if team_ids is None:
                return f"team_ids are required for '{action}'."
            verb = "replace" if action == "add_teams" else "remove"
            _, err = await _api(
                base_url, headers, "put",
                f"/api/scenarios/{scenario_id}/teams/{verb}",
                "update scenario teams", json={"scenario_teams": team_ids},
            )
            if err:
                return err
            return f"{'Set' if verb == 'replace' else 'Removed'} teams on scenario {scenario_id}."

        if action in ("enable_players", "disable_players"):
            if not team_id or not player_ids:
                return f"team_id and player_ids are required for '{action}'."
            verb = "enable" if action == "enable_players" else "disable"
            _, err = await _api(
                base_url, headers, "put",
                f"/api/scenarios/{scenario_id}/teams/{team_id}/players/{verb}",
                "update scenario team players",
                json={"scenario_team_players": player_ids},
            )
            if err:
                return err
            return f"{verb.capitalize()}d {len(player_ids)} players on team {team_id}."

        return (
            f"Unknown action '{action}'. Valid actions: launch, schedule_recurrence, "
            "duplicate, healthcheck, add_teams, remove_teams, enable_players, "
            "disable_players."
        )

    async def export_openaev_scenario(
        scenario_id: str = "",
        with_teams: bool = False,
        with_scope_definition: bool = True,
    ) -> str | ToolResult:
        if not scenario_id:
            return "scenario_id is required."
        # This tool is read-classified and runs unattended (no HITL approval), so it
        # must never exfiltrate PII or secrets: player data (emails) and variable
        # values (which can hold secrets) are always excluded from the export.
        # OpenAEV expects lowercase boolean query params (Spring @RequestParam).
        # _as_bool: a model may pass the literal "false", which bool() treats as True.
        params = {
            "isWithTeams": str(_as_bool(with_teams)).lower(),
            "isWithPlayers": "false",
            "isWithVariableValues": "false",
            "isWithScopeDefinition": str(_as_bool(with_scope_definition)).lower(),
        }
        # scenario_id is model-supplied: quote it so a value like "a/../../exercises/b"
        # cannot re-target another OpenAEV route with the caller's token. auth_only
        # drops the tenant resolver that rides on `headers`, so scope the path by hand.
        export_path, scope_hint = await _scope_path(
            headers, f"/api/scenarios/{quote(str(scenario_id), safe='')}/export"
        )
        # auth_only sends a bare bearer through egress_client (no AutoRefreshClient),
        # so proactively re-mint a platform-managed JWT that is about to expire first;
        # otherwise a long run sends an expired token and 401s. No-op for a static token.
        refresh_fn = getattr(headers, "refresh_fn", None)
        if refresh_fn is not None:
            await _ensure_fresh_token(headers, refresh_fn)
        auth_only = {"Authorization": headers.get("Authorization", "")}
        # Stream the archive with a hard cap: fetch_file refuses from the Content-Length
        # and at the first chunk over the limit, so a larger export is never held whole
        # in memory (the size is not known before the request).
        async with egress_client(timeout=60) as client:
            fetched = await fetch_file(
                client, "get", f"{base_url}{export_path}",
                provider=_PROVIDER, action="export scenario",
                max_bytes=_MAX_SCENARIO_BYTES, headers=auth_only, params=params,
            )
        if isinstance(fetched, FetchError):
            return _with_scope_hint(str(fetched), scope_hint)
        if not fetched.data:
            return f"Scenario {scenario_id} returned an empty export."
        # The platform derives the Content-Disposition name from the author-authored
        # scenario name; deliver_file already quarantines it, but a stable,
        # id-derived name is also cleaner for the stored file.
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", str(scenario_id))[:64] or "scenario"
        content_type = (fetched.content_type or "").split(";", 1)[0].strip() or "application/zip"
        return await deliver_file(
            data=fetched.data,
            filename=f"openaev-scenario-{safe_id}.zip",
            content_type=content_type,
            source="OpenAEV",
            persist_file_fn=persist_file_fn,
            mark_unstored=False,
        )

    async def import_openaev_scenario(
        file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        if not (file_id or "").strip():
            return (
                "file_id (a scenario export .zip attached to this conversation) is "
                "required."
            )
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_SCENARIO_BYTES, target="OpenAEV"
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        upload_bytes, piped_name, piped_mime = piped
        filename = (piped_name or "").strip() or "scenario.zip"
        # auth_only drops the tenant resolver that rides on `headers`, so
        # scope the path with the full headers.
        upload_path, scope_hint = await _scope_path(headers, "/api/scenarios/import")
        # Re-mint a platform-managed JWT that is about to expire before sending the
        # bare bearer through egress_client; otherwise a long run 401s. No-op for a
        # static token. (Same gap the streamed export had.)
        refresh_fn = getattr(headers, "refresh_fn", None)
        if refresh_fn is not None:
            await _ensure_fresh_token(headers, refresh_fn)
        auth_only = {"Authorization": headers.get("Authorization", "")}
        files = {"file": (filename, upload_bytes, piped_mime or "application/zip")}
        async with egress_client(timeout=60) as client:
            # tenant-scoped by hand: upload_path
            resp, err = await safe_request(
                client, "post", f"{base_url}{upload_path}", _PROVIDER,
                "import scenario", headers=auth_only, files=files,
            )
        if err:
            message = _with_scope_hint(err, scope_hint)
            # A timeout or transport error is ambiguous: the non-idempotent import may
            # already have been applied server-side before the answer was lost. Tell the
            # model so it does not import the scenario a second time on retry.
            if "timed out" in err or "unreachable" in err or "Could not connect" in err:
                message += (
                    " The import is not idempotent and the scenario may already have "
                    "been created: check the platform before retrying."
                )
            return message
        # The import already happened server-side; a non-JSON 200 (proxy, renamed or
        # dropped body) must not look like a tool failure, or the model retries and
        # imports the scenario twice. Treat an unparseable body as "done, unknown
        # missingActions" rather than raising.
        result: object = {}
        if resp is not None and resp.content:
            try:
                result = resp.json()
            except ValueError:
                result = None
        if not isinstance(result, dict):
            return (
                "Imported the scenario. The platform did not return a readable result, "
                "so its missingActions (the injector capabilities it may lack) could not "
                "be determined - verify the import on the platform."
            )
        missing = result.get("missingActions")
        if missing:
            # missingActions[].name is author-authored text from a shared archive
            # (importing someone else's scenario is the use case), so it only reaches
            # the model inside a quarantine block (repo rule).
            listed = json_answer(missing, 1500)
            return (
                "Imported the scenario. The target platform is MISSING "
                f"{len(missing)} capability/capabilities needed by the scenario "
                "(missingActions; the names come from the archive):\n"
                + wrap_untrusted(listed, source="OpenAEV scenario import result")
            )
        if missing is None:
            # Key absent (older OpenAEV, renamed field): do not claim full coverage.
            return (
                "Imported the scenario. The platform returned no missingActions field, "
                "so whether it has every needed injector capability could not be "
                "confirmed - verify on the platform."
            )
        return (
            "Imported the scenario. missingActions: [] - the target platform has every "
            "injector capability the scenario needs."
        )

    executor.register("manage_openaev_simulation", manage_openaev_simulation)
    executor.register("manage_openaev_scenario", manage_openaev_scenario)
    executor.register("export_openaev_scenario", export_openaev_scenario)
    executor.register("import_openaev_scenario", import_openaev_scenario)

    return [
        ToolDefinition(
            name="manage_openaev_simulation",
            description=(
                "Manage OpenAEV simulations (exercises - the actual runs). Actions: "
                "'list' (search), 'get' (exercise_id), 'create' (name + optional "
                "subtitle/description/category/main_focus/severity/tags/start_date), "
                "'update' (exercise_id + fields), 'delete' (exercise_id, "
                "keep_findings), 'duplicate' (exercise_id), 'status' (exercise_id + "
                "status in SCHEDULED/RUNNING/"
                "PAUSED/FINISHED/CANCELED - RUNNING starts the simulation against live "
                "assets), 'set_start_date' (exercise_id + start_date ISO 8601), "
                "'results' (exercise_id), 'add_teams'/'remove_teams' (exercise_id + "
                "team_ids), 'enable_players'/'disable_players' (exercise_id + team_id + "
                "player_ids)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "get", "create", "update", "delete", "duplicate",
                            "status", "set_start_date", "results", "add_teams",
                            "remove_teams", "enable_players", "disable_players",
                        ],
                    },
                    "exercise_id": {"type": "string"},
                    "name": {"type": "string"},
                    "subtitle": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {"type": "string"},
                    "main_focus": {"type": "string"},
                    "severity": {"type": "string", "description": "low/medium/high/critical."},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Tag IDs."},
                    "status": {"type": "string", "enum": list(_EXERCISE_STATUSES)},
                    "start_date": {"type": "string", "description": "ISO 8601 start datetime."},
                    "team_id": {"type": "string", "description": "Team ID (enable/disable players)."},
                    "team_ids": {"type": "array", "items": {"type": "string"}},
                    "player_ids": {"type": "array", "items": {"type": "string"}},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                    "keep_findings": keep_findings_schema(),
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_scenario",
            description=(
                "Manage OpenAEV scenario lifecycle (create/update/delete are separate "
                "create_openaev_scenario / update_openaev_scenario / "
                "delete_openaev_scenario tools). Actions (all require scenario_id): "
                "'launch' (creates and starts a running simulation from the scenario - "
                "executes against live assets), 'schedule_recurrence' (recurrence_cron "
                "+ optional recurrence_start/recurrence_end), 'duplicate', "
                "'healthcheck' (pre-flight checks), 'add_teams'/'remove_teams' "
                "(team_ids), 'enable_players'/'disable_players' (team_id + player_ids)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "launch", "schedule_recurrence", "duplicate", "healthcheck",
                            "add_teams", "remove_teams", "enable_players",
                            "disable_players",
                        ],
                    },
                    "scenario_id": {"type": "string"},
                    "recurrence_cron": {"type": "string", "description": "Cron expression (schedule_recurrence)."},
                    "recurrence_start": {"type": "string", "description": "ISO 8601 start (optional)."},
                    "recurrence_end": {"type": "string", "description": "ISO 8601 end (optional)."},
                    "team_id": {"type": "string"},
                    "team_ids": {"type": "array", "items": {"type": "string"}},
                    "player_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["action", "scenario_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="export_openaev_scenario",
            description=(
                "Export an OpenAEV scenario to a portable archive (a .zip containing "
                "the scenario JSON) - the artifact a user imports on another platform. "
                "Player data and variable values are never included (the export is "
                "safe to share). Args: scenario_id (required); optional with_teams "
                "(default false) and with_scope_definition (default true)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scenario_id": {"type": "string"},
                    "with_teams": {"type": "boolean", "default": False},
                    "with_scope_definition": {"type": "boolean", "default": True},
                },
                "required": ["scenario_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="import_openaev_scenario",
            description=(
                "Import an OpenAEV scenario from an export archive (.zip) attached to "
                "this conversation. Args: file_id (the attached export file, required). "
                "Returns the import result including missingActions - the injector "
                "contracts the target platform is missing (empty when the platform has "
                "every capability the scenario needs)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "ID of the .zip scenario export attached to this conversation.",
                    },
                },
                "required": ["file_id"],
            },
            source=_SOURCE,
        ),
    ]
