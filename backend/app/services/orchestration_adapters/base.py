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

"""Orchestration adapter contract.

An adapter plugs a specific job type's behaviour into the generic engine. The
engine owns scheduling / leasing / retries / recovery; the adapter owns "what
the work actually is" and "how to clean up if it dies".

Adapters never touch the engine directly — they receive an ``AdapterContext``
(DB session + worker id) and return an ``AdapterResult`` describing the outcome.
The reconciler maps the result onto the engine's terminal transitions. This
keeps adapters unit-testable without a live reconciler.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.orchestration import OrchestrationExecution, OrchestrationOutboxEvent


@dataclass
class AdapterContext:
    """Envelope handed to every adapter call.

    ``db`` is a live session whose transaction the reconciler commits after the
    adapter returns. ``worker_id`` identifies the reconciler instance (also the
    lease owner) for diagnostics.

    ``cancel_event`` means "stop this execution". Only an adapter that opts in
    gets one, in either of two ways; otherwise it is ``None``:

    * ``cancel_poll_seconds``: the reconciler fires it the instant this
      execution's row goes terminal out-of-band — an operator cancel / delete
      flips it ``cancelled`` from the web process;
    * ``stop_on_lease_loss``: the lease heartbeat fires it when the lease is
      lost, so this worker stops writing to reassigned work. The row
      is still ``running`` then: "event set" does not mean "row terminal".

    A long-running ``execute`` (and any ``run_agent_chat`` it drives) should
    thread it through and bail promptly so a stop takes effect NOW rather than
    at the end of the current cycle. The lifecycle hooks (``periodic``,
    ``on_dead_letter``, outbox handling) never get one.

    ``stopping`` is set once this worker stops (a deploy, a restart): work that
    can resume elsewhere may hand its execution back (``AdapterResult.defer``,
    no attempt spent) before the worker goes, instead of leaving its lease to
    expire. ``draining`` is set from the moment a stop is asked, before
    it: the worker claims nothing new meanwhile.

    ``give_back`` is given to an execution running in another adapter's idle
    slot (``lends_idle_slots_to``), and set when that adapter takes the slot
    back (``takes_back_lent_slots_for``); and to an execution in its own
    slot of an adapter that keeps its slots (``keeps_slot_through``),
    set when an execution that held one before it and lost it to a stopping or
    a gone worker needs it: work that can resume may hand its execution back
    the same way, its place in the queue kept.
    """

    db: AsyncSession
    worker_id: str
    cancel_event: asyncio.Event | None = None
    stopping: asyncio.Event | None = None
    draining: asyncio.Event | None = None
    give_back: asyncio.Event | None = None


@dataclass(frozen=True)
class AdapterResult:
    """The outcome of an adapter ``execute`` / ``handle_event`` call.

    Use the classmethods rather than constructing directly.
    """

    kind: str  # "completed" | "failed" | "await" | "defer"
    error: str | None = None
    retryable: bool = True
    adapter_state: str | None = None
    deadline_at: datetime | None = None
    payload_update: dict | None = None
    # Seconds before a retryable failure is retried; ``None`` keeps the engine's
    # exponential backoff. Attempts and the deadline bound it either way. For a
    # deferral: the seconds until the execution is due again.
    backoff_seconds: float | None = None
    # For a deferral: where it waits in its adapter's queue
    # (``orchestration_engine.DEFER_PLACES``).
    place: str = "after"
    # For a deferral: ``(worker, seconds)``, the worker it is handed to, only
    # that one claiming it for that long.
    claim_for: tuple[str, float] | None = None
    # For a deferral that keeps its place: the worker that cannot start it yet,
    # the only one that waits before claiming it again.
    not_for: str | None = None
    # For a deferral: ``(version, seconds)``, the release whose workers do not
    # claim it for that long, a worker of any other release at once.
    not_release: tuple[str, float] | None = None

    @classmethod
    def completed(cls, *, payload_update: dict | None = None) -> AdapterResult:
        return cls(kind="completed", payload_update=payload_update)

    @classmethod
    def failed(
        cls,
        error: str,
        *,
        retryable: bool = True,
        backoff_seconds: float | None = None,
    ) -> AdapterResult:
        return cls(
            kind="failed",
            error=error,
            retryable=retryable,
            backoff_seconds=backoff_seconds,
        )

    @classmethod
    def await_event(
        cls,
        *,
        adapter_state: str,
        deadline_at: datetime | None = None,
    ) -> AdapterResult:
        return cls(kind="await", adapter_state=adapter_state, deadline_at=deadline_at)

    @classmethod
    def defer(
        cls,
        reason: str,
        *,
        delay_seconds: float,
        adapter_state: str,
        payload_update: dict | None = None,
        place: str = "after",
        claim_for: tuple[str, float] | None = None,
        not_for: str | None = None,
    ) -> AdapterResult:
        """The work could not start for a reason outside it: hand it back to
        wait, due again in ``delay_seconds``, without consuming an attempt
        (``orchestration_engine.defer``). The adapter bounds how long.

        *place*: ``after`` the jobs due before then, ``keep`` its own place in
        the queue (a job waiting its turn), or ``head`` (work a stopping
        worker hands back). *claim_for*: ``(worker, seconds)``, the worker
        that runs it, only that one claiming it for that long (work this
        worker cannot start and another can). *not_for*: with ``keep``,
        the worker that cannot start it yet - only that one waits the delay,
        any other claims it at once."""
        return cls(
            kind="defer",
            error=reason,
            adapter_state=adapter_state,
            payload_update=payload_update,
            backoff_seconds=delay_seconds,
            place=place,
            claim_for=claim_for,
            not_for=not_for,
        )


class OrchestrationAdapter(ABC):
    """Behaviour for one ``adapter`` discriminator value."""

    #: Discriminator matching ``OrchestrationExecution.adapter``.
    name: str = ""

    #: Optional DEDICATED concurrency budget for this adapter's executions.
    #:
    #: ``None`` (the default) means "share the reconciler's global execution
    #: pool" (``max_concurrent_runs + max_concurrent_sub_agent_tasks``). A
    #: positive int gives the adapter its OWN semaphore + claim accounting,
    #: separate from the shared pool — so a burst of one long-lived workload
    #: (e.g. dozens of autonomous pentest engagements) can't starve normal
    #: assignment / sub-agent execution, and vice-versa. Override as a property
    #: to read a live setting. Its semaphore is built at the first use and
    #: grows with ``slots_added``, never shrinks.
    concurrency_limit: int | None = None

    #: How many of its dedicated ``concurrency_limit`` slots THIS worker holds back now.
    #:
    #: ``0`` (the default). An adapter whose work competes with another for the worker's CPUs (the Video Studio preview
    #: lane beside a whole render) returns the slots it keeps free for now, as a property read every cycle: the
    #: reconciler claims none into them, and the executions already running keep theirs.
    slots_held_back: int = 0

    #: How many slots THIS worker adds now above its dedicated ``concurrency_limit``: ``slots_held_back``'s
    #: counterpart.
    #:
    #: ``0`` (the default). An adapter whose lane may grow with its queue while the worker has the room (the Video
    #: Studio preview lane, bounded by the CPUs a preview needs) returns the slots it adds for now, as a property read
    #: every cycle: the reconciler claims into them and grows the adapter's semaphore to them; when the addition falls,
    #: the executions already running keep their slots and none is claimed until the lane is back under its slots.
    slots_added: int = 0

    #: The CPUs this adapter's running work reserves on THIS worker now.
    #:
    #: ``0`` (the default). An adapter whose work needs the worker's CPUs whole (a Video Studio render's pages)
    #: returns those it holds, as a property read every cycle: while any adapter reserves some, the work of the
    #: shared pool goes to a worker that reserves none first (``orchestration_reconciler``), and runs here on what
    #: is left only when no such worker takes it.
    cpus_reserved: int = 0

    #: Whether THIS worker claims the adapter's executions.
    #:
    #: ``True`` (the default). ``False`` - a worker configured to run none of
    #: them, such as a Video Studio worker that draws scene previews and no
    #: whole renders - leaves them to the workers that do: it claims none, lends
    #: none of its slots to them, and its presence does not list it
    #: (``worker_presence.count(claiming=...)``). Override as a property to read
    #: a live setting; its ``periodic`` sweep still runs.
    claims_here: bool = True

    #: Whether a LOST LEASE fires ``ctx.cancel_event`` for this adapter.
    #:
    #: ``False``: when the heartbeat finds the work reassigned it stops beating,
    #: the work runs to the end of its unit, and the lease check rejects its
    #: final write. ``True`` stops the work within one heartbeat interval -- for
    #: adapters whose every write is ownership-checked and whose half-done unit
    #: is safe to abandon. Only the evaluation adapter opts in; work that
    #: did not opt in must not change. The fast operator-cancel watcher below is
    #: independent of this.
    stop_on_lease_loss: bool = False

    #: Whether this adapter's executions run agent turns that must have one writer.
    #:
    #: ``False`` (the default). ``True`` binds a single-writer check around ``execute``
    #: (``core.turn_writer``): before every tool round the agent loop reads the execution row, and a turn whose
    #: execution another attempt took over (taken back, claimed by another worker) stops there with
    #: ``TurnSuperseded``, writing nothing. For work that resumes from its checkpoints on another worker (background
    #: tasks, assignment runs), where a worker that lost its lease must not go on calling tools beside the attempt
    #: that replaced it.
    single_writer_turns: bool = False

    #: Optional fast operator-cancel poll cadence (seconds) for a LONG-RUNNING
    #: ``execute``.
    #:
    #: ``None`` / non-positive (the default) means "no fast cancel poll": a
    #: cancel is only noticed by the lease heartbeat (``lease/3`` ~= 20s) or at
    #: the adapter's own next state re-read. Set a small positive value (as a
    #: property reading a live setting) to have the reconciler poll this
    #: execution's row every ``cancel_poll_seconds`` and fire ``ctx.cancel_event``
    #: the instant it goes terminal — for adapters whose ``execute`` can run for
    #: many seconds/minutes (autonomous bursts) so an operator cancel aborts the
    #: in-flight agent loop between iterations instead of after the whole cycle.
    cancel_poll_seconds: float | None = None

    #: Optional lease (seconds) this adapter's executions are claimed and
    #: heartbeated under, instead of ``ORCHESTRATION_LEASE_SECONDS``.
    #:
    #: ``None`` (the default) keeps the engine's lease. A longer one suits work
    #: that runs for many minutes at a stretch and resumes from its own
    #: checkpoints (a Video Studio render): a heartbeat late by a minute
    #: no longer throws the attempt away. The reconciler renews it every third
    #: of its length; crash recovery waits that much longer for its executions.
    lease_seconds: int | None = None

    #: Whether this adapter's executions are resumed first, and at once, when
    #: their worker goes.
    #:
    #: ``False`` (the default) leaves an execution whose worker died to its
    #: lease: it runs again once the lease expired, behind the queue. ``True``
    #: suits work that resumes from its own checkpoints and is harmless to run
    #: twice (a Video Studio render): the reconciler takes it back the moment
    #: the holder is known gone (``worker_presence``), and puts it - and one
    #: whose lease expired - first in the adapter's queue.
    resume_when_worker_gone: bool = False

    #: The ``adapter_state`` values of its own pending executions for which it takes back a slot it lent
    #: (``lends_idle_slots_to``).
    #:
    #: Empty (the default): a lent slot is held until the execution in it ends. An adapter whose work left by a stopped
    #: worker must not wait behind the work it lent its slot to (a Video Studio render) names those states: once such
    #: an execution has waited due for a reconciler interval or two, the reconciler sets ``AdapterContext.give_back``
    #: on an execution in a slot it lent on that worker, which hands its execution back and frees the slot.
    takes_back_lent_slots_for: tuple[str, ...] = ()

    #: The ``adapter_state`` values in which an execution of this adapter waits with the slot it held: a
    #: stopping worker handed it back, or it was taken back from a worker gone.
    #:
    #: Empty (the default): a slot is held while an execution runs, and freed when it is handed back. Set, an
    #: execution keeps its slot from its first claim to its end (``claim_due(keep_slot=True)``): a running worker
    #: keeps a slot free for each one in flight on a worker asked to stop, and when one waits in these states with
    #: no slot free, the worker running the execution that took its slot last gives that slot back - its execution
    #: handed back in its place (``AdapterContext.give_back``), which the adapter must honour - so the one waiting
    #: takes it within a cycle, before anything queued.
    keeps_slot_through: tuple[str, ...] = ()

    #: Whether an execution taken back from a worker gone WITHOUT a stop request gets its attempt back.
    #:
    #: False (the default): a worker that went without a word may have died of that very work, so the attempt is
    #: spent. True for an adapter whose work keeps its progress across workers (a Video Studio render resumes from
    #: its kept frames), where a node lost under a rolling deploy must not exhaust ``max_attempts``: the engine
    #: gives the attempt back when the holder's last record shows no sign of an out-of-memory kill, a bounded number
    #: of times (``orchestration_engine.MAX_SILENT_LOSSES_GIVEN_BACK``), counted apart (``infra_relaunches``).
    gives_back_silent_losses: bool = False

    @staticmethod
    def _configured_cancel_poll() -> float | None:
        """``settings.orchestration_cancel_poll_seconds`` as a poll cadence.

        Shared by the adapters whose ``execute`` drives an agent loop for
        minutes (assignment, sub-agent, flow, evaluation) so an emergency stop
        or an operator cancel aborts them between iterations. Read live, not
        at import, so a settings change applies to the next execution.
        """
        from app.core.config import settings

        poll = settings.orchestration_cancel_poll_seconds
        return float(poll) if isinstance(poll, (int, float)) and poll > 0 else None

    def raised_attempt_bound(self, max_attempts: int) -> int:
        """The attempt bound an execution of this adapter created at *max_attempts* runs under now.

        The bound kept (the default). An adapter whose bound grew after some of its executions were queued names
        the new one for a row still at the old: the engine reads it before it decides that a row it takes back
        (a worker gone, a lease expired) has no attempt left, in the same transaction as that take-back, so a row
        queued before the change is not dead-lettered at the old bound before its next claim raises it. Pure: no
        I/O, never raises.
        """
        return max_attempts

    @abstractmethod
    async def execute(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> AdapterResult:
        """Run one unit of work for a claimed (``running``) execution.

        Return ``AdapterResult.completed()`` / ``.failed(...)`` / ``.await_event(...)``.
        Raising is treated as a retryable failure by the reconciler.
        """
        raise NotImplementedError

    async def on_dead_letter(
        self, ctx: AdapterContext, execution: OrchestrationExecution
    ) -> None:
        """Clean up after the engine dead-letters this execution.

        Called when a deadline is exceeded or attempts are exhausted (e.g. mark
        the domain ``AssignmentRun`` / ``EvaluationRun`` failed). Default no-op.
        """
        return None

    async def handle_event(
        self,
        ctx: AdapterContext,
        execution: OrchestrationExecution,
        event: OrchestrationOutboxEvent,
    ) -> AdapterResult | None:
        """Drive a transactional-outbox event against this execution.

        Used for resume-from-callback / chaining flows. Return an
        ``AdapterResult`` to have the reconciler apply a terminal/await
        transition (lease-free), or ``None`` if the handler fully managed the
        execution state itself (e.g. re-armed it for re-claim). Default no-op.
        """
        return None

    async def periodic(self, ctx: AdapterContext) -> None:
        """Run once per reconciler cycle for adapter-specific domain sweeps.

        Cheap, idempotent maintenance that isn't tied to a specific execution
        (e.g. timing out evaluation episodes waiting on an external callback).
        Default no-op.
        """
        return None

    def presence(self) -> dict:
        """What this adapter adds to its worker's presence record (``worker_presence.publish``), read when the
        record is written: what other workers price their work against. Empty by default; never raises."""
        return {}
