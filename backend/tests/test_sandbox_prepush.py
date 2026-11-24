"""Pre-push guards (``sandbox_workspace/prepush.py``): tsc and knip named before a push that never saw them pass."""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Iterable
from types import SimpleNamespace

import pytest

from app.services.sandbox_workspace import prepush
from app.services.sandbox_workspace.checkout import RECORD_NAME, CheckoutChanges, build_record, changes_since
from app.services.sandbox_workspace.checks import CHECKS_FILE, ChecksError, load_checks

CHECKS = b"""
[check.frontend-types]
argv = ["yarn", "typecheck"]
cwd = "frontend"
pre_push = ["*.ts", "*.tsx"]

[check.frontend-knip]
argv = ["yarn", "knip"]
cwd = "frontend"
pre_push = ["*.ts", "*.tsx"]

[check.backend-lint]
argv = ["ruff", "check", "."]
cwd = "backend"
"""

BASE = {
    "frontend/src/a.ts": b"export const a = 1;\n",
    "frontend/src/b.tsx": b"export const B = () => null;\n",
    "backend/app/x.py": b"x = 1\n",
    CHECKS_FILE: CHECKS,
}


class _Session:
    """A workspace holding one checkout at ``repo``: bytes by workspace path, the manifest from them."""

    available = True

    def __init__(self) -> None:
        self.record = build_record(
            provider="github",
            repository="acme/app",
            ref="main",
            commit="c" * 40,
            path="repo",
            sub_paths=(),
            files=list(BASE.items()),
        )
        self.files: dict[str, bytes] = {f"repo/{p}": d for p, d in BASE.items()}
        self.files[f"repo/{RECORD_NAME}"] = self.record.to_json()
        self.reads = 0

    async def entries(self, *, refresh: bool = False) -> dict[str, SimpleNamespace]:
        return {
            p: SimpleNamespace(sha256=hashlib.sha256(d).hexdigest(), mtime=time.time()) for p, d in self.files.items()
        }

    async def get_bytes(self, path: str) -> tuple[SimpleNamespace, bytes]:
        self.reads += 1
        if path not in self.files:
            raise FileNotFoundError(path)
        return SimpleNamespace(path=path), self.files[path]

    async def put_tree(self, files: Iterable[tuple[str, bytes]], *, operation: str) -> None:
        for path, data in files:
            self.files[path] = data

    def edit(self, repo_path: str, data: bytes) -> None:
        self.files[f"repo/{repo_path}"] = data

    async def changes(self) -> CheckoutChanges:
        return changes_since(self.record, await self.entries())


async def _run(session: _Session, check: str, argv: tuple[str, ...], exit_code: int) -> bool:
    return await prepush.record_run(session, "repo", check=check, argv=argv, exit_code=exit_code)


@pytest.mark.asyncio
async def test_an_unused_export_change_with_no_knip_run_is_named_before_the_push() -> None:
    session = _Session()
    session.edit("frontend/src/a.ts", b"export const a = 1;\nexport const unused = 2;\n")
    text = await prepush.commit_warning(session, session.record, await session.changes())
    assert "frontend-knip never ran on this checkout" in text
    assert "frontend-types never ran on this checkout" in text
    assert "frontend/src/a.ts" in text
    assert "backend-lint" not in text


@pytest.mark.asyncio
async def test_a_failed_tsc_run_on_the_tree_is_named_as_failed() -> None:
    session = _Session()
    session.edit("frontend/src/b.tsx", b"export const B = (): number => 'x';\n")
    assert await _run(session, "frontend-types", ("yarn", "typecheck"), 2)
    assert await _run(session, "frontend-knip", ("yarn", "knip"), 0)
    text = await prepush.commit_warning(session, session.record, await session.changes())
    assert "frontend-types failed (exit 2) on this tree" in text
    assert "frontend-knip" not in text


@pytest.mark.asyncio
async def test_a_clean_change_that_passed_both_guards_on_its_tree_passes() -> None:
    session = _Session()
    session.edit("frontend/src/a.ts", b"export const a = 2;\n")
    assert await _run(session, "frontend-types", ("yarn", "typecheck"), 0)
    assert await _run(session, "frontend-knip", ("yarn", "knip"), 0)
    assert await prepush.commit_warning(session, session.record, await session.changes()) == ""


