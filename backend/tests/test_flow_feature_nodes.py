"""The flow nodes of the recent platform features.

Work items (update), fleets (hand work over), workspaces (add a source),
reports and presentations (create, export), the Video Studio (render), Deep
Investigation, and the platform-event trigger. Each node rides the service
or the built-in tool that owns the feature, so these tests assert on what the
node hands that owner (the spy's arguments) and on the outcome it publishes,
never on text grepped from the source. Hermetic: sessions and services are
faked, nothing reaches PostgreSQL or Redis.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import flow_catalog, flow_triggers
from app.services.company_managed_writes import flow_writer
from app.services.flow_catalog import NODE_KINDS_BY_ID, PLATFORM_EVENT_TRIGGER_KIND
from app.services.flow_runner import FlowWalker, _nodes_by_id, _Renderer
from app.tools.base import ToolResult

NEW_KINDS = {
    "trigger.platform_event",
    "action.update_work_item",
    "action.dispatch_to_fleet",
    "action.workspace_add_source",
    "action.create_report",
    "action.export_report",
    "action.export_presentation",
    "action.render_video",
    "action.deep_investigation",
}


# ── Fakes ────────────────────────────────────────────────────────────────────


class _Rows:
    def __init__(self, value: Any = None, rows: list | None = None):
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Db:
    """Answers every query with *value* (or each of *answers* in turn);
    records commits and rollbacks."""

    def __init__(self, value: Any = None, *, answers: list | None = None):
        self.value = value
        self.answers = list(answers) if answers is not None else None
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _statement):
        if self.answers is not None:
            return _Rows(self.answers.pop(0))
        return _Rows(self.value)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _own_session(monkeypatch, db: _Db) -> None:
    """``async_session()`` yields *db* (the nodes that write in their own session)."""

    @asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr("app.core.database.async_session", _session)


def _walker(*, outputs: dict | None = None, untrusted: set[str] | None = None, db: Any = None) -> FlowWalker:
    walker = object.__new__(FlowWalker)
    walker.graph = {"nodes": [], "edges": []}
    walker.nodes = _nodes_by_id(walker.graph)
    walker.state = {"merges": {}, "queue": [], "loops": {}}
    walker.db = db or _Db()
    walker.user = SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")
    walker.run = SimpleNamespace(id=uuid.uuid4(), flow_id=uuid.uuid4(), is_company_managed=False)
    walker.cancel_event = asyncio.Event()
    walker._renderer = _Renderer(
        node_outputs=outputs or {},
        untrusted_nodes=untrusted or set(),
        trigger_payload={},
        trigger_untrusted=False,
        variables={},
        trigger_source="manual trigger",
    )
    return walker


class _Spy:
    def __init__(self, result: Any = None, *, raises: BaseException | None = None):
        self.calls: list[tuple[tuple, dict]] = []
        self.result = result
        self.raises = raises

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.raises is not None:
            raise self.raises
        return self.result


# ── Catalog ──────────────────────────────────────────────────────────────────


def test_every_new_kind_is_in_the_catalog():
    assert set(NODE_KINDS_BY_ID) >= NEW_KINDS


def test_the_platform_event_trigger_offers_exactly_the_platform_events():
    from app.services.platform_events import PLATFORM_EVENTS

    field = next(f for f in NODE_KINDS_BY_ID[PLATFORM_EVENT_TRIGGER_KIND].config_fields if f.name == "events")
    assert [o["value"] for o in field.options] == sorted(PLATFORM_EVENTS)


@pytest.mark.parametrize("enabled", [True, False], ids=["flag-on", "flag-off"])
def test_a_feature_gated_node_follows_its_flag(monkeypatch, enabled):
    from app.services.flow_access import available_node_kinds

    monkeypatch.setattr("app.core.feature_flags.is_feature_enabled", lambda _flag: enabled)
    kinds = {nk["kind"] for nk in available_node_kinds()}
    gated = set(flow_catalog.FEATURE_GATED_NODE_KINDS)
    assert gated == {"action.render_video", "action.deep_investigation"}
    assert (gated <= kinds) is enabled
    assert gated.isdisjoint(kinds) is not enabled


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False], ids=["switches-on", "switches-off"])
async def test_the_tool_node_offers_the_new_suites_only_with_their_switch(monkeypatch, enabled):
    from app.core.config import settings
    from app.services.flow_access import catalog_sources

    monkeypatch.setattr("app.core.feature_flags.is_feature_enabled", lambda _flag: enabled)
    monkeypatch.setattr(settings, "runners_enabled", enabled)
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    sources = await catalog_sources(
        _Db(), user, is_admin=False, only=frozenset({"tools"}), user_group_ids=[]
    )
    offered = {o["value"] for o in sources["tools"]}
    suites = {"builtin:video_studio", "builtin:deep_investigation", "builtin:computer_use"}
    assert (suites <= offered) is enabled
    assert suites.isdisjoint(offered) is not enabled


# ── Update work item ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_note_goes_through_the_service_with_the_flows_writer(monkeypatch):
    update = _Spy({"id": "w1", "status": "in_progress"})
    monkeypatch.setattr("app.services.work_item.update_work_item", update)
    walker = _walker(outputs={"src": {"output": "half done"}})
    item_id = str(uuid.uuid4())

    result = await walker._exec_update_work_item(
        {"work_item_id": item_id, "operation": "note", "text": "{{src.output}}"}
    )

    assert result.error is None
    assert result.outputs == {"work_item_id": "w1", "status": "in_progress", "_tainted": False}
    (args, kwargs), = update.calls
    assert args == (item_id,)
    assert kwargs["note"] == "half done"
    assert kwargs["user_id"] == str(walker.user.id)
    assert kwargs["writer"] == flow_writer(is_company_managed=False, flow_id=walker.run.flow_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "status", "service", "expected_status"),
    [
        ("status", "in_progress", "update_work_item", "in_progress"),
        ("status", "cancelled", "complete_work_item", "cancelled"),
        ("complete", "", "complete_work_item", "done"),
        ("complete", "cancelled", "complete_work_item", "cancelled"),
    ],
    ids=["status-open", "status-ending", "complete-default", "complete-cancelled"],
)
async def test_an_ending_status_completes_the_item(monkeypatch, operation, status, service, expected_status):
    spies = {name: _Spy({"id": "w1", "status": expected_status}) for name in ("update_work_item", "complete_work_item")}
    for name, spy in spies.items():
        monkeypatch.setattr(f"app.services.work_item.{name}", spy)

    result = await _walker()._exec_update_work_item(
        {"work_item_id": str(uuid.uuid4()), "operation": operation, "status": status, "text": "shipped"}
    )

    assert result.error is None
    assert len(spies[service].calls) == 1
    other = "update_work_item" if service == "complete_work_item" else "complete_work_item"
    assert spies[other].calls == []
    assert spies[service].calls[0][1]["status"] == expected_status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"work_item_id": "not-an-id", "operation": "note", "text": "x"}, "The work item id is not a work item id."),
        ({"work_item_id": str(uuid.UUID(int=1)), "operation": "note", "text": "  "}, "The note is empty."),
        ({"work_item_id": str(uuid.UUID(int=1)), "operation": "status", "status": "lost"}, None),
        (
            {"work_item_id": str(uuid.UUID(int=1)), "operation": "status", "status": "blocked", "text": "waits"},
            "A flow cannot set a work item blocked: leave it open with a later not_before, or create the wait "
            "with blocked_on. Nothing was written.",
        ),
    ],
    ids=["bad-id", "empty-note", "unknown-status", "blocked-black-or-white"],
)
async def test_an_invalid_update_is_refused_before_the_service(monkeypatch, config, message):
    update = _Spy({"id": "w1", "status": "open"})
    monkeypatch.setattr("app.services.work_item.update_work_item", update)

    result = await _walker()._exec_update_work_item(config)

    assert result.error is not None
    if message is not None:
        assert result.error == message
    assert update.calls == []


def test_the_update_node_does_not_offer_blocked():
    from app.services.flow_catalog import WORK_ITEM_STATUS_OPTIONS

    offered = {o["value"] for o in WORK_ITEM_STATUS_OPTIONS}
    assert "blocked" not in offered
    assert {"open", "in_progress", "done", "cancelled"} <= offered


@pytest.mark.asyncio
async def test_an_item_the_owner_may_not_write_reads_as_not_found(monkeypatch):
    monkeypatch.setattr("app.services.work_item.update_work_item", _Spy(None))

    result = await _walker()._exec_update_work_item(
        {"work_item_id": str(uuid.uuid4()), "operation": "note", "text": "x"}
    )

    assert result.error == "No work item the flow's owner may change has this id."


# ── Hand to fleet ────────────────────────────────────────────────────────────


def _fleet(**fields):
    base = {
        "id": uuid.uuid4(),
        "name": "Blue team",
        "is_archived": False,
        "orchestrator_agent_id": uuid.uuid4(),
        "workspace_id": uuid.uuid4(),
    }
    return SimpleNamespace(**{**base, **fields})


def _fleet_walker(monkeypatch, fleet, orchestrator: Any = "enabled"):
    async def _groups(_db, _uid):
        return []

    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    if orchestrator == "enabled":
        orchestrator = SimpleNamespace(enabled=True)
    # The fleet, then (when it has one) its orchestrator agent.
    return _walker(db=_Db(answers=[fleet, orchestrator]))


@pytest.mark.asyncio
async def test_the_work_goes_to_the_orchestrator_in_the_fleets_workspace(monkeypatch):
    fleet = _fleet()
    create = _Spy({"id": "item-1"})
    monkeypatch.setattr("app.services.work_item.create_work_item", create)
    walker = _fleet_walker(monkeypatch, fleet)

    result = await walker._exec_dispatch_to_fleet(
        {"fleet_id": str(fleet.id), "title": "Triage CVE-2026-1", "priority": "normal", "correlation_key": "cve-1"}
    )

    assert result.error is None
    assert result.outputs == {"work_item_id": "item-1", "_tainted": False}
    (_args, kwargs), = create.calls
    assert kwargs["owner_agent_id"] == str(fleet.orchestrator_agent_id)
    assert kwargs["workspace_id"] == fleet.workspace_id
    assert kwargs["priority"] == "medium"
    assert kwargs["dedupe"] is True
    assert kwargs["source"] == "flow"
    assert kwargs["origin"].flow_run_id == walker.run.id
    assert kwargs["writer"] == flow_writer(is_company_managed=False, flow_id=walker.run.flow_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "orchestrator",
    [None, SimpleNamespace(enabled=False)],
    ids=["deleted", "disabled"],
)
async def test_a_fleet_whose_orchestrator_cannot_work_creates_nothing(monkeypatch, orchestrator):
    fleet = _fleet()
    create = _Spy({"id": "item-1"})
    monkeypatch.setattr("app.services.work_item.create_work_item", create)

    result = await _fleet_walker(monkeypatch, fleet, orchestrator)._exec_dispatch_to_fleet(
        {"fleet_id": str(fleet.id), "title": "Triage", "wait": True}
    )

    assert result.error == "The fleet 'Blue team' has no enabled orchestrator to hand work to: set one on the fleet."
    assert result.park is None
    assert create.calls == []


@pytest.mark.asyncio
async def test_waiting_on_the_fleet_parks_on_the_item(monkeypatch):
    fleet = _fleet()
    monkeypatch.setattr("app.services.work_item.create_work_item", _Spy({"id": "item-1"}))

    result = await _fleet_walker(monkeypatch, fleet)._exec_dispatch_to_fleet(
        {"fleet_id": str(fleet.id), "title": "Triage", "wait": True}
    )

    assert result.park is not None
    assert result.park["kind"] == "work_item"
    assert result.park["work_item_id"] == "item-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fleet",
    [None, _fleet(is_archived=True), _fleet(orchestrator_agent_id=None)],
    ids=["not-visible", "archived", "no-orchestrator"],
)
async def test_a_fleet_that_cannot_take_work_creates_nothing(monkeypatch, fleet):
    create = _Spy({"id": "item-1"})
    monkeypatch.setattr("app.services.work_item.create_work_item", create)

    result = await _fleet_walker(monkeypatch, fleet)._exec_dispatch_to_fleet(
        {"fleet_id": str(uuid.uuid4()), "title": "Triage"}
    )

    assert result.error is not None
    assert create.calls == []


@pytest.mark.asyncio
async def test_a_workspace_the_owner_may_not_write_is_an_error(monkeypatch):
    from app.services.shared_state import WorkspaceWriteRefusedError

    fleet = _fleet()
    monkeypatch.setattr(
        "app.services.work_item.create_work_item",
        _Spy(raises=WorkspaceWriteRefusedError("You do not have permission to create work items in that workspace")),
    )

    result = await _fleet_walker(monkeypatch, fleet)._exec_dispatch_to_fleet(
        {"fleet_id": str(fleet.id), "title": "Triage"}
    )

    assert result.error == "You do not have permission to create work items in that workspace"


# ── Add workspace source ─────────────────────────────────────────────────────


def _source_fakes(monkeypatch, *, manager: bool, existed: bool = True):
    workspace = SimpleNamespace(id=uuid.uuid4(), name="Exposure")
    kb = SimpleNamespace(id=uuid.uuid4())
    db = _Db()
    _own_session(monkeypatch, db)

    async def _groups(_db, _uid):
        return []

    async def _resolve(_db, _user, _ref, *, user_group_ids=None):
        return workspace

    async def _ensure(_db, _workspace, _role):
        return kb

    async def _existing(_db, _workspace_id, _role):
        return kb if existed else None

    spies = {
        "text": _Spy(SimpleNamespace(id=uuid.uuid4())),
        "url": _Spy(SimpleNamespace(id=uuid.uuid4())),
        "audit": _Spy(None),
        "enqueue": _Spy(True),
    }
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)
    monkeypatch.setattr("app.api.v1.helpers.can_manage", lambda _u, _w, _g: manager)
    monkeypatch.setattr("app.services.flow_runner.resolve_workspace_scope", _resolve)
    monkeypatch.setattr("app.services.workspaces.ensure_workspace_kb", _ensure)
    monkeypatch.setattr("app.services.workspaces.workspace_kb", _existing)
    monkeypatch.setattr("app.services.kb_documents.add_text_document", spies["text"])
    monkeypatch.setattr("app.services.kb_documents.add_url_document", spies["url"])
    monkeypatch.setattr("app.services.audit.log_audit", spies["audit"])
    monkeypatch.setattr("app.services.kb_helpers.enqueue_document_processing", spies["enqueue"])
    return db, workspace, kb, spies


@pytest.mark.asyncio
async def test_a_text_source_is_added_audited_and_queued(monkeypatch):
    db, workspace, kb, spies = _source_fakes(monkeypatch, manager=True)

    result = await _walker()._exec_workspace_add_source(
        {"workspace_id": str(workspace.id), "source_kind": "text", "title": "Brief", "content": "Scope: edge"}
    )

    assert result.error is None
    doc_id = str(spies["text"].result.id)
    assert result.outputs == {"document_id": doc_id, "knowledge_base_id": str(kb.id), "_tainted": False}
    (_args, kwargs), = spies["text"].calls
    assert kwargs == {"filename": "Brief", "content": "Scope: edge", "source_type": "text"}
    assert spies["audit"].calls[0][1]["entity_id"] == workspace.id
    # The knowledge base (its row lock released before the document), then the
    # document with its audit row.
    assert db.commits == 2
    assert spies["enqueue"].calls == [((doc_id,), {})]


@pytest.mark.asyncio
@pytest.mark.parametrize("existed", [True, False], ids=["existing-sources", "first-source"])
async def test_creating_the_sources_is_audited_once(monkeypatch, existed):
    _db, workspace, _kb, spies = _source_fakes(monkeypatch, manager=True, existed=existed)

    await _walker()._exec_workspace_add_source(
        {"workspace_id": str(workspace.id), "source_kind": "text", "content": "x"}
    )

    summaries = [call[1]["summary"] for call in spies["audit"].calls]
    created = "Created the sources of workspace 'Exposure' from a flow"
    assert summaries.count(created) == (0 if existed else 1)
    assert summaries[-1] == "Added a source to workspace 'Exposure' from a flow"


@pytest.mark.asyncio
async def test_a_reader_of_the_workspace_adds_nothing(monkeypatch):
    db, workspace, _kb, spies = _source_fakes(monkeypatch, manager=False)

    result = await _walker()._exec_workspace_add_source(
        {"workspace_id": str(workspace.id), "source_kind": "text", "content": "x"}
    )

    assert result.error == "The flow's owner can read this workspace but not edit its sources."
    assert spies["text"].calls == []
    assert spies["enqueue"].calls == []
    assert db.commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ["ftp://example.com/x", "javascript:alert(1)", "example.com", "file:///etc/passwd"],
    ids=["ftp", "javascript", "no-scheme", "file"],
)
async def test_a_web_source_needs_an_http_address(monkeypatch, content):
    _db, workspace, _kb, spies = _source_fakes(monkeypatch, manager=True)

    result = await _walker()._exec_workspace_add_source(
        {"workspace_id": str(workspace.id), "source_kind": "url", "content": content}
    )

    assert result.error == "A web page source needs an http(s) address."
    assert spies["url"].calls == []


@pytest.mark.asyncio
async def test_a_web_source_is_fetched_by_the_guarded_helper(monkeypatch):
    _db, workspace, _kb, spies = _source_fakes(monkeypatch, manager=True)

    result = await _walker()._exec_workspace_add_source(
        {"workspace_id": str(workspace.id), "source_kind": "url", "content": " https://example.com/a "}
    )

    assert result.error is None
    assert spies["url"].calls[0][1] == {"url": "https://example.com/a"}


# ── Reports and presentations ────────────────────────────────────────────────


def _document_tool(walker: FlowWalker, result: ToolResult) -> list[tuple[str, str, dict]]:
    calls: list[tuple[str, str, dict]] = []

    async def _run(suite, tool, arguments):
        calls.append((suite, tool, arguments))
        return result

    walker._run_document_tool = _run  # type: ignore[method-assign]
    return calls


@pytest.mark.asyncio
async def test_a_report_is_written_by_the_generate_report_tool(monkeypatch):
    monkeypatch.setattr("app.services.email.platform_url", lambda: "https://xtm.example")
    walker = _walker(outputs={"kpi": {"output": "42"}})
    report_id = str(uuid.uuid4())
    calls = _document_tool(
        walker, ToolResult(tool_call_id="", content="Created", structured={"report_id": report_id, "revision": 1})
    )

    result = await walker._exec_create_report(
        {
            "title": "Weekly",
            "header": {"title": "Weekly exposure"},
            "blocks": [{"block": "paragraph", "text": "Open findings: {{kpi.output}}"}],
            "theme": "dark",
        }
    )

    assert result.error is None
    assert result.outputs == {
        "report_id": report_id,
        "revision": 1,
        "url": f"https://xtm.example/reports/{report_id}",
        "_tainted": False,
    }
    assert calls == [
        (
            "report",
            "generate_report",
            {
                "title": "Weekly",
                "header": {"title": "Weekly exposure"},
                "blocks": [{"block": "paragraph", "text": "Open findings: 42"}],
                "theme": "dark",
            },
        )
    ]


@pytest.mark.asyncio
async def test_a_refused_spec_is_the_nodes_error():
    walker = _walker()
    _document_tool(walker, ToolResult(tool_call_id="", content="blocks[0].block is unknown", is_error=True))

    result = await walker._exec_create_report({"title": "W", "header": {}, "blocks": []})

    assert result.error == "blocks[0].block is unknown"


@pytest.mark.asyncio
async def test_the_generate_report_tool_answers_its_id_for_a_reader_without_a_model(monkeypatch):
    from app.services.builtin_tools.report import build_report_tools
    from app.tools.base import ToolCall

    report = SimpleNamespace(id=uuid.uuid4(), revision=3, spec={"title": "Weekly", "theme": "light", "blocks": []})
    _own_session(monkeypatch, _Db())
    monkeypatch.setattr("app.services.reports.save_report", _Spy(report))

    _defs, executor = build_report_tools(None, str(uuid.uuid4()))  # type: ignore[arg-type]
    result = await executor.execute_tool(
        ToolCall(id="1", name="generate_report", arguments={"title": "Weekly", "header": {"title": "W"}, "blocks": []})
    )

    assert result.is_error is False
    assert result.structured == {"report_id": str(report.id), "revision": 3}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "config", "expected_call"),
    [
        ("action.export_report", {"report_id": "{id}"}, ("report", "export_report", {"report_id": "{id}"})),
        (
            "action.export_presentation",
            {"presentation_id": "{id}", "format": "pptx_native"},
            ("presentation", "export_presentation", {"presentation_id": "{id}", "format": "pptx_native"}),
        ),
    ],
    ids=["report", "presentation"],
)
async def test_an_export_publishes_the_stored_file(kind, config, expected_call):
    doc_id = str(uuid.uuid4())
    walker = _walker()
    stored = SimpleNamespace(file_id="file-9", filename="weekly.pdf")
    calls = _document_tool(walker, ToolResult(tool_call_id="", content="Exported", files=[stored]))
    config = {k: v.replace("{id}", doc_id) for k, v in config.items()}

    result = await walker._exec_export_document(kind, config)

    assert result.error is None
    assert result.outputs == {"file_id": "file-9", "filename": "weekly.pdf", "_tainted": False}
    suite, tool, arguments = expected_call
    assert calls == [(suite, tool, {k: v.replace("{id}", doc_id) for k, v in arguments.items()})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [
        ToolResult(tool_call_id="", content="No presentation you can access was found.", is_error=True),
        ToolResult(tool_call_id="", content="Exported", files=[]),
    ],
    ids=["refused", "no-file"],
)
async def test_an_export_without_a_file_is_an_error(result):
    walker = _walker()
    _document_tool(walker, result)

    outcome = await walker._exec_export_document(
        "action.export_presentation", {"presentation_id": str(uuid.uuid4())}
    )

    assert outcome.error is not None
    assert outcome.outputs == {}


@pytest.mark.asyncio
async def test_an_export_of_no_id_never_reaches_the_tool():
    walker = _walker()
    calls = _document_tool(walker, ToolResult(tool_call_id="", content="Exported"))

    result = await walker._exec_export_document("action.export_report", {"report_id": "latest"})

    assert result.error == "The id to export is not a document id."
    assert calls == []


# ── Render video ─────────────────────────────────────────────────────────────


def _render_fakes(monkeypatch, *, enabled=True, project=True, manager=True, started=None, raises=None):
    from app.services.video_studio import projects

    db = _Db()
    _own_session(monkeypatch, db)
    monkeypatch.setattr("app.core.feature_flags.is_feature_enabled", lambda _flag: enabled)
    monkeypatch.setattr("app.services.email.platform_url", lambda: "https://xtm.example")
    found = SimpleNamespace(id=uuid.uuid4()) if project else None

    async def _load(_db, _id, _user):
        return found

    async def _can(_db, _project, _user):
        return manager

    start = _Spy(started, raises=raises)
    ran = _Spy(None)
    monkeypatch.setattr(projects, "load_visible_project", _load)
    monkeypatch.setattr(projects, "can_manage_project", _can)
    monkeypatch.setattr(projects, "start_render", start)
    monkeypatch.setattr(projects, "run_started", ran)
    return db, start, ran


def _render(status="pending", **fields):
    return SimpleNamespace(id=uuid.uuid4(), status=status, file_id=fields.get("file_id"), error=fields.get("error"))


@pytest.mark.asyncio
async def test_a_render_is_started_committed_and_waited_for(monkeypatch):
    render = _render("pending")
    db, start, ran = _render_fakes(monkeypatch, started=SimpleNamespace(render=render))
    video_id = str(uuid.uuid4())

    result = await _walker()._exec_render_video({"video_id": video_id, "quality": "standard"})

    assert result.park is not None
    assert result.park["kind"] == "video_render"
    assert result.park["render_id"] == str(render.id)
    assert start.calls[0][1] == {"quality": "standard"}
    assert db.commits == 1
    assert len(ran.calls) == 1


@pytest.mark.asyncio
async def test_a_render_that_already_covers_the_revision_answers_at_once(monkeypatch):
    render = _render("ready", file_id=uuid.uuid4())
    _render_fakes(monkeypatch, started=SimpleNamespace(render=render))
    video_id = str(uuid.uuid4())

    result = await _walker()._exec_render_video({"video_id": video_id})

    assert result.park is None
    assert result.next_handles == ["ok"]
    assert result.outputs["file_id"] == str(render.file_id)
    assert result.outputs["url"] == f"https://xtm.example/videos/{video_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fakes", "config", "error"),
    [
        ({"enabled": False}, {}, "The Video Studio is turned off on this platform."),
        ({}, {"quality": "8k"}, "The quality must be one of: draft, standard, high, ultra."),
        ({"project": False}, {}, "No video the flow's owner may render has this id."),
        ({"manager": False}, {}, "No video the flow's owner may render has this id."),
    ],
    ids=["flag-off", "bad-quality", "not-visible", "read-only"],
)
async def test_a_render_that_may_not_start_takes_the_fail_pin(monkeypatch, fakes, config, error):
    _db, start, _ran = _render_fakes(monkeypatch, started=SimpleNamespace(render=_render()), **fakes)

    result = await _walker()._exec_render_video({"video_id": str(uuid.uuid4()), **config})

    assert result.next_handles == ["fail"]
    assert result.outputs["status"] == "refused"
    assert result.outputs["error"] == error
    assert start.calls == []


@pytest.mark.asyncio
async def test_a_gated_render_names_every_reason(monkeypatch):
    from app.services.video_studio.render_checks import RenderGateError

    gate = RenderGateError.__new__(RenderGateError)
    gate.reasons = ["scene 2 changed since its preview", "an AI shot is not ready"]
    db, _start, ran = _render_fakes(monkeypatch, raises=gate)

    result = await _walker()._exec_render_video({"video_id": str(uuid.uuid4())})

    assert result.next_handles == ["fail"]
    assert result.outputs["error"] == (
        "The render was refused: scene 2 changed since its preview; an AI shot is not ready"
    )
    assert db.rollbacks == 1
    assert ran.calls == []


@pytest.mark.parametrize(
    ("status", "handle"),
    [("ready", "ok"), ("failed", "fail"), ("cancelled", "fail")],
)
def test_an_ended_render_takes_ok_only_when_ready(monkeypatch, status, handle):
    monkeypatch.setattr("app.services.email.platform_url", lambda: "https://xtm.example")
    render = _render(status, file_id=uuid.uuid4(), error="voice-over heard wrong" if status == "failed" else None)

    result = FlowWalker._video_render_result(render, {"video_id": "v1", "tainted": False})

    assert result.next_handles == [handle]
    # A failed render may still carry its MP4: published either way.
    assert result.outputs["file_id"] == str(render.file_id)
    assert result.outputs["status"] == status
    expected_error = {"ready": "", "failed": "voice-over heard wrong", "cancelled": "The render ended cancelled."}
    assert result.outputs["error"] == expected_error[status]


# ── Deep investigation ───────────────────────────────────────────────────────


def _investigation_fakes(monkeypatch, *, enabled=True, results=None, text="Report [1]"):
    _own_session(monkeypatch, _Db())
    monkeypatch.setattr("app.core.feature_flags.is_feature_enabled", lambda _flag: enabled)
    calls: list[dict] = []

    async def _turn(_db, **kwargs):
        calls.append(kwargs)
        if results is not None:
            kwargs["result_out"].extend(results)
        return text

    monkeypatch.setattr("app.services.deep_investigation_chat.run_deep_investigation_turn", _turn)
    return calls


@pytest.mark.asyncio
async def test_a_completed_investigation_publishes_its_report_as_tainted(monkeypatch):
    from app.services.investigation_events import build_investigation_marker

    investigation = SimpleNamespace(id=uuid.uuid4(), status="completed")
    calls = _investigation_fakes(
        monkeypatch,
        results=[SimpleNamespace(investigation=investigation)],
        text=f"APT29 uses [1].\n{build_investigation_marker(investigation.id)}",
    )
    walker = _walker()

    result = await walker._exec_deep_investigation({"subject": "APT29", "source_packs": '["web"]'})

    assert result.error is None
    assert result.outputs == {
        "investigation_id": str(investigation.id),
        "report": "APT29 uses [1].",
        "_tainted": True,
    }
    assert calls[0]["subject"] == "APT29"
    assert calls[0]["user_id"] == walker.user.id
    assert calls[0]["source_pack_slugs"] == ["web"]
    assert calls[0]["cancel_event"] is walker.cancel_event


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fakes", "config", "error"),
    [
        ({"enabled": False}, {"subject": "APT29"}, "Deep Investigation is turned off on this platform."),
        ({}, {"subject": "  "}, "The investigation needs a subject."),
        ({}, {"subject": "APT29", "source_packs": '{"web": 1}'}, "Source packs must be a JSON list of pack slugs."),
        ({}, {"subject": "APT29", "source_packs": "[web"}, "Source packs must be a JSON list of pack slugs."),
    ],
    ids=["flag-off", "no-subject", "not-a-list", "not-json"],
)
async def test_an_investigation_that_may_not_start_never_runs(monkeypatch, fakes, config, error):
    calls = _investigation_fakes(monkeypatch, **fakes)

    result = await _walker()._exec_deep_investigation(config)

    assert result.error == error
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("results", "error"),
    [
        (None, "Your investigation quota is used up."),
        (
            [SimpleNamespace(investigation=SimpleNamespace(id=uuid.uuid4(), status="aborted"))],
            "The investigation ended `aborted`: Your investigation quota is used up.",
        ),
    ],
    ids=["refused", "aborted"],
)
async def test_an_investigation_that_did_not_complete_is_an_error(monkeypatch, results, error):
    _investigation_fakes(monkeypatch, results=results, text="Your investigation quota is used up.")

    result = await _walker()._exec_deep_investigation({"subject": "APT29"})

    assert result.error == error
    assert result.outputs == {}


# ── Platform-event trigger ───────────────────────────────────────────────────

OWNER = uuid.uuid4()
AGENT = str(uuid.uuid4())


@pytest.mark.parametrize(
    ("payload", "agent_ids", "scope_all", "expected"),
    [
        ({"personal_owner_id": str(OWNER)}, set(), False, True),
        ({"personal_owner_id": str(uuid.uuid4())}, {AGENT}, False, False),
        ({"personal_owner_ids": [str(OWNER), str(uuid.uuid4())]}, set(), False, False),
        ({"participant_agent_ids": [AGENT]}, {AGENT}, False, True),
        ({"participant_agent_ids": [str(uuid.uuid4())]}, {AGENT}, False, False),
        ({}, set(), False, False),
        ({"personal_owner_id": str(uuid.uuid4())}, set(), True, True),
    ],
    ids=[
        "own-personal-object",
        "someone-elses-personal-object",
        "shared-between-owners",
        "agent-takes-part",
        "agent-not-involved",
        "nothing-ties-it",
        "admin-every-object",
    ],
)
def test_the_flow_scope_mirrors_the_assignment_scope(payload, agent_ids, scope_all, expected):
    assert (
        flow_triggers.platform_event_in_flow_scope(
            owner_id=OWNER, agent_ids=agent_ids, payload=payload, scope_all=scope_all
        )
        is expected
    )


def _flow(*, events=("video.render.finished",), filter_=None, scope="own", graph_extra=()):
    node = {
        "id": "t1",
        "kind": PLATFORM_EVENT_TRIGGER_KIND,
        "config": {"events": list(events), "filter": filter_ or {}, "scope": scope},
    }
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=OWNER,
        name="On render",
        graph={"nodes": [node, *graph_extra], "edges": []},
    ), node


def _dispatch_fakes(
    monkeypatch, flow, node, *, role="user", seen=False, held=False, readable=(), manageable=None
):
    db = _Db(role)
    _own_session_triggers(monkeypatch, db)

    async def _targets(_db, kind):
        assert kind == PLATFORM_EVENT_TRIGGER_KIND
        return [(flow, [node])]

    async def _seen(_db, _flow_id, _node_id, _event_id):
        return seen

    async def _held(_db, _flow, **_kw):
        return held

    async def _tiers(_db, _user_id):
        return list(readable), []

    async def _nothing(*_a, **_kw):
        return None

    async def _manageable(_db, _owner_id, _is_admin, agent_ids):
        # Every agent node manageable unless the test names the ones that are.
        return set(agent_ids) if manageable is None else set(agent_ids) & set(manageable)

    create = _Spy(SimpleNamespace(id=uuid.uuid4()))
    monkeypatch.setattr(flow_triggers, "_manageable_agent_ids", _manageable)
    monkeypatch.setattr(flow_triggers, "_active_flows_with_trigger", _targets)
    monkeypatch.setattr(flow_triggers, "_platform_event_seen", _seen)
    monkeypatch.setattr(flow_triggers, "hold_back_if_ai_required", _held)
    monkeypatch.setattr(flow_triggers, "_lock_flow_run_admission", _nothing)
    monkeypatch.setattr(flow_triggers, "create_flow_run", create)
    monkeypatch.setattr("app.services.workspaces.workspace_scope_tiers", _tiers)
    monkeypatch.setattr("app.services.orchestration_engine.notify_orchestration_wake", _nothing)
    return db, create


def _own_session_triggers(monkeypatch, db: _Db) -> None:
    @asynccontextmanager
    async def _session():
        yield db

    monkeypatch.setattr(flow_triggers, "async_session", _session)


def _video_event(**fields):
    return {
        "event": "video.render.finished",
        "event_id": "ev-1",
        "status": "ready",
        "file_id": "f-1",
        "personal_owner_id": str(OWNER),
        "participant_agent_ids": [],
        "object_type": "video_render",
        "object_id": "r-1",
        **fields,
    }


@pytest.mark.asyncio
async def test_a_subscribed_flow_runs_once_with_the_event_as_its_payload(monkeypatch):
    flow, node = _flow()
    db, create = _dispatch_fakes(monkeypatch, flow, node)
    payload = _video_event()

    outcomes = await flow_triggers.dispatch_flow_platform_event("video.render.finished", payload)

    assert len(create.calls) == 1
    args, kwargs = create.calls[0]
    assert args[1] is flow
    assert kwargs["trigger_type"] == "platform_event"
    assert kwargs["trigger_payload"] == {"payload": payload, "event": "video.render.finished", "event_id": "ev-1"}
    assert kwargs["trigger_context"]["trigger_node_id"] == "t1"
    assert kwargs["trigger_context"]["event_id"] == "ev-1"
    assert kwargs["user_id"] == OWNER
    assert db.commits == 1
    assert outcomes == [f"flow {flow.id} run {create.result.id}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flow_kwargs", "fakes", "payload_fields", "name"),
    [
        ({"events": ("video.render.failed",)}, {}, {}, "video.render.finished"),
        ({"filter_": {"status": "failed"}}, {}, {}, "video.render.finished"),
        ({}, {}, {"personal_owner_id": str(uuid.uuid4())}, "video.render.finished"),
        ({"scope": "all"}, {"role": "user"}, {"personal_owner_id": str(uuid.uuid4())}, "video.render.finished"),
        ({}, {"seen": True}, {}, "video.render.finished"),
        ({}, {"held": True}, {}, "video.render.finished"),
        ({}, {"readable": ()}, {"workspace_id": str(uuid.uuid4())}, "video.render.finished"),
        ({}, {}, {"event_id": ""}, "video.render.finished"),
    ],
    ids=[
        "other-event",
        "filtered-out",
        "another-users-object",
        "all-scope-needs-an-admin",
        "already-delivered",
        "ai-held-back",
        "unreadable-workspace",
        "no-event-id",
    ],
)
async def test_an_event_the_flow_must_not_take_starts_nothing(monkeypatch, flow_kwargs, fakes, payload_fields, name):
    flow, node = _flow(**flow_kwargs)
    _db, create = _dispatch_fakes(monkeypatch, flow, node, **fakes)

    await flow_triggers.dispatch_flow_platform_event(name, _video_event(**payload_fields))

    assert create.calls == []


def _task_event(agent_id: str) -> dict:
    """A background task's end: no personal owner, its agents the only scope."""
    return {
        "event_id": "ev-task",
        "task_id": str(uuid.uuid4()),
        "agent_id": agent_id,
        "outcome_preview": "the task's result",
        "participant_agent_ids": [agent_id],
        "object_type": "background_task",
        "object_id": "t-1",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("manageable", [True, False], ids=["owner-manages-the-agent", "owner-only-sees-it"])
async def test_an_agent_node_scopes_events_only_for_an_owner_who_manages_it(monkeypatch, manageable):
    agent_node = {"id": "ag", "kind": "agent", "config": {"agent_id": AGENT}}
    flow, node = _flow(events=("background_task.completed",), graph_extra=(agent_node,))
    _db, create = _dispatch_fakes(monkeypatch, flow, node, manageable={AGENT} if manageable else set())

    await flow_triggers.dispatch_flow_platform_event("background_task.completed", _task_event(AGENT))

    assert len(create.calls) == (1 if manageable else 0)


def _agent_row(**fields):
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "is_company_managed": False,
        "shared_user_ids": [],
        "shared_user_permissions": {},
        "shared_group_ids": [],
        "shared_group_permissions": {},
    }
    return SimpleNamespace(**{**base, **fields})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make", "is_admin", "manages"),
    [
        (lambda me, grp: _agent_row(user_id=me), False, True),
        (lambda me, grp: _agent_row(shared_user_ids=[str(me)], shared_user_permissions={str(me): "config"}), False, True),
        (lambda me, grp: _agent_row(shared_group_ids=[grp], shared_group_permissions={grp: "config"}), False, True),
        (lambda me, grp: _agent_row(shared_user_ids=[str(me)], shared_user_permissions={str(me): "read_only"}), False, False),
        (lambda me, grp: _agent_row(shared_group_ids=[grp], shared_group_permissions={grp: "read_only"}), False, False),
        (lambda me, grp: _agent_row(), False, False),
        (lambda me, grp: _agent_row(is_company_managed=True), False, False),
        (lambda me, grp: _agent_row(is_company_managed=True), True, True),
    ],
    ids=[
        "own",
        "user-config-grant",
        "group-config-grant",
        "user-read-only",
        "group-read-only",
        "share-revoked",
        "company-managed-member",
        "company-managed-admin",
    ],
)
async def test_the_manage_rule_decides_which_agent_nodes_scope_events(monkeypatch, make, is_admin, manages):
    owner, group = uuid.uuid4(), str(uuid.uuid4())
    agent = make(owner, group)

    async def _groups(_db, _uid):
        return [group]

    class _AgentsDb:
        async def execute(self, _statement):
            return _Rows(rows=[agent])

    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _groups)

    managed = await flow_triggers._manageable_agent_ids(_AgentsDb(), owner, is_admin, {str(agent.id)})

    assert managed == ({str(agent.id)} if manages else set())


