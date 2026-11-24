"""The three-way merge and the rebase plan of a checkout.

Pure tests of ``rebase.py``: ``merge_text`` (diff3 with git-style markers),
``plan`` (what each file of the base, the workspace and the ref needs),
``merge_file`` (text and binary) and ``rebased_record``. The tool-level test
(``sandbox_checkout_rebase``) is in ``test_sandbox_workspace_tools.py``.
"""

from __future__ import annotations

import hashlib

from app.services.sandbox_workspace.checkout import CheckoutRecord, RecordedFile
from app.services.sandbox_workspace.rebase import (
    OURS_LABEL,
    THEIRS_LABEL,
    FileStep,
    RebaseSummary,
    Step,
    merge_file,
    merge_text,
    plan,
    rebased_record,
)

BASE = "a\nb\nc\nd\ne\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _record(files: dict[str, bytes], sub_paths: tuple[str, ...] = ()) -> CheckoutRecord:
    return CheckoutRecord(
        provider="github",
        repository="acme/repo",
        ref="main",
        commit="1111111",
        path="repo",
        sub_paths=sub_paths,
        files={p: RecordedFile(sha256=_sha(d), size=len(d)) for p, d in files.items()},
    )


def test_disjoint_edits_are_both_kept() -> None:
    merged = merge_text(BASE, "A\nb\nc\nd\ne\n", "a\nb\nc\nd\nE\n")
    assert merged.text == "A\nb\nc\nd\nE\n"
    assert merged.conflicts == 0


def test_overlapping_edits_are_written_between_markers() -> None:
    merged = merge_text(BASE, "a\nX\nc\nd\ne\n", "a\nY\nc\nd\ne\n")
    assert merged.conflicts == 1
    assert merged.text == f"a\n<<<<<<< {OURS_LABEL}\nX\n=======\nY\n>>>>>>> {THEIRS_LABEL}\nc\nd\ne\n"


def test_identical_edits_are_taken_once() -> None:
    merged = merge_text(BASE, "a\nX\nc\nd\ne\n", "a\nX\nc\nd\ne\n")
    assert merged.text == "a\nX\nc\nd\ne\n"
    assert merged.conflicts == 0


def test_one_side_unchanged_takes_the_other() -> None:
    assert merge_text(BASE, BASE, "z\n").text == "z\n"
    assert merge_text(BASE, "z\n", BASE).text == "z\n"


def test_an_insertion_and_a_distant_deletion_merge() -> None:
    merged = merge_text(BASE, "a\nc\nd\ne\n", "a\nb\nc\nd\ne\nf\n")
    assert merged.text == "a\nc\nd\ne\nf\n"
    assert merged.conflicts == 0


def test_appends_on_both_sides_conflict() -> None:
    merged = merge_text(BASE, BASE + "f\n", BASE + "g\n")
    assert merged.conflicts == 1
    assert merged.text.startswith(BASE + f"<<<<<<< {OURS_LABEL}\nf\n=======\ng\n")


def test_add_add_with_different_content_conflicts() -> None:
    merged = merge_text("", "x\n", "y\n")
    assert merged.conflicts == 1
    assert merged.text == f"<<<<<<< {OURS_LABEL}\nx\n=======\ny\n>>>>>>> {THEIRS_LABEL}\n"


def test_a_conflict_on_a_last_line_without_newline_keeps_the_markers_on_their_own_lines() -> None:
    merged = merge_text("a\nb", "a\nX", "a\nY")
    assert merged.conflicts == 1
    assert f"X\n=======\nY\n>>>>>>> {THEIRS_LABEL}\n" in merged.text


def test_plan_steps_every_case() -> None:
    record = _record(
        {
            "same.txt": b"s",
            "theirs_only.txt": b"t",
            "ours_only.txt": b"o",
            "both.txt": b"b",
            "deleted_by_ref.txt": b"d",
            "deleted_by_ref_edited.txt": b"e",
            "deleted_here_changed_there.txt": b"f",
        }
    )
    ours = {
        "same.txt": _sha(b"s"),
        "theirs_only.txt": _sha(b"t"),
        "ours_only.txt": _sha(b"o2"),
        "both.txt": _sha(b"b-ours"),
        "deleted_by_ref.txt": _sha(b"d"),
        "deleted_by_ref_edited.txt": _sha(b"e2"),
        "new_here.txt": _sha(b"n"),
    }
    theirs = {
        "same.txt": b"s",
        "theirs_only.txt": b"t2",
        "ours_only.txt": b"o",
        "both.txt": b"b-theirs",
        "deleted_here_changed_there.txt": b"f2",
        "new_there.txt": b"m",
    }
    steps = {s.path: s for s in plan(record, ours, theirs)}
    assert "same.txt" not in steps
    assert steps["theirs_only.txt"].step is Step.TAKE
    assert steps["new_there.txt"].step is Step.TAKE
    assert steps["ours_only.txt"].step is Step.KEEP
    assert steps["new_here.txt"].step is Step.KEEP
    assert steps["both.txt"].step is Step.MERGE
    assert steps["deleted_by_ref.txt"].step is Step.REMOVE
    assert steps["deleted_by_ref_edited.txt"].step is Step.CONFLICT
    assert "removed on the ref" in steps["deleted_by_ref_edited.txt"].reason
    assert steps["deleted_here_changed_there.txt"].step is Step.CONFLICT
    assert "removed in the workspace" in steps["deleted_here_changed_there.txt"].reason


