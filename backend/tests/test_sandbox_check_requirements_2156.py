"""A check run on a narrow checkout names what it reads that the checkout left out (#2156).

``sandbox_run_command`` grows the checkout by those paths before the run; these tests pin what is named.
"""

from __future__ import annotations

import uuid

from app.services.sandbox_workspace import commands
from app.services.sandbox_workspace.checkout import RECORD_NAME, build_record
from app.services.sandbox_workspace.checks import CheckSpec
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

CHECKS = b"""
[check.editions]
argv = ["python", "scripts/license_headers.py", "--check"]
cwd = "backend"
image = "python-dev"
install_first = false
needs = ["LICENSE", "licensing"]

[check.frontend-unit]
argv = ["yarn", "vitest", "run"]
cwd = "frontend"
image = "nodejs-dev"
install_first = false

[check.root-gate]
argv = ["python", "gate.py"]
image = "python-dev"
install_first = false
"""


def _workspace() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())
    return WorkspaceSession(
        scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=8 * 1024 * 1024, max_files=200)
    )


async def _checkout(workspace: WorkspaceSession, sub_paths: list[str], held: list[tuple[str, bytes]]) -> None:
    files = [(".xtm-one/checks.toml", CHECKS), *held]
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="abc", path="co", sub_paths=sub_paths, files=files
    )
    await workspace.put_tree([*((f"co/{p}", d) for p, d in files), (f"co/{RECORD_NAME}", record.to_json())])


async def _resolve(workspace: WorkspaceSession, check: str) -> commands.ResolvedCommand:
    return await commands.resolve(
        workspace,
        checkout="co",
        check=check,
        allow_argv=False,
        max_timeout=3600,
        known_images={"python-dev", "nodejs-dev"},
    )


def test_a_check_requires_its_directory_then_its_needs_and_a_root_check_its_needs_only() -> None:
    spec = CheckSpec(name="editions", argv=("python",), cwd="backend", needs=("LICENSE", "licensing", "backend/x"))
    assert commands.check_requirements(spec) == ("backend", "LICENSE", "licensing")
    assert commands.check_requirements(CheckSpec(name="g", argv=("python",), needs=("LICENSE",))) == ("LICENSE",)
    assert commands.check_requirements(CheckSpec(name="g", argv=("python",))) == ()


async def test_a_frontend_checkout_running_the_editions_gate_names_backend_and_the_license() -> None:
    """editions-check-needs-license-in-checkout: a frontend-only checkout lacked LICENSE and backend/."""
    workspace = _workspace()
    await _checkout(workspace, [".xtm-one", "frontend"], [("frontend/src/a.ts", b"x")])
    command = await _resolve(workspace, "editions")
    assert command.requires == ("backend", "LICENSE", "licensing") and command.cwd_root == "co"
    assert await commands.missing_requirements(workspace, "co", command) == ("o/r", ["backend", "LICENSE", "licensing"])


async def test_an_exact_files_checkout_names_the_directory_its_unit_check_runs_from() -> None:
    """exact-files-checkout-misses-test-setup and the conftest case: one file held, its check directory not."""
    workspace = _workspace()
    await _checkout(workspace, [".xtm-one", "frontend/src/a.test.ts"], [("frontend/src/a.test.ts", b"x")])
    command = await _resolve(workspace, "frontend-unit")
    assert await commands.missing_requirements(workspace, "co", command) == ("o/r", ["frontend"])


async def test_a_checkout_holding_what_the_check_reads_names_nothing() -> None:
    workspace = _workspace()
    await _checkout(
        workspace,
        [".xtm-one", "backend", "LICENSE", "licensing"],
        [("backend/app.py", b"x"), ("LICENSE", b"l"), ("licensing/editions.yml", b"e")],
    )
    assert await commands.missing_requirements(workspace, "co", await _resolve(workspace, "editions")) == ("", [])
    # A check run from the root with no needs reads the whole tree as it is: nothing to grow.
    assert await commands.missing_requirements(workspace, "co", await _resolve(workspace, "root-gate")) == ("", [])


async def test_a_whole_tree_checkout_or_a_tree_without_record_names_nothing() -> None:
    workspace = _workspace()
    await _checkout(workspace, [], [("frontend/src/a.ts", b"x")])
    assert await commands.missing_requirements(workspace, "co", await _resolve(workspace, "editions")) == ("", [])
    bare = _workspace()
    await bare.put_tree([("co/.xtm-one/checks.toml", CHECKS)])
    assert await commands.missing_requirements(bare, "co", await _resolve(bare, "editions")) == ("", [])