@pytest.mark.asyncio
async def test_a_flow_never_hears_the_events_its_own_writes_raise(monkeypatch):
    flow, node = _flow(events=("work_item.note_added",))
    _db, create = _dispatch_fakes(monkeypatch, flow, node)
    event = _video_event(event="work_item.note_added", object_type="work_item")

    await flow_triggers.dispatch_flow_platform_event("work_item.note_added", {**event, "actor_flow_id": str(flow.id)})
    assert create.calls == []

    await flow_triggers.dispatch_flow_platform_event(
        "work_item.note_added", {**event, "actor_flow_id": str(uuid.uuid4())}
    )
    assert len(create.calls) == 1


def test_a_flow_nodes_write_names_the_flow_on_its_events():
    from app.services.work_item import update_events

    flow_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), status="in_progress", user_id=OWNER, is_company_managed=False, workspace_id=None)

    events = update_events(
        item,
        previous_status="open",
        note="half done",
        writer=flow_writer(is_company_managed=False, flow_id=flow_id),
    )

    assert [name for name, _data in events] == ["work_item.note_added", "work_item.status_changed"]
    assert {data["actor_flow_id"] for _name, data in events} == {str(flow_id)}


@pytest.mark.asyncio
@pytest.mark.parametrize("fakes", [{"seen": True}, {"held": True}], ids=["duplicate", "held-back"])
async def test_a_skipped_delivery_releases_the_admission_lock(monkeypatch, fakes):
    flow, node = _flow()
    db, create = _dispatch_fakes(monkeypatch, flow, node, **fakes)

    await flow_triggers.dispatch_flow_platform_event("video.render.finished", _video_event())

    assert create.calls == []
    assert db.commits == 1


