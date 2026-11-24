"""The seeded fleet skills cite only mechanisms the code has, and cover the coordination ones (#1050).

"Fleet design and operations" and "Fleet health advisory" teach every fleet
orchestrator how to coordinate with the platform's own mechanisms. A tool, an
event, a trigger type, a status or a field they cite that the code no longer
has reads as an instruction the agent cannot follow, so each backticked name
is checked against the code: tools against the built-in tool definitions,
dotted names against the platform event catalog, the rest against the
vocabularies and string literals of ``backend/app``.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pytest

from app.agents.builtin_agents import BUILTIN_SKILLS
from app.agents.seed import _resolve_seed_content

_APP = Path(__file__).resolve().parents[1] / "app"
_SKILLS = ("fleet-design-practices", "fleet-health-advisory")

#: A backticked name, optionally called with arguments: how the skills cite code.
_CITED = re.compile(r"`([a-z_][a-z0-9_.]*)(?:\([^`]*\))?`")
#: A name led by a verb is a tool (a list-op field ending in ``_json`` is not).
_TOOL_SHAPED = re.compile(
    r"^(?:get|list|set|create|complete|claim|delegate|cancel|reschedule|schedule|arm|watch|"
    r"record|dispose|resolve|delete|consult|spawn|dismiss|workset)_[a-z0-9_]+$"
)
#: Dotted payload paths a filter reads, not event names.
_PAYLOAD_PATHS = {"fleet_impact.targets"}


def _content(slug: str) -> str:
    entry = next(s for s in BUILTIN_SKILLS if s["slug"] == slug)
    content = _resolve_seed_content("skill_content", entry["content"])
    assert content is not None, slug
    return content


@cache
def _tool_names() -> frozenset[str]:
    from app.services.builtin_tool_aliases import BUILTIN_GROUP_ALIASES
    from app.services.builtin_tools.handover import HANDOVER_TOOL_NAMES

    names = {fn for _, functions in BUILTIN_GROUP_ALIASES.values() for fn in functions}
    names |= set(HANDOVER_TOOL_NAMES.values())
    pattern = re.compile(r'(?:name=|[A-Z_]*TOOL = |register\()"([a-z][a-z0-9_]+)"')
    for path in (_APP / "services" / "builtin_tools").glob("*.py"):
        names |= set(pattern.findall(path.read_text(encoding="utf-8")))
    return frozenset(names)


@cache
def _string_literals() -> frozenset[str]:
    literal = re.compile(r"""["']([a-z_][a-z0-9_]*)["']""")
    found: set[str] = set()
    for path in _APP.rglob("*.py"):
        found |= set(literal.findall(path.read_text(encoding="utf-8")))
    return frozenset(found)


@cache
def _vocabulary() -> frozenset[str]:
    from app.models.work_item import WORK_ITEM_STATUSES
    from app.services.builtin_tools.agent_builder import ASSIGNMENT_TRIGGER_TYPES
    from app.services.fleet_health_signals import FINDING_FIX
    from app.services.work_item import BLOCKED_ON_RELEASE_ON
    from app.services.work_item_blocked_refs import REF_KINDS, RELEASE_ON

    return frozenset(
        {
            *WORK_ITEM_STATUSES,
            *ASSIGNMENT_TRIGGER_TYPES,
            *REF_KINDS,
            *RELEASE_ON,
            *BLOCKED_ON_RELEASE_ON,
            *FINDING_FIX,
        }
    )


@pytest.mark.parametrize("slug", _SKILLS)
def test_every_name_a_fleet_skill_cites_exists_in_the_code(slug: str) -> None:
    from app.services.platform_events import PLATFORM_EVENTS

    missing: list[str] = []
    for name in sorted(set(_CITED.findall(_content(slug)))):
        if "." in name:
            ok = name in PLATFORM_EVENTS or name in _PAYLOAD_PATHS
        elif _TOOL_SHAPED.match(name) and not name.endswith("_json"):
            ok = name in _tool_names()
        else:
            ok = name in _vocabulary() or name in _tool_names() or name in _string_literals()
        if not ok:
            missing.append(name)
    assert not missing, f"{slug} cites names the code does not have: {missing}"


def test_the_design_skill_covers_every_coordination_mechanism() -> None:
    content = _content("fleet-design-practices")
    for name in (
        # work items and the blocked rule
        "blocked_on",
        "blocked_on_refs",
        "blocked_on_release_on",
        "needs_human",
        "not_before",
        "correlation_prefix",
        "priority_min",
        "max_concurrent_runs",
        # triggers and events
        "work_item",
        "platform_event",
        "integration_event",
        "list_platform_events",
        "list_integration_catalog",
        "watch_platform_event",
        "activation_check",
        "work_item.completed",
        "work_item.released",
        "work_item.wait_cycle",
        "work_item.release_exhausted",
        "run.orphaned",
        "run.failed",
        "platform.release.deployed",
        "fleet_impact",
        # timers and one-off work
        "arm_timer",
        "unless_work_item_id",
        "unless_shared_state_key",
        "timer.due",
        "schedule_work_item",
        # shared state
        "hold",
        "append_json",
        "update_json",
        "unique_by_json",
        "keep_last_json",
        "terminal_condition",
        # worksets
        "workset_add_item",
        "workset_digest",
        # fleet operations
        "set_fleet_automations",
        "get_fleet_health",
    ):
        assert f"`{name}`" in content, name
    for kind in ("pull_request", "issue", "deployment", "workflow_run"):
        assert f"`{kind}`" in content, kind
    assert "Graph" in content


@pytest.mark.parametrize("slug", _SKILLS)
def test_no_fleet_skill_tells_the_agent_to_poll_where_an_event_exists(slug: str) -> None:
    content = _content(slug)
    assert "safety net" not in content or "not a safety net" in content
    assert not re.search(r"\b\d+ days?\b.*\b(re-?send|repeat|again)\b", content, re.I)
    assert "not answered for 7 days" not in content
