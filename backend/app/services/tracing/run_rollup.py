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

"""The per-run rollup stored on a trace when it closes, and its percentiles.

``end_trace`` already holds every span of the run in memory (the local span
records it flushes to the trace store). This module turns them into one small
object written to the trace's ``metadata.rollup``: how many iterations, model
calls and tool calls, how many tool calls failed, the tokens (cache reads and
writes apart), where the wall time went (model, tools, ``wait``), how much the
tool results weighed and which were the biggest. Sizes and timings only: no
payload content, no argument, no tool result text.

Before it, the rollup was computed on read (``builtin_tools.traces._run_rollup``)
and only in the span-inclusive mode, so a listing of fifty runs could not say
which one was slow, and nothing gave a median or a p90. With the rollup on the
trace, every list read (``inspect_*_traces`` without spans,
``list_platform_traces``) carries it, and :func:`rollup_percentiles` gives p50
and p90 of each figure over the runs listed.

The rollup measures its own cost (``overhead_ms``) so the instrumentation's
weight is itself a figure of every trace (the performance program's gate:
under 1% of the run's wall time).
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from itertools import islice
from typing import Any

from app.services.tracing.cost_calculator import cache_write_tokens, cached_input_tokens

#: Bumped when a figure changes meaning, so a reader can tell old rollups apart.
ROLLUP_VERSION = 2

#: How many of a run's biggest tool results the rollup names.
LARGEST_RESULTS = 5

#: How many tools the per-tool table keeps (the heaviest by result size): a run
#: calling hundreds of distinct tools must not grow the trace metadata without bound.
MAX_TOOLS = 25

#: The tools whose span time is idle waiting, not work: ``wait`` pauses, and
#: ``check_task_status`` may block until a delegated task ends.
WAIT_TOOL_NAMES = frozenset({"wait", "check_task_status"})

#: How many nodes of one tool result the size walk visits before it stops and
#: marks the size as a lower bound: the walk costs nodes, never bytes.
MAX_SIZE_NODES = 2000

#: How many integrations the per-provider HTTP table keeps (the busiest by calls).
MAX_PROVIDERS = 10

#: The counters of one provider's HTTP calls, summed from the tool spans' ``metadata.http``.
HTTP_COUNTERS: tuple[str, ...] = ("calls", "retries", "5xx", "429", "network_errors")

#: The figures :func:`rollup_percentiles` summarises, in the order it returns them.
PERCENTILE_FIGURES: tuple[str, ...] = (
    "wall_ms",
    "first_token_ms",
    # A chat turn's pre-generation timeline (``tracing/turn_stages``): set on chat turns only.
    "pre_generation_ms",
    "turn_first_token_ms",
    "model_ms",
    "tool_ms",
    "wait_ms",
    "iterations",
    "tool_calls",
    "tool_errors",
    "http_5xx",
    "http_retries",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "tool_result_chars",
    "total_cost",
    "overhead_ms",
)


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _ms(value: Any) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _result_chars(output: Any) -> tuple[int, bool]:
    """Approximate size of a tool result in characters, and whether the walk was cut.

    Sums the lengths of the string leaves and keys (a ``len`` is O(1)) and a
    fixed width for numbers, without serializing: a result of megabytes costs
    the same as one of bytes, and at most :data:`MAX_SIZE_NODES` nodes are
    visited (beyond them the size is a lower bound, ``cut`` is true).
    """
    if output is None:
        return 0, False
    if isinstance(output, str):
        return len(output), False
    total = 0
    visited = 0
    cut = False
    stack: list[Any] = [output]
    while stack:
        if visited >= MAX_SIZE_NODES:
            return total, True
        node = stack.pop()
        visited += 1
        if isinstance(node, str):
            total += len(node)
        elif isinstance(node, Mapping):
            # Bounded per container too: a mapping of a million keys is cut, not walked.
            if len(node) > MAX_SIZE_NODES:
                cut = True
            for key, value in islice(node.items(), MAX_SIZE_NODES):
                total += len(key) if isinstance(key, str) else 8
                stack.append(value)
        elif isinstance(node, list | tuple):
            if len(node) > MAX_SIZE_NODES:
                cut = True
            stack.extend(node[:MAX_SIZE_NODES])
        elif isinstance(node, bytes | bytearray):
            total += len(node)
        elif node is not None:
            total += 8
    return total, cut


def _tool_name(span: Mapping[str, Any]) -> str:
    metadata = span.get("metadata")
    if isinstance(metadata, Mapping) and isinstance(metadata.get("tool_name"), str):
        return metadata["tool_name"]
    name = str(span.get("name") or "tool")
    return name.removeprefix("tool:")


def _add_http(providers: dict[str, dict[str, Any]], metadata: Any) -> None:
    """Sum one tool span's ``metadata.http`` into the run's per-provider table."""
    http = metadata.get("http") if isinstance(metadata, Mapping) else None
    if not isinstance(http, Mapping):
        return
    for name, figures in http.items():
        if not isinstance(figures, Mapping):
            continue
        entry = providers.setdefault(str(name), {"tool_calls": 0, "ms": 0.0, **dict.fromkeys(HTTP_COUNTERS, 0)})
        entry["tool_calls"] += 1
        entry["ms"] = round(entry["ms"] + _ms(figures.get("ms")), 1)
        for counter in HTTP_COUNTERS:
            entry[counter] += _int(figures.get(counter))


def rollup_from_spans(
    spans: Iterable[Mapping[str, Any]],
    *,
    wall_ms: float | None,
    first_token_ms: float | None = None,
) -> dict[str, Any]:
    """The rollup of one run from its local span records (``end_trace``'s ``local_spans``).

    *first_token_ms* is the time from the trace's start to the first streamed
    output of the run: what a chat user waits before anything moves.
    It is set only when a streamed generation produced output, never estimated.

    Never raises on a malformed record: a span that is not a mapping is skipped.
    """
    started = time.perf_counter()
    iterations = generations = tool_calls = tool_errors = 0
    providers: dict[str, dict[str, Any]] = {}
    input_tokens = output_tokens = cache_read = cache_write = 0
    model_ms = tool_ms = wait_ms = 0.0
    tools: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    ttfts: list[float] = []
    sizes_cut = 0
    for span in spans:
        if not isinstance(span, Mapping):
            continue
        kind = span.get("type")
        if kind == "ITERATION":
            iterations += 1
        elif kind == "GENERATION":
            generations += 1
            model_ms += _ms(span.get("duration_ms"))
            ttft = span.get("time_to_first_token")
            if isinstance(ttft, int | float) and not isinstance(ttft, bool) and math.isfinite(ttft):
                ttfts.append(float(ttft))
            input_tokens += _int(span.get("input_tokens"))
            output_tokens += _int(span.get("output_tokens"))
            usage = span.get("usage_details")
            if isinstance(usage, Mapping):
                cache_read += cached_input_tokens(dict(usage))
                cache_write += cache_write_tokens(dict(usage))
        elif kind == "TOOL":
            tool_calls += 1
            name = _tool_name(span)
            duration = _ms(span.get("duration_ms"))
            failed = str(span.get("level") or "").upper() == "ERROR"
            chars, cut = _result_chars(span.get("output"))
            sizes_cut += int(cut)
            tool_errors += int(failed)
            if name in WAIT_TOOL_NAMES:
                wait_ms += duration
            else:
                tool_ms += duration
            entry = tools.setdefault(name, {"calls": 0, "errors": 0, "result_chars": 0, "ms": 0.0})
            entry["calls"] += 1
            entry["errors"] += int(failed)
            entry["result_chars"] += chars
            entry["ms"] += duration
            if chars:
                results.append({"span_id": str(span.get("id") or ""), "tool": name, "result_chars": chars})
            _add_http(providers, span.get("metadata"))

    results.sort(key=lambda item: item["result_chars"], reverse=True)
    ranked_tools = sorted(tools.items(), key=lambda kv: (-kv[1]["result_chars"], -kv[1]["calls"], kv[0]))
    rollup: dict[str, Any] = {
        "version": ROLLUP_VERSION,
        # A run recorded without iteration spans still counts its turns by model calls.
        "iterations": iterations or generations,
        "generations": generations,
        "tool_calls": tool_calls,
        "tool_errors": tool_errors,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
        "wall_ms": round(_ms(wall_ms), 1),
        "model_ms": round(model_ms, 1),
        # Slowest first output of a streamed generation in the run (``None``: none streamed).
        "max_ttft_ms": round(max(ttfts), 1) if ttfts else None,
        "tool_ms": round(tool_ms, 1),
        "wait_ms": round(wait_ms, 1),
        "tool_result_chars": sum(item["result_chars"] for item in tools.values()),
        # Results whose size walk stopped at MAX_SIZE_NODES: their size is a lower bound.
        "tool_result_sizes_cut": sizes_cut,
        "tools": {
            name: {**entry, "ms": round(entry["ms"], 1)} for name, entry in ranked_tools[:MAX_TOOLS]
        },
        "tools_omitted": max(0, len(ranked_tools) - MAX_TOOLS),
        # Integration HTTP calls by provider: calls, ms, retries, 5xx, 429, network errors.
        "http_5xx": sum(entry["5xx"] for entry in providers.values()),
        "http_retries": sum(entry["retries"] for entry in providers.values()),
        "providers": dict(
            sorted(providers.items(), key=lambda kv: (-kv[1]["calls"], kv[0]))[:MAX_PROVIDERS]
        ),
        "largest_tool_results": results[:LARGEST_RESULTS],
    }
    if first_token_ms is not None:
        rollup["first_token_ms"] = round(_ms(first_token_ms), 1)
    rollup["overhead_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
    return rollup


#: The rollup figures that add up across the segments of one run.
ADDITIVE_FIGURES: tuple[str, ...] = (
    "iterations",
    "generations",
    "tool_calls",
    "tool_errors",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "wall_ms",
    "model_ms",
    "tool_ms",
    "wait_ms",
    "tool_result_chars",
    "tool_result_sizes_cut",
    "http_5xx",
    "http_retries",
)


def merge_segment_rollups(current: Mapping[str, Any], earlier: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """One run's rollup over every segment it ran in: *current* plus the *earlier* ones.

    A run resumed after a worker stop records one trace per segment (the
    attempt handed back closes its trace, the next attempt opens a new one).
    Each figure of :data:`ADDITIVE_FIGURES` is summed, ``max_ttft_ms`` is the
    slowest of all, the per-tool table and the largest results are merged, and
    ``segments`` counts the rollups folded in (``resumes`` is that count minus
    one). *current* keeps its other keys (version, first_token_ms, overhead_ms).
    Never raises on a malformed segment: one that is not a mapping is skipped.
    """
    merged: dict[str, Any] = dict(current)
    segments = [current, *(item for item in earlier if isinstance(item, Mapping))]
    for name in ADDITIVE_FIGURES:
        values = [item.get(name) for item in segments]
        numbers = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)]
        if not numbers:
            continue
        total = sum(numbers)
        merged[name] = round(total, 1) if any(isinstance(v, float) for v in numbers) else int(total)
    ttfts = [
        item.get("max_ttft_ms")
        for item in segments
        if isinstance(item.get("max_ttft_ms"), int | float) and not isinstance(item.get("max_ttft_ms"), bool)
    ]
    merged["max_ttft_ms"] = max(ttfts) if ttfts else current.get("max_ttft_ms")

    tools: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for item in segments:
        table = item.get("tools")
        if isinstance(table, Mapping):
            for name, figures in table.items():
                if not isinstance(figures, Mapping):
                    continue
                entry = tools.setdefault(str(name), {"calls": 0, "errors": 0, "result_chars": 0, "ms": 0.0})
                entry["calls"] += _int(figures.get("calls"))
                entry["errors"] += _int(figures.get("errors"))
                entry["result_chars"] += _int(figures.get("result_chars"))
                entry["ms"] = round(entry["ms"] + _ms(figures.get("ms")), 1)
        largest = item.get("largest_tool_results")
        if isinstance(largest, list):
            results.extend(r for r in largest if isinstance(r, Mapping))
    if tools:
        ranked = sorted(tools.items(), key=lambda kv: (-kv[1]["result_chars"], -kv[1]["calls"], kv[0]))
        merged["tools"] = dict(ranked[:MAX_TOOLS])
        merged["tools_omitted"] = max(0, len(ranked) - MAX_TOOLS)
    merged["largest_tool_results"] = sorted(
        (dict(r) for r in results), key=lambda r: _int(r.get("result_chars")), reverse=True
    )[:LARGEST_RESULTS]
    merged["segments"] = len(segments)
    merged["resumes"] = len(segments) - 1
    return merged


def stored_rollup(metadata: Any) -> dict[str, Any] | None:
    """The rollup a trace carries in its metadata, or ``None`` (an older trace)."""
    if isinstance(metadata, Mapping):
        rollup = metadata.get("rollup")
        if isinstance(rollup, Mapping):
            return dict(rollup)
    return None


def percentile(values: list[float], fraction: float) -> float | None:
    """The *fraction* percentile of *values* by linear interpolation between ranks (``None`` when empty)."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * min(1.0, max(0.0, fraction))
    low = math.floor(position)
    high = math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def rollup_percentiles(figures: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """p50 and p90 of each figure of :data:`PERCENTILE_FIGURES` over *figures*, with the count it was read on.

    A figure absent from a run (an older rollup, a cost the platform hides) is
    left out of that figure's count rather than read as zero.
    """
    columns: dict[str, list[float]] = {name: [] for name in PERCENTILE_FIGURES}
    runs = 0
    for item in figures:
        if not isinstance(item, Mapping):
            continue
        runs += 1
        for name in PERCENTILE_FIGURES:
            value = item.get(name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            if math.isfinite(value):
                columns[name].append(float(value))
    summary: dict[str, Any] = {"runs": runs}
    for name, values in columns.items():
        if not values:
            continue
        p50 = percentile(values, 0.5)
        p90 = percentile(values, 0.9)
        summary[name] = {
            "p50": round(p50, 3) if p50 is not None else None,
            "p90": round(p90, 3) if p90 is not None else None,
            "n": len(values),
        }
    return summary