@pytest.mark.asyncio
async def test_an_admins_every_object_scope_hears_another_users_object(monkeypatch):
    flow, node = _flow(scope="all")
    _db, create = _dispatch_fakes(monkeypatch, flow, node, role="admin")

    await flow_triggers.dispatch_flow_platform_event(
        "video.render.finished", _video_event(personal_owner_id=str(uuid.uuid4()))
    )

    assert len(create.calls) == 1


@pytest.mark.asyncio
async def test_a_flow_never_wakes_on_the_end_of_an_assignment_it_triggers(monkeypatch):
    assignment_id = str(uuid.uuid4())
    trigger_node = {"id": "a1", "kind": "action.trigger_assignment", "config": {"assignment_id": assignment_id}}
    agent_node = {"id": "ag", "kind": "agent", "config": {"agent_id": AGENT}}
    flow, node = _flow(events=("run.completed",), graph_extra=(trigger_node, agent_node))
    _db, create = _dispatch_fakes(monkeypatch, flow, node)
    event = {"event_id": "ev-2", "assignment_id": assignment_id, "participant_agent_ids": [AGENT]}

    await flow_triggers.dispatch_flow_platform_event("run.completed", event)
    assert create.calls == []

    await flow_triggers.dispatch_flow_platform_event(
        "run.completed", {**event, "assignment_id": str(uuid.uuid4())}
    )
    assert len(create.calls) == 1


