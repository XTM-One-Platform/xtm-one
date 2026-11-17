"""A from_checkout commit cut after it landed no longer leaves the checkout record behind (#12377).

Desk run de25577c (xtm-one #12248): a ``create_github_commit(from_checkout)``
cut by a rollout landed on the branch, but the record stayed at main; the next
commit was refused as overlapping its own changes, and a rebase onto the branch
wrote conflict markers of the workspace against its own pushed lines.

The call now notes the commit it is about to make in the record; the next call
adopts the branch tip when it is exactly that commit. A three-way merge whose
workspace hunk carries the ref's hunk plus more edits takes the workspace side.
"""

from __future__ import annotations

import hashlib

from app.services.sandbox_workspace.checkout import (
    CheckoutRecord,
    RecordedFile,
    adopt_pending,
    advance,
    git_blob_sha,
    pending_matches,
    with_pending,
)
from app.services.sandbox_workspace.rebase import has_conflict_markers, merge_file, merge_text

BASE = "a" * 40
LANDED = "b" * 40


def _record(**files: bytes) -> CheckoutRecord:
    return CheckoutRecord(
        provider="github",
        repository="o/r",
        ref="main",
        commit=BASE,
        path="co",
        sub_paths=("src",),
        files={
            path.replace("__", "/"): RecordedFile(hashlib.sha256(data).hexdigest(), len(data))
            for path, data in files.items()
        },
    )


def _noted(record: CheckoutRecord, written: dict[str, bytes], removed: list[str] | None = None) -> CheckoutRecord:
    return with_pending(record, branch="fix/x", parent=BASE, written=written, removed=removed or [])


def test_the_pending_note_survives_the_record_file():
    noted = _noted(_record(src__a=b"one\n"), {"src/a": b"two\n"})
    again = CheckoutRecord.from_json(noted.to_json(), path="co")
    assert again.pending == noted.pending
    assert again.pending["files"]["src/a"]["blob"] == git_blob_sha(b"two\n")


def test_a_replayed_commit_leaves_the_record_at_the_pushed_commit():
    noted = _noted(_record(src__a=b"one\n", src__gone=b"x\n"), {"src/a": b"two\n"}, ["src/gone"])
    changed = {"src/a": git_blob_sha(b"two\n"), "src/gone": None}
    assert pending_matches(noted, parent=BASE, changed=changed)
    moved = adopt_pending(noted, commit=LANDED)
    assert moved.commit == LANDED
    assert moved.ref == "fix/x"
    assert moved.pending is None
    assert moved.files["src/a"].sha256 == hashlib.sha256(b"two\n").hexdigest()
    assert "src/gone" not in moved.files
    # The branch's own change still counts from the commit checked out, as after a commit that answered.
    assert moved.baseline == BASE
    assert moved.branch == {"src/a": "M", "src/gone": "D"}
    # The pushed bytes are no longer a change: the next commit carries only what came after.
    assert CheckoutRecord.from_json(moved.to_json(), path="co").pending is None


def test_another_commit_on_the_branch_is_never_adopted():
    noted = _noted(_record(src__a=b"one\n"), {"src/a": b"two\n"})
    assert not pending_matches(noted, parent="c" * 40, changed={"src/a": git_blob_sha(b"two\n")})
    assert not pending_matches(noted, parent=BASE, changed={"src/a": git_blob_sha(b"three\n")})
    extra = {"src/a": git_blob_sha(b"two\n"), "src/b": git_blob_sha(b"b\n")}
    assert not pending_matches(noted, parent=BASE, changed=extra)
    assert not pending_matches(_record(src__a=b"one\n"), parent=BASE, changed={"src/a": git_blob_sha(b"two\n")})


def test_a_commit_that_finished_clears_the_note():
    noted = _noted(_record(src__a=b"one\n"), {"src/a": b"two\n"})
    moved = advance(noted, commit=LANDED, ref="fix/x", written={"src/a": b"two\n"}, removed=[])
    assert moved.pending is None
    assert b"pending" not in moved.to_json()


BASE_TEXT = "def f():\n    return 1\n\n\ndef g():\n    return 2\n"
# The commit made from the workspace pushed this (the ref) ...
PUSHED = "def f():\n    return 10\n\n\ndef g():\n    return 2\n"


def test_a_workspace_equal_to_the_ref_merges_without_markers():
    merged = merge_file(BASE_TEXT.encode(), PUSHED.encode(), PUSHED.encode(), path="m.py")
    assert merged.conflicts == 0
    assert merged.data.decode() == PUSHED


def test_a_workspace_that_kept_the_pushed_lines_and_added_more_merges_without_markers():
    ours = "def f():\n    return 10\n    # checked\n\n\ndef g():\n    return 2\n"
    merged = merge_text(BASE_TEXT, ours, PUSHED)
    assert merged.conflicts == 0
    assert merged.text == ours
    assert not has_conflict_markers(merged.text.encode())


def test_two_different_changes_of_the_same_lines_still_conflict():
    ours = "def f():\n    return 11\n\n\ndef g():\n    return 2\n"
    merged = merge_text(BASE_TEXT, ours, PUSHED)
    assert merged.conflicts == 1
    assert has_conflict_markers(merged.text.encode())


def test_a_delete_against_a_rewrite_still_conflicts():
    ours = "\n\ndef g():\n    return 2\n"
    theirs = "def f():\n    return 10\n\n\ndef g():\n    return 2\n"
    assert merge_text(BASE_TEXT, ours, theirs).conflicts == 1
