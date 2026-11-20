"""``on_conflict``: a hunk both sides changed, written without markers in a stated order (#11479)."""

from __future__ import annotations

from app.services.sandbox_workspace.rebase import OnConflict, has_conflict_markers, merge_file, merge_text

#: The workspace added a line after a line the ref rewrote: the case markers made a hand edit of.
BASE = "a\nold()\nc\n"
OURS = "a\nold()\nmine()\nc\n"
THEIRS = "a\nnew()\nc\n"


def test_the_default_still_writes_markers() -> None:
    merged = merge_text(BASE, OURS, THEIRS)
    assert merged.conflicts == 1
    assert merged.text == "a\n<<<<<<< workspace\nold()\nmine()\n=======\nnew()\n>>>>>>> ref\nc\n"


def test_ref_then_workspace_keeps_the_ref_line_and_adds_only_the_workspace_line() -> None:
    assert merge_text(BASE, OURS, THEIRS, lead="theirs").text == "a\nnew()\nmine()\nc\n"
    outcome = merge_file(BASE.encode(), OURS.encode(), THEIRS.encode(), on_conflict=OnConflict.REF_THEN_WORKSPACE)
    assert outcome.data == b"a\nnew()\nmine()\nc\n"
    assert outcome.conflicts == 0 and not has_conflict_markers(outcome.data)


def test_workspace_then_ref_leads_with_the_workspace_side() -> None:
    outcome = merge_file(BASE.encode(), OURS.encode(), THEIRS.encode(), on_conflict=OnConflict.WORKSPACE_THEN_REF)
    assert outcome.data == b"a\nold()\nmine()\nnew()\nc\n"
    assert outcome.conflicts == 0


def test_a_hunk_at_the_end_without_a_final_newline_keeps_the_lines_apart() -> None:
    assert merge_text("a\nb", "a\nB", "a\nb2", lead="theirs").text == "a\nb2\nB"


def test_a_clean_merge_is_the_same_whatever_the_mode() -> None:
    base, ours, theirs = "a\nb\nc\nd\ne\n", "A\nb\nc\nd\ne\n", "a\nb\nc\nd\nE\n"
    for mode in OnConflict:
        assert merge_file(base.encode(), ours.encode(), theirs.encode(), on_conflict=mode).data == b"A\nb\nc\nd\nE\n"


def test_ref_keeps_the_ref_lines_alone_and_merges_the_other_hunks() -> None:
    base, ours, theirs = "x\na\nold()\nc\n", "X\na\nold()\nmine()\nc\n", "x\na\nnew()\nc\n"
    outcome = merge_file(base.encode(), ours.encode(), theirs.encode(), on_conflict=OnConflict.REF)
    assert outcome.data == b"X\na\nnew()\nc\n"
    assert outcome.conflicts == 0 and not has_conflict_markers(outcome.data)


def test_workspace_keeps_the_workspace_lines_alone() -> None:
    outcome = merge_file(BASE.encode(), OURS.encode(), THEIRS.encode(), on_conflict=OnConflict.WORKSPACE)
    assert outcome.data == OURS.encode()
    assert outcome.conflicts == 0


def test_a_binary_changed_on_both_sides_takes_the_chosen_side() -> None:
    base, ours, theirs = b"\x00\x01base", b"\x00\x01ours", b"\x00\x01theirs"
    assert merge_file(base, ours, theirs, on_conflict=OnConflict.REF).data == theirs
    assert merge_file(base, ours, theirs, on_conflict=OnConflict.WORKSPACE).data == ours
    assert merge_file(base, ours, theirs).conflicts == 1


def test_the_modes_are_named_as_the_tool_takes_them() -> None:
    assert [mode.value for mode in OnConflict] == [
        "markers",
        "ref-then-workspace",
        "workspace-then-ref",
        "ref",
        "workspace",
    ]
