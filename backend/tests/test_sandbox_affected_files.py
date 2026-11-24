"""sandbox_run_command(affected=true) on a check that declares affected = "files" | "lines" (a formatter)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.services.sandbox_workspace import affected
from app.services.sandbox_workspace import checkout as checkout_mod
from app.services.sandbox_workspace.checkout import CheckoutRecord, advance, build_record, changes_since
from app.services.sandbox_workspace.checks import ChecksError, describe_checks, load_checks
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.rebase import rebased_record
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope
from scripts import format_changed

UNFORMATTED = b"x = {  'a':1 }\n\n\ndef f( a ):\n    return a\n\n\ny=[1,2]\n"
EDITED = b"x = {  'a':1 }\n\n\ndef f( a ):\n    return a\n\n\ndef added(b):\n    return b\n\n\ny=[1,2]\n"


def _workspace() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())
    return WorkspaceSession(
        scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=8 * 1024 * 1024, max_files=200)
    )


def test_line_ranges_are_the_lines_the_change_wrote_merged():
    assert affected.line_ranges("a\nb\nc\n", "a\nB\nnew\nc\nd\n") == [(2, 3), (5, 5)]
    assert affected.line_ranges("a\nb\n", "a\n") == []


def test_globs_match_the_path_or_the_name_and_no_glob_keeps_all():
    assert affected.matches_globs("app/x.py", ["*.py"])
    assert affected.matches_globs("app/x.py", ["app/*"])
    assert not affected.matches_globs("app/x.md", ["*.py", "*.pyi"])
    assert affected.matches_globs("anything.txt", [])


async def test_a_new_file_runs_whole_and_an_edited_unformatted_file_only_on_its_changed_lines():
    """The acceptance of the change: the drift of the existing file's untouched lines is never handed to the formatter."""
    workspace = _workspace()
    tree = [("backend/app/old.py", UNFORMATTED), ("backend/README.md", b"# x\n"), ("frontend/a.py", b"z = 1\n")]
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=[], files=tree
    )
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree(
        [
            ("co/backend/app/old.py", EDITED),
            ("co/backend/app/new.py", b"def g( ):\n    pass\n"),
            ("co/backend/README.md", b"# x\n\nmore\n"),
            ("co/frontend/a.py", b"z = 2\n"),
        ]
    )
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree)[repo_path]

    files = await affected.changed_files(
        workspace, record, changes, cwd="co/backend", read_base=read_base, globs=["*.py", "*.pyi"], lines=True
    )
    # Outside the check's directory (frontend/) is not named; prose is left out by the globs and said.
    # The four inserted lines (def added, its body, two blank lines); lines 1-7 and the drift on line 12 are not named.
    assert files.args == ["app/new.py", "app/old.py:8-11"]
    assert files.skipped == ["README.md"]
    text = files.describe(mode="lines")
    assert text.startswith("Changed lines: 2 file(s)") and "README.md" in text

    whole = await affected.changed_files(
        workspace, record, changes, cwd="co/backend", read_base=read_base, globs=["*.py"]
    )
    assert whole.args == ["app/new.py", "app/old.py"]

    # A repository-wide selection script takes the files outside its directory too, relative to it.
    every = await affected.changed_files(
        workspace, record, changes, cwd="co/backend", read_base=read_base, whole_repository=True
    )
    assert every.args == ["README.md", "app/new.py", "app/old.py", "../frontend/a.py"]


async def test_an_empty_change_after_a_commit_says_committed_lines_are_left_out():
    """#11122: once a commit moved the record, an empty change must not read as "the committed lines were checked"."""
    workspace = _workspace()
    tree = [("backend/app/old.py", UNFORMATTED)]
    record = build_record(
        provider="github", repository="o/r", ref="b", commit="c" * 40, path="co", sub_paths=[], files=tree
    )
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    changes = changes_since(record, await workspace.entries(refresh=True))
    assert changes.empty

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree)[repo_path]

    files = await affected.changed_files(workspace, record, changes, cwd="co/backend", read_base=read_base)
    assert files.args == []
    assert "commits made from it" in affected.COMMITTED_LEFT_OUT
    assert "as args" in affected.COMMITTED_LEFT_OUT


