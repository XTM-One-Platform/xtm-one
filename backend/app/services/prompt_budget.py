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

"""Prompt size observability: estimation, breakdown, debounced warnings.

Single source of truth for the "large system prompt" diagnostics that
``agent_chat``, ``general_chat`` and ``assignment_helpers`` previously
implemented ad hoc.  Three problems with the old inline warnings:

1. They fired on **every message** of a conversation with a large-persona
   agent — pure log spam in production.
2. They reported only the total size, so operators could not tell WHICH
   component (persona, attached prompts, skills, memory, scaffolding) was
   oversized and therefore could not act on the warning.
3. The token heuristic (``chars // 3``) was duplicated per call site.

Usage::

    from app.services.prompt_budget import (
        estimate_tokens,
        format_section_breakdown,
        should_log_large_prompt,
    )

    est = estimate_tokens(system_prompt)
    if est > LARGE_PROMPT_TOKEN_THRESHOLD and should_log_large_prompt(key):
        logger.warning("... %s", format_section_breakdown(sections, total))
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from typing import Any

# ~3 chars per token is a conservative cross-provider heuristic (English
# prose averages ~4; prompts with markup/JSON trend lower).
_CHARS_PER_TOKEN = 3

# Threshold above which instruction-following fidelity measurably degrades
# on long-persona agents (kept from the original inline warnings).
LARGE_PROMPT_TOKEN_THRESHOLD = 20_000

# Warn at most once per key (agent / assignment) per interval.
_WARN_INTERVAL_SECONDS = 3600.0

# Bounded so a pathological key cardinality (e.g. per-conversation keys by
# mistake) cannot grow memory unbounded for the process lifetime.
_MAX_TRACKED_KEYS = 4096

_warn_lock = threading.Lock()
_last_warned: dict[str, float] = {}


def estimate_tokens(text: str) -> int:
    """Estimate the token count of *text* (shared chars//3 heuristic)."""
    return estimate_tokens_for_length(len(text))


def estimate_tokens_for_length(length: int) -> int:
    """:func:`estimate_tokens` for a text of *length* characters not loaded."""
    return length // _CHARS_PER_TOKEN


def should_log_prompt_warning(
    key: str, *, interval_seconds: float = _WARN_INTERVAL_SECONDS
) -> bool:
    """Return True at most once per *interval_seconds* for a given *key*.

    In-process debounce (per worker).  Multi-replica deployments may emit
    one warning per replica per interval — acceptable for observability
    and far better than one warning per message.  Shared by every
    system-prompt warning (large prompt, unresolved placeholders); each
    names its own key namespace.
    """
    now = time.monotonic()
    with _warn_lock:
        last = _last_warned.get(key)
        if last is not None and (now - last) < interval_seconds:
            return False
        if len(_last_warned) >= _MAX_TRACKED_KEYS and key not in _last_warned:
            # Evict the stalest entry instead of refusing to track.
            stalest = min(_last_warned, key=_last_warned.__getitem__)
            del _last_warned[stalest]
        _last_warned[key] = now
        return True


should_log_large_prompt = should_log_prompt_warning


def section_chars(sections: list[tuple[str, str]]) -> dict[str, int]:
    """Characters per label of *sections*, a label used twice summed.

    The ``context_sections`` an agent loop splits its context readout's
    ``system`` bucket by (``agent_loop._context_detail``): the same sections
    the large-prompt warning breaks down.
    """
    out: dict[str, int] = {}
    for label, text in sections:
        if text:
            out[label] = out.get(label, 0) + len(text)
    return out


def format_section_breakdown(
    sections: list[tuple[str, str]], total_chars: int
) -> str:
    """Render a per-component size breakdown for a large-prompt warning.

    *sections* maps a label to the text it contributed; everything not
    covered by a labelled section is reported as ``platform_scaffolding``.
    Sections are sorted by size (largest first) and empty ones skipped so
    the log line points straight at the dominant contributor.
    """
    sized = [(label, len(text)) for label, text in sections if text]
    accounted = sum(size for _, size in sized)
    scaffolding = max(total_chars - accounted, 0)
    if scaffolding:
        sized.append(("platform_scaffolding", scaffolding))
    sized.sort(key=lambda item: item[1], reverse=True)
    return ", ".join(
        f"{label}={size} chars (~{size // _CHARS_PER_TOKEN} tok)"
        for label, size in sized
    )


#: Tool sources a turn's ``fixed_context`` names one by one; the rest are summed as ``other``.
TOP_TOOL_SOURCES = 15


def section_sizes(sections: list[tuple[str, str]], total_chars: int) -> dict[str, int]:
    """The chars each labelled prompt section contributes, a label repeated summed.

    The remainder of *total_chars* no section covers is ``platform_scaffolding``
    (clamped at zero), as in :func:`format_section_breakdown`. Empty sections
    are left out; the result is ordered largest first.
    """
    sizes: dict[str, int] = {}
    for label, text in sections:
        if text:
            sizes[label] = sizes.get(label, 0) + len(text)
    scaffolding = max(total_chars - sum(sizes.values()), 0)
    if scaffolding:
        sizes["platform_scaffolding"] = sizes.get("platform_scaffolding", 0) + scaffolding
    return dict(sorted(sizes.items(), key=lambda item: item[1], reverse=True))


def _tool_schema_chars(tool: Any) -> int:
    """Chars one tool costs on the wire: its name, description and input schema."""
    try:
        schema = json.dumps(
            getattr(tool, "input_schema", None) or {},
            separators=(",", ":"),
            ensure_ascii=False,
            default=str,
        )
    except (TypeError, ValueError):
        schema = ""
    name = getattr(tool, "name", "") or ""
    description = getattr(tool, "description", "") or ""
    return len(name) + len(description) + len(schema)


def tool_schema_sizes(tools: Iterable[Any], *, top: int = TOP_TOOL_SOURCES) -> dict[str, Any]:
    """The tool schema chars a generation carries: total, count and by tool source.

    ``by_source`` names the *top* largest sources, largest first; the others
    are summed under ``other``. Never raises on a malformed tool: it counts
    what it can read.
    """
    by_source: dict[str, int] = {}
    count = 0
    for tool in tools:
        count += 1
        source = str(getattr(tool, "source", "") or "unknown")
        by_source[source] = by_source.get(source, 0) + _tool_schema_chars(tool)
    ranked = sorted(by_source.items(), key=lambda item: item[1], reverse=True)
    named = dict(ranked[:top])
    rest = sum(size for _, size in ranked[top:])
    if rest:
        named["other"] = rest
    return {
        "tool_schema_chars": sum(by_source.values()),
        "tool_count": count,
        "by_source": named,
    }


def fixed_context(
    sections: list[tuple[str, str]], system_prompt: str, tools: Iterable[Any]
) -> dict[str, Any]:
    """What a generation of this turn sends before any message: prompt sections and tool schemas.

    Sizes only, never content. ``est_tokens`` uses :func:`estimate_tokens_for_length`.
    """
    tool_sizes = tool_schema_sizes(tools)
    prompt_chars = len(system_prompt or "")
    total = prompt_chars + tool_sizes["tool_schema_chars"]
    return {
        "system_prompt_chars": prompt_chars,
        "sections": section_sizes(sections, prompt_chars),
        **tool_sizes,
        "total_chars": total,
        "est_tokens": estimate_tokens_for_length(total),
    }


def sent_context(visible_tools: Iterable[Any], catalog_prompt: str) -> dict[str, Any]:
    """What a gated generation sends of its tools: the visible schemas and the catalog message.

    Progressive tool disclosure offers the model the always-on tools, ``load_tools``
    and the revealed groups, plus a catalog message naming the gated groups;
    :func:`fixed_context` sizes the whole catalogue before the gate. Sizes only.
    """
    tool_sizes = tool_schema_sizes(visible_tools)
    catalog_chars = len(catalog_prompt or "")
    total = tool_sizes["tool_schema_chars"] + catalog_chars
    return {
        **tool_sizes,
        "catalog_prompt_chars": catalog_chars,
        "total_chars": total,
        "est_tokens": estimate_tokens_for_length(total),
    }


def reset_warning_state() -> None:
    """Clear the debounce state (test helper)."""
    with _warn_lock:
        _last_warned.clear()
