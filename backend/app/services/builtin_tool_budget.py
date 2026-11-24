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

"""The built-in tool schema budget: what each group costs on the wire against its ceiling.

Every built-in group's function schemas (name, description, input schema, as
``builtin_tool_groups.schema_chars`` counts them) ride in the tool list of a
generation, so their total is capped: ``TOTAL_SCHEMA_CHARS_CEILING`` for all
groups together, ``GROUP_SCHEMA_CHARS_CEILING`` for the groups that carry a
ceiling of their own. ``tests/test_builtin_tool_groups.py`` asserts them.

This module answers the question a change adding tool arguments has before the
test runs: per group, its size, its ceiling, the headroom left and the delta
against a baseline measured on the base checkout, plus the functions that grew
most. ``format_breakdown`` then says where one group's weight is: its functions
by size with their largest arguments, and the arguments whose schema is the
same text in several of its functions, so a group at its ceiling is trimmed in
one round. ``scripts/tool_schema_budget.py`` prints it (the ``backend-schema-budget``
check of ``.xtm-one/checks.toml``); the size test prints the same table when it
fails. Read-only: nothing here changes a ceiling.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.builtin_tool_groups import schema_chars

#: What every built-in group costs on the wire together, measured on 3 Nov 2026
#: (441,087 characters; agent_builder 120,572, generate_presentation 99,908,
#: video_studio 94,264) plus headroom. Growing past it is a deliberate change:
#: raise the figure in the PR that adds the weight, and say why. Raised to
#: 472,000 for the mobile companion's ``phone`` group (953 characters, measured
#: on 5 Nov 2026), which tipped the total over 470,000; then to 479,000 for
#: the ``custom_dashboards`` group (6,112 characters, measured the same day),
#: which took the total to 477,436. Lowered to 435,000 when the deck catalogue
#: left the generate_presentation description for get_presentation(reference=)
#: (#8812: generate_presentation 101,282 -> 56,483, total 478,013 -> 433,214).
#: Raised to 454,000 for Platform Health's tools (total 452,062 on 5 Nov 2026).
#: Lowered to 449,000 when the platform and coordination descriptions were
#: trimmed (#8812: total 447,765 on 5 Nov 2026). Lowered to 441,000 when the
#: generate_video_clip description sent its checks' detail to the Video
#: Production skill (#8812: video_studio 96,619 -> 88,824, total 439,975).
#: Raised to 443,500 for the six ROI tools of the platform group (#10006 S2:
#: +2,233 characters, total 441,225 on 8 Nov 2026). Raised to 444,500 for the
#: live poll tool of the generate_presentation group (#10157: presentation_poll,
#: total 443,767 on 8 Nov 2026). Raised to 445,000 for the update_skill
#: content modes on update_prompt (mode, section, edits, confirm_shrink).
#: Raised to 446,000 for the two chat ROI tools of the platform group (#10544
#: S4: get_roi_chat_series and adjust_roi_chat_line, +627 characters, total
#: 445,569 on 10 Nov 2026). Raised to 447,700 for the two program tools of
#: the coordination group (#10833: list_programs and get_program_status,
#: +1,532 characters, total 447,532 on 10 Nov 2026). Raised to 448,400 for the
#: span search of the two trace listing tools (span_tool and span_match, shared
#: by the observability and platform groups, total 448,349 on 10 Nov 2026).
#: Raised to 448,600 for the browser tool's ``record`` action and its ``seconds`` argument (#11114: web +177,
#: total 448,557 on 11 Nov 2026). Raised to 449,200 for the verdict release of
#: blocked_on (#11214: complete_work_item verdict, blocked_on_release_on on create and
#: update, total 449,101 on 11 Nov 2026). Raised to 449,500 for get_fleet_setup, the fleet setup
#: interview (#11250: platform +~200, total 449,397 on 11 Nov 2026). Raised to 449,800 for the
#: organization context cascade (#11533: get_organization_context category/query/offset/limit and
#: organization_context_mode/_categories on the agent and assignment tools, total 449,539 on 13 Nov 2026).
#: Raised to 451,600 for the organization_context write group (#11594: update_organization_context
#: and its proposal tool, 1,713, opt-in and administrator-only, total 451,345 on 13 Nov 2026).
#: Raised to 452,000 for the shared state lifecycle arguments (#11696: lease, work_item_id,
#: expires_at, terminal_condition on set_shared_state, coordination +349, total 451,923 on
#: 13 Nov 2026).
#: Raised to 452,400 for get_fleet_health, the fleet health findings every orchestrator reads
#: (#11700: platform +~340, total about 452,270 on 14 Nov 2026).
#: Raised to 452,600 for take_over on update_work_item and complete_work_item (#11740:
#: coordination +256, total 452,515 on 14 Nov 2026).
#: Raised to 453,000 for patch mode on stix_bundle_validator (#11825: patch and
#: strict_input, cti +~370, total 452,888 on 14 Nov 2026).
#: Raised to 454,300 for the intent-binding tools (#11865: platform +~1,150, total
#: 454,116 on 14 Nov 2026).
#: Raised to 455,500 for the three ROI unit catalog tools (#11881: create_roi_unit,
#: update_roi_unit, archive_roi_unit, admin only, platform +~1,120 on 14 Nov 2026).
#: Raised to 458,000 for the row and import tools of the organization_context group (#11888:
#: upsert_organization_rows, remove_organization_rows, run_organization_import, +2,242, opt-in and
#: administrator-only, on 14 Nov 2026). Raised to 458,300 for the recipient of a needs_human
#: item (#11893: coordination +~250 on 14 Nov 2026). Raised to 459,400 for the sync tools of the
#: organization_context group (#11888: list_organization_syncs, create_organization_sync,
#: run_organization_sync, +~1,300, opt-in and administrator-only, total 459,222 on 14 Nov 2026).
#: Raised to 460,000 for the shared-context clause of the organization_context tools (#11994: seven
#: descriptions +~110 each, opt-in and administrator-only, on 15 Nov 2026).
#: Raised to 460,400 for dismiss_fleet_health_findings (#12014: platform +429, total
#: 460,266 on 15 Nov 2026). Raised to 460,700 for group_by on get_shared_state and
#: list_work_items (#12105: coordination +~300 on 15 Nov 2026). Raised to 461,000 when the
#: catalog counted workset_resolve, offered on every implicit corpus since #12168 (#12218:
#: coordination +279, total 460,976 on 16 Nov 2026).
#: Raised to 461,700 for ``extract_stix_from_document``, the ``stix_extractor`` group's grounded
#: extraction (#9126: +629, total 461,605 on 16 Nov 2026).
#: Raised to 461,800 for list_shared_state's key_prefix (coordination +~80 on 18 Nov 2026).
#: Raised to 466,800 for the user_admin group (#250: eleven tools, 4,873, opt-in and
#: administrator-only, total 466,669 on 18 Nov 2026).
#: Raised to 467,400 when Platform Health became built in and its eight groups joined the ids
#: create_agent, update_agent and clone_agent advertise (#101: platform +474,
#: health_orchestrator +20, on top of user_admin, 19 Nov 2026).
#: Raised to 468,300 for list_organization_proposals and decide_organization_proposal (#475:
#: organization_context +~830, opt-in and administrator-only, 19 Nov 2026).
#: Raised to 469,700 for the knowledge base sync-source tools (#469: platform +~1,400,
#: on top of #475, 19 Nov 2026).
#: Raised to 470,300 for get_user_default_agents (#529: platform_insights +520,
#: 19 Nov 2026).
#: Raised to 470,900 for the readable decision card ask (#776: coordination +579,
#: 20 Nov 2026).
#: Raised to 472,300 for the health_modules group (#101 S14a-2b: list_module_findings and
#: record_finding_recommendation, +1,300, carried by Platform Health's Module Analyst alone,
#: total 472,200 on 21 Nov 2026).
#: Raised to 472,950 for send_decision_card in the coordination group (#1156: +621,
#: total 472,914 on 22 Nov 2026).
#: Raised to 473,900 for record_module_check_report in the health_modules group (#101 S15c:
#: about +900, carried by Platform Health's three module checkers alone, 22 Nov 2026).
#: Raised to 475,400 for the ctem_read group (#1337: three read-only CTEM functions, +1,454,
#: opt-in on any agent, and +33 on the agent builder's group list, total 475,367 on 23 Nov 2026).
#: Raised to 477,000 for the profile versions, restore and OpenCRQ reconcile tools of the
#: organization_context group (#1676: +1,528, opt-in and administrator-only, total 476,895 on 24 Nov 2026).
#: Raised to 478,000 for the ctem_repair group (#1778: three CTEM administrator repairs, +977,
#: and +13 on the agent builder's group list, total 477,872 on 24 Nov 2026).
TOTAL_SCHEMA_CHARS_CEILING = 478_000

#: Ceilings of the groups that carry one of their own (group id -> characters);
#: generate_presentation, platform and coordination since the chat-reactivity
#: program cut them (#8812: platform 138,567, coordination 34,394,
#: video_studio 88,824).
GROUP_SCHEMA_CHARS_CEILING: dict[str, int] = {
    # 58,000 with presentation_poll (#10157: 57,548 on 8 Nov 2026).
    "generate_presentation": 58_000,
    # 141,000 with the ROI tools (#10006 S2: 139,857 on 8 Nov 2026); 142,000
    # with the chat ROI tools (#10544 S4: 141,502 on 10 Nov 2026); 142,300 with
    # get_fleet_setup (#11250: 142,113 on 11 Nov 2026); 142,800 with the organization
    # context cascade fields (#11533: 142,545 on 13 Nov 2026); 143,100 with
    # get_fleet_health (#11700: 142,940 on 14 Nov 2026); 144,400 with the three
    # intent-binding tools (#11865: 144,249 on 14 Nov 2026); 145,600 with the
    # three ROI unit catalog tools (#11881: about 145,370 on 14 Nov 2026); 145,700 with
    # dismiss_fleet_health_findings (#12014: 145,602 on 15 Nov 2026); 146,000 with
    # get_trace_payload's include_reasoning (#12356: +227, on a main already at 146,033);
    # 146,100 with Platform Health built in, its groups advertised by the agent builder
    # (#101: +158 on each of create_agent, update_agent and clone_agent, 146,023 on 18 Nov 2026);
    # 147,600 with the three knowledge base sync-source tools (#469: 147,482 on 19 Nov 2026);
    # 147,650 with the ctem_read group in the agent builder's group list (#1337: 147,614 on 23 Nov 2026);
    # 147,700 with the ctem_repair group in that list (#1778: 147,663 on 24 Nov 2026).
    "platform": 147_700,
    # 35,700 with list_programs and get_program_status (#10833: 35,547 on
    # 10 Nov 2026); 36,200 with the verdict release of blocked_on (#11214: 36,186 on
    # 11 Nov 2026); 36,600 with the shared state lifecycle arguments (#11696:
    # 36,549 on 13 Nov 2026); 36,900 with take_over on update_work_item and
    # complete_work_item (#11740: 36,805 on 14 Nov 2026); 37,200 with the
    # recipient of a needs_human item (#11893: 37,148 on 14 Nov 2026); 37,500 with
    # group_by on get_shared_state and list_work_items (#12105, 15 Nov 2026); 37,800 with
    # workset_resolve counted (#12218: 37,779 on 16 Nov 2026); 37,900 with
    # list_shared_state's key_prefix (about 37,870 on 18 Nov 2026); 38,500 with the
    # readable decision card ask (#776: 38,479 on 20 Nov 2026); 39,150 with
    # send_decision_card (#1156: 39,107 on 22 Nov 2026).
    "coordination": 39_150,
    "video_studio": 89_500,
}

#: Where the readout keeps the baseline it measures the delta against,
#: relative to ``backend/`` (``.xtm/`` holds the sandbox checks' outputs).
DEFAULT_BASELINE = Path(".xtm") / "tool-schema-baseline.json"

#: Sizes per group, then per function: ``{group id: {function name: chars}}``.
SchemaSizes = dict[str, dict[str, int]]

#: Function definitions per group, as the catalog builds them: ``{group id: [definition]}``.
GroupDefinitions = dict[str, list[dict[str, Any]]]

#: Below this size a repeated argument (a ``limit``, an ``offset``) is not worth naming.
REPEATED_ARGUMENT_MIN_CHARS = 100


def measure_definitions() -> GroupDefinitions:
    """Every built-in group's function definitions, every flag on."""
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    return build_builtin_tool_catalog(include_disabled=True)