@pytest.mark.asyncio
async def test_an_edit_after_the_pass_names_the_guards_again() -> None:
    session = _Session()
    session.edit("frontend/src/a.ts", b"export const a = 2;\n")
    assert await _run(session, "frontend-types", ("yarn", "typecheck"), 0)
    assert await _run(session, "frontend-knip", ("yarn", "knip"), 0)
    session.edit("frontend/src/b.tsx", b"export const B = () => 1;\n")
    text = await prepush.commit_warning(session, session.record, await session.changes())
    assert "frontend-types ran on an older tree" in text
    assert "frontend-knip ran on an older tree" in text


@pytest.mark.asyncio
async def test_a_backend_only_change_names_no_guard() -> None:
    session = _Session()
    session.edit("backend/app/x.py", b"x = 2\n")
    assert await prepush.commit_warning(session, session.record, await session.changes()) == ""


@pytest.mark.asyncio
async def test_a_guard_run_on_some_files_proves_nothing_and_is_not_kept() -> None:
    session = _Session()
    session.edit("frontend/src/a.ts", b"export const a = 2;\n")
    assert not await _run(session, "frontend-knip", ("yarn", "knip", "src/a.ts"), 0)
    assert not await _run(session, "backend-lint", ("ruff", "check", "."), 0)
    text = await prepush.commit_warning(session, session.record, await session.changes())
    assert "frontend-knip never ran" in text


def test_the_guard_state_is_a_run_output_never_a_change_of_the_checkout() -> None:
    path = prepush.state_path("work/repo")
    assert path == ".xtm/runs/prepush/work__repo.json"
    session = _Session()
    session.files[prepush.state_path("repo")] = b"{}"
    changes = changes_since(session.record, asyncio.run(session.entries()), since=0.0)
    assert changes.empty and not changes.untracked


def test_pre_push_must_be_a_list_of_globs() -> None:
    with pytest.raises(ChecksError, match="pre_push"):
        load_checks('[check.x]\nargv = ["true"]\npre_push = "*.ts"\n')
    assert load_checks(CHECKS.decode())["frontend-knip"].pre_push == ("*.ts", "*.tsx")


def test_the_repository_declares_tsc_and_knip_as_guards() -> None:
    from pathlib import Path

    declared = load_checks((Path(__file__).resolve().parents[2] / CHECKS_FILE).read_text())
    assert {spec.name for spec in prepush.guards(declared)} >= {
        "frontend-types",
        "frontend-knip",
        "frontend-design-system-classes",
        "office-addin-install",
    }


STAND_IN = b"""
[check.frontend-bump-knip]
argv = ["yarn", "knip"]
cwd = "frontend"
image = "toolchain-node"
stands_for = ["frontend-knip"]
"""


def _with_stand_in(session: _Session) -> None:
    session.files[f"repo/{CHECKS_FILE}"] = CHECKS + STAND_IN


@pytest.mark.asyncio
async def test_a_bump_knip_run_on_toolchain_node_counts_as_the_knip_guard() -> None:
    session = _Session()
    _with_stand_in(session)
    session.edit("frontend/src/a.ts", b"export const a = 2;\n")
    assert await _run(session, "frontend-bump-knip", ("yarn", "knip"), 0)
    text = await prepush.commit_warning(session, session.record, await session.changes())
    assert "frontend-knip" not in text
    assert "frontend-types never ran on this checkout" in text


@pytest.mark.asyncio
async def test_a_failed_stand_in_run_is_named_as_the_guard_failing() -> None:
    session = _Session()
    _with_stand_in(session)
    session.edit("frontend/src/a.ts", b"export const a = 1;\nexport const unused = 2;\n")
    assert await _run(session, "frontend-bump-knip", ("yarn", "knip"), 1)
    text = await prepush.commit_warning(session, session.record, await session.changes())
    assert "frontend-knip failed (exit 1) on this tree" in text


