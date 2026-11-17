"""Stage bounds of a stateless code_interpreter run on OpenSandbox (#8548).

A container that never starts, or code that never ends, answers an error
naming the stage before the agent loop's own per-tool timeout, instead of
holding the call until that timeout says "timed out after 300s".
"""

from __future__ import annotations

import asyncio
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services import _sandbox_selector as ss
from app.services import code_interpreter as ci


@pytest.fixture(autouse=True)
def _opensandbox_up(monkeypatch):
    from app.services import quota as quota_module

    async def probe():
        return True

    monkeypatch.setattr(ss, "_probe_opensandbox_available", probe)
    monkeypatch.setattr(settings, "custom_tool_sandbox", "opensandbox")
    monkeypatch.setattr(settings, "opensandbox_domain", "localhost:8090")
    monkeypatch.setattr(settings, "opensandbox_network_allow", "")
    monkeypatch.setattr(quota_module, "enforce_combined_sandbox_quota", AsyncMock(return_value=None))
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", AsyncMock(return_value=None))


def _sandbox(run=None) -> MagicMock:
    result = MagicMock()
    result.exit_code = 0
    result.logs.stdout = [MagicMock(text='{"stdout": "x", "error": null}')]
    result.logs.stderr = []
    result.complete = SimpleNamespace(execution_time_in_millis=5)
    sandbox = AsyncMock()
    sandbox.__aenter__ = AsyncMock(return_value=sandbox)
    sandbox.__aexit__ = AsyncMock(return_value=False)
    sandbox.files.write_file = AsyncMock()
    sandbox.files.list_directory = AsyncMock(return_value=[])
    sandbox.commands.run = run or AsyncMock(return_value=result)
    sandbox.get_metrics = AsyncMock(return_value=None)
    sandbox.kill = AsyncMock()
    return sandbox


async def _never(*_args, **_kwargs):
    await asyncio.sleep(3600)


def test_stage_budget_keeps_a_margin_and_honours_the_cap():
    now = time.monotonic()
    assert ci._stage_budget(300, now) <= 300 - 15
    assert ci._stage_budget(300, now, cap=130) <= 130
    assert ci._stage_budget(30, now) <= 27
    assert ci._stage_budget(10, now - 100) == 1.0


async def test_a_create_that_never_answers_is_named_before_the_timeout(monkeypatch):
    # The budget is stubbed (its floor and cap are covered above), so the
    # assertions name the stage and its timing format, never a wall-clock value.
    monkeypatch.setattr(ci, "_stage_budget", lambda *_a, **_k: 0.1)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = _never
        result = await ci.execute_python_opensandbox('print("x")', timeout=300)
    assert result["error"].startswith("Execution stopped at stage create")
    assert "the sandbox service did not start a container in time" in result["error"]
    assert re.search(r"\(timeout 300 s; create \d+\.\d s\)", result["error"])
    assert result["stdout"] == "" and result["mounted"] == []
    assert result["platform_failure"] is True


async def test_a_create_cut_by_the_stage_cap_is_a_sandbox_failure_of_the_run(monkeypatch):
    # The cap cancels _create_opensandbox before its _fail path: the call records it (#8528).
    from app.services import frictions

    seen = []
    monkeypatch.setattr(frictions, "note_sandbox_failure", lambda usage, evidence: seen.append((usage, evidence)))
    monkeypatch.setattr(ci, "_stage_budget", lambda *_a, **_k: 0.1)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = _never
        await ci.execute_python_opensandbox('print("x")', timeout=300)
    assert len(seen) == 1
    assert seen[0][0] == "code_interpreter"
    assert "stage create" in seen[0][1] and "did not start a container in time" in seen[0][1]


async def test_a_create_stage_timeout_names_the_python_pool_state(monkeypatch):
    # #9355: the error and the friction say how many python pods the server had.
    from app.services import frictions

    seen = []

    async def _state(image_key=None, **_k):
        assert image_key == "python"
        return {"python": {"ready": 0, "creating": 2, "pulling": 1, "other": 0}}

    monkeypatch.setattr(ss, "sandbox_pool_state", _state)
    monkeypatch.setattr(settings, "opensandbox_k8s_pool_names", {})
    monkeypatch.setattr(frictions, "note_sandbox_failure", lambda usage, evidence: seen.append(evidence))
    monkeypatch.setattr(ci, "_stage_budget", lambda *_a, **_k: 0.1)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = _never
        result = await ci.execute_python_opensandbox('print("x")', timeout=300)
    words = "Sandbox pods for image key 'python' on the server now: 0 running, 2 creating, 1 pulling the image."
    assert result["error"].endswith(words)
    assert words in seen[0]