def measure_schema_sizes(catalog: GroupDefinitions | None = None) -> SchemaSizes:
    """Every built-in group's functions with what each costs on the wire, every flag on."""
    if catalog is None:
        catalog = measure_definitions()
    return {
        group_id: {definition["name"]: schema_chars(definition) for definition in definitions}
        for group_id, definitions in catalog.items()
    }


@dataclass(frozen=True)
class GroupBudget:
    """One group's size against its ceiling, and its change against the baseline."""

    group: str
    size: int
    ceiling: int | None
    delta: int | None

    @property
    def headroom(self) -> int | None:
        return None if self.ceiling is None else self.ceiling - self.size


@dataclass(frozen=True)
class SchemaBudget:
    """The whole budget: every group, the total, and the functions that grew most."""

    groups: list[GroupBudget]
    total_ceiling: int
    has_baseline: bool
    grown: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(group.size for group in self.groups)

    @property
    def headroom(self) -> int:
        return self.total_ceiling - self.total

    @property
    def total_delta(self) -> int | None:
        if not self.has_baseline:
            return None
        return sum(group.delta or 0 for group in self.groups)

    @property
    def over(self) -> list[str]:
        """What is past its ceiling: group ids, and ``total`` for the sum."""
        names = [group.group for group in self.groups if group.headroom is not None and group.headroom < 0]
        if self.headroom < 0:
            names.append("total")
        return names


