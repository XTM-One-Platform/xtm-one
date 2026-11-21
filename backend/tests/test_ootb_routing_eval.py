"""Golden-task routing eval of the out-of-the-box chat entry agents.

A simple question typed into the embedded chat of OpenCTI, OpenAEV or OpenCRQ,
or into XTM One, lands on the agent the intent catalog names first for
``global.assistant`` on that surface's vertical, then travels the seeded
handover graph to the agent that does the work. Every hop is a model turn,
tokens and seconds, so the hop count of each golden task is pinned here at its
baseline: a change of the seeded agents may lower it (and lowers the baseline
in the same pull request), never raise it.

Deterministic, no LLM: the entry agent is the catalog rule
(``api/v1/intents.py::get_intent_catalog``: vertical and universal bindings,
effective priority descending) replayed on the seeded bindings
(``seed._build_intent_bindings``), the hops are a breadth-first walk of the
seeded ``handover_type_slugs``. The live-model half (success, tokens, wall
time) runs on a lab, see ``dev-docs/ootb-routing-eval.md``.
"""

from __future__ import annotations

from collections import deque

import pytest

from app.agents.builtin_agents import BUILTIN_AGENTS
from app.agents.seed import _build_intent_bindings, seed_matches_platform

ENTRY_INTENT = "global.assistant"

#: Surface -> the vertical its chat asks the catalog with (OpenCTI
#: ``xtm-one-client.ts``: ``vertical=cti``; OpenAEV ``XtmOneClient.java``:
#: ``vertical=aev``); XTM One's own chat has no vertical.
SURFACE_VERTICAL: dict[str, str | None] = {
    "opencti": "cti",
    "openaev": "aev",
    "opencrq": "crq",
    "xtm_one": None,
}

#: (surface, golden task, agent that does the work, baseline hops from the
#: surface's entry agent). The first baseline was main at 81aefae9 (7 Nov
#: 2026, the CTEM Assistant entry of every surface); the module rows were
#: lowered by one hop when each module assistant became its chat's entry.
GOLDEN_TASKS: list[tuple[str, str, str, int]] = [
    ("opencti", "create a dashboard with widgets", "opencti-dashboard-creator", 1),
    # The widget creator is not a chat agent (disable_chat): the dashboard
    # creator holds the chat turn, plans, and spawns it once the plan is approved.
    ("opencti", "add a widget to a dashboard", "opencti-dashboard-creator", 1),
    ("opencti", "search an entity", "opencti-assistant", 0),
    ("opencti", "create an indicator or a relationship", "opencti-assistant", 0),
    ("opencti", "summarize a report", "opencti-entity-insights", 1),
    ("opencti", "entity insights", "opencti-entity-insights", 1),
    ("opencti", "threat hunt", "opencti-threat-hunt-creator", 1),
    ("openaev", "create a scenario", "openaev-scenario-creator", 1),
    ("openaev", "publish a scenario", "openaev-scenario-publisher", 1),
    ("openaev", "explain an expectation result", "openaev-assistant", 0),
    ("opencrq", "control gap analysis", "opencrq-assistant", 0),
    ("opencrq", "risk scenario", "opencrq-assistant", 0),
    ("opencrq", "assessment summary", "opencrq-assistant", 0),
    ("xtm_one", "posture question", "ctem-assistant", 0),
    ("xtm_one", "which CVEs do we fix Monday", "ctem-assistant", 0),
    ("xtm_one", "plain small talk", "ctem-assistant", 0),
    ("xtm_one", "create an OpenCTI dashboard from XTM One", "opencti-dashboard-creator", 1),
    ("xtm_one", "create an OpenAEV scenario from XTM One", "openaev-scenario-creator", 1),
    ("openaev", "a threat intelligence question asked in OpenAEV", "opencti-assistant", 1),
    ("opencrq", "an OpenCTI dashboard asked in OpenCRQ", "opencti-dashboard-creator", 2),
]

#: Chat-enabled specialists and the most hops their surface's entry agent may
#: need to reach them (baseline).
SPECIALIST_REACH: dict[str, tuple[str, int]] = {
    "opencti-assistant": ("opencti", 0),
    "opencti-dashboard-creator": ("opencti", 1),
    "opencti-threat-hunt-creator": ("opencti", 1),
    "opencti-entity-insights": ("opencti", 1),
    "fimi-analyst": ("opencti", 1),
    "openaev-assistant": ("openaev", 0),
    "openaev-scenario-creator": ("openaev", 1),
    "openaev-scenario-publisher": ("openaev", 1),
    "opencrq-assistant": ("opencrq", 0),
    "xtm-one-assistant": ("xtm_one", 1),
    "ctem-assistant": ("xtm_one", 0),
}

