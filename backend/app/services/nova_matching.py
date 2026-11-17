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

"""Nova rule matching in a worker process, with a budget per rule.

Matching a message against the ``.nov`` rules is CPU work: every ``keywords:``
regex of every enabled rule runs over the whole message, and the full engine
(``nova-hunting``) parses each rule and runs its matcher as well. On the event
loop that work stopped the process - other chat turns, health probes,
WebSockets - for as long as it took, and a regex that backtracks (``(a|a)*b``
passes the parse-time guard of ``nova_guard``) could take for ever. A thread is
no answer: CPython's ``re`` holds the GIL for the whole of one match. So the
work runs here, in one worker process per web or worker process, and the event
loop only waits on it.

**The budget.** A rule may match one input for :func:`rule_budget_seconds`:
``RULE_BUDGET_SECONDS`` plus ``RULE_BUDGET_SECONDS_PER_MILLION_CHARS`` per
million characters of the input (the slowest rule of the public catalogue takes
0.16 s per million characters). The worker publishes which rule it is on; a
rule still running past its budget is cut by killing the worker, and a rule the
worker died on is treated the same way. Either way the rule is skipped for that
input and a WARNING names it with the input's length, never its content - what
a rule that raises mid-match already got (``nova_guard.evaluate`` skips it) -
and the rules after it run in a fresh worker. A slow rule costs its own verdict
and no other: every rule within its budget gives the verdict it gave inline.

**Its clock.** The budget is the worker's CPU time on the rule
(``core/cpu_time``): a loaded host that deschedules the worker spends none of
it. Where the worker's CPU time
cannot be read (Windows, macOS) it is wall-clock time. That clock stops while
the host does not run the worker (stopped, starved), and the memory-write guard
waits on it with no bound of its own, so a rule that held the worker
``RULE_WALL_CLOCK_BACKSTOP_FACTOR`` times its budget in wall-clock time is cut
all the same.

**The same code.** The worker runs what the event loop ran: ``nova_guard``'s
parser and evaluator for the keyword tier; ``NovaParser`` and ``NovaMatcher``
with evaluators over the pre-computed semantic scores and LLM verdicts for the
full engine. Parsing moves too, and each distinct rule text is parsed once per
worker (``NovaParser`` starts afresh on every ``parse``; a matcher never
changes the rule it is given).

**No worker.** When none can be started (the host refuses to spawn one, or its
start overran ``WORKER_START_TIMEOUT_SECONDS``), matching runs in this process
on the parse pool - the same verdicts, with no budget - and a WARNING says so;
a worker is tried again ``WORKER_RETRY_SECONDS`` later.

**A start cut short.** A start that does not end with a started worker - its
caller cancelled while the process spawns or runs its first task, a start that
failed, any other exception - kills the process and shuts its executor down.
The spawn runs in a thread, inside the executor's first submit: a kill asked
meanwhile is done by that thread once the process is known, so the event loop
never waits on it (``core/worker_process.ProcessWorker``, shared with the
Teams Graph notification worker).

The worker imports this module: it stays light (the stdlib and ``nova_guard``;
``nova`` itself only once the full engine is asked for).
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import logging
import multiprocessing
import os
import threading
import time
import traceback
import weakref
from collections import OrderedDict
from collections.abc import Callable, Sequence
from concurrent.futures.process import BrokenProcessPool
from typing import Any, NamedTuple

from app.core.cpu_time import Clock, CpuStopwatch, process_cpu_clock
from app.core.worker_process import ProcessWorker, exit_with_parent
from app.services import nova_guard
from app.services.nova_guard import NovaMatch

logger = logging.getLogger(__name__)

#: What one rule may take on any input (seconds): far above what a rule that
#: does not backtrack takes on a chat message.
RULE_BUDGET_SECONDS = 2.0
#: What one rule may take on top of it per million characters of the input
#: (seconds): linear matching grows with the input, backtracking does not stop.
RULE_BUDGET_SECONDS_PER_MILLION_CHARS = 5.0
#: Past this many times its budget in wall-clock time, a rule is cut even when
#: its worker's CPU clock did not reach the budget: the host did not run it.
RULE_WALL_CLOCK_BACKSTOP_FACTOR = 10.0
#: How long a fresh worker may take to start (spawn, imports), kept out of every
#: rule's budget: a slow start on a loaded host never cuts a rule.
WORKER_START_TIMEOUT_SECONDS = 30.0
#: After a worker could not be started, how long matching stays in this process
#: before a worker is tried again (seconds).
WORKER_RETRY_SECONDS = 60.0

#: How often the budget of the rule being matched is checked (seconds).
_POLL_SECONDS = 0.05
#: A worker that went away before it started a job's first rule (not the job's
#: doing) is replaced this many times in a row before matching stays in process.
_MAX_WORKER_RETRIES = 3
#: The worker publishes ``job * _JOB_SPAN + rule index`` in one 64-bit store:
#: a job carries fewer rules than that.
_JOB_SPAN = 1 << 20
_MAX_BATCH_RULES = _JOB_SPAN - 1
#: Parsed rule texts a process keeps (the worker's cache, or this process's when
#: matching runs here).
_PARSE_CACHE_MAX = 4096

_KEYWORDS = "keywords"
_ENGINE_PARSE = "engine_parse"
_ENGINE_MATCH = "engine_match"

_OVERRAN = "overran"
_STALLED = "stalled"
_DIED = "died"

# A ``semantics:`` pattern declares its own threshold; this is the fallback when
# a rule omits it (Nova's own default).
_DEFAULT_SEMANTIC_THRESHOLD = 0.5


# --- What is matched, and what comes back -------------------------------------


class KeywordRule(NamedTuple):
    """A stored rule as the keyword tier reads it; *label* names it in the log."""

    label: str
    text: str
    action: str


class EngineRule(NamedTuple):
    """A stored rule as the full engine reads it: its text and its row's identity."""

    label: str
    text: str
    name: str
    category: str
    action: str


