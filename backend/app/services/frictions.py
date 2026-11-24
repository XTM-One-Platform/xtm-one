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

"""The friction ledger: what a run struggled with, recorded and disposed of.

A friction (``app/models/agent_friction.py``) is recorded two ways:

* by the platform itself, with no model call: :func:`note_tool_error` is
  called by the agentic loop on every tool result that is an error, and
  records it under the :class:`FrictionScope` the run bound with
  :func:`binding_friction_scope`. Outside a bound scope it does nothing, so
  a path that has not opted in is unchanged.
* by the agent, through the coordination tools ``record_friction``,
  ``list_frictions`` and ``dispose_friction``
  (``services/builtin_tools/frictions.py``).

Within one run a friction is unique by (kind, tool, evidence hash): the same
error met again raises ``count`` and ``last_seen_at`` instead of adding a row.
The evidence is bounded (``FRICTION_EVIDENCE_MAX_CHARS``) and hashed after
its digits, ids and whitespace are normalised, so "timeout after 31 s" and
"timeout after 33 s" are one friction.

Best-effort by contract for the automatic path: a ledger failure is logged
and never changes the tool result or the run.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import uuid
from collections.abc import Coroutine, Iterator, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update

from app.core.utils import format_exception_message, safe_uuid
from app.models.agent_friction import (
    FRICTION_CLASSES,
    FRICTION_DISPOSITIONS,
    FRICTION_EVIDENCE_MAX_CHARS,
    FRICTION_KINDS,
    AgentFriction,
)

logger = logging.getLogger(__name__)

#: A listing's page bound.
LIST_MAX_LIMIT = 100


@dataclass(frozen=True)
class FrictionScope:
    """Who a friction recorded now belongs to: the agent, the person, the run."""

    agent_id: uuid.UUID
    user_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    assignment_id: uuid.UUID | None = None
    work_item_id: uuid.UUID | None = None


_scope: ContextVar[FrictionScope | None] = ContextVar("friction_scope", default=None)

#: Automatic records in flight, held so they are not garbage-collected before they run.
_PENDING: set[asyncio.Task] = set()


def make_scope(
    agent_id: object,
    *,
    user_id: object = None,
    run_id: object = None,
    assignment_id: object = None,
    work_item_id: object = None,
) -> FrictionScope | None:
    """A :class:`FrictionScope` from loosely typed ids; ``None`` without a valid agent id."""

    def _uuid(value: object) -> uuid.UUID | None:
        if value is None or value == "":
            return None
        return value if isinstance(value, uuid.UUID) else safe_uuid(str(value))

    agent = _uuid(agent_id)
    if agent is None:
        return None
    return FrictionScope(
        agent_id=agent,
        user_id=_uuid(user_id),
        run_id=_uuid(run_id),
        assignment_id=_uuid(assignment_id),
        work_item_id=_uuid(work_item_id),
    )


@contextlib.contextmanager
def binding_friction_scope(scope: FrictionScope | None) -> Iterator[None]:
    """Record the automatic frictions met inside the block under *scope* (``None``: none)."""
    token = _scope.set(scope)
    try:
        yield
    finally:
        _scope.reset(token)


def current_friction_scope() -> FrictionScope | None:
    return _scope.get()


_NORMALISE = (
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "_ID_"),
    (re.compile(r"\b[0-9a-f]{12,64}\b", re.I), "_HEX_"),
    (re.compile(r"\d+"), "_N_"),
    (re.compile(r"\s+"), " "),
)


def bound_evidence(evidence: str | None) -> str:
    text = (evidence or "").strip()
    if len(text) <= FRICTION_EVIDENCE_MAX_CHARS:
        return text
    return text[: FRICTION_EVIDENCE_MAX_CHARS - 1] + "…"


def evidence_hash(kind: str, tool: str, evidence: str) -> str:
    """The dedupe key: the evidence's first 500 characters with ids, numbers and spaces normalised."""
    text = (evidence or "")[:500].lower()
    for pattern, repl in _NORMALISE:
        text = pattern.sub(repl, text)
    return hashlib.sha256(f"{kind}\x00{tool}\x00{text.strip()}".encode()).hexdigest()


def serialize_friction(row: AgentFriction) -> dict:
    return {
        "id": str(row.id),
        "agent_id": str(row.agent_id),
        "run_id": str(row.run_id) if row.run_id else None,
        "assignment_id": str(row.assignment_id) if row.assignment_id else None,
        "work_item_id": str(row.work_item_id) if row.work_item_id else None,
        "kind": row.kind,
        "tool": row.tool or "",
        "evidence": row.evidence or "",
        "count": row.count,
        "class": row.friction_class,
        "disposition": (
            {"kind": row.disposition_kind, "ref": row.disposition_ref} if row.disposition_kind else None
        ),
        "recorded_at": row.recorded_at.isoformat() if row.recorded_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "disposed_at": row.disposed_at.isoformat() if row.disposed_at else None,
    }