#: Surface -> the agent its chat opens on.
ENTRY_AGENTS: dict[str, str] = {
    "opencti": "opencti-assistant",
    "openaev": "openaev-assistant",
    "opencrq": "opencrq-assistant",
    "xtm_one": "ctem-assistant",
}


def _seeded(mode: str) -> dict[str, dict]:
    return {a["slug"]: a for a in BUILTIN_AGENTS if seed_matches_platform(a, mode)}


def entry_agent(surface: str, mode: str = "xtm_one") -> str | None:
    """The agent the catalog names first for ``global.assistant`` on *surface*."""
    agents = _seeded(mode)
    vertical = SURFACE_VERTICAL[surface]
    candidates = [
        (b["priority"], b["agent_slug"])
        for b in _build_intent_bindings()
        if b["intent_name"] == ENTRY_INTENT
        and b["agent_slug"] in agents
        and not agents[b["agent_slug"]].get("disable_chat")
        and (b["vertical"] is None or b["vertical"] == vertical)
    ]
    if not candidates:
        return None
    # Effective priority descending; the API breaks ties by row order, the
    # slug keeps this replay deterministic.
    return sorted(candidates, key=lambda c: (-c[0], c[1]))[0][1]


def hops(source: str, target: str, mode: str = "xtm_one") -> int | None:
    """Fewest handovers from *source* to *target* through the seeded graph."""
    agents = _seeded(mode)
    queue: deque[tuple[str, int]] = deque([(source, 0)])
    seen = {source}
    while queue:
        slug, depth = queue.popleft()
        if slug == target:
            return depth
        for nxt in (agents.get(slug, {}).get("handover_type_slugs") or {}):
            if nxt in agents and nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, depth + 1))
    return None


def test_every_surface_has_a_chat_entry_agent():
    for surface in SURFACE_VERTICAL:
        assert entry_agent(surface) is not None, surface


@pytest.mark.parametrize(("surface", "slug"), sorted(ENTRY_AGENTS.items()))
def test_each_surface_opens_on_its_own_assistant(surface, slug):
    """A module chat answers module questions with no transfer through a hub."""
    assert entry_agent(surface) == slug


@pytest.mark.parametrize("slug", ["opencti-assistant", "openaev-assistant", "opencrq-assistant"])
def test_ctem_assistant_stays_one_transfer_from_every_module_chat(slug):
    """Cross-module and posture questions still reach the CTEM Assistant."""
    assert hops(slug, "ctem-assistant") == 1


def test_module_assistants_keep_their_entry_bindings():
    """Never removed, disabled or moved (Sam, 7 Nov 2026)."""
    by_agent: dict[str, set[tuple[str, str | None]]] = {}
    for b in _build_intent_bindings():
        by_agent.setdefault(b["agent_slug"], set()).add((b["intent_name"], b["vertical"]))
    for slug, vertical in (
        ("opencti-assistant", "cti"),
        ("openaev-assistant", "aev"),
        ("opencrq-assistant", "crq"),
        ("fimi-analyst", "fimi"),
    ):
        assert (ENTRY_INTENT, vertical) in by_agent[slug], slug


@pytest.mark.parametrize(("surface", "task", "target", "baseline"), GOLDEN_TASKS)
def test_golden_task_routes_within_its_baseline(surface, task, target, baseline):
    entry = entry_agent(surface)
    assert entry is not None
    found = hops(entry, target)
    assert found is not None, f"{task!r}: {target} unreachable from {entry} ({surface})"
    assert found <= baseline, (
        f"{task!r} on {surface}: {found} hop(s) from {entry} to {target}, baseline {baseline}"
    )


@pytest.mark.parametrize(("slug", "surface_and_max"), sorted(SPECIALIST_REACH.items()))
def test_every_chat_specialist_is_reachable_from_its_surface(slug, surface_and_max):
    surface, most = surface_and_max
    entry = entry_agent(surface)
    found = hops(entry, slug)
    assert found is not None and found <= most, (
        f"{slug}: {found} hop(s) from {entry} ({surface}), at most {most}"
    )


def test_baseline_is_not_stale():
    """A slice that lowers a hop count lowers its baseline in the same PR."""
    stale = [
        (surface, task, hops(entry_agent(surface), target), baseline)
        for surface, task, target, baseline in GOLDEN_TASKS
        if hops(entry_agent(surface), target) < baseline
    ]
    assert not stale, f"lower the baseline of: {stale}"
