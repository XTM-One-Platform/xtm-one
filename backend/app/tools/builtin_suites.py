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

"""Which built-in tool suite owns a tool name.

Built-in tool suites (``work_items``, ``shared_state``, ``generate_presentation``,
...) are opt-in per agent through ``Agent.builtin_tool_ids``.  When a model
calls ``update_work_item`` on an agent that does not carry the ``work_items``
suite, the executors refuse the call as an unknown name - and give
no fuzzy "Did you mean: update_knowledge_document?" hint.  Such a hint
is wrong in kind: the name is not a typo of an available tool, it is a
REAL platform tool the agent simply does not have, and pointing the model at
an unrelated tool is what sent a background sub-agent into narrating a
fabricated tool transcript instead of reporting the missing capability.

This module answers the one question the executors need to say the truth:
*is this name a platform built-in tool, and which suite is it from?*  The
index is derived from ``build_builtin_tool_catalog(include_disabled=True)``
- the platform's registry of built-in ids and their sub-tool definitions (the
same source the settings catalog endpoint and skill-package validation read)
- so it can never drift from the tools the builders actually produce.  Two
details of that registry matter here.  A suite this deployment has switched
off (``computer_use`` without Runners) is still indexed: a call into it is a
call to a real built-in that is unavailable, not a typo of some offered tool.
And a tool granted by several ids is owned by the FIRST suite that lists it,
which the catalog orders narrowest first: ``inspect_my_traces`` belongs to
the ``inspect_my_traces`` id, not to the ``agent_builder`` alias that also
grants it, so a refusal names the smallest built-in that would fix it - while
:func:`builtin_tool_witnesses` decides from the full set of grantors which
offered tools actually prove the refused one is wired.  The index is built
lazily on the first refusal and cached for the life of the process; the
catalog is pure object construction (every builder is called with dummy
parameters), so a failure there is deterministic and is cached as an empty
index rather than retried on every refused call.

Lookups accept the model's name exactly, as a reordered variant of a built-in
name (``work_item_update`` for ``update_work_item``) through the same
order-independent token key ``ToolNameResolver`` uses but only when
that key belongs to exactly one built-in tool, or - when the caller asks for
it - as a close misspelling (``update_work_items``), so a near-miss of a
built-in still learns which suite it was reaching for instead of getting no
hint at all.

The catalog (``services/builtin_tool_catalog.py``) runs every built-in tool
builder, whose import chain reaches most of the services layer, so it is
imported inside :func:`_index`, and the first build - a
``sys.modules``-cold import chain plus every builder - is offloaded to a
thread by :func:`ensure_suite_index_loaded` so the executor's refusal never
stalls the event loop (the pattern of ``core.hot_imports``).
"""

from __future__ import annotations

import asyncio
import difflib
import functools
import logging
from typing import NamedTuple

from app.tools.base import normalize_tool_key

logger = logging.getLogger(__name__)

#: Similarity ratio (``difflib``) above which a name that is neither an exact
#: nor a reordered built-in name is still reported as a near-miss of one.
#: Stricter than the executors' ``Did you mean`` cutoff on purpose: this hint
#: only fires when NOTHING offered was close, and it must not turn a typo of
#: an unrelated tool into a false "this is a built-in" claim.
_CLOSE_MATCH_CUTOFF = 0.8


class BuiltinTool(NamedTuple):
    """A built-in tool resolved from a model-supplied name.

    ``match`` says how: ``"exact"`` (the name itself), ``"tokens"`` (a
    reordered variant of ``name``) or ``"close"`` (a misspelling of ``name``,
    only with ``resolve_builtin_tool(..., fuzzy=True)``).
    """

    name: str
    suite: str
    match: str = "exact"


class _SuiteIndex(NamedTuple):
    by_name: dict[str, str]  # tool -> owning suite (the first one listing it)
    by_key: dict[tuple[str, ...], str | None]
    suite_tools: dict[str, tuple[str, ...]]  # suite -> every tool it grants
    grantors: dict[str, frozenset[str]]  # tool -> every suite granting it
    names: tuple[str, ...]