async def record_friction(
    scope: FrictionScope,
    *,
    kind: str,
    tool: str = "",
    evidence: str = "",
    disposed: tuple[str, str, str] | None = None,
) -> tuple[dict, bool]:
    """Record one friction under *scope*; returns ``(friction, created)``.

    Met again in the same run (same kind, tool and normalised evidence): the
    existing row's ``count`` goes up and nothing is added (``created`` False).
    A friction outside a run is never merged: each is its own row.

    *disposed* = ``(class, disposition, ref)`` writes a new row already
    disposed of, in the same write (a declared probe's expected error): it is
    never counted as undisposed and raises no ``run.friction_recorded``.
    """
    from app.core.database import async_session

    if kind not in FRICTION_KINDS:
        raise ValueError(f"unknown friction kind {kind!r}; one of {', '.join(FRICTION_KINDS)}")
    tool = (tool or "")[:200]
    text = bound_evidence(evidence)
    digest = evidence_hash(kind, tool, text)
    now = datetime.now(UTC)
    async with async_session() as db:
        row = None
        if scope.run_id is not None:
            row = (
                await db.execute(
                    select(AgentFriction)
                    .where(
                        AgentFriction.run_id == scope.run_id,
                        AgentFriction.kind == kind,
                        AgentFriction.tool == tool,
                        AgentFriction.evidence_hash == digest,
                    )
                    .with_for_update()
                    .limit(1)
                )
            ).scalar_one_or_none()
        created = row is None
        if row is None:
            row = AgentFriction(
                id=uuid.uuid4(),
                user_id=scope.user_id,
                agent_id=scope.agent_id,
                run_id=scope.run_id,
                assignment_id=scope.assignment_id,
                work_item_id=scope.work_item_id,
                kind=kind,
                tool=tool,
                evidence=text,
                evidence_hash=digest,
                count=1,
                friction_class="unclassified",
                recorded_at=now,
                last_seen_at=now,
            )
            if disposed is not None:
                row.friction_class, row.disposition_kind, ref = disposed
                row.disposition_ref = (ref or "")[:500] or None
                row.disposed_at = now
            db.add(row)
        else:
            row.count = (row.count or 1) + 1
            row.last_seen_at = now
        await db.commit()
        await db.refresh(row)
        friction = serialize_friction(row)
    if created and disposed is None:
        await _publish_recorded(friction, scope.user_id)
    return friction, created


async def _publish_recorded(friction: dict, user_id: uuid.UUID | None) -> None:
    """Raise ``run.friction_recorded`` for a new friction; never raises."""
    try:
        from app.services import platform_events

        await platform_events.publish(
            "run.friction_recorded",
            platform_events.friction_data(friction, user_id=user_id),
            # Stable: a second publish of the same friction starts no second run.
            event_id=f"friction:{friction['id']}",
        )
    except Exception as exc:  # best-effort: the ledger never changes the run
        logger.warning("run.friction_recorded not published: %s", format_exception_message(exc))


def _schedule(coro: Coroutine[Any, Any, Any]) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _PENDING.add(task)
    task.add_done_callback(_PENDING.discard)


async def _record_quietly(
    scope: FrictionScope,
    kind: str,
    tool: str,
    evidence: str,
    disposed: tuple[str, str, str] | None = None,
) -> None:
    try:
        await record_friction(scope, kind=kind, tool=tool, evidence=evidence, disposed=disposed)
    except Exception as exc:  # best-effort: the ledger never changes the run
        logger.warning("Friction not recorded (%s, %s): %s", kind, tool, format_exception_message(exc))


#: Tools whose own errors are not frictions of the run: recording one through
#: the ledger's own tools would loop.
_LEDGER_TOOLS = frozenset({"record_friction", "list_frictions", "dispose_friction"})

#: The top-level argument a call carries to declare its error the expected
#: outcome of a probe. The agent loop strips it before the tool runs.
EXPECT_ERROR_ARGUMENT = "expect_error"

#: The reason stored when a probe declares ``expect_error: true`` with no words.
EXPECTED_ERROR_DEFAULT_REASON = "declared expect_error"


def expected_error_reason(value: object) -> str | None:
    """The probe reason an ``expect_error`` value declares, or ``None`` when it declares none.

    ``true`` (or ``"true"``) declares a probe with the default reason; a
    non-empty string is the reason itself (bounded to 200 characters);
    ``false``, ``"false"``, an empty string or anything else declares nothing.
    """
    if value is True:
        return EXPECTED_ERROR_DEFAULT_REASON
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in {"false", "0", "no"}:
            return None
        if text.lower() in {"true", "1", "yes"}:
            return EXPECTED_ERROR_DEFAULT_REASON
        return text[:200]
    return None


def note_friction(
    kind: str,
    tool: str = "",
    evidence: str | None = "",
    *,
    scope: FrictionScope | None = None,
) -> None:
    """Record an automatic friction in the background; never raises, never waits.

    *scope* defaults to the one the run bound (:func:`binding_friction_scope`);
    with neither, nothing happens (a path that has not opted in is unchanged).
    """
    scope = scope or _scope.get()
    if scope is None or tool in _LEDGER_TOOLS or kind not in FRICTION_KINDS:
        return
    _schedule(_record_quietly(scope, kind, tool, evidence or ""))


def note_tool_error(tool: str, content: str | None) -> None:
    """Record a tool error returned to the model, in the background, under the bound scope.

    Called by the agentic loop for every result that is an error. No scope
    bound (a path that has not opted in) or a ledger tool: nothing happens.
    """
    note_friction("tool_error", tool, content)


def note_expected_error(tool: str, content: str | None, reason: str) -> None:
    """Record the error of a call declared ``expect_error`` (a probe), disposed of at write time.

    The row is ``expected_error``, class ``platform_acceptable``, disposition
    ``accepted`` with the probe's *reason* as ref: it never counts as
    undisposed and raises no ``run.friction_recorded``. Same scope rules as
    :func:`note_friction`.
    """
    scope = _scope.get()
    if scope is None or tool in _LEDGER_TOOLS:
        return
    reason = (reason or "").strip() or EXPECTED_ERROR_DEFAULT_REASON
    evidence = f"expected ({reason}): {content or ''}"
    _schedule(
        _record_quietly(scope, "expected_error", tool, evidence, ("platform_acceptable", "accepted", reason))
    )