class Unparseable(NamedTuple):
    """The keyword tier could not parse the rule: ``nova_guard``'s reason."""

    error: str


class RuleFailed(NamedTuple):
    """The full engine's matcher raised on the rule, with its traceback."""

    error: str


class PatternRef(NamedTuple):
    """One ``semantics:`` / ``llm:`` pattern as the advanced tiers read it: its
    text and its threshold (for an ``llm:`` pattern, the LLM temperature)."""

    pattern: str
    threshold: float | None = None


class RulePatterns(NamedTuple):
    """The patterns of a parsed Nova rule, in the shape the parsed rule has them:
    ``semantics`` and ``llms`` map a variable to an object whose ``pattern`` is
    the text (what ``nova_engine``'s pre-computation reads)."""

    semantics: dict[str, PatternRef]
    llms: dict[str, PatternRef]


def rule_budget_seconds(prompt: str) -> float:
    """What one rule may take to match *prompt* (seconds of its worker's CPU time)."""
    return RULE_BUDGET_SECONDS + RULE_BUDGET_SECONDS_PER_MILLION_CHARS * len(prompt) / 1_000_000


async def match_keyword_rules(prompt: str, rules: Sequence[KeywordRule]) -> list[NovaMatch | Unparseable | None]:
    """The keyword tier's verdict on each of *rules*, in order: its match, the
    reason it could not be parsed, or ``None`` (no match, or cut)."""
    return await _match(_KEYWORDS, prompt, rules)


async def parse_engine_rules(rules: Sequence[EngineRule]) -> list[RulePatterns | None]:
    """The patterns of each of *rules* as ``NovaParser`` reads them, in order,
    ``None`` for a rule it cannot parse. Raises when ``nova`` cannot be imported."""
    return await _match(_ENGINE_PARSE, "", rules)


async def match_engine_rules(
    prompt: str,
    rules: Sequence[EngineRule],
    semantic_scores: dict[str, float],
    llm_verdicts: dict[tuple[str, float | None], bool],
) -> list[NovaMatch | RuleFailed | None]:
    """The full engine's verdict on each of *rules*, in order, its evaluators
    reading *semantic_scores* and *llm_verdicts*: the match, the matcher's
    failure, or ``None`` (no match, unparseable, or cut)."""
    return await _match(_ENGINE_MATCH, prompt, rules, (semantic_scores, llm_verdicts))


# --- The matching itself (in the worker, or in this process) ------------------