@functools.cache
def _index() -> _SuiteIndex:
    # Lazy: the catalog imports every built-in tool builder (see the module
    # docstring).
    try:
        from app.services.builtin_tool_catalog import build_builtin_tool_catalog

        # Disabled suites included: see the module docstring.
        catalog = build_builtin_tool_catalog(include_disabled=True)
    except Exception as exc:  # pragma: no cover - defensive, see module docstring
        logger.warning(
            "Built-in tool suite index unavailable (%s); refused calls to built-in tools will not be classified",
            exc,
        )
        return _SuiteIndex({}, {}, {}, {}, ())

    by_name: dict[str, str] = {}
    by_key: dict[tuple[str, ...], str | None] = {}
    suite_tools: dict[str, tuple[str, ...]] = {}
    grantors: dict[str, set[str]] = {}
    for suite, definitions in catalog.items():
        names = tuple(str(d["name"]) for d in definitions)
        suite_tools[suite] = names
        for name in names:
            # The first suite listing a tool owns it.  The catalog lists the
            # narrow trace ids before the ``agent_builder`` alias that grants
            # them as well, so ownership is the narrowest built-in to enable.
            by_name.setdefault(name, suite)
            grantors.setdefault(name, set()).add(suite)
            key = normalize_tool_key(name)
            # A token key shared by two DIFFERENT built-ins is ambiguous: keep
            # it as an explicit ``None`` so a reordered variant never resolves
            # to the wrong tool.  The same tool listed under a second suite
            # (the alias) is not a conflict.
            by_key[key] = None if key in by_key and by_key[key] != name else name
    return _SuiteIndex(
        by_name,
        by_key,
        suite_tools,
        {name: frozenset(suites) for name, suites in grantors.items()},
        tuple(by_name),
    )


async def ensure_suite_index_loaded() -> None:
    """Build the index off the event loop if this process has not yet.

    The first build imports the catalog's module chain and runs every
    built-in tool builder (about 100 ms with the LLM layer already loaded,
    seconds in a cold worker); awaiting this before the sync lookup keeps that
    off the event loop.  A no-op once cached.  Concurrent first calls may
    build twice - the build is pure, so the second result is identical.
    """
    if _index.cache_info().currsize:
        return
    await asyncio.to_thread(_index)


def resolve_builtin_tool(name: str, *, fuzzy: bool = False) -> BuiltinTool | None:
    """Return the built-in tool *name* denotes, or ``None`` for any other name.

    Exact names win; a reordered variant is accepted only when its token key
    belongs to exactly one built-in tool.  With *fuzzy*, a close misspelling
    of a built-in name (``_CLOSE_MATCH_CUTOFF``) is accepted last - callers
    only ask for it after nothing the model was offered came close.
    """
    idx = _index()
    suite = idx.by_name.get(name)
    if suite is not None:
        return BuiltinTool(name, suite, "exact")
    canonical = idx.by_key.get(normalize_tool_key(name))
    if canonical is not None:
        return BuiltinTool(canonical, idx.by_name[canonical], "tokens")
    if fuzzy and name:
        close = difflib.get_close_matches(name, idx.names, n=1, cutoff=_CLOSE_MATCH_CUTOFF)
        if close:
            return BuiltinTool(close[0], idx.by_name[close[0]], "close")
    return None


def builtin_suite_tools(suite: str) -> tuple[str, ...]:
    """The tool names a built-in suite grants (empty for an unknown suite).

    The registry's own listing, alias-granted tools included: ``agent_builder``
    lists the trace tools it grants even though the narrow trace ids own them.
    Not the right set for deciding whether a suite is WIRED for an agent -
    that is :func:`builtin_tool_witnesses`.

    A [DEPRECATED] former id (``builtin_tool_aliases``) is no
    catalog key since its group took its place; it still names exactly the
    functions it granted, so an agent row or an assignment that carries one
    is read the way the runtime wires it.
    """
    from app.services.builtin_tool_aliases import alias_scoped_functions

    listed = _index().suite_tools.get(suite)
    if listed is not None:
        return listed
    return alias_scoped_functions(suite)


def builtin_suite_catalog() -> dict[str, tuple[str, ...]]:
    """Every built-in suite and the tool names it grants (the registry's own listing, a copy)."""
    return dict(_index().suite_tools)


def builtin_tool_witnesses(name: str) -> tuple[str, ...]:
    """The other built-in tools whose presence proves *name* is granted.

    A tool ``other`` witnesses *name* when every suite granting ``other``
    grants *name* too: then ``other`` being offered (or registered) means one
    of those suites is enabled, and that suite carries *name* as well - so a
    refused *name* was withheld, not "never enabled".  The subset test is what
    keeps aliases and shared tools honest:

    * ``list_work_items`` witnesses ``update_work_item`` (same single suite);
    * ``create_agent`` witnesses ``inspect_my_traces`` (``agent_builder``
      grants both) but ``inspect_my_traces`` does NOT witness ``create_agent``
      - an agent may carry the narrower ``observability`` group alone;
    * the trace functions witness each other: one ``observability`` group
      grants them all, so a missing one was withheld by scoping or
      the deny list, never "not enabled".

    Empty for a name that is not a built-in tool.
    """
    idx = _index()
    mine = idx.grantors.get(name)
    if not mine:
        return ()
    return tuple(other for other, theirs in idx.grantors.items() if other != name and theirs <= mine)