def note_probe_passed(tool: str, reason: str) -> None:
    """Record a call declared ``expect_error`` that succeeded: undisposed, the probe's expectation was wrong."""
    reason = (reason or "").strip() or EXPECTED_ERROR_DEFAULT_REASON
    note_friction(
        "probe_passed_unexpectedly",
        tool,
        f"the call was declared expect_error ({reason}) but succeeded: the probe's expectation does not hold",
    )


def run_scope(run: Any, assignment: Any = None) -> FrictionScope | None:
    """The :class:`FrictionScope` of an assignment run, read off the run itself."""
    payload = getattr(run, "trigger_payload", None)
    context = getattr(run, "trigger_context", None)
    item = (payload if isinstance(payload, dict) else {}).get("work_item_id") or (
        context if isinstance(context, dict) else {}
    ).get("work_item_id")
    return make_scope(
        getattr(run, "agent_id", None),
        user_id=getattr(run, "user_id", None),
        run_id=getattr(run, "id", None),
        assignment_id=getattr(assignment, "id", None) or getattr(run, "assignment_id", None),
        work_item_id=item,
    )


def note_approval_pause(run: Any, assignment: Any, proposals: Any) -> None:
    """An assignment run paused on the per-tool approval gate: one friction per tool asked."""
    scope = run_scope(run, assignment)
    for proposal in proposals if isinstance(proposals, list) else []:
        if not isinstance(proposal, dict):
            continue
        tool = str(proposal.get("tool_name") or "")
        note_friction(
            "approval_pause",
            tool,
            f"the run paused for a person's approval of {tool} (source {proposal.get('source') or 'unknown'})",
            scope=scope,
        )


def note_turn_resumed(
    run: Any, *, cause: str, replayed_calls: int, graceful: bool, wait_continued: bool = False
) -> None:
    """An assignment run was resumed after its worker stopped: one friction per cause.

    *wait_continued*: the replayed rounds end on a ``wait`` the stopping worker
    cut, which the resumed turn finishes itself before its first model call
    (``agent_loop._continue_cut_wait``): the evidence says
    ``wait_continued: true``, so the resume is disposed of in one line.
    """
    note_friction(
        "turn_resumed",
        "",
        f"resumed after: {cause}; {replayed_calls} completed call(s) replayed; "
        f"{'graceful' if graceful else 'counted'} resume"
        + ("; wait_continued: true" if wait_continued else ""),
        scope=run_scope(run),
    )


#: A chat turn a person waited on longer than this between its cut and its
#: resumed answer is a person-visible cut: recorded undisposed (#11924).
PERSON_VISIBLE_CUT_SECONDS = 30.0


def note_chat_turn_resumed(
    *,
    agent_id: object,
    user_id: object,
    run_id: object,
    conversation_id: object,
    entry: str,
    channel: str | None,
    waited_seconds: float | None,
    resumes: int,
    start_latency_seconds: float | None = None,
) -> bool:
    """A chat turn cut by a platform stop answered on its automatic resume: one ``turn_resumed`` friction.

    A turn a person sent (any entry but the platform's own ``wake``) whose
    answer came more than :data:`PERSON_VISIBLE_CUT_SECONDS` after its first
    cut is a person-visible cut: written undisposed, so it is classified by
    hand and never auto-accepted. Any other resumed chat turn is written
    disposed (``platform_acceptable``, ``accepted``). The rollout counter
    ``xtmone_chat_turn_cuts_total`` counts both, by visibility and build.
    *start_latency_seconds*, how long the latest hop waited for its resume to
    start, is named in the evidence apart from the total wait, so a long wait
    reads as restart latency or as the resumed turn's own work.
    Whether the cut was person-visible; never raises.
    """
    from app.services.rollout_counters import count_chat_turn_cut

    visible = entry != "wake" and waited_seconds is not None and waited_seconds > PERSON_VISIBLE_CUT_SECONDS
    count_chat_turn_cut(visible=visible)
    scope = make_scope(agent_id, user_id=user_id, run_id=run_id)
    if scope is None:
        return visible
    waited = f"{waited_seconds:.0f} s" if waited_seconds is not None else "an unknown time"
    evidence = (
        f"chat turn of conversation {conversation_id} ({entry}, channel {channel or 'web'}) cut by a platform "
        f"stop and answered by automatic resume {resumes + 1} after {waited}"
    )
    if start_latency_seconds is not None:
        evidence += f" (resume started {start_latency_seconds:.0f} s after its latest cut)"
    if visible:
        _schedule(_record_quietly(scope, "turn_resumed", "", f"person-visible cut: {evidence}"))
    else:
        reason = "chat turn resumed within the person-visible bound"
        _schedule(_record_quietly(scope, "turn_resumed", "", evidence, ("platform_acceptable", "accepted", reason)))
    return visible


def note_sandbox_failure(usage: str, evidence: str) -> None:
    """A sandbox container could not be created (timeout, sidecar, SDK error), under the bound scope."""
    note_friction("sandbox_failure", usage, evidence)


def note_runner_failure(command_kind: str, evidence: str) -> None:
    """A runner command was refused or found no runner to run on, under the bound scope."""
    note_friction("runner_failure", command_kind, evidence)