async def test_an_unreadable_base_passes_the_file_whole_and_a_pure_removal_checks_nothing():
    workspace = _workspace()
    tree = [("backend/a.py", b"a = 1\nb = 2\n"), ("backend/b.py", b"c = 3\n")]
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=[], files=tree
    )
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([("co/backend/a.py", b"a = 1\n"), ("co/backend/b.py", b"c = 4\n")])
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree)[repo_path] if repo_path == "backend/a.py" else "not readable"

    files = await affected.changed_files(workspace, record, changes, cwd="co/backend", read_base=read_base, lines=True)
    assert files.args == ["b.py"]
    assert any("passed whole" in n and "b.py" in n for n in files.notes)
    assert any("Only lines removed" in n and "a.py" in n for n in files.notes)
    assert files.removed_only == ["a.py"]


async def test_bare_paths_named_to_a_fixer_are_narrowed_to_the_changed_lines():
    """The acceptance of #11073: naming a drifted file to backend-format-fix hands ruff its changed range only."""
    workspace = _workspace()
    tree = [("backend/app/old.py", UNFORMATTED), ("backend/app/same.py", b"q=1\n"), ("backend/app/cut.py", b"a\nb\n")]
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=[], files=tree
    )
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree(
        [
            ("co/backend/app/old.py", EDITED),
            ("co/backend/app/new.py", b"def g( ):\n    pass\n"),
            ("co/backend/app/cut.py", b"a\n"),
        ]
    )
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree)[repo_path]

    files = await affected.changed_files(
        workspace, record, changes, cwd="co/backend", read_base=read_base, globs=["*.py"], lines=True
    )
    # A bare modified file gets its range; an untouched one passes whole and is said; a ranged arg is kept.
    args, notes = affected.narrow_args(["app/old.py", "app/same.py", "app/x.py:1-2"], files)
    assert args == ["app/old.py:8-11", "app/same.py", "app/x.py:1-2"]
    assert any("Not changed since the checkout" in n and "app/same.py" in n for n in notes)
    # A directory is the changed files under it; a file the change only cut lines from is left out.
    args, notes = affected.narrow_args(["./app/"], files)
    assert args == ["app/new.py", "app/old.py:8-11"]
    args, notes = affected.narrow_args(["app/cut.py"], files)
    assert args == [] and any("Only lines removed" in n for n in notes)
    assert affected.narrow_args(["."], files)[0] == ["app/new.py", "app/old.py:8-11"]
    # backend-format args ["--write"]: an option alone runs on the whole change, as no args would.
    assert affected.narrow_args(["--write"], files) == (["--write", "app/new.py", "app/old.py:8-11"], [])
    assert affected.narrow_args(["--write", "app/old.py"], files)[0] == ["--write", "app/old.py:8-11"]


