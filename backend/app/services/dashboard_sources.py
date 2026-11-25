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

"""Reading a dashboard's data sources, each with the viewer's own access.

One entry point, :func:`read_source`, for every type of the definition
(``dashboard_definition``): ``static`` as written, ``platform`` live through
``dashboard_live``, ``assignment`` the answer of the assignment's last
completed run the viewer may see, ``variable`` the value of a variable the
viewer may read (never a secret one), ``ask`` the answer of the question the
viewer last asked the agent (:func:`ask_agent`), ``integration`` the answer of a
read tool of an integration the viewer may use, called with fixed arguments,
``knowledge`` a table kept in a knowledge base document the viewer reads,
``derived`` another source's result through ``dashboard_pipeline``.

The answer is ``{"status": "ok", "shape", "data", "fetched_at"}`` or
``{"status": "error", "code"}`` with a fixed code (:data:`ERROR_CODES`): the
precise reason goes to the logs, never to the caller. Every check reads the
rows on every call - a revoked share, a run made private, a variable turned
secret stop the data at once.

What a writer points a source at is checked too (:func:`check_source_references`):
an assignment, an agent or a variable the writer cannot reach is refused, as
an integration is.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.custom_dashboard_spec import DashboardSpecError
from app.services.dashboard_definition import REFERENCE_SOURCE_TYPES, upgrade
from app.services.dashboard_live import LiveError, closed_static_data, text_data
from app.services.dashboard_pipeline import PipelineError, Step, run_pipeline

logger = logging.getLogger(__name__)

#: The fixed codes a source answers besides ``dashboard_live.ERROR_CODES``.
ERROR_CODES: tuple[str, ...] = (
    "no_result",  # an assignment with no completed run the viewer may see
    "not_run",  # a question the viewer has not asked yet
    "running",  # a question being answered
    "invalid_result",  # an answer that does not fit the source's shape, a step that does not apply
    "waiting",  # a token the source uses has no value yet (an input not set)
)

#: How long the question a viewer asked is remembered (its answer is the agent's run).
ASK_MEMORY_SECONDS = 7 * 24 * 3600
#: Questions one person may ask through dashboards per minute.
ASKS_PER_MINUTE = 6

_STEPS = TypeAdapter(list[Step])
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


class SourceError(LiveError):
    """A source that cannot be read now, with its own codes (:data:`ERROR_CODES`) besides the live widgets' ones.

    ``dashboard_live.LiveError`` folds a code it does not know into ``upstream_error``: an answer that does not fit
    its shape would then read as a platform that failed, and the remedy offered would be the wrong one.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        if code in ERROR_CODES:
            self.code = code


def _error(code: str) -> dict:
    return {"status": "error", "code": code}


def _ok(shape: str, data: dict, fetched_at: datetime | None = None) -> dict:
    return {"status": "ok", "shape": shape, "data": data, "fetched_at": (fetched_at or datetime.now(UTC)).isoformat()}


# ── Answers in words ─────────────────────────────────────────────────────────


def _at_path(value: Any, path: str | None) -> Any:
    for part in (path or "").split(".") if path else []:
        if isinstance(value, dict):
            value = value.get(part)
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise SourceError("invalid_result")
    return value