def note_trigger_config_edit(tool: str, assignment_name: str, keys: Any, fix_applied: str = "") -> None:
    """An agent tool rewrote an assignment's trigger_config (a filter edited by hand), under the bound scope.

    *fix_applied* (a short reason naming the merged PR or the work item) declares
    the write as the planned application of a fix: the row keeps its kind but is
    written disposed (``platform_acceptable``, ``accepted``, the reason as ref),
    the way :func:`note_expected_error` disposes of a declared probe. Without it
    the row is undisposed, as before.
    """
    shown = ", ".join(sorted(str(k) for k in keys)) if isinstance(keys, dict | list | set | tuple) else ""
    evidence = f"trigger_config of assignment {assignment_name!r} rewritten by an agent tool (keys: {shown or 'none'})"
    reason = (fix_applied or "").strip()[:200]
    if not reason:
        note_friction("trigger_filter_hand_edit", tool, evidence)
        return
    scope = _scope.get()
    if scope is None or tool in _LEDGER_TOOLS:
        return
    _schedule(
        _record_quietly(
            scope,
            "trigger_filter_hand_edit",
            tool,
            f"fix applied ({reason}): {evidence}",
            ("platform_acceptable", "accepted", reason),
        )
    )


def checkout_source_writes(written: Any, workspace_paths: Any) -> dict[str, list[str]]:
    """The *written* paths that fall in a checkout, grouped by checkout directory.

    A checkout is a directory of *workspace_paths* holding a checkout record
    (``sandbox_workspace/checkout.py``, current or legacy name); the record itself
    is not a source file, nor is a Python bytecode cache an import wrote (#9518).
    Pure, for the code_interpreter read-back and its tests.
    """
    from app.services.sandbox_workspace.checkout import is_record_name, record_root
    from app.services.sandbox_workspace.paths import is_bytecode_cache

    roots = sorted(
        {root for path in workspace_paths or () if isinstance(path, str) and (root := record_root(path)) is not None},
        key=len,
        reverse=True,
    )
    found: dict[str, list[str]] = {}
    for path in written or ():
        if not isinstance(path, str) or is_record_name(path) or is_bytecode_cache(path):
            continue
        for root in roots:
            if root == "" or path.startswith(root + "/"):
                found.setdefault(root or ".", []).append(path)
                break
    return found


#: A directory whose JSON files are translation catalogs (``frontend/src/i18n/lang/en.json``, ``locales/fr.json``).
_CATALOG_DIRS = frozenset({"i18n", "locales"})


def is_i18n_catalog(path: str) -> bool:
    """A ``*.json`` file under an ``i18n/`` or ``locales/`` directory: a key catalog, edited with sandbox_json_keys."""
    if not isinstance(path, str) or not path.endswith(".json"):
        return False
    return not _CATALOG_DIRS.isdisjoint(path.split("/")[:-1])


def catalog_write_hint(root: str, paths: list[str]) -> str:
    """The line that names sandbox_json_keys when code_interpreter wrote catalogs of checkout *root*; empty otherwise."""
    catalogs = [path for path in paths if is_i18n_catalog(path)]
    if not catalogs:
        return ""
    listed = ", ".join(repr(path) for path in catalogs[:4]) + (", ..." if len(catalogs) > 4 else "")
    return (
        f"This wrote {len(catalogs)} catalog file(s) of checkout {root}: catalog keys go through "
        f"sandbox_json_keys(paths=[{listed}], set={{path: {{key: value}}}}, place={{key: {{after: sibling}}}}); "
        "get reads parity."
    )


def source_write_hint(root: str, paths: list[str]) -> str:
    """The line that names the in-place edit tools when code_interpreter wrote files of checkout *root* (#11483)."""
    if not paths:
        return ""
    return (
        f"This wrote {len(paths)} file(s) of checkout {root}: a source file is edited in place with "
        "sandbox_edit_file, written whole with sandbox_write_file, changed across files with "
        "sandbox_replace_in_files; a nested JSON value with sandbox_json_keys and a /pointer key; "
        "a generated file by the checkout's check that runs its generator."
    )


def code_interpreter_write_hints(groups: dict[str, list[str]]) -> list[str]:
    """Per checkout of *groups* (:func:`checkout_source_writes`): its catalog hint, else its source-write hint."""
    return [
        hint
        for root, paths in groups.items()
        if (hint := catalog_write_hint(root, paths) or source_write_hint(root, paths))
    ]


def note_code_interpreter_source_writes(written: Any, workspace_paths: Any) -> None:
    """code_interpreter changed files of a checkout: one friction per checkout, under the bound scope.

    A source file is edited with sandbox_edit_file / sandbox_write_file; a
    rewrite through code_interpreter is the friction the ledger counts. A
    write to an i18n catalog carries the sandbox_json_keys hint the result
    shows too (:func:`catalog_write_hint`).
    """
    if _scope.get() is None:
        return
    for root, paths in checkout_source_writes(written, workspace_paths).items():
        shown = ", ".join(paths[:5]) + (f", +{len(paths) - 5} more" if len(paths) > 5 else "")
        hint = catalog_write_hint(root, paths) or source_write_hint(root, paths)
        note_friction(
            "code_interpreter_source_write",
            "code_interpreter",
            f"code_interpreter wrote {len(paths)} file(s) of the checkout {root}: {shown}"
            + (f". {hint}" if hint else ""),
        )