def test_plan_keeps_a_change_the_ref_made_the_same_way() -> None:
    record = _record({"f.txt": b"old"})
    assert plan(record, {"f.txt": _sha(b"new")}, {"f.txt": b"new"}) == [FileStep("f.txt", Step.KEEP)]


def test_plan_add_add_is_a_merge() -> None:
    record = _record({})
    assert plan(record, {"n.txt": _sha(b"x")}, {"n.txt": b"y"}) == [FileStep("n.txt", Step.MERGE)]


def test_plan_keeps_a_branch_file_the_squash_on_the_ref_carries_as_checked_out() -> None:
    """A file the branch added, edited since, then squashed onto the ref unchanged: no add-add merge."""
    pushed, edited = b"a\nb\n", b"a\nb\nc\n"
    checked_out = _record({"snapshots.py": pushed})
    merge_base = _record({})
    steps = plan(merge_base, {"snapshots.py": _sha(edited)}, {"snapshots.py": pushed}, checked_out=checked_out.files)
    assert steps == [FileStep("snapshots.py", Step.KEEP)]
    # Without what the checkout wrote, the same three sides are a whole-file add-add merge.
    assert plan(merge_base, {"snapshots.py": _sha(edited)}, {"snapshots.py": pushed}) == [
        FileStep("snapshots.py", Step.MERGE)
    ]
    # The ref changed it past the checkout: still merged.
    moved = plan(merge_base, {"snapshots.py": _sha(edited)}, {"snapshots.py": b"z\n"}, checked_out=checked_out.files)
    assert moved == [FileStep("snapshots.py", Step.MERGE)]


def test_plan_leaves_a_tracked_file_outside_the_sub_paths_alone() -> None:
    record = _record({"src/a.py": b"a", "README.md": b"r"}, sub_paths=("src",))
    steps = plan(record, {"src/a.py": _sha(b"a"), "README.md": _sha(b"r")}, {"src/a.py": b"a"}, in_scope=record.covers)
    assert steps == []
    moved = rebased_record(record, ref="main", commit="2222222", theirs={"src/a.py": b"a"}, in_scope=record.covers)
    assert set(moved.files) == {"src/a.py", "README.md"}


def test_merge_file_text_and_binary() -> None:
    merged = merge_file(BASE.encode(), b"A\nb\nc\nd\ne\n", b"a\nb\nc\nd\nE\n")
    assert merged.data == b"A\nb\nc\nd\nE\n"
    assert merged.conflicts == 0 and not merged.reason
    binary = merge_file(b"\x00\x01", b"\x00\x02", b"\x00\x03")
    assert binary.data == b"\x00\x02"
    assert binary.conflicts == 1 and "binary" in binary.reason


def test_merge_file_add_add_has_no_base() -> None:
    merged = merge_file(None, b"x\n", b"x\n")
    assert merged.data == b"x\n" and merged.conflicts == 0


def test_rebased_record_names_the_new_ref_and_its_files() -> None:
    record = _record({"a.txt": b"a", "gone.txt": b"g"})
    moved = rebased_record(record, ref="feature", commit="3333333", theirs={"a.txt": b"a2", "b.txt": b"b"})
    assert (moved.ref, moved.commit, moved.path) == ("feature", "3333333", "repo")
    assert moved.files == {
        "a.txt": RecordedFile(sha256=_sha(b"a2"), size=2),
        "b.txt": RecordedFile(sha256=_sha(b"b"), size=1),
    }


def test_summary_describes_each_list() -> None:
    summary = RebaseSummary(updated=["u"], removed=["r"], merged=["m"], kept=["k"], conflicts=[("c", "1 hunk(s)")])
    text = summary.describe(shown_as="repo", before="acme/repo@main", after="acme/repo@main (commit 2)")
    assert "Updated from the ref (1): u." in text
    assert "Removed by the ref (1): r." in text
    assert "Merged cleanly (1): m." in text
    assert "kept as they were (1): k." in text
    assert "In conflict (1): c (1 hunk(s))" in text
    assert "Nothing changed" in RebaseSummary().describe(shown_as="repo", before="x", after="y")