def _as_shape_input(shape: str, value: Any) -> dict:
    """An answer's JSON as ``closed_static_data`` reads it: a bare list or number is accepted too."""
    if shape == "number":
        return value if isinstance(value, dict) else {"value": value}
    if shape == "text":
        return {"text": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        if shape == "distribution":
            return {"items": _items_of(value)}
        if shape == "series":
            return {"series": value}
        if shape == "rows":
            keys = list(dict.fromkeys(k for row in value if isinstance(row, dict) for k in row if k != "_url"))
            return {"columns": [{"key": k, **({"kind": "number"} if _numeric(value, k) else {})} for k in keys], "rows": value}
    raise SourceError("invalid_result")


def _numeric(rows: list, key: str) -> bool:
    """Whether every row that has *key* holds a number there (a table's count column)."""
    cells = [row[key] for row in rows if isinstance(row, dict) and row.get(key) not in (None, "")]
    return bool(cells) and all(isinstance(c, int | float) and not isinstance(c, bool) for c in cells)


def _items_of(rows: list) -> list:
    """A distribution's items from a list: as written when they carry ``label`` and ``value``, else the rows of a
    table read by their columns - the first that is not a number labels each item, the first number is its value
    (a ``severity,count`` table). :class:`SourceError` ``invalid_result`` when a table has no such pair."""
    records = [row for row in rows if isinstance(row, dict)]
    if not records or all("label" in row and "value" in row for row in records):
        return rows
    keys = list(dict.fromkeys(k for row in records for k in row if not str(k).startswith("_") and k != "link"))
    value_key = next((k for k in keys if _numeric(records, k)), None)
    label_key = next((k for k in keys if k != value_key and not _numeric(records, k)), None)
    if value_key is None or label_key is None:
        raise SourceError("invalid_result")
    return [{**({"link": row["link"]} if "link" in row else {}), "label": row.get(label_key), "value": row.get(value_key)} for row in records]


def _json_in(raw: str) -> Any:
    """The JSON an answer holds: all of it, a fenced block, or the one object or list written inside its words."""
    fenced = _FENCE.match(raw)
    candidates = [fenced.group(1) if fenced else raw]
    # The object or the list, from its first opening bracket to its last closing one, the earlier first.
    for opening, closing in sorted((("{", "}"), ("[", "]")), key=lambda pair: raw.find(pair[0]) % (len(raw) + 1)):
        start, end = raw.find(opening), raw.rfind(closing)
        if 0 <= start < end:
            candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except (ValueError, TypeError):
            continue
    raise SourceError("invalid_result")


def answer_data(answer: str | None, shape: str, path: str | None) -> dict:
    """An answer in words as the closed *shape*: the text itself, or the JSON it holds (at *path*)."""
    from app.channels.base import strip_artifact_markers

    # A chat card's marker (``[[DASHBOARD:<id>]]``...) is a chat surface's render instruction, not prose.
    raw = strip_artifact_markers((answer or "").strip(), keep_lines=True)
    if shape == "text" and not path:
        return text_data(raw)
    if shape == "number" and not path:
        try:
            return closed_static_data("number", {"value": float(raw)})
        except ValueError:
            pass
    parsed = _json_in(raw)
    try:
        return closed_static_data(shape, _as_shape_input(shape, _at_path(parsed, path)))
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        raise SourceError("invalid_result") from exc


# ── Each type ────────────────────────────────────────────────────────────────


async def _group_ids(db: AsyncSession, user_id: uuid.UUID) -> list[str]:
    from app.api.v1.helpers import get_user_group_ids

    return await get_user_group_ids(db, user_id)


async def _assignment(db: AsyncSession, user_id: uuid.UUID, options: dict) -> dict:
    from app.api.v1.helpers import find_user_assignment, run_visible_filter
    from app.models.assignment import AssignmentRun

    # The viewer's own reach, no admin bypass: an admin never reads a colleague's private runs through a dashboard.
    assignment = await find_user_assignment(db, user_id, str(options.get("assignment_id")), await _group_ids(db, user_id))
    if assignment is None:
        return _error("access_denied")
    history = options.get("history")
    runs = (
        await db.execute(
            select(AssignmentRun)
            .where(
                AssignmentRun.assignment_id == assignment.id,
                AssignmentRun.status == "completed",
                AssignmentRun.parent_run_id.is_(None),
                run_visible_filter(user_id),
            )
            .order_by(AssignmentRun.created_at.desc())
            .limit(int(history) if history else 1)
        )
    ).scalars().all()
    if history:
        return _trend(assignment, list(reversed(runs)), options)
    run = runs[0] if runs else None
    if run is None or not (run.result_summary or "").strip():
        return _error("no_result")
    shape = options.get("shape") or "text"
    try:
        return _ok(shape, answer_data(run.result_summary, shape, options.get("path")), run.completed_at or run.created_at)
    except LiveError:
        logger.info("Assignment %s: its last answer does not fit the %s shape", assignment.id, shape)
        return _error("invalid_result")


async def _visible_variable(db: AsyncSession, user_id: uuid.UUID, variable_id: object):
    from app.api.v1.helpers import user_visible_filter
    from app.models.variable import Variable

    try:
        vid = uuid.UUID(str(variable_id))
    except ValueError:
        return None
    return (
        await db.execute(
            select(Variable).where(Variable.id == vid, user_visible_filter(Variable, user_id, await _group_ids(db, user_id)))
        )
    ).scalar_one_or_none()


def _variables_disabled() -> bool:
    """The platform's switch turning custom variables off (``disable_custom_variables``): no variable is read then."""
    from app.core.security_options import security_option

    return bool(security_option("disable_custom_variables"))


def _trend(assignment, runs: list, options: dict) -> dict:
    """The figure of each run's answer (at ``path``) as one series over the runs' completion times, oldest first;
    a run whose answer holds no figure is left out."""
    points = []
    for run in runs:
        try:
            value = answer_data(run.result_summary, "number", options.get("path")).get("value")
        except LiveError:
            continue
        if isinstance(value, int | float):
            points.append({"x": (run.completed_at or run.created_at).isoformat(), "y": value})
    if not points:
        return _error("no_result")
    name = options.get("path") or getattr(assignment, "name", "") or "value"
    return _ok("series", closed_static_data("series", {"series": [{"name": str(name)[:80], "points": points}]}), runs[-1].completed_at)


async def _variable(db: AsyncSession, user_id: uuid.UUID, options: dict) -> dict:
    if _variables_disabled():
        return _error("unsupported")
    variable = await _visible_variable(db, user_id, options.get("variable_id"))
    # A secret is never drawn, whoever reads it.
    if variable is None or variable.is_secret:
        return _error("access_denied")
    shape = options.get("shape") or "text"
    try:
        return _ok(shape, answer_data(variable.value, shape, None), variable.updated_at)
    except LiveError:
        return _error("invalid_result")


def ask_key(dashboard_id: object, source_id: str, user_id: object) -> str:
    return f"dashboard-ask:{dashboard_id}:{source_id}:{user_id}"


def ask_fingerprint(options: dict) -> str:
    """What a question is: asked again when the agent or the question change, not on every save."""
    raw = json.dumps([str(options.get("agent_id")), options.get("question")], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


async def _ask(dashboard_id: object, source_id: str, user_id: uuid.UUID, options: dict) -> dict:
    from app.core.redis import redis_client
    from app.services.sub_agent import WAIT_TERMINAL_STATUSES, get_task_result

    try:
        raw = await (await redis_client()).get(ask_key(dashboard_id, source_id, user_id))
    except Exception:  # an unreachable store: nothing asked as far as we know
        logger.warning("Dashboard ask: the store could not be read")
        return _error("upstream_error")
    remembered = json.loads(raw) if raw else None
    if not remembered or remembered.get("fingerprint") != ask_fingerprint(options):
        return _error("not_run")
    result = await get_task_result(None, str(remembered.get("task_id")), str(user_id))
    if result is None:
        return _error("not_run")
    status = result.get("status")
    if status not in WAIT_TERMINAL_STATUSES:
        return _error("running")
    if status != "completed":
        return _error("upstream_error")
    shape = options.get("shape") or "text"
    completed = result.get("completed_at")
    try:
        return _ok(
            shape,
            answer_data(result.get("result"), shape, options.get("path")),
            datetime.fromisoformat(completed) if completed else None,
        )
    except LiveError:
        return _error("invalid_result")


async def read_source(
    db: AsyncSession,
    user_id: uuid.UUID,
    definition: dict,
    source_id: str,
    range_key: str | None = None,
    *,
    dashboard_id: object = None,
    depth: int = 0,
    tokens: dict | None = None,
    fresh: bool = False,
    marking_ceiling: tuple[str, ...] = (),
    use_snapshots: bool = True,
) -> dict:
    """One data source of *definition* as *user_id* sees it, *tokens* put in (see the module docstring); *fresh*,
    a platform read again rather than its answer kept a minute; under a published link's *marking_ceiling*.

    A source refreshed on the server (``refreshMode`` ``server`` / ``manual``) of a stored dashboard
    (*dashboard_id*) is its snapshot, read with its publisher's access (``dashboard_refresh``); the scheduler
    reading it passes *use_snapshots* off, and a published link's ceiling always reads it as the link does.
    """
    from app.services.dashboard_tokens import TokensWaitingError

    try:
        return await _read_source(
            db, user_id, definition, source_id, range_key, dashboard_id, depth, tokens or {}, fresh, marking_ceiling,
            use_snapshots and not marking_ceiling,
        )  # fmt: skip
    except TokensWaitingError:
        return _error("waiting")


async def _read_source(
    db: AsyncSession,
    user_id: uuid.UUID,
    definition: dict,
    source_id: str,
    range_key: str | None,
    dashboard_id: object,
    depth: int,
    tokens: dict,
    fresh: bool = False,
    marking_ceiling: tuple[str, ...] = (),
    use_snapshots: bool = False,
) -> dict:
    from app.services.dashboard_definition import MAX_CHAIN_DEPTH, optional_tokens
    from app.services.dashboard_live import read_widget_data, request_for
    from app.services.dashboard_tokens import put_in_filters, range_of

    source = (definition.get("dataSources") or {}).get(source_id)
    if not isinstance(source, dict) or depth > MAX_CHAIN_DEPTH:
        return _error("unsupported")
    options = source.get("options") if isinstance(source.get("options"), dict) else {}
    kind = source.get("type")
    if use_snapshots and dashboard_id is not None:
        from app.services.dashboard_refresh import snapshot_of

        snapshot = await snapshot_of(db, dashboard_id, source_id, source)
        if snapshot is not None:
            return snapshot
    if kind == "static":
        return _ok(str(options.get("shape")), options.get("data") or {})
    if kind == "platform":
        native = put_in_filters(options.get("native"), tokens, optional_tokens(definition))
        try:
            request = request_for(
                {"source": options.get("source"), "integration_id": options.get("integration_id"), "native": native},
                range_of(options.get("range"), tokens, range_key),
                marking_ceiling,
            )
        except (ValueError, KeyError, TypeError):
            # A source stored under an older catalog: drawn as unavailable, not a 500.
            return _error("unsupported")
        return await read_widget_data(db, user_id, request, fresh=fresh)
    if kind == "assignment":
        return await _assignment(db, user_id, options)
    if kind == "variable":
        return await _variable(db, user_id, options)
    if kind == "ask":
        return await _ask(dashboard_id, source_id, user_id, options)
    if kind == "integration":
        return await _integration(db, user_id, options, fresh=fresh)
    if kind == "knowledge":
        return await _knowledge(db, user_id, options)
    if kind == "derived":
        parent = await _read_source(
            db, user_id, definition, str(options.get("extends")), range_key, dashboard_id, depth + 1, tokens, fresh, marking_ceiling,
            use_snapshots,
        )  # fmt: skip
        if parent.get("status") != "ok":
            return parent
        steps = put_in_filters(options.get("steps") or [], tokens)
        try:
            shape, data = run_pipeline(_STEPS.validate_python(steps), parent["shape"], parent["data"])
        except (PipelineError, ValidationError, LiveError, ValueError, TypeError, KeyError) as exc:
            logger.info("Derived source %s: %s", source_id, str(exc)[:200])
            # Which step does not apply and why, in the pipeline's own closed words: what the studio tells the agent
            # writing it (``describe_read``); a viewer's page reads the code alone.
            reason = str(exc)[:240] if isinstance(exc, PipelineError) else None
            return {**_error("invalid_result"), **({"detail": reason} if reason else {})}
        # A derived result is not what the module draws: its presentation stays with its parent.
        return {**parent, "shape": shape, "data": data, "presentation": None}
    return _error("unsupported")


# ── An integration's read tool ───────────────────────────────────────────────

#: How long a read tool may take before the source answers ``timeout``.
INTEGRATION_TIMEOUT_SECONDS = 30
#: How much of what an integration tool answered in error goes with the code, as ``detail``.
DETAIL_CHARS = 300


#: The statuses of an integration a source can read through.
_USABLE_STATUSES = ("connected", "configured")


async def _integration_tools(db: AsyncSession, user_id: uuid.UUID, integration_id: object):
    """The tools of the integration *user_id* may use with their own credential: ``(definitions, executor)``;
    :class:`LiveError` (a fixed code) otherwise."""
    from fastapi import HTTPException

    from app.api.v1.helpers import get_user_integration
    from app.services.integration_tools import BindingBasis, ToolBindings, build_integration_tools
    from app.services.integration_user_credentials import usable_credential_holder

    try:
        integration = await get_user_integration(db, user_id, str(integration_id), await _group_ids(db, user_id))
        await usable_credential_holder(db, integration, user_id)
    except HTTPException as exc:
        raise SourceError("integration_unavailable" if exc.status_code == 404 else "access_denied") from None
    if integration.status not in _USABLE_STATUSES:
        raise SourceError("integration_unavailable")
    return await build_integration_tools(
        db,
        ToolBindings({integration.provider: str(integration.id)}, basis=BindingBasis.CALLER_CHECKED, reason="the dashboard reader's own integration access"),
        context={"source": "dashboard", "integration": integration.name, "credential_user_id": str(user_id)},
    )


async def read_tools(db: AsyncSession, user_id: uuid.UUID, integration_id: object) -> list:
    """The tools of an integration an ``integration`` source may call for *user_id*: those that only read."""
    from app.services.tool_classification import definition_tool_type

    definitions, _executor = await _integration_tools(db, user_id, integration_id)
    return [d for d in definitions if definition_tool_type(d) == "read"]


async def readable_integrations(db: AsyncSession, user_id: uuid.UUID) -> list:
    """The integrations *user_id* sees that a source can read through (connected or configured), by name."""
    from sqlalchemy import select

    from app.api.v1.helpers import user_visible_filter
    from app.models.integration import Integration

    rows = await db.execute(
        select(Integration)
        .where(Integration.status.in_(_USABLE_STATUSES), user_visible_filter(Integration, user_id, await _group_ids(db, user_id)))
        .order_by(Integration.name)
    )
    return list(rows.scalars())


async def readable_documents(db: AsyncSession, user_id: uuid.UUID, knowledge_base_id: object = None) -> list[tuple]:
    """``(knowledge base, document)`` pairs *user_id* reads - in one base when *knowledge_base_id* is given -,
    the same visibility a ``knowledge`` source reads with (:func:`_readable_document`)."""
    from sqlalchemy import select

    from app.api.v1.helpers import user_visible_filter
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

    query = (
        select(KnowledgeBase, KnowledgeDocument)
        .join(KnowledgeDocument, KnowledgeDocument.knowledge_base_id == KnowledgeBase.id)
        .where(user_visible_filter(KnowledgeBase, user_id, await _group_ids(db, user_id)))
        .order_by(KnowledgeBase.name, KnowledgeDocument.filename)
    )
    if knowledge_base_id is not None:
        query = query.where(KnowledgeBase.id == knowledge_base_id)
    return [(kb, doc) for kb, doc in (await db.execute(query)).all()]


async def _read_tool(db: AsyncSession, user_id: uuid.UUID, integration_id: object, tool: str):
    """The integration *user_id* may use with their own credential and its read tool *tool*:
    ``(definition, executor)``; :class:`LiveError` (a fixed code) otherwise."""
    from app.services.tool_classification import definition_tool_type

    definitions, executor = await _integration_tools(db, user_id, integration_id)
    definition = next((d for d in definitions if d.name == tool), None)
    if definition is None:
        raise SourceError("integration_unavailable")
    if definition_tool_type(definition) != "read":
        # A dashboard only reads: a tool that may change something is never called from one.
        raise SourceError("unsupported")
    return definition, executor


async def _integration(db: AsyncSession, user_id: uuid.UUID, options: dict, *, fresh: bool = False) -> dict:
    import asyncio

    from app.services.dashboard_live import _cache_get, _cache_set, _enforce_rate, _Slot
    from app.tools.agent_loop import validate_tool_arguments
    from app.tools.base import ToolCall

    tool = str(options.get("tool") or "")
    arguments = options.get("arguments") if isinstance(options.get("arguments"), dict) else {}
    key = "dash:integration:" + hashlib.sha256(
        json.dumps([str(user_id), str(options.get("integration_id")), tool, arguments, options.get("shape"), options.get("path")], sort_keys=True, default=str).encode()
    ).hexdigest()
    if not fresh:
        cached = await _cache_get(key)
        if cached is not None:
            return cached
    try:
        definition, executor = await _read_tool(db, user_id, options.get("integration_id"), tool)
        if validate_tool_arguments(tool, arguments, definition.input_schema):
            raise SourceError("invalid_result")
        await _enforce_rate(user_id)
        async with _Slot(str(user_id)):
            result = await asyncio.wait_for(
                executor.execute_tool(ToolCall(id="dashboard", name=tool, arguments=arguments)), timeout=INTEGRATION_TIMEOUT_SECONDS
            )
    except LiveError as exc:
        return _error(exc.code)
    except TimeoutError:
        return _error("timeout")
    except Exception as exc:
        logger.warning("Integration source failed (%s): %r", tool, str(exc)[:200])
        return _error("upstream_error")
    if result.is_error:
        # What the tool said goes with the code: "nothing found" and "wrong argument" call for different fixes.
        said = str(result.content).strip().removeprefix("Error:").strip()[:DETAIL_CHARS]
        logger.info("Integration source %s answered an error: %s", tool, said[:200])
        return {**_error("upstream_error"), **({"detail": said} if said else {})}
    try:
        answer = _ok(str(options.get("shape") or "rows"), answer_data(result.content, str(options.get("shape") or "rows"), options.get("path")))
    except LiveError as exc:
        return _error(exc.code)
    await _cache_set(key, answer)
    return answer


# ── A table kept in a knowledge base ─────────────────────────────────────────

_MD_RULE = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")


def _cell(value: str) -> Any:
    text = value.strip()
    if _NUMBER.match(text):
        return float(text) if "." in text else int(text)
    return text


def _rows_of(header: list[str], lines: list[list[str]]) -> list[dict]:
    return [{name: _cell(cells[i]) if i < len(cells) else "" for i, name in enumerate(header)} for cells in lines]


def table_value(text: str, table_format: str = "auto") -> Any:
    """What a document's table holds: JSON as written, or the rows of JSON lines, CSV or a Markdown table.

    :class:`LiveError` ``invalid_result`` when it is none of them.
    """
    import csv
    import io

    body = text.strip()
    if table_format in ("auto", "json"):
        try:
            return _json_in(body)
        except LiveError:
            if table_format == "json":
                raise
    lines = [line for line in body.splitlines() if line.strip()]
    if table_format in ("auto", "jsonl") and lines:
        try:
            values = [json.loads(line) for line in lines]
            if all(isinstance(v, dict) for v in values):
                return values
        except ValueError:
            pass
        if table_format == "jsonl":
            raise SourceError("invalid_result")
    if table_format in ("auto", "markdown"):
        table = [line.strip() for line in lines if line.strip().startswith("|")]
        if len(table) >= 2 and _MD_RULE.match(table[1]):
            split = [[c.strip() for c in row.strip("|").split("|")] for row in table]
            return _rows_of(split[0], [r for r in split[2:]])
        if table_format == "markdown":
            raise SourceError("invalid_result")
    if table_format in ("auto", "csv") and len(lines) >= 2 and "," in lines[0]:
        parsed = list(csv.reader(io.StringIO("\n".join(lines))))
        return _rows_of([h.strip() for h in parsed[0]], parsed[1:])
    raise SourceError("invalid_result")


async def _readable_document(db: AsyncSession, user_id: uuid.UUID, options: dict):
    """The knowledge base document *user_id* reads, or :class:`LiveError`."""
    from fastapi import HTTPException
    from sqlalchemy import select

    from app.api.v1.helpers import get_user_knowledge_base
    from app.models.knowledge_base import KnowledgeDocument

    try:
        kb = await get_user_knowledge_base(db, user_id, str(options.get("knowledge_base_id")), await _group_ids(db, user_id))
    except HTTPException:
        raise SourceError("access_denied") from None
    document = (
        await db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.id == uuid.UUID(str(options.get("document_id"))), KnowledgeDocument.knowledge_base_id == kb.id
            )
        )
    ).scalar_one_or_none()
    if document is None:
        raise SourceError("access_denied")
    return document