async def undisposed_of_run(run_id: object) -> list[str]:
    """The ids of the frictions of run *run_id* that have no disposition yet."""
    from app.core.database import async_session

    rid = run_id if isinstance(run_id, uuid.UUID) else safe_uuid(str(run_id or ""))
    if rid is None:
        return []
    async with async_session() as db:
        rows = (
            await db.execute(
                select(AgentFriction.id)
                .where(AgentFriction.run_id == rid, AgentFriction.disposed_at.is_(None))
                .order_by(AgentFriction.recorded_at)
            )
        ).scalars().all()
    return [str(r) for r in rows]


async def undisposed_kinds_of_run(run_id: object) -> list[str]:
    """The distinct kinds of the frictions of run *run_id* with no disposition, sorted.

    Carried by ``run.completed_with_undisposed_frictions`` so a listener can
    filter on kind (a rollout's ``turn_resumed`` alone) without reading the ledger.
    """
    from app.core.database import async_session

    rid = run_id if isinstance(run_id, uuid.UUID) else safe_uuid(str(run_id or ""))
    if rid is None:
        return []
    async with async_session() as db:
        rows = (
            await db.execute(
                select(AgentFriction.kind).where(AgentFriction.run_id == rid, AgentFriction.disposed_at.is_(None))
            )
        ).scalars().all()
    return sorted({str(r) for r in rows if isinstance(r, str)})


async def run_friction_summary(db: Any, run_id: object) -> dict | None:
    """The frictions of run *run_id* in a rollup's shape, or ``None`` when it has none or is unread.

    ``{"recorded": N, "undisposed": M, "by_kind": {kind: N}}``: one entry per
    record (a friction met again raises its ``count``, not ``recorded``). Read
    in the caller's session *db*; never raises.
    """
    rid = run_id if isinstance(run_id, uuid.UUID) else safe_uuid(str(run_id or ""))
    if rid is None:
        return None
    try:
        rows = (
            await db.execute(
                select(AgentFriction.kind, AgentFriction.disposed_at.is_(None), func.count())
                .where(AgentFriction.run_id == rid)
                .group_by(AgentFriction.kind, AgentFriction.disposed_at.is_(None))
            )
        ).all()
    except Exception as exc:
        logger.debug("Frictions of run %s unread: %s", rid, format_exception_message(exc))
        return None
    by_kind: dict[str, int] = {}
    undisposed = 0
    for kind, is_undisposed, count in rows:
        by_kind[kind] = by_kind.get(kind, 0) + int(count)
        if is_undisposed:
            undisposed += int(count)
    if not by_kind:
        return None
    return {"recorded": sum(by_kind.values()), "undisposed": undisposed, "by_kind": dict(sorted(by_kind.items()))}


#: How many frictions the run page lists (the oldest first).
RUN_PAGE_MAX_FRICTIONS = 50


async def run_frictions(db: Any, run_id: object, limit: int = RUN_PAGE_MAX_FRICTIONS) -> list[dict] | None:
    """The frictions of run *run_id*, oldest first, for the run page; ``None`` when it has none or is unread.

    Each entry is :func:`serialize_friction`'s shape. Read in the caller's
    session *db* (the run page has already checked the caller may open the
    run); never raises, so an unreadable ledger leaves the page unchanged.
    """
    rid = run_id if isinstance(run_id, uuid.UUID) else safe_uuid(str(run_id or ""))
    if rid is None:
        return None
    try:
        rows = (
            await db.execute(
                select(AgentFriction)
                .where(AgentFriction.run_id == rid)
                .order_by(AgentFriction.recorded_at, AgentFriction.id)
                .limit(max(1, int(limit)))
            )
        ).scalars().all()
    except Exception as exc:
        logger.debug("Frictions of run %s unread: %s", rid, format_exception_message(exc))
        return None
    return [serialize_friction(r) for r in rows] or None


async def run_friction_summaries(db: Any, run_ids: Any) -> dict[str, dict]:
    """The frictions of each of *run_ids* in a rollup's shape, keyed by run id (``str``).

    The same shape as :func:`run_friction_summary`, read in one grouped query
    for a listing of runs; a run with no friction is absent. Read in the
    caller's session *db*; never raises (an unreadable ledger answers ``{}``).
    """
    rids = {
        rid
        for rid in (r if isinstance(r, uuid.UUID) else safe_uuid(str(r or "")) for r in run_ids or ())
        if rid is not None
    }
    if not rids:
        return {}
    try:
        rows = (
            await db.execute(
                select(AgentFriction.run_id, AgentFriction.kind, AgentFriction.disposed_at.is_(None), func.count())
                .where(AgentFriction.run_id.in_(rids))
                .group_by(AgentFriction.run_id, AgentFriction.kind, AgentFriction.disposed_at.is_(None))
            )
        ).all()
    except Exception as exc:
        logger.debug("Frictions of %d run(s) unread: %s", len(rids), format_exception_message(exc))
        return {}
    grouped: dict[str, list[tuple[str, bool, int]]] = {}
    for run_id, kind, is_undisposed, count in rows:
        grouped.setdefault(str(run_id), []).append((kind, bool(is_undisposed), int(count)))
    summaries: dict[str, dict] = {}
    for run_id, kinds in grouped.items():
        by_kind: dict[str, int] = {}
        undisposed = 0
        for kind, is_undisposed, count in kinds:
            by_kind[kind] = by_kind.get(kind, 0) + count
            if is_undisposed:
                undisposed += count
        summaries[run_id] = {
            "recorded": sum(by_kind.values()),
            "undisposed": undisposed,
            "by_kind": dict(sorted(by_kind.items())),
        }
    return summaries


