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

"""Each orchestration attempt's agentic charge, fenced to that attempt.

An assignment run or a delegated (sub-agent) task is charged one agentic
action as its work starts. When the orchestration engine loses the lease of
its execution - a worker restart, or a heartbeat that could not reach the
database for a lease period - it runs the execution again from the start, and
the re-run is charged again. The lost attempt is not necessarily gone: losing
the lease only stops its heartbeat, the adapter body keeps running and may
still finish, or fail and credit its own charge back. So every charge is
credited back by at most one path - the re-run that replaces its attempt, that
attempt's own failure handler, or the dead-letter - and a run re-run after a
lost lease is charged at most one action in total:

- An assignment run records its charge on the run row
  (``AssignmentRun.trigger_context``) with the id and the ``attempt_count`` of
  the attempt that made it. A path credits the charge only after it has
  removed that record, under the row lock, and found it was the record it
  expected; a re-run takes only the charge of an attempt before it. An attempt
  that charges after a later attempt recorded its own charge was replaced
  before it charged: it gives its charge back and records nothing
  (``claim_charge_record``).
- A delegated task is charged inside its agent turn, whose metering receipt is
  named after the task AND the orchestration attempt
  (``task_receipt_token``). A re-run redeems the receipts of the attempts
  before it, the dead-letter those of every attempt, the charging turn's own
  failure handler its own (``quota.claim_own_receipt``); a receipt is
  redeemed once (``quota._REDEEM_RECEIPT_LUA``), and a charge that lands
  under a token already redeemed is given straight back.

Flow runs are not covered: they meter one action per agent node.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid

logger = logging.getLogger(__name__)

# ── Assignment runs: the charge recorded on the run row ─────────────────────

QUOTA_METERED_KEY = "quota_metered"
QUOTA_METERED_USER_KEY = "quota_metered_user_id"
# The attempt that made the recorded charge: its id, and the execution's
# ``attempt_count`` when it ran. Absent from a record written by a release
# before charges were fenced: such a record is credited by the dead-letter
# only, as it always was.
QUOTA_METERED_CHARGE_KEY = "quota_metered_charge"
QUOTA_METERED_ATTEMPT_KEY = "quota_metered_attempt"
# The pause cycle the attempt ran in: a HITL resume counts its attempts from 1
# again, so an attempt is placed by ``(epoch, attempt)``.
QUOTA_METERED_EPOCH_KEY = "quota_metered_epoch"
# The charge an attempt took as a lost attempt's and is crediting back
# (``take_lost_charge``): kept on the run until the credit landed
# (``settle_lost_charge``), so a worker that dies in between, or a credit that
# fails, leaves the record to the dead letter rather than losing it. Another
# attempt never takes a charge marked so.
QUOTA_METERED_SETTLING_KEY = "quota_metered_settling"
_CHARGE_RECORD_KEYS = (
    QUOTA_METERED_KEY,
    QUOTA_METERED_USER_KEY,
    QUOTA_METERED_CHARGE_KEY,
    QUOTA_METERED_ATTEMPT_KEY,
    QUOTA_METERED_EPOCH_KEY,
    QUOTA_METERED_SETTLING_KEY,
)
# ``[epoch, attempt]`` of the latest attempt that started: written by an
# attempt the engine started to replace another, and by the HITL pause, which
# opens the next epoch at attempt 0. An attempt placed before it is replaced,
# whether or not the attempt that replaced it has charged yet.
QUOTA_ATTEMPT_FENCE_KEY = "quota_attempt_fence"


@dataclass(frozen=True)
class RecordedCharge:
    """A charge recorded on an assignment run: whom it was charged to (``None``:
    no account behind the run) and which attempt made it."""

    user_id: uuid.UUID | None
    charge_id: str | None
    attempt: int | None = None
    epoch: int = 0


def _count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def new_charge_id() -> str:
    """The id of one attempt's charge."""
    return uuid.uuid4().hex