async def test_lines_a_pushed_commit_wrote_stay_the_branchs_own_after_the_push() -> None:
    """changed-lines-baseline-after-push: once create_github_commit(from_checkout) moved the record, the files the
    branch added stay whole and a modified file is ranged against the commit it was checked out at; the drift main
    already had on untouched lines is still left out."""
    workspace = _workspace()
    tree = [("backend/app/old.py", UNFORMATTED), ("backend/app/same.py", b"q=1\n")]
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="a" * 40, path="co", sub_paths=[], files=tree
    )
    new = b"def g( ):\n    x: int = 'no'\n    return x\n"
    pushed = advance(
        record,
        commit="b" * 40,
        ref="fix/x",
        written={"backend/app/new.py": new, "backend/app/old.py": EDITED},
        removed=(),
    )
    assert pushed.commit == "b" * 40 and pushed.baseline == "a" * 40
    assert pushed.branch_paths() == {"backend/app/new.py": "A", "backend/app/old.py": "M"}
    await workspace.put_tree(
        [
            ("co/backend/app/old.py", EDITED),
            ("co/backend/app/new.py", new),
            ("co/backend/app/same.py", b"q=1\n"),
            (pushed.record_path, pushed.to_json()),
        ]
    )
    changes = changes_since(pushed, await workspace.entries(refresh=True))
    assert changes.empty
    reads: list[str] = []

    async def read_base(repo_path: str) -> bytes | str:
        reads.append(repo_path)
        return dict(tree)[repo_path]

    files = await affected.changed_files(
        workspace, pushed, changes, cwd="co/backend", read_base=read_base, globs=["*.py"], lines=True
    )
    # (1) the added file whole: its format and type findings are the branch's; the edit's lines against main.
    # (2) lines 1-7 and the drift on line 12 of old.py, and same.py, are not the branch's: still left out.
    assert files.args == ["app/new.py", "app/old.py:8-11"]
    assert reads == ["backend/app/old.py"]


async def test_after_a_push_a_new_edit_joins_the_committed_change_and_a_fresh_checkout_is_unchanged() -> None:
    """The after-push path: a second edit of a committed file is ranged with the commit's lines; a record no commit
    moved keeps the old baseline (its commit) and the old change."""
    tree = [("backend/app/old.py", UNFORMATTED)]
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="a" * 40, path="co", sub_paths=[], files=tree
    )
    assert record.baseline == "a" * 40 and record.branch_paths() == {}
    assert checkout_mod.baseline_origin(record) == checkout_mod.origin(record)
    assert "base" not in json.loads(record.to_json())
    pushed = advance(record, commit="b" * 40, ref="fix/x", written={"backend/app/old.py": EDITED}, removed=())
    again = advance(pushed, commit="c" * 40, ref="fix/x", written={"backend/app/n.py": b"z\n"}, removed=())
    assert again.baseline == "a" * 40
    assert again.branch_paths() == {"backend/app/old.py": "M", "backend/app/n.py": "A"}
    assert "a" * 12 in checkout_mod.baseline_origin(again) and "c" * 12 in checkout_mod.baseline_origin(again)
    # A file the branch added then deleted is no longer part of its change.
    gone = advance(again, commit="d" * 40, ref="fix/x", written={}, removed=["backend/app/n.py"])
    assert gone.branch_paths() == {"backend/app/old.py": "M"}
    # The record round-trips its baseline.
    read = CheckoutRecord.from_json(gone.to_json(), path="co")
    assert read.baseline == "a" * 40 and read.branch_paths() == {"backend/app/old.py": "M"}

    workspace = _workspace()
    third = EDITED + b"\n\ndef more( ):\n    pass\n"
    await workspace.put_tree([("co/backend/app/old.py", third), (gone.record_path, gone.to_json())])
    changes = changes_since(gone, await workspace.entries(refresh=True))
    assert changes.modified == ("backend/app/old.py",)

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree)[repo_path]

    files = await affected.changed_files(workspace, gone, changes, cwd="co/backend", read_base=read_base, lines=True)
    assert files.args == ["app/old.py:8-11,13-16"]


def test_a_rebase_moves_the_branch_baseline_to_the_ref() -> None:
    tree = [("backend/app/old.py", UNFORMATTED)]
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="a" * 40, path="co", sub_paths=[], files=tree
    )
    pushed = advance(record, commit="b" * 40, ref="fix/x", written={"backend/app/old.py": EDITED}, removed=())
    moved = rebased_record(pushed, ref="main", commit="e" * 40, theirs=dict(tree))
    assert moved.baseline == "e" * 40 and moved.branch_paths() == {}