#: The worker's progress cell (set by :func:`_init_worker`); ``None`` elsewhere.
_progress: Any = None
_parse_cache: OrderedDict[tuple[str, str], Any] = OrderedDict()
_parse_cache_lock = threading.Lock()


def _run_job(job: int, kind: str, prompt: str, rules: tuple, context: Any) -> list[Any]:
    """Match *rules* against *prompt* one by one, saying which one is running."""
    state = _prepare(kind, context)
    match_one = _MATCHERS[kind]
    outcomes: list[Any] = []
    for index, rule in enumerate(rules):
        if _progress is not None:
            _progress.value = job * _JOB_SPAN + index
        outcomes.append(match_one(prompt, rule, state))
    return outcomes


def _prepare(kind: str, context: Any) -> Any:
    """What every rule of a job shares; imports ``nova`` for the full engine, so
    a missing engine fails the job before any rule (the caller falls back)."""
    if kind == _KEYWORDS:
        return None
    from nova.core.matcher import NovaMatcher
    from nova.core.parser import NovaParser

    if kind == _ENGINE_PARSE:
        return NovaParser
    semantic_scores, llm_verdicts = context
    return (
        NovaParser,
        NovaMatcher,
        _make_semantic_evaluator(semantic_scores),
        _make_llm_evaluator(llm_verdicts),
    )


def _parsed(kind: str, text: str, parse: Callable[[str], Any]) -> Any:
    key = (kind, text)
    with _parse_cache_lock:
        if key in _parse_cache:
            _parse_cache.move_to_end(key)
            return _parse_cache[key]
    value = parse(text)
    with _parse_cache_lock:
        # Two threads that parsed the same text at once keep the first value.
        if key in _parse_cache:
            _parse_cache.move_to_end(key)
            return _parse_cache[key]
        _parse_cache[key] = value
        while len(_parse_cache) > _PARSE_CACHE_MAX:
            _parse_cache.popitem(last=False)
    return value


def _parse_keyword_rule(text: str) -> nova_guard.ParsedRule | Unparseable:
    try:
        return nova_guard.parse_rule(text)
    except nova_guard.NovaRuleError as exc:
        return Unparseable(str(exc))


def _match_keyword_rule(prompt: str, rule: KeywordRule, _state: None) -> NovaMatch | Unparseable | None:
    parsed = _parsed(_KEYWORDS, rule.text, _parse_keyword_rule)
    if isinstance(parsed, Unparseable):
        return parsed
    matches = nova_guard.evaluate(prompt, [(parsed, rule.action)])
    return matches[0] if matches else None


def _parse_engine_rule(parser_class: Any, text: str) -> Any:
    try:
        return parser_class().parse(text)
    except Exception:
        return None


def _engine_rule(parser_class: Any, text: str) -> Any:
    return _parsed(_ENGINE_PARSE, text, lambda value: _parse_engine_rule(parser_class, value))


def _pattern_refs(rule: Any, section: str) -> dict[str, PatternRef]:
    return {
        name: PatternRef(pattern_text(p), getattr(p, "threshold", None))
        for name, p in (getattr(rule, section, None) or {}).items()
    }


def _engine_patterns(_prompt: str, rule: EngineRule, parser_class: Any) -> RulePatterns | None:
    parsed = _engine_rule(parser_class, rule.text)
    if parsed is None:
        return None
    return RulePatterns(semantics=_pattern_refs(parsed, "semantics"), llms=_pattern_refs(parsed, "llms"))


def _match_engine_rule(prompt: str, rule: EngineRule, state: tuple) -> NovaMatch | RuleFailed | None:
    parser_class, matcher_class, semantic_evaluator, llm_evaluator = state
    parsed = _engine_rule(parser_class, rule.text)
    if parsed is None:
        return None
    try:
        matcher = matcher_class(
            parsed,
            semantic_evaluator=semantic_evaluator,
            llm_evaluator=llm_evaluator,
            # Never let Nova build its own OpenAI evaluator from ambient env:
            # the llm tier must go through the platform router.
            create_llm_evaluator=False,
        )
        result = matcher.check_prompt(prompt)
    except Exception:
        return RuleFailed(traceback.format_exc())
    if not _is_match(result):
        return None
    return NovaMatch(
        rule_name=rule.name,
        category=rule.category,
        action=rule.action,
        matched_keywords=_matched_keywords(result),
    )