def recorded_charge(trigger_context: object) -> RecordedCharge | None:
    """The charge *trigger_context* records, or ``None``.

    A record without the user key, or with a user that does not parse, is not
    a charge that may be credited back: crediting it could give back another
    run's charge.
    """
    context = trigger_context if isinstance(trigger_context, dict) else {}
    if not context.get(QUOTA_METERED_KEY) or QUOTA_METERED_USER_KEY not in context:
        return None
    raw_user = context.get(QUOTA_METERED_USER_KEY)
    user_id = safe_uuid(str(raw_user)) if raw_user else None
    if raw_user and user_id is None:
        return None
    charge_id = context.get(QUOTA_METERED_CHARGE_KEY)
    return RecordedCharge(
        user_id=user_id,
        charge_id=charge_id if isinstance(charge_id, str) and charge_id else None,
        attempt=_count(context.get(QUOTA_METERED_ATTEMPT_KEY)),
        epoch=_count(context.get(QUOTA_METERED_EPOCH_KEY)) or 0,
    )


def attempt_fence(trigger_context: object) -> tuple[int, int]:
    """``(epoch, attempt)`` of the latest attempt *trigger_context* records as
    started; ``(0, 0)`` when none is."""
    context = trigger_context if isinstance(trigger_context, dict) else {}
    raw = context.get(QUOTA_ATTEMPT_FENCE_KEY)
    if isinstance(raw, list | tuple) and len(raw) == 2:
        epoch, attempt = _count(raw[0]), _count(raw[1])
        if epoch is not None and attempt is not None:
            return epoch, attempt
    return 0, 0


def _with_fence(trigger_context: dict | None, place: tuple[int, int]) -> dict:
    return {**(trigger_context or {}), QUOTA_ATTEMPT_FENCE_KEY: [place[0], place[1]]}


def recorded_charge_id(trigger_context: object) -> str | None:
    """The attempt whose charge *trigger_context* records, if any."""
    charge = recorded_charge(trigger_context)
    return charge.charge_id if charge is not None else None


def with_recorded_charge(
    trigger_context: dict | None,
    *,
    charge_id: str,
    user_id: uuid.UUID | None,
    attempt: int | None = None,
    epoch: int = 0,
) -> dict:
    """*trigger_context* recording the charge *charge_id* made to *user_id* by
    orchestration attempt *attempt* of pause cycle *epoch*."""
    context = {
        key: value
        for key, value in (trigger_context or {}).items()
        if key not in (QUOTA_METERED_ATTEMPT_KEY, QUOTA_METERED_EPOCH_KEY)
    }
    context.update(
        {
            QUOTA_METERED_KEY: True,
            QUOTA_METERED_USER_KEY: str(user_id) if user_id else None,
            QUOTA_METERED_CHARGE_KEY: charge_id,
        }
    )
    if attempt is not None:
        context[QUOTA_METERED_ATTEMPT_KEY] = attempt
        context[QUOTA_METERED_EPOCH_KEY] = epoch
    return context


def without_recorded_charge(trigger_context: dict | None) -> dict | None:
    """*trigger_context* without its charge record: for the run a record was
    taken from."""
    if not isinstance(trigger_context, dict):
        return trigger_context
    return {
        key: value
        for key, value in trigger_context.items()
        if key not in _CHARGE_RECORD_KEYS
    }


def without_charge_state(trigger_context: dict | None) -> dict | None:
    """*trigger_context* without its charge record and its attempt fence: for a
    new run copied from this one (a retry), which neither made that charge nor
    runs those attempts."""
    context = without_recorded_charge(trigger_context)
    if not isinstance(context, dict):
        return context
    return {key: value for key, value in context.items() if key != QUOTA_ATTEMPT_FENCE_KEY}