@pytest.mark.asyncio
async def test_a_deactivated_owner_is_skipped_without_failing_the_dispatch(monkeypatch):
    from app.services.assignment_pause import FlowOwnerDeactivatedError

    flow, node = _flow()
    db, _create = _dispatch_fakes(monkeypatch, flow, node)
    monkeypatch.setattr(flow_triggers, "create_flow_run", _Spy(raises=FlowOwnerDeactivatedError(flow)))

    outcomes = await flow_triggers.dispatch_flow_platform_event("video.render.finished", _video_event())

    assert outcomes == []
    # Committed to release the flow's admission lock; nothing was added.
    assert db.commits == 1


@pytest.mark.asyncio
async def test_the_platform_dispatcher_hands_every_event_to_the_flows(monkeypatch):
    from app.services import platform_events

    @asynccontextmanager
    async def _session():
        yield _Db()

    async def _no_subscriptions(_db, _name):
        return []

    async def _no_watches(_name, _payload):
        return []

    seen: list[tuple[str, dict]] = []

    async def _flows(name, payload):
        seen.append((name, payload))
        return ["flow x run y"]

    monkeypatch.setattr("app.core.database.async_session", _session)
    monkeypatch.setattr(platform_events, "_subscriptions", _no_subscriptions)
    monkeypatch.setattr("app.services.platform_event_task_watches.dispatch_event", _no_watches)
    monkeypatch.setattr(flow_triggers, "dispatch_flow_platform_event", _flows)

    outcomes = await platform_events.dispatch_event("video.render.finished", {"event_id": "ev-1"})

    assert seen == [("video.render.finished", {"event_id": "ev-1"})]
    assert outcomes == ["flow x run y"]


