"""Agent-built custom dashboards: the spec, the edits, the tools and the markers.

Unit tests, no database: the tools run on a fake session (``async_session`` is
swapped), the channel resolvers on a fake result. The routes, ownership and
the row lock are exercised against PostgreSQL in
``test_custom_dashboards_postgres.py``.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.custom_dashboard_spec import (
    DASHBOARD_TONES,
    DASHBOARD_WIDGET_KINDS,
    MAX_WIDGETS,
    DashboardSpecError,
    apply_operations,
    validate_spec,
    widget_catalog,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SPEC = BACKEND_ROOT.parent / "frontend" / "src" / "components" / "custom-dashboard" / "spec.ts"


# ── Samples: the "Today" companion screen, one widget of each kind ───────────

SAMPLES: dict[str, dict] = {
    "score": {
        "kind": "score",
        "id": "score",
        "label": "Exposure score",
        "value": 62,
        "max": 100,
        "trend": {"direction": "down", "label": "8 pts this week", "tone": "success"},
        "series": [78, 77, 74, 75, 71, 72, 68, 69, 65, 64, 62],
        "series_label": "Exposure score over 30 days",
        "stats": [
            {"label": "Critical", "value": 7, "tone": "warning"},
            {"label": "Exploitable", "value": "3"},
            {"label": "Fixed (7d)", "value": 12},
        ],
    },
    "stats": {
        "kind": "stats",
        "items": [{"label": "Risk score", "value": 9.6}, {"label": "To domain admin", "value": "4 steps"}],
    },
    "stages": {
        "kind": "stages",
        "title": "CTEM cycle · Q4 scope",
        "link": {"label": "Details", "href": "/ctem"},
        "items": [
            {"label": "Scoping", "state": "done", "status": "Done"},
            {"label": "Prioritize", "state": "active", "status": "Live"},
            {"label": "Validate", "state": "pending", "status": "2 running", "progress": 0.4},
        ],
    },
    "decision": {
        "kind": "decision",
        "eyebrow": "Needs your decision",
        "time": "2 min ago",
        "text": "Isolate **3 internet-facing VPN gateways** until the patch window.",
        "tags": ["Validated by simulation"],
        "actions": [
            {"label": "Review", "prompt": "Show me the containment plan for the VPN gateways"},
            {"label": "Open runs", "href": "/operations"},
        ],
    },
    "feed": {
        "kind": "feed",
        "title": "Latest from your agents",
        "items": [{"badge": "DSC", "title": "Attack surface discovery", "time": "07:40", "text": "14 new assets."}],
    },
    "callout": {
        "kind": "callout",
        "eyebrow": "Threat landscape",
        "text": "2 intrusion sets match 4 exposures.",
        "tone": "ai",
    },
    "ranked_list": {
        "kind": "ranked_list",
        "items": [{"title": "VPN gateways - RCE", "meta": "3 assets", "badge": {"label": "CRIT", "tone": "danger"}}],
    },
    "steps": {"kind": "steps", "items": [{"label": "Initial access", "detail": "Not prevented", "tone": "danger"}]},
    "checklist": {"kind": "checklist", "items": [{"label": "Isolate the gateways", "done": True}, {"label": "Re-run"}]},
    "progress": {
        "kind": "progress",
        "title": "Breach & attack simulation",
        "status": {"label": "Running", "tone": "accent"},
        "progress": 0.64,
        "progress_label": "Step 7 of 11",
        "lines": [{"label": "T1566 Phishing - prevented", "tone": "success"}],
    },
    "prompts": {"kind": "prompts", "suggestions": ["Which threat actors could use these exposures?"]},
    "text": {"kind": "text", "body": "Your score improved by **8 points**."},
    "platform": {
        "kind": "platform",
        "source": "opencrq",
        "integration_id": "11111111-1111-4111-8111-111111111111",
        "native": {"metric": "kev_findings"},
    },
}


def _spec(*kinds: str) -> dict:
    return {"title": "Today", "widgets": [copy.deepcopy(SAMPLES[k]) for k in kinds or ("score",)]}


def _error_of(callable_, *args, **kwargs) -> str:
    with pytest.raises(DashboardSpecError) as exc:
        callable_(*args, **kwargs)
    return str(exc.value)


# ── The catalog ──────────────────────────────────────────────────────────────


def test_samples_cover_the_whole_catalog():
    assert sorted(SAMPLES) == sorted(DASHBOARD_WIDGET_KINDS)
    assert sorted(widget_catalog()) == sorted(DASHBOARD_WIDGET_KINDS)


@pytest.mark.parametrize("kind", DASHBOARD_WIDGET_KINDS)
def test_every_kind_validates(kind):
    spec = validate_spec(_spec(kind))
    assert [w["kind"] for w in spec["widgets"]] == [kind]


def test_the_frontend_draws_the_same_catalog():
    """The renderer's list of kinds and tones is the validator's, in order."""
    source = FRONTEND_SPEC.read_text(encoding="utf-8")

    def _array(name: str) -> list[str]:
        match = re.search(rf"export const {name} = \[(.*?)\] as const", source, re.S)
        assert match, f"{name} not found in {FRONTEND_SPEC}"
        return re.findall(r'"([a-z_]+)"', match.group(1))

    assert _array("DASHBOARD_WIDGET_KINDS") == list(DASHBOARD_WIDGET_KINDS)
    assert _array("DASHBOARD_TONES") == list(DASHBOARD_TONES)


def test_a_whole_dashboard_normalizes_and_gets_ids():
    spec = validate_spec({"title": "Today", "widgets": [{"kind": "text", "body": "a"}, {"kind": "text", "body": "b"}]})
    assert [w["id"] for w in spec["widgets"]] == ["text", "text-2"]
    assert [w["width"] for w in spec["widgets"]] == ["full", "full"]


def test_figures_sent_as_numbers_are_shown_as_written():
    spec = validate_spec(_spec("score"))
    assert [s["value"] for s in spec["widgets"][0]["stats"]] == ["7", "3", "12"]
    stats = validate_spec(_spec("stats"))["widgets"][0]["items"]
    assert [s["value"] for s in stats] == ["9.6", "4 steps"]


# ── Failing loudly at write time ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("widget", "fragment"),
    [
        ({"kind": "score", "label": "x", "value": 1, "colour": "#f00"}, "widgets.0.colour (kind=score): Extra inputs"),
        ({"kind": "pie_chart", "values": [1]}, "widgets.0"),
        ({"kind": "score", "label": "x"}, "widgets.0.value (kind=score): Field required"),
        ({"kind": "stages", "items": [{"label": "a", "state": "late"}]}, "widgets.0.items.0.state"),
        ({"kind": "score", "label": "x", "value": 1, "series": [1]}, "widgets.0.series"),
        ({"kind": "callout", "text": "x", "tone": "red"}, "widgets.0.tone"),
        ({"kind": "text", "body": "x", "id": "Bad Id"}, "widgets.0.id"),
        ({"kind": "progress", "progress": 1.5}, "widgets.0.progress"),
    ],
)
def test_an_invalid_widget_names_the_field(widget, fragment):
    message = _error_of(validate_spec, {"title": "T", "widgets": [widget]})
    assert "was NOT saved" in message
    assert fragment in message
    assert "Allowed widget kinds: score, stats" in message


def test_the_widget_cap_is_enforced():
    widgets = [{"kind": "text", "body": str(i)} for i in range(MAX_WIDGETS + 1)]
    assert "widgets" in _error_of(validate_spec, {"title": "T", "widgets": widgets})


def test_two_widgets_cannot_share_an_id():
    widgets = [{"kind": "text", "body": "a", "id": "x"}, {"kind": "text", "body": "b", "id": "x"}]
    assert _error_of(validate_spec, {"title": "T", "widgets": widgets}) == (
        "Two widgets have the id 'x': each widget needs its own id."
    )


def test_a_payload_that_is_not_an_object_is_refused():
    assert "JSON object" in _error_of(validate_spec, [{"kind": "text"}])


# ── Every value a third party may have shaped is hostile ─────────────────────


@pytest.mark.parametrize(
    "href",
    [
        "/agents",
        "/presentations/3f0c?tab=slides#/s1",
        "/operations/executions",
        "/ctem",
    ],
)
def test_a_platform_path_is_a_link(href):
    widget = {"kind": "callout", "text": "x", "actions": [{"label": "Open", "href": href}]}
    assert validate_spec({"title": "T", "widgets": [widget]})["widgets"][0]["actions"][0]["href"] == href


@pytest.mark.parametrize(
    "href",
    [
        "https://evil.example/login",
        "http://evil.example",
        "//evil.example/x",
        "/\\evil.example",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "/api/v1/users",
        "/API/v1/users",
        "/api",
        "agents",
        "/agents\nfoo",
        "/agents foo",
        '/agents"onmouseover="x',
        "/agents<script>",
        "",
        "/" + "a" * 301,
    ],
)
def test_anything_else_is_refused_as_a_link(href):
    for widget in (
        {"kind": "callout", "text": "x", "actions": [{"label": "Open", "href": href}]},
        {"kind": "feed", "items": [{"title": "x", "href": href}]},
        {"kind": "ranked_list", "items": [{"title": "x", "href": href}]},
        {"kind": "text", "body": "x", "link": {"label": "Open", "href": href}},
    ):
        message = _error_of(validate_spec, {"title": "T", "widgets": [widget]})
        assert "href" in message or "String should have at most" in message


@pytest.mark.parametrize(
    "action",
    [
        {"label": "Both", "href": "/agents", "prompt": "hi"},
        {"label": "Neither"},
        {"label": "Empty prompt", "prompt": ""},
        {"href": "/agents"},
    ],
)
def test_an_action_has_exactly_one_target(action):
    widget = {"kind": "decision", "text": "x", "actions": [action]}
    assert "widgets.0.actions.0" in _error_of(validate_spec, {"title": "T", "widgets": [widget]})


def test_text_is_stored_as_written_never_as_markup():
    body = "<img src=x onerror=alert(1)> **bold** [[danger:red]]"
    stored = validate_spec({"title": "<b>T</b>", "widgets": [{"kind": "text", "body": body}]})
    # Nothing is escaped or interpreted here: the renderer tokenizes into React
    # nodes and never into an HTML string.
    assert stored["widgets"][0]["body"] == body
    assert stored["title"] == "<b>T</b>"


# ── Partial edits ────────────────────────────────────────────────────────────


def _stored(*kinds: str) -> dict:
    return validate_spec(_spec(*kinds))


def test_an_upsert_replaces_the_widget_of_that_id_in_place():
    spec = _stored("score", "stages", "feed")
    new_score = {**SAMPLES["score"], "value": 55}
    edited = apply_operations(spec, [{"op": "upsert", "widget": new_score}])
    assert [w["id"] for w in edited["widgets"]] == ["score", "stages", "feed"]
    assert edited["widgets"][0]["value"] == 55


def test_an_upsert_adds_a_new_widget_last_or_after_one():
    spec = _stored("score", "feed")
    edited = apply_operations(
        spec,
        [
            {"op": "upsert", "widget": {"kind": "text", "id": "note", "body": "x"}},
            {"op": "upsert", "widget": {"kind": "text", "body": "y"}, "after": "start"},
            {"op": "upsert", "widget": {"kind": "callout", "id": "intel", "text": "z"}, "after": "score"},
        ],
    )
    assert [w["id"] for w in edited["widgets"]] == ["text", "score", "intel", "feed", "note"]


def test_remove_and_move_by_id():
    spec = _stored("score", "stages", "feed")
    edited = apply_operations(spec, [{"op": "move", "id": "feed", "after": "start"}, {"op": "remove", "id": "stages"}])
    assert [w["id"] for w in edited["widgets"]] == ["feed", "score"]


@pytest.mark.parametrize(
    ("operations", "fragment"),
    [
        ([{"op": "remove", "id": "nope"}], "operations.0: no widget has the id 'nope' (ids: score, feed)"),
        ([{"op": "move", "id": "feed"}], "operations.0: a move needs 'after'"),
        ([{"op": "move", "id": "feed", "after": "feed"}], "cannot move after itself"),
        ([{"op": "move", "id": "feed", "after": "ghost"}], "operations.0.after names no widget 'ghost'"),
        ([{"op": "upsert"}], "operations.0: an upsert needs 'widget'"),
        ([{"op": "upsert", "widget": {"kind": "gauge"}}], "operations.0.widget.kind must be one of"),
        ([{"op": "explode", "id": "feed"}], "operations.0 is invalid"),
        ([{"op": "remove", "id": "score"}, {"op": "remove", "id": "feed"}], "widgets"),
        ("remove score", "operations must be an array"),
    ],
)
def test_an_invalid_edit_names_the_operation_and_writes_nothing(operations, fragment):
    spec = _stored("score", "feed")
    before = copy.deepcopy(spec)
    assert fragment in _error_of(apply_operations, spec, operations)
    assert spec == before


def test_a_widget_of_an_older_catalog_keeps_the_others_editable_once_replaced():
    spec = _stored("score", "feed")
    # A widget stored under a catalog that has since changed.
    spec["widgets"].append({"id": "legacy", "kind": "gauge", "value": 3})
    blocked = _error_of(apply_operations, spec, [{"op": "remove", "id": "feed"}])
    assert blocked.startswith("Widget 'legacy' no longer matches the widget catalog")

    replaced = apply_operations(spec, [{"op": "upsert", "widget": {"kind": "text", "id": "legacy", "body": "3"}}])
    assert [w["kind"] for w in replaced["widgets"]] == ["score", "feed", "text"]
    removed = apply_operations(spec, [{"op": "remove", "id": "legacy"}])
    assert [w["id"] for w in removed["widgets"]] == ["score", "feed"]


def test_a_rename_alone_is_an_edit():
    spec = _stored("score")
    edited = apply_operations(spec, [], title="Morning brief", subtitle="Fri 16 Oct")
    assert (edited["title"], edited["subtitle"]) == ("Morning brief", "Fri 16 Oct")
    cleared = apply_operations(edited, [], subtitle="")
    assert "subtitle" not in cleared


# ── The tools ────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, one=None, rows=None):
        self._one, self._rows = one, rows or []

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._one


class _FakeSession:
    def __init__(self, results=None):
        self._results = list(results or [])
        self.statements: list = []
        self.added: list = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement, *a, **k):
        self.statements.append(statement)
        return self._results.pop(0) if self._results else _Result()

    async def commit(self):
        self.committed = True

    async def flush(self):
        return None

    def add(self, obj):
        self.added.append(obj)


@pytest.fixture
def session(monkeypatch):
    import app.core.database as database

    fake = _FakeSession()
    monkeypatch.setattr(database, "async_session", lambda: fake)
    return fake


def _tools(user_id: str | None, conversation_id: str | None = None, run_id: str | None = None):
    from app.services.builtin_tools import build_custom_dashboard_tools

    _defs, executor = build_custom_dashboard_tools(None, user_id or "", conversation_id, assignment_run_id=run_id)
    return executor


def _call(executor, name: str, arguments: dict):
    from app.tools.base import ToolCall

    return asyncio.run(executor.execute_tool(ToolCall(id="call-1", name=name, arguments=arguments)))


def test_build_dashboard_saves_the_user_dashboard_and_returns_its_marker(session):
    user_id, conversation_id = str(uuid.uuid4()), str(uuid.uuid4())
    result = _call(_tools(user_id, conversation_id), "build_dashboard", _spec("score", "decision"))

    assert result.is_error is False, result.content
    [row] = session.added
    assert (str(row.user_id), str(row.conversation_id)) == (user_id, conversation_id)
    assert (row.title, row.widget_count, row.revision) == ("Today", 2, 1)
    assert session.committed is True
    assert result.content.endswith(f"[[DASHBOARD:{row.id}]]")
    assert "  - score [xtm.score, 0,0 12x3] Exposure score\n  - decision [xtm.decision, 0,3 12x3]" in result.content


def test_the_marker_is_the_one_the_channels_read():
    from app.channels.base import _DASHBOARD_MARKER_RE
    from app.services.custom_dashboards import DASHBOARD_MARKER

    dashboard_id = uuid.uuid4()
    assert _DASHBOARD_MARKER_RE.fullmatch(DASHBOARD_MARKER.format(id=dashboard_id)).group(1) == str(dashboard_id)


def test_widgets_sent_as_a_json_string_are_parsed(session):
    payload = {"title": "Today", "widgets": json.dumps([SAMPLES["text"]])}
    result = _call(_tools(str(uuid.uuid4())), "build_dashboard", payload)
    assert result.is_error is False, result.content
    assert session.added[0].widget_count == 1


def test_an_invalid_dashboard_writes_nothing_and_says_why(session):
    result = _call(_tools(str(uuid.uuid4())), "build_dashboard", {"title": "T", "widgets": [{"kind": "gauge"}]})
    assert result.is_error is True
    assert "widgets.0" in result.content
    assert session.added == []
    assert session.committed is False


def test_a_signed_out_caller_cannot_build_a_dashboard(session):
    result = _call(_tools(None), "build_dashboard", _spec("text"))
    assert (result.is_error, result.content) == (True, "Dashboards can only be built for a signed-in user.")
    assert session.statements == []


def test_editing_someone_elses_dashboard_finds_nothing(session):
    # The visibility-scoped lookup (after the user's groups) answers no row for another user's id.
    session._results = [_Result(rows=[]), _Result(one=None)]
    dashboard_id = uuid.uuid4()
    result = _call(_tools(str(uuid.uuid4())), "edit_dashboard", {"dashboard_id": str(dashboard_id), "operations": []})
    assert (result.is_error, result.content) == (True, f"No dashboard `{dashboard_id}` you can edit was found.")
    [_groups, statement] = session.statements
    compiled = str(statement.compile(compile_kwargs={"literal_binds": False}))
    assert "custom_dashboards.user_id = " in compiled
    assert "custom_dashboards.shared_user_ids" in compiled
    assert "FOR UPDATE" in compiled
    assert session.committed is False


def test_edit_dashboard_applies_the_operations_and_bumps_the_revision(session):
    from app.models.custom_dashboard import CustomDashboard

    user_id = uuid.uuid4()
    row = CustomDashboard(
        id=uuid.uuid4(), user_id=user_id, title="Today", widget_count=2, revision=3, spec=_stored("score", "feed")
    )
    session._results = [_Result(rows=[]), _Result(one=row)]
    run_id = str(uuid.uuid4())
    result = _call(
        _tools(str(user_id), run_id=run_id),
        "edit_dashboard",
        {"dashboard_id": str(row.id), "revision": 3, "operations": json.dumps([{"op": "remove", "id": "feed"}])},
    )
    assert result.is_error is False, result.content
    assert (row.revision, row.widget_count, str(row.assignment_run_id)) == (4, 1, run_id)
    assert session.committed is True


def test_an_edit_at_a_stale_revision_is_refused(session):
    from app.models.custom_dashboard import CustomDashboard

    user_id = uuid.uuid4()
    row = CustomDashboard(
        id=uuid.uuid4(), user_id=user_id, title="T", widget_count=1, revision=5, spec=_stored("score")
    )
    session._results = [_Result(rows=[]), _Result(one=row)]
    result = _call(
        _tools(str(user_id)), "edit_dashboard", {"dashboard_id": str(row.id), "revision": 4, "operations": []}
    )
    assert result.is_error is True
    assert "it is now at revision 5" in result.content
    assert row.revision == 5
    assert session.committed is False


def test_every_agentic_entry_point_wires_the_suite():
    for module in ("agent_chat", "general_chat", "assignment_engine"):
        source = (BACKEND_ROOT / "app" / "services" / f"{module}.py").read_text(encoding="utf-8")
        assert '"custom_dashboards" in ' in source, module
        assert "build_custom_dashboard_tools(" in source, module


def test_the_suite_is_a_known_builtin_tool_id_everywhere():
    from app.models.general_assistant_config import (
        DEFAULT_GA_BUILTIN_TOOL_IDS,
        GENERAL_ASSISTANT_BUILTIN_TOOL_IDS,
    )
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog
    from app.services.builtin_tools.agent_builder import _VALID_BUILTIN_TOOL_IDS

    assert "custom_dashboards" in _VALID_BUILTIN_TOOL_IDS
    assert "custom_dashboards" in GENERAL_ASSISTANT_BUILTIN_TOOL_IDS
    # On by default, as the deck and video tools: the Dashboard Studio needs an assistant out of the box.
    assert "custom_dashboards" in DEFAULT_GA_BUILTIN_TOOL_IDS
    names = [tool["name"] for tool in build_builtin_tool_catalog()["custom_dashboards"]]
    assert names == ["build_dashboard", "edit_dashboard", "get_dashboard", "list_dashboards"]


def test_live_platform_widgets_and_their_tools_come_with_the_dashboard_studio(dashboard_studio_enabled):
    from app.services.builtin_tools.custom_dashboard import build_custom_dashboard_tools

    definitions, _executor = build_custom_dashboard_tools(None, str(uuid.uuid4()))
    names = [d.name for d in definitions]
    assert names == [
        "build_dashboard", "edit_dashboard", "get_dashboard", "list_dashboards",
        "list_platform_integrations", "preview_platform_widget", "list_widget_packs", "get_widget_pack",
        "insert_widgets_from_pack", "save_dashboard_as_pack", "import_widget_pack", "publish_dashboard",
        "export_dashboard", "send_dashboard_export",
        "dashboard_catalog", "describe_platform_widget", "lookup_platform_values", "preview_source", "add_widget",
        "check_dashboard",
    ]  # fmt: skip
    # What only reads is declared so: no approval asked of the agent's users for it.
    from app.services.tool_classification import definition_tool_type

    assert {d.name for d in definitions if definition_tool_type(d) == "read"} >= {
        "dashboard_catalog", "describe_platform_widget", "lookup_platform_values", "preview_source", "check_dashboard",
    }  # fmt: skip
    build = definitions[0].input_schema["properties"]["widgets"]["items"]["properties"]["kind"]["enum"]
    assert build[-1] == "platform"
    assert "- platform: LIVE data from OpenCTI, OpenAEV or OpenCRQ" in definitions[0].description


def test_without_the_studio_no_platform_kind_is_offered(monkeypatch):
    from app.core.config import settings
    from app.services.builtin_tools.custom_dashboard import build_custom_dashboard_tools

    monkeypatch.setattr(settings, "enabled_features", "")
    definitions, _executor = build_custom_dashboard_tools(None, str(uuid.uuid4()))
    kinds = definitions[0].input_schema["properties"]["widgets"]["items"]["properties"]["kind"]["enum"]
    assert "platform" not in kinds
    assert "platform:" not in definitions[0].description


def test_the_preview_tool_refuses_what_is_not_a_platform_widget(dashboard_studio_enabled):
    executor = _tools(str(uuid.uuid4()))
    text = _call(executor, "preview_platform_widget", {"widget": {"kind": "text", "body": "x"}})
    assert text.is_error is True
    assert text.content == "widget must be a platform widget: {kind: 'platform', source, integration_id, native}."
    ranged = _call(
        executor,
        "preview_platform_widget",
        {"widget": {"kind": "platform", "source": "opencrq", "integration_id": str(uuid.uuid4()), "native": {"metric": "kev_findings"}}, "range": "2y"},
    )
    assert ranged.content == "range must be one of: 1d, 7d, 30d, 1m, 90d, 3m, 6m, 1y, all."


# ── Markers on every other surface ───────────────────────────────────────────


def test_a_channel_gets_a_link_to_the_dashboard_page():
    from app.channels.base import resolve_channel_dashboard_markers

    dashboard_id = uuid.uuid4()
    session = _FakeSession(results=[_Result(rows=[SimpleNamespace(id=dashboard_id, title="Today")])])
    out = asyncio.run(
        resolve_channel_dashboard_markers(
            session, f"Here: [[DASHBOARD:{dashboard_id}]]", "https://app.example.org/", conversation_id=uuid.uuid4()
        )
    )
    assert out == f"Here: [Today](https://app.example.org/dashboards/{dashboard_id})"


def test_a_marker_out_of_scope_is_stripped_never_linked():
    from app.channels.base import resolve_channel_dashboard_markers

    session = _FakeSession(results=[_Result(rows=[])])
    out = asyncio.run(
        resolve_channel_dashboard_markers(
            session, f"See [[DASHBOARD:{uuid.uuid4()}]]", "https://app.example.org", user_id=uuid.uuid4()
        )
    )
    assert out == "See "


def test_a_preview_snippet_drops_the_marker():
    from app.channels.base import strip_artifact_markers

    assert strip_artifact_markers(f"Built it.\n\n[[DASHBOARD:{uuid.uuid4()}]]") == "Built it."


@pytest.mark.parametrize("link_only", [False, True])
def test_no_panel_sees_a_dashboard_marker_streamed(link_only):
    from app.services.platform_chat_artifacts import ArtifactMarkerStreamFilter

    dashboard_id, report_id = uuid.uuid4(), uuid.uuid4()
    stream = "Done [[DASH" + f"BOARD:{dashboard_id}]] and [[REPORT:{report_id}]]."
    stream_filter = ArtifactMarkerStreamFilter(link_only=link_only)
    shown = (
        "".join(stream_filter.feed(chunk) for chunk in (stream[:12], stream[12:30], stream[30:]))
        + stream_filter.flush()
    )
    assert "DASHBOARD" not in shown
    # A panel that renders reports keeps the report marker; another does not.
    assert (f"[[REPORT:{report_id}]]" in shown) is link_only


def test_a_panel_that_renders_artifacts_gets_the_dashboard_as_a_link(monkeypatch):
    from app.core.config import settings
    from app.services.platform_chat_artifacts import link_only_markers_as_links

    monkeypatch.setattr(settings, "frontend_url", "https://app.example.org")
    dashboard_id, report_id = uuid.uuid4(), uuid.uuid4()
    session = _FakeSession(results=[_Result(rows=[SimpleNamespace(id=dashboard_id, title="Today")])])
    out = asyncio.run(
        link_only_markers_as_links(
            session, f"[[DASHBOARD:{dashboard_id}]] [[REPORT:{report_id}]]", user_id=uuid.uuid4()
        )
    )
    assert out == f"[Today](https://app.example.org/dashboards/{dashboard_id}) [[REPORT:{report_id}]]"


def test_a_dashboard_card_never_depends_on_the_model_writing_its_marker():
    from app.services.custom_dashboards import append_markers

    a, b = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"
    assert append_markers("Your dashboard is ready.", [a]) == f"Your dashboard is ready.\n\n[[DASHBOARD:{a}]]"
    # Already there (any case): nothing added; one card per dashboard, in order.
    assert append_markers(f"Ready: [[DASHBOARD:{a.upper()}]]", [a]) == f"Ready: [[DASHBOARD:{a.upper()}]]"
    assert append_markers("Done.", [a, b, a]) == f"Done.\n\n[[DASHBOARD:{a}]]\n\n[[DASHBOARD:{b}]]"
    assert append_markers("", [a]) == f"[[DASHBOARD:{a}]]"
    assert append_markers("Nothing built.", []) == "Nothing built."


def test_the_studio_follows_the_widget_a_save_added_or_named():
    from app.services.builtin_tools.custom_dashboard import _focus_widget

    # The last visualization a save added wins, whatever the operations said.
    assert _focus_widget(["a", "b"], ["a", "b", "c", "d"], [{"op": "upsert", "widget": {"id": "a"}}]) == "d"
    # Otherwise the last operation naming a visualization that still exists.
    assert _focus_widget(["a", "b"], ["a", "b"], [{"op": "upsert", "widget": {"id": "a"}}, {"op": "move", "id": "b"}]) == "b"
    assert _focus_widget(["a"], ["a"], [{"op": "set", "path": "visualizations.a", "value": {}}]) == "a"
    assert _focus_widget(["a", "b"], ["a", "b"], [{"op": "layout", "items": [{"id": "a"}, {"id": "b"}]}]) == "b"
    # A removal, a data source edit or an id that is gone names nothing.
    assert _focus_widget(["a", "b"], ["a"], [{"op": "remove", "id": "b"}]) is None
    assert _focus_widget(["a"], ["a"], [{"op": "set", "path": "dataSources.ds_a", "value": {}}]) is None
    assert _focus_widget(["a"], ["a"], [{"op": "move", "id": "zz"}]) is None
    assert _focus_widget(["a"], ["a"], "not a list") is None
