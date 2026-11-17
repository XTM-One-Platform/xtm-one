"""sandbox_diff_files: diff two workspace files or merge three.

The pure part (``filediff``) is covered here; the tool end to end (reads,
refusals, write_merge_to) is in ``test_sandbox_workspace_tools.py``.
"""

from __future__ import annotations

from app.services.sandbox_workspace import filediff

BASE = "a = 1\nb = 2\nc = 3\nd = 4\n"


def test_two_way_is_a_unified_diff_with_the_file_names():
    text = filediff.two_way("x/a.py", BASE, "y/a.py", BASE.replace("b = 2", "b = 20"))
    assert text.startswith("--- x/a.py\n+++ y/a.py\n")
    assert "-b = 2\n+b = 20\n" in text


def test_two_way_of_identical_texts_says_so():
    assert filediff.two_way("x", BASE, "y", BASE) == "x and y have the same content.\n"


def test_two_way_bounds_long_lines_and_the_whole_diff(monkeypatch):
    long_line = "z" * (filediff.LONG_LINE_CHARS + 50) + "\n"
    text = filediff.two_way("x", "", "y", long_line)
    assert f"+{'z' * filediff.LONG_LINE_CHARS} ... [+50 chars]\n" in text
    monkeypatch.setattr(filediff, "MAX_DIFF_CHARS", 40)
    cut = filediff.two_way("x", BASE, "y", "")
    assert "[diff cut:" in cut and len(cut) < 120


def test_two_way_marks_a_missing_final_newline():
    text = filediff.two_way("x", "a\n", "y", "a\nb")
    assert "+b\n\\ No newline at end of file\n" in text


def test_three_way_merges_disjoint_changes_cleanly():
    ours = BASE.replace("a = 1", "a = 10")
    theirs = BASE.replace("d = 4", "d = 40")
    merged = filediff.three_way(BASE, ours, theirs, ours_label="pr/a.py", theirs_label="main/a.py")
    assert merged.conflicts == 0 and merged.marker_lines == ()
    assert merged.text == "a = 10\nb = 2\nc = 3\nd = 40\n"


def test_three_way_locates_each_conflict_by_its_first_marker():
    ours = BASE.replace("b = 2", "b = 20")
    theirs = BASE.replace("b = 2", "b = 21")
    merged = filediff.three_way(BASE, ours, theirs, ours_label="pr/a.py", theirs_label="main/a.py")
    assert merged.conflicts == 1
    lines = merged.text.splitlines()
    assert merged.marker_lines == (2,)
    assert lines[1:6] == ["<<<<<<< pr/a.py", "b = 20", "=======", "b = 21", ">>>>>>> main/a.py"]


def test_changed_lines_counts_like_a_diffstat():
    assert filediff.changed_lines(BASE, BASE.replace("b = 2", "b = 3") + "e = 5\n") == (2, 1)


def test_clamp_context_and_describe_markers():
    assert filediff.clamp_context(99) == filediff.MAX_CONTEXT_LINES
    assert filediff.clamp_context(-1) == 0
    assert filediff.clamp_context("x") == filediff.DEFAULT_CONTEXT_LINES
    many = tuple(range(1, filediff.MAX_LISTED_CONFLICTS + 3))
    assert filediff.describe_markers(many).endswith(" and 2 more")
    assert filediff.describe_markers((4, 9)) == "line(s) 4, 9"