_MATCHERS: dict[str, Callable[[str, Any, Any], Any]] = {
    _KEYWORDS: _match_keyword_rule,
    _ENGINE_PARSE: _engine_patterns,
    _ENGINE_MATCH: _match_engine_rule,
}


def pattern_text(pattern: Any) -> str:
    """The text of a ``semantics:`` / ``llm:`` pattern (its cache key)."""
    return str(getattr(pattern, "pattern", "") or "")


def llm_verdict_key(template: str, temperature: Any) -> tuple[str, float | None]:
    """Where an ``llm:`` verdict is kept: its condition and the temperature it
    was judged at. Nova passes a pattern's threshold as that temperature."""
    try:
        return (template, None if temperature is None else float(temperature))
    except (TypeError, ValueError):
        return (template, None)


def _pattern_threshold(pattern: Any) -> float:
    value = getattr(pattern, "threshold", None)
    try:
        return float(value) if value is not None else _DEFAULT_SEMANTIC_THRESHOLD
    except (TypeError, ValueError):
        return _DEFAULT_SEMANTIC_THRESHOLD


def _matched_keywords(result: Any) -> tuple[str, ...]:
    """Keyword names Nova reports as hit, for the audit trail.

    Nova's verdict shape varies by version, so read defensively - the audit
    detail is nice to have, never worth failing a guard over.
    """
    if not isinstance(result, dict):
        return ()
    details = result.get("matching_keywords") or result.get("matched_keywords") or {}
    if isinstance(details, dict):
        return tuple(str(k) for k, v in details.items() if v)
    if isinstance(details, (list, tuple)):
        return tuple(str(k) for k in details)
    return ()


def _is_match(result: Any) -> bool:
    """Nova's ``check_prompt`` returns a dict verdict; be tolerant of shape."""
    if isinstance(result, dict):
        return bool(result.get("matched") or result.get("match"))
    return bool(result)


def _make_semantic_evaluator(scores: dict[str, float]):
    """A Nova ``SemanticEvaluator`` reading pre-computed similarities."""
    from nova.evaluators.base import SemanticEvaluator

    class _XtmOneSemanticEvaluator(SemanticEvaluator):
        def evaluate(self, pattern, text=None):
            key = pattern_text(pattern)
            score = scores.get(key)
            if score is None:
                return (False, 0.0)
            return (score >= _pattern_threshold(pattern), score)

    return _XtmOneSemanticEvaluator()


def _make_llm_evaluator(verdicts: dict[tuple[str, float | None], bool]):
    """A Nova ``LLMEvaluator`` reading pre-computed judgements (:func:`llm_verdict_key`).

    ``evaluate_prompt`` takes the ``temperature`` nova-hunting 0.3.1 passes
    (the pattern's threshold): refusing it made every ``llm:`` rule
    fail closed.
    """
    from nova.evaluators.base import LLMEvaluator

    class _XtmOneLLMEvaluator(LLMEvaluator):
        def evaluate(self, pattern, text=None):
            matched = verdicts.get(llm_verdict_key(pattern_text(pattern), getattr(pattern, "threshold", None)), False)
            return (matched, 1.0 if matched else 0.0)

        def evaluate_prompt(self, prompt_template, text=None, temperature=None):
            matched = verdicts.get(llm_verdict_key(str(prompt_template), temperature), False)
            return (matched, 1.0 if matched else 0.0, {})

    return _XtmOneLLMEvaluator()


# --- The worker process -------------------------------------------------------


def _init_worker(progress: Any, process_id: Any) -> None:
    """Worker initializer: the progress cell it writes, its pid, and the watch on its parent."""
    global _progress
    _progress = progress
    process_id.value = os.getpid()
    exit_with_parent("nova-matching-parent-watch")


def _start_worker() -> None:
    """A fresh worker's first task: the engine's imports, when it is installed,
    so that no rule's budget pays for them. Only the full engine needs them."""
    if importlib.util.find_spec("nova") is None:
        return
    try:
        for name in ("nova.core.matcher", "nova.core.parser", "nova.evaluators.base"):
            importlib.import_module(name)
    except Exception:  # the job importing it fails the same way, and says so
        return