def test_checks_toml_declares_the_affected_mode_and_its_globs():
    specs = load_checks(
        '[check.fmt]\nargv = ["fmt"]\naffected = "lines"\naffected_globs = ["*.py"]\n[check.unit]\nargv = ["pytest"]\n'
    )
    assert specs["fmt"].affected == "lines" and specs["fmt"].affected_globs == ("*.py",)
    assert specs["unit"].affected == "tests" and specs["unit"].affected_globs == ()
    listing = describe_checks(specs)
    assert "[affected=true: the changed lines]" in listing and "unit: pytest" in listing
    with pytest.raises(ChecksError, match="affected must be one of"):
        load_checks('[check.x]\nargv = ["x"]\naffected = "everything"\n')
    with pytest.raises(ChecksError, match="affected_globs applies"):
        load_checks('[check.x]\nargv = ["x"]\naffected_globs = ["*.py"]\n')


def test_checks_toml_declares_the_ratchet_gates_a_tests_check_always_runs():
    specs = load_checks('[check.unit]\nargv = ["vitest"]\naffected_always = ["tests_unit/*-gate.test.ts"]\n')
    assert specs["unit"].affected_always == ("tests_unit/*-gate.test.ts",)
    with pytest.raises(ChecksError, match="affected_always applies"):
        load_checks('[check.x]\nargv = ["x"]\naffected = "lines"\naffected_always = ["a.test.ts"]\n')
    with pytest.raises(ChecksError, match="affected_always must be a list"):
        load_checks('[check.x]\nargv = ["x"]\naffected_always = "a.test.ts"\n')


def test_checks_toml_declares_the_source_scan_gate_patterns_of_a_tests_check() -> None:
    specs = load_checks('[check.unit]\nargv = ["pytest"]\naffected_scan_patterns = ["from tests import app_sources"]\n')
    assert specs["unit"].affected_scan_patterns == ("from tests import app_sources",)
    with pytest.raises(ChecksError, match="affected_scan_patterns applies"):
        load_checks('[check.x]\nargv = ["x"]\naffected = "lines"\naffected_scan_patterns = ["a"]\n')
    with pytest.raises(ChecksError, match="is not a regular expression"):
        load_checks('[check.x]\nargv = ["x"]\naffected_scan_patterns = ["("]\n')


def test_the_repository_backend_unit_check_selects_the_app_sources_tripwires() -> None:
    """#659 was dequeued on a flag-tier guard of test_assignment_visibility.py that affected=true never ran."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = load_checks((root / ".xtm-one" / "checks.toml").read_text(encoding="utf-8"))["backend-unit"]
    guard = (root / "backend" / "tests" / "test_assignment_visibility.py").read_text(encoding="utf-8")
    assert any(re.search(pattern, guard) for pattern in spec.affected_scan_patterns)


def test_the_repository_frontend_unit_check_always_runs_the_form_ratchet_gates():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = load_checks((root / ".xtm-one" / "checks.toml").read_text(encoding="utf-8"))["frontend-unit"]
    assert "tests_unit/form-controls-gate.test.ts" in spec.affected_always
    assert "src/pages/settings/settings-form-kit.test.ts" in spec.affected_always
    for path in spec.affected_always:
        assert (root / "frontend" / path).is_file(), path


def test_the_repository_frontend_unit_check_runs_the_affected_tests_without_args() -> None:
    """A bare frontend-unit call never starts the 855-file suite (337 s on ECONNREFUSED 127.0.0.1:3100)."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = load_checks((root / ".xtm-one" / "checks.toml").read_text(encoding="utf-8"))["frontend-unit"]
    assert spec.affected == "tests" and spec.affected_by_default
    assert 0 < spec.whole_timeout < spec.timeout
    # The whole suite ran 889 s in the sandbox on 14 Nov 2026 (#11815): its bound keeps headroom over that.
    assert spec.whole_timeout >= 1200
    # The sharded opt-in, as ci.yml splits the suite, is named where the listing shows it.
    assert "--shard=K/3" in spec.description