async def _knowledge(db: AsyncSession, user_id: uuid.UUID, options: dict) -> dict:
    from app.services.kb_sections import SectionError, find_section

    try:
        document = await _readable_document(db, user_id, options)
        text = document.content or ""
        if options.get("section"):
            try:
                heading = find_section(text, options["section"])
            except SectionError:
                return _error("invalid_result")
            text = text[heading.start : heading.end]
            # The heading line is the section's name, not a row.
            text = text.split("\n", 1)[1] if "\n" in text else ""
        shape = str(options.get("shape") or "rows")
        value = _at_path(table_value(text, str(options.get("format") or "auto")), options.get("path"))
        return _ok(shape, closed_static_data(shape, _as_shape_input(shape, value)), document.updated_at if getattr(document, "updated_at", None) else None)
    except LiveError as exc:
        return _error(exc.code)
    except (TypeError, ValueError, KeyError, AttributeError):
        return _error("invalid_result")


# ── Asking an agent ──────────────────────────────────────────────────────────


class AskRefusedError(Exception):
    """A question that cannot be asked: the code says why (``access_denied`` / ``rate_limited`` / ``upstream_error``)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


async def _visible_agent(db: AsyncSession, user_id: uuid.UUID, agent_id: object):
    from app.api.v1.helpers import user_visible_filter
    from app.models.agent import Agent

    try:
        aid = uuid.UUID(str(agent_id))
    except ValueError:
        return None
    return (
        await db.execute(select(Agent).where(Agent.id == aid, user_visible_filter(Agent, user_id, await _group_ids(db, user_id))))
    ).scalar_one_or_none()


def _ask_window() -> int:
    """The wall-clock minute the ask counter is kept for (a seam the tests pin)."""
    return int(datetime.now(UTC).timestamp() // 60)


async def _count_ask(user_id: uuid.UUID) -> bool:
    """Whether *user_id* may ask one more question this minute; refused when the count cannot be kept."""
    from app.core.redis import redis_client

    key = f"dashboard-ask-rate:{user_id}:{_ask_window()}"
    try:
        client = await redis_client()
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, 120)
    except Exception:  # fail closed: no count, no question
        logger.warning("Dashboard ask: the rate counter could not be kept")
        return False
    return count <= ASKS_PER_MINUTE


async def ask_agent(db: AsyncSession, user_id: uuid.UUID, dashboard_id: object, source_id: str, definition: dict) -> None:
    """Ask the question of an ``ask`` source as *user_id*, to an agent they may chat with; the answer comes as its run ends."""
    from app.core.redis import redis_client
    from app.services.sub_agent import create_sub_agent_task

    source = (definition.get("dataSources") or {}).get(source_id)
    if not isinstance(source, dict) or source.get("type") != "ask":
        raise AskRefusedError("unsupported")
    options = source.get("options") or {}
    agent = await _visible_agent(db, user_id, options.get("agent_id"))
    # An agent kept out of chat (``disable_chat``) is not asked from a dashboard either.
    if agent is None or agent.enabled is False or getattr(agent, "disable_chat", False):
        raise AskRefusedError("access_denied")
    if not await _count_ask(user_id):
        raise AskRefusedError("rate_limited")
    task = await create_sub_agent_task(
        None,
        user_id=str(user_id),
        parent_agent_id=str(agent.id),
        target_agent_id=str(agent.id),
        task_description=str(options.get("question")),
        allow_self=True,
        extra_metadata={"origin": "dashboard_ask", "dashboard_id": str(dashboard_id), "data_source": source_id},
    )
    if task is None:
        raise AskRefusedError("upstream_error")
    remembered = json.dumps({"task_id": str(task.id), "fingerprint": ask_fingerprint(options)})
    try:
        await (await redis_client()).set(ask_key(dashboard_id, source_id, user_id), remembered, ex=ASK_MEMORY_SECONDS)
    except Exception as exc:  # the run goes on; its answer cannot be found again
        logger.warning("Dashboard ask: the question could not be remembered")
        raise AskRefusedError("upstream_error") from exc


# ── What a writer may point a source at ──────────────────────────────────────


#: The reference sources checked here (a platform one is checked by ``custom_dashboards.check_platform_integrations``).
_CHECKED_HERE = tuple(t for t in REFERENCE_SOURCE_TYPES if t != "platform")


def _reference_sources(spec: object) -> dict[str, tuple[str, dict, str]]:
    """The sources of *spec* that read something of the platform's: ``{id: (type, options, key)}``."""
    if not isinstance(spec, dict):
        return {}
    found = {}
    for sid, source in (upgrade(spec).get("dataSources") or {}).items():
        if isinstance(source, dict) and source.get("type") in _CHECKED_HERE:
            options = source.get("options") or {}
            found[sid] = (source["type"], options, json.dumps([source["type"], options], sort_keys=True, default=str))
    return found