async def _locked_trigger_context(
    db: AsyncSession, run_id: uuid.UUID
) -> tuple[bool, dict | None]:
    """``(exists, trigger_context)`` of run *run_id* as committed, row locked
    until *db* commits.

    Never flushed first: the caller's in-memory copy was read before whatever
    another attempt committed since, and writing it would erase that.
    """
    from app.models.assignment import AssignmentRun

    contexts = (
        await db.execute(
            select(AssignmentRun.trigger_context)
            .where(AssignmentRun.id == run_id)
            .with_for_update()
            .execution_options(autoflush=False)
        )
    ).scalars().all()
    if not contexts:
        return False, None
    return True, contexts[0] if isinstance(contexts[0], dict) else None


def lost_attempt_charge(
    run: object, *, attempt: int | None, epoch: int = 0, handed_back_at: int | None = None
) -> RecordedCharge | None:
    """The charge an earlier attempt of *run* recorded and never settled, as
    loaded: the attempt the orchestration engine replaced by *attempt*.

    Never the charge of a later attempt: an attempt that only loads the run
    after the one that replaced it recorded its charge is the stale one. Never
    the charge of another pause cycle: a paused run (``awaiting_human``) keeps
    the charge of the attempt that paused it, whose work is done, and its
    resume is charged on its own.

    *handed_back_at*: the attempt a stopping worker handed the run back at.
    Its deferral gives the attempt back, so the claim resuming it
    runs at that same attempt number: the charge that attempt recorded is the
    one it lost, although it is not earlier.
    """
    if getattr(run, "status", None) == "awaiting_human":
        return None
    charge = recorded_charge(getattr(run, "trigger_context", None))
    if charge is None or charge.charge_id is None:
        return None
    if is_settling(getattr(run, "trigger_context", None)):
        # Another attempt took it and is crediting it, or died doing so: the
        # dead letter credits what it left, never a second attempt.
        return None
    if handed_back_at is not None and charge.attempt == handed_back_at == attempt and charge.epoch == epoch:
        return charge
    if (
        attempt is not None
        and charge.attempt is not None
        and (charge.epoch != epoch or charge.attempt >= attempt)
    ):
        return None
    return charge


async def fence_attempt(
    db: AsyncSession, run: object, *, attempt: int | None
) -> tuple[int, int] | None:
    """Place this attempt in *run*'s order as it starts: ``(epoch, attempt)``,
    ``None`` for a run not executed by the orchestration engine.

    An attempt the engine started to replace another (``attempt`` > 1) records
    its place on the row and commits, before the attempt it replaces could
    charge: that attempt's claim is then refused whether or not this one has
    charged yet (``claim_charge_record``). A first attempt has nothing to
    replace and writes nothing. The epoch is the one this attempt loaded: a
    pause that opened a later one since makes this attempt the stale one.
    Raises on a database failure, like ``take_lost_charge``.
    """
    if attempt is None:
        return None
    place = (attempt_fence(getattr(run, "trigger_context", None))[0], attempt)
    if attempt <= 1:
        return place
    exists, fresh = await _locked_trigger_context(db, run.id)
    if not exists:
        return place
    run.trigger_context = fresh
    if place > attempt_fence(fresh):
        run.trigger_context = _with_fence(fresh, place)
    await db.commit()
    return place


def fence_for_deferral(trigger_context: dict | None) -> dict | None:
    """*trigger_context* with the next epoch opened, for a live attempt whose
    hold the engine defers, or ``None`` when nothing needs to change.

    A deferral gives the claim back and restores the attempts counted before
    the hold (``orchestration_engine.defer``), so the next claim can carry a
    LOWER attempt number than a re-run that fenced the run during the hold (a
    worker lost mid-check). Without a new epoch that next attempt would read as
    replaced by an attempt the engine no longer runs. The next epoch at attempt
    0 keeps every attempt of before the hold replaced and every attempt after
    it live, like the HITL pause (``open_pause_epoch``). A fence still at
    attempt 0 already does that: a hold no re-run fenced writes nothing.
    """
    epoch, attempt = attempt_fence(trigger_context)
    if attempt == 0:
        return None
    return _with_fence(trigger_context, (epoch + 1, 0))