def test_the_repository_backend_format_check_runs_on_the_changed_lines():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / ".xtm-one" / "checks.toml").read_text(encoding="utf-8")
    spec = load_checks(text)["backend-format"]
    assert spec.affected == "lines" and "*.py" in spec.affected_globs
    assert spec.argv == ("python", "-m", "scripts.format_changed")
    # A checkout has no .git: without args the check runs on the change, never the ~3,500 drifted files.
    assert spec.affected_by_default
    # A file named as an arg is narrowed to the change's lines of it, as backend-format-fix does, so main's
    # drift in that file is never the answer; --write is the one-call write path the description names.
    assert spec.affected_narrows_args
    assert "--write" in spec.description and "narrowed to its changed lines" in spec.description


def test_affected_by_default_is_a_files_or_lines_switch_shown_in_the_listing():
    specs = load_checks('[check.fmt]\nargv = ["fmt"]\naffected = "lines"\naffected_by_default = true\n')
    assert specs["fmt"].affected_by_default
    assert "[no args: affected=true; --whole alone: the whole run, at most 600 s]" in describe_checks(specs)
    assert not load_checks('[check.fmt]\nargv = ["fmt"]\naffected = "lines"\n')["fmt"].affected_by_default
    with pytest.raises(ChecksError, match="affected_by_default must be true or false"):
        load_checks('[check.x]\nargv = ["x"]\naffected = "files"\naffected_by_default = "yes"\n')


def test_a_tests_check_may_run_on_the_change_by_default_with_a_bounded_whole_run() -> None:
    specs = load_checks(
        '[check.unit]\nargv = ["vitest"]\ntimeout = 1800\naffected_by_default = true\nwhole_timeout = 900\n'
    )
    assert specs["unit"].affected == "tests" and specs["unit"].affected_by_default
    assert specs["unit"].whole_timeout == 900
    assert "--whole alone: the whole run, at most 900 s" in describe_checks(specs)
    # The bound never exceeds the check's own timeout.
    capped = load_checks('[check.u]\nargv = ["v"]\ntimeout = 60\naffected_by_default = true\nwhole_timeout = 900\n')
    assert capped["u"].whole_timeout == 60
    # On a tests check whole_timeout alone turns it on: a parser older than the key ignores it.
    assert load_checks('[check.u]\nargv = ["v"]\nwhole_timeout = 900\n')["u"].affected_by_default
    with pytest.raises(ChecksError, match="whole_timeout applies to"):
        load_checks('[check.u]\nargv = ["v"]\naffected = "files"\nwhole_timeout = 900\n')
    with pytest.raises(ChecksError, match="whole_timeout must be a positive number"):
        load_checks('[check.u]\nargv = ["v"]\naffected_by_default = true\nwhole_timeout = "long"\n')


def test_affected_narrows_args_is_a_files_or_lines_switch_shown_in_the_listing():
    specs = load_checks('[check.fix]\nargv = ["fix"]\naffected = "lines"\naffected_narrows_args = true\n')
    assert specs["fix"].affected_narrows_args
    assert "[args narrowed to the changed lines; --whole: as given]" in describe_checks(specs)
    with pytest.raises(ChecksError, match="affected_narrows_args applies"):
        load_checks('[check.x]\nargv = ["x"]\naffected_narrows_args = true\n')
    with pytest.raises(ChecksError, match="affected_narrows_args must be true or false"):
        load_checks('[check.x]\nargv = ["x"]\naffected = "lines"\naffected_narrows_args = 1\n')


