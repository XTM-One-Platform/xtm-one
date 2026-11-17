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

"""Prompt-guard screening of tool results, opt-in per tool.

A tool result is the widest untrusted input an agent reads: a fetched page, a
ticket, a channel thread, another server's answer. The connected prompt guard
(Nova, through :func:`app.services.prompt_guards.evaluate_text`) can screen
the results of the tools an administrator names in Settings > Access and security
(``prompt_guard_scanned_tools``). Screening is DETECTION: nothing here fences,
rewrites or annotates a result.

* The scope and the mode are read once per run, from the security options
  snapshot (:meth:`ToolResultScreen.for_run`): an empty scope is ``None``, and
  the loop then does exactly what it did before the option existed - no
  connection check, no session, no task. With no guard connected (known
  through :func:`~app.services.prompt_guards.known_unconnected`) a result
  costs a time comparison. The scope names a tool by its registered name, a
  custom tool by ``custom:<its id>``, matched through the ``entity_id`` of
  the called tool's definition: a rename needs no settings write.
* What is evaluated is what the model reads: the result's text, as the loop
  truncates it, followed by the text of its non-image attachments, extracted
  by the reader the providers use (``file_resolver._extract_fallback``,
  memoised per content hash). Pixels are not text.
* By default a match is only recorded (a ``security_alert`` audit entry and a
  WARNING line), and the evaluation runs in a background task the loop does
  not wait for, so the next model call is not delayed. With
  ``prompt_guard_block_tool_results`` on, the verdict is awaited before the
  result is appended, and a ``block`` rule withholds it: the model reads
  :data:`WITHHELD_RESULT_TEMPLATE` instead, without the result's files.
* Every evaluation is bounded (:data:`TOOL_RESULT_EVALUATION_TIMEOUT_SECS`,
  the slot wait and the attachment reading included), holds one of
  :data:`MAX_CONCURRENT_EVALUATIONS` process-wide slots and fails open: a
  guard that fails or runs out of time costs its verdict and nothing else.
  The screened results of one parallel batch are evaluated together, in one
  gather under those slots, before any of them is appended.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
import weakref
from collections.abc import Callable, Coroutine, Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.core.utils import format_exception_message, safe_uuid

if TYPE_CHECKING:
    from app.services.prompt_guards import GuardVerdict
    from app.tools.base import ToolDefinition

logger = logging.getLogger(__name__)

#: The security options this module reads (``app/core/security_options.py``).
SCOPE_OPTION = "prompt_guard_scanned_tools"
BLOCK_OPTION = "prompt_guard_block_tool_results"

#: The bound on one evaluation: waiting for a slot, reading the attachments
#: and the guard's own work. Shorter than the guard's default: in blocking
#: mode the agent waits for it.
TOOL_RESULT_EVALUATION_TIMEOUT_SECS = 20.0

#: Evaluations in flight at once in a process, blocking and background alike.
MAX_CONCURRENT_EVALUATIONS = 4

#: Background tasks (evaluations and audit writes) a process keeps at most;
#: past it a match is logged but not screened or audited.
MAX_PENDING_BACKGROUND = 64

#: What the model reads in place of a result a ``block`` rule matched.
WITHHELD_RESULT_TEMPLATE = (
    "[Withheld] The result of '{tool}' was withheld by the platform: a prompt-safety rule "
    "matched its content. Tell the user this tool's output was refused instead of calling it again."
)

# A backlog warning at most this often: under load it would fire per result.
_BACKLOG_WARNING_INTERVAL_SECS = 60.0


@dataclass(frozen=True, slots=True)
class _Evaluation:
    """One tool result to evaluate, with what its audit entry names."""

    tool: str
    text: str
    files: tuple[Any, ...]
    entity_id: str | None
    user_id: str | None


class ToolResultScreen:
    """One run's screening of tool results: the scope and the mode, read once.

    Build it with :meth:`for_run`; ``None`` means nothing is screened.
    """

    def __init__(
        self,
        scope: frozenset[str],
        *,
        blocking: bool,
        tools: Sequence[ToolDefinition] | None = None,
        wall_clock_deadline: float | None = None,
    ) -> None:
        self._scope = scope
        self._blocking = blocking
        self._tools = tuple(tools or ())
        self._deadline = wall_clock_deadline
        self._definitions: dict[str, ToolDefinition] | None = None

    @classmethod
    def for_run(
        cls,
        tools: Sequence[ToolDefinition] | None,
        *,
        wall_clock_deadline: float | None = None,
    ) -> ToolResultScreen | None:
        """The screen of a run, or ``None`` when the scope is empty.

        Reads the security options snapshot, never the database, and only
        here: a scope change applies from the next run. Never raises: a
        screen that cannot be built screens nothing, and says so.
        """
        from app.core.security_options import security_option

        try:
            scope = security_option(SCOPE_OPTION)
            if not scope:
                return None
            return cls(
                frozenset(scope),
                blocking=bool(security_option(BLOCK_OPTION)),
                tools=tools,
                wall_clock_deadline=wall_clock_deadline,
            )
        except Exception:
            logger.error("Could not read the tool-result screening scope: no tool result is screened", exc_info=True)
            return None

    @property
    def blocking(self) -> bool:
        return self._blocking

    def _definition(self, name: str) -> ToolDefinition | None:
        if self._definitions is None:
            self._definitions = {tool.name: tool for tool in self._tools}
        return self._definitions.get(name)

    def covers(self, name: str) -> bool:
        """Whether the results of the tool registered as *name* are screened.

        By its name, or by the name it was renamed from when several
        instances of one integration are connected (``base_name``):
        the scope names the tools the catalogue declares. A custom tool also
        by its id (``custom:<entity_id>``): its owner may rename it,
        and a renamed tool stays screened with no settings write.
        """
        if name in self._scope:
            return True
        definition = self._definition(name)
        if definition is None:
            return False
        if definition.base_name is not None and definition.base_name in self._scope:
            return True
        return _custom_tool_entry(definition) in self._scope

    async def screen(self, name: str, result: Any, *, truncate: Callable[[str], str]) -> Any:
        """*result*, or the result that withholds it (blocking mode only)."""
        withheld = await self.screen_batch([(None, name, result)], truncate=truncate)
        return withheld.get(None, result)

    async def screen_batch(
        self,
        items: Iterable[tuple[Hashable, str, Any]],
        *,
        truncate: Callable[[str], str],
    ) -> dict[Hashable, Any]:
        """The results to withhold, by key, among ``(key, registered name, result)``.

        *truncate* turns a result's text into what the model reads. Empty
        unless blocking: a background evaluation never changes a result.
        Never raises (a cancellation of the caller aside): fail-open.
        """
        try:
            return await self._screen_batch(items, truncate)
        except Exception:
            logger.error("Tool-result screening failed: the results are left as they are", exc_info=True)
            return {}

    async def _screen_batch(
        self, items: Iterable[tuple[Hashable, str, Any]], truncate: Callable[[str], str]
    ) -> dict[Hashable, Any]:
        from app.services.prompt_guards import known_unconnected

        if known_unconnected():
            return {}
        jobs = [
            (key, result, job)
            for key, name, result in items
            if self.covers(name) and (job := self._evaluation(name, result, truncate)) is not None
        ]
        if not jobs:
            return {}
        if not self._blocking:
            for _key, _result, job in jobs:
                _spawn(_screen_in_background, job)
            return {}
        # One gather for the whole batch: its evaluations run side by
        # side under the process-wide slots, so a batch waits about as long as
        # its slowest one, and the caller appends the results in call order.
        bound = self._bound()
        verdicts = await asyncio.gather(*(_verdict(job, bound) for _key, _result, job in jobs))
        withheld: dict[Hashable, Any] = {}
        for (key, result, job), verdict in zip(jobs, verdicts, strict=True):
            if not verdict.flagged:
                continue
            _report(job, verdict, blocked=verdict.blocked)
            if verdict.blocked:
                withheld[key] = _withheld(result, job.tool)
        return withheld

    def _evaluation(self, name: str, result: Any, truncate: Callable[[str], str]) -> _Evaluation | None:
        text = truncate(str(getattr(result, "content", "") or ""))
        files = tuple(ref for ref in getattr(result, "files", None) or () if not _is_image(ref))
        if not text.strip() and not files:
            return None
        definition = self._definition(name)
        entity_id = definition.entity_id if definition is not None else None
        return _Evaluation(
            tool=name,
            text=text,
            files=files,
            entity_id=str(entity_id) if entity_id and safe_uuid(str(entity_id)) is not None else None,
            user_id=_acting_user_id(),
        )

    def _bound(self) -> float:
        bound = TOOL_RESULT_EVALUATION_TIMEOUT_SECS
        if self._deadline is not None:
            bound = min(bound, self._deadline - time.monotonic())
        return bound


def _custom_tool_entry(definition: ToolDefinition) -> str | None:
    """``custom:<id>`` for a custom tool's definition (``source`` ``custom:...``), else ``None``."""
    from app.core.config import CUSTOM_TOOL_SCAN_PREFIX

    if not definition.entity_id or not str(definition.source or "").startswith(CUSTOM_TOOL_SCAN_PREFIX):
        return None
    entity_id = safe_uuid(str(definition.entity_id))
    return f"{CUSTOM_TOOL_SCAN_PREFIX}{entity_id}" if entity_id is not None else None


