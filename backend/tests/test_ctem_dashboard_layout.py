"""The command center each person builds (``users.ctem_dashboard_layouts``).

A per-user preference written one vertical at a time through
``PUT /settings/preferences``: the request model accepts known widgets only,
each once, placed inside the grid, for a known vertical (anything else is a
422, never a silent drop); ``layout: null`` resets that vertical; the other
verticals' layouts are kept, and omitting the field leaves them all alone.

The route coroutine is driven directly with a fake session, like
``test_user_favorite_agents.py``; ``test_ctem_dashboard_layout_postgres.py``
covers the real routes on PostgreSQL.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

import app.api.v1.settings as settings_api
from app.api.v1.schemas.settings import CtemDashboardLayout, UserPreferencesRequest
from app.services.dashboard_layouts import (
    CTEM_DASHBOARD_WIDGETS,
    GRID_COLUMNS,
    MAX_WIDGET_HEIGHT,
    max_grid_row,
)

MAX_GRID_ROW = max_grid_row(CTEM_DASHBOARD_WIDGETS)


class _StoredLayouts:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    """Answers the layouts re-read with what the row holds (``stored``)."""

    def __init__(self, stored):
        self._stored = stored
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _StoredLayouts(self._stored)

    async def flush(self):
        return None

    async def commit(self):
        return None


def _user(**overrides: Any) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        email="u@example.com",
        full_name="Test User",
        title=None,
        preferred_llm_model=None,
        ctem_vertical="cyber",
        ctem_dashboard_layouts=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


async def _update(monkeypatch, user, body: UserPreferencesRequest, db=None):
    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(settings_api, "log_audit", _no_audit)
    return await settings_api.update_preferences(
        body=body,
        request=SimpleNamespace(headers={}, client=None),
        user=user,
        db=db or _FakeDB(user.ctem_dashboard_layouts),
    )


LAYOUT = {
    "items": [
        {"i": "digests", "x": 0, "y": 0, "w": 12, "h": 6},
        {"i": "hero", "x": 0, "y": 6, "w": 8, "h": 8},
        {"i": "kpi_agents", "x": 8, "y": 6, "w": 4, "h": 3},
    ],
    "hidden": ["briefing", "stage_governance"],
}


def _request(vertical="cyber", layout=LAYOUT) -> UserPreferencesRequest:
    return UserPreferencesRequest(ctem_dashboard_layout={"vertical": vertical, "layout": layout})


def _item(**overrides: Any) -> dict[str, Any]:
    return {"i": "hero", "x": 0, "y": 0, "w": 12, "h": 8, **overrides}


def test_the_widgets_are_the_thirty_of_the_command_center():
    assert len(CTEM_DASHBOARD_WIDGETS) == 30
    assert len(set(CTEM_DASHBOARD_WIDGETS)) == 30


def test_the_widgets_are_the_ones_the_dashboard_draws():
    """The frontend sends every widget it draws in each layout it saves, so a
    widget on one side only would turn every save into a 422: the backend list
    is pinned against ``CTEM_WIDGETS`` in the dashboard's layout module, as
    ``SPA_FIRST_SEGMENTS`` is against ``App.tsx`` (``test_base_path.py``)."""
    source = (
        Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "xtm-one" / "ctem" / "dashboard-layout.ts"
    ).read_text(encoding="utf-8")
    block = re.search(
        r"export const CTEM_WIDGETS: Record<CtemWidgetId, WidgetSpec> = \{(.*?)\n\};",
        source,
        re.DOTALL,
    )
    assert block, "CTEM_WIDGETS not found in dashboard-layout.ts"
    frontend_ids = re.findall(r"^  (\w+):", block.group(1), re.MULTILINE)
    assert frontend_ids == list(CTEM_DASHBOARD_WIDGETS)


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param(LAYOUT, id="partial"),
        pytest.param({"items": [], "hidden": list(CTEM_DASHBOARD_WIDGETS)}, id="all-hidden"),
        pytest.param({"items": [_item()]}, id="hidden-omitted"),
        pytest.param(
            {"items": [_item(x=GRID_COLUMNS - 1, w=1, y=MAX_GRID_ROW, h=MAX_WIDGET_HEIGHT)]},
            id="grid-bounds",
        ),
        pytest.param(None, id="reset"),
    ],
)
def test_a_valid_layout_is_accepted(layout):
    update = _request(layout=layout).ctem_dashboard_layout
    assert update.vertical == "cyber"
    if layout is None:
        assert update.layout is None
    else:
        # As stored (exclude_none): the optional settings of S2f add no key.
        assert _layout(_request(layout=layout)).model_dump(exclude_none=True)["items"] == layout["items"]


@pytest.mark.parametrize(
    "update",
    [
        pytest.param({"vertical": "space", "layout": None}, id="unknown-vertical"),
        pytest.param({"vertical": "Cyber", "layout": None}, id="vertical-wrong-case"),
        pytest.param({"layout": None}, id="vertical-missing"),
        pytest.param({"vertical": "cyber"}, id="layout-missing"),
        pytest.param({"vertical": "cyber", "layout": None, "extra": 1}, id="extra-key"),
        pytest.param({"vertical": "cyber", "layout": {"items": [_item(i="unknown")]}}, id="unknown-widget"),
        pytest.param({"vertical": "cyber", "layout": {"items": [_item(), _item(y=8)]}}, id="widget-twice"),
        pytest.param(
            {"vertical": "cyber", "layout": {"items": [_item()], "hidden": ["hero"]}},
            id="shown-and-hidden",
        ),
        pytest.param({"vertical": "cyber", "layout": {"items": [], "hidden": ["nope"]}}, id="unknown-hidden"),
        pytest.param({"vertical": "cyber", "layout": {"items": [_item(x=-1)]}}, id="x-negative"),
        pytest.param({"vertical": "cyber", "layout": {"items": [_item(x=6, w=7)]}}, id="past-last-column"),
        pytest.param({"vertical": "cyber", "layout": {"items": [_item(w=0)]}}, id="zero-width"),
        pytest.param({"vertical": "cyber", "layout": {"items": [_item(w=13)]}}, id="too-wide"),
        pytest.param({"vertical": "cyber", "layout": {"items": [_item(h=0)]}}, id="zero-height"),
        pytest.param({"vertical": "cyber", "layout": {"items": [_item(h=MAX_WIDGET_HEIGHT + 1)]}}, id="too-tall"),
        pytest.param({"vertical": "cyber", "layout": {"items": [_item(y=MAX_GRID_ROW + 1)]}}, id="too-low"),
        pytest.param({"vertical": "cyber", "layout": {"items": [_item(w=2.5)]}}, id="fractional"),
        pytest.param({"vertical": "cyber", "layout": {"items": [{**_item(), "z": 1}]}}, id="item-extra-key"),
        pytest.param({"vertical": "cyber", "layout": {"items": "hero"}}, id="items-not-a-list"),
        pytest.param(
            {"vertical": "cyber", "layout": {"items": [], "hidden": [*CTEM_DASHBOARD_WIDGETS, "hero"]}},
            id="hidden-too-long",
        ),
        pytest.param({"vertical": "cyber", "layout": {"items": [_item(i="h" * 10_000)]}}, id="item-id-too-long"),
        pytest.param({"vertical": "cyber", "layout": {"items": [], "hidden": ["h" * 10_000]}}, id="hidden-id-too-long"),
    ],
)
def test_an_invalid_layout_is_refused(update):
    with pytest.raises(ValidationError) as excinfo:
        UserPreferencesRequest(ctem_dashboard_layout=update)
    errors = excinfo.value.errors()
    assert errors[0]["loc"][0] == "ctem_dashboard_layout"
    assert all(error["loc"][0] == "ctem_dashboard_layout" for error in errors)


@pytest.mark.asyncio
async def test_a_layout_is_stored_for_its_vertical(monkeypatch):
    user = _user()
    await _update(monkeypatch, user, _request())
    assert user.ctem_dashboard_layouts == {"cyber": LAYOUT}


@pytest.mark.asyncio
async def test_a_layout_keeps_the_other_verticals(monkeypatch):
    fraud = {"items": [_item()], "hidden": []}
    user = _user(ctem_dashboard_layouts={"fraud": fraud})
    await _update(monkeypatch, user, _request(vertical="fimi"))
    assert user.ctem_dashboard_layouts == {"fraud": fraud, "fimi": LAYOUT}


@pytest.mark.asyncio
async def test_a_reset_drops_that_vertical_only(monkeypatch):
    fraud = {"items": [_item()], "hidden": []}
    user = _user(ctem_dashboard_layouts={"cyber": LAYOUT, "fraud": fraud})
    await _update(monkeypatch, user, _request(layout=None))
    assert user.ctem_dashboard_layouts == {"fraud": fraud}


@pytest.mark.asyncio
async def test_resetting_the_last_layout_stores_null(monkeypatch):
    user = _user(ctem_dashboard_layouts={"cyber": LAYOUT})
    await _update(monkeypatch, user, _request(layout=None))
    assert user.ctem_dashboard_layouts is None


@pytest.mark.asyncio
async def test_the_stored_layouts_are_a_new_object(monkeypatch):
    """A plain JSON column only notices a reassignment, never an in-place edit."""
    stored = {"fraud": {"items": [], "hidden": []}}
    user = _user(ctem_dashboard_layouts=stored)
    await _update(monkeypatch, user, _request())
    assert user.ctem_dashboard_layouts is not stored
    assert stored == {"fraud": {"items": [], "hidden": []}}


@pytest.mark.asyncio
async def test_another_preference_leaves_the_layouts_alone(monkeypatch):
    user = _user(ctem_dashboard_layouts={"cyber": LAYOUT})
    await _update(monkeypatch, user, UserPreferencesRequest(ctem_vertical="fraud"))
    assert user.ctem_dashboard_layouts == {"cyber": LAYOUT}
    assert user.ctem_vertical == "fraud"


@pytest.mark.asyncio
async def test_a_null_update_changes_nothing(monkeypatch):
    user = _user(ctem_dashboard_layouts={"cyber": LAYOUT})
    await _update(monkeypatch, user, UserPreferencesRequest(ctem_dashboard_layout=None))
    assert user.ctem_dashboard_layouts == {"cyber": LAYOUT}


@pytest.mark.asyncio
async def test_the_layouts_are_re_read_under_a_row_lock(monkeypatch):
    """The row's layouts win over the copy loaded with the request: another tab
    may have saved another vertical in between, and its layout must stay."""
    fraud = {"items": [_item()], "hidden": []}
    user = _user(ctem_dashboard_layouts=None)
    db = _FakeDB({"fraud": fraud})
    await _update(monkeypatch, user, _request(), db=db)
    assert user.ctem_dashboard_layouts == {"fraud": fraud, "cyber": LAYOUT}
    assert len(db.statements) == 1
    assert db.statements[0]._for_update_arg is not None


# The context bar and the widget settings (dev-docs/ctem-stage-viz.md 8.2, 8.3):
# optional fields, so a layout saved before them reads and stores unchanged.
CONTEXT_LAYOUT = {
    "items": [
        {"i": "hero", "x": 0, "y": 0, "w": 12, "h": 11},
        {"i": "exposure_stats", "x": 0, "y": 11, "w": 6, "h": 10, "settings": {"period": 90}},
    ],
    "hidden": ["briefing"],
    "context": {"framework": "disarm", "period": 7},
}


def _layout(request: Any) -> CtemDashboardLayout:
    update = request.ctem_dashboard_layout
    assert update is not None and update.layout is not None
    layout: CtemDashboardLayout = update.layout
    return layout


def test_a_layout_saved_before_the_context_reads_unchanged() -> None:
    layout = _layout(_request())
    assert layout.context is None
    assert all(item.settings is None for item in layout.items)
    assert layout.model_dump(exclude_none=True) == LAYOUT


def test_a_layout_with_a_context_and_widget_settings_is_accepted() -> None:
    layout = _layout(_request(layout=CONTEXT_LAYOUT))
    assert layout.model_dump(exclude_none=True) == CONTEXT_LAYOUT


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param({**LAYOUT, "context": {"period": 14}}, id="period-not-offered"),
        pytest.param({**LAYOUT, "context": {"framework": ""}}, id="framework-empty"),
        pytest.param({**LAYOUT, "context": {"framework": "f" * 129}}, id="framework-too-long"),
        pytest.param({**LAYOUT, "context": {"scope": "all"}}, id="context-extra-key"),
        pytest.param({"items": [{**_item(), "settings": {"period": 1}}]}, id="widget-period-not-offered"),
        pytest.param({"items": [{**_item(), "settings": {"rows": 3}}]}, id="widget-settings-extra-key"),
    ],
)
def test_an_invalid_context_is_refused(layout: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _request(layout=layout)


@pytest.mark.asyncio
async def test_a_layout_without_context_is_stored_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``context: null`` nor ``settings: null`` key is written for a layout
    that sets none: the stored value is the one saved before S2f."""
    user = _user()
    await _update(monkeypatch, user, _request())
    assert user.ctem_dashboard_layouts == {"cyber": LAYOUT}


@pytest.mark.asyncio
async def test_a_layout_with_a_context_is_stored_with_it(monkeypatch: pytest.MonkeyPatch) -> None:
    user = _user()
    await _update(monkeypatch, user, _request(layout=CONTEXT_LAYOUT))
    assert user.ctem_dashboard_layouts == {"cyber": CONTEXT_LAYOUT}


@pytest.mark.asyncio
async def test_no_layout_update_reads_nothing(monkeypatch):
    user = _user()
    db = _FakeDB(None)
    await _update(monkeypatch, user, UserPreferencesRequest(ctem_vertical="fraud"), db=db)
    assert db.statements == []