def test_affected_whole_repository_is_a_files_switch_shown_in_the_listing():
    specs = load_checks('[check.q]\nargv = ["q"]\naffected = "files"\naffected_whole_repository = true\n')
    assert specs["q"].affected_whole_repository
    assert "changed files of the whole repository" in describe_checks(specs)
    with pytest.raises(ChecksError, match="affected_whole_repository applies"):
        load_checks('[check.x]\nargv = ["x"]\naffected = "lines"\naffected_whole_repository = true\n')
    with pytest.raises(ChecksError, match="affected_whole_repository must be true or false"):
        load_checks('[check.x]\nargv = ["x"]\naffected = "files"\naffected_whole_repository = 1\n')


def test_the_repository_lint_checks_take_a_path_and_cover_the_lab():
    from pathlib import Path

    from app.services.sandbox_workspace.checks import command_argv

    text = (Path(__file__).resolve().parents[2] / ".xtm-one" / "checks.toml").read_text(encoding="utf-8")
    checks = load_checks(text)
    backend = checks["backend-lint"]
    assert command_argv(backend)[-1] == "."
    assert command_argv(backend, ["app/x.py"])[-1] == "app/x.py" and "." not in command_argv(backend, ["app/x.py"])
    lab = checks["lab-lint"]
    assert lab.cwd == ".xtm-one" and lab.report == "sarif"
    assert "--isolated" in lab.argv and lab.default_args == ("lab", "stores.py")
    assert command_argv(lab, ["lab/lab_asgi.py"])[-1] == "lab/lab_asgi.py"


def test_format_changed_runs_ruff_on_whole_files_and_never_with_a_widening_range():
    """A ranged file is formatted through stdin and split by line; ``ruff --range`` widened to the enclosing statement."""
    assert format_changed.parse("app/x.py:3-9,20-20") == ("app/x.py", ["3-9", "20-20"])
    assert format_changed.parse("app/x.py") == ("app/x.py", [])
    assert format_changed.commands(["new.py", "old.py:8-10,30-31"]) == [
        ["ruff", "format", "--check", "--diff", "new.py"]
    ]
    assert format_changed.commands(["new.py", "old.py:8-10"], fix=True) == [["ruff", "format", "new.py"]]
    assert format_changed.commands([]) == []
    assert format_changed.lines_of(["3-4", "9-9"]) == {3, 4, 9}


def test_split_sorts_each_edit_by_the_changed_lines_it_touches():
    source = [b"a\n", b"B\n", b"C\n", b"d\n", b"e\n", b"f\n", b"g\n"]
    formatted = [b"a\n", b"b\n", b"c\n", b"d\n", b"ef\n", b"g\n"]
    parts = format_changed.split(source, formatted, {2, 5})
    assert parts.yours == [(1, 2, 1, 2)]  # line 2, changed
    assert parts.preexisting == [(2, 3, 2, 3)]  # line 3, drift next to the change, split from line 2
    assert parts.enclosing == [(4, 6, 4, 5)]  # lines 5-6 joined: one changed, one not
    assert format_changed.apply(source, formatted, parts.yours) == [b"a\n", b"b\n", *source[2:]]
    assert format_changed.label(parts.enclosing) == "5-6"


def test_the_pre_existing_drift_names_its_first_ranges_and_counts_the_rest() -> None:
    """Gap format-changed-preexisting-list: a drifted module printed its 456 ranges, about 19,800 characters."""
    edits = [(i * 3, i * 3 + 1, i * 3, i * 3 + 1) for i in range(20)]
    shown = format_changed.label(edits, format_changed.PREEXISTING_SHOWN)
    assert shown == "1, 4, 7, 10, 13, 16, 19, 22, and 12 more"
    assert format_changed.label(edits[:3], format_changed.PREEXISTING_SHOWN) == "1, 4, 7"
    assert format_changed.label(edits).count(",") == 19