#: How many undisposed frictions the finalisation line names before "and N more".
NAG_MAX_NAMED = 5


async def finalisation_nag(scope: FrictionScope | None = None) -> str | None:
    """The one system line a run is shown before it completes, or ``None``.

    Names the frictions of the bound run (*scope*, else the one bound with
    :func:`binding_friction_scope`) that have no disposition yet, after the
    automatic records still in flight are given a moment to land. ``None``
    when no run is bound, the run left none, or the ledger cannot be read:
    the loop then ends as before. Never raises.
    """
    from app.core.database import async_session

    scope = scope or _scope.get()
    if scope is None or scope.run_id is None:
        return None
    try:
        pending = list(_PENDING)
        if pending:
            await asyncio.wait(pending, timeout=2)
        async with async_session() as db:
            rows = (
                await db.execute(
                    select(AgentFriction)
                    .where(AgentFriction.run_id == scope.run_id, AgentFriction.disposed_at.is_(None))
                    .order_by(AgentFriction.recorded_at)
                )
            ).scalars().all()
    except Exception as exc:
        logger.debug("Finalisation nag of run %s unread: %s", scope.run_id, format_exception_message(exc))
        return None
    if not rows:
        return None
    named = []
    for row in rows[:NAG_MAX_NAMED]:
        what = row.kind + (f" on {row.tool}" if row.tool else "")
        times = f" x{row.count}" if (row.count or 1) > 1 else ""
        named.append(f"`{row.id}` {what}{times}")
    more = f" and {len(rows) - NAG_MAX_NAMED} more" if len(rows) > NAG_MAX_NAMED else ""
    return (
        f"[SYSTEM: frictions] {len(rows)} friction(s) of this run have no disposition: "
        f"{'; '.join(named)}{more}. Before you finish, call dispose_friction on each "
        "(class, and the work item, shared state, skill or prompt edit that carries its fix, "
        "or accepted), then give your final answer." + await _finished_item_clause(scope)
    )


async def _finished_item_clause(scope: FrictionScope) -> str:
    """The sentence the finalisation line adds when the run's own work item is already finished.

    A work item run that completed its item before its final answer is still
    given the disposal round; without this sentence it writes on the item
    again and is refused (gap work-item-autocompleted-before-friction-nudge,
    #269). Empty when the scope names no item, the item is open, or it cannot
    be read.
    """
    from app.core.database import async_session
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

    if scope.work_item_id is None:
        return ""
    try:
        async with async_session() as db:
            status = (
                await db.execute(select(WorkItem.status).where(WorkItem.id == scope.work_item_id))
            ).scalar_one_or_none()
    except Exception as exc:
        logger.debug(
            "Work item %s of the finalisation line unread: %s", scope.work_item_id, format_exception_message(exc)
        )
        return ""
    if status not in WORK_ITEM_TERMINAL_STATUSES:
        return ""
    return (
        f" Your work item `{scope.work_item_id}` is already {status}: write nothing more on it "
        "(no status, no note); this round is for dispose_friction and your final answer."
    )


async def settle_run_frictions(run: Any) -> int:
    """At a run's terminal ending, after its commit: record and announce what it left undisposed.

    Counts the run's frictions with no disposition (the automatic records
    still in flight are given a few seconds to land first), writes the count
    to ``assignment_runs.frictions_undisposed`` in a session of its own, and
    raises ``run.completed_with_undisposed_frictions``. A run that left none
    is not written and raises nothing. Never raises; returns the count.
    """
    from app.core.database import async_session
    from app.models.assignment import AssignmentRun

    try:
        pending = list(_PENDING)
        if pending:
            await asyncio.wait(pending, timeout=5)
        ids = await undisposed_of_run(run.id)
        if not ids:
            return 0
        kinds = await undisposed_kinds_of_run(run.id)
        async with async_session() as db:
            await db.execute(
                update(AssignmentRun).where(AssignmentRun.id == run.id).values(frictions_undisposed=len(ids))
            )
            await db.commit()
        run.frictions_undisposed = len(ids)
    except Exception as exc:
        logger.warning("Frictions of run %s not settled: %s", run.id, format_exception_message(exc))
        return 0
    await _publish_undisposed(run, ids, kinds)
    return len(ids)


async def _publish_undisposed(run: Any, friction_ids: list[str], kinds: list[str] | None = None) -> None:
    """Raise ``run.completed_with_undisposed_frictions``; never raises."""
    try:
        from app.services import platform_events

        await platform_events.publish(
            "run.completed_with_undisposed_frictions",
            platform_events.undisposed_frictions_data(
                run_id=run.id,
                agent_id=run.agent_id,
                assignment_id=getattr(run, "assignment_id", None),
                friction_ids=friction_ids,
                kinds=kinds or (),
            ),
            event_id=f"undisposed-frictions:{run.id}",
        )
    except Exception as exc:
        logger.warning("run.completed_with_undisposed_frictions not published: %s", format_exception_message(exc))