def _is_image(ref: Any) -> bool:
    # Every provider takes ``image/*`` natively (vision): no text is read from it.
    return str(getattr(ref, "content_type", "") or "").startswith("image/")


def _acting_user_id() -> str | None:
    """The user the run acts for: the one the model resolution bound (``llm.identity``)."""
    from app.llm import identity

    caller = identity._llm_caller.get()
    return str(caller.user_id) if caller is not None else None


def _withheld(result: Any, tool: str) -> Any:
    """*result* with its text replaced by the refusal and its files dropped."""
    from app.tools.base import ToolResult

    content = WITHHELD_RESULT_TEMPLATE.format(tool=tool)
    if isinstance(result, ToolResult):
        return dataclasses.replace(result, content=content, files=None)
    return ToolResult(
        tool_call_id=str(getattr(result, "tool_call_id", "") or ""),
        content=content,
        is_error=bool(getattr(result, "is_error", False)),
    )


# ---------------------------------------------------------------------------
# The evaluation
# ---------------------------------------------------------------------------

_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _evaluation_slot() -> asyncio.Semaphore:
    # One per event loop: an asyncio primitive binds to the loop that first
    # waits on it, and the worker and the test suite run several loops.
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(MAX_CONCURRENT_EVALUATIONS)
    return slot