def test_format_changed_fix_rewrites_only_the_changed_lines_of_a_drifted_file(tmp_path, monkeypatch):
    """The acceptance of the changed-lines fix: the pre-existing drift (lines 1-7 and 12) is left as it is."""
    import shutil

    if shutil.which("ruff") is None:
        pytest.skip("ruff is not on PATH")
    drifted = EDITED.replace(b"def added(b):", b"def added( b ):").replace(b"return b", b"return  b")
    target = tmp_path / "old.py"
    target.write_bytes(drifted)
    monkeypatch.chdir(tmp_path)
    assert format_changed.main(["--fix", "old.py:8-11"]) == 0
    assert target.read_bytes() == EDITED
    before, after = drifted.splitlines(), target.read_bytes().splitlines()
    assert before[:7] == after[:7] and before[-1] == after[-1] == b"y=[1,2]"


def _ruff_or_skip() -> None:
    import shutil

    if shutil.which("ruff") is None:
        pytest.skip("ruff is not on PATH")


DRIFTED_DICT = b"x = {  'a':1 }\n\n\nX = {\n    'a':1,\n    \"b\": 2,\n}\n"


def test_a_range_never_expands_to_the_enclosing_drift(tmp_path, monkeypatch, capsys):
    """Folded gap format-changed-range-expands-to-enclosing-drift: line 6 is formatted, line 5 of the same dict drifts."""
    _ruff_or_skip()
    target = tmp_path / "old.py"
    target.write_bytes(DRIFTED_DICT)
    monkeypatch.chdir(tmp_path)
    assert format_changed.main(["old.py:6-6"]) == 0
    out = capsys.readouterr().out
    assert "pre-existing, not yours: 2 edit(s) on lines 1, 5" in out and "---" not in out
    assert format_changed.main(["--fix", "old.py:6-6"]) == 0
    assert target.read_bytes() == DRIFTED_DICT


JOINED = b"y=[1,2]\n\n\nz = foo(\n    1,\n    2\n)\n"


def test_a_statement_reformatted_whole_is_named_not_written(tmp_path, monkeypatch, capsys):
    """Folded gap format-changed-reformats-enclosing-statement: ruff joins lines 4-7 into one, only line 6 changed."""
    _ruff_or_skip()
    target = tmp_path / "old.py"
    target.write_bytes(JOINED)
    monkeypatch.chdir(tmp_path)
    assert format_changed.main(["old.py:6-6"]) == 0
    out = capsys.readouterr().out
    assert "lines 4-7 reformat as one statement" in out and "old.py:4-7" in out
    assert format_changed.main(["--fix", "old.py:6-6"]) == 0
    assert target.read_bytes() == JOINED
    assert format_changed.main(["--fix", "old.py:4-7"]) == 0
    assert target.read_bytes() == b"y=[1,2]\n\n\nz = foo(1, 2)\n"


