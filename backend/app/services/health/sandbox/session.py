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

"""The interception of a sandbox run or a Platform Health dry-run (#8255, PRD 6.6).

A :class:`SandboxSession` is bound to a turn with :func:`sandbox_session`; every
call a composite executor dispatches in that context then goes through
:meth:`SandboxSession.intercept` (``tools.base.intercepting_dispatch``), after
the approval gate. In ``sandbox`` mode:

1. **replay first**: a call matching a recording of the source traces gets the
   recorded output, read or write, once the recording's consent still holds;
2. a read (the shared classifier, ``side_effects.tool_call_effect``, trusted
   only for a source the platform vouches for) runs live;
3. a write is intercepted - never executed - logged, and answered by a model
   simulating it from the tool's recorded outputs (``simulation``).

In ``dry_run`` mode reads run live and every write is logged and answered that
it was not executed, without a model.

Every outcome is counted on the session's :class:`SandboxReport` and stamped on
the call's trace span (``sandbox_outcome``), which keeps an answered call out of
every later replay index.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.health.sandbox import side_effects
from app.services.health.sandbox.replay import SANDBOX_OUTCOME_KEY, RecordedCall, ReplayIndex
from app.services.health.sandbox.side_effects import CallEffect
from app.tools.base import ToolCall, ToolDefinition, ToolResult, intercepting_dispatch

logger = logging.getLogger(__name__)

SessionMode = Literal["sandbox", "dry_run"]
CallOutcome = Literal["replayed", "live", "simulated", "intercepted"]

#: Whether a recording may still be used: the consent of its agent and its user,
#: read at the moment it is used (level 1's rule).
ConsentCheck = Callable[[RecordedCall], Awaitable[bool]]

#: Recorded calls kept on a report's log, and characters of each argument dump.
_LOGGED_CALLS = 200
_LOGGED_ARGUMENT_CHARS = 2000

INTERCEPTED_NOTICE = (
    "This action was intercepted by the sandbox and not executed, and no result "
    "could be simulated for it. Continue as if its outcome were unknown."
)
DRY_RUN_NOTICE = (
    "Dry-run: this action was logged and not executed. Continue as if it had "
    "been done; nothing was changed."
)


_COUNTS = ("replayed", "live_reads", "simulated_writes", "intercepted_writes", "unanswered_writes", "simulation_tokens")


@dataclass
class InterceptedCall:
    """One call a session answered other than by running it, or ran live."""

    tool: str
    effect: str
    outcome: CallOutcome
    arguments: str


@dataclass
class SandboxReport:
    """What a sandbox run or a dry-run did with its calls (exact counts)."""

    mode: SessionMode = "sandbox"
    case_id: str | None = None
    replayed: int = 0
    live_reads: int = 0
    #: Writes answered by a model: the count stated beside a verdict.
    simulated_writes: int = 0
    #: Writes not executed, however answered (replayed, simulated or not).
    intercepted_writes: int = 0
    #: Writes no model could answer: answered with ``INTERCEPTED_NOTICE``.
    unanswered_writes: int = 0
    #: Tokens the simulations used, which the caller charges to its budget.
    simulation_tokens: int = 0
    calls: list[InterceptedCall] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def add(self, other: SandboxReport) -> None:
        """Add *other*'s counts and log to this report (a flow's bursts)."""
        for name in _COUNTS:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.calls = (self.calls + other.calls)[:_LOGGED_CALLS]

    @classmethod
    def from_dict(cls, data: Any) -> SandboxReport:
        if not isinstance(data, dict):
            return cls()
        report = cls(
            mode="dry_run" if data.get("mode") == "dry_run" else "sandbox",
            case_id=data.get("case_id") if isinstance(data.get("case_id"), str) else None,
        )
        for name in _COUNTS:
            value = data.get(name)
            setattr(report, name, value if isinstance(value, int) and not isinstance(value, bool) else 0)
        report.calls = [
            InterceptedCall(**c)
            for c in (data.get("calls") or [])[:_LOGGED_CALLS]
            if isinstance(c, dict) and set(c) == {"tool", "effect", "outcome", "arguments"}
        ]
        return report


def _argument_dump(arguments: Any) -> str:
    text = json.dumps(arguments, ensure_ascii=False, default=str)
    return text if len(text) <= _LOGGED_ARGUMENT_CHARS else text[:_LOGGED_ARGUMENT_CHARS] + "…"


def _stamp_span(outcome: CallOutcome) -> None:
    from app.services.langfuse_tracing import annotate_current_tool_span

    annotate_current_tool_span({SANDBOX_OUTCOME_KEY: outcome})


#: ``(tool, arguments, grounding, description) -> (answer or None, tokens used)``.
Simulator = Callable[[str, dict[str, Any], list[RecordedCall], str], Awaitable[tuple[str | None, int]]]


@dataclass
class SandboxSession:
    """The interceptor of one sandbox case or one dry-run turn."""

    mode: SessionMode
    replay: ReplayIndex = field(default_factory=ReplayIndex)
    #: Answers a write no recording answers; ``None`` in dry-run.
    simulate: Simulator | None = None
    consent: ConsentCheck | None = None
    report: SandboxReport = field(default_factory=SandboxReport)
    #: The ``sandbox_runs`` row this session's report is stored on, once known.
    run_id: uuid.UUID | None = None
    #: The company-managed MCP servers, read at the session's first MCP call.
    company_mcp_servers: frozenset[str] | None = None

    def __post_init__(self) -> None:
        self.report.mode = self.mode

    def log_call(self, tool: str, effect: str, outcome: CallOutcome, arguments: Any) -> None:
        if len(self.report.calls) < _LOGGED_CALLS:
            self.report.calls.append(InterceptedCall(tool, effect, outcome, _argument_dump(arguments)))

    async def _consented(self, recordings: list[RecordedCall]) -> list[RecordedCall]:
        if self.consent is None:
            return recordings
        return [r for r in recordings if await self.consent(r)]

    async def recorded(self, tool: str, arguments: Any) -> RecordedCall | None:
        """The recording answering this call, consent read now; ``None`` for none."""
        key, recordings = self.replay.candidates(tool, arguments)
        if not recordings:
            return None
        recording = self.replay.take(key, recordings)
        return recording if await self._consented([recording]) else None

    async def answer_write(
        self, tool: str, arguments: dict[str, Any], description: str = ""
    ) -> tuple[str, CallOutcome]:
        """Count and answer a write that is not executed: its answer and how it
        was answered (``simulated`` by a model in sandbox, else ``intercepted``)."""
        self.report.intercepted_writes += 1
        if self.mode == "dry_run":
            self.log_call(tool, "write", "intercepted", arguments)
            logger.info("Concierge dry-run: write %r logged, not executed", tool[:120])
            return DRY_RUN_NOTICE, "intercepted"
        grounding = await self._consented(self.replay.outputs_of(tool))
        text, tokens = await self.simulate(tool, arguments, grounding, description) if self.simulate else (None, 0)
        self.report.simulation_tokens += tokens
        if text is None:
            self.report.unanswered_writes += 1
            self.log_call(tool, "write", "intercepted", arguments)
            return INTERCEPTED_NOTICE, "intercepted"
        self.report.simulated_writes += 1
        self.log_call(tool, "write", "simulated", arguments)
        return text, "simulated"

    async def _effect(self, call: ToolCall, definition: ToolDefinition | None) -> CallEffect:
        """The call's effect; a call no definition describes is a write."""
        if definition is None:
            return "write"
        servers: frozenset[str] = frozenset()
        if definition.entity_id and definition.source.startswith("mcp:"):
            if self.company_mcp_servers is None:
                self.company_mcp_servers = await side_effects.company_mcp_server_ids()
            servers = self.company_mcp_servers
        vouched = side_effects.vouched_source(
            definition.source, entity_id=definition.entity_id, company_mcp_servers=servers
        )
        return side_effects.tool_call_effect(call.name, base_name=definition.base_name, vouched=vouched)

    async def intercept(
        self,
        call: ToolCall,
        definition: ToolDefinition | None,
        run: Callable[[], Awaitable[ToolResult]],
    ) -> ToolResult:
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        if definition is not None and definition.reads_own_configuration:
            self.report.live_reads += 1
            return await run()
        effect = await self._effect(call, definition)
        if self.mode == "sandbox":
            recording = await self.recorded(call.name, arguments)
            if recording is not None:
                self.report.replayed += 1
                if effect == "write":
                    self.report.intercepted_writes += 1
                self.log_call(call.name, effect, "replayed", arguments)
                _stamp_span("replayed")
                return ToolResult(tool_call_id=call.id, content=recording.output, is_error=recording.is_error)
        if effect == "read":
            self.report.live_reads += 1
            return await run()
        text, outcome = await self.answer_write(call.name, arguments, definition.description if definition else "")
        _stamp_span(outcome)
        return ToolResult(tool_call_id=call.id, content=text, no_op=outcome == "intercepted")


_current: ContextVar[SandboxSession | None] = ContextVar("concierge_sandbox_session", default=None)


def current_sandbox_session() -> SandboxSession | None:
    """The session the running turn is intercepted by, if any."""
    return _current.get()


@contextlib.contextmanager
def sandbox_session(session: SandboxSession) -> Iterator[SandboxSession]:
    """Intercept every call dispatched in this context with *session*.

    A session already bound (a consulted agent inside a sandbox case, a flow's
    agent node) is kept: the nested turn counts on the case it belongs to.
    """
    outer = _current.get()
    if outer is not None:
        yield outer
        return
    token = _current.set(session)
    try:
        with intercepting_dispatch(session):
            yield session
    finally:
        _current.reset(token)


def model_simulator(db: AsyncSession, *, user_id: str, model: str | None) -> Simulator:
    """The :data:`Simulator` that asks *model* for *user_id* (``simulation``)."""
    from app.services.health.sandbox.simulation import simulate_write

    async def _simulate(
        tool: str, arguments: dict[str, Any], recorded: list[RecordedCall], description: str
    ) -> tuple[str | None, int]:
        return await simulate_write(
            db, user_id=user_id, model=model, tool=tool, arguments=arguments, recorded=recorded, description=description
        )

    return _simulate
