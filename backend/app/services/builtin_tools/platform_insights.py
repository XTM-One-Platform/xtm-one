# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""The ``platform_insights`` built-in suite: the platform's own figures, read by an agent.

Twelve read-only tools over the read paths the admin pages use
(``services/platform_insights``): LLM analytics, the trace and session lists,
the objects of the platform, the deployed builds, AI adoption, the
audit log, an overview of what the platform holds, the backend's own
``xtmone_*`` Prometheus series, the friction ledger's counts, each person's own default chat
agent (``get_user_default_agents``), and ``render_chart``, which
turns their rows into a chart the answer shows inline. Plus the two
admin-only platform AI settings tools of ``platform_admin``. Each call opens its own
short session: the turn's session may sit in a long transaction while the loop
runs.

Who reads what is decided per call (``platform_insights.scope.load_scope``): an
administrator's agent reads the platform, a group manager the LLM analytics of
their teams on top of their own slice, anyone else their own slice, and every
result says which (``scope``). The charts a turn renders go to its chart sink
and are placed in the answer by ``chat_charts.inject_charts``.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException

from app.core import database
from app.core.utils import format_exception_message
from app.services.tracing.read_store import ANALYTICS_PERIODS, TRACE_SESSION_SORT_FIELDS, TRACE_SORT_FIELDS
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import logger

PLATFORM_INSIGHTS_SUITE_ID = "platform_insights"

_SCOPE_SENTENCE = (
    "Admin-gated: an administrator's agent reads the whole platform, anyone else only their own slice; the "
    "result says which ('scope') - say it when it is 'self'."
)

_TEAM_SENTENCE = (
    "A group manager's call ('scope' team) also reads the usage of the CURRENT members of the groups they "
    "manage, administrators excepted, and may name those groups and their members - say whose usage the "
    "answer covers."
)

_PERIOD_SCHEMA = {
    "type": "string",
    "enum": [*ANALYTICS_PERIODS, "custom"],
    "default": "30d",
    "description": "The window back from now (24h to 1y); custom takes date_from and date_to.",
}
def _list_of(description: str) -> dict[str, Any]:
    # A list of strings; a comma-separated string is read too (``scope.as_list``).
    return {"type": "array", "items": {"type": "string"}, "description": description}