async def _screened_text(job: _Evaluation) -> str:
    """The result's text followed by the text of its attachments, as the model reads them."""
    parts = [job.text] if job.text.strip() else []
    if job.files:
        from app.llm.file_resolver import _extract_fallback

        for ref in job.files:
            parts.append((await _extract_fallback(ref)).text)
    return "\n\n".join(parts)


async def _verdict(job: _Evaluation, bound: float) -> GuardVerdict:
    """The guard's verdict on *job* within *bound* seconds. Never raises: fail-open."""
    from app.services.prompt_guards import NOT_FLAGGED, SURFACE_TOOL_RESULT, evaluate_text, known_unconnected

    if bound <= 0:
        logger.warning("No time left to screen the result of tool '%s' with the prompt guard, skipped", job.tool)
        return NOT_FLAGGED
    try:
        async with asyncio.timeout(bound):
            async with _evaluation_slot():
                # Learned by another evaluation while this one waited for its
                # slot: its attachments are not read.
                if known_unconnected():
                    return NOT_FLAGGED
                text = await _screened_text(job)
                return await evaluate_text(text, surface=SURFACE_TOOL_RESULT, user_id=job.user_id, timeout=bound)
    except Exception as exc:
        logger.warning(
            "The prompt guard could not screen the result of tool '%s' within %.0f s, skipped: %s",
            job.tool,
            bound,
            format_exception_message(exc),
        )
        return NOT_FLAGGED


