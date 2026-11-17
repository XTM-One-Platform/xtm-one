"""The checkout record and the change set computed from it.

Pure tests: the record's round trip, ``changes_since`` over a manifest, and
the status lines and per-file diffs of ``sandbox_checkout_status`` /
``sandbox_checkout_diff``, and ``advance``: the record moved to the
commit made from it.
The tool-level tests (``materialize_repository`` writing the record,
``create_github_commit(from_checkout=...)``) are in
``test_sandbox_workspace_tools.py`` and the GitHub provider tests.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest

from app.services.sandbox_workspace.checkout import (
    RECORD_NAME,
    RECORD_VERSION,
    CheckoutRecord,
    CheckoutRecordError,
    RecordedFile,
    advance,
    advance_files,
    build_record,
    changes_since,
    clamp_diff_context,
    file_diff,
    format_status,
    status_lines,
)


@dataclass(frozen=True)
class _Entry:
    sha256: str


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record() -> CheckoutRecord:
    return build_record(
        provider="github",
        repository="acme/widgets",
        ref="main",
        commit="0123abcd",
        path="base",
        sub_paths=("src",),
        files=[("src/a.py", b"a = 1\n"), ("src/pkg/b.py", b"b = 2\n")],
    )


def _manifest(record: CheckoutRecord, overrides: dict[str, bytes | None] | None = None) -> dict[str, _Entry]:
    entries = {record.workspace_path(p): _Entry(f.sha256) for p, f in record.files.items()}
    entries[record.record_path] = _Entry("record")
    for repo_path, data in (overrides or {}).items():
        key = record.workspace_path(repo_path)
        if data is None:
            entries.pop(key, None)
        else:
            entries[key] = _Entry(_sha(data))
    return entries


def test_the_record_round_trips_through_json() -> None:
    record = _record()
    again = CheckoutRecord.from_json(record.to_json(), path="base")
    assert again == record
    assert again.record_path == f"base/{RECORD_NAME}"
    assert b'"version": %d' % RECORD_VERSION in record.to_json()


def test_a_record_of_another_version_or_a_broken_one_is_refused_with_what_to_do() -> None:
    with pytest.raises(CheckoutRecordError, match="check the repository out again"):
        CheckoutRecord.from_json(b'{"version": 99, "files": {}}', path="base")
    with pytest.raises(CheckoutRecordError, match="not a checkout record"):
        CheckoutRecord.from_json(b"not json", path="base")
    with pytest.raises(CheckoutRecordError, match="no file list"):
        CheckoutRecord.from_json(b'{"version": 1}', path="base")


def test_an_unchanged_checkout_has_no_change_and_the_record_is_never_one() -> None:
    record = _record()
    changes = changes_since(record, _manifest(record))
    assert changes.empty
    assert changes.describe() == "no change"


def test_edits_additions_and_removals_under_the_recorded_sub_paths_are_the_change_set() -> None:
    record = _record()
    entries = _manifest(record, {"src/a.py": b"a = 2\n", "src/new.py": b"new\n", "src/pkg/b.py": None})
    changes = changes_since(record, entries)
    assert changes.modified == ("src/a.py",)
    assert changes.added == ("src/new.py",)
    assert changes.removed == ("src/pkg/b.py",)
    assert changes.count == 3
    assert "1 modified (src/a.py)" in changes.describe()


def test_a_file_outside_the_recorded_sub_paths_or_the_checkout_is_not_a_change() -> None:
    record = _record()
    entries = _manifest(record)
    entries["base/README.md"] = _Entry(_sha(b"stray"))
    entries["work/src/a.py"] = _Entry(_sha(b"elsewhere"))
    assert changes_since(record, entries).empty


def test_a_bytecode_cache_an_import_wrote_is_never_a_change() -> None:
    """code_interpreter importing a checkout module leaves the status clean (#9518)."""
    record = _record()
    entries = _manifest(
        record,
        {
            "src/__pycache__/a.cpython-314.pyc": b"\x00bytecode",
            "src/pkg/__pycache__/b.cpython-314.pyc": b"\x00bytecode",
            "src/legacy.pyc": b"\x00bytecode",
        },
    )
    assert changes_since(record, entries).empty
    assert changes_since(record, entries, paths=["src"]).empty
    entries[record.workspace_path("src/new.py")] = _Entry(_sha(b"new\n"))
    assert changes_since(record, entries).added == ("src/new.py",)


def test_the_paths_filter_narrows_the_change_set() -> None:
    record = _record()
    entries = _manifest(record, {"src/a.py": b"a = 2\n", "src/pkg/b.py": b"b = 3\n"})
    assert changes_since(record, entries).count == 2
    narrowed = changes_since(record, entries, paths=["src/pkg"])
    assert narrowed.modified == ("src/pkg/b.py",)
    assert narrowed.removed == ()


def test_the_paths_filter_keeps_an_added_file_beside_a_modified_one() -> None:
    record = _record()
    entries = _manifest(record, {"src/a.py": b"a = 2\n", "src/new_test.py": b"def test(): ...\n"})
    changes = changes_since(record, entries, paths=["src/a.py", "src/new_test.py"])
    assert changes.modified == ("src/a.py",)
    assert changes.added == ("src/new_test.py",)
    assert changes.unmatched == ()


def test_a_file_named_in_paths_outside_the_recorded_sub_paths_is_added() -> None:
    # A narrow checkout (sub_paths=src) and a new test written beside it:
    # naming it in paths commits it instead of dropping it silently.
    record = _record()
    entries = _manifest(record, {"src/a.py": b"a = 2\n"})
    entries["base/tests/test_a.py"] = _Entry(_sha(b"def test(): ...\n"))
    changes = changes_since(record, entries, paths=["src/a.py", "tests/test_a.py"])
    assert changes.modified == ("src/a.py",)
    assert changes.added == ("tests/test_a.py",)
    # Without paths, a stray file outside the sub_paths is still never a change.
    assert changes_since(record, entries).added == ()


def test_an_out_of_scope_edit_is_named_against_the_baseline_through_every_record_rewrite() -> None:
    """The baseline is a hash per file, carried by advance and grown: no record rewrite or clock hides an edit (#10144)."""
    from app.services.sandbox_workspace.checkout import grown, outside_baseline

    before = {"base/README.md": _Entry(_sha(b"# repo\n")), "base/docs/a.md": _Entry(_sha(b"a\n"))}
    before["base/node_modules/x.js"] = _Entry(_sha(b"x"))
    baseline = outside_baseline("base", ("src",), ("src/a.py",), before)
    assert set(baseline) == {"README.md", "docs/a.md"}
    record = build_record(
        provider="github",
        repository="acme/widgets",
        ref="main",
        commit="0123abcd",
        path="base",
        sub_paths=("src",),
        files=[("src/a.py", b"a = 1\n")],
        outside=baseline,
    )
    assert CheckoutRecord.from_json(record.to_json(), path="base") == record
    entries = {**_manifest(record), **before}
    assert changes_since(record, entries).untracked == ()
    entries["base/README.md"] = _Entry(_sha(b"# edited\n"))
    del entries["base/docs/a.md"]
    entries["base/tests/test_new.py"] = _Entry(_sha(b"t\n"))
    expected = ("README.md", "docs/a.md", "tests/test_new.py")
    assert changes_since(record, entries).untracked == expected
    # No mtime at all: the hash alone decides, whatever the clocks say.
    moved = advance(record, commit="4567", ref="main", written={"src/a.py": b"a = 2\n"}, removed=[])
    assert changes_since(moved, entries).untracked == expected
    # Growing over docs/ makes it tracked: it leaves the baseline and the note.
    bigger = grown(moved, sub_paths=["docs"], files=[("docs/a.md", b"a\n")])
    assert "docs/a.md" not in (bigger.outside_baseline or {})
    assert changes_since(bigger, entries).untracked == ("README.md", "tests/test_new.py")
    assert changes_since(bigger, entries).removed == ("docs/a.md",)
    # Naming paths narrows to them: no note, the named file is the change.
    assert changes_since(record, entries, paths=["README.md"]).untracked == ()


def test_a_file_an_earlier_tree_left_outside_the_record_is_named_as_a_leftover() -> None:
    """A file outside the record, untouched since a wider tree wrote it, is not at the record's commit: named, never a change."""
    from app.services.sandbox_workspace.checkout import format_status, outside_baseline

    before = {"base/README.md": _Entry(_sha(b"# old\n")), "base/ci-requirements.txt": _Entry(_sha(b"black\n"))}
    record = build_record(
        provider="github",
        repository="acme/widgets",
        ref="main",
        commit="0123abcd",
        path="base",
        sub_paths=("src",),
        files=[("src/a.py", b"a = 1\n")],
        outside=outside_baseline("base", ("src",), ("src/a.py",), before),
    )
    entries = {**_manifest(record), **before}
    changes = changes_since(record, entries)
    assert changes.empty and changes.untracked == ()
    assert changes.leftover == ("README.md", "ci-requirements.txt")
    status = format_status(record, changes, shown_as="base")
    assert "2 file(s) under the checkout lie outside its record" in status and "ci-requirements.txt" in status
    # An edit there is untracked, no longer a leftover.
    entries["base/README.md"] = _Entry(_sha(b"# edited\n"))
    edited = changes_since(record, entries)
    assert edited.untracked == ("README.md",) and edited.leftover == ("ci-requirements.txt",)
    # Naming paths narrows to them: no leftover note.
    assert changes_since(record, entries, paths=["src"]).leftover == ()


def test_a_record_without_a_baseline_falls_back_to_its_write_time() -> None:
    @dataclass(frozen=True)
    class _Timed:
        sha256: str
        mtime: float

    legacy = CheckoutRecord.from_json(_record().to_json().replace(b',\n "outside": {}', b""), path="base")
    assert legacy.outside_baseline is None
    entries: dict[str, object] = dict(_manifest(legacy))
    entries["base/old.md"] = _Timed(_sha(b"o"), 10.0)
    entries["base/new.md"] = _Timed(_sha(b"n"), 30.0)
    assert changes_since(legacy, entries, since=20.0).untracked == ("new.md",)


def test_a_named_path_with_nothing_changed_under_it_is_said() -> None:
    record = _record()
    entries = _manifest(record, {"src/a.py": b"a = 2\n"})
    changes = changes_since(record, entries, paths=["src/a.py", "src/missing.py"])
    assert changes.unmatched == ("src/missing.py",)
    assert "nothing changed under src/missing.py" in changes.describe()


def test_a_nested_checkout_is_skipped_whole_and_named_once() -> None:
    """Another repository checked out inside this one (its own record) is never part of this change set."""
    record = build_record(
        provider="github",
        repository="acme/widgets",
        ref="main",
        commit="0123abcd",
        path="base",
        sub_paths=(),
        files=[("src/a.py", b"a = 1\n")],
    )
    entries = _manifest(record)
    entries[f"base/lab/{RECORD_NAME}"] = _Entry("nested record")
    entries["base/lab/package.json"] = _Entry(_sha(b"{}"))
    entries["base/lab/src/index.ts"] = _Entry(_sha(b"export {}\n"))
    changes = changes_since(record, entries)
    assert changes.empty
    assert changes.nested == ("lab",)
    assert f"1 nested checkout(s) excluded whole, each with its own {RECORD_NAME} (lab)" in changes.ignored_note()
    text = format_status(record, changes, shown_as="workspace/base")
    assert text.startswith("workspace/base/ is clean")
    assert "lab/package.json" not in text
    # A stray file beside the nested directory still counts; a named path under it does not.
    entries["base/lab-notes.md"] = _Entry(_sha(b"n\n"))
    entries["base/src/new.py"] = _Entry(_sha(b"n = 1\n"))
    changes = changes_since(record, entries)
    assert changes.added == ("lab-notes.md", "src/new.py")
    assert changes_since(record, entries, paths=["lab"]).empty
    # Under a narrow checkout, the nested tree is not named as untracked either.
    narrow = _record()
    narrow_entries = _manifest(narrow)
    narrow_entries[f"base/src/lab/{RECORD_NAME}"] = _Entry("nested record")
    narrow_entries["base/src/lab/x.ts"] = _Entry(_sha(b"x"))
    narrow_entries[f"base/other/{RECORD_NAME}"] = _Entry("nested record")
    narrow_entries["base/other/y.ts"] = _Entry(_sha(b"y"))
    narrow_changes = changes_since(narrow, narrow_entries, since=0.0)
    assert narrow_changes.empty
    assert narrow_changes.untracked == ()
    assert narrow_changes.nested == ("other", "src/lab")


# -- git status / git diff of a checkout --------------------------------


def test_status_lists_one_line_per_change_sorted_by_path() -> None:
    record = _record()
    entries = _manifest(record, {"src/a.py": b"a = 2\n", "src/pkg/b.py": None, "src/new.py": b"n = 1\n"})
    changes = changes_since(record, entries)
    assert status_lines(changes) == ["M src/a.py", "A src/new.py", "D src/pkg/b.py"]
    text = format_status(record, changes, shown_as="workspace/base")
    assert text.startswith(
        "workspace/base/: checkout of acme/widgets@main (commit 0123abcd); 3 file(s) changed: 1 modified, 1 added, 1 removed."
    )
    assert 'create_github_commit(from_checkout="base") commits exactly these.' in text
    assert text.splitlines()[1:] == ["M src/a.py", "A src/new.py", "D src/pkg/b.py"]


def test_status_says_clean_and_bounds_its_lines() -> None:
    record = _record()
    clean = format_status(record, changes_since(record, _manifest(record)), shown_as="workspace/base")
    assert clean == "workspace/base/ is clean: nothing changed since its checkout of acme/widgets@main (commit 0123abcd)."
    many = _manifest(record, {f"src/n{i:02}.py": b"x\n" for i in range(5)})
    text = format_status(record, changes_since(record, many), shown_as="workspace/base", limit=2)
    assert text.splitlines()[1:] == ["A src/n00.py", "A src/n01.py", "[3 more file(s) not listed: narrow with paths]"]


def test_file_diff_shows_modified_added_and_removed_files() -> None:
    modified = file_diff("src/a.py", b"a = 1\nb = 2\n", b"a = 1\nb = 3\n")
    assert modified.startswith("--- a/src/a.py\n+++ b/src/a.py\n")
    assert "-b = 2\n+b = 3\n" in modified
    added = file_diff("src/new.py", None, b"n = 1\n")
    assert added.startswith("--- /dev/null\n+++ b/src/new.py\n") and "+n = 1\n" in added
    removed = file_diff("src/old.py", b"o = 1\n", None)
    assert removed.startswith("--- a/src/old.py\n+++ /dev/null\n") and "-o = 1\n" in removed
    no_newline = file_diff("src/a.py", b"a = 1", b"a = 2")
    assert "\\ No newline at end of file" in no_newline


def test_file_diff_names_a_binary_and_cuts_a_long_diff() -> None:
    binary = file_diff("img/logo.png", b"\x89PNG\x00\x01", b"\x89PNG\x00\x02\x03")
    assert binary == "Binary file img/logo.png differs (6 -> 7 bytes).\n"
    long = file_diff("src/big.py", b"", "".join(f"line {i}\n" for i in range(2000)).encode(), max_chars=500)
    assert len(long) < 700 and "[diff of src/big.py cut:" in long


def test_diff_context_is_clamped() -> None:
    assert clamp_diff_context(0) == 0
    assert clamp_diff_context(99) == 10
    assert clamp_diff_context("x") == 3
    assert clamp_diff_context(None) == 3


# -- the record moved to the commit made from it ------------------------


def test_advance_moves_the_record_to_the_commit_and_what_it_carried_is_no_change() -> None:
    record = _record()
    overrides: dict[str, bytes | None] = {"src/a.py": b"a = 2\n", "src/new.py": b"new\n", "src/pkg/b.py": None}
    entries = _manifest(record, overrides)
    moved = advance(
        record,
        commit="fedc9876",
        ref="feat/x",
        written={"src/a.py": b"a = 2\n", "src/new.py": b"new\n"},
        removed=["src/pkg/b.py"],
    )
    assert (moved.commit, moved.ref, moved.sub_paths, moved.path) == ("fedc9876", "feat/x", ("src",), "base")
    assert sorted(moved.files) == ["src/a.py", "src/new.py"]
    assert moved.files["src/new.py"] == RecordedFile(sha256=_sha(b"new\n"), size=4)
    assert changes_since(moved, entries).empty
    # The original is untouched (frozen) and the moved record round-trips.
    assert record.commit == "0123abcd" and "src/pkg/b.py" in record.files
    assert CheckoutRecord.from_json(moved.to_json(), path="base") == moved


def test_advance_keeps_an_edit_the_commit_did_not_carry() -> None:
    record = _record()
    entries = _manifest(record, {"src/a.py": b"a = 2\n", "src/pkg/b.py": b"b = 3\n"})
    # Committed with paths=["src/a.py"]: b.py's edit is still to commit.
    moved = advance(record, commit="fedc9876", ref="feat/x", written={"src/a.py": b"a = 2\n"}, removed=[])
    changes = changes_since(moved, entries)
    assert changes.modified == ("src/pkg/b.py",) and changes.count == 1


def test_a_file_committed_outside_the_sub_paths_is_tracked_from_then_on() -> None:
    record = _record()
    entries = _manifest(record)
    entries["base/tests/test_a.py"] = _Entry(_sha(b"def test(): ...\n"))
    moved = advance(record, commit="fedc9876", ref="feat/x", written={"tests/test_a.py": b"def test(): ...\n"}, removed=[])
    assert moved.tracks("tests/test_a.py") and not moved.covers("tests/test_a.py")
    assert changes_since(moved, entries).empty
    # Its next edit is a change without naming it in paths; removing it is one too.
    entries["base/tests/test_a.py"] = _Entry(_sha(b"def test(): assert True\n"))
    assert changes_since(moved, entries).modified == ("tests/test_a.py",)
    del entries["base/tests/test_a.py"]
    assert changes_since(moved, entries).removed == ("tests/test_a.py",)
    # Another stray file outside the sub_paths is still never a change.
    entries["base/README.md"] = _Entry(_sha(b"stray"))
    assert "README.md" not in changes_since(moved, entries).added


def test_advance_without_a_commit_or_ref_keeps_the_recorded_ones() -> None:
    record = _record()
    moved = advance(record, commit="", ref="", written={}, removed=[])
    assert (moved.commit, moved.ref) == ("0123abcd", "main")


# -- the record moved by a files[] commit of its own workspace files ----------


def _files_advance(record: CheckoutRecord, **kwargs: object) -> CheckoutRecord | None:
    defaults: dict[str, object] = {
        "repository": "acme/widgets",
        "branch": "feat/x",
        "parent": "0123abcd",
        "commit": "fedc9876",
        "written": {"base/src/a.py": ("src/a.py", b"a = 2\n")},
        "committed": ["src/a.py"],
    }
    defaults.update(kwargs)
    return advance_files(record, **defaults)  # type: ignore[arg-type]


def test_advance_files_moves_the_record_when_the_commit_carries_its_own_files() -> None:
    record = _record()
    entries = _manifest(record, {"src/a.py": b"a = 2\n"})
    moved = _files_advance(record)
    assert moved is not None
    assert (moved.commit, moved.ref) == ("fedc9876", "feat/x")
    assert changes_since(moved, entries).empty


def test_advance_files_tracks_a_file_added_outside_the_sub_paths() -> None:
    record = _record()
    entries = _manifest(record)
    entries["base/docs/new.md"] = _Entry(_sha(b"v1\n"))
    moved = _files_advance(record, written={"base/docs/new.md": ("docs/new.md", b"v1\n")}, committed=["docs/new.md"])
    assert moved is not None and moved.tracks("docs/new.md")
    assert changes_since(moved, entries).empty
    # Its next edit is a change from_checkout commits, not an overlap with the branch.
    entries["base/docs/new.md"] = _Entry(_sha(b"v2\n"))
    assert changes_since(moved, entries).modified == ("docs/new.md",)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"parent": "99999999"},  # the record is behind the branch: the reconcile merges next time
        {"repository": "acme/other"},
        {"commit": ""},
        {"written": {"base/src/a.py": ("lib/a.py", b"a = 2\n")}},  # committed at another path
        {"written": {"elsewhere/a.py": ("src/a.py", b"a = 2\n")}},  # not a file of the checkout
        {"committed": ["src/a.py", "src/pkg/b.py"]},  # another tracked file written from elsewhere
    ],
)
def test_advance_files_keeps_the_record_when_the_commit_is_not_its_own(kwargs: dict[str, object]) -> None:
    assert _files_advance(_record(), **kwargs) is None