async def list_frictions(
    *,
    user_id: str | uuid.UUID | None,
    agent_id: str | uuid.UUID | None = None,
    run_id: str | uuid.UUID | None = None,
    undisposed: bool = False,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    kinds: list[str] | None = None,
    supervised_agent_ids: Sequence[uuid.UUID] | None = (),
) -> tuple[list[dict], int]:
    """The frictions the caller sees, newest first, and their total.

    Visibility is the person's: a friction recorded for them, or by one of
    the agents they read (``agent_id`` narrows to one agent). *kinds* keeps
    only those kinds (a run of 26k characters read for its two resumed turns).
    *supervised_agent_ids* widens it to the frictions of the agents the caller
    may supervise (:func:`supervision_clause`), ``None`` for every agent.
    """
    from app.core.database import async_session

    clauses = []
    uid = safe_uuid(str(user_id)) if user_id else None
    own = AgentFriction.user_id == uid if uid is not None else None
    widened = supervision_clause(own, supervised_agent_ids)
    if widened is not None:
        clauses.append(widened)
    visible = list(clauses)
    limit = max(1, min(int(limit or 50), LIST_MAX_LIMIT))
    offset = max(0, int(offset or 0))
    async with async_session() as db:
        # A filter is never dropped: a value that is not a full id resolves
        # as an id prefix among the caller's frictions, or is refused; a
        # dropped agent or run filter answered every friction of the person
        # (#11553).
        aid = await _filter_id(db, AgentFriction.agent_id, agent_id, "agent", visible) if agent_id else None
        if aid is not None:
            clauses.append(AgentFriction.agent_id == aid)
        rid = await _filter_id(db, AgentFriction.run_id, run_id, "run", visible) if run_id else None
        if rid is not None:
            clauses.append(AgentFriction.run_id == rid)
        if not clauses:
            return [], 0
        if undisposed:
            clauses.append(AgentFriction.disposed_at.is_(None))
        if since is not None:
            clauses.append(AgentFriction.last_seen_at >= since)
        if kinds:
            clauses.append(AgentFriction.kind.in_(list(kinds)))
        total = (await db.execute(select(func.count()).select_from(AgentFriction).where(*clauses))).scalar() or 0
        rows = (
            await db.execute(
                select(AgentFriction)
                .where(*clauses)
                .order_by(AgentFriction.last_seen_at.desc(), AgentFriction.id)
                .offset(offset)
                .limit(limit)
            )
        ).scalars().all()
    return [serialize_friction(r) for r in rows], int(total)


class FrictionNotFoundError(LookupError):
    """No friction with that id that the caller may change."""


def supervision_clause(own: Any, supervised_agent_ids: Sequence[uuid.UUID] | None) -> Any:
    """The visibility clause of a caller whose own frictions are *own* (a
    clause, or ``None`` for no person) and who may supervise
    *supervised_agent_ids*: ``()`` keeps *own* alone; a list adds those agents'
    frictions; ``None`` (an admin with the supervision surface) sees every
    friction. The grant is the trace supervision one (``traces._supervision_scope``),
    so supervising frictions opens no new boundary (#1928)."""
    from sqlalchemy import or_, true

    if supervised_agent_ids is None:
        return true()
    agents = [a for a in supervised_agent_ids if a is not None]
    if not agents:
        return own
    member = AgentFriction.agent_id.in_(agents)
    return member if own is None else or_(own, member)


async def count_run_frictions(run_id: str | uuid.UUID) -> int:
    """How many frictions *run_id* holds, whoever may read them: the answer
    that tells a caller "not visible to you" from "none recorded" (#1928)."""
    from app.core.database import async_session

    rid = safe_uuid(str(run_id))
    if rid is None:
        return 0
    async with async_session() as db:
        return int(
            (
                await db.execute(select(func.count()).select_from(AgentFriction).where(AgentFriction.run_id == rid))
            ).scalar()
            or 0
        )


async def _filter_id(db: Any, column: Any, value: object, noun: str, visible: list[Any]) -> uuid.UUID:
    """The full id a ``list_frictions`` filter *value* names: a full id as
    it is; an id prefix (8+ hexadecimal characters) for the one *column*
    value of the caller's frictions it starts. Several, none, or a value that
    is neither raise ``ValueError`` (the candidates named), so the listing
    never widens to every friction."""
    from app.services.builtin_tools.id_prefix import (
        ID_PREFIX_MIN_CHARS,
        ambiguous_text,
        distinct_ids_by_prefix,
        id_prefix,
    )

    text = str(value).strip()
    full = safe_uuid(text)
    if full is not None:
        return full
    prefix = id_prefix(text)
    if prefix is None:
        raise ValueError(
            f"{noun}_id must be a{'n' if noun[0] in 'aeiou' else ''} {noun} id or its first "
            f"{ID_PREFIX_MIN_CHARS}+ hexadecimal characters, got {text!r}"
        )
    if not visible:
        # No person to scope the lookup to: a prefix would search everyone's.
        raise ValueError(f"{noun}_id needs the full id here, got {text!r}")
    candidates = await distinct_ids_by_prefix(db, column, prefix, visible)
    if len(candidates) == 1:
        return uuid.UUID(str(candidates[0][0]))
    if candidates:
        raise ValueError(
            ambiguous_text(text, f"{noun}s with frictions", candidates).removeprefix("Error: ").removesuffix(".")
        )
    raise ValueError(f"no {noun} of your frictions has an id starting with `{text}`")


