"""The Copilot dashboard each person builds (``users.copilot_dashboard_layout``).

A per-user preference written whole through ``PUT /settings/preferences``: the
request model accepts the Copilot dashboard's widgets only, each once, placed
inside the grid (anything else is a 422, never a silent drop); an explicit
``null`` resets the default layout, and omitting the field leaves it alone.
The CTEM command center's widgets are not the Copilot dashboard's.

The route coroutine is driven directly, like ``test_ctem_dashboard_layout.py``;
``test_copilot_dashboard_layout_postgres.py`` covers the real routes.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import app.api.v1.settings as settings_api
from app.api.v1.schemas.settings import UserPreferencesRequest
from app.services.dashboard_layouts import (
    COPILOT_DASHBOARD_WIDGETS,
    CTEM_DASHBOARD_WIDGETS,
    GRID_COLUMNS,
    MAX_WIDGET_HEIGHT,
    max_grid_row,
)

MAX_GRID_ROW = max_grid_row(COPILOT_DASHBOARD_WIDGETS)


class _FakeDB:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        raise AssertionError("the Copilot layout is written whole: nothing to read")

    async def flush(self):
        return None

    async def commit(self):
        return None


def _user(**overrides):
    base = dict(
        id=uuid.uuid4(),
        email="u@example.com",
        full_name="Test User",
        title=None,
        preferred_llm_model=None,
        ctem_vertical="cyber",
        ctem_dashboard_layouts=None,
        copilot_dashboard_layout=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _update(monkeypatch, user, body: UserPreferencesRequest):
    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_api, "log_audit", _no_audit)
    return await settings_api.update_preferences(
        body=body,
        request=SimpleNamespace(headers={}, client=None),
        user=user,
        db=_FakeDB(),
    )


def _item(**overrides):
    return {"i": "hero", "x": 0, "y": 0, "w": 12, "h": 7, **overrides}


LAYOUT = {
    "items": [
        {"i": "live_activity", "x": 0, "y": 0, "w": 12, "h": 10},
        {"i": "hero", "x": 0, "y": 10, "w": 8, "h": 7},
        {"i": "kpi_agents", "x": 8, "y": 10, "w": 4, "h": 4},
    ],
    "hidden": ["digests", "kpi_documents"],
}


def test_the_widgets_are_the_thirteen_of_the_copilot_dashboard():
    # The Value card (#9836 R6b), the top value drivers beside it (#12009) and the Fleets widget (#12063).
    assert len(COPILOT_DASHBOARD_WIDGETS) == 13
    assert len(set(COPILOT_DASHBOARD_WIDGETS)) == 13
    assert COPILOT_DASHBOARD_WIDGETS[-1] == "fleets"


def test_the_widgets_are_the_ones_the_copilot_dashboard_draws():
    """The frontend sends every widget it draws in each layout it saves, so a
    widget on one side only would turn every save into a 422: the backend list
    is pinned against ``COPILOT_WIDGETS``, as the command center's is against
    ``CTEM_WIDGETS`` (``test_ctem_dashboard_layout.py``)."""
    source = (
        Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "dashboard" / "copilot-dashboard-layout.ts"
    ).read_text(encoding="utf-8")
    block = re.search(
        r"export const COPILOT_WIDGETS: Record<CopilotWidgetId, WidgetSpec> = \{(.*?)\n\};",
        source,
        re.DOTALL,
    )
    assert block, "COPILOT_WIDGETS not found in copilot-dashboard-layout.ts"
    frontend_ids = re.findall(r"^  (\w+):", block.group(1), re.MULTILINE)
    assert frontend_ids == list(COPILOT_DASHBOARD_WIDGETS)


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param(LAYOUT, id="partial"),
        pytest.param({"items": [], "hidden": list(COPILOT_DASHBOARD_WIDGETS)}, id="all-hidden"),
        pytest.param({"items": [_item()]}, id="hidden-omitted"),
        pytest.param(
            {"items": [_item(x=GRID_COLUMNS - 1, w=1, y=MAX_GRID_ROW, h=MAX_WIDGET_HEIGHT)]},
            id="grid-bounds",
        ),
    ],
)
def test_a_valid_layout_is_accepted(layout):
    parsed = UserPreferencesRequest(copilot_dashboard_layout=layout).copilot_dashboard_layout
    assert parsed.model_dump()["items"] == layout["items"]


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param({"items": [_item(i="stage_scoping")]}, id="command-center-widget"),
        pytest.param({"items": [_item(i="unknown")]}, id="unknown-widget"),
        pytest.param({"items": [_item(), _item(y=7)]}, id="widget-twice"),
        pytest.param({"items": [_item()], "hidden": ["hero"]}, id="shown-and-hidden"),
        pytest.param({"items": [], "hidden": ["nope"]}, id="unknown-hidden"),
        pytest.param({"items": [_item(x=6, w=7)]}, id="past-last-column"),
        pytest.param({"items": [_item(w=0)]}, id="zero-width"),
        pytest.param({"items": [_item(h=MAX_WIDGET_HEIGHT + 1)]}, id="too-tall"),
        pytest.param({"items": [_item(y=MAX_GRID_ROW + 1)]}, id="too-low"),
        pytest.param({"items": [_item(i="h" * 10_000)]}, id="item-id-too-long"),
        pytest.param({"items": [], "hidden": ["h" * 10_000]}, id="hidden-id-too-long"),
        pytest.param({"items": [{**_item(), "z": 1}]}, id="item-extra-key"),
        pytest.param({"items": [], "vertical": "cyber"}, id="layout-extra-key"),
        pytest.param({"items": [], "hidden": [*COPILOT_DASHBOARD_WIDGETS, "hero"]}, id="hidden-too-long"),
        pytest.param("hero", id="not-an-object"),
    ],
)
def test_an_invalid_layout_is_refused(layout):
    with pytest.raises(ValidationError) as excinfo:
        UserPreferencesRequest(copilot_dashboard_layout=layout)
    errors = excinfo.value.errors()
    assert all(error["loc"][0] == "copilot_dashboard_layout" for error in errors)


def test_the_command_center_still_refuses_copilot_widgets():
    with pytest.raises(ValidationError):
        UserPreferencesRequest(
            ctem_dashboard_layout={"vertical": "cyber", "layout": {"items": [_item(i="kpi_documents")]}}
        )
    assert "kpi_documents" not in CTEM_DASHBOARD_WIDGETS


@pytest.mark.asyncio
async def test_a_layout_is_stored_as_sent(monkeypatch):
    user = _user()
    await _update(monkeypatch, user, UserPreferencesRequest(copilot_dashboard_layout=LAYOUT))
    assert user.copilot_dashboard_layout == LAYOUT


@pytest.mark.asyncio
async def test_an_explicit_null_resets_the_default_layout(monkeypatch):
    user = _user(copilot_dashboard_layout=LAYOUT)
    await _update(monkeypatch, user, UserPreferencesRequest(copilot_dashboard_layout=None))
    assert user.copilot_dashboard_layout is None


@pytest.mark.asyncio
async def test_another_preference_leaves_the_layout_alone(monkeypatch):
    ctem = {"cyber": {"items": [], "hidden": []}}
    user = _user(copilot_dashboard_layout=LAYOUT, ctem_dashboard_layouts=ctem)
    await _update(monkeypatch, user, UserPreferencesRequest(theme="light"))
    assert user.copilot_dashboard_layout == LAYOUT
    assert user.ctem_dashboard_layouts == ctem
