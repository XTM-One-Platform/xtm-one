# SPDX-License-Identifier: Apache-2.0

"""Delete CTEM cases, one or many, administrators only (#1723).

A delete removes the case and everything tied to it, so its fix key is free and
the next case for that key opens fresh, with no old history:

- its members and history (``ctem_case_issues``, ``ctem_case_events``: ``ON DELETE CASCADE``);
- its open work items (gates, loop steps, fleet case items, re-tests): every
  non-terminal item whose correlation key names the case id is cancelled with
  a result that says why;
- its armed timers (loop reminders, gate and loop watches): every armed timer
  whose correlation key names the case id is cancelled;
- its signals: a signal that opened or joined the case goes back to the scope
  gate (state ``new``, decision cleared, decided again), so the scheduled case
  sweep regroups it on its fix key.

A case UUID names one case only, so a correlation key that holds it belongs to
that case whatever the fleet or step that wrote it: no key list to keep in step.
An item or timer whose key cannot hold the case id (a fleet keyed by its own
objects, the OpenAEV detection desk's ``gap``, ``retest`` and ``launch``
items) belongs to the case through its payload (#2389): ``ctem_case_id``
names the case, or ``simulation_id`` names a simulation the case records (a
validation reference or an attack-path hop), for an item filed before the
writer set the case link.

One audit row per case (who, when, reason, and the case summary, kept in the
audit row since the case is gone). ``dry_run`` answers the same summary and
writes nothing. The summary names the case's threat package: its OpenCTI draft
or Grouping lives outside XTM One and is not removed by the delete (#2328). Its
security coverage is turned off (``auto_enrichment_disable`` true, in the
package's draft while the package is drafted) with the administrator's own
OpenCTI access, so the validation module never builds a scenario for a case
that no longer exists; the summary and the audit row say what was done, and a
dry run names it and calls nothing (#2397). A
case read on the id of a deleted case answers that it was deleted, from the
audit row (:func:`deleted_case_answer`), never a bare "No case".

The source issues of a case built by a recompute (OpenCTI,
OpenAEV) are not recreated by a recompute alone: recompute never builds a case
(#1529); they come back through their signals and the gate.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ctem_case import CtemCase, CtemCaseEvent, CtemCaseIssue

logger = logging.getLogger(__name__)

#: Most cases one delete names or one filter selects.
MAX_CASES = 200
#: The ``opened_by`` filter value of a case built by a source read before signals.
OPENED_BY_LEGACY = "legacy"
#: The result written on a work item a case delete cancelled.
CANCELLED_ITEM_RESULT = "Cancelled: CTEM case {case_id} was deleted by an administrator ({actor}). {reason}"
#: The outcome written on a timer a case delete cancelled.
CANCELLED_TIMER_OUTCOME = "cancelled: CTEM case {case_id} deleted"
#: Payload field naming the CTEM case an item or timer belongs to when its key cannot (#2389).
CASE_LINK_FIELD = "ctem_case_id"
#: Payload field naming the simulation an item was derived from; matched on the case's simulations (#2389).
SIMULATION_LINK_FIELD = "simulation_id"
#: What the summary says about a threat package the delete leaves in OpenCTI (#2328).
PACKAGE_LEFT = (
    "The delete does not remove this threat package from OpenCTI: discard its draft or Grouping there, "
    "or reuse it for the next case on the same fix."
)
#: The outcome of a coverage the delete turned off (#2397).
COVERAGE_DISABLED = "auto enrichment turned off: the validation module builds no scenario from it"
#: The outcome a dry run names for a coverage (#2397).
COVERAGE_WOULD_DISABLE = "would turn auto enrichment off; nothing was called"
#: How long one coverage write may take before the delete goes on without it (#2397).
COVERAGE_TIMEOUT = 15


def origin(events: Iterable[Any]) -> str:
    """How a case came to be, from its history: ``signal``, ``split`` or ``legacy``.

    The same reading as the Cases page (``case-loop-reason.ts`` ``noSignalOrigin``):
    a case split by a person or an agent has a ``created`` event to ``manual``; a
    case a signal opened or joined names it; a case with neither was built by a
    source read before the readers recorded signals.
    """
    rows = list(events)
    if any(getattr(e, "kind", None) == "created" and getattr(e, "to_value", None) == "manual" for e in rows):
        return "split"
    for e in rows:
        kind = getattr(e, "kind", None)
        if kind == "signal" or (kind == "created" and (getattr(e, "reason", None) or "").startswith("signal ")):
            return "signal"
    return OPENED_BY_LEGACY


@dataclass
class CaseDeletion:
    """What one case delete removes (also the dry-run answer)."""

    id: str
    title: str
    state: str
    keys: list[str]
    opened_by: str
    severity: str | None
    created_at: str | None
    members: int
    events: int
    work_items: list[str] = field(default_factory=list)
    timers: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    threat_package: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None

    def summary(self) -> dict[str, Any]:
        package = {**self.threat_package, "left_in_opencti": PACKAGE_LEFT} if self.threat_package else None
        if package is not None and self.coverage is not None:
            package["coverage"] = self.coverage
        return {
            "id": self.id,
            "title": self.title,
            "state": self.state,
            "keys": self.keys,
            "opened_by": self.opened_by,
            "severity": self.severity,
            "created_at": self.created_at,
            "members": self.members,
            "events": self.events,
            "work_items": self.work_items,
            "timers": self.timers,
            "signals": self.signals,
            "threat_package": package,
        }


async def select_cases(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    case_ids: Iterable[uuid.UUID] = (),
    states: Iterable[str] = (),
    opened_by: str | None = None,
    vertical: str | None = None,
) -> tuple[list[CtemCase], list[str]]:
    """The cases a delete names (ids) or selects (a filter); ``(cases, unknown ids)``.

    Ids and a filter combine: a named case must also match the filter. At most
    :data:`MAX_CASES` cases are answered (the oldest first).
    """
    query = select(CtemCase).where(CtemCase.organization_id == organization_id)
    wanted = list(dict.fromkeys(case_ids))
    if wanted:
        query = query.where(CtemCase.id.in_(wanted))
    state_list = [s for s in states if s]
    if state_list:
        query = query.where(CtemCase.state.in_(state_list))
    if vertical:
        query = query.where(CtemCase.vertical == vertical)
    cases = list((await db.execute(query.order_by(CtemCase.created_at.asc()))).scalars())
    if opened_by:
        history = await _events(db, [c.id for c in cases])
        cases = [c for c in cases if origin(history.get(c.id, [])) == opened_by]
    found = {c.id for c in cases}
    unknown = [str(cid) for cid in wanted if cid not in found]
    return cases[:MAX_CASES], unknown


async def _events(db: AsyncSession, case_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[CtemCaseEvent]]:
    out: dict[uuid.UUID, list[CtemCaseEvent]] = {cid: [] for cid in case_ids}
    if not case_ids:
        return out
    for row in (await db.execute(select(CtemCaseEvent).where(CtemCaseEvent.case_id.in_(case_ids)))).scalars():
        out.setdefault(row.case_id, []).append(row)
    return out


def case_simulations(case: Any) -> list[str]:
    """The simulation ids *case* records: its validation references and its attack-path hops (#2389)."""
    found: list[str] = []
    for entry in getattr(case, "validations", None) or []:
        ref = str((entry or {}).get("ref") or "").strip() if isinstance(entry, dict) else ""
        if ref:
            found.append(ref)
    path = getattr(case, "attack_path", None) or {}
    for hop in (path.get("hops") or []) if isinstance(path, dict) else []:
        sim = str((hop or {}).get("simulation") or "").strip() if isinstance(hop, dict) else ""
        if sim:
            found.append(sim)
    return list(dict.fromkeys(found))


def _uuid_like(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _belongs_to(column: Any, payload: Any, case: Any, *, text_column: Any = None) -> Any:
    """The rows of *case*: its id in the key, its id in the payload case link, or one of its simulations.

    A simulation matches the payload ``simulation_id``; on a work item filed before
    any link field existed, a simulation UUID written in its description matches too
    (a UUID names one simulation only, so a free-text hit cannot belong to another case).
    """
    case_id = str(case.id)
    clauses = [column.contains(case_id), payload[CASE_LINK_FIELD].as_string() == case_id]
    simulations = case_simulations(case)
    if simulations:
        clauses.append(payload[SIMULATION_LINK_FIELD].as_string().in_(simulations))
        if text_column is not None:
            clauses.extend(text_column.contains(sim) for sim in simulations if _uuid_like(sim))
    return or_(*clauses)


async def _open_items(db: AsyncSession, case: Any) -> list[Any]:
    from app.models.work_item import WORK_ITEM_TERMINAL_STATUSES, WorkItem

    return list(
        (
            await db.execute(
                select(WorkItem).where(
                    _belongs_to(WorkItem.correlation_key, WorkItem.payload, case, text_column=WorkItem.description),
                    WorkItem.status.notin_(WORK_ITEM_TERMINAL_STATUSES),
                )
            )
        ).scalars()
    )


async def _armed_timers(db: AsyncSession, case: Any) -> list[Any]:
    from app.models.platform_timer import TIMER_ARMED, PlatformTimer

    return list(
        (
            await db.execute(
                select(PlatformTimer).where(
                    _belongs_to(PlatformTimer.correlation_key, PlatformTimer.payload, case),
                    PlatformTimer.state == TIMER_ARMED,
                )
            )
        ).scalars()
    )


async def _signals(db: AsyncSession, case_id: uuid.UUID) -> list[Any]:
    from app.models.ctem_signal import CtemSignal

    return list((await db.execute(select(CtemSignal).where(CtemSignal.case_id == case_id))).scalars())


async def plan(db: AsyncSession, case: CtemCase, history: list[CtemCaseEvent]) -> CaseDeletion:
    """What deleting *case* removes, read without writing."""
    from app.services.ctem_cases_view import threat_package_of

    members = (await db.execute(select(CtemCaseIssue.id).where(CtemCaseIssue.case_id == case.id))).all()
    created = getattr(case, "created_at", None)
    return CaseDeletion(
        id=str(case.id),
        title=case.title,
        state=case.state,
        keys=list(case.keys or []),
        opened_by=origin(history),
        severity=getattr(case, "severity", None),
        created_at=created.isoformat() if created else None,
        members=len(members),
        events=len(history),
        work_items=[i.correlation_key for i in await _open_items(db, case)],
        timers=[t.correlation_key for t in await _armed_timers(db, case)],
        signals=[str(s.id) for s in await _signals(db, case.id)],
        threat_package=threat_package_of(history),
    )


def deleted_case_text(case_id: str, row: Any) -> str:
    """The answer of a case read on a deleted case, from its delete audit row (#2328)."""
    at = getattr(row, "created_at", None)
    details = getattr(row, "details", None) or {}
    when = at.isoformat() if at else "an unknown time"
    who = getattr(row, "user_email", None) or "an administrator"
    text_out = f"Case {case_id} was deleted on {when} by {who}"
    reason = details.get("reason")
    text_out += f" ({reason})." if reason else "."
    package = (details.get("case") or {}).get("threat_package")
    if package:
        name = f" {package['name']}" if package.get("name") else ""
        text_out += (
            f" Its threat package{name} (Grouping {package.get('grouping_id')}, "
            f"page {package.get('url') or 'none'}) is left in OpenCTI."
        )
    return text_out + " A new case on the same fix has a new id: find it with list_ctem_cases."


async def deleted_case_answer(db: AsyncSession, case_id: uuid.UUID) -> str | None:
    """What a case read answers for *case_id* when a delete removed it, else ``None``."""
    from app.models.audit_log import AuditLog

    row = (
        await db.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == "ctem_case", AuditLog.action == "delete", AuditLog.entity_id == case_id)
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return deleted_case_text(str(case_id), row) if row is not None else None


async def _release_signals(db: AsyncSession, case_ids: list[uuid.UUID], *, now: datetime) -> list[uuid.UUID]:
    """Send the signals of the deleted cases back to the scope gate (state ``new``, no case, no decision).

    One UPDATE for every case, no gate run and no remote read inside the delete:
    deciding each signal here read OpenCRQ once per signal and held the row locks
    for minutes on a case with a thousand signals (#1950). The released signals
    are decided again after the commit by :func:`decide_released`. Answers their ids.
    """
    from app.models.ctem_signal import CtemSignal

    if not case_ids:
        return []
    await _bound_lock_wait(db)
    ids = list((await db.execute(select(CtemSignal.id).where(CtemSignal.case_id.in_(case_ids)))).scalars())
    if ids:
        await db.execute(
            update(CtemSignal)
            .where(CtemSignal.id.in_(ids))
            .values(case_id=None, state="new", decision=None, updated_at=now)
            .execution_options(synchronize_session=False)
        )
    return ids


#: How long a delete waits for a signal row another writer holds (a CTEM recompute) before it answers busy.
LOCK_TIMEOUT = "10s"
#: What a delete answers when another writer held the signal rows past :data:`LOCK_TIMEOUT`.
LOCK_BUSY_DETAIL = (
    "The signals of these cases are being updated by a CTEM recompute; nothing was deleted. Try again in a minute."
)


def is_lock_wait(exc: BaseException) -> bool:
    """A PostgreSQL lock or statement timeout (``55P03`` lock_not_available, ``57014`` query_canceled)."""
    orig = getattr(exc, "orig", None)
    code = getattr(orig, "sqlstate", None) or getattr(getattr(orig, "__cause__", None), "sqlstate", None)
    return (
        code in {"55P03", "57014"}
        or "LockNotAvailable" in type(orig).__name__
        or "QueryCanceled" in type(orig).__name__
    )


async def _bound_lock_wait(db: AsyncSession) -> None:
    """Fail fast with :data:`LOCK_BUSY_DETAIL` instead of queueing behind a long recompute (#1950)."""
    bind = getattr(db, "bind", None)
    if getattr(getattr(bind, "dialect", None), "name", None) == "postgresql":
        await db.execute(text(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'"))


#: How many released signals one transaction of :func:`decide_released` decides.
DECIDE_BATCH = 200


async def decide_released(signal_ids: list[uuid.UUID], *, sessions: Any = None) -> int:
    """The signals a case delete released, decided again by the scope gate after its commit.

    Runs in its own sessions, :data:`DECIDE_BATCH` signals per transaction, the
    OpenCRQ read once per reader per batch (``_decide_again``). A released signal
    is never put in a case here: the scheduled case sweep groups the confirmed
    ones by fix key. A failure leaves the rest ``new`` for the on-demand gate pass
    (POST /ctem-signals/evaluate). Answers how many were decided.
    """
    from app.models.ctem_signal import CtemSignal
    from app.services import ctem_scope_gate_store as gate_store

    if sessions is None:
        from app.core.database import async_session as sessions
    decided = 0
    for start in range(0, len(signal_ids), DECIDE_BATCH):
        chunk = signal_ids[start : start + DECIDE_BATCH]
        try:
            async with sessions() as db:
                rows = list(
                    (
                        await db.execute(select(CtemSignal).where(CtemSignal.id.in_(chunk), CtemSignal.state == "new"))
                    ).scalars()
                )
                await gate_store._decide_again(db, rows)
                await db.commit()
                decided += len(rows)
        except Exception:
            logger.warning("CTEM signals released by a case delete: gate not run on %d", len(chunk), exc_info=True)
    return decided


_DECIDING: set[asyncio.Task[int]] = set()


def schedule_decide_released(signal_ids: list[uuid.UUID]) -> asyncio.Task[int] | None:
    """Start :func:`decide_released` in the background once the delete committed; ``None`` when nothing to decide."""
    if not signal_ids:
        return None
    task = asyncio.create_task(decide_released(list(signal_ids)), name="ctem-case-delete-decide")
    _DECIDING.add(task)
    task.add_done_callback(_DECIDING.discard)
    return task


def hand_off_released(result: dict[str, Any]) -> dict[str, Any]:
    """After the commit of :func:`delete_cases`: schedule the re-decision of its released signals.

    Every caller (the REST route, the ``delete_ctem_cases`` agent tool) calls it once the
    delete committed. It drops the ids from *result* and answers it with ``released``
    (their count), so no caller leaves released signals ``new`` or echoes thousands of ids.
    """
    released = result.pop("released_signals", [])
    schedule_decide_released(released)
    result["released"] = len(released)
    return result


def _package_draft(url: Any) -> str:
    """The draft id of a package recorded with its OpenCTI draft page, ``""`` otherwise (#2397)."""
    from app.services.ctem_case_loop import _DRAFT_URL

    page = str(url or "").strip()
    if not page or _DRAFT_URL.match(page) is None:
        return ""
    return page.rstrip("/").split("/data/import/draft/", 1)[1].split("/", 1)[0].lower()


async def _patch_coverage(base_url: str, headers: dict[str, str], coverage_id: str, draft_id: str) -> str | None:
    """Set ``auto_enrichment_disable`` true on *coverage_id* in *draft_id* (live when empty); the error or ``None``."""
    from app.core.egress import egress_client
    from app.integrations.providers.opencti._common import _DRAFT_HEADER, _graphql
    from app.integrations.providers.opencti.capabilities import attach_probe
    from app.integrations.providers.opencti.security_coverage import _PATCH_MUTATION

    sent = {k: v for k, v in headers.items() if k != _DRAFT_HEADER}
    if draft_id:
        sent[_DRAFT_HEADER] = draft_id
    variables = {"id": coverage_id, "input": [{"key": "auto_enrichment_disable", "value": [True]}]}
    async with egress_client(timeout=COVERAGE_TIMEOUT) as client:
        data, err = await _graphql(client, base_url, attach_probe(sent, base_url), _PATCH_MUTATION, variables)
    if err:
        return err
    if not ((data or {}).get("securityCoverageFieldPatch") or {}).get("id"):
        return f"OpenCTI answered no coverage {coverage_id}"
    return None


#: Most coverage writes one delete runs at once (#2397).
COVERAGE_CONCURRENCY = 8
#: What the summary says when the administrator has no OpenCTI access (#2397).
COVERAGE_NO_ACCESS = "not turned off: no OpenCTI access for this administrator; turn it off in OpenCTI"


async def retire_coverage(
    cred: tuple[str, dict[str, str]] | None, package: dict[str, Any] | None, *, dry_run: bool
) -> dict[str, Any] | None:
    """Turn off the security coverage of a deleted case's threat package (#2397); what was done, ``None`` without one.

    *cred* is the administrator's own OpenCTI access (``_user_creds``). The write
    runs in the package's draft while the package is drafted, then live when
    that draft is gone (validated or deleted). The Grouping is kept (#2335). A
    dry run calls nothing. A failure never stops the delete: it is named.
    """
    coverage_id = str((package or {}).get("coverage_id") or "").strip()
    if not coverage_id:
        return None
    draft_id = _package_draft((package or {}).get("url"))
    out: dict[str, Any] = {"id": coverage_id, "draft_id": draft_id or None}
    if dry_run:
        return {**out, "action": COVERAGE_WOULD_DISABLE}
    if cred is None:
        return {**out, "action": COVERAGE_NO_ACCESS}
    from app.core.utils import format_exception_message

    base_url, headers = cred
    errors: list[str] = []
    for context in [draft_id, ""] if draft_id else [""]:
        try:
            err = await _patch_coverage(base_url, headers, coverage_id, context)
        except Exception as exc:
            err = format_exception_message(exc)
        if err is None:
            where = f"in draft {context}" if context else "live"
            return {**out, "action": f"{COVERAGE_DISABLED} ({where})"}
        errors.append(f"{'draft ' + context if context else 'live'}: {err}"[:300])
    logger.warning("CTEM case delete: coverage %s not turned off: %s", coverage_id, "; ".join(errors))
    return {**out, "action": "not turned off, turn it off in OpenCTI: " + "; ".join(errors)}


async def _retire_coverages(
    db: AsyncSession, actor: Any, packages: list[dict[str, Any] | None], *, dry_run: bool
) -> list[dict[str, Any] | None]:
    """:func:`retire_coverage` for every package, :data:`COVERAGE_CONCURRENCY` at a time, in order.

    The access is read once on *db*; the writes run concurrently with no session.
    """
    if dry_run or not any((p or {}).get("coverage_id") for p in packages):
        return [await retire_coverage(None, p, dry_run=dry_run) for p in packages]
    from app.core.utils import format_exception_message
    from app.services.ctem_metrics_collector import _user_creds

    try:
        cred = await _user_creds(db, str(getattr(actor, "id", "") or ""), "opencti")
    except Exception as exc:
        logger.info("CTEM case delete: OpenCTI access unreadable: %s", format_exception_message(exc))
        cred = None
    gate = asyncio.Semaphore(COVERAGE_CONCURRENCY)

    async def one(package: dict[str, Any] | None) -> dict[str, Any] | None:
        async with gate:
            return await retire_coverage(cred, package, dry_run=False)

    return list(await asyncio.gather(*(one(p) for p in packages)))


async def delete_cases(
    db: AsyncSession,
    cases: list[CtemCase],
    *,
    actor: Any,
    reason: str | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete *cases* as the administrator *actor* (a ``User``); the caller commits.

    Answers ``{dry_run, count, cases: [summary], released_signals: [ids]}``. A dry
    run writes nothing. The caller commits, then hands ``released_signals`` to
    :func:`schedule_decide_released`: no signal is decided and no case is opened
    inside the delete.
    """
    from app.services.audit import log_audit

    now = now or datetime.now(UTC)
    history = await _events(db, [c.id for c in cases])
    summaries: list[dict[str, Any]] = []
    actor_name = getattr(actor, "email", None) or str(getattr(actor, "id", "an administrator"))
    note = (reason or "").strip()
    deletions = [await plan(db, case, history.get(case.id, [])) for case in cases]
    # The coverages are turned off before any row lock is taken (#1950), all
    # cases at once: a delete refused afterwards (409) leaves them off, which
    # only stops an unattended scenario and is undone with one update.
    retired = await _retire_coverages(db, actor, [d.threat_package for d in deletions], dry_run=dry_run)
    for case, deletion, coverage in zip(cases, deletions, retired, strict=True):
        deletion.coverage = coverage
        summaries.append(deletion.summary())
        if dry_run:
            continue
        for item in await _open_items(db, case):
            item.status = "cancelled"
            item.needs_human = False
            item.completed_at = now
            item.result = CANCELLED_ITEM_RESULT.format(case_id=case.id, actor=actor_name, reason=note).strip()
        for timer in await _armed_timers(db, case):
            from app.models.platform_timer import TIMER_CANCELLED

            timer.state = TIMER_CANCELLED
            timer.outcome = CANCELLED_TIMER_OUTCOME.format(case_id=case.id)
            timer.ended_at = now
        await log_audit(
            db,
            user=actor,
            action="delete",
            entity_type="ctem_case",
            entity_id=case.id,
            entity_name=case.title[:255] if case.title else None,
            summary=f"Deleted CTEM case {case.title!r}" + (f": {note}" if note else ""),
            details={"reason": note or None, "case": deletion.summary(), "deleted_at": now.isoformat()},
        )
    released: list[uuid.UUID] = []
    if not dry_run:
        released = await _release_signals(db, [c.id for c in cases], now=now)
        for case in cases:
            await db.delete(case)
        await db.flush()
    return {"dry_run": dry_run, "count": len(summaries), "cases": summaries, "released_signals": released}
