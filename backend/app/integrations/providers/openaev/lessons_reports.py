# SPDX-License-Identifier: Apache-2.0

"""OpenAEV lessons-learned + reporting tools.

* ``manage_openaev_lessons``   - lessons templates + scenario/simulation lessons
  categories/questions/answers + apply-template/send/stats.
* ``manage_openaev_reporting`` - the OpenAEV v3 reporting module: templates,
  manual + scheduled generations, and PDF/HTML download.

The legacy ``manage_openaev_report`` tool targeted ``/api/reports`` and
``/api/exercises/{id}/reports``. Both endpoints were DELETED in OpenAEV v3
(the whole ``ReportApi`` package went away, together with the ``report_*``
field namespace), so every action returned 404. ``/api/reportings`` with the
``reporting_*`` namespace is the replacement surface.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from app.integrations.file_delivery import deliver_file
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._common import (
    REPORTING_CONTEXT_TYPES,
    REPORTING_FORMATS,
    REPORTING_MODULE_TYPES,
    REPORTING_SCHEDULE_PERIODS,
    REPORTING_TIME_RANGES,
    PersistFileFn,
    _api,
    _fmt_reporting,
    _fmt_reporting_generation,
    _search_body,
    json_answer,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"


def _schedule_body(
    extra: dict,
    name: str,
    output_format: str,
    schedule_period: str,
    schedule_time: str,
    schedule_enabled: bool | None,
    recipient_emails: list[str] | None,
    validate: Callable[[str, list[str], str], tuple[str, str]],
) -> tuple[dict, str]:
    """Build a ``ReportingScheduleInput`` body from the tool arguments.

    Only fields the caller actually provided are sent, so updating one
    attribute cannot silently clear the rest of the schedule. Returns
    ``(body, error)``.
    """
    body: dict = dict(extra)
    if name:
        body["reporting_schedule_name"] = name
    if schedule_period:
        value, err = validate(
            schedule_period, REPORTING_SCHEDULE_PERIODS, "schedule_period"
        )
        if err:
            return {}, err
        body["reporting_schedule_period"] = value
    if schedule_time:
        body["reporting_schedule_time"] = schedule_time
    if output_format:
        value, err = validate(output_format, REPORTING_FORMATS, "output_format")
        if err:
            return {}, err
        body["reporting_schedule_format"] = value
    # Boolean flags must be omitted when None so an update never flips a
    # schedule off by accident.
    if schedule_enabled is not None:
        body["reporting_schedule_enabled"] = schedule_enabled
    if recipient_emails is not None:
        body["reporting_schedule_recipient_emails"] = recipient_emails
    return body, ""


def register(
    base_url: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    persist_file_fn: PersistFileFn | None = None,
) -> list[ToolDefinition]:
    async def manage_openaev_lessons(
        action: str = "",
        scope: str = "simulation",
        parent_id: str = "",
        template_id: str = "",
        config: str = "",
    ) -> str:
        action = (action or "").strip().lower()
        scope = (scope or "simulation").strip().lower()
        root = "/api/exercises" if scope == "simulation" else "/api/scenarios"

        extra: dict = {}
        if config:
            try:
                extra = json.loads(config)
            except ValueError as exc:
                return f"config must be a valid JSON object: {exc}"
            if not isinstance(extra, dict):
                return "config must be a JSON object of field->value pairs."

        if action == "list_templates":
            resp, err = await _api(
                base_url, headers, "get", "/api/lessons_templates",
                "list lessons templates",
            )
            if err:
                return err
            items = resp.json() or []
            if not items:
                return "No lessons templates found."
            return f"Lessons templates ({len(items)}):\n" + "\n".join(
                f"- {t.get('lessons_template_name', '?')} (id: {t.get('lessonstemplate_id') or t.get('lessons_template_id')})"
                for t in items
            )

        if not parent_id:
            return "parent_id (exercise_id or scenario_id) is required for this action."
        base = f"{root}/{parent_id}"

        if action == "apply_template":
            if not template_id:
                return "template_id is required for 'apply_template'."
            _, err = await _api(
                base_url, headers, "post",
                f"{base}/lessons_apply_template/{template_id}", "apply lessons template",
            )
            if err:
                return err
            return f"Applied lessons template {template_id} to {scope} {parent_id}."

        if action == "list_categories":
            resp, err = await _api(
                base_url, headers, "get", f"{base}/lessons_categories",
                "list lessons categories",
            )
            if err:
                return err
            return json_answer(resp.json(), 2500)

        if action == "create_category":
            if not extra:
                return "config (JSON with lessons_category_name, ...) is required."
            resp, err = await _api(
                base_url, headers, "post", f"{base}/lessons_categories",
                "create lessons category", json=extra,
            )
            if err:
                return err
            return f"Created lessons category in {scope} {parent_id}."

        if action == "create_question":
            if not extra:
                return "config (JSON with lessons_category, lessons_question_content, ...) is required."
            resp, err = await _api(
                base_url, headers, "post", f"{base}/lessons_questions",
                "create lessons question", json=extra,
            )
            if err:
                return err
            return f"Created lessons question in {scope} {parent_id}."

        if action == "create_answer":
            if not extra:
                return "config (JSON with the answer fields) is required."
            resp, err = await _api(
                base_url, headers, "post", f"{base}/lessons_answers",
                "create lessons answer", json=extra,
            )
            if err:
                return err
            return f"Created lessons answer in {scope} {parent_id}."

        if action == "send":
            _, err = await _api(
                base_url, headers, "post", f"{base}/lessons_send", "send lessons",
                json=extra or {},
            )
            if err:
                return err
            return f"Sent lessons questionnaire for {scope} {parent_id}."

        if action == "stats":
            resp, err = await _api(
                base_url, headers, "get", f"{base}/lessons_stats", "lessons stats"
            )
            if err:
                return err
            return json_answer(resp.json(), 2000)

        return (
            f"Unknown action '{action}'. Valid actions: list_templates, "
            "apply_template, list_categories, create_category, create_question, "
            "create_answer, send, stats."
        )

    async def manage_openaev_reporting(
        action: str = "",
        reporting_id: str = "",
        generation_id: str = "",
        schedule_id: str = "",
        name: str = "",
        description: str = "",
        context_type: str = "",
        context_id: str = "",
        output_format: str = "",
        time_range: str = "",
        modules: list[str] | None = None,
        schedule_period: str = "",
        schedule_time: str = "",
        schedule_enabled: bool | None = None,
        recipient_emails: list[str] | None = None,
        config: str = "",
        search: str = "",
        limit: int = 20,
    ) -> str | ToolResult:
        action = (action or "").strip().lower()

        extra: dict = {}
        if config:
            try:
                extra = json.loads(config)
            except ValueError as exc:
                return f"config must be a valid JSON object: {exc}"
            if not isinstance(extra, dict):
                return "config must be a JSON object of field->value pairs."

        def _validated(value: str, allowed: list[str], label: str) -> tuple[str, str]:
            """Uppercase + validate an enum value. Returns (value, error)."""
            if not value:
                return "", ""
            upper = value.strip().upper()
            if upper not in allowed:
                return "", (
                    f"{label} must be one of: {', '.join(allowed)} (got '{value}')."
                )
            return upper, ""

        def _template_body() -> tuple[dict, str]:
            body: dict = dict(extra)
            if name:
                body["reporting_name"] = name
            if description:
                body["reporting_description"] = description
            if context_type:
                value, err = _validated(
                    context_type, REPORTING_CONTEXT_TYPES, "context_type"
                )
                if err:
                    return {}, err
                body["reporting_context_type"] = value
            if context_id:
                body["reporting_context_id"] = context_id
            if output_format:
                value, err = _validated(
                    output_format, REPORTING_FORMATS, "output_format"
                )
                if err:
                    return {}, err
                body["reporting_default_format"] = value
            if time_range:
                value, err = _validated(time_range, REPORTING_TIME_RANGES, "time_range")
                if err:
                    return {}, err
                body["reporting_time_range"] = value
            if modules is not None:
                resolved = []
                for m in modules:
                    value, err = _validated(m, REPORTING_MODULE_TYPES, "modules")
                    if err:
                        return {}, err
                    resolved.append({"module_type": value})
                body["reporting_modules"] = resolved
            return body, ""

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/reportings/search",
                "search reportings",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No reporting templates found."
            return f"Reporting templates ({len(items)}):\n\n" + "\n\n".join(
                _fmt_reporting(r) for r in items
            )

        if action == "context":
            value, verr = _validated(
                context_type, REPORTING_CONTEXT_TYPES, "context_type"
            )
            if verr:
                return verr
            if not value:
                return (
                    "context_type is required for action 'context' (one of: "
                    f"{', '.join(REPORTING_CONTEXT_TYPES)})."
                )
            path = f"/api/reportings/context/{value}"
            if context_id:
                path = f"{path}/{context_id}"
            resp, err = await _api(
                base_url, headers, "get", path, "list reportings for subject"
            )
            if err:
                return err
            items = resp.json() or []
            if not items:
                return f"No reporting templates configured for {value}."
            return f"Reporting templates for {value} ({len(items)}):\n\n" + "\n\n".join(
                _fmt_reporting(r) for r in items
            )

        if action == "get":
            if not reporting_id:
                return "reporting_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/reportings/{reporting_id}",
                "get reporting",
            )
            if err:
                return err
            return _fmt_reporting(resp.json())

        if action == "create":
            body, berr = _template_body()
            if berr:
                return berr
            if not body.get("reporting_name") or not body.get("reporting_context_type"):
                return (
                    "name and context_type are required for 'create' (context_type "
                    f"one of: {', '.join(REPORTING_CONTEXT_TYPES)})."
                )
            resp, err = await _api(
                base_url, headers, "post", "/api/reportings", "create reporting",
                json=body,
            )
            if err:
                return err
            return "Created reporting template.\n" + _fmt_reporting(resp.json())

        if action == "update":
            if not reporting_id:
                return "reporting_id is required for action 'update'."
            body, berr = _template_body()
            if berr:
                return berr
            if not body:
                return (
                    "Provide at least one field to update (name, description, "
                    "context_type, context_id, output_format, time_range, modules, "
                    "or config)."
                )
            resp, err = await _api(
                base_url, headers, "put", f"/api/reportings/{reporting_id}",
                "update reporting", json=body,
            )
            if err:
                return err
            return "Updated reporting template.\n" + _fmt_reporting(resp.json())

        if action == "delete":
            if not reporting_id:
                return "reporting_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/reportings/{reporting_id}",
                "delete reporting",
            )
            if err:
                return err
            return f"Deleted reporting template {reporting_id}."

        if action == "generate":
            if not reporting_id:
                return "reporting_id is required for action 'generate'."
            body: dict = {}
            if output_format:
                value, verr = _validated(
                    output_format, REPORTING_FORMATS, "output_format"
                )
                if verr:
                    return verr
                body["reporting_generation_format"] = value
            resp, err = await _api(
                base_url, headers, "post",
                f"/api/reportings/{reporting_id}/generate", "generate reporting",
                json=body or None,
            )
            if err:
                return err
            gen = resp.json() if resp.content else {}
            return (
                "Requested a report generation (rendering runs asynchronously - "
                "poll with action='generation' until the status is SUCCESS, then "
                "action='download').\n" + _fmt_reporting_generation(gen)
            )

        if action == "generations":
            if not reporting_id:
                return "reporting_id is required for action 'generations'."
            resp, err = await _api(
                base_url, headers, "get",
                f"/api/reportings/{reporting_id}/generations", "list generations",
            )
            if err:
                return err
            items = resp.json() or []
            if not items:
                return "No generations for this reporting template yet."
            return f"Generations ({len(items)}):\n\n" + "\n\n".join(
                _fmt_reporting_generation(g) for g in items
            )

        if action == "generation":
            if not generation_id:
                return "generation_id is required for action 'generation'."
            resp, err = await _api(
                base_url, headers, "get",
                f"/api/reportings/generations/{generation_id}", "get generation",
            )
            if err:
                return err
            return _fmt_reporting_generation(resp.json())

        if action == "delete_generation":
            if not generation_id:
                return "generation_id is required for action 'delete_generation'."
            _, err = await _api(
                base_url, headers, "delete",
                f"/api/reportings/generations/{generation_id}", "delete generation",
            )
            if err:
                return err
            return f"Deleted generation {generation_id} and its document."

        if action == "download":
            if not generation_id:
                return "generation_id is required for action 'download'."
            resp, err = await _api(
                base_url, headers, "get",
                f"/api/reportings/generations/{generation_id}/file",
                "download generated report",
            )
            if err:
                return err
            data = resp.content or b""
            if not data:
                return (
                    f"Generation {generation_id} returned an empty document. It may "
                    "still be rendering - check action='generation' for its status."
                )
            content_type = resp.headers.get("content-type", "application/pdf")
            suffix = "html" if "html" in content_type.lower() else "pdf"
            filename = f"openaev-report-{generation_id}.{suffix}"
            return await deliver_file(
                data=data,
                filename=filename,
                content_type=content_type.split(";", 1)[0].strip(),
                source="OpenAEV",
                persist_file_fn=persist_file_fn,
                links=[
                    (
                        "Download URL",
                        f"{base_url}/api/reportings/generations/{generation_id}/file",
                    )
                ],
                details=[f"Generated report {generation_id} is ready."],
                mark_unstored=False,
            )

        if action == "create_schedule":
            if not reporting_id:
                return "reporting_id is required for action 'create_schedule'."
            body, serr = _schedule_body(
                extra, name, output_format, schedule_period, schedule_time,
                schedule_enabled, recipient_emails, _validated,
            )
            if serr:
                return serr
            if not body:
                return (
                    "Provide at least schedule_period (HOUR/DAY/WEEK/MONTH) for "
                    "'create_schedule'."
                )
            resp, err = await _api(
                base_url, headers, "post",
                f"/api/reportings/{reporting_id}/schedules", "create schedule",
                json=body,
            )
            if err:
                return err
            s = resp.json() if resp.content else {}
            return (
                f"Created schedule '{s.get('reporting_schedule_name', name or '?')}' "
                f"(id: {s.get('reporting_schedule_id')})."
            )

        if action == "update_schedule":
            if not reporting_id or not schedule_id:
                return (
                    "reporting_id and schedule_id are required for "
                    "'update_schedule'."
                )
            body, serr = _schedule_body(
                extra, name, output_format, schedule_period, schedule_time,
                schedule_enabled, recipient_emails, _validated,
            )
            if serr:
                return serr
            if not body:
                return (
                    "Provide at least one field to update (name, schedule_period, "
                    "schedule_time, schedule_enabled, output_format, "
                    "recipient_emails, or config)."
                )
            _, err = await _api(
                base_url, headers, "put",
                f"/api/reportings/{reporting_id}/schedules/{schedule_id}",
                "update schedule", json=body,
            )
            if err:
                return err
            return f"Updated schedule {schedule_id}."

        if action == "delete_schedule":
            if not reporting_id or not schedule_id:
                return (
                    "reporting_id and schedule_id are required for "
                    "'delete_schedule'."
                )
            _, err = await _api(
                base_url, headers, "delete",
                f"/api/reportings/{reporting_id}/schedules/{schedule_id}",
                "delete schedule",
            )
            if err:
                return err
            return f"Deleted schedule {schedule_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, context, get, create, "
            "update, delete, generate, generations, generation, delete_generation, "
            "download, create_schedule, update_schedule, delete_schedule."
        )

    executor.register("manage_openaev_lessons", manage_openaev_lessons)
    executor.register("manage_openaev_reporting", manage_openaev_reporting)

    return [
        ToolDefinition(
            name="manage_openaev_lessons",
            description=(
                "Manage OpenAEV lessons learned. Actions: 'list_templates'; and (with "
                "scope='simulation'|'scenario' + parent_id) 'apply_template' "
                "(template_id), 'list_categories', 'create_category'/'create_question'/"
                "'create_answer' (config = JSON body for the respective resource), "
                "'send' (send the questionnaire), 'stats' (response statistics)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list_templates", "apply_template", "list_categories",
                            "create_category", "create_question", "create_answer",
                            "send", "stats",
                        ],
                    },
                    "scope": {"type": "string", "enum": ["simulation", "scenario"], "default": "simulation"},
                    "parent_id": {"type": "string", "description": "exercise_id or scenario_id."},
                    "template_id": {"type": "string", "description": "Lessons template ID (apply_template)."},
                    "config": {"type": "string", "description": "JSON body for create_* / send actions."},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_reporting",
            description=(
                "Manage OpenAEV reporting: reusable report TEMPLATES plus the "
                "generations (rendered PDF/HTML reports) produced from them. A "
                "template is attached to a subject via context_type (+ context_id "
                "for everything except PLATFORM) and is composed of modules such "
                "as EXECUTIVE_SUMMARY, MITRE_COVERAGE, RESULTS_BREAKDOWN or "
                "FAILED_EXPECTATIONS. Actions: 'list' (search templates), "
                "'context' (context_type [+ context_id] - templates available for "
                "one subject), 'get', 'create' (name + context_type, optional "
                "description/output_format/time_range/modules), 'update', "
                "'delete', 'generate' (reporting_id - renders asynchronously), "
                "'generations' (reporting_id - render history), 'generation' "
                "(generation_id - poll one render), 'delete_generation', "
                "'download' (generation_id - fetch the rendered file once its "
                "status is SUCCESS), 'create_schedule' / 'update_schedule' / "
                "'delete_schedule' (recurring delivery). Replaces the removed "
                "OpenAEV report API - there is no report_global_observation "
                "field any more."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "context", "get", "create", "update", "delete",
                            "generate", "generations", "generation",
                            "delete_generation", "download", "create_schedule",
                            "update_schedule", "delete_schedule",
                        ],
                    },
                    "reporting_id": {"type": "string", "description": "Reporting template ID."},
                    "generation_id": {"type": "string", "description": "Generation (rendered report) ID."},
                    "schedule_id": {"type": "string", "description": "Schedule ID."},
                    "name": {"type": "string", "description": "Template name, or schedule name for the *_schedule actions."},
                    "description": {"type": "string"},
                    "context_type": {
                        "type": "string",
                        "enum": REPORTING_CONTEXT_TYPES,
                        "description": (
                            "The subject the report is about. PLATFORM needs no "
                            "context_id; every other type requires the matching "
                            "entity id in context_id."
                        ),
                    },
                    "context_id": {"type": "string", "description": "ID of the subject entity (simulation, scenario, endpoint, team, ...)."},
                    "output_format": {
                        "type": "string",
                        "enum": REPORTING_FORMATS,
                        "description": "Render format - the template default, the generate format, or the schedule format.",
                    },
                    "time_range": {
                        "type": "string",
                        "enum": REPORTING_TIME_RANGES,
                        "description": "Window of data the report covers.",
                    },
                    "modules": {
                        "type": "array",
                        "items": {"type": "string", "enum": REPORTING_MODULE_TYPES},
                        "description": (
                            "Ordered report sections. Rendered in the order given. "
                            "FAILED_EXPECTATIONS lists the security controls that "
                            "did not detect or prevent the simulated attacks."
                        ),
                    },
                    "schedule_period": {
                        "type": "string",
                        "enum": REPORTING_SCHEDULE_PERIODS,
                        "description": "Recurrence granularity for the *_schedule actions.",
                    },
                    "schedule_time": {"type": "string", "description": "Time of day / offset within the period (schedule actions)."},
                    "schedule_enabled": {"type": "boolean", "description": "Enable or pause a schedule. Omit to leave unchanged."},
                    "recipient_emails": {"type": "array", "items": {"type": "string"}, "description": "Email recipients for a schedule."},
                    "config": {"type": "string", "description": "JSON object of raw reporting_* / reporting_schedule_* fields merged into the body (e.g. reporting_branding, full module objects)."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
