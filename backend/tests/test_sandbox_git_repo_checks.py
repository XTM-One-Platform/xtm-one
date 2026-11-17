"""``git_repo = true``: a declared check that reads git runs in a throwaway repository of the checkout.

A checkout written by ``materialize_repository`` has no ``.git``, so a CI gate
whose script lists the tracked files (``git ls-files``) or finds the repository
root (``git rev-parse --show-toplevel``) failed in the sandbox and passed in CI:
the agent pushed blind. The wrapper (:data:`checks.GIT_REPO_SHELL`) is run here
for real with ``sh`` and ``git`` on a temporary tree, so what is tested is what
the container runs.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
import uuid
from pathlib import Path

import pytest

from app.services.sandbox_workspace import checks, commands
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

ROOT = Path(__file__).resolve().parents[2]

needs_git = pytest.mark.skipif(shutil.which("git") is None or shutil.which("sh") is None, reason="git and sh needed")


def _spec(toml: str) -> checks.CheckSpec:
    (spec,) = checks.load_checks(toml).values()
    return spec


def test_git_repo_is_read_from_the_declaration_and_defaults_to_false():
    specs = checks.load_checks('[check.a]\nargv = ["x"]\n[check.b]\nargv = ["x"]\ngit_repo = true\n')
    assert specs["a"].git_repo is False and specs["b"].git_repo is True
    with pytest.raises(checks.ChecksError, match="git_repo must be true or false"):
        checks.load_checks('[check.a]\nargv = ["x"]\ngit_repo = "yes"\n')


def test_a_git_repo_check_wraps_its_argv_with_the_checkout_root_relative_to_its_cwd():
    root = _spec('[check.a]\nargv = ["bash", "s.sh"]\ngit_repo = true\n')
    assert checks.command_argv(root) == ("sh", "-c", checks.GIT_REPO_SHELL, "xtm-git", ".", "bash", "s.sh")
    nested = _spec(
        '[check.a]\nargv = ["python", "x.py"]\ncwd = "backend/sub"\ndefault_args = ["--check"]\ngit_repo = true\n'
    )
    assert checks.command_argv(nested)[3:] == ("xtm-git", "../..", "python", "x.py", "--check")
    assert checks.command_argv(nested, ["--fix"])[-1] == "--fix"
    # Without the key nothing changes.
    plain = _spec('[check.a]\nargv = ["python", "x.py"]\ndefault_args = ["."]\n')
    assert checks.command_argv(plain) == ("python", "x.py", ".")


def test_the_listing_says_the_check_runs_in_a_throwaway_repository():
    listing = checks.describe_checks(checks.load_checks('[check.a]\nargv = ["x"]\ngit_repo = true\n'))
    assert "[in a throwaway git repository of the checkout]" in listing


def _run(argv: tuple[str, ...], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, timeout=60, check=False)  # noqa: S603


def _tree(tmp_path: Path) -> Path:
    co = tmp_path / "co"
    (co / "backend").mkdir(parents=True)
    (co / "backend" / "app.py").write_text("x = 1\n")
    (co / "README.md").write_text("hi\n")
    (co / ".gitignore").write_text("ignored.txt\n")
    (co / "ignored.txt").write_text("no\n")
    (co / ".xtm-checkout.json").write_text("{}\n")
    (co / ".xtm").mkdir()
    (co / ".xtm" / "run.txt").write_text("out\n")
    (co / "web" / "node_modules" / "pkg").mkdir(parents=True)
    (co / "web" / "node_modules" / "pkg" / "index.js").write_text("1\n")
    return co


@needs_git
def test_a_git_reading_script_sees_the_tracked_tree_and_the_repository_is_removed_after(tmp_path):
    co = _tree(tmp_path)
    script = (
        'cd "$(git rev-parse --show-toplevel)" && git ls-files && '
        "git rev-parse --verify -q origin/main >/dev/null && echo base-ok"
    )
    spec = _spec(f'[check.a]\nargv = ["sh", "-c", {json.dumps(script)}]\ncwd = "backend"\ngit_repo = true\n')
    done = _run(checks.command_argv(spec), co / "backend")
    assert done.returncode == 0, done.stderr
    listed = done.stdout.split()
    assert {"README.md", "backend/app.py", ".gitignore"} <= set(listed) and "base-ok" in listed
    # .gitignore honoured; the checkout record, run outputs and installed trees never tracked.
    for left_out in ("ignored.txt", ".xtm-checkout.json", ".xtm/run.txt", "web/node_modules/pkg/index.js"):
        assert left_out not in listed
    assert not (co / ".git").exists()


@needs_git
def test_the_command_exit_code_is_kept_and_the_repository_removed_on_failure(tmp_path):
    co = _tree(tmp_path)
    spec = _spec('[check.a]\nargv = ["sh", "-c", "git ls-files >/dev/null && exit 3"]\ngit_repo = true\n')
    done = _run(checks.command_argv(spec), co)
    assert done.returncode == 3 and not (co / ".git").exists()


@needs_git
def test_a_checkout_that_holds_its_own_repository_is_run_as_it_is(tmp_path):
    co = _tree(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=co, check=True)  # noqa: S607
    marker = co / ".git" / "mine"
    marker.write_text("kept\n")
    spec = _spec('[check.a]\nargv = ["sh", "-c", "test -e .git/mine"]\ngit_repo = true\n')
    done = _run(checks.command_argv(spec), co)
    assert done.returncode == 0 and marker.exists()


async def test_resolve_wraps_a_git_repo_check_and_keeps_its_default_args_for_affected():
    scope = WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())
    workspace = WorkspaceSession(scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=1 << 20, max_files=50))
    toml = '[check.gate]\nargv = ["python", "g.py"]\ncwd = "backend"\ndefault_args = ["--check"]\ngit_repo = true\n'
    await workspace.put_tree([("co/.xtm-one/checks.toml", toml.encode())])
    common = {"checkout": "co", "allow_argv": False, "max_timeout": 600, "known_images": {"python"}}
    bare = await commands.resolve(workspace, check="gate", **common)
    assert bare.argv[:2] == ("sh", "-c") and bare.argv[-2:] == ("g.py", "--check") and bare.default_args == ("--check",)
    given = await commands.resolve(workspace, check="gate", args=["--fix"], **common)
    assert given.argv[-1] == "--fix" and given.default_args == ()


def test_a_git_repo_run_never_saves_the_throwaway_repository_back():
    assert ".git/" in commands.IGNORED_PATTERNS and "*/.git/" in commands.IGNORED_PATTERNS


def test_the_repository_declares_its_git_reading_ci_gates_as_git_repo_checks():
    declared = tomllib.loads((ROOT / checks.CHECKS_FILE).read_text(encoding="utf-8"))["check"]
    specs = checks.load_checks((ROOT / checks.CHECKS_FILE).read_text(encoding="utf-8"))
    for name in ("editions", "agents-md-hygiene"):
        assert specs[name].git_repo is True, name
    assert declared["editions"]["argv"] == ["python", "scripts/license_headers.py", "--check"]
    assert declared["agents-md-hygiene"]["argv"] == ["bash", "scripts/check-agents-md-hygiene.sh"]
    # A narrow checkout takes what each gate reads, so neither reports a file it lacks.
    assert specs["editions"].needs == ("LICENSE", "licensing")
    assert specs["agents-md-hygiene"].needs == ("AGENTS.md", "dev-docs")
    for name in ("frontend-commands", "runner-fmt", "relay-fmt", "relay-agent-core-fmt"):
        assert name in specs, name
