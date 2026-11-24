"""Custom dashboards stay behind ``DASHBOARD_STUDIO``, off by default.

The flag covers the whole feature, the dashboards an agent builds included.
Off:

- the ``/api/v1/custom-dashboards`` and ``/api/v1/dashboard-widget-packs``
  routes answer 404, before authentication, and are not in the published
  schema;
- a published link answers like an unknown one;
- the ``custom_dashboards`` tool is never built and never offered;
- no conversation binds to a dashboard;
- a stored home dashboard is not served (``/auth/me`` carries null).

An agent that already carries the tool id keeps it in its configuration: the
flag decides what runs, not what is stored.

The frontend half is pinned by source assertions, as in
``test_video_studio_feature_flag.py``. The home dashboard's routes are covered
against PostgreSQL in ``test_home_dashboard_postgres.py``.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from app.core.feature_flags import KNOWN_FEATURE_FLAGS, is_feature_enabled
from app.services.builtin_tool_aliases import is_former_builtin_id
from app.services.builtin_tools import agent_builder

BACKEND = Path(__file__).resolve().parent.parent
FRONTEND = BACKEND.parent / "frontend"

FLAG = "DASHBOARD_STUDIO"
TOOL = "custom_dashboards"

requires_flag_off = pytest.mark.skipif(
    is_feature_enabled(FLAG),
    reason=f"ENABLED_FEATURES has {FLAG} on; these assert the flag-off shape",
)


def _squash(text: str) -> str:
    return " ".join(text.split())


def _has(source: str, snippet: str) -> bool:
    return _squash(snippet) in _squash(source)


def _read_backend(*parts: str) -> str:
    return BACKEND.joinpath(*parts).read_text(encoding="utf-8")


def _read_frontend(*parts: str) -> str:
    return FRONTEND.joinpath(*parts).read_text(encoding="utf-8")


def _line(source: str, needle: str) -> str:
    return next(line for line in source.splitlines() if needle in line)


def test_the_flag_is_in_both_registries():
    assert FLAG in KNOWN_FEATURE_FLAGS
    assert f'"{FLAG}",' in _read_frontend("src", "lib", "utils.ts")
    assert agent_builder._FEATURE_GATED_BUILTIN_TOOL_IDS[TOOL] == FLAG


@requires_flag_off
async def test_the_dashboard_api_answers_404_and_is_unpublished():
    """The flag's 404 answers before authentication, on the routes of the
    dashboards an agent builds as on the studio's."""
    from app.main import application

    dashboard = uuid.uuid4()
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for method, path in (
            ("GET", "/api/v1/custom-dashboards"),
            ("POST", "/api/v1/custom-dashboards"),
            ("GET", f"/api/v1/custom-dashboards/{dashboard}"),
            ("PATCH", f"/api/v1/custom-dashboards/{dashboard}"),
            ("DELETE", f"/api/v1/custom-dashboards/{dashboard}"),
            ("POST", f"/api/v1/custom-dashboards/{dashboard}/duplicate"),
            ("PUT", f"/api/v1/custom-dashboards/{dashboard}/sharing"),
            ("GET", f"/api/v1/custom-dashboards/{dashboard}/versions"),
            ("GET", "/api/v1/dashboard-widget-packs"),
        ):
            response = await client.request(method, path, json={})
            assert response.status_code == 404, (method, path)

    leaked = [
        p
        for p in application.openapi()["paths"]
        if p.startswith(("/api/v1/custom-dashboards", "/api/v1/dashboard-widget-packs", "/api/v1/public/dashboards"))
    ]
    assert leaked == [], f"dashboard endpoints still published: {leaked}"


@requires_flag_off
def test_published_links_answer_like_unknown_ones_whatever_the_security_option(monkeypatch):
    from app.services import dashboard_public_links

    monkeypatch.setattr(dashboard_public_links, "security_option", lambda _key: True)
    assert dashboard_public_links.public_links_enabled() is False


@requires_flag_off
async def test_no_conversation_binds_to_a_dashboard():
    from app.api.v1.chat import _visible_dashboard_binding

    with pytest.raises(HTTPException) as refused:
        await _visible_dashboard_binding(None, None, str(uuid.uuid4()))  # type: ignore[arg-type]
    assert (refused.value.status_code, refused.value.detail) == (404, "Dashboard not found")


@requires_flag_off
async def test_a_conversation_bound_to_a_dashboard_reads_no_studio_context():
    from app.services.dashboard_studio import load_dashboard_studio_context

    context = await load_dashboard_studio_context(None, user_id=uuid.uuid4(), dashboard_id=uuid.uuid4())  # type: ignore[arg-type]
    assert context is None


@requires_flag_off
async def test_the_catalog_withholds_the_tool_but_the_registry_keeps_it():
    from app.api.v1.settings import get_builtin_tool_catalog
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    assert TOOL in build_builtin_tool_catalog(include_disabled=True)
    assert TOOL not in await get_builtin_tool_catalog(_user=None)
    assert _has(
        _read_backend("app", "api", "v1", "settings.py"),
        'if not is_feature_enabled("DASHBOARD_STUDIO"): catalog.pop("custom_dashboards", None) '
        'if not is_feature_enabled("VIDEO_STUDIO"): catalog.pop("video_studio", None) for definitions in catalog.values():',
    ), "the prompt-guard scope offers the tool"