@pytest.mark.asyncio
async def test_a_stand_in_run_on_some_files_is_not_kept() -> None:
    session = _Session()
    _with_stand_in(session)
    session.edit("frontend/src/a.ts", b"export const a = 2;\n")
    assert not await _run(session, "frontend-bump-knip", ("yarn", "knip", "src/a.ts"), 0)
    text = await prepush.commit_warning(session, session.record, await session.changes())
    assert "frontend-knip never ran" in text


def test_stands_for_must_name_other_checks() -> None:
    with pytest.raises(ChecksError, match="stands_for"):
        load_checks('[check.x]\nargv = ["true"]\nstands_for = "y"\n')
    with pytest.raises(ChecksError, match="stands_for"):
        load_checks('[check.x]\nargv = ["true"]\nstands_for = ["x"]\n')
    specs = load_checks((CHECKS + STAND_IN).decode())
    assert [g.name for g in prepush.recorded_guards(specs["frontend-bump-knip"], specs)] == ["frontend-knip"]
    assert prepush.recorded_guards(specs["backend-lint"], specs) == []


def test_the_repository_declares_bump_stand_ins_for_tsc_and_knip() -> None:
    from pathlib import Path

    declared = load_checks((Path(__file__).resolve().parents[2] / CHECKS_FILE).read_text())
    assert declared["frontend-bump-knip"].stands_for == ("frontend-knip",)
    assert declared["frontend-bump-knip"].image == declared["frontend-bump-types"].image == "toolchain-node"
    assert declared["frontend-bump-types"].stands_for == ("frontend-types",)
    # A bump's verify-classes on the build (toolchain-node) is the design-system guard's run: nodejs-dev cannot
    # link a bumped lock, so the guard otherwise stays "never ran" after the gate passed.
    assert declared["frontend-bump-classes"].stands_for == ("frontend-design-system-classes",)
    assert declared["frontend-design-system-classes"].pre_push
    assert [g.name for g in prepush.recorded_guards(declared["frontend-bump-classes"], declared)] == [
        "frontend-design-system-classes"
    ]


def test_a_class_change_names_verify_classes_and_a_backend_or_test_change_does_not() -> None:
    """verify-classes guards the frontend sources and stylesheets, under any folder, never a backend file."""
    from pathlib import Path

    spec = load_checks((Path(__file__).resolve().parents[2] / CHECKS_FILE).read_text())["frontend-design-system-classes"]
    assert "--compile" in spec.argv
    changes = CheckoutChanges(
        modified=("frontend/src/components/x/card.tsx", "frontend/src/index.css", "backend/app/x.py"),
        added=("frontend/scripts/other.mjs",),
        removed=(),
    )
    assert prepush.touched(changes, spec) == ["frontend/src/components/x/card.tsx", "frontend/src/index.css"]


def test_shellcheck_guards_only_the_scripts_it_checks_and_skips_those_a_narrow_checkout_lacks() -> None:
    """Gap prepush-shellcheck-guard-narrow-checkout: a change to scripts/check-agents-md-hygiene.sh asked for
    shellcheck, whose fixed list does not hold that script, and the check failed on scripts the checkout lacked."""
    from pathlib import Path

    spec = load_checks((Path(__file__).resolve().parents[2] / CHECKS_FILE).read_text())["shellcheck"]
    other = CheckoutChanges(modified=("scripts/check-agents-md-hygiene.sh",), added=(), removed=())
    assert prepush.touched(other, spec) == []
    held = CheckoutChanges(
        modified=(".github/scripts/ci-license-url.sh", "apps/relay/dist/install.sh"),
        added=("apps/runner/scripts/release-stage.sh",),
        removed=(),
    )
    assert prepush.touched(held, spec) == [
        ".github/scripts/ci-license-url.sh",
        "apps/relay/dist/install.sh",
        "apps/runner/scripts/release-stage.sh",
    ]
    # Every script is run through the existence filter: a narrow checkout skips what it does not hold.
    script = spec.argv[-1]
    assert '[ -f "$f" ]' in script and "[ $# -eq 0 ] ||" in script
    assert "shellcheck apps/" not in script and "shellcheck ." not in script