class _Worker(ProcessWorker):
    """One worker process and the cell it says which rule it is on in.

    Its start and its kill are ``ProcessWorker``'s: :func:`_start_worker` is
    submitted from a thread, and a kill asked while it spawns the process is
    done by that thread once the process is known.
    """

    def __init__(self) -> None:
        context = multiprocessing.get_context("spawn")
        self.progress = context.RawValue("q", -1)
        self.process_id = context.RawValue("q", 0)
        super().__init__(_init_worker, _start_worker, "Nova matching worker", initargs=(self.progress, self.process_id))
        #: The job that killed this worker on purpose (a cut, a cancelled caller).
        self.killed_by: int | None = None
        #: The worker's CPU time, read once it started (``None``: this platform cannot).
        self.cpu_clock: Clock | None = None

    def running_rule(self, job: int) -> int | None:
        """The index of the rule *job* is on, ``None`` while it has not started."""
        running_job, index = divmod(self.progress.value, _JOB_SPAN)
        return index if running_job == job else None


class _Run(NamedTuple):
    """How a job went: its outcomes, or the rule it was cut on, or neither (the
    worker went away before the job started: try again)."""

    outcomes: list[Any] | None = None
    cut: int | None = None
    reason: str = ""


_RETRY = _Run()

_worker: _Worker | None = None
_worker_lock = threading.Lock()
_retry_at = 0.0
_job_ids = itertools.count(1)
#: One job at a time per event loop, so a rule's budget measures the rule and
#: never a queue in front of it.
_gates: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _gate() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = _gates[loop] = asyncio.Lock()
    return gate


def _discard(worker: _Worker, *, killed_by: int | None = None) -> None:
    """Kill the worker and let the next job start a new one."""
    global _worker
    with _worker_lock:
        if _worker is worker:
            _worker = None
    if killed_by is not None and worker.killed_by is None:
        worker.killed_by = killed_by
    worker.kill()


def _stay_in_process(reason: str) -> None:
    global _retry_at
    _retry_at = time.monotonic() + WORKER_RETRY_SECONDS
    logger.warning(
        "Nova matching worker unavailable (%s): rules are matched in this process, "
        "with no budget per rule, for the next %.0fs",
        reason,
        WORKER_RETRY_SECONDS,
    )


async def _ready_worker() -> _Worker | None:
    """The started worker, a new one, or ``None`` when none can be had now."""
    global _worker
    if time.monotonic() < _retry_at:
        return None
    try:
        with _worker_lock:
            if _worker is None:
                _worker = _Worker()
            worker = _worker
    except Exception as exc:  # no process can be spawned here
        _stay_in_process(repr(exc)[:200])
        return None
    if worker.started:
        return worker
    try:
        # The first submit spawns the process: from a thread, not the loop's.
        started = await asyncio.to_thread(worker.submit_start)
        await asyncio.wait_for(asyncio.wrap_future(started), WORKER_START_TIMEOUT_SECONDS)
    except Exception as exc:  # a start that overran its bound, or a worker that died starting
        _discard(worker)
        _stay_in_process(f"start failed: {repr(exc)[:200]}")
        return None
    except BaseException:
        # A caller cancelled mid-start (or a start ended any other way) takes
        # the half-started worker with it.
        _discard(worker)
        raise
    worker.cpu_clock = process_cpu_clock(worker.process_id.value)
    worker.started = True
    return worker