async def open_pause_epoch(db: AsyncSession, run: object) -> None:
    """Open the next pause cycle on *run* as it pauses for a human: its resume
    counts its attempts from 1 again, and every attempt placed before the pause
    is replaced. Row locked until the pause commits; raises on a database
    failure."""
    exists, fresh = await _locked_trigger_context(db, run.id)
    if not exists:
        return
    run.trigger_context = _with_fence(fresh, (attempt_fence(fresh)[0] + 1, 0))


def replaceable_charge_id(run: object) -> str | None:
    """The recorded charge an attempt's own record may replace, as it starts:
    the paused run's (its resume is charged on its own), nothing otherwise.

    Read once the lost attempt's charge was taken. A charge still recorded then
    belongs to an attempt that started after this one, which was replaced
    before it charged (``claim_charge_record``).
    """
    if getattr(run, "status", None) != "awaiting_human":
        return None
    return recorded_charge_id(getattr(run, "trigger_context", None))


async def take_lost_charge(
    db: AsyncSession, run: object, *, attempt: int | None, epoch: int = 0, handed_back_at: int | None = None
) -> RecordedCharge | None:
    """Take the charge of the attempt *attempt* replaces off *run*, so that it
    alone credits it back.

    The record is marked settling under the row lock and committed before the
    caller credits anything: that attempt's own failure handler and
    another attempt then find nothing to credit, and the record stays on the
    run until the caller's credit landed (:func:`settle_lost_charge`). A
    credit that fails, or a worker that dies before it, leaves the marked
    record to the dead letter, which credits it once. ``None`` when there is
    no such charge, or when another path took it first. Raises on a database
    failure: the record then stays for the next attempt or the dead-letter.
    """
    charge = lost_attempt_charge(run, attempt=attempt, epoch=epoch, handed_back_at=handed_back_at)
    if charge is None:
        return None
    exists, fresh = await _locked_trigger_context(db, run.id)
    if not exists:
        return None
    if recorded_charge_id(fresh) != charge.charge_id or is_settling(fresh):
        # Taken meanwhile: in memory, the run records what the row does.
        run.trigger_context = fresh
        await db.commit()
        return None
    run.trigger_context = {**(fresh or {}), QUOTA_METERED_SETTLING_KEY: charge.charge_id}
    await db.commit()
    return charge


def is_settling(trigger_context: object) -> bool:
    """Whether the charge *trigger_context* records was taken by an attempt crediting it back."""
    context = trigger_context if isinstance(trigger_context, dict) else {}
    marker = context.get(QUOTA_METERED_SETTLING_KEY)
    return isinstance(marker, str) and marker == context.get(QUOTA_METERED_CHARGE_KEY)


async def settle_lost_charge(db: AsyncSession, run: object, *, charge_id: str) -> None:
    """Take the charge *charge_id*, credited back, off *run* and commit.

    Only once its credit landed: the record :func:`take_lost_charge` marked
    settling is removed under the row lock when it still names *charge_id*.
    A record that changed meanwhile is left as it is.
    """
    exists, fresh = await _locked_trigger_context(db, run.id)
    if not exists:
        return
    if recorded_charge_id(fresh) == charge_id and is_settling(fresh):
        fresh = without_recorded_charge(fresh)
    run.trigger_context = fresh
    await db.commit()


def charge_receipt_token(run_id: object, charge_id: str) -> str:
    """The settlement receipt of the charge *charge_id* recorded on assignment
    run *run_id*: set to credited, with the counters, by whichever
    path credits it back (``quota.credit_agentic_charge_once``)."""
    return f"assignment_run_charge:{run_id}:{charge_id}"