def test_a_secondary_npm_package_bump_without_its_lock_names_the_install_guard() -> None:
    """Gap prepush-guard-misses-office-addin-lock: a package.json bump of the Office add-in (an npm project out of
    the frontend's workspace) was pushed without its package-lock.json and the commit named only frontend guards;
    CI's `npm ci` then failed out of sync. The add-in's install is the guard of its manifest and lock."""
    from pathlib import Path

    spec = load_checks((Path(__file__).resolve().parents[2] / CHECKS_FILE).read_text())["office-addin-install"]
    assert spec.argv[:2] == ("npm", "ci")
    bump = CheckoutChanges(modified=("apps/microsoft-office/package.json",), added=(), removed=())
    assert prepush.touched(bump, spec) == ["apps/microsoft-office/package.json"]
    session = _Session()
    entries = asyncio.run(session.entries())
    text = prepush.warning(session.record, bump, entries, [spec], {}, checkout="repo")
    assert "office-addin-install never ran on this checkout (apps/microsoft-office/package.json)" in text
    regenerated = CheckoutChanges(
        modified=("apps/microsoft-office/package.json", "apps/microsoft-office/package-lock.json"), added=(), removed=()
    )
    assert prepush.touched(regenerated, spec) == [
        "apps/microsoft-office/package.json",
        "apps/microsoft-office/package-lock.json",
    ]
    # A source change of the add-in or a frontend manifest is not this guard's: tsc and the frontend guards own it.
    other = CheckoutChanges(
        modified=("apps/microsoft-office/src/taskpane.ts", "frontend/package.json"), added=(), removed=()
    )
    assert prepush.touched(other, spec) == []


def test_a_file_added_in_a_new_ee_directory_names_the_editions_gate_and_an_edit_does_not() -> None:
    """Gap pre-push-guard-misses-editions-new-dir: a file added under a new directory was pushed without the Editions
    gate, which declared no pre_push, and CI's Editions job went red one cycle later. The gate guards a source file
    added or removed anywhere in the checkout (git_repo: the whole tree), never an edit of an existing file."""
    from pathlib import Path

    spec = load_checks((Path(__file__).resolve().parents[2] / CHECKS_FILE).read_text())["editions"]
    assert spec.git_repo and spec.pre_push_on == ("added", "removed")
    added = CheckoutChanges(
        modified=("backend/app/x.py",),
        added=("backend/app/services/newpkg/__init__.py", "frontend/src/pages/new/page.tsx", "docs/new.md"),
        removed=("apps/relay/src/old.rs",),
    )
    assert prepush.touched(added, spec) == [
        "backend/app/services/newpkg/__init__.py",
        "frontend/src/pages/new/page.tsx",
        "apps/relay/src/old.rs",
    ]
    session = _Session()
    entries = asyncio.run(session.entries())
    text = prepush.warning(session.record, added, entries, [spec], {}, checkout="repo")
    assert "editions never ran on this checkout (backend/app/services/newpkg/__init__.py" in text
    edit = CheckoutChanges(modified=("backend/app/x.py", "frontend/src/a.ts"), added=(), removed=())
    assert prepush.touched(edit, spec) == []


def test_pre_push_on_defaults_to_every_change_and_refuses_an_unknown_kind() -> None:
    specs = load_checks(CHECKS.decode())
    assert specs["frontend-knip"].pre_push_on == ("added", "modified", "removed")
    for bad in ('"added"', "[]", '["renamed"]'):
        with pytest.raises(ChecksError, match="pre_push_on"):
            load_checks(f'[check.x]\nargv = ["true"]\npre_push = ["*.py"]\npre_push_on = {bad}\n')
    only_added = load_checks('[check.x]\nargv = ["true"]\npre_push = ["*.py"]\npre_push_on = ["removed", "added"]\n')
    assert only_added["x"].pre_push_on == ("added", "removed")
    changes = CheckoutChanges(modified=("a.py",), added=("b.py",), removed=("c.py",))
    assert prepush.touched(changes, only_added["x"]) == ["b.py", "c.py"]