async def _run_in_worker(worker: _Worker, kind: str, prompt: str, rules: tuple, context: Any, budget: float) -> _Run:
    job = next(_job_ids)
    loop = asyncio.get_running_loop()
    try:
        future = loop.run_in_executor(worker.executor, _run_job, job, kind, prompt, rules, context)
    except Exception:  # a pool that broke while idle, or was shut down meanwhile
        _discard(worker)
        return _RETRY
    watched, stopwatch = -1, None
    try:
        while True:
            await asyncio.wait({future}, timeout=_POLL_SECONDS)
            if future.done():
                # Cancelled by a shutdown before it started, not by this caller.
                return _RETRY if future.cancelled() else _Run(outcomes=future.result())
            running = worker.running_rule(job)
            if running is None:
                continue
            if running != watched or stopwatch is None:
                watched, stopwatch = running, CpuStopwatch(worker.cpu_clock)
                continue
            reason = None
            if stopwatch.charged() >= budget:
                reason = _OVERRAN
            elif stopwatch.elapsed() >= budget * RULE_WALL_CLOCK_BACKSTOP_FACTOR:
                reason = _STALLED
            if reason is not None:
                future.cancel()
                _discard(worker, killed_by=job)
                return _Run(cut=running, reason=reason)
    except asyncio.CancelledError:
        # Nothing else would ever stop what the worker is busy with, and the
        # next job would queue behind it: kill it with its caller.
        if not future.done():
            future.cancel()
            _discard(worker, killed_by=job)
        raise
    except BrokenProcessPool:
        running = worker.running_rule(job)
        _discard(worker)
        if running is None or worker.killed_by not in (None, job):
            return _RETRY
        return _Run(cut=running, reason=_DIED)


def _log_cut(rule: KeywordRule | EngineRule, kind: str, reason: str, budget: float, prompt: str) -> None:
    """Say which rule was skipped and why: its label and the input's length only."""
    what = "while it was parsed" if kind == _ENGINE_PARSE else f"on a {len(prompt)}-character input"
    if reason == _OVERRAN:
        logger.warning("Nova rule %s overran its %.1fs budget %s; skipped for this input", rule.label, budget, what)
    elif reason == _STALLED:
        logger.warning(
            "Nova rule %s held the matching worker %.0fs of wall-clock time %s; skipped for this input",
            rule.label,
            budget * RULE_WALL_CLOCK_BACKSTOP_FACTOR,
            what,
        )
    else:
        logger.warning("Nova rule %s stopped the matching worker %s; skipped for this input", rule.label, what)


async def _match(kind: str, prompt: str, rules: Sequence[Any], context: Any = None) -> list[Any]:
    outcomes: list[Any] = [None] * len(rules)
    pending = list(range(len(rules)))
    budget = rule_budget_seconds(prompt)
    retries = 0
    async with _gate():
        while pending:
            worker = None if retries > _MAX_WORKER_RETRIES else await _ready_worker()
            if worker is None:
                if retries > _MAX_WORKER_RETRIES:
                    _stay_in_process("its worker kept going away")
                from app.core.parse_offload import run_parse

                rest = tuple(rules[i] for i in pending)
                for index, outcome in zip(
                    pending, await run_parse(_run_job, 0, kind, prompt, rest, context), strict=True
                ):
                    outcomes[index] = outcome
                break
            batch = pending[:_MAX_BATCH_RULES]
            run = await _run_in_worker(worker, kind, prompt, tuple(rules[i] for i in batch), context, budget)
            if run.outcomes is not None:
                for index, outcome in zip(batch, run.outcomes, strict=True):
                    outcomes[index] = outcome
                pending = pending[len(batch) :]
                retries = 0
            elif run.cut is not None:
                # The rules before it were matched, their verdicts lost with the
                # worker: they run again with the ones after it.
                cut = batch[run.cut]
                _log_cut(rules[cut], kind, run.reason, budget, prompt)
                pending.remove(cut)
                retries = 0
            else:
                retries += 1
    return outcomes


def shutdown_nova_matching_worker() -> None:
    """Best-effort teardown (lifespan / worker shutdown): never waits on a match."""
    global _worker, _retry_at
    with _worker_lock:
        worker = _worker
        _worker = None
    if worker is not None:
        _discard(worker)
    _retry_at = 0.0
    with _parse_cache_lock:
        _parse_cache.clear()


__all__ = [
    "RULE_BUDGET_SECONDS",
    "RULE_BUDGET_SECONDS_PER_MILLION_CHARS",
    "RULE_WALL_CLOCK_BACKSTOP_FACTOR",
    "WORKER_RETRY_SECONDS",
    "WORKER_START_TIMEOUT_SECONDS",
    "EngineRule",
    "KeywordRule",
    "PatternRef",
    "RuleFailed",
    "RulePatterns",
    "Unparseable",
    "match_engine_rules",
    "match_keyword_rules",
    "parse_engine_rules",
    "pattern_text",
    "rule_budget_seconds",
    "shutdown_nova_matching_worker",
]