@pytest.mark.asyncio
async def test_unreadable_assignment_subscriptions_still_reach_the_flows(monkeypatch):
    from app.services import platform_events

    @asynccontextmanager
    async def _session():
        yield _Db()

    async def _unreadable(_db, _name):
        raise RuntimeError("lock timeout")

    async def _no_watches(_name, _payload):
        return []

    async def _flows(_name, _payload):
        return ["flow x run y"]

    monkeypatch.setattr("app.core.database.async_session", _session)
    monkeypatch.setattr(platform_events, "_subscriptions", _unreadable)
    monkeypatch.setattr("app.services.platform_event_task_watches.dispatch_event", _no_watches)
    monkeypatch.setattr(flow_triggers, "dispatch_flow_platform_event", _flows)

    assert await platform_events.dispatch_event("video.render.finished", {"event_id": "ev-1"}) == ["flow x run y"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("waiting", "cancelled"),
    [({"kind": "video_render", "render_id": str(uuid.UUID(int=7))}, True), ({"kind": "delay"}, False), (None, False)],
    ids=["waiting-on-a-render", "waiting-on-something-else", "not-waiting"],
)
async def test_a_given_up_run_cancels_the_render_it_waits_on(monkeypatch, waiting, cancelled):
    from app.services import flow_runner
    from app.services.video_studio import jobs

    render = SimpleNamespace(id=uuid.UUID(int=7), status="running")
    cancels = _Spy(True)
    monkeypatch.setattr(jobs, "cancel", cancels)

    async def _give_up(_db, _run_id, _reason):
        return None

    monkeypatch.setattr("app.services.runner_transport.give_up_flow_run_commands", _give_up)

    class _GetDb:
        async def get(self, _model, ident):
            return render if ident == render.id else None

    run = SimpleNamespace(id=uuid.uuid4(), state={"waiting": waiting} if waiting else {})
    await flow_runner.release_runner_commands(_GetDb(), run, reason="The flow run was cancelled.")

    expected = [(("render", render), {"reason": "The flow run was cancelled."})] if cancelled else []
    assert [(args[1:], kwargs) for args, kwargs in cancels.calls] == expected