def compute_budget(
    sizes: SchemaSizes,
    baseline: SchemaSizes | None = None,
    *,
    total_ceiling: int = TOTAL_SCHEMA_CHARS_CEILING,
    group_ceilings: dict[str, int] | None = None,
) -> SchemaBudget:
    """*sizes* against the ceilings, with the delta against *baseline* when given.

    A group or function absent from the baseline counts whole as growth; one
    the baseline has and *sizes* lacks counts as shrinkage (negative delta).
    """
    ceilings = GROUP_SCHEMA_CHARS_CEILING if group_ceilings is None else group_ceilings
    group_ids = list(sizes) + [group for group in (baseline or {}) if group not in sizes]
    groups: list[GroupBudget] = []
    grown: list[tuple[str, str, int]] = []
    for group_id in group_ids:
        functions = sizes.get(group_id, {})
        size = sum(functions.values())
        delta: int | None = None
        if baseline is not None:
            before = baseline.get(group_id, {})
            delta = size - sum(before.values())
            for name in list(functions) + [n for n in before if n not in functions]:
                change = functions.get(name, 0) - before.get(name, 0)
                if change > 0:
                    grown.append((group_id, name, change))
        groups.append(GroupBudget(group=group_id, size=size, ceiling=ceilings.get(group_id), delta=delta))
    groups.sort(key=lambda g: (-(g.delta or 0), -g.size))
    grown.sort(key=lambda item: -item[2])
    return SchemaBudget(groups=groups, total_ceiling=total_ceiling, has_baseline=baseline is not None, grown=grown)


