"""A rebase never takes the workspace's older text as the branch side (#1029).

A ``from_checkout`` commit moves the checkout record to the branch commit but
keeps the hash of every file it did not write. When the branch gained commits
the checkout never held (main merged into it on the server), those workspace
files are older than the branch: ``stale_branch_sides`` names them so the
rebase reads the branch's version first, and ``plan`` then lets the ref win
for a file only the ref changed and merges a file both sides changed from the
branch's text.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json

from app.services.sandbox_workspace.checkout import CheckoutRecord, RecordedFile
from app.services.sandbox_workspace.rebase import (
    RebaseSummary,
    Step,
    base_files_of,
    merge_file,
    plan,
    stale_branch_sides,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _files(files: dict[str, bytes]) -> dict[str, RecordedFile]:
    return {p: RecordedFile(sha256=_sha(d), size=len(d)) for p, d in files.items()}


def _record(files: dict[str, bytes]) -> CheckoutRecord:
    return CheckoutRecord(
        provider="github",
        repository="acme/repo",
        ref="feat/x",
        commit="b" * 40,
        path="repo",
        files=_files(files),
        base_commit="a" * 40,
        branch={"src/feature.py": "M"},
    )


def _catalog(**keys: str) -> bytes:
    return (json.dumps(keys, indent=2, sort_keys=True) + "\n").encode()


def test_untouched_file_older_than_the_branch_is_named() -> None:
    old = b"v1\n"
    branch_version = b"v2\n"
    recorded = _files({"lib/util.py": old, "src/feature.py": b"mine\n"})
    ours = {"lib/util.py": _sha(old), "src/feature.py": _sha(b"edited\n")}
    refreshed, gone = stale_branch_sides(
        recorded, ours, {"lib/util.py": branch_version, "src/feature.py": b"other\n"}, in_scope=lambda _p: True
    )
    # The edited file is the workspace side and is never replaced.
    assert refreshed == {"lib/util.py": branch_version}
    assert gone == []


def test_file_the_branch_removed_is_named_gone_only_in_scope() -> None:
    recorded = _files({"lib/a.py": b"a\n", "other/b.py": b"b\n"})
    ours = {"lib/a.py": _sha(b"a\n"), "other/b.py": _sha(b"b\n")}
    refreshed, gone = stale_branch_sides(recorded, ours, {}, in_scope=lambda p: p.startswith("lib/"))
    assert refreshed == {}
    assert gone == ["lib/a.py"]


def test_file_matching_the_branch_is_left_alone() -> None:
    same = b"same\n"
    refreshed, gone = stale_branch_sides(
        _files({"x.py": same}), {"x.py": _sha(same)}, {"x.py": same}, in_scope=lambda _p: True
    )
    assert refreshed == {} and gone == []


def test_ref_only_change_follows_the_ref_once_the_branch_side_is_refreshed() -> None:
    """A file main changed and the branch only carried through its merge of main: the ref wins."""
    checked_out = b"old\n"
    on_branch = b"main at the merge base\n"
    on_ref = b"main later\n"
    record = _record({"lib/util.py": checked_out})
    ours = {"lib/util.py": _sha(checked_out)}
    refreshed, _gone = stale_branch_sides(record.files, ours, {"lib/util.py": on_branch}, in_scope=record.covers)
    ours["lib/util.py"] = _sha(refreshed["lib/util.py"])
    base = {"lib/util.py": on_branch}  # the merge base of the branch and the ref
    for_plan = dataclasses.replace(record, files=base_files_of(record, base))
    steps = plan(for_plan, ours, {"lib/util.py": on_ref}, in_scope=record.covers, checked_out=record.files)
    assert [(s.path, s.step) for s in steps] == [("lib/util.py", Step.TAKE)]


def test_stale_side_would_have_been_kept_over_the_ref() -> None:
    """The defect itself: without the refresh the older text reads as a workspace change and is kept."""
    checked_out = b"old\n"
    on_branch = b"main at the merge base\n"
    record = _record({"lib/util.py": checked_out})
    for_plan = dataclasses.replace(record, files=base_files_of(record, {"lib/util.py": on_branch}))
    steps = plan(
        for_plan,
        {"lib/util.py": _sha(checked_out)},
        {"lib/util.py": on_branch},
        in_scope=record.covers,
        checked_out=record.files,
    )
    assert [(s.path, s.step) for s in steps] == [("lib/util.py", Step.KEEP)]


def test_catalog_keeps_the_keys_of_both_sides_from_the_branch_text() -> None:
    checked_out = _catalog(a="A")
    merge_base = _catalog(a="A", m1="main one")
    on_branch = _catalog(a="A", b="branch", m1="main one")
    on_ref = _catalog(a="A", m1="main one", m2="main two")
    record = _record({"i18n/en.json": checked_out})
    ours = {"i18n/en.json": _sha(checked_out)}
    refreshed, _gone = stale_branch_sides(record.files, ours, {"i18n/en.json": on_branch}, in_scope=record.covers)
    mine = refreshed["i18n/en.json"]
    merged = merge_file(merge_base, mine, on_ref, path="i18n/en.json")
    assert merged.conflicts == 0
    assert json.loads(merged.data) == {"a": "A", "b": "branch", "m1": "main one", "m2": "main two"}


def test_summary_names_the_refreshed_files() -> None:
    text = RebaseSummary(refreshed=["lib/util.py"]).describe(shown_as="repo", before="feat/x", after="main")
    assert "Read from the branch commit first, the workspace held them as first checked out (1): lib/util.py." in text
