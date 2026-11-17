"""sandbox_move_files: move or rename workspace files in one write, and the bounded echo of a long insertion."""

from __future__ import annotations

import uuid

import pytest

from app.services.sandbox_workspace import editing, moving
from app.services.sandbox_workspace.manifest import ManifestEntry
from tests.test_sandbox_workspace_tools import _call, _composite, _put, _workspace_bytes, store  # noqa: F401


def _manifest(*paths: str) -> dict[str, ManifestEntry]:
    return {p: ManifestEntry(path=p, size=len(p), sha256=f"sha-{p}", mtime=1.0) for p in paths}


def test_plan_moves_a_file_and_a_directory_keeping_the_blobs():
    current = _manifest("pkg/a.py", "pkg/sub/b.py", "other.py")
    planned = moving.plan([("pkg", "lib/pkg"), ("other.py", "lib/other.py")], current)
    assert [(old, e.path) for old, e in planned.moved] == [
        ("other.py", "lib/other.py"),
        ("pkg/a.py", "lib/pkg/a.py"),
        ("pkg/sub/b.py", "lib/pkg/sub/b.py"),
    ]
    assert all(e.sha256 == f"sha-{old}" for old, e in planned.moved)
    assert planned.removals == ["other.py", "pkg/a.py", "pkg/sub/b.py"] and planned.replaced == []


@pytest.mark.parametrize(
    ("pairs", "message"),
    [
        ([("missing.py", "x.py")], "is not in the workspace"),
        ([("pkg", "pkg/inner")], "cannot go into itself"),
        ([("a.py", "c.py"), ("pkg/a.py", "c.py")], "would both land on"),
        ([("a.py", "b.py")], "already exists"),
        ([("a.py", "pkg")], "is a directory"),
        ([("a.py", "b.py/c.py")], "is a file"),
        ([("a.py", "a.py")], "onto itself"),
        ([("repo/.xtm-checkout.json", "elsewhere.json")], "checkout record"),
    ],
)
def test_plan_refusals(pairs, message):
    current = _manifest("a.py", "b.py", "pkg/a.py", "repo/.xtm-checkout.json", "repo/x.py")
    with pytest.raises(moving.MoveError, match=message):
        moving.plan(pairs, current)


def test_plan_overwrite_and_whole_checkout_move():
    current = _manifest("a.py", "b.py", "repo/.xtm-checkout.json", "repo/x.py")
    planned = moving.plan([("a.py", "b.py")], current, overwrite=True)
    assert planned.replaced == ["b.py"]
    moved = moving.plan([("repo", "main")], current)
    assert "main/.xtm-checkout.json" in [e.path for e in moved.upserts]


def test_plan_copy_keeps_the_sources_and_shares_the_blobs():
    current = _manifest("lab/host.tsx", "pkg/a.py", "pkg/sub/b.py", "src/x.ts")
    planned = moving.plan([("lab/host.tsx", "src/host.tsx"), ("pkg", "copy/pkg")], current, copy=True)
    assert [(old, e.path) for old, e in planned.moved] == [
        ("lab/host.tsx", "src/host.tsx"),
        ("pkg/a.py", "copy/pkg/a.py"),
        ("pkg/sub/b.py", "copy/pkg/sub/b.py"),
    ]
    assert all(e.sha256 == f"sha-{old}" for old, e in planned.moved)


def test_plan_copy_refuses_a_path_the_source_still_holds():
    # A move may land under a path it empties; a copy keeps its source, so the same target is refused.
    current = _manifest("a.py", "d/x.py")
    pairs = [("d/x.py", "e.py"), ("a.py", "d/x.py/z.py")]
    assert len(moving.plan(pairs, current).moved) == 2
    with pytest.raises(moving.MoveError, match="is a file"):
        moving.plan(pairs, current, copy=True)
    with pytest.raises(moving.MoveError, match="copied onto itself"):
        moving.plan([("a.py", "a.py")], current, copy=True)
    with pytest.raises(moving.MoveError, match="already exists"):
        moving.plan([("a.py", "d/x.py")], current, copy=True)
    assert moving.plan([("a.py", "d/x.py")], current, copy=True, overwrite=True).replaced == ["d/x.py"]


def test_parse_shapes():
    assert moving.parse({"from": "a", "to": "b"}) == [("a", "b")]
    with pytest.raises(moving.MoveError):
        moving.parse([])
    with pytest.raises(moving.MoveError, match="needs both"):
        moving.parse([{"from": "a"}])


async def test_move_files_writes_once_and_keeps_the_content(store):  # noqa: F811
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "repo/src/old.py", b"def f():\n    return 1\n")
    await _put(composite, "repo/src/pkg/m.py", b"x = 1\n")
    done = await _call(
        composite,
        "sandbox_move_files",
        moves=[{"from": "repo/src/old.py", "to": "repo/src/new.py"}, {"from": "repo/src/pkg", "to": "repo/lib/pkg"}],
    )
    assert not done.is_error, done.content
    assert done.content.startswith("Moved 2 file(s) in one write, content unchanged")
    assert await _workspace_bytes(store, conversation, "repo/src/new.py") == b"def f():\n    return 1\n"
    assert await _workspace_bytes(store, conversation, "repo/lib/pkg/m.py") == b"x = 1\n"
    listing = await _call(composite, "sandbox_list_files", path="repo")
    assert "old.py" not in listing.content and "src/pkg/m.py" not in listing.content


async def test_copy_files_writes_once_and_keeps_the_source(store):  # noqa: F811
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "repo/lab/host.tsx", b"export const Host = 1;\n")
    done = await _call(
        composite,
        "sandbox_move_files",
        moves=[{"from": "repo/lab/host.tsx", "to": "repo/src/host.tsx"}],
        copy=True,
    )
    assert not done.is_error, done.content
    assert done.content.startswith("Copied 1 file(s) in one write, content unchanged")
    assert await _workspace_bytes(store, conversation, "repo/lab/host.tsx") == b"export const Host = 1;\n"
    assert await _workspace_bytes(store, conversation, "repo/src/host.tsx") == b"export const Host = 1;\n"
    # Removing the copy leaves the source and its shared blob readable.
    removed = await _call(composite, "sandbox_remove_files", paths=["repo/src/host.tsx"])
    assert not removed.is_error, removed.content
    assert await _workspace_bytes(store, conversation, "repo/lab/host.tsx") == b"export const Host = 1;\n"


async def test_move_files_refusal_writes_nothing(store):  # noqa: F811
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "a.py", b"a\n")
    await _put(composite, "b.py", b"b\n")
    refused = await _call(composite, "sandbox_move_files", moves=[{"from": "a.py", "to": "b.py"}])
    assert refused.is_error and "Nothing was written" in refused.content
    assert await _workspace_bytes(store, conversation, "a.py") == b"a\n"
    assert await _workspace_bytes(store, conversation, "b.py") == b"b\n"


def test_long_insertion_echo_is_bounded_with_its_line_range():
    before = "top\nbottom\n"
    inserted = "".join(f"line {n}\n" for n in range(1, 301))
    diff = editing.unified_diff("f.txt", before, "top\n" + inserted + "bottom\n")
    assert "+line 30\n" in diff and "+line 31\n" not in diff and "+line 300\n" in diff
    assert "265 more added lines, 32 to 296: sandbox_read_file reads them" in diff
    short = editing.unified_diff("f.txt", before, "top\nline 1\nline 2\nbottom\n")
    assert "+line 1\n+line 2\n" in short and "more added lines" not in short
