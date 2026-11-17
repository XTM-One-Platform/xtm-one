# SPDX-License-Identifier: Apache-2.0

"""The dashboards each person builds on a grid of widgets, and their bounds.

Two dashboards are built this way, each with its own widgets:

* the CTEM command center (XTM One home), one layout per CTEM vertical, in
  ``users.ctem_dashboard_layouts`` (vertical -> layout);
* the Copilot dashboard (the home of a deployment that is not XTM One), one
  layout, in ``users.copilot_dashboard_layout``.

Both are written through ``PUT /settings/preferences``. This module is the
backend's list of the widgets each layout may name and the bounds of the grid
they share. The frontend lists the same ids, with their default places, in
``CTEM_WIDGETS`` (``frontend/src/pages/xtm-one/ctem/dashboard-layout.ts``) and
``COPILOT_WIDGETS`` (``frontend/src/pages/dashboard/copilot-dashboard-layout.ts``).

A layout places its visible widgets (``items``) and names the ones the person
removed (``hidden``). A widget it names in neither (one added after the layout
was saved) is shown at its default size below the others, so a new widget
never needs a data migration.
"""

from __future__ import annotations

CTEM_DASHBOARD_WIDGETS: tuple[str, ...] = (
    "hero",
    "kpi_posture",
    "kpi_exposure_1",
    "kpi_exposure_2",
    "kpi_exposure_3",
    "kpi_agents",
    "kpi_executions",
    "briefing",
    "lifecycle_header",
    "stage_scoping",
    "stage_discovery",
    "stage_prioritization",
    "stage_validation",
    "stage_remediation",
    "stage_governance",
    "recommended_actions",
    "live_activity",
    "digests",
    "fleet_activity",
    "fleet_timeline",
    "coverage_matrix",
    "exposure_stats",
    "value_band",
    "act_now",
    "lifecycle_band",
    "stats_coverage",
    "done_for_you",
    "fleets_row",
    "prioritization_funnel",
    "cases",
)
"""Every widget of the CTEM command center (``value_band``: the v2 value band, C2 slice V1;
``act_now``: the v2 Act now queue, C2 slice V2; ``done_for_you``: the v2 timeline, C2 slice V3;
``lifecycle_band``: the v2 stage chips, C2 slice V4; ``stats_coverage``: the v2 stats and coverage, C2 slice V5;
``fleets_row``: the v2 fleets row, C2 slice V6; ``prioritization_funnel``: the compact
prioritization funnel, #11546; ``cases``: the CTEM Cases counts by state, #11545 S5a)."""

COPILOT_DASHBOARD_WIDGETS: tuple[str, ...] = (
    "hero",
    "kpi_agents",
    "kpi_conversations",
    "kpi_executions",
    "kpi_integrations",
    "kpi_documents",
    "agents",
    "conversations",
    "live_activity",
    "digests",
    "value",
    "value_drivers",
    "fleets",
)
"""Every widget of the Copilot dashboard (``value``: the ROI card of #9836 R6b;
``value_drivers``: the top value drivers card beside it, #12009; ``fleets``:
the domain-neutral Fleets widget under the Value row, #12063)."""

GRID_COLUMNS = 12
"""Columns of the grid; a widget spans 1 to ``GRID_COLUMNS`` of them."""

MAX_WIDGET_HEIGHT = 40
"""Tallest a widget may be, in grid rows."""


def max_widget_id_length(widgets: tuple[str, ...]) -> int:
    """Longest id of ``widgets``: a longer string is refused before it is looked up."""
    return max(len(widget) for widget in widgets)


def max_grid_row(widgets: tuple[str, ...]) -> int:
    """Lowest row a widget may start on: every widget stacked at its tallest fits."""
    return len(widgets) * MAX_WIDGET_HEIGHT


def check_widget_ids(ids: list[str], widgets: tuple[str, ...]) -> list[str]:
    """Every id must name one of ``widgets``, once (``ValueError`` otherwise)."""
    known = set(widgets)
    seen: set[str] = set()
    for widget in ids:
        if widget not in known:
            raise ValueError("unknown dashboard widget")
        if widget in seen:
            raise ValueError("a dashboard widget appears more than once")
        seen.add(widget)
    return ids