def test_a_one_line_change_in_a_drifted_file_is_a_one_line_diff(tmp_path, monkeypatch, capsys):
    """Folded gap format-check-preexisting-findings: the findings outside the change are reported apart, never in the diff."""
    _ruff_or_skip()
    target = tmp_path / "old.py"
    target.write_bytes(PROBED)
    monkeypatch.chdir(tmp_path)
    assert format_changed.main(["old.py:5-5"]) == 1
    out = capsys.readouterr().out
    diff = [line for line in out.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    assert diff == ["-_PROBE   =   {  'a':1,'b' :2 }", '+_PROBE = {"a": 1, "b": 2}']
    assert "pre-existing, not yours: 3 edit(s) on lines 1, 4, 6" in out
    assert target.read_bytes() == PROBED


PROBED = b"x = {  'a':1 }\n\n\ny=[1,2]\n_PROBE   =   {  'a':1,'b' :2 }\n_PROBE_2   =   [ 1,2 ,3 ]\n"


def test_a_one_line_range_is_flagged_by_the_check_and_formatted_by_the_fix(tmp_path, monkeypatch):
    """The retest of the changed-lines fix: a single-line edit and the last line of a hunk are checked and formatted, the drift above is not."""
    import shutil

    if shutil.which("ruff") is None:
        pytest.skip("ruff is not on PATH")
    target = tmp_path / "old.py"
    target.write_bytes(PROBED)
    monkeypatch.chdir(tmp_path)
    assert format_changed.main(["old.py:5-5"]) == 1
    assert format_changed.main(["old.py:6-6"]) == 1
    assert target.read_bytes() == PROBED
    assert format_changed.main(["--fix", "old.py:5-6"]) == 0
    lines = target.read_bytes().splitlines()
    assert lines[:4] == PROBED.splitlines()[:4]
    assert lines[4:] == [b'_PROBE = {"a": 1, "b": 2}', b"_PROBE_2 = [1, 2, 3]"]
    assert format_changed.main(["old.py:5-6"]) == 0


#: Lines 1, 3 and 5 drift on main; lines 2, 4 and 6 are the change's three hunks, badly formatted.
THREE_HUNKS = b"x = {  'a':1 }\na1   =  1\ny=[1,2]\nb1   =  2\nz=( 3 )\nc1   =  3\n"


def test_three_hunks_in_a_drifted_file_report_only_those_hunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gap backend-format-check (a): the diff holds the change's three hunks, main's drift is counted apart."""
    _ruff_or_skip()
    target = tmp_path / "old.py"
    target.write_bytes(THREE_HUNKS)
    monkeypatch.chdir(tmp_path)
    assert format_changed.main(["old.py:2-2,4-4,6-6"]) == 1
    out = capsys.readouterr().out
    diff = [line for line in out.splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    assert diff == ["-a1   =  1", "+a1 = 1", "-b1   =  2", "+b1 = 2", "-c1   =  3", "+c1 = 3"]
    assert "pre-existing, not yours: 3 edit(s) on lines 1, 3, 5" in out
    assert target.read_bytes() == THREE_HUNKS


def test_a_clean_change_reports_nothing_while_main_drifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gap backend-format-check (b): formatted hunks in a drifted file exit 0 with no diff."""
    _ruff_or_skip()
    clean = (
        THREE_HUNKS.replace(b"a1   =  1", b"a1 = 1").replace(b"b1   =  2", b"b1 = 2").replace(b"c1   =  3", b"c1 = 3")
    )
    (tmp_path / "old.py").write_bytes(clean)
    monkeypatch.chdir(tmp_path)
    assert format_changed.main(["old.py:2-2,4-4,6-6"]) == 0
    out = capsys.readouterr().out
    assert "---" not in out and "+++" not in out and ": formatted; apart, not written: 3 pre-existing" in out


def test_write_formats_the_changed_lines_in_place_in_one_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Gap backend-format-check (c): backend-format args ["--write"] is the write path, the drift stays as it is."""
    _ruff_or_skip()
    target = tmp_path / "old.py"
    target.write_bytes(THREE_HUNKS)
    monkeypatch.chdir(tmp_path)
    assert format_changed.main(["--write", "old.py:2-2,4-4,6-6"]) == 0
    assert target.read_bytes().splitlines() == [
        b"x = {  'a':1 }",
        b"a1 = 1",
        b"y=[1,2]",
        b"b1 = 2",
        b"z=( 3 )",
        b"c1 = 3",
    ]
    assert format_changed.main(["old.py:2-2,4-4,6-6"]) == 0


def test_an_unknown_option_is_refused_with_the_write_path_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An option is never handed to ruff as a path: it is refused, and the message names --write and backend-format-fix."""
    target = tmp_path / "old.py"
    target.write_bytes(THREE_HUNKS)
    monkeypatch.chdir(tmp_path)
    assert format_changed.main(["--in-place", "old.py:2-2"]) == 2
    err = capsys.readouterr().err
    assert "unknown option(s) --in-place" in err and "['--write']" in err and "backend-format-fix" in err
    assert target.read_bytes() == THREE_HUNKS