def test_the_tool_is_only_built_with_the_flag_on():
    for module, gate in (
        ("agent_chat.py", 'if "custom_dashboards" in agent_builtin_tool_ids and user_id and is_feature_enabled("DASHBOARD_STUDIO"):'),
        (
            "assignment_engine.py",
            'if "custom_dashboards" in agent_builtin_tool_ids and _run_file_owner_id and is_feature_enabled("DASHBOARD_STUDIO"):',
        ),
        ("general_chat.py", 'if "custom_dashboards" in ga_builtin_tool_ids and user_id and is_feature_enabled("DASHBOARD_STUDIO"):'),
        ("flow_access.py", 'and not (tool_id == "custom_dashboards" and not is_feature_enabled("DASHBOARD_STUDIO"))'),
    ):
        assert _has(_read_backend("app", "services", module), gate), module


@requires_flag_off
def test_the_agent_builder_does_not_name_the_tool_but_still_accepts_it():
    assert TOOL not in agent_builder._advertised_builtin_tool_ids()
    assert TOOL in agent_builder._VALID_BUILTIN_TOOL_IDS
    current = {t for t in agent_builder._VALID_BUILTIN_TOOL_IDS if not is_former_builtin_id(t)}
    withheld = {t for t, flag in agent_builder._FEATURE_GATED_BUILTIN_TOOL_IDS.items() if not is_feature_enabled(flag)}
    assert TOOL in withheld
    # Platform Health's internal group is never advertised either (#1943).
    assert set(agent_builder._advertised_builtin_tool_ids()) == current - withheld - {"health_specialists"}


def test_with_the_flag_on_the_agent_builder_names_the_tool(monkeypatch):
    monkeypatch.setattr(agent_builder, "is_feature_enabled", lambda flag: flag == FLAG)
    assert TOOL in agent_builder._advertised_builtin_tool_ids()


def test_the_frontend_offers_nothing_with_the_flag_off():
    nav = _read_frontend("src", "components", "layout", "nav-model.ts")
    assert _has(nav, 'leaf("/dashboards", "layout-dashboard", "Dashboards", (f) => f.dashboardStudio)')
    assert _has(_read_frontend("src", "components", "layout", "Sidebar.tsx"), "dashboardStudio: dashboardStudioEnabled,")
    # The top bar search resolves the nav with the shared flags (#11201).
    assert "useNavFlags()" in _read_frontend("src", "components", "layout", "GlobalSearch.tsx")
    assert _has(
        _read_frontend("src", "components", "layout", "use-nav-group.ts"),
        'const dashboardStudio = useFeatureFlag("DASHBOARD_STUDIO");',
    )

    app = _read_frontend("src", "App.tsx")
    for route in ('path="/dashboards"', 'path="/dashboards/packs"', 'path="/dashboards/:id"'):
        assert '<FeatureFlagGate flag="DASHBOARD_STUDIO">' in _line(app, route + " "), route
    studio_route = app.split('path="/dashboards/:id/studio"', 1)[1].split("<Route", 1)[0]
    assert '<FeatureFlagGate flag="DASHBOARD_STUDIO">' in studio_route
    public_route = app.split('path="/public/dashboards/:token"', 1)[1].split("<Route", 1)[0]
    assert "FeatureFlagGate" not in public_route, "a public link answers like an unknown one on its own"

    from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS

    assert next(g for g in BUILTIN_TOOL_GROUPS if g.id == TOOL).gate == "dashboard_studio"
    assert _has(_read_frontend("src", "types", "index.ts"), '| "dashboard_studio" |')
    assert _has(_read_frontend("src", "hooks", "useBuiltinTools.ts"), "dashboard_studio: dashboardStudioEnabled,")

    # The chat draws a dashboard's card only while the studio is on (the panel's marker cards).
    markers = _read_frontend("src", "components", "chat-panel", "markers.tsx")
    assert _has(markers, 'card = flags.dashboardStudio ? <CustomDashboardCard dashboardId={id} /> : null;')
    for host in ("XtmChatPanel.tsx", "InlineFileContent.tsx"):
        assert _has(_read_frontend("src", "components", "chat-panel", host), 'useFeatureFlag("DASHBOARD_STUDIO")'), host
    work = _read_frontend("src", "pages", "Work.tsx")
    assert _has(work, "const dashboards = useCustomDashboards(dashboardStudio);")
    # The card is the Work group's `/dashboards` child (#9301 N2c): `hubCards` shows a
    # card only for a visible child, and the nav leaf is gated on `f.dashboardStudio`.
    assert _has(work, 'hubCards(useNavGroup("work"), {')
    nav_group = _read_frontend("src", "components", "layout", "use-nav-group.ts")
    assert _has(nav_group, 'const dashboardStudio = useFeatureFlag("DASHBOARD_STUDIO");')
