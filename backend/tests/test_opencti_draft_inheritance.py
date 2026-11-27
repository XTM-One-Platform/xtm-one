"""A delegated run inherits the OpenCTI draft its parent works in (#2688).

The draft a run activates (``use_opencti_draft``) or creates lives in the
request headers of its tool build. The build mirrors it into the run's
``scope_headers``, the dict every delegation path (consult, background task,
transfer) forwards, so the child's build starts in the same draft. A read that
finds nothing says where it looked instead of a bare "not found".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.integrations.providers.opencti import build_tools
from app.integrations.providers.opencti.draft_scope import (
    DRAFT_HEADER,
    DraftScopedHeaders,
    not_found_message,
)
from app.tools.base import CallableToolExecutor, ToolCall

TOKEN = "https://demo.opencti.io||test-token"
BACKEND = Path(__file__).resolve().parents[1]


async def _run(executor, name, arguments):
    return await executor.execute_tool(ToolCall(id=f"call-{name}", name=name, arguments=arguments))


# ── The headers mirror ───────────────────────────────────────────────────


def test_draft_header_written_is_mirrored_into_the_run_scope():
    scope: dict[str, str] = {}
    headers = DraftScopedHeaders({"Authorization": "Bearer t"}, run_scope=scope)
    headers[DRAFT_HEADER] = "draft-1"
    assert scope == {DRAFT_HEADER: "draft-1"}
    headers[DRAFT_HEADER] = "draft-2"
    assert scope == {DRAFT_HEADER: "draft-2"}


def test_draft_header_dropped_leaves_the_run_scope():
    scope = {DRAFT_HEADER: "draft-1", "other": "kept"}
    headers = DraftScopedHeaders({DRAFT_HEADER: "draft-1"}, run_scope=scope)
    headers.pop(DRAFT_HEADER, None)
    assert scope == {"other": "kept"}
    headers[DRAFT_HEADER] = "draft-2"
    del headers[DRAFT_HEADER]
    assert DRAFT_HEADER not in scope


def test_bearer_and_other_headers_never_reach_the_run_scope():
    scope: dict[str, str] = {}
    headers = DraftScopedHeaders({}, run_scope=scope)
    headers["Authorization"] = "Bearer secret"
    headers.update({"Content-Type": "application/json"})
    assert scope == {}
    headers.update({DRAFT_HEADER: "draft-3"})
    assert scope == {DRAFT_HEADER: "draft-3"}


def test_no_run_scope_behaves_as_plain_headers():
    headers = DraftScopedHeaders({}, run_scope=None)
    headers[DRAFT_HEADER] = "draft-1"
    assert headers[DRAFT_HEADER] == "draft-1"


# ── The provider build ───────────────────────────────────────────────────


async def test_use_opencti_draft_reaches_a_child_build_through_the_scope(monkeypatch):
    """Parent activates draft D; a child built from the forwarded scope reads in D."""
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    seen_drafts: list[str | None] = []

    async def _fake_graphql(client, base_url, headers, query, variables=None, **_kw):
        if "draftWorkspace(" in query or "draftWorkspace (" in query:
            return {"draftWorkspace": {"id": "draft-D", "name": "hunt", "draft_status": "open"}}, None
        seen_drafts.append(headers.get(DRAFT_HEADER))
        if headers.get(DRAFT_HEADER) == "draft-D":
            return {
                "stixCoreObject": {
                    "id": "ind-1",
                    "entity_type": "Indicator",
                    "name": "draft-only indicator",
                }
            }, None
        return {"stixCoreObject": None}, None

    monkeypatch.setattr(knowledge_mod, "_graphql", _fake_graphql)

    run_scope: dict[str, str] = {}
    parent = CallableToolExecutor()
    build_tools(TOKEN, parent, scope_headers=run_scope)
    await _run(parent, "use_opencti_draft", {"draft_id": "draft-D"})
    assert run_scope.get(DRAFT_HEADER) == "draft-D"

    # A delegation forwards the run's scope: the child's own copy of it.
    child_scope = json.loads(json.dumps(run_scope))
    child = CallableToolExecutor()
    build_tools(TOKEN, child, scope_headers=child_scope)
    result = await _run(child, "get_opencti_entity", {"entity_id": "ind-1"})

    assert not result.is_error, result.content
    assert "draft-only indicator" in result.content
    assert seen_drafts[-1] == "draft-D"


async def test_child_of_a_parent_with_no_draft_is_unchanged(monkeypatch):
    run_scope: dict[str, str] = {}
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, scope_headers=run_scope)
    assert run_scope == {}


async def test_ui_draft_in_the_scope_is_the_build_draft():
    run_scope = {DRAFT_HEADER: "ui-draft"}
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, scope_headers=run_scope)
    assert run_scope == {DRAFT_HEADER: "ui-draft"}


# ── The not-found answer ─────────────────────────────────────────────────


def test_not_found_without_a_draft_names_the_draft_possibility():
    message = not_found_message("Entity 'x'", {})
    assert "not found in the live data" in message
    assert "draft that is " in message and "not active in this run" in message
    # A run started in a UI draft has no use_opencti_draft: the answer never names it.
    assert "use_opencti_draft" not in message


def test_not_found_in_a_draft_names_the_draft():
    message = not_found_message("Entity 'x'", {DRAFT_HEADER: "draft-D"})
    assert "active OpenCTI draft draft-D" in message


async def test_get_entity_miss_is_never_bare(monkeypatch):
    import app.integrations.providers.opencti.knowledge as knowledge_mod

    async def _fake_graphql(client, base_url, headers, query, variables=None, **_kw):
        return {"stixCoreObject": None}, None

    monkeypatch.setattr(knowledge_mod, "_graphql", _fake_graphql)
    executor = CallableToolExecutor()
    build_tools(TOKEN, executor, scope_headers={})
    result = await _run(executor, "get_opencti_entity", {"entity_id": "ind-404"})
    assert "not found in the live data" in result.content
    assert result.content != "Entity 'ind-404' not found."


# ── The delegation paths forward the run's scope ─────────────────────────


@pytest.mark.parametrize(
    ("path", "needles"),
    [
        # Consult and transfer of an assignment run, and its background tasks.
        (
            "app/services/assignment_engine.py",
            ['"scope_headers": _run_scope_headers', "scope_headers=_run_scope_headers"],
        ),
        # A chat turn copies its scope once, then its consult, transfer and
        # background-task paths forward that dict.
        ("app/services/agent_chat.py", ["scope_headers = dict(scope_headers) if scope_headers else {}"]),
    ],
)
def test_delegation_paths_forward_the_run_scope(path, needles):
    source = (BACKEND / path).read_text(encoding="utf-8")
    for needle in needles:
        assert needle in source, f"{path} no longer forwards the run scope: {needle!r}"


def test_assignment_run_forwards_its_scope_on_consult_transfer_and_tasks():
    source = (BACKEND / "app/services/assignment_engine.py").read_text(encoding="utf-8")
    # The consult, the transfer and the background-task builder: three forwards.
    assert source.count("scope_headers=_run_scope_headers") == 3


def test_converter_persona_never_converts_an_unread_entity():
    persona = (BACKEND / "app/agents/seed_data/agent_persona/opencti-detection-rule-converter.md").read_text(
        encoding="utf-8"
    )
    assert "Read before converting" in persona
    assert "error entry for that id" in persona