def _listing_schema(sort_fields: tuple[str, ...]) -> dict[str, Any]:
    """The arguments of the trace and session listings; *sort_fields* first is the default order."""
    return {
        "type": "object",
        "properties": {
            "period": _PERIOD_SCHEMA,
            "date_from": {"type": "string", "description": "ISO-8601, period custom only."},
            "date_to": {"type": "string", "description": "ISO-8601, period custom only."},
            "agent_ids": _list_of("Agent UUIDs."),
            "user_ids": _list_of("User ids or emails."),
            "group_ids": _list_of("Group ids or exact group names (current members)."),
            "models": _list_of("Model names."),
            "sources": _list_of("Trace sources (agent-chat, general-chat, assignment, ...)."),
            "status": {"type": "string", "enum": ["", "ok", "error"]},
            "search": {"type": "string"},
            "sort_by": {"type": "string", "enum": list(sort_fields), "default": sort_fields[0]},
            "sort_dir": {"type": "string", "enum": ["asc", "desc"], "default": "desc"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
        },
        "additionalProperties": False,
    }


async def _run(user_id: str, fn: Callable[..., Awaitable[dict[str, Any]]], args: dict[str, Any]) -> str:
    from app.services.platform_insights.scope import InsightRefusedError, load_scope

    try:
        # The factory read at call time, not bound at import: whatever the
        # process binds it to when the tool runs is the one the call uses.
        async with database.async_session() as db:
            scope = await load_scope(db, user_id)
            result = await fn(db, scope, args)
    except InsightRefusedError as refusal:
        return refusal.message
    except HTTPException as exc:
        return f"Error: {exc.detail}"
    except Exception as exc:
        logger.error("platform insights tool failed: %s", format_exception_message(exc), exc_info=True)
        return "Error: the platform figures could not be read right now."
    return json.dumps(result, ensure_ascii=False, default=str)


def build_platform_insights_tools(
    user_id: str,
    *,
    chart_sink: list | None = None,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The suite's tools acting for *user_id*; charts go to *chart_sink* (``chat_charts.ChatChart``).

    *agent_id* is the agent the tools run in: ``get_user_default_agents`` reads
    the people it is shared with or whose fleet workspace it orchestrates.

    Without a sink (a surface with no answer to place a chart in) ``render_chart``
    returns the chart block itself, to be copied into the reply verbatim. An
    analytics export (``export: csv``) is a file of *conversation_id* /
    *assignment_run_id*, registered on the loop's file context.
    """
    from app.services.platform_insights.adoption import VIEWS as ADOPTION_VIEWS
    from app.services.platform_insights.adoption import query_ai_adoption
    from app.services.platform_insights.analytics import DIMENSIONS as ANALYTICS_DIMENSIONS
    from app.services.platform_insights.analytics import GROUP_BYS as ANALYTICS_GROUP_BYS
    from app.services.platform_insights.analytics import METRICS as ANALYTICS_METRICS
    from app.services.platform_insights.analytics import query_platform_analytics
    from app.services.platform_insights.audit import query_audit_log
    from app.services.platform_insights.default_agents import get_user_default_agents
    from app.services.platform_insights.frictions import BUCKETS as FRICTION_BUCKETS
    from app.services.platform_insights.frictions import GROUP_BYS as FRICTION_GROUP_BYS
    from app.services.platform_insights.frictions import MEASURES as FRICTION_MEASURES
    from app.services.platform_insights.frictions import query_platform_frictions
    from app.services.platform_insights.metrics import query_platform_metrics
    from app.services.platform_insights.objects import query_platform_objects
    from app.services.platform_insights.overview import get_platform_overview
    from app.services.platform_insights.traces import list_platform_sessions, list_platform_traces

    async def _release_history(db, scope, args):
        from app.services.platform_builds import (
            history_note,
            read_rollout,
            release_window_slice,
            release_windows,
            running_build,
        )
        from app.services.platform_insights.scope import InsightRefusedError, parse_instant

        last_n = args.get("last_n")
        if last_n is not None and (isinstance(last_n, bool) or not isinstance(last_n, int) or last_n < 1):
            raise InsightRefusedError(f"Error: last_n must be a positive integer, not {last_n!r}.")
        since = parse_instant(args.get("since"), label="since")
        windows = await release_windows(db)
        version, git_sha = running_build()
        deployment = await read_rollout(db, windows, (version, git_sha), user_id=scope.user.id)
        running = {"version": version, "git_sha": git_sha or None}
        if args.get("running_only"):
            # The running build alone: its first boot and the build before it, read off the recorded windows.
            index = next(
                (i for i, w in enumerate(windows) if (w.version, w.git_sha) == (version, git_sha)), None
            )
            current = windows[index] if index is not None else None
            previous = windows[index - 1] if index else None
            return {
                **scope.header(),
                "running": {
                    **running,
                    "release": current.label if current else None,
                    "first_seen_at": current.first_seen_at.isoformat() if current else None,
                    "previous_release": previous.label if previous else None,
                },
                "rollout": deployment.payload(),
                "window": {"running_only": True, "recorded_releases": len(windows)},
                "caveats": [deployment.words()],
            }
        served = release_window_slice(windows, last_n=last_n, since=since)
        result = {
            **scope.header(),
            "releases": [window.payload() for window in served],
            "running": running,
            "rollout": deployment.payload(),
            "history_starts_at": windows[0].first_seen_at.isoformat() if windows else None,
            "caveats": [history_note(windows), deployment.words()],
        }
        if last_n is not None or since is not None:
            result["window"] = {
                "last_n": last_n,
                "since": since.isoformat() if since else None,
                "order": "newest_first",
                "served": len(served),
                "recorded_releases": len(windows),
            }
        return result

    def _handler(fn: Callable[..., Awaitable[dict[str, Any]]]) -> Callable[..., Awaitable[str]]:
        # ``**args``: every argument reaches the service, which reads the ones
        # its schema names (``CallableToolExecutor`` filters nothing then).
        async def _call(**args: Any) -> str:
            return await _run(user_id, fn, args)

        return _call

    async def _query_analytics(file_context: Any = None, **args: Any) -> str:
        # ``file_context`` is the loop's, injected by ``CallableToolExecutor``.
        async def _export_sink(filename: str, payload: bytes) -> str:
            from app.services.builtin_tools._shared import persist_tool_artifact

            ref = await persist_tool_artifact(
                user_id=user_id,
                filename=filename,
                raw_bytes=payload,
                content_type="text/csv",
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
                file_context=file_context,
            )
            return f"[[FILE:{ref.file_id}]]"

        # A surface with no conversation, run nor loop has nowhere to keep a file.
        keeps_files = bool(conversation_id or assignment_run_id) or file_context is not None

        async def _read(db, scope, call_args):
            return await query_platform_analytics(
                db, scope, call_args, export_sink=_export_sink if keeps_files else None
            )

        return await _run(user_id, _read, args)

    async def _render_chart(**args: Any) -> str:
        from app.services.chat_charts import chart_fence, new_chart
        from app.services.platform_insights.charts import build_chart
        from app.services.platform_insights.scope import InsightRefusedError

        try:
            block = build_chart(args)
        except InsightRefusedError as refusal:
            return refusal.message
        chart = new_chart(block)
        summary = {
            "categories": len(block["categories"]),
            "series": [item["name"] for item in block["series"]],
        }
        if chart_sink is None:
            return json.dumps(
                {
                    **summary,
                    "chart_block": chart_fence(block),
                    "instruction": "Copy chart_block into your reply verbatim, where the chart belongs.",
                },
                ensure_ascii=False,
            )
        chart_sink.append(chart)
        return json.dumps(
            {
                **summary,
                "marker": chart.marker,
                "instruction": (
                    f"Put {chart.marker} in your reply where the chart belongs: it is replaced by the chart. "
                    "Left out, the chart is shown after the reply. Repeat the caveats of the query in words."
                ),
            },
            ensure_ascii=False,
        )

    definitions = [
        ToolDefinition(
            source="builtin:platform_insights",
            name="query_platform_analytics",
            description=(
                "LLM usage of this platform as the LLM Analytics dashboard computes it (the same read path, so a "
                "total here equals the dashboard's). METRICS: cost (USD), tokens, input_tokens, output_tokens, "
                "cached_tokens, traces, errors, error_rate (%), active_users (distinct people), duration (average "
                "ms), latency (median ms), feedback, cost_per_active_user, cost_per_trace, cost_per_1k_tokens. "
                "MATRIX: every metric but latency over every group_by - a dimension (department, group, user, "
                "agent, model, source) or a time bucket (day, week, month, release) - with split_by on any "
                "OTHER dimension (group_by month + split_by department = one series per department); latency only "
                "as a total, by model or by day. The result's 'capabilities' repeats the matrix; a refusal names "
                "the nearest computed combination. ATTRIBUTION: usage counts for the person it is attributed to - "
                "the trace's user, else its assignment run's acting user, its parent trace's person (sub-agents), "
                "its conversation's owner, or its agent's owner (webhooks, channels); attribution raw reads the "
                "traces' own users. What nobody is behind is 'unattributed', by class (system, anonymous_web, "
                "unknown, unstamped). DEPARTMENTS: one per person, resolved when the usage was written - the "
                "directory department the identity provider sends (an HRIS field), else the first admin group "
                "rule by priority, else Unassigned - so department rows never overlap and add up to the total, "
                "and history does not follow a move. Groups are CURRENT membership and a person counts in each "
                "of their groups ('overlapping_membership'); group_kinds lists org units apart from access lists. "
                "department_ids / group_ids / user_ids narrow (together), exclude_* remove, complement true reads "
                "everyone OUTSIDE the named ones (the 'Others' bucket). Rows are tidy {bucket, dimension, value} "
                "(an agent row carries status active / disabled / deleted, owner and company_managed; a deleted "
                "agent its last known name). projection {method linear|cagr|last_n_avg, horizon_months} on "
                "group_by month fits complete months and projects each series, with fit quality and the current "
                "month's run rate; compare_to previous_period adds deltas per row; export csv writes every row "
                "with every metric as a file. Also returned: totals, 'unpriced_models' (cost missing from every "
                "total), 'aggregation_stale', 'stats_coverage_start' / 'raw_trace_coverage_start' / "
                "'aggregate_coverage_start', 'partial' buckets and 'caveats'. Costs the platform hides come back "
                "null ('cost_masked'), never zero - never present them as zero. "
                f"Repeat every caveat to the user. {_SCOPE_SENTENCE} {_TEAM_SENTENCE}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "metric": {"type": "string", "enum": list(ANALYTICS_METRICS), "default": "cost"},
                    "group_by": {
                        "type": "string",
                        "enum": ["", *ANALYTICS_GROUP_BYS],
                        "description": "Omit for the window's total. A chart holds 16 categories: use week or month for 30d / 90d.",
                    },
                    "split_by": {
                        "type": "string",
                        "enum": ["", *ANALYTICS_DIMENSIONS],
                        "description": "A second dimension, other than group_by: one series (time group_by) or one column each.",
                    },
                    "period": _PERIOD_SCHEMA,
                    "date_from": {"type": "string", "description": "ISO-8601, period custom only."},
                    "date_to": {"type": "string", "description": "ISO-8601, period custom only."},
                    "agent_ids": _list_of("Agent UUIDs."),
                    "user_ids": _list_of("User ids or emails."),
                    "group_ids": _list_of("Group ids or exact group names (current members)."),
                    "department_ids": _list_of("Department ids or exact names, 'unassigned', 'unattributed'."),
                    "models": _list_of("Model names."),
                    "sources": _list_of("Trace sources (agent-chat, general-chat, assignment, ...)."),
                    "exclude_user_ids": _list_of("People left out (ids or emails)."),
                    "exclude_group_ids": _list_of("Groups whose current members are left out."),
                    "exclude_department_ids": _list_of("Departments left out."),
                    "complement": {
                        "type": "boolean",
                        "default": False,
                        "description": "Read everyone NOT in the named user_ids / group_ids / department_ids.",
                    },
                    "group_kinds": _list_of("group_by / split_by group: only these kinds (org_unit, access, distribution, auto)."),
                    "attribution": {"type": "string", "enum": ["attributed", "raw"], "default": "attributed"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50,
                              "description": "A dimension grouping: the rows listed; the rest are summed in 'other'."},
                    "projection": {
                        "type": "object",
                        "properties": {
                            "method": {"type": "string", "enum": ["linear", "cagr", "last_n_avg"], "default": "linear"},
                            "horizon_months": {"type": "integer", "minimum": 1, "maximum": 36, "default": 12},
                            "last_n": {"type": "integer", "minimum": 1, "maximum": 24, "default": 3},
                        },
                        "description": "group_by month only: fit the complete months and project each series.",
                    },
                    "compare_to": {"type": "string", "enum": ["", "previous_period"]},
                    "export": {
                        "type": "string",
                        "enum": ["", "csv"],
                        "description": "Write every row, every metric as a column, as a CSV file of this conversation.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name="list_platform_traces",
            description=(
                "The LLM traces of this platform as the Traces page lists them (the same read path, filters and "
                "order), newest first by default: one row per agent run, chat turn or LLM call with when it "
                "started, its agent, its user, model, source, status and failure cause, duration, tokens, cost "
                "and the start of its error. At most 100 rows a call, paged with next_offset; 'total' counts the "
                "whole match. Filters: period (or date_from / date_to), agent_ids, user_ids, group_ids (current "
                "members), models, sources, status (ok / error), search (trace name, model, agent). sort_by "
                "started_at, name, model, duration_ms, total_tokens, total_cost or status. Rows say what a trace "
                "IS, never what it holds. Costs the platform hides come back null ('cost_masked'). "
                f"{_SCOPE_SENTENCE}"
            ),
            input_schema=_listing_schema(TRACE_SORT_FIELDS),
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name="list_platform_sessions",
            description=(
                "The trace sessions of this platform as the Sessions page lists them: one row per conversation "
                "with its user, first and last activity, trace and error counts, how many agents and models it "
                "involved, tokens and cost. Most recent activity first by default, at most 100 rows a call, "
                "paged with next_offset. Same filters as list_platform_traces (search reads the conversation id, "
                "model and agent name); sort_by any listed column. "
                f"{_SCOPE_SENTENCE}"
            ),
            input_schema=_listing_schema(TRACE_SESSION_SORT_FIELDS),
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name="query_platform_objects",
            description=(
                "The objects of this platform as the Admin Objects browser unions them (agents, runs, "
                "conversations, files, flows, work items, ...): listed newest first (at most "
                "100 a call, paged with next_offset), counted per type (count_only), or counted per day / week / "
                "month / release of their CREATION (bucket) as tidy rows {bucket, dimension: object_type, value}. "
                "Filters: object_types, search (name), owner (id or email), group_id (objects owned by the group's "
                "members TODAY), is_company_managed, created_from / created_to / updated_from (ISO-8601). "
                "bucket release counts what was created under each deployed build; what predates the recorded "
                f"history is under 'before_release_history' - say so. {_SCOPE_SENTENCE}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "object_types": _list_of("Types to read (agent, presentation, conversation, ...); omit for all."),
                    "search": {"type": "string"},
                    "owner": _list_of("Owner ids or emails."),
                    "group_id": _list_of("Group ids or exact names: objects owned by its current members."),
                    "is_company_managed": {"type": "boolean"},
                    "created_from": {"type": "string"},
                    "created_to": {"type": "string"},
                    "updated_from": {"type": "string"},
                    "bucket": {"type": "string", "enum": ["", "day", "week", "month", "release"]},
                    "count_only": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name="get_platform_release_history",
            description=(
                "The builds this platform has run (the About dialog's version and commit), each with when it "
                "was first and last seen and until when it was deployed, and the 'rollout' of the newest "
                "(in_progress with its sha and start, draining, complete or rolled_back): what answers 'is this "
                "fix deployed'. A build is known from its first boot. The history is never backfilled "
                "('history_starts_at'): say so when a question reaches further back. bucket release on "
                "query_platform_objects or query_platform_analytics cuts a count or a cost per build. No "
                "argument: every build, oldest first; a deploy check passes running_only; last_n and "
                "since answer NEWEST first with a 'window'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "last_n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "description": "The newest N, after 'since'.",
                    },
                    "since": {
                        "type": "string",
                        "description": "ISO-8601: first seen at or after.",
                    },
                    "running_only": {
                        "type": "boolean",
                        "description": "Running build and rollout only.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name="query_ai_adoption",
            description=(
                "The AI Adoption dashboard (agentic AI on this platform): view overview (active users, top "
                "users) or gaps (inactive and low-activity people, monthly adoption rate), over 'days', "
                "optionally per group, as tidy rows. "
                f"{_SCOPE_SENTENCE} Outside the administrator scope only "
                "your own activity is served."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "view": {
                        "type": "string",
                        "enum": list(ADOPTION_VIEWS),
                        "default": "overview",
                    },
                    "group_id": _list_of("Group ids or exact names."),
                    "days": {"type": "integer", "minimum": 1, "maximum": 365, "default": 30},
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name="query_audit_log",
            description=(
                "Entries of the audit log, newest first, at most 200 a call (paged with next_offset): filter by "
                "actor (id or email), action, entity_type, entity_id, period (7d / 30d / 90d) or date_from / date_to, "
                "and free text over the summary, the entity name and the actor's email. An administrator's agent "
                "reads every entry, as the audit page does; anyone else the entries of their own actions. Every "
                "read is itself recorded in the audit log (once per identical read every 15 minutes). Read-only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "actor": _list_of("Actor ids or emails."),
                    "action": _list_of("Actions (create, update, delete, login, ...)."),
                    "entity_type": _list_of("Entity types (agent, assignment, settings, ...)."),
                    "entity_id": {"type": "string"},
                    "period": {"type": "string", "enum": ["", "7d", "30d", "90d"]},
                    "date_from": {"type": "string"},
                    "date_to": {"type": "string"},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
                    "offset": {"type": "integer", "minimum": 0, "default": 0},
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name="get_platform_overview",
            description=(
                "What this platform holds today, in one call: its identity (Instance ID, name, URL), objects per "
                "type (the Admin Objects counts), agents by state, assignments by trigger type and state, "
                "integrations by provider, each connected module's registered version (the CTEM chips' string; "
                "read builds from it), knowledge bases with "
                "their document and chunk totals, MCP servers, machines by status, files and their storage "
                "footprint, users active / total and groups, plus the deployed build, the feature flags on, the "
                "trace retention and whether the daily analytics aggregation is fresh. "
                f"{_SCOPE_SENTENCE}"
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name="query_platform_metrics",
            description=(
                "The backend's own xtmone_* Prometheus series (sandbox create histogram, rollout counters, queue "
                "gauges) from the process registry. Without 'series': the served series with type and labels. A "
                "counter or gauge: one row {series, labels, value} per label set; a histogram: count, sum, mean, "
                "p50 / p95 from its buckets. 'labels' keeps rows with those exact values. Other names are refused "
                "(no PromQL). PROCESS-LOCAL: the answering worker since it started ('view'), never the cluster "
                "sum - repeat that caveat. Administrators only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "series": {
                        "type": "string",
                        "description": "An xtmone_* series name (counters with or without _total); omit to list them.",
                    },
                    "labels": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Exact label values the rows must carry, e.g. {\"version\": \"f474aac6\"}.",
                    },
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name="query_platform_frictions",
            description=(
                "The friction ledger counted: what agent runs struggled with (tool errors, retries, approval "
                "pauses, sandbox and runner failures, resumed turns, self-recorded frictions) and whether each "
                "was disposed of. 'totals': recorded (one per kind, tool and evidence in a run), occurrences and "
                "undisposed; group_by and / or bucket add tidy rows {bucket, dimension, value} of the measure, "
                "for render_chart. list_frictions reads the records. "
                f"{_SCOPE_SENTENCE}"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_by": {"type": "string", "enum": ["", *FRICTION_GROUP_BYS]},
                    "bucket": {"type": "string", "enum": ["", *FRICTION_BUCKETS]},
                    "measure": {"type": "string", "enum": list(FRICTION_MEASURES), "default": "recorded"},
                    "period": _PERIOD_SCHEMA,
                    "date_from": {"type": "string", "description": "ISO-8601, period custom only."},
                    "date_to": {"type": "string", "description": "ISO-8601, period custom only."},
                    "agent_ids": _list_of("Agent UUIDs."),
                    "kinds": _list_of("Friction kinds (tool_error, retry, approval_pause, ...)."),
                    "classes": _list_of("Friction classes (harness, procedure, ..., unclassified)."),
                    "undisposed": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name="get_user_default_agents",
            description=(
                "Each person's OWN default chat agent (none named: yours): status chosen (agent, fleet, "
                "set_at), general_assistant, platform_wired (not a choice) or none. Admin: anyone; else the "
                "people this agent is shared with or whose fleet it orchestrates; else yourself."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "users": _list_of("Ids or emails."),
                    "group_ids": _list_of("Groups (current members)."),
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name="render_chart",
            description=(
                "Turn the 'rows' a platform insights query returned into a chart shown inline in the answer (no "
                "presentation needed). Buckets become the categories and dimensions the series (one series when a "
                "row has only one of them). Bounds: 16 categories, 6 series - query with week or month buckets for "
                "long windows. A null value (a masked cost) refuses the chart: say it in words. A missing cell is a "
                "zero only with missing_values zero, right for counts and sums; use error for averages. "
                "fold_others sums the smallest categories into 'Other' (sums and counts only). Returns a marker to "
                "put in the reply where the chart belongs. Repeat the query's caveats beside the chart."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "rows": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "The 'rows' of a query result, as returned.",
                    },
                    "kind": {"type": "string", "enum": ["bar", "stacked_bar", "line", "area", "pie", "donut"], "default": "bar"},
                    "title": {"type": "string"},
                    "caption": {"type": "string", "description": "The note under the chart: the caveats, the window."},
                    "unit": {"type": "string", "description": "USD for a cost, or a short unit (ms, traces)."},
                    "format": {"type": "string", "enum": ["", "number", "currency", "percent"]},
                    "decimals": {"type": "integer", "minimum": 0, "maximum": 3},
                    "series_name": {"type": "string", "description": "The series' name when rows have no dimension."},
                    "missing_values": {"type": "string", "enum": ["zero", "error"], "default": "zero"},
                    "fold_others": {"type": "boolean", "default": False},
                },
                "required": ["rows"],
                "additionalProperties": False,
            },
        ),
    ]

    # The platform AI settings an administrator's agent reads and changes,
    # admin-gated at every call.
    from .platform_admin import build_platform_admin_tools
    from .platform_admin_credentials import build_platform_admin_credential_tools

    admin_definitions, admin_handlers = build_platform_admin_tools(
        user_id, assignment_run_id=assignment_run_id, conversation_id=conversation_id
    )
    credential_definitions, credential_handlers = build_platform_admin_credential_tools(user_id)
    definitions.extend(admin_definitions)
    definitions.extend(credential_definitions)
    admin_handlers = {**admin_handlers, **credential_handlers}

    executor = CallableToolExecutor()
    for admin_name, admin_handler in admin_handlers.items():
        executor.register(admin_name, admin_handler)
    executor.register("query_platform_analytics", _query_analytics)
    executor.register("list_platform_traces", _handler(list_platform_traces))
    executor.register("list_platform_sessions", _handler(list_platform_sessions))
    executor.register("query_platform_objects", _handler(query_platform_objects))
    executor.register("get_platform_release_history", _handler(_release_history))
    executor.register("query_ai_adoption", _handler(query_ai_adoption))
    executor.register("query_audit_log", _handler(query_audit_log))
    executor.register("get_platform_overview", _handler(get_platform_overview))
    executor.register("query_platform_metrics", _handler(query_platform_metrics))
    executor.register("query_platform_frictions", _handler(query_platform_frictions))
    executor.register(
        "get_user_default_agents",
        _handler(functools.partial(get_user_default_agents, agent_id=agent_id)),
    )
    executor.register("render_chart", _render_chart)
    return definitions, executor