async def test_a_create_stage_timeout_without_pool_state_is_unchanged(monkeypatch):
    async def _hangs(*_a, **_k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(ss, "sandbox_pool_state", _hangs)
    monkeypatch.setattr(ci, "_stage_margin", lambda _t: 0.1)
    monkeypatch.setattr(ci, "_stage_budget", lambda *_a, **_k: 0.1)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = _never
        result = await ci.execute_python_opensandbox('print("x")', timeout=300)
    assert result["error"].endswith("out of capacity for the python image key.")
    assert "Sandbox pods" not in result["error"]


async def test_a_create_stage_timeout_is_an_error_result_of_the_tool(monkeypatch):
    # is_error true, so the agent loop records a tool_error friction too.
    from app.services.builtin_tools.media import build_code_interpreter_tool
    from app.tools.base import ToolCall

    async def _timed_out(*_args, **_kwargs):
        return ci._stage_timeout_result("create", 300, time.monotonic(), {"create": 130.0})

    monkeypatch.setattr(ci, "execute_python", _timed_out)
    _definitions, executor = build_code_interpreter_tool()
    result = await executor.execute_tool(ToolCall(id="1", name="code_interpreter", arguments={"code": "print(1)"}))
    assert result.is_error is True
    assert "stage create" in result.content


def test_a_run_stage_timeout_is_not_a_platform_failure():
    # Only the create stage is the platform's failure: a run (or upload) stage
    # timeout keeps the shape it had, with no sandbox_failure of its own.
    result = ci._stage_timeout_result("run", 300, time.monotonic(), {"create": 1.0, "run": 280.0})
    assert result["platform_failure"] is False
    assert "stage run" in result["error"]


async def test_code_that_never_ends_is_named_and_its_container_killed(monkeypatch):
    sandbox = _sandbox(run=_never)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox("while True: pass", timeout=2)
    assert result["error"].startswith("Execution stopped at stage run")
    assert "session=true" in result["error"]
    sandbox.kill.assert_awaited()


async def test_a_trivial_print_is_unchanged():
    sandbox = _sandbox()
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox('print("x")', timeout=300)
    assert result["error"] is None and result["stdout"] == "x"
    sandbox.commands.run.assert_awaited_once()


async def test_a_workspace_preparation_that_hangs_is_named(monkeypatch):
    from app.services.sandbox_workspace import run as run_module

    monkeypatch.setattr(run_module, "prepare_remote", _never)
    monkeypatch.setattr(ci, "_stage_budget", lambda *_a, **_k: 0.1)
    result = await ci.execute_python_opensandbox('print("x")', timeout=300, workspace=MagicMock())
    assert result["error"].startswith("Execution stopped at stage workspace")


def _mount(sizes: dict[str, int], push=None) -> SimpleNamespace:
    entries = {path: SimpleNamespace(size=size) for path, size in sizes.items()}
    return SimpleNamespace(
        entries=entries,
        env={},
        push=push or AsyncMock(),
        pull=AsyncMock(),
        finish=MagicMock(return_value=None),
    )


def test_large_file_hint_names_the_large_files_and_the_in_place_tools():
    mount = _mount({"src/i18n/en.json": 1_500_000, "src/i18n/fr.json": 1_400_000, "README.md": 2_000})
    hint = ci._large_file_hint(mount)
    assert "workspace/src/i18n/en.json 1.5 MB, workspace/src/i18n/fr.json 1.4 MB" in hint
    assert "README.md" not in hint
    for tool in ("sandbox_edit_file", "sandbox_grep", "sandbox_read_file"):
        assert tool in hint
    assert ci._large_file_hint(_mount({"small.json": 10})) == ""
    assert ci._large_file_hint(None) == ""


async def test_an_upload_of_a_large_file_that_hangs_is_named_with_the_dedicated_tools(monkeypatch):
    from app.services.sandbox_workspace import run as run_module

    async def push(_sandbox):
        await asyncio.sleep(3600)

    mount = _mount({"catalog.json": 1_500_000}, push=push)

    async def prepare(_workspace, _code):
        return mount

    monkeypatch.setattr(run_module, "prepare_remote", prepare)
    monkeypatch.setattr(ci, "_stage_budget", lambda *_a, **_k: 0.1)
    sandbox = _sandbox()
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox(
            'open("workspace/catalog.json").read()', timeout=300, workspace=MagicMock()
        )
    assert result["error"].startswith("Execution stopped at stage upload")
    assert "workspace/catalog.json 1.5 MB" in result["error"]
    assert "sandbox_edit_file" in result["error"]
    sandbox.commands.run.assert_not_awaited()
    sandbox.kill.assert_awaited()


async def test_a_run_over_a_large_mounted_file_names_the_dedicated_tools(monkeypatch):
    from app.services.sandbox_workspace import run as run_module

    mount = _mount({"catalog.json": 1_500_000})

    async def prepare(_workspace, _code):
        return mount

    monkeypatch.setattr(run_module, "prepare_remote", prepare)
    sandbox = _sandbox(run=_never)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox(
            'open("workspace/catalog.json").read()', timeout=2, workspace=MagicMock()
        )
    assert result["error"].startswith("Execution stopped at stage run")
    assert "sandbox_edit_file" in result["error"]
    sandbox.kill.assert_awaited()