async def check_source_references(db: AsyncSession, user_id: uuid.UUID | None, previous: object, spec: dict) -> None:
    """Refuse a new or changed assignment, ask or variable source pointing at what the writer cannot reach.

    The same rule as reading it, the writer's own reach without an admin
    bypass: an assignment whose agent they see, an agent they may chat with,
    a variable they read that is not secret. Like a platform source, these
    sources are the Dashboard Studio's.
    """
    from app.api.v1.helpers import find_user_assignment
    from app.core.feature_flags import is_feature_enabled

    before = {sid: key for sid, (_t, _o, key) in _reference_sources(previous).items()}
    changed = [(sid, kind, options) for sid, (kind, options, key) in _reference_sources(spec).items() if before.get(sid) != key]
    if not changed:
        return
    if not is_feature_enabled("DASHBOARD_STUDIO"):
        raise DashboardSpecError(
            f"Data source '{changed[0][0]}' reads an assignment, an agent or a variable, which needs the Dashboard "
            "Studio; it is not enabled on this platform."
        )
    if user_id is None:
        raise DashboardSpecError(f"Data source '{changed[0][0]}' needs a person to read it as.")
    groups = await _group_ids(db, user_id)
    for sid, kind, options in changed:
        if kind == "assignment":
            if await find_user_assignment(db, user_id, str(options.get("assignment_id")), groups) is None:
                raise DashboardSpecError(f"Data source '{sid}': assignment_id is not an assignment you can see.")
        elif kind == "ask":
            agent = await _visible_agent(db, user_id, options.get("agent_id"))
            if agent is None or agent.enabled is False or getattr(agent, "disable_chat", False):
                raise DashboardSpecError(f"Data source '{sid}': agent_id is not an agent you can chat with.")
        elif kind == "integration":
            try:
                await _read_tool(db, user_id, options.get("integration_id"), str(options.get("tool") or ""))
            except LiveError as exc:
                reason = {
                    "unsupported": "it is not a tool that only reads",
                    "access_denied": "you cannot use this integration with your own account",
                }.get(exc.code, "it is not a tool of an integration you can use")
                raise DashboardSpecError(f"Data source '{sid}': tool '{options.get('tool')}': {reason}.") from None
        elif kind == "knowledge":
            try:
                await _readable_document(db, user_id, options)
            except LiveError:
                raise DashboardSpecError(f"Data source '{sid}': document_id is not a document of a knowledge base you read.") from None
        elif kind == "variable":
            if _variables_disabled():
                raise DashboardSpecError(f"Data source '{sid}': custom variables are disabled on this platform.")
            variable = await _visible_variable(db, user_id, options.get("variable_id"))
            if variable is None or variable.is_secret:
                raise DashboardSpecError(f"Data source '{sid}': variable_id is not a variable you can read (a secret one never is).")
