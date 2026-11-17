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

"""Dashboard Studio - a chat bound to the custom dashboard it is building.

The twin of the Presentation and Video Studios (``services/presentation_studio.py``,
``services/video_studio/studio.py``): the studio page docks a conversation next
to a dashboard, and ``Conversation.custom_dashboard_id`` is what makes it a
studio. Per turn (:func:`load_dashboard_studio_context`, against the CALLING
user's own tier - ``custom_dashboards.load_dashboard_for``, no admin reach) the
chat layer derives what the agent needs to work on the dashboard without the
user saying so: which dashboard, its revision, its widgets, the widget selected
on screen and whether the user may change it (:meth:`DashboardStudioContext.prompt`);
it guarantees the dashboard tools (:attr:`DashboardStudioContext.tool_ids`), whose
``list_platform_integrations`` names the integrations a live widget can read
through when the agent needs them - not on every turn. A
dashboard since unshared or deleted drops out: the conversation goes on as an
ordinary chat. Nothing while ``DASHBOARD_STUDIO`` is off.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.custom_dashboard import CustomDashboard

#: Built-in tool ids every dashboard studio turn carries, on top of the agent's own.
STUDIO_TOOL_IDS: tuple[str, ...] = ("custom_dashboards",)

_ACCESS_NOTES = {
    True: "you may save edits",
    False: (
        "READ-ONLY - edit_dashboard will refuse to save it; describe the changes you would make, "
        "or offer a copy (build_dashboard without dashboard_id)"
    ),
}


@dataclass(frozen=True, slots=True)
class DashboardStudioContext:
    """The dashboard a conversation is bound to, as the calling user sees it."""

    dashboard: CustomDashboard
    can_edit: bool
    #: The id of the widget selected on the user's screen, when it exists.
    focused_widget: str | None = None
    tool_ids: tuple[str, ...] = STUDIO_TOOL_IDS

    def prompt(self) -> str:
        return format_dashboard_studio_prompt(self)


async def load_dashboard_studio_context(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    dashboard_id: uuid.UUID | None,
    focused_widget: object = None,
) -> DashboardStudioContext | None:
    """Resolve a conversation's dashboard binding for one turn, or ``None``.

    ``None`` when there is no binding, when the dashboard is not (or no
    longer) readable by *user_id* as themselves - the agent must not learn
    that a dashboard exists from a binding the user can no longer follow -
    and while the ``DASHBOARD_STUDIO`` feature flag is off.
    """
    from app.core.feature_flags import is_feature_enabled

    if dashboard_id is None or not is_feature_enabled("DASHBOARD_STUDIO"):
        return None
    from app.services.custom_dashboards import EDIT_TIERS, load_dashboard_for

    found = await load_dashboard_for(db, dashboard_id, user_id=user_id)
    if found is None:
        return None
    dashboard, tier = found
    from app.services.dashboard_definition import upgrade

    widget_ids = set(upgrade(dashboard.spec).get("visualizations") or {})
    focused = focused_widget if isinstance(focused_widget, str) and focused_widget in widget_ids else None
    return DashboardStudioContext(dashboard=dashboard, can_edit=tier in EDIT_TIERS, focused_widget=focused)


def _where(position: dict) -> tuple[tuple[int, int], str]:
    """Where an entry sits, to sort by and to say: a box ``x,y wxh``, a line ``from -> to`` (a point or ``item.port``)."""
    if "from" in position:
        ends = [position["from"], position["to"]]
        said = " -> ".join(f"{e['item']}.{e['port']}" if "item" in e else f"{e['x']},{e['y']}" for e in ends)
        point = next((e for e in ends if "x" in e), {"x": 0, "y": 0})
        return (point["y"], point["x"]), said
    return (position["y"], position["x"]), f"{position['x']},{position['y']} {position['w']}x{position['h']}"


def outline(spec: dict) -> str:
    """The definition as the agent reads it: tabs, their visualizations and places, data sources, inputs."""
    from app.services.dashboard_definition import upgrade

    definition = upgrade(spec)
    sources = definition.get("dataSources") or {}
    visualizations = definition.get("visualizations") or {}
    tabs = definition["layout"]["tabs"]["items"]
    layouts = definition["layout"]["layoutDefinitions"]
    lines = []
    for tab in tabs:
        layout = layouts.get(tab["layoutId"]) or {}
        if len(tabs) > 1:
            lines.append(f"  Tab {tab['layoutId']} \"{tab['label']}\" ({layout.get('type', 'grid')}):")
        entries = sorted(layout.get("structure") or [], key=lambda e: _where(e["position"])[0])
        for entry in entries:
            place = _where(entry["position"])[1]
            if entry.get("type") == "input":
                lines.append(f"  - {entry['item']} [input, {place}]")
                continue
            viz = visualizations.get(entry["item"]) or {}
            kind = viz.get("type", "?")
            sid = (viz.get("dataSources") or {}).get("primary")
            if sid:
                source = sources.get(sid) or {}
                origin = (source.get("options") or {}).get("source") or source.get("type")
                kind = f"{kind} <- {sid} ({origin})"
            heading = viz.get("title") or (viz.get("options") or {}).get("label") or ""
            lines.append(f"  - {entry['item']} [{kind}, {place}] {heading}".rstrip())
    inputs = definition.get("inputs") or {}
    if inputs:
        lines.append("  Inputs: " + ", ".join(f"{iid} ({i.get('type')}, ${i.get('token')}$)" for iid, i in inputs.items()))
    return "\n".join(lines) or "  (empty)"


def format_dashboard_studio_prompt(ctx: DashboardStudioContext) -> str:
    """The system-prompt block for a dashboard studio turn: identity, access, the widget in view, the outline.

    Routing only - what each dashboard tool guarantees is the tool's own
    description (invariant 16).
    """
    dashboard = ctx.dashboard
    spec = dashboard.spec if isinstance(dashboard.spec, dict) else {}
    lines = [
        "\n[Dashboard Studio]",
        (
            f'The user has the dashboard "{dashboard.title}" open next to this chat and is building it with you. '
            f"dashboard_id: {dashboard.id} - {dashboard.widget_count} widgets, revision {dashboard.revision}. "
            "Their page refreshes the moment you save."
        ),
        f"- Access: {_ACCESS_NOTES[ctx.can_edit]}.",
        (
            f"- Every change goes to THIS dashboard: edit_dashboard with dashboard_id={dashboard.id} (one widget "
            "at a time). Never build a new dashboard unless the user asks for a copy."
        ),
    ]
    if ctx.focused_widget is not None:
        lines.append(
            f'- The user has selected widget {ctx.focused_widget}: "this widget", "here" or "it" means that one '
            "unless they say otherwise."
        )
    lines.append("Current widgets (id [type, x,y wxh on the 12-column grid] heading):\n" + outline(spec))
    return "\n".join(lines)


__all__ = ["STUDIO_TOOL_IDS", "DashboardStudioContext", "load_dashboard_studio_context"]