def _report(job: _Evaluation, verdict: GuardVerdict, *, blocked: bool) -> None:
    """Log a match and audit it in the background."""
    logger.warning(
        "Prompt guard '%s' matched the result of tool '%s' (rules=%s, rule_action=%s, withheld=%s)",
        verdict.provider,
        job.tool,
        verdict.rules,
        verdict.rule_action,
        blocked,
    )
    _spawn(_audit, job, verdict, blocked)


async def _audit(job: _Evaluation, verdict: GuardVerdict, blocked: bool) -> None:
    """The ``security_alert`` entry of a match, on a session of its own. Never raises."""
    from app.services.prompt_guards import SURFACE_TOOL_RESULT

    if job.user_id is None:
        # No user to attribute it to (a run with none bound): the WARNING
        # line of ``_report`` is the record.
        return
    try:
        from app.core.database import async_session
        from app.services.audit import log_security_event

        async with asyncio.timeout(TOOL_RESULT_EVALUATION_TIMEOUT_SECS), async_session() as audit_db:
            await log_security_event(
                audit_db,
                user_id=job.user_id,
                action="security_alert",
                entity_type="security",
                entity_id=job.entity_id,
                entity_name=job.tool,
                summary=f"{verdict.provider.capitalize()} rule match",
                details={
                    "source": verdict.source,
                    "surface": SURFACE_TOOL_RESULT,
                    "tool": job.tool,
                    "rules": verdict.rules,
                    "categories": verdict.categories,
                    "rule_action": verdict.rule_action,
                    "action": "withheld" if blocked else "allowed",
                    "blocked": blocked,
                },
            )
            await audit_db.commit()
    except Exception as exc:
        logger.warning(
            "Could not audit a prompt guard match on the result of tool '%s': %s",
            job.tool,
            format_exception_message(exc),
        )


async def _screen_in_background(job: _Evaluation) -> None:
    verdict = await _verdict(job, TOOL_RESULT_EVALUATION_TIMEOUT_SECS)
    if verdict.flagged:
        _report(job, verdict, blocked=False)


# ---------------------------------------------------------------------------
# Background tasks
# ---------------------------------------------------------------------------

# Strong references until each task ends: the event loop keeps only weak ones.
_background: set[asyncio.Task[None]] = set()
_backlog_warned_at = float("-inf")


def _spawn(function: Callable[..., Coroutine[Any, Any, None]], *args: Any) -> bool:
    """Run ``function(*args)`` in a task of its own; ``False`` when the backlog is full."""
    global _backlog_warned_at
    if len(_background) >= MAX_PENDING_BACKGROUND:
        now = time.monotonic()
        if now - _backlog_warned_at >= _BACKLOG_WARNING_INTERVAL_SECS:
            _backlog_warned_at = now
            logger.warning(
                "%d prompt guard screenings of tool results are pending: new ones are skipped until they finish",
                len(_background),
            )
        return False
    task = asyncio.get_running_loop().create_task(function(*args), name="tool-result-guard")
    _background.add(task)
    task.add_done_callback(_background.discard)
    return True


async def drain_background() -> None:
    """Wait for this event loop's pending screenings and audit writes (tests, shutdown)."""
    loop = asyncio.get_running_loop()
    while pending := [task for task in _background if task.get_loop() is loop and not task.done()]:
        await asyncio.gather(*pending, return_exceptions=True)


def reset_background() -> None:
    """Forget every pending task reference, for tests (a closed loop's tasks never end)."""
    _background.clear()