async def credit_recorded_charge(
    run: object,
    charge: RecordedCharge,
    *,
    db: AsyncSession | None,
    reason: str,
    cause: str,
) -> bool:
    """Credit back, free, the charge *charge* recorded on *run*, at most once
    across the engine and the dead letter.

    The counters and the charge's receipt move in one Redis script
    (``quota.credit_agentic_charge_once``): a path that finds the receipt
    credited moves nothing, so a worker that dies after its credit landed and
    before it took the record off the run never has that credit replayed by
    the dead letter, and a credit that failed moved nothing, so the record
    left on the run is credited by the next path.

    ``True`` when the charge is credited back, here or already by another
    path: the caller may take its record off the run. ``False`` when the
    credit did not apply (or its answer was lost): the record must stay.
    Never raises.
    """
    from app.services import quota

    if charge.charge_id is None:
        return False
    state = await quota.credit_agentic_charge_once(
        charge_receipt_token(getattr(run, "id", None), charge.charge_id),
        charge.user_id,
        getattr(run, "agent_id", None),
        db=db,
        reason=reason,
        cause=cause,
    )
    return state in (quota.CHARGE_CREDIT_LANDED, quota.CHARGE_CREDIT_ALREADY)


async def settle_recorded_charge(
    db: AsyncSession,
    run: object,
    charge: RecordedCharge,
    *,
    reason: str,
    cause: str,
) -> bool:
    """Credit back the charge *charge* still recorded on *run*, for a path
    that found it there with no attempt left to settle it: the dead letter.

    The record is marked settling under the row lock and committed first, so
    no attempt takes it meanwhile (``lost_attempt_charge``) and the attempt's
    own failure handler leaves it (``release_charge_record``). It is credited
    through the charge's receipt (:func:`credit_recorded_charge`), and taken
    off the run only once that credit landed or had landed already
    (:func:`settle_lost_charge`): a credit that did not apply leaves the
    marked record for the next pass. ``False`` then, or when the row no longer
    records *charge*. Raises on a database failure: the record stays.
    """
    if charge.charge_id is None:
        return False
    exists, fresh = await _locked_trigger_context(db, run.id)
    if not exists:
        return False
    if recorded_charge_id(fresh) != charge.charge_id:
        run.trigger_context = fresh
        await db.commit()
        return False
    if not is_settling(fresh):
        fresh = {**(fresh or {}), QUOTA_METERED_SETTLING_KEY: charge.charge_id}
    run.trigger_context = fresh
    await db.commit()
    if not await credit_recorded_charge(run, charge, db=db, reason=reason, cause=cause):
        logger.warning(
            "Assignment run %s: charge %s could not be credited back now; its "
            "record stays for the next pass",
            getattr(run, "id", None),
            charge.charge_id,
        )
        return False
    await settle_lost_charge(db, run, charge_id=charge.charge_id)
    return True


async def claim_charge_record(
    db: AsyncSession,
    run: object,
    *,
    charge_id: str,
    attempt: int | None,
    user_id: uuid.UUID | None,
    expected: str | None,
    epoch: int = 0,
) -> bool:
    """Record this attempt's charge on *run*, once charged, unless a later
    attempt started or recorded its own charge since this one started.

    *expected* is the charge this attempt's record may replace
    (``replaceable_charge_id``); ``(epoch, attempt)`` is its place
    (``fence_attempt``). The row stays locked until *db* commits (the run's
    "running" commit), and *run*'s in-memory context is re-read from it either
    way, so that commit never writes back a context another attempt changed
    meanwhile. ``False``: this attempt was replaced before it charged, and the
    caller gives its charge back.
    """
    exists, fresh = await _locked_trigger_context(db, run.id)
    if not exists:
        run.trigger_context = with_recorded_charge(
            getattr(run, "trigger_context", None),
            charge_id=charge_id,
            user_id=user_id,
            attempt=attempt,
            epoch=epoch,
        )
        return True
    replaced = attempt is not None and attempt_fence(fresh) > (epoch, attempt)
    if replaced or recorded_charge_id(fresh) != expected:
        run.trigger_context = fresh
        return False
    run.trigger_context = with_recorded_charge(
        fresh, charge_id=charge_id, user_id=user_id, attempt=attempt, epoch=epoch
    )
    return True


