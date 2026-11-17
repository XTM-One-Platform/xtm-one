"""``get_shared_state(to_workspace_path)``: a large value lands in the workspace, not the context.

A shared state value of a few hundred thousand characters (a gap register)
could only be paged through the context 50,000 characters at a time. The read
is now a ``workspace_ingest`` tool: with ``to_workspace_path`` its selection
(the whole value, or ``path`` / ``where_json`` / ``fields`` / ``item_*``) is
written whole as JSON, and the answer names the path, size and sha256 only.
Synthetic fixtures: the service is monkeypatched, the workspace store is the
in-memory one.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
from app.services.builtin_tools import build_shared_state_tools
from app.services.sandbox_workspace import local
from app.services.sandbox_workspace import session as session_module
from app.services.sandbox_workspace.local import BlobCache
from app.services.sandbox_workspace.store import MemoryWorkspaceStore
from app.tools.base import WORKSPACE_PATH_ARGUMENT, CompositeToolExecutor, FileContext, ToolCall, capturing_ingest

MB = 1024 * 1024


def _big_state() -> dict:
    gaps = [
        {"id": f"gap-{i:04d}", "status": "open" if i % 3 else "merged", "evidence": "x" * 1_000, "count": i}
        for i in range(300)
    ]
    return {
        "key": "dev:harness-gaps",
        "label": "Gaps",
        "value": {"gaps": gaps, "meta": {"owner": "dev", "families": [{"id": "f1"}, {"id": "f2"}]}},
        "notes": [{"at": "2026-11-11T00:00:00+00:00", "note": "a note that must not land in the file"}],
    }


@pytest.fixture
def state(monkeypatch) -> dict:
    value = _big_state()

    async def _get(**_kwargs):
        return json.loads(json.dumps(value))

    monkeypatch.setattr(shared_state_service, "get_state", _get)
    return value


@pytest.fixture
def store(monkeypatch, tmp_path) -> MemoryWorkspaceStore:
    from app.core.config import settings

    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    monkeypatch.setattr(settings, "code_interpreter_workspace_enabled", True)
    monkeypatch.setattr(local, "_cache", BlobCache(root=str(tmp_path / "cache"), max_bytes=64 * MB))
    memory = MemoryWorkspaceStore()
    monkeypatch.setattr(session_module, "default_store", lambda: memory)
    return memory


def _tools():
    return build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))


async def _export(**arguments):
    """The tool's answer as the dispatcher sees it when the content goes to the workspace."""
    _, executor = _tools()
    with capturing_ingest():
        return await executor.execute_tool(ToolCall(id="c", name="get_shared_state", arguments=arguments))


def test_get_shared_state_is_a_workspace_ingest_tool_and_the_writes_are_not():
    definitions, _ = _tools()
    by_name = {d.name: d for d in definitions}
    assert by_name["get_shared_state"].workspace_ingest
    assert not by_name["set_shared_state"].workspace_ingest


@pytest.mark.asyncio
async def test_the_whole_value_is_exported_as_plain_json_without_window_or_notes(state):
    result = await _export(key="dev:harness-gaps")
    assert not result.is_error, result.content[:300]
    assert json.loads(result.content) == state["value"]
    assert len(result.content) > 300_000  # past any windowed read
    assert "UNTRUSTED" not in result.content and "a note that must not land" not in result.content


@pytest.mark.asyncio
async def test_path_where_and_fields_select_what_is_exported(state):
    gaps = json.loads((await _export(key="k", path="gaps")).content)
    assert len(gaps) == 300
    merged = json.loads(
        (await _export(key="k", path="gaps", where_json='{"status": "merged"}', fields=["id", "count"])).content
    )
    assert merged == [{"id": g["id"], "count": g["count"]} for g in state["value"]["gaps"] if g["status"] == "merged"]
    page = json.loads((await _export(key="k", path="gaps", item_offset=10, item_limit=5, fields=["id"])).content)
    assert page == [{"id": f"gap-{i:04d}"} for i in range(10, 15)]
    nested = json.loads((await _export(key="k", path="meta.families")).content)
    assert nested == [{"id": "f1"}, {"id": "f2"}]


@pytest.mark.asyncio
async def test_a_layout_free_where_exports_the_matches_grouped_by_list_path(state):
    groups = json.loads((await _export(key="k", where_json='{"id": "gap-0007"}')).content)
    assert groups == [{"path": "gaps", "matches": [state["value"]["gaps"][7]]}]
    under = json.loads((await _export(key="k", path="meta", where_json='{"id": "f2"}')).content)
    assert under == [{"path": "meta.families", "matches": [{"id": "f2"}]}]


@pytest.mark.asyncio
async def test_a_missing_key_or_path_writes_nothing(monkeypatch, state):
    missing_path = await _export(key="k", path="nope")
    assert missing_path.content.startswith("Error:") and "nope" in missing_path.content

    async def _none(**_kwargs):
        return None

    monkeypatch.setattr(shared_state_service, "get_state", _none)
    missing_key = await _export(key="absent")
    assert missing_key.content.startswith("Error:") and "nothing was written" in missing_key.content


@pytest.mark.asyncio
async def test_without_the_argument_the_read_is_unchanged(state):
    _, executor = _tools()
    result = await executor.execute_tool(
        ToolCall(id="c", name="get_shared_state", arguments={"key": "k", "path": "gaps"})
    )
    assert "characters 0 to 20000 of" in result.content


@pytest.mark.asyncio
async def test_to_workspace_path_writes_the_value_into_the_workspace(store, state):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    from app.services.builtin_tools.media import build_code_interpreter_tool

    composite = CompositeToolExecutor()
    composite.set_file_context(FileContext())
    ci_defs, ci_exec = build_code_interpreter_tool(user_id=str(user), conversation_id=str(conversation))
    composite.add_tools(ci_defs, ci_exec)
    composite.add_tools(*_tools())
    offered = {d.name: d for d in composite.all_tools}
    assert WORKSPACE_PATH_ARGUMENT in offered["get_shared_state"].input_schema["properties"]

    result = await composite.execute_tool(
        ToolCall(
            id="c1",
            name="get_shared_state",
            arguments={"key": "dev:harness-gaps", WORKSPACE_PATH_ARGUMENT: "work/gaps.json"},
        )
    )
    assert not result.is_error, result.content
    assert "workspace/work/gaps.json" in result.content and "sha256" in result.content
    assert "gap-0001" not in result.content and len(result.content) < 2_000
    (record,) = [r for r in store.records.values() if r.conversation_id == conversation]
    entry = store.files[record.id]["work/gaps.json"]
    written = json.loads(await store.read_blob(record.id, entry.sha256))
    assert written == state["value"]