async def _full_friction_id(
    friction_id: str,
    *,
    agent_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    supervised_agent_ids: Sequence[uuid.UUID] | None = (),
) -> uuid.UUID:
    """The friction *friction_id* names, a full id or an id prefix (8+
    hexadecimal characters) of one friction of the calling agent or of the
    person it acts for. An ambiguous prefix raises ``ValueError`` naming the
    candidates; anything else that names no friction raises
    :class:`FrictionNotFoundError`."""
    from sqlalchemy import or_

    from app.core.database import async_session
    from app.services.builtin_tools.id_prefix import ambiguous_text, id_prefix, ids_by_prefix

    full = safe_uuid(friction_id or "")
    if full is not None:
        return full
    prefix = id_prefix(friction_id)
    owners = [
        c
        for c in (
            AgentFriction.agent_id == agent_id if agent_id is not None else None,
            AgentFriction.user_id == user_id if user_id is not None else None,
        )
        if c is not None
    ]
    reach = supervision_clause(or_(*owners) if owners else None, supervised_agent_ids)
    if prefix is None or reach is None:
        raise FrictionNotFoundError(friction_id)
    async with async_session() as db:
        candidates = await ids_by_prefix(db, AgentFriction, prefix, [reach], label=AgentFriction.kind)
    if len(candidates) == 1:
        return uuid.UUID(str(candidates[0][0]))
    if candidates:
        raise ValueError(ambiguous_text(friction_id, "frictions", candidates).removeprefix("Error: ").removesuffix("."))
    raise FrictionNotFoundError(friction_id)


async def _full_work_item_ref(ref: str, *, user_id: str | None) -> str:
    """*ref* with an id prefix (8+ hexadecimal characters) replaced by the
    full id of the one work item the caller sees that it starts, as the work
    item tools resolve it (``builtin_tools/id_prefix.py``). No match leaves
    *ref* as it is (the caller answers not found); several raise
    ``ValueError`` naming the candidates."""
    from app.services.builtin_tools.id_prefix import MAX_CANDIDATES, id_prefix, resolved_or_refused
    from app.services.work_item import work_items_by_id_prefix

    prefix = id_prefix(ref)
    if prefix is None:
        return ref
    items = await work_items_by_id_prefix(prefix, user_id=user_id, limit=MAX_CANDIDATES + 1)
    full, ambiguous = resolved_or_refused(ref, "work items", [(i["id"], i.get("title") or "") for i in items])
    if ambiguous:
        raise ValueError(ambiguous.removeprefix("Error: ").removesuffix("."))
    return str(full)


#: The dispositions a ``harness`` friction cannot take: a skill or prompt edit does not fix the platform,
#: and closing a harness friction with one dropped the defect out of tracking (#1686).
HARNESS_REFUSED_DISPOSITIONS: tuple[str, ...] = ("skill_edit", "prompt_edit")


async def dispose_friction(
    friction_id: str,
    *,
    agent_id: str | uuid.UUID,
    user_id: str | uuid.UUID | None,
    friction_class: str,
    disposition: str,
    ref: str = "",
    supervised_agent_ids: Sequence[uuid.UUID] | None = (),
) -> dict:
    """Classify a friction and name what carries its fix.

    A ``work_item`` disposition must name a work item the caller sees;
    ``accepted`` needs no ref; every other disposition needs one. A ``harness``
    friction never takes a skill or prompt edit (:data:`HARNESS_REFUSED_DISPOSITIONS`).
    The friction must belong to the calling agent or to the person it acts for,
    or to an agent the caller may supervise (*supervised_agent_ids*, ``None``
    for every agent: :func:`supervision_clause`).
    """
    from app.core.database import async_session

    if friction_class not in FRICTION_CLASSES or friction_class == "unclassified":
        allowed = ", ".join(c for c in FRICTION_CLASSES if c != "unclassified")
        raise ValueError(f"class must be one of {allowed}")
    if disposition not in FRICTION_DISPOSITIONS:
        raise ValueError(f"disposition must be one of {', '.join(FRICTION_DISPOSITIONS)}")
    ref = (ref or "").strip()[:500]
    if disposition != "accepted" and not ref:
        raise ValueError(f"a {disposition} disposition needs ref (the id, key or section that carries the fix)")
    if friction_class == "harness" and disposition in HARNESS_REFUSED_DISPOSITIONS:
        raise ValueError(
            f"a harness friction is fixed in the platform, not by a {disposition}: dispose it work_item "
            "(the item that carries the fix) or shared_state (the gaps register key), or classify it again"
        )
    if disposition == "work_item":
        from app.services.work_item import get_work_item

        ref = await _full_work_item_ref(ref, user_id=str(user_id) if user_id else None)
        item = await get_work_item(ref, user_id=str(user_id) if user_id else None)
        if item is None:
            raise ValueError(
                f"work item {ref} not found: give the id, or its first 8+ characters, of an item you can read"
            )
        ref = str(item.get("id") or ref)
    aid = safe_uuid(str(agent_id)) if agent_id else None
    uid = safe_uuid(str(user_id)) if user_id else None
    fid = await _full_friction_id(friction_id, agent_id=aid, user_id=uid, supervised_agent_ids=supervised_agent_ids)
    supervised = None if supervised_agent_ids is None else set(supervised_agent_ids)
    async with async_session() as db:
        row = (
            await db.execute(select(AgentFriction).where(AgentFriction.id == fid).with_for_update())
        ).scalar_one_or_none()
        if row is None or not (
            (aid is not None and row.agent_id == aid)
            or (uid is not None and row.user_id == uid)
            or supervised is None
            or row.agent_id in supervised
        ):
            raise FrictionNotFoundError(friction_id)
        row.friction_class = friction_class
        row.disposition_kind = disposition
        row.disposition_ref = ref or None
        row.disposed_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(row)
        return serialize_friction(row)
