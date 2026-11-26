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

"""Progressive tool disclosure ("tool multiplexer") for the agentic loop.

Large agents accumulate many tools — a single OpenCTI or OpenAEV integration
contributes dozens, and a Notion / GitHub / Slack stack adds dozens more.
Sending *every* tool's JSON schema to the LLM on *every* turn is expensive: a
trivial ``"hello"`` pays the full tool-definition token cost for tools it never
uses, and large flat tool lists measurably degrade tool-selection accuracy.

This module implements the agentic best practice of **progressive tool
disclosure**:

1. Tools are partitioned into an *always-on* core (cheap built-ins such as
   knowledge-base search, memory, handover, the agent builder) and *gateable
   groups* keyed by their ``source`` — one group per integration provider, one
   per MCP server, and one for all custom tools.
2. A compact **catalogue** of the gated groups (id + label + tool count + a few
   sample names) is injected into the system context — a few dozen tokens
   instead of the full schemas.
3. A single ``load_tools`` meta-tool lets the model reveal a group's full tool
   schemas on demand.  Revealed groups stay visible for the remainder of the
   loop — unless the provider's tool budget forces one out (below).
4. The provider's ``max_tools`` cap is a property of the **wire** — how many
   schemas one request may carry — not of the agent.  With gating active only
   the visible list is ever on the wire, so the cap is enforced *here*, on the
   visible list (``max_visible``), and the agent's inventory is left whole:
   every group stays in the catalogue on every provider.  When a
   reveal does not fit, the **least recently used** revealed group(s) are
   unloaded first.  Recency is refreshed on reveal and on every tool call —
   resolved with the executor's own exact / reordered / alias rules — and the
   groups a model response is calling into are additionally *protected* for
   every ``load_tools`` call of that response, so a group in active use is
   never the one to go.  A single group larger than
   the remaining room is revealed **partially**, highest-priority tools first,
   and the remainder stays reachable by name through the ``tools`` argument of
   ``load_tools`` — so the full inventory is always reachable, on a 128-tool
   provider as much as on a 1024-tool one.  The remainder listing is an
   ordinary tool result that the loop stubs once it is stale, so the exact
   names are also re-fetchable at any time through the bounded ``search``
   argument of ``load_tools``.
5. The budget always keeps **one slot for a gated tool** (``MIN_GATED_BUDGET``
   = the meta-tool plus one real tool): the always-on core is trimmed to
   ``max_visible - 2`` and the built-ins that do not fit are not dropped but
   **demoted** into a synthetic gated group (``CORE_OVERFLOW_GROUP_ID``) that
   is catalogued and loadable like any other — so the catalogue never
   advertises a tool the gate can not reveal, and nothing is silently lost.
6. Every gate lookup keyed by a model-supplied tool name resolves over the
   **whole offered surface** (core + gated) with the executor's own rules, so
   the gate always resolves a name to the SAME tool dispatch executes — an
   exact core name wins over a token-equivalent gated one, and ambiguity is
   judged over the same candidate set; a name that denotes a core tool is
   then simply not a recency event (the core is never evicted).

For a simple conversation the model answers directly from the always-on core
and never pays for the gated schemas.  For a real task it issues one
``load_tools`` call, then proceeds with the freshly-revealed tools.

The gate only controls **which tool schemas the LLM sees** — every tool stays
registered in the executor, so tool *execution* is unaffected.  This makes the
mechanism safe: even if a model references a not-yet-loaded tool by name, the
executor can still run it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.tools.base import ToolDefinition, ToolNameResolver, declared_aliases, tool_retention_key

#: Name of the meta-tool the LLM calls to reveal gated tool groups.
#:
#: Also spelled as a literal in ``app.tools.approval.LOOP_HANDLED_TOOLS``,
#: which stays import-free by design. Renaming this alone leaves that set
#: stale — ``tests/test_gate_outcome_recording.py`` pins the two together,
#: so the break is loud, but change both.
LOAD_TOOLS_TOOL_NAME = "load_tools"

#: The smallest provider tool budget (``max_tools``) gating can run on: the
#: ``load_tools`` meta-tool itself plus room for at least ONE real tool.  Below
#: this a gate is both useless and over-cap — ``visible_tools()`` always carries
#: the meta-tool, so on a ``max_tools == 0`` provider the request would break
#: the cap with a single schema, and on ``max_tools == 1`` the model could load
#: nothing, ever.  ``build_tool_gate`` returns ``None`` under this budget so
#: the legacy path truncates the wire list to the cap instead (to zero on a
#: zero-tool provider), and a budget downgrade onto such a provider is refused
#: rather than re-capping a gate the provider cannot carry.  The same two
#: slots are what ``_fit_core`` reserves out of the budget: the core is
#: trimmed to ``max_visible - MIN_GATED_BUDGET`` so at least one gated tool can
#: always be revealed — a gate whose core fills the cap would catalogue groups
#: that every ``load_tools`` call answers with ``no_room``.
MIN_GATED_BUDGET = 2

#: Group id of the always-on tools the budget could not keep always-on (see
#: ``ToolGate._fit_core``).  They are demoted into this synthetic gated group
#: — catalogued, loadable, searchable like any other — instead of being
#: dropped, so a tight cap changes WHEN a built-in is on the wire, never
#: WHETHER it can be.  The id can not collide with a real group: the only
#: ``builtin:`` ids ``tool_group`` produces are the on-demand built-in groups
#: the caller names (``BuiltinOnDemand``), and no built-in group is called
#: ``overflow``.
CORE_OVERFLOW_GROUP_ID = "builtin:overflow"
_CORE_OVERFLOW_LABEL = "Built-in tools beyond the always-on budget"
#: Demoted always-on tools named one by one in the catalogue (#2191).
_DEMOTED_NOTICE_MAX_NAMES = 30

#: Built-in groups made loadable on demand: built-in group id (the
#: part after ``builtin:`` in a tool's ``source``) -> (catalogue label, the
#: names that stay always-on).  The rest of such a group is gated as
#: ``builtin:<id>``; a built-in group the mapping does not name stays
#: always-on whole.  The caller supplies it (the agent loop reads it off the
#: built-in registry), so this module keeps no product knowledge.
BuiltinOnDemand = Mapping[str, tuple[str, frozenset[str]]]

#: ``source`` categories whose tools are gated behind ``load_tools``.  Built-in
#: tools (KB search, memory, handover, …) are core to agent behaviour, so they
#: stay always-on for zero-round-trip simple chat, except the part of a large
#: built-in group outside its always-on core (``BuiltinOnDemand``).
#
#: ``channel_bot`` is here because a bot's tools used to arrive as
#: ``integration:slack_bot`` and were gated with the rest. Restamping them
#: ``channel_bot:<provider>:<id>`` quietly moved a dozen send/edit/delete
#: tools into the always-on set, growing every request's schema.
_GATEABLE_CATEGORIES: frozenset[str] = frozenset(
    {"integration", "mcp", "custom", "channel_bot"}
)

#: How many sample tool names to show per group in the catalogue prompt.
_CATALOG_SAMPLE_LIMIT = 6

#: How many not-loaded names one ``load_tools(search=...)`` returns at most.
#: Bounded so the recovery path can never re-inflate the context the way
#: re-listing a whole 300-tool remainder would; a wider match is narrowed
#: by the model, not truncated silently.  Applied AFTER every match across
#: the searched groups is ranked by ``tool_retention_key`` — the cut is
#: "least important first", never "later group first".
_SEARCH_RESULT_LIMIT = 40

#: How many ALREADY-LOADED matches a search names.  Deliberately much smaller
#: than the not-loaded cap: these tools' schemas are on the wire right now, so
#: the list is a reminder, not a handle — a truncated tail loses nothing the
#: model cannot already see, while an uncapped one (a broad search after a
#: large group was revealed) would re-list hundreds of visible names.
_SEARCH_LOADED_LIMIT = 12

def _string_items(value: Any) -> list[str]:
    """A ``load_tools`` argument as the strings it names: a bare string is one,
    a list keeps its strings, anything else (a number, a boolean, an object)
    names nothing - the same reading the loop gives a live call."""
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


#: How many groups ``preload_from_transcript`` reveals from earlier turns -
#: the MOST RECENTLY used ones. A long conversation can touch many
#: integrations; revealing every one of them would put their schemas on every
#: request (the flat-catalogue cost the gate exists to avoid) and grow the
#: catalogue's "Already loaded" line without bound. An older group is one
#: ``load_tools`` away, as before.
_PRELOAD_MAX_GROUPS = 6

#: A search query is split into word tokens (letters, digits, underscore), so
#: ``"create, issue"`` and ``"github.create"`` search like ``"create issue"``
#: does — the model's punctuation never turns a hit into a miss.  Underscore
#: stays inside a token so ``tool_29`` matches the sanitised name it names.
_SEARCH_WORD_RE = re.compile(r"\w+")


def _humanize(slug: str) -> str:
    """Turn a provider/server slug into a human-friendly label; a name written with capitals is kept."""
    if slug != slug.lower():
        return slug.strip()
    return slug.replace("_", " ").replace("-", " ").strip().title() or slug


def tool_group(
    tool: ToolDefinition, on_demand: BuiltinOnDemand | None = None
) -> tuple[str, str] | None:
    """Return ``(group_id, label)`` for a gateable tool, or ``None``.

    ``None`` means the tool is always-on (never gated).  A built-in tool is
    gated only when *on_demand* names its group and the tool is outside the
    group's always-on core; a ``core`` tool (one every agent carries) never is.
    """
    source = tool.source or ""
    category, _, rest = source.partition(":")
    if category == "builtin":
        spec = on_demand.get(rest) if on_demand and rest else None
        if spec is None or tool.core or tool.name in spec[1]:
            return None
        return f"builtin:{rest}", spec[0]
    if category not in _GATEABLE_CATEGORIES:
        return None
    if category == "integration":
        provider = rest or "integration"
        return f"integration:{provider}", tool.source_label or _humanize(provider)
    if category == "channel_bot":
        # ``channel_bot:<provider>:<id>``. Grouped by provider, not by bot: the
        # group is what a model reads in the catalogue prompt, and two Slack
        # bots offering the same tools are one capability to it.
        provider = rest.partition(":")[0] or "channel_bot"
        return f"channel_bot:{provider}", tool.source_label or _humanize(provider)
    if category == "mcp":
        server = rest or "server"
        return f"mcp:{server}", f"{_humanize(server)} (MCP)"
    # custom tools all share a single group — their per-tool source carries a
    # UUID that is meaningless to the LLM.
    return "custom", "Custom Tools"


def _sample(tools: list[ToolDefinition]) -> str:
    """``name, name, …, +N more`` for a tool list (catalogue / reveal text)."""
    shown = ", ".join(t.name for t in tools[:_CATALOG_SAMPLE_LIMIT])
    extra = len(tools) - _CATALOG_SAMPLE_LIMIT
    return f"{shown}, +{extra} more" if extra > 0 else shown


@dataclass
class ToolGroup:
    """A revealable collection of tools that share a ``source`` group."""

    id: str
    label: str
    tools: list[ToolDefinition] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.tools)

    def catalog_line(self) -> str:
        """One compact catalogue line: id, label, count, sample names."""
        return f'- "{self.id}" — {self.label} ({self.count} tools): {_sample(self.tools)}'

    def ranked(self) -> list[ToolDefinition]:
        """The group's tools, most important first (stable within a rank).

        This is the order a partial reveal fills its room in, so the tools an
        integration marked ``priority=0`` are the ones that make it onto the
        wire when the whole group cannot.
        """
        return sorted(self.tools, key=tool_retention_key)


def group_tools(
    tools: list[ToolDefinition],
    *,
    on_demand: BuiltinOnDemand | None = None,
) -> tuple[list[ToolDefinition], dict[str, ToolGroup]]:
    """Partition *tools* into ``(always_on, groups)``.

    ``always_on`` preserves input order; ``groups`` is keyed by group id and
    ordered by first appearance. A group is labelled with the first
    ``source_label`` one of its tools carries, else its humanised slug (an
    on-demand built-in group: the label *on_demand* gives it).
    """
    always_on: list[ToolDefinition] = []
    groups: dict[str, ToolGroup] = {}
    labelled: set[str] = set()
    for tool in tools:
        grouped = tool_group(tool, on_demand)
        if grouped is None:
            always_on.append(tool)
            continue
        group_id, label = grouped
        group = groups.get(group_id)
        if group is None:
            group = ToolGroup(id=group_id, label=label)
            groups[group_id] = group
        if tool.source_label and group_id not in labelled:
            group.label = label
            labelled.add(group_id)
        group.tools.append(tool)
    return always_on, groups


def build_load_tools_definition(groups: dict[str, ToolGroup]) -> ToolDefinition:
    """Build the ``load_tools`` meta-tool schema for the given *groups*.

    ``groups`` is the normal way in (an enum of every group id, so the model
    cannot misspell one).  ``tools`` names individual tools — the way back to
    the part of a group that did not fit the provider's tool budget when it
    was loaded (the ``load_tools`` result lists those names).  ``search``
    finds those exact names again once that result has aged out of the
    context (the loop stubs older tool results — see
    ``agent_loop._clear_stale_tool_results``), so the remainder never depends
    on the first listing still being in the transcript.  Any combination may
    be given; the schema deliberately has no ``required`` so a call with only
    ``tools`` or only ``search`` validates on every provider.
    """
    group_ids = list(groups.keys())
    sample_ids = ", ".join(group_ids[:3])
    return ToolDefinition(
        name=LOAD_TOOLS_TOOL_NAME,
        description=(
            "Reveal the full set of tools for one or more tool groups so you "
            "can call them. The available groups are listed in the system "
            "context under 'Available tool groups'. Call this with the exact "
            "group name(s) you need BEFORE attempting to use their tools. "
            "Tools already visible to you do NOT need to be loaded. Only load a "
            "group when the user's request actually requires it — do not load "
            "groups for greetings, small talk, or questions you can already "
            "answer. If a previous load reported tools it could not fit, pass "
            "their exact names in 'tools' to load just those; if you no longer "
            "have those names, find them with 'search'."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "groups": {
                    "type": "array",
                    "items": {"type": "string", "enum": group_ids},
                    "description": f"Tool group name(s) to load, e.g. {sample_ids}.",
                },
                "tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Exact tool name(s) to load individually — for tools a "
                        "previous load_tools result listed as not loaded."
                    ),
                },
                "search": {
                    "type": "string",
                    "description": (
                        "Keyword(s) to find tools by name or description; "
                        "returns the exact names of not-yet-loaded ones to "
                        "pass in 'tools' and names the matches you can "
                        "already call. Scoped to 'groups' when both are given."
                    ),
                },
            },
        },
        source="builtin:tool_gating",
        priority=0,
    )


def build_catalog_prompt(
    groups: dict[str, ToolGroup],
    *,
    max_visible: int | None = None,
    core_count: int = 0,
    demoted: Sequence[str] = (),
) -> str:
    """Build the compact catalogue system message for the gated *groups*.

    *demoted* names the always-on tools ``ToolGate._fit_core`` moved into
    ``CORE_OVERFLOW_GROUP_ID``: one line names each of them (bounded) with the
    exact ``load_tools`` call, so an agent that expects one of its always-on
    tools is told where it went instead of finding it silently missing (#2191).

    When the provider's tool budget (``max_visible``) cannot hold the core plus
    every gated tool at once, one extra paragraph tells the model how the
    budget behaves — it is the only prompt text the cap adds, and it replaces
    the far longer "N tools were unavailable this run" notice the legacy
    inventory truncation used to inject. Groups preloaded from earlier
    turns need no line of their own: their tools are on the wire, and
    the intro already says a visible tool requires no loading.
    """
    lines = [
        "── Available tool groups (loaded on demand) ──",
        (
            "Additional tools are available but NOT yet loaded, to keep this "
            "conversation efficient. To use any tool from a group below, first "
            f"call `{LOAD_TOOLS_TOOL_NAME}` with the exact group name(s) (the "
            "quoted id). Tools already visible to you require no loading. Do "
            "NOT load any group for greetings, small talk, or requests you can "
            "already satisfy — only load what the task genuinely needs."
        ),
    ]
    gated_total = sum(group.count for group in groups.values())
    if max_visible is not None and core_count + 1 + gated_total > max_visible:
        room = max(max_visible - core_count - 1, 0)
        lines.append(
            f"Tool budget: this model accepts at most {max_visible} tool "
            f"schemas per request, {room} of which are free for loaded groups. "
            "Loading a group that does not fit unloads the least recently "
            "used loaded group(s) first (you can load them again). A group "
            "larger than the free room is loaded partially, most important "
            "tools first; the result names the rest, which you can load "
            f"individually with the `tools` argument of `{LOAD_TOOLS_TOOL_NAME}`."
        )
    if demoted:
        shown = list(demoted[:_DEMOTED_NOTICE_MAX_NAMES])
        extra = len(demoted) - len(shown)
        names = ", ".join(shown) + (f", +{extra} more" if extra > 0 else "")
        lines.append(
            f"Moved out of your always-on tools for this run (provider tool budget): {names}. "
            f'They are not lost: load them with `{LOAD_TOOLS_TOOL_NAME}` groups=["{CORE_OVERFLOW_GROUP_ID}"], '
            "or one by name with its `tools` argument."
        )
    lines.append("")
    lines.extend(group.catalog_line() for group in groups.values())
    return "\n".join(lines)


@dataclass
class RevealResult:
    """What one ``load_tools`` call changed — the input of ``describe_reveal``.

    Group ids and tool names are the model-facing (sanitised) ones.
    """

    #: Groups now FULLY visible because of this call.
    revealed: list[str] = field(default_factory=list)
    #: Groups this call could only reveal partially: id -> visible tool count.
    partial: dict[str, int] = field(default_factory=dict)
    #: Individual tools loaded by name.
    pinned: list[str] = field(default_factory=list)
    #: Groups unloaded (least recently used first) to make room.
    evicted: list[str] = field(default_factory=list)
    #: Groups that gave up only the tools the run has NOT called to make room:
    #: id -> how many tools were unloaded.  The called tools stay visible.
    trimmed: dict[str, int] = field(default_factory=dict)
    #: Visible tools dropped from a group to fit a tool of the SAME group
    #: named in ``tools`` when nothing else could be unloaded.
    displaced: list[str] = field(default_factory=list)
    #: Requested groups that were already fully visible (no-op).
    already: list[str] = field(default_factory=list)
    #: Requested groups already PARTIALLY visible for which no more room could
    #: be freed: id -> visible tool count.  A no-op apart from the recency
    #: refresh — nothing was added, so this is deliberately NOT ``partial``
    #: (which re-lists the not-loaded names and would bloat the conversation
    #: on every repeated ``load_tools`` call).
    at_capacity: dict[str, int] = field(default_factory=dict)
    #: Group tokens / tool names that matched nothing.
    unknown: list[str] = field(default_factory=list)
    unknown_tools: list[str] = field(default_factory=list)
    #: Tool names the agent carries but this turn withholds on purpose:
    #: name -> reason (``CompositeToolExecutor.withhold``).  Never unknown.
    withheld_tools: dict[str, str] = field(default_factory=dict)
    #: Requests (group ids or tool names) that could not be satisfied at all:
    #: the budget is full and nothing evictable was left — this same call's
    #: own loads (or the response's protected groups) fill it.  The core alone
    #: never does: ``_fit_core`` keeps one slot for a gated tool.
    no_room: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.revealed or self.partial or self.pinned)


@dataclass
class SearchResult:
    """What one ``load_tools(search=...)`` found — the input of ``describe_search``.

    Read-only: a search never changes what is visible.  Tool names are the
    model-facing (sanitised) ones, exactly as ``tools=[...]`` expects them.
    """

    query: str
    #: Group ids the search was scoped to.  Empty with ``unknown`` empty =
    #: every gated group (no ``groups`` given); empty with ``unknown`` set =
    #: ``groups`` was given but nothing resolved, so NOTHING was searched.
    scope: list[str] = field(default_factory=list)
    #: Group tokens that resolved to no group (reported, never widened).
    unknown: list[str] = field(default_factory=list)
    #: Matching tools NOT currently visible, most important first ACROSS all
    #: searched groups (``tool_retention_key``), capped at
    #: ``_SEARCH_RESULT_LIMIT``: (tool name, group id).
    not_loaded: list[tuple[str, str]] = field(default_factory=list)
    #: How many not-loaded tools matched before the cap.
    not_loaded_total: int = 0
    #: Matching tools that are ALREADY visible — callable right now.  Capped at
    #: ``_SEARCH_LOADED_LIMIT``; ``loaded_total`` says how many matched.
    loaded: list[str] = field(default_factory=list)
    loaded_total: int = 0
    #: Withheld functions whose name matches: name -> reason.
    withheld: dict[str, str] = field(default_factory=dict)


class ToolGate:
    """Holds the always-on / gated split and tracks which groups are revealed.

    Mutated as the agent loop proceeds: ``reveal()`` is called when the model
    invokes ``load_tools``, ``touch()`` on every tool call the model makes,
    ``set_max_visible()`` when the run moves to a provider with a different
    cap, and ``visible_tools()`` returns the schemas to send on the next
    iteration — never more than ``max_visible`` of them.

    Recency is the dict order of ``_visible`` (oldest first): a group is moved
    to the end whenever it is (re)loaded or one of its tools is called, and
    eviction always takes the front.
    """

    def __init__(
        self,
        always_on: list[ToolDefinition],
        groups: dict[str, ToolGroup],
        load_tools_definition: ToolDefinition,
        *,
        max_visible: int | None = None,
        aliases: Mapping[str, str] | None = None,
        resolver: ToolNameResolver | None = None,
        withheld: Mapping[str, str] | None = None,
        withheld_integrations: Mapping[str, str] | None = None,
    ) -> None:
        self._always_on = list(always_on)
        #: Functions the agent carries that this turn withholds: name -> reason.
        self._withheld: dict[str, str] = dict(withheld or {})
        #: Integrations the run left out (their tools were never built):
        #: label -> reason. Named when a requested tool resolves to nothing.
        self._withheld_integrations: dict[str, str] = dict(withheld_integrations or {})
        self._groups = dict(groups)
        self._load_tools_definition = load_tools_definition
        self._max_visible = max_visible
        #: group id -> the group's currently visible tools (full or partial),
        #: in least-recently-used order.
        self._visible: dict[str, list[ToolDefinition]] = {}
        #: Gated tool names the run has called (``touch``): pinned against
        #: eviction, displacement and a re-cap that still fits them.
        self._called: set[str] = set()
        #: tool name -> (group id, tool) for every gated tool.
        self._tool_index: dict[str, tuple[str, ToolDefinition]] = {
            tool.name: (group.id, tool)
            for group in groups.values()
            for tool in group.tools
        }
        #: Always-on tools the budget could not keep always-on (see
        #: ``_fit_core``): demoted into the ``CORE_OVERFLOW_GROUP_ID`` group,
        #: never dropped.  Exposed so the loop can log what moved.
        self.demoted_always_on: list[ToolDefinition] = []
        #: Groups revealed from the conversation transcript before the first
        #: request (``preload_from_transcript``) - for the loop's log;
        #: the catalogue needs no line for them (their tools are visible).
        self.preloaded_group_ids: list[str] = []
        self._fit_core()
        #: Exact / reordered / alias resolution for model-supplied tool names —
        #: the SAME rules ``CompositeToolExecutor`` dispatches with, over the
        #: SAME candidates: the whole offered surface, core included, not just
        #: the gated tools.  Resolving over the gated tools alone let the gate
        #: and dispatch disagree: with core ``jira_search_issues`` and gated
        #: ``search_jira_issues`` the executor runs the exact core tool while
        #: the gate token-matched the gated one and falsely protected its
        #: group.  Over the full surface the exact match wins
        #: and ambiguity is judged over the same set; ``_resolve_tool`` then
        #: keeps only canonicals that are GATED.  The name SPACE is the
        #: sanitised (LLM-visible) one — the loop builds the gate from the
        #: sanitised definitions and passes call names verbatim — so an
        #: *aliases* table must be in that space (``visible_aliases``).  The
        #: loop hands in its dispatch map's own *resolver* instance, so the
        #: two can not drift; *aliases* is for a gate built stand-alone.
        self._names = (
            resolver
            if resolver is not None
            else ToolNameResolver(self.offered_names(), aliases=aliases)
        )
        self.catalog_prompt = self._build_catalog_prompt()

    # ── Introspection ─────────────────────────────────────────────────

    @property
    def load_tools_definition(self) -> ToolDefinition:
        return self._load_tools_definition

    @property
    def group_count(self) -> int:
        return len(self._groups)

    @property
    def gated_tool_count(self) -> int:
        return sum(group.count for group in self._groups.values())

    @property
    def revealed_group_ids(self) -> set[str]:
        """Groups whose EVERY tool is currently visible."""
        return {
            group_id
            for group_id, visible in self._visible.items()
            if len(visible) == self._groups[group_id].count
        }

    @property
    def partially_revealed_group_ids(self) -> set[str]:
        return {
            group_id
            for group_id, visible in self._visible.items()
            if len(visible) < self._groups[group_id].count
        }

    @property
    def max_visible(self) -> int | None:
        """The provider's tool budget, or ``None`` when unbounded."""
        return self._max_visible

    @property
    def visible_count(self) -> int:
        return (
            len(self._always_on)
            + 1
            + sum(len(visible) for visible in self._visible.values())
        )

    @property
    def min_visible_count(self) -> int:
        """The smallest list this gate ever sends: core + ``load_tools``."""
        return len(self._always_on) + 1

    @property
    def required_budget(self) -> int:
        """The smallest cap this gate can be re-capped to AS IT STANDS.

        Core + ``load_tools`` + one slot for a gated tool (``MIN_GATED_BUDGET``
        beyond the core) — the surface a provider must carry for a gated run
        to move onto it (the loop's downgrade guard).  One slot short and the
        re-cap would have to demote a core tool the model has been using as
        always-on; two short and the gate could reveal nothing at all.
        """
        return len(self._always_on) + MIN_GATED_BUDGET

    def offered_names(self) -> list[str]:
        """Every tool name the gate can put on the wire: core + all gated.

        The candidate set a name resolver for this gate must be built over —
        the same set the loop's dispatch map resolves against.
        """
        return [*(t.name for t in self._always_on), *self._tool_index]

    # ── Per-iteration view ────────────────────────────────────────────

    def visible_tools(self) -> list[ToolDefinition]:
        """Return the tool schemas the LLM should see this iteration.

        Always-on tools + the ``load_tools`` meta-tool + every revealed group
        (in stable group order, each group's visible subset).
        """
        visible = list(self._always_on)
        visible.append(self._load_tools_definition)
        for group_id in self._groups:
            revealed = self._visible.get(group_id)
            if revealed:
                visible.extend(revealed)
        return visible

    # ── Budget plumbing ───────────────────────────────────────────────

    def _build_catalog_prompt(self) -> str:
        return build_catalog_prompt(
            self._groups,
            max_visible=self._max_visible,
            core_count=len(self._always_on),
            demoted=[tool.name for tool in self.demoted_always_on],
        )

    # ── Persistence across turns ──────────────────────────────

    def preload_from_transcript(self, messages: Iterable[Any]) -> list[str]:
        """Reveal the groups the conversation's earlier turns used.

        A gate is built per ``run_agent_loop`` call, so without this every
        turn of a conversation paid a ``load_tools`` round-trip for the same
        Notion / HiBob / Metabase groups the previous turn had loaded and
        called into (25 Sep 2026 run). The transcript the loop receives is
        the durable record of that state: every ``load_tools`` call an
        earlier assistant turn made (its ``groups`` / ``tools`` arguments)
        and every tool call and tool result of those turns names a group.
        Those groups are revealed here - at most :data:`_PRELOAD_MAX_GROUPS`,
        the most recently used - oldest first and most recently used last, so
        an LRU eviction under the provider cap keeps the groups the
        conversation used most recently. Names resolve through the same
        resolver dispatch uses, over THIS gate's tools only, so a preload can
        never widen the agent's surface: a name that resolves to nothing (a
        tool the agent no longer carries, a tool of the agent a conversation
        was transferred from) is skipped, text in a user message is never
        read, and execution stays scoped by the executor whatever is
        visible. Returns the group ids revealed.

        Duck-typed over the message shape (``role``, ``tool_calls`` with
        ``name`` / ``arguments``, ``name``) so this module stays free of the
        LLM message types, and tolerant of whatever a model once sent: the
        history replays a ``load_tools`` call's arguments as they were, and a
        number or a boolean where a list belongs must cost a preload, never
        the turn.
        """
        ordered: list[str] = []

        def _note(group_id: str | None) -> None:
            if group_id is None:
                return
            if group_id in ordered:
                ordered.remove(group_id)
            ordered.append(group_id)

        def _note_tool(raw: Any) -> None:
            if isinstance(raw, str) and raw:
                entry = self._resolve_tool(raw)
                _note(entry[0] if entry is not None else None)

        for msg in messages:
            role = getattr(msg, "role", None)
            if role == "assistant":
                for call in getattr(msg, "tool_calls", None) or []:
                    name = getattr(call, "name", None)
                    if name == LOAD_TOOLS_TOOL_NAME:
                        arguments = getattr(call, "arguments", None) or {}
                        if not isinstance(arguments, dict):
                            continue
                        for token in _string_items(arguments.get("groups")):
                            _note(self._resolve(token))
                        for tool_name in _string_items(arguments.get("tools")):
                            _note_tool(tool_name)
                    else:
                        _note_tool(name)
            elif role == "tool":
                _note_tool(getattr(msg, "name", None))

        return self._reveal_preloaded(ordered[-_PRELOAD_MAX_GROUPS:])

    def preload_groups(self, tokens: Any) -> list[str]:
        """Reveal the groups a run declares up front, before the first request.

        An assignment names the groups its runs always need
        (``trigger_config.preload_tool_groups``) so their schemas are on the
        wire from iteration 1: a mid-run ``load_tools`` changes the tool list
        at the head of the request, which invalidates the provider's whole
        cached prefix (tools, system and messages), and the next iteration
        pays a full cache write. Tokens resolve like ``load_tools`` groups
        (exact id, bare provider name or label) over THIS gate's groups only,
        so a declaration never widens the agent's surface: an unknown token
        is skipped. At most :data:`_PRELOAD_MAX_GROUPS`, the first ones kept,
        revealed inside the provider budget like any preload. Returns the
        group ids revealed.
        """
        ordered: list[str] = []
        # A string or a list of strings; anything else a stored config
        # could hold (a number, null, a mapping) preloads nothing.
        for token in _string_items(tokens):
            group_id = self._resolve(token)
            if group_id is not None and group_id not in ordered:
                ordered.append(group_id)
        return self._reveal_preloaded(ordered[:_PRELOAD_MAX_GROUPS])

    def _reveal_preloaded(self, group_ids: list[str]) -> list[str]:
        """Reveal ``group_ids`` in order and record the fully visible ones."""
        revealed: list[str] = []
        for group_id in group_ids:
            result = self.reveal([group_id])
            # Only a group whose EVERY tool is now on the wire is "loaded":
            # a partial reveal leaves tools hidden the catalogue line would
            # otherwise call callable, and ``at_capacity`` / ``no_room``
            # changed nothing at all.
            if result.revealed or result.already:
                revealed.append(group_id)
        # Only groups still fully visible after the LRU pass under the cap
        # count as preloaded.
        for group_id in revealed:
            if group_id not in self.preloaded_group_ids:
                self.preloaded_group_ids.append(group_id)
        fully_visible = self.revealed_group_ids
        self.preloaded_group_ids = [
            gid for gid in self.preloaded_group_ids if gid in fully_visible
        ]
        self.catalog_prompt = self._build_catalog_prompt()
        return [gid for gid in revealed if gid in fully_visible]

    def _room(self) -> int | None:
        if self._max_visible is None:
            return None
        return self._max_visible - self.visible_count

    def _fit_core(self) -> list[ToolDefinition]:
        """Trim the always-on core to ``max_visible - MIN_GATED_BUDGET``.

        The budget must always hold the meta-tool AND one gated tool beyond
        the core: a core of ``max_visible - 1`` filled the visible list on its
        own, reveals can not evict core entries, so every group and named load
        came back ``no_room`` and the catalogue advertised tools that were
        permanently unreachable — realistic on a 64-tool provider with the
        large builtin suites enabled.

        Only reachable when the core alone is too big for the cap — the gated
        groups are never the reason a core tool loses its slot.  The trimmed
        tools (lowest ``tool_retention_key`` first) are NOT dropped: they are
        demoted into the ``CORE_OVERFLOW_GROUP_ID`` group, catalogued and
        loadable like any other, so the model keeps every capability and only
        pays a ``load_tools`` round-trip for the demoted ones.  Returns the
        tools demoted by this call (also accumulated on ``demoted_always_on``).
        """
        if self._max_visible is None:
            return []
        keep = max(self._max_visible - MIN_GATED_BUDGET, 0)
        if len(self._always_on) <= keep:
            return []
        ranked = sorted(self._always_on, key=tool_retention_key)
        kept_names = {tool.name for tool in ranked[:keep]}
        demoted = [tool for tool in self._always_on if tool.name not in kept_names]
        self._always_on = [tool for tool in self._always_on if tool.name in kept_names]
        overflow = self._groups.get(CORE_OVERFLOW_GROUP_ID)
        if overflow is None:
            overflow = ToolGroup(id=CORE_OVERFLOW_GROUP_ID, label=_CORE_OVERFLOW_LABEL)
            # First in the catalogue — these are the agent's own built-ins —
            # and in the meta-tool's ``groups`` enum, so the model can name it.
            self._groups = {CORE_OVERFLOW_GROUP_ID: overflow, **self._groups}
            self._load_tools_definition = build_load_tools_definition(self._groups)
        overflow.tools.extend(demoted)
        for tool in demoted:
            self._tool_index[tool.name] = (CORE_OVERFLOW_GROUP_ID, tool)
        self.demoted_always_on.extend(demoted)
        return demoted

    def _touch_group(self, group_id: str) -> None:
        """Mark *group_id* as most recently used."""
        visible = self._visible.pop(group_id, None)
        if visible is not None:
            self._visible[group_id] = visible

    def _evict_until(self, needed: int, protect: set[str], result: RevealResult | None = None) -> list[str]:
        """Unload least-recently-used groups until *needed* slots are free.

        Groups in *protect* (touched by the call in progress) are never
        evicted.  A tool the run has called (``touch``) goes last: a group
        holding one first gives up only as many of its uncalled tools as the
        load needs (recorded in ``result.trimmed``) and stays visible with
        the called ones, so a sandbox or GitHub tool used earlier in the run
        is still on the wire when the model comes back to it.  Only when
        every uncalled tool is gone and the load still does not fit is the
        least recently used group unloaded whole, called tools included.
        Stops early when nothing evictable is left.  Returns the groups
        unloaded whole.
        """
        evicted: list[str] = []
        room = self._room()
        if room is None:
            return evicted
        for group_id in list(self._visible):
            if room >= needed:
                break
            if group_id in protect:
                continue
            current = self._visible[group_id]
            kept = [t for t in current if t.name in self._called]
            if not kept:
                room += len(self._visible.pop(group_id))
                evicted.append(group_id)
                continue
            # Only as many uncalled tools as the load needs, least important
            # first (the reverse of a partial reveal's order).
            uncalled = sorted(
                (t for t in current if t.name not in self._called),
                key=tool_retention_key,
                reverse=True,
            )
            drop = {t.name for t in uncalled[: max(needed - room, 0)]}
            dropped = len(drop)
            if dropped == 0:
                continue
            self._visible[group_id] = [t for t in current if t.name not in drop]
            room += dropped
            if result is not None:
                result.trimmed[group_id] = result.trimmed.get(group_id, 0) + dropped
        # Last resort: the budget is full of called tools.  A load the model
        # asked for must still be able to land, so the least recently used
        # group goes whole, called tools included, and is named as evicted.
        for group_id in list(self._visible):
            if room >= needed:
                break
            if group_id in protect:
                continue
            room += len(self._visible.pop(group_id))
            evicted.append(group_id)
            if result is not None:
                result.trimmed.pop(group_id, None)
        return evicted

    def _describe_occupancy(self) -> str:
        """What fills the budget, for a load that could not fit.

        Names each loaded group with its visible count and how many of those
        tools the run has called (pinned), and the way out: load fewer tools
        by exact name through the ``tools`` argument, which unloads the
        uncalled tools of the least recently used group first.
        """
        held = []
        for group_id, visible in self._visible.items():
            called = sum(1 for t in visible if t.name in self._called)
            held.append(f'"{self._groups[group_id].label}" ({group_id}): {len(visible)} loaded, {called} called')
        occupied = "; ".join(held) if held else "the always-on tools only"
        return (
            f"The budget holds {len(self._always_on) + 1} always-on tool(s) and "
            f"{occupied}. Called tools are never unloaded. Load only the tools "
            f"you need by exact name with the `tools` argument of "
            f"`{LOAD_TOOLS_TOOL_NAME}` (find names with `search`)."
        )

    @property
    def called_tool_names(self) -> list[str]:
        """The gated tools the run has called, pinned against eviction."""
        return sorted(self._called)

    def set_max_visible(self, max_visible: int | None) -> tuple[list[str], list[str]]:
        """Re-cap the visible list, e.g. after a mid-run model downgrade.

        Unloads least-recently-used groups until the visible list fits the new
        cap, then — only if the core alone still leaves no room for a gated
        tool — demotes the least important core tools into the overflow group
        (``_fit_core``).  Returns ``(evicted_group_ids, demoted_core_names)``
        so the loop can tell the model what changed.  The loop's downgrade
        guard requires the fallback to hold ``required_budget``, so on that
        path the second list is always empty: a tool the model has been using
        as always-on is never demoted mid-run.

        A cap below ``MIN_GATED_BUDGET`` is refused (``ValueError``): the
        visible list always carries the meta-tool, so such a gate would put a
        request over the provider's cap — the loop must not switch a gated run
        onto that provider at all (see ``_plan_model_downgrade``).
        """
        if max_visible is not None and max_visible < MIN_GATED_BUDGET:
            raise ValueError(
                f"a tool budget of {max_visible} cannot hold the "
                f"{LOAD_TOOLS_TOOL_NAME} meta-tool plus one real tool "
                f"(minimum {MIN_GATED_BUDGET}); disable gating instead of re-capping"
            )
        self._max_visible = max_visible
        evicted: list[str] = []
        if max_visible is not None:
            # Uncalled tools go first (whole groups, or the uncalled part of a
            # group the run has called into); called tools only when the new
            # hard cap still does not fit them.
            evicted.extend(self._evict_until(0, set()))
            for group_id in list(self._visible):
                if self.visible_count <= max_visible:
                    break
                del self._visible[group_id]
                evicted.append(group_id)
        demoted = self._fit_core()
        self.catalog_prompt = self._build_catalog_prompt()
        return evicted, [tool.name for tool in demoted]

    # ── Mutation ──────────────────────────────────────────────────────

    def _resolve(self, raw: str) -> str | None:
        """Map an LLM-supplied group token to a real group id (lenient).

        Accepts the exact id, the bare provider/server name (the part after
        the colon), or the human label — case-insensitively.
        """
        if not isinstance(raw, str):
            return None
        token = raw.strip()
        if token in self._groups:
            return token
        lowered = token.lower()
        for group_id, group in self._groups.items():
            if group_id.lower() == lowered:
                return group_id
            if group_id.split(":", 1)[-1].lower() == lowered:
                return group_id
            if group.label.lower() == lowered:
                return group_id
        return None

    def _resolve_tool(self, raw: str) -> tuple[str, ToolDefinition] | None:
        """Map an LLM-supplied tool name to ``(group_id, tool)``.

        Accepts exactly the call forms the executor dispatches (exact,
        unambiguous reordered tokens, known alias — see ``ToolNameResolver``)
        and nothing more: a name the gate recognised but the executor would
        refuse would pin a tool the model cannot actually call.  The name is
        handed to the shared resolver VERBATIM — no trimming or other
        normalisation here, because the executor applies none either: a
        padded ``" jira_search_issues "`` whose token key is ambiguous is
        refused at dispatch, so the gate must not protect or pin it.

        The resolver spans the whole offered surface, so the canonical it
        returns is the very tool dispatch executes; one that is an always-on
        core tool is ``None`` here — the core is never evicted, so a call to
        it is not a recency event and there is nothing to pin.
        """
        if not isinstance(raw, str):
            return None
        canonical = self._names.canonical(raw)
        return self._tool_index.get(canonical) if canonical is not None else None

    def _is_visible(self, group_id: str, name: str) -> bool:
        return any(t.name == name for t in self._visible.get(group_id, ()))

    def _reveal_group(self, group_id: str, protect: set[str], result: RevealResult) -> None:
        group = self._groups[group_id]
        current = self._visible.get(group_id)
        if current is not None and len(current) == group.count:
            self._touch_group(group_id)
            result.already.append(group_id)
            return
        visible_names = {t.name for t in current} if current else set()
        missing = [t for t in group.ranked() if t.name not in visible_names]
        needed = len(missing)
        room = self._room()
        if room is not None and needed > room:
            result.evicted.extend(self._evict_until(needed, protect, result))
            room = self._room()
        if room is None or needed <= room:
            # Full reveal keeps the catalogue order, as before the cap existed.
            self._visible[group_id] = list(group.tools)
            self._touch_group(group_id)
            result.revealed.append(group_id)
            return
        take = missing[:room] if room > 0 else []
        if not take:
            if current is None:
                result.no_room.append(group_id)
                return
            # Already partially loaded and nothing more fits: only the recency
            # moves.  Reported as such — recording a ``partial`` here would
            # claim a change that did not happen and re-list every not-loaded
            # name on each repeated call.
            self._touch_group(group_id)
            result.at_capacity[group_id] = len(current)
            return
        self._visible[group_id] = [*(current or []), *take]
        self._touch_group(group_id)
        result.partial[group_id] = len(self._visible[group_id])

    def _is_core_name(self, raw: str) -> bool:
        """True when *raw* denotes an always-on tool (resolved like dispatch)."""
        if not isinstance(raw, str):
            return False
        canonical = self._names.canonical(raw)
        return canonical is not None and canonical not in self._tool_index

    def _reveal_tool(self, raw: str, protect: set[str], result: RevealResult) -> None:
        resolved = self._resolve_tool(raw)
        if resolved is None:
            # An always-on tool is on the wire already: nothing to load, and
            # not an unknown name either (the model can call it right now).
            if isinstance(raw, str) and raw in self._withheld:
                result.withheld_tools[raw] = self._withheld[raw]
            elif not self._is_core_name(raw):
                result.unknown_tools.append(str(raw))
            return
        group_id, tool = resolved
        if self._is_visible(group_id, tool.name):
            self._touch_group(group_id)
            return
        protect.add(group_id)
        room = self._room()
        if room is not None and room < 1:
            result.evicted.extend(self._evict_until(1, protect, result))
            room = self._room()
        if room is not None and room < 1:
            # Nothing else to unload: make room inside the tool's own group by
            # dropping its least important visible tool.  The named tool wins
            # because the model asked for it by name — the strongest signal of
            # intent the gate ever gets.
            # Tools this same call just loaded by name are never the victim —
            # ``load_tools(tools=[A, B])`` must not load A and then drop it
            # to fit B (the group-level ``protect`` cannot express that).
            current = self._visible.get(group_id)
            # A tool the run has called is never the victim either.
            candidates = [t for t in (current or []) if t.name not in result.pinned and t.name not in self._called]
            if not candidates:
                result.no_room.append(tool.name)
                return
            victim = max(candidates, key=tool_retention_key)
            current.remove(victim)
            result.displaced.append(victim.name)
        self._visible.setdefault(group_id, []).append(tool)
        self._touch_group(group_id)
        result.pinned.append(tool.name)

    def reveal(
        self,
        group_tokens: list[str],
        tool_names: list[str] | None = None,
        *,
        protect: set[str] | None = None,
    ) -> RevealResult:
        """Reveal the requested groups and/or individually named tools.

        Groups are processed first, in the order given, then tool names.
        Everything this call reveals is protected from being evicted by a
        later step of the SAME call, so ``load_tools(groups=[A, B])`` can
        never load A and immediately unload it to fit B.

        *protect* widens that guarantee to a whole model response: the loop
        seeds it with the groups whose tools the response is calling (see
        ``groups_in_use``) and passes the SAME set to every ``load_tools``
        call of the response, so the set accumulates what each reveal loaded.
        A group in active use, or loaded by an earlier meta-call of the same
        response, is then never the one evicted — the request that does not
        fit is reported as ``partial`` / ``no_room`` instead.  The set is
        mutated in place.
        """
        result = RevealResult()
        if protect is None:
            protect = set()
        # Resolve first, then reveal each group ONCE: ``groups=["notion",
        # "integration:notion"]`` (or a plain repeat) names one group twice,
        # and revealing it twice would record a partial reveal AND an
        # ``at_capacity`` no-op for the same group — a result that says both
        # "loaded N" and "nothing new was loaded".  First occurrence wins the
        # order.
        group_ids: list[str] = []
        for token in group_tokens or []:
            group_id = self._resolve(token)
            if group_id is None:
                result.unknown.append(str(token))
            elif group_id not in group_ids:
                group_ids.append(group_id)
        for group_id in group_ids:
            protect.add(group_id)
            self._reveal_group(group_id, protect, result)
        # Tool names need no such pass: ``_reveal_tool`` is idempotent — a name
        # repeated in one call (or two call forms of the same tool) finds the
        # tool already visible the second time and only refreshes recency.
        for raw in tool_names or []:
            self._reveal_tool(raw, protect, result)
        return result

    def groups_in_use(self, tool_names: list[str]) -> list[str]:
        """The VISIBLE groups the calls in *tool_names* resolve to, deduplicated.

        Names resolve like the executor dispatches them (exact, unambiguous
        reordered tokens, known alias); an ambiguous or unknown name maps to
        nothing, mirroring the executor's refusal.  Only visible groups are
        returned — a tool the model calls without having loaded its group is
        the executor's business, not a recency event.
        """
        groups: list[str] = []
        for name in tool_names:
            entry = self._resolve_tool(name)
            if entry is not None and entry[0] in self._visible and entry[0] not in groups:
                groups.append(entry[0])
        return groups

    def touch(self, tool_names: list[str]) -> None:
        """Refresh the recency of the groups the model just called into.

        Called with every batch of tool calls the loop dispatches, so LRU
        eviction never picks a group whose tools are in active use.  A
        reordered or aliased call that the executor accepts refreshes its
        group exactly like the exact name does (``groups_in_use``).
        """
        for name in tool_names:
            entry = self._resolve_tool(name)
            if entry is not None and self._is_visible(entry[0], entry[1].name):
                self._called.add(entry[1].name)
        for group_id in self.groups_in_use(tool_names):
            self._touch_group(group_id)

    def search(self, query: str, group_tokens: list[str] | None = None) -> SearchResult:
        """Find gated tools by keyword — the durable way back to a remainder.

        The names a partial reveal lists are the model's only handle on the
        rest of an oversized group, and that listing is an ordinary tool
        result: once it has aged out of the context (the loop stubs older
        results to keep the working set small) it is gone, while re-requesting
        the group is an ``at_capacity`` no-op that deliberately omits the
        names.  ``search`` makes them re-fetchable on demand, bounded
        (``_SEARCH_RESULT_LIMIT``) so recovery never re-inflates the context
        the way re-listing hundreds of names on every retry would.

        Every word of *query* (``_SEARCH_WORD_RE`` tokens) must occur
        (case-insensitive substring) in the tool's name or description — the
        same lower-casing the gate's group lookup uses.  Scope follows the
        ``load_tools`` contract literally: no *group_tokens* = every gated
        group plus the always-on core (its matches are reported as already
        loaded); *group_tokens* given = ONLY the groups that resolve, and when
        none does the search covers nothing and reports the unknown tokens —
        a misspelled group must not silently widen into a catalogue-wide
        search that returns unrelated tools.  Read-only: recency and
        visibility are untouched.

        Both lists are bounded, and the not-loaded cut honours the "most
        important first" contract: EVERY match across the searched groups is
        collected, ranked by the one retention key a partial reveal and LRU
        eviction already use, and only then sliced — so a ``priority=0`` tool
        in the last group searched is never hidden behind forty default-
        priority matches from the first.  (Sorting is stable, so within one
        rank the catalogue order — group order, then declaration order —
        still holds, exactly as in ``ToolGroup.ranked``.)
        """
        words = _SEARCH_WORD_RE.findall(query.lower())
        result = SearchResult(query=query.strip())
        for token in group_tokens or []:
            group_id = self._resolve(token)
            if group_id is None:
                result.unknown.append(str(token))
            elif group_id not in result.scope:
                result.scope.append(group_id)
        if not words:
            return result
        if group_tokens and not result.scope:
            return result  # scoped to nothing that exists: search nothing
        def _matches(tool: ToolDefinition) -> bool:
            haystack = f"{tool.name} {tool.description or ''}".lower()
            return all(word in haystack for word in words)

        not_loaded: list[tuple[ToolDefinition, str]] = []
        loaded: list[str] = []
        for group_id in result.scope or list(self._groups):
            visible = {t.name for t in self._visible.get(group_id, ())}
            for tool in self._groups[group_id].ranked():
                if not _matches(tool):
                    continue
                if tool.name in visible:
                    loaded.append(tool.name)
                else:
                    not_loaded.append((tool, group_id))
        if not result.scope:
            # The always-on core is on the wire too: an unscoped search that
            # left it out answered "No not-loaded tool matches" for a core
            # tool and nothing else, which a model read as "this tool does
            # not exist" while its schema sat in the request.  Listed after
            # the revealed groups' matches, so a core tool whose description
            # merely mentions the keyword never crowds those out of the cap.
            loaded.extend(tool.name for tool in self._always_on if _matches(tool))
        not_loaded.sort(key=lambda entry: tool_retention_key(entry[0]))
        result.not_loaded_total = len(not_loaded)
        result.not_loaded = [
            (tool.name, group_id) for tool, group_id in not_loaded[:_SEARCH_RESULT_LIMIT]
        ]
        result.loaded_total = len(loaded)
        result.loaded = loaded[:_SEARCH_LOADED_LIMIT]
        if not result.scope:
            # A withheld function has no schema to match a description
            # against: its name alone answers, so a search for it says it is
            # withheld instead of "no tool matches".
            result.withheld = {
                name: reason
                for name, reason in self._withheld.items()
                if all(word in name.lower() for word in words)
            }
        return result

    # ── Model-facing text ─────────────────────────────────────────────

    def _not_loaded(self, group_id: str) -> list[ToolDefinition]:
        visible = {t.name for t in self._visible.get(group_id, ())}
        return [t for t in self._groups[group_id].ranked() if t.name not in visible]

    def describe_search(self, result: SearchResult) -> str:
        """Build the ``search`` part of a ``load_tools`` tool-result text."""
        if not result.query:
            return "Empty `search` ignored."
        parts: list[str] = []
        if result.unknown:
            unknown = ", ".join(f'"{token}"' for token in result.unknown)
            parts.append(f"Unknown group(s) for `search`: {unknown}.")
            if not result.scope:
                # Nothing was searched — say so instead of answering the
                # question the model did not ask (a catalogue-wide search).
                parts.append(
                    "Nothing was searched: check the group id in the catalogue, "
                    "or omit `groups` to search every group."
                )
                return " ".join(parts)
        where = (
            " in " + ", ".join(f'"{self._groups[gid].label}" ({gid})' for gid in result.scope)
            if result.scope
            else ""
        )
        if result.not_loaded:
            shown = len(result.not_loaded)
            count = (
                f"{shown} of {result.not_loaded_total}"
                if result.not_loaded_total > shown
                else str(shown)
            )
            listed = ", ".join(f"{name} ({gid})" for name, gid in result.not_loaded)
            parts.append(
                f'Not-loaded tool(s) matching "{result.query}"{where} ({count}): '
                f"{listed}. To use one, call `{LOAD_TOOLS_TOOL_NAME}` with its "
                "exact name in the `tools` argument."
            )
            if result.not_loaded_total > shown:
                parts.append("Narrow the search to see the rest.")
        else:
            parts.append(f'No not-loaded tool matches "{result.query}"{where}.')
        if result.withheld:
            parts.append(describe_withheld_tools(result.withheld))
        if result.loaded:
            hidden = result.loaded_total - len(result.loaded)
            # The tail is cut, never silently: the model is told the list is a
            # sample so it does not conclude a visible tool did not match.
            more = f", +{hidden} more loaded match(es)" if hidden > 0 else ""
            parts.append(
                f"Already loaded (call directly): {', '.join(result.loaded)}{more}."
            )
        return " ".join(parts)

    def describe_reveal(self, result: RevealResult) -> str:
        """Build the ``load_tools`` tool-result text for the model."""
        parts: list[str] = []
        for group_id in result.revealed:
            group = self._groups[group_id]
            # Sample only — the full schemas are now in the visible tool list,
            # so listing every name here would re-bloat the conversation history
            # and undercut the token savings for large groups.
            parts.append(
                f'Loaded {group.count} tool(s) from "{group.label}" '
                f"({group_id}): {_sample(group.tools)}"
            )
        for group_id, visible_count in result.partial.items():
            group = self._groups[group_id]
            # EVERY not-loaded name, deliberately: ``tools=[...]`` needs the
            # exact name and a re-request of a group at capacity is a no-op.
            # The list is emitted once per partial reveal, not on retries —
            # ``search`` is the way back to a name once this result has aged
            # out of the context.
            listed = ", ".join(t.name for t in self._not_loaded(group_id))
            parts.append(
                f'Loaded {visible_count} of {group.count} tool(s) from "{group.label}" '
                f"({group_id}) — the model's tool budget is full. Not loaded: "
                f"{listed}. To use one of those, call `{LOAD_TOOLS_TOOL_NAME}` "
                "with its exact name in the `tools` argument."
            )
        for group_id, visible_count in result.at_capacity.items():
            group = self._groups[group_id]
            # Terse on purpose: the names were listed when the group was first
            # loaded partially, and repeating them here on every retry is
            # exactly the history bloat this branch exists to avoid.  That
            # listing is not durable, though (older tool results are stubbed
            # out of the context), so this retry path names the way to get
            # the exact names back: ``search``.
            parts.append(
                f'No new tools were loaded from "{group.label}" ({group_id}): '
                f"{visible_count} of {group.count} tool(s) are already loaded and "
                "the model's tool budget is full. To use one of the others, call "
                f"`{LOAD_TOOLS_TOOL_NAME}` with its exact name in the `tools` "
                "argument — if you no longer have the name, find it with the "
                "`search` argument."
            )
        if result.pinned:
            parts.append(f"Loaded tool(s) by name: {', '.join(result.pinned)}.")
        if result.evicted:
            labels = ", ".join(
                f'"{self._groups[gid].label}" ({gid})' for gid in result.evicted
            )
            parts.append(
                "Unloaded to make room (least recently used first): "
                f"{labels}. Load them again if you need them."
            )
        if result.trimmed:
            labels = ", ".join(f'"{self._groups[gid].label}" ({gid}, {count})' for gid, count in result.trimmed.items())
            parts.append(
                f"Unloaded the tools not called yet to make room: {labels}. The tools already called stay loaded."
            )
        if result.displaced:
            parts.append(
                "Unloaded from the same group to make room: "
                f"{', '.join(result.displaced)}."
            )
        if result.no_room:
            parts.append(
                f"Could not load {', '.join(result.no_room)}: the model's tool "
                f"budget ({self._max_visible}) is full and nothing else could be "
                "unloaded to make room."
            )
            parts.append(self._describe_occupancy())
        if result.unknown:
            valid = ", ".join(f'"{gid}"' for gid in self._groups)
            parts.append(
                f"Unknown group(s) ignored: {', '.join(result.unknown)}. "
                f"Valid groups are: {valid}."
            )
        if result.unknown_tools:
            parts.append(
                f"Unknown tool name(s) ignored: {', '.join(result.unknown_tools)}. "
                "Use the `search` argument to find exact names."
            )
            if self._withheld_integrations:
                # A tool an earlier turn had may belong to an integration
                # this run left out: say which and why, never just "unknown".
                parts.append(describe_withheld_integrations(self._withheld_integrations))
        if result.withheld_tools:
            parts.append(describe_withheld_tools(result.withheld_tools))
        if not result.changed:
            if not result.at_capacity and not result.no_room:
                # The at-capacity / no-room paragraphs above already say why
                # nothing moved — and a ``no_room`` request DID match, so the
                # "did not match anything" line would contradict them.  This
                # generic line covers the remaining no-op reasons only.
                parts.insert(
                    0,
                    "No new tools were loaded — the requested group(s) or tool(s) "
                    "were already loaded or did not match anything available.",
                )
            return "\n".join(parts)
        parts.append("You can now call the loaded tools directly.")
        return "\n".join(parts)


def describe_withheld_tools(withheld: Mapping[str, str]) -> str:
    """One line per reason naming the functions this turn withholds, never "unknown"."""
    by_reason: dict[str, list[str]] = {}
    for name, reason in withheld.items():
        by_reason.setdefault(reason, []).append(name)
    return " ".join(
        f"Withheld from this turn (they cannot be loaded): {', '.join(names)}, because {reason}."
        for reason, names in by_reason.items()
    )


def describe_withheld_integrations(withheld: Mapping[str, str]) -> str:
    """One line per reason naming the integrations a run left out (#11866)."""
    by_reason: dict[str, list[str]] = {}
    for label, reason in withheld.items():
        by_reason.setdefault(reason, []).append(label)
    return " ".join(
        f"Integration(s) not available in this run, so their tools cannot be loaded: "
        f"{', '.join(json.dumps(label) for label in labels)}, because {reason}."
        for reason, labels in by_reason.items()
    )


def build_tool_gate(
    tools: list[ToolDefinition],
    *,
    enabled: bool,
    min_tools: int,
    max_visible: int | None = None,
    aliases: Mapping[str, str] | None = None,
    resolver: ToolNameResolver | None = None,
    on_demand: BuiltinOnDemand | None = None,
    withheld: Mapping[str, str] | None = None,
    withheld_integrations: Mapping[str, str] | None = None,
) -> ToolGate | None:
    """Build a :class:`ToolGate` for *tools*, or ``None`` when gating is off.

    *on_demand* names the built-in groups whose functions outside an
    always-on core are gated like an integration (``BuiltinOnDemand``); they
    count toward ``min_tools`` like any gated tool.

    *withheld* (name -> reason, ``CompositeToolExecutor.withheld``) are the
    functions the agent carries that this turn leaves out on purpose:
    ``load_tools`` names them with the reason instead of "unknown".
    *withheld_integrations* (label -> reason,
    ``CompositeToolExecutor.withheld_integrations``) are the integrations the
    run left out: a requested tool that resolves to nothing names them.

    ``max_visible`` is the provider's ``max_tools`` cap; the gate keeps the
    on-the-wire list under it (see the module docstring) while the catalogue
    still lists every group.  ``None`` means unbounded.  ``resolver`` is the
    name resolver the gate shares with the loop's dispatch map — built over
    exactly *tools*' names, in the same (sanitised) name space, with the
    alias table translated into it — so a call name means the same tool to
    both.  Without one the gate builds its own over *tools*, with ``aliases``
    (``visible_aliases`` when *tools* are the sanitised definitions).

    Returns ``None`` (no gating, all tools sent up-front) when:

    * ``enabled`` is false, or
    * there are no gateable tools / groups, or
    * the number of gateable tools is below ``min_tools`` (gating would only
      add a discovery round-trip with no meaningful savings), or
    * a real tool already uses the ``load_tools`` meta-tool name (a collision
      would make the loop intercept calls meant for that tool — fall back to
      the legacy "send everything" path), or
    * ``max_visible`` is below ``MIN_GATED_BUDGET`` — the budget cannot hold
      the meta-tool plus one real tool, so a gate would be over-cap (it always
      sends ``load_tools``) and could never reveal anything; the legacy path
      truncates the wire list to the cap instead, to zero on a zero-tool
      provider.
    """
    if not enabled or not tools:
        return None
    if max_visible is not None and max_visible < MIN_GATED_BUDGET:
        return None
    if any(t.name == LOAD_TOOLS_TOOL_NAME for t in tools):
        return None
    always_on, groups = group_tools(tools, on_demand=on_demand)
    if not groups:
        return None
    gated_count = sum(group.count for group in groups.values())
    if gated_count < min_tools:
        return None
    if resolver is None and aliases is None:
        # A gate built stand-alone reads the aliases its tools declare.
        aliases = declared_aliases(tools)
    load_tools_definition = build_load_tools_definition(groups)
    return ToolGate(
        always_on,
        groups,
        load_tools_definition,
        max_visible=max_visible,
        aliases=aliases,
        resolver=resolver,
        withheld=withheld,
        withheld_integrations=withheld_integrations,
    )
