"""``set_shared_state(from_workspace_path)``: a large write is read from the workspace, not the context.

The write-side counterpart of ``get_shared_state(to_workspace_path)``: a
condensed register of 250 entries (about 60 KB) used to be printed from the
code interpreter and resent inline in batches. The file holds the ``*_json``
arguments as one JSON object; the tool reads it on the server through the
run's workspace and applies it with the same list semantics. Synthetic
fixtures: ``upsert_state`` is monkeypatched, the workspace store is the
in-memory one.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
from app.services.builtin_tools import build_shared_state_tools
from app.services.builtin_tools.media import build_code_interpreter_tool
from app.services.sandbox_workspace import local
from app.services.sandbox_workspace import session as session_module
from app.services.sandbox_workspace.local import BlobCache
from app.services.sandbox_workspace.store import MemoryWorkspaceStore
from app.tools.base import WORKSPACE_READ_ARGUMENT, CompositeToolExecutor, FileContext, ToolCall

MB = 1024 * 1024


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


@pytest.fixture
def writes(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key"], "value": {"gaps": (kwargs.get("append") or {}).get("gaps", [])}, "notes": []}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    return calls


def _composite():
    composite = CompositeToolExecutor()
    composite.set_file_context(FileContext())
    ci_defs, ci_exec = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    composite.add_tools(ci_defs, ci_exec)
    composite.add_tools(*build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4())))
    return composite, ci_exec


def _gaps(n: int = 250) -> list[dict]:
    return [{"id": f"gap-{i:04d}", "status": "open", "evidence": "e" * 200} for i in range(n)]


async def _set(composite, **arguments):
    return await composite.execute_tool(ToolCall(id="c", name="set_shared_state", arguments=arguments))


def test_set_shared_state_is_offered_from_workspace_path_only_with_a_workspace(store):
    definitions, _ = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    by_name = {d.name: d for d in definitions}
    assert by_name["set_shared_state"].workspace_read
    assert WORKSPACE_READ_ARGUMENT not in by_name["set_shared_state"].input_schema["properties"]
    assert not by_name["get_shared_state"].workspace_read
    composite, _ = _composite()
    offered = {d.name: d for d in composite.all_tools}
    assert WORKSPACE_READ_ARGUMENT in offered["set_shared_state"].input_schema["properties"]


@pytest.mark.asyncio
async def test_a_60_kb_append_of_250_items_is_applied_from_one_workspace_file(store, writes):
    composite, ci_exec = _composite()
    body = {"append_json": {"gaps": _gaps()}, "unique_by_json": {"gaps": "id"}}
    raw = json.dumps(body).encode()
    assert len(raw) > 50_000
    await ci_exec.session.put_bytes("work/x.json", raw)
    result = await _set(composite, key="dev:harness-gaps", note="condensed", **{WORKSPACE_READ_ARGUMENT: "work/x.json"})
    assert not result.is_error and result.content.startswith("Saved shared state."), result.content[:300]
    assert len(result.content) < 2_000 and "eeeeeeeeee" not in result.content
    (call,) = writes
    assert call["append"] == body["append_json"] and call["unique_by"] == {"gaps": "id"}
    assert call["note"] == "condensed" and call["remove"] is None


@pytest.mark.asyncio
async def test_inline_arguments_combine_with_the_file_and_json_strings_are_accepted(store, writes):
    composite, ci_exec = _composite()
    await ci_exec.session.put_bytes(
        "work/u.json",
        json.dumps({"update_json": '{"gaps": {"match": {"id": "a"}, "increment": {"count": 1}}}'}).encode(),
    )
    result = await _set(
        composite, key="k", remove_json='{"gaps": [{"id": "b"}]}', **{WORKSPACE_READ_ARGUMENT: "work/u.json"}
    )
    assert not result.is_error, result.content
    (call,) = writes
    assert call["update"] == {"gaps": {"match": {"id": "a"}, "increment": {"count": 1}}}
    assert call["remove"] == {"gaps": [{"id": "b"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "inline", "expected"),
    [
        (b"{not json", {}, "not a UTF-8 JSON file"),
        (b"[1, 2]", {}, "must hold a JSON object of arguments"),
        (b'{"hold": "take"}', {}, "names hold"),
        (b'{"append_json": {"gaps": [1]}}', {"append_json": '{"gaps": [2]}'}, "both inline and in from_workspace_path"),
        (b'{"append_json": {"gaps": 1}}', {}, "must map each top-level key to a list"),
    ],
)
async def test_a_bad_file_writes_nothing(store, writes, content, inline, expected):
    composite, ci_exec = _composite()
    await ci_exec.session.put_bytes("work/bad.json", content)
    result = await _set(composite, key="k", **inline, **{WORKSPACE_READ_ARGUMENT: "work/bad.json"})
    assert result.content.startswith("Error:") and expected in result.content, result.content
    assert writes == []


@pytest.mark.asyncio
async def test_a_missing_file_writes_nothing(store, writes):
    composite, _ = _composite()
    result = await _set(composite, key="k", **{WORKSPACE_READ_ARGUMENT: "work/absent.json"})
    assert result.content.startswith("Error: from_workspace_path refused") and "Nothing was written" in result.content
    assert writes == []


@pytest.mark.asyncio
async def test_without_a_workspace_the_argument_is_refused(writes):
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await executor.execute_tool(
        ToolCall(id="c", name="set_shared_state", arguments={"key": "k", WORKSPACE_READ_ARGUMENT: "work/x.json"})
    )
    assert "needs the sandbox workspace" in result.content
    assert writes == []