def _signed(value: int | None) -> str:
    return "-" if value is None else f"{value:+,}"


def _plain(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"


def format_budget(budget: SchemaBudget, *, top: int = 5) -> str:
    """The budget as a plain-text table, the groups that changed first."""
    rows = [("group", "size", "ceiling", "headroom", "delta")]
    rows += [(g.group, _plain(g.size), _plain(g.ceiling), _plain(g.headroom), _signed(g.delta)) for g in budget.groups]
    rows.append(
        (
            "TOTAL",
            _plain(budget.total),
            _plain(budget.total_ceiling),
            _plain(budget.headroom),
            _signed(budget.total_delta),
        )
    )
    widths = [max(len(row[i]) for row in rows) for i in range(5)]
    lines = [
        "  ".join(cell.ljust(widths[i]) if i == 0 else cell.rjust(widths[i]) for i, cell in enumerate(row))
        for row in rows
    ]
    if budget.over:
        lines.append(f"OVER THE CEILING: {', '.join(budget.over)}")
    if not budget.has_baseline:
        lines.append("delta: no baseline (save one on the base checkout with --save-baseline)")
    elif budget.grown:
        lines.append("grew most:")
        lines += [f"  {group}.{name} {change:+,}" for group, name, change in budget.grown[:top]]
    else:
        lines.append("grew most: nothing grew against the baseline")
    return "\n".join(lines)


def save_baseline(sizes: SchemaSizes, path: Path) -> None:
    """Write *sizes* as the baseline later readouts measure their delta against."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sizes, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def load_baseline(path: Path) -> SchemaSizes | None:
    """The baseline at *path*, or None when there is none."""
    if not path.is_file():
        return None
    stored: SchemaSizes = json.loads(path.read_text(encoding="utf-8"))
    return stored


@dataclass(frozen=True)
class FunctionBreakdown:
    """One function's cost on the wire: its total, its description, its arguments largest first."""

    name: str
    size: int
    description: int
    arguments: list[tuple[str, int]]


def _properties(definition: dict[str, Any]) -> dict[str, Any]:
    properties = (definition.get("input_schema") or {}).get("properties")
    return properties if isinstance(properties, dict) else {}


def _compact(schema: Any, *, sort_keys: bool = False) -> str:
    return json.dumps(schema, separators=(",", ":"), ensure_ascii=False, sort_keys=sort_keys)


def breakdown_definitions(definitions: list[dict[str, Any]]) -> list[FunctionBreakdown]:
    """Each function of *definitions*, largest first, with its arguments by the size of their schema."""
    rows = [
        FunctionBreakdown(
            name=definition["name"],
            size=schema_chars(definition),
            description=len(definition["description"]),
            arguments=sorted(
                ((name, len(_compact(schema))) for name, schema in _properties(definition).items()),
                key=lambda item: -item[1],
            ),
        )
        for definition in definitions
    ]
    rows.sort(key=lambda row: -row.size)
    return rows


def repeated_arguments(
    definitions: list[dict[str, Any]], *, min_chars: int = REPEATED_ARGUMENT_MIN_CHARS
) -> list[tuple[str, int, list[str]]]:
    """Arguments whose schema is the same text in several functions: ``(name, chars, functions)``.

    The text is paid once per function on the wire; the ones that cost the most
    beyond their first copy come first, and those under *min_chars* are left out.
    """
    seen: dict[tuple[str, str], list[str]] = {}
    for definition in definitions:
        for name, schema in _properties(definition).items():
            seen.setdefault((name, _compact(schema, sort_keys=True)), []).append(definition["name"])
    repeated = [
        (name, len(text), functions)
        for (name, text), functions in seen.items()
        if len(functions) > 1 and len(text) >= min_chars
    ]
    repeated.sort(key=lambda item: (-(item[1] * (len(item[2]) - 1)), item[0]))
    return repeated


def format_breakdown(
    group_id: str,
    definitions: list[dict[str, Any]],
    *,
    ceiling: int | None = None,
    top: int | None = None,
    arguments: int = 3,
) -> str:
    """Where *group_id*'s weight is: its functions by size, then its repeated arguments.

    *top* bounds the functions listed (all when None); *arguments* the largest
    arguments named per function.
    """
    rows = breakdown_definitions(definitions)
    size = sum(row.size for row in rows)
    head = f"{group_id}: {size:,} chars in {len(rows)} functions"
    if ceiling is not None:
        head += f", ceiling {ceiling:,}, headroom {ceiling - size:,}"
    lines = [head]
    shown = rows if top is None else rows[:top]
    width = max((len(row.name) for row in shown), default=0)
    for row in shown:
        largest = ", ".join(f"{name} {chars:,}" for name, chars in row.arguments[:arguments])
        line = f"  {row.name.ljust(width)}  {row.size:>7,}  description {row.description:,}"
        lines.append(f"{line}  arguments: {largest}" if largest else line)
    if len(shown) < len(rows):
        lines.append(f"  ... {len(rows) - len(shown)} more (--group {group_id} lists them all)")
    repeated = repeated_arguments(definitions)
    if repeated:
        lines.append("repeated arguments (the same schema paid in each function):")
        lines += [
            f"  {name} {chars:,} x{len(functions)}: {', '.join(functions)}" for name, chars, functions in repeated
        ]
    return "\n".join(lines)


__all__ = [
    "DEFAULT_BASELINE",
    "GROUP_SCHEMA_CHARS_CEILING",
    "REPEATED_ARGUMENT_MIN_CHARS",
    "TOTAL_SCHEMA_CHARS_CEILING",
    "FunctionBreakdown",
    "GroupBudget",
    "GroupDefinitions",
    "SchemaBudget",
    "SchemaSizes",
    "breakdown_definitions",
    "compute_budget",
    "format_breakdown",
    "format_budget",
    "load_baseline",
    "measure_definitions",
    "measure_schema_sizes",
    "repeated_arguments",
    "save_baseline",
]
