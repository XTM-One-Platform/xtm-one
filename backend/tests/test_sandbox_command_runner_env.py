"""The detached command runner (``remote.TOOL_SRC``): the heap it gives Node, the user bin on PATH, no core dumps."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.services.sandbox_workspace import remote


def _runner() -> ModuleType:
    module = ModuleType("xtm_workspace_runner")
    exec(compile(remote.TOOL_SRC, "<workspace runner>", "exec"), module.__dict__)  # noqa: S102 - the runner's own source
    return module


GIB = 1024 * 1024 * 1024


def test_node_heap_is_three_quarters_of_the_container() -> None:
    env = _runner()._node_heap_env({}, 4 * GIB)
    assert env["NODE_OPTIONS"] == "--max-old-space-size=3072"


def test_node_heap_keeps_other_node_options() -> None:
    env = _runner()._node_heap_env({"NODE_OPTIONS": "--enable-source-maps"}, 4 * GIB)
    assert env["NODE_OPTIONS"] == "--enable-source-maps --max-old-space-size=3072"


@pytest.mark.parametrize("given", ["--max-old-space-size=8192", "--max_old_space_size=8192"])
def test_node_heap_set_by_the_command_wins(given: str) -> None:
    env = _runner()._node_heap_env({"NODE_OPTIONS": given}, 4 * GIB)
    assert env["NODE_OPTIONS"] == given


@pytest.mark.parametrize("limit", [None, 0, 128 * 1024 * 1024])
def test_no_heap_option_without_a_usable_limit(limit: int | None) -> None:
    assert "NODE_OPTIONS" not in _runner()._node_heap_env({}, limit)


def test_user_bin_of_a_pip_user_install_is_first_on_path() -> None:
    env = _runner()._user_bin_env({"HOME": "/home/runner", "PATH": "/usr/local/bin:/usr/bin"})
    assert env["PATH"] == "/home/runner/.local/bin:/usr/local/bin:/usr/bin"


def test_user_bin_follows_pythonuserbase_and_is_added_once() -> None:
    runner = _runner()
    env = runner._user_bin_env({"PYTHONUSERBASE": "/opt/pyuser", "HOME": "/home/runner", "PATH": "/usr/bin"})
    assert env["PATH"] == "/opt/pyuser/bin:/usr/bin"
    assert runner._user_bin_env(dict(env))["PATH"] == "/opt/pyuser/bin:/usr/bin"


@pytest.mark.skipif(sys.platform != "linux", reason="the runner runs in a Linux container")
def test_a_tool_a_user_install_left_in_the_user_bin_runs_by_its_name(tmp_path: Path) -> None:
    runner = _runner()
    home = tmp_path / "home"
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    tool = user_bin / "xtm-user-tool"
    tool.write_text("#!/bin/sh\necho from-user-bin\n", encoding="utf-8")
    tool.chmod(0o755)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "argv.json").write_text(json.dumps(["xtm-user-tool"]), encoding="utf-8")
    (run_dir / "env.json").write_text(json.dumps({"HOME": str(home), "PYTHONUSERBASE": ""}), encoding="utf-8")
    runner._exec(str(run_dir), str(tmp_path), "30")
    assert json.loads((run_dir / "exit.json").read_text(encoding="utf-8"))["exit_code"] == 0
    assert (run_dir / "stdout.txt").read_text(encoding="utf-8").strip() == "from-user-bin"


@pytest.mark.skipif(sys.platform != "linux", reason="the runner runs in a Linux container")
def test_command_runs_without_core_files_and_with_the_heap_env(tmp_path: Path) -> None:
    runner = _runner()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    script = "import os, resource; print(resource.getrlimit(resource.RLIMIT_CORE)[0]); print(os.environ.get('NODE_OPTIONS', ''))"
    (run_dir / "argv.json").write_text(json.dumps([sys.executable, "-c", script]), encoding="utf-8")
    runner._memory_limit = lambda: 2 * GIB
    previous = os.environ.pop("NODE_OPTIONS", None)
    try:
        runner._exec(str(run_dir), str(tmp_path), "30")
    finally:
        if previous is not None:
            os.environ["NODE_OPTIONS"] = previous
    assert json.loads((run_dir / "exit.json").read_text(encoding="utf-8"))["exit_code"] == 0
    core, node_options = (run_dir / "stdout.txt").read_text(encoding="utf-8").splitlines()
    assert core == "0"
    assert node_options == "--max-old-space-size=1536"