async def release_charge_record(
    db: AsyncSession, run: object, *, charge_id: str
) -> bool:
    """Take this attempt's own charge off *run* before its failure handler
    credits it back, and commit.

    ``False`` when the row no longer records it - the re-run that replaced
    this attempt, or the dead-letter, credited it already - or when the row
    could not be read or written: the caller then credits nothing, and a
    record left on the row is credited by the engine. ``True`` also when the
    run is gone: nothing else can credit it.
    """
    try:
        exists, fresh = await _locked_trigger_context(db, run.id)
        if not exists:
            return True
        if recorded_charge_id(fresh) != charge_id or is_settling(fresh):
            # Gone, or taken by the re-run or the dead letter, which credits
            # it: never credited here as well.
            logger.info(
                "Assignment run %s: its charge was credited back when the "
                "orchestration engine replaced this attempt",
                run.id,
            )
            return False
        run.trigger_context = without_recorded_charge(fresh)
        await db.commit()
    except Exception:
        logger.warning(
            "Assignment run %s: could not take its charge record, so the failed "
            "run is not credited back here",
            getattr(run, "id", None),
            exc_info=True,
        )
        return False
    return True


# ── Delegated tasks: one metering receipt per attempt ───────────────────────

_TASK_RECEIPT_PREFIX = "sub_agent_task"


def task_receipt_token(task_id: str | uuid.UUID, *, attempt: int | None) -> str:
    """The metering receipt a task's agent turn writes when it is charged.

    Named after the orchestration *attempt* that runs the turn, so a re-run
    after a lost lease charges under its own receipt. ``None`` (the task is
    not run by the engine) is the task-wide receipt of an older release,
    which the dead-letter still redeems.
    """
    if attempt is None:
        return f"{_TASK_RECEIPT_PREFIX}:{task_id}"
    return f"{_TASK_RECEIPT_PREFIX}:{task_id}:{attempt}"


def _attempt_tokens(task_id: str | uuid.UUID, attempts: range) -> Iterator[str]:
    for attempt in attempts:
        yield task_receipt_token(task_id, attempt=attempt)


async def credit_lost_attempts(
    task_id: str | uuid.UUID, *, attempt: int, db: AsyncSession | None = None
) -> int:
    """Credit back, free, what the attempts before *attempt* were charged for
    task *task_id*: the engine lost them, and this attempt is charged for the
    task. Each of their receipts is redeemed once, so an attempt still alive
    can neither credit its charge again nor charge for the task any more.
    Returns how many actions came back. Never raises.
    """
    from app.services.quota import refund_agentic_usage_by_receipt

    credited = 0
    for token in _attempt_tokens(task_id, range(1, attempt)):
        if await refund_agentic_usage_by_receipt(
            token,
            reason=f"sub-agent task {task_id}: attempt lost by the orchestration engine",
            db=db,
        ):
            credited += 1
    return credited


async def credit_abandoned_task(
    task_id: str | uuid.UUID,
    *,
    attempts: int,
    db: AsyncSession | None = None,
    include_task_wide: bool = True,
) -> int:
    """Credit back, free, every charge made for task *task_id* that nothing
    credited yet - the engine gave up on it. Its attempts' receipts and, with
    *include_task_wide*, the task-wide one of an older release. Returns
    how many actions came back. Never raises.
    """
    from app.services.quota import refund_agentic_usage_by_receipt

    credited = 0
    tokens = [
        *([task_receipt_token(task_id, attempt=None)] if include_task_wide else []),
        *_attempt_tokens(task_id, range(1, max(attempts, 0) + 1)),
    ]
    for token in tokens:
        if await refund_agentic_usage_by_receipt(
            token, reason="abandoned sub-agent task", db=db
        ):
            credited += 1
    return credited
