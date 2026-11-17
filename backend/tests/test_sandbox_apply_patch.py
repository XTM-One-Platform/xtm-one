"""sandbox_apply_patch: a unified diff applied to a checkout, fuzz 0, all or nothing."""

from __future__ import annotations

import uuid

import pytest

from app.services.sandbox_workspace import patching
from tests.test_sandbox_workspace_tools import _call, _composite, _put, _workspace_bytes, store  # noqa: F401

BEFORE = "one\ntwo\nthree\nfour\nfive\nsix\nseven\n"

MODIFY = """diff --git a/src/a.txt b/src/a.txt
index 1111111..2222222 100644
--- a/src/a.txt
+++ b/src/a.txt
@@ -2,3 +2,3 @@
 two
-three
+THREE
 four
"""


def test_parse_reads_git_headers_new_deleted_renamed_and_mode():
    diff = (
        MODIFY
        + "diff --git a/new.py b/new.py\nnew file mode 100644\nindex 0000000..1\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1,2 @@\n+x = 1\n+y = 2\n"
        + "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
        + "diff --git a/m.py b/n.py\nsimilarity index 100%\nrename from m.py\nrename to n.py\n"
        + "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
        + "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    )
    files = patching.parse(diff)
    assert [(f.path, f.kind) for f in files] == [
        ("src/a.txt", "modified"),
        ("new.py", "added"),
        ("old.py", "deleted"),
        ("n.py", "renamed"),
        ("run.sh", "modified"),
        ("logo.png", "modified"),
    ]
    assert files[0].stat() == (1, 1)
    assert files[4].mode_only and files[5].binary
    assert files[1].hunks[0].new_lines == ["x = 1", "y = 2"]


def test_parse_refuses_what_is_not_a_diff_and_a_cut_hunk():
    with pytest.raises(patching.PatchError, match="not a unified diff"):
        patching.parse("hello\n")
    with pytest.raises(patching.PatchError, match="is cut"):
        patching.parse("--- a/x\n+++ b/x\n@@ -1,3 +1,3 @@\n a\n-b\n")
    with pytest.raises(patching.PatchError, match="unreadable hunk header"):
        patching.parse("--- a/x\n+++ b/x\n@@ bogus @@\n")


def test_apply_at_the_stated_line_and_at_an_offset():
    (patch,) = patching.parse(MODIFY)
    assert patching.apply_file(BEFORE, patch).text == BEFORE.replace("three", "THREE")
    shifted = "zero\nzero2\n" + BEFORE
    applied = patching.apply_file(shifted, patch)
    assert applied.text == shifted.replace("three", "THREE")
    assert applied.offsets == [("@@ -2,3 +2,3 @@", 2)]


def test_a_stale_hunk_is_refused_with_its_line():
    (patch,) = patching.parse(MODIFY)
    with pytest.raises(
        patching.PatchError,
        match=r"hunk @@ -2,3 \+2,3 @@ does not apply: line 3: the patch expects 'three', the file has 'THREE'",
    ):
        patching.apply_file(BEFORE.replace("three", "THREE"), patch)


def test_fuzz_zero_never_drops_context():
    (patch,) = patching.parse(MODIFY)
    with pytest.raises(patching.PatchError):
        patching.apply_file(BEFORE.replace("four", "FOUR"), patch)


def test_new_deleted_and_no_newline_at_end():
    (added,) = patching.parse("--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,2 @@\n+a\n+b\n")
    assert patching.apply_file(None, added).text == "a\nb\n"
    with pytest.raises(patching.PatchError, match="already exists"):
        patching.apply_file("x\n", added)
    (deleted,) = patching.parse("--- a/o.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-a\n-b\n")
    assert patching.apply_file("a\nb\n", deleted).text == ""
    with pytest.raises(patching.PatchError):
        patching.apply_file("a\nb\nc\n", deleted)
    (tail,) = patching.parse("--- a/t\n+++ b/t\n@@ -1,2 +1,2 @@\n a\n-b\n\\ No newline at end of file\n+c\n")
    assert patching.apply_file("a\nb", tail).text == "a\nc\n"
    (cut,) = patching.parse("--- a/t\n+++ b/t\n@@ -1,2 +1,2 @@\n a\n-b\n+c\n\\ No newline at end of file\n")
    assert patching.apply_file("a\nb\n", cut).text == "a\nc"


def test_a_crlf_file_takes_an_lf_patch_and_keeps_its_endings():
    (patch,) = patching.parse(MODIFY)
    crlf = BEFORE.replace("\n", "\r\n")
    assert patching.apply_file(crlf, patch).text == crlf.replace("three", "THREE")


def test_a_git_quoted_path_is_decoded():
    (patch,) = patching.parse('--- "a/caf\\303\\251 x.txt"\n+++ "b/caf\\303\\251 x.txt"\n@@ -1 +1 @@\n-a\n+b\n')
    assert patch.path == "caf\u00e9 x.txt"


def test_exclude_and_paths_outside_the_checkout():
    (patch,) = patching.parse(MODIFY)
    assert (
        patching.excluded(patch, ["src"])
        and patching.excluded(patch, ["*.txt"])
        and patching.excluded(patch, ["src/a.txt"])
    )
    assert not patching.excluded(patch, ["src/b.txt", ""])
    with pytest.raises(patching.PatchError, match="outside the checkout"):
        patching.check_path("../etc/passwd")


async def test_apply_patch_writes_every_file_in_one_write(store):  # noqa: F811
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "repo/src/a.txt", BEFORE.encode())
    await _put(composite, "repo/old.py", b"gone\n")
    await _put(composite, "repo/keep.md", b"keep\n")
    diff = (
        MODIFY
        + "diff --git a/new.py b/new.py\nnew file mode 100644\n--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+x = 1\n"
        + "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n"
        + "diff --git a/keep.md b/keep.md\n--- a/keep.md\n+++ b/keep.md\n@@ -1 +1 @@\n-stale\n+new\n"
    )
    await _put(composite, "work/pr.diff", diff.encode())
    dry = await _call(
        composite, "sandbox_apply_patch", patch="work/pr.diff", checkout="repo", exclude=["keep.md"], dry_run=True
    )
    assert not dry.is_error, dry.content
    assert (
        dry.content.startswith("Dry run, nothing written: 3 file patch(es)") and "Excluded (1): keep.md" in dry.content
    )
    assert await _workspace_bytes(store, conversation, "repo/src/a.txt") == BEFORE.encode()
    done = await _call(composite, "sandbox_apply_patch", patch="work/pr.diff", checkout="repo", exclude=["keep.md"])
    assert not done.is_error, done.content
    assert "(2 written, 1 removed)" in done.content and "src/a.txt: modified, 1 hunk(s), +1 -1" in done.content
    assert await _workspace_bytes(store, conversation, "repo/src/a.txt") == BEFORE.replace("three", "THREE").encode()
    assert await _workspace_bytes(store, conversation, "repo/new.py") == b"x = 1\n"
    (record,) = [r for r in store.records.values() if r.conversation_id == conversation]
    assert "repo/old.py" not in store.files[record.id]
    assert await _workspace_bytes(store, conversation, "repo/keep.md") == b"keep\n"


async def test_a_stale_hunk_writes_nothing(store):  # noqa: F811
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "repo/src/a.txt", BEFORE.encode())
    await _put(composite, "repo/keep.md", b"keep\n")
    diff = MODIFY + "diff --git a/keep.md b/keep.md\n--- a/keep.md\n+++ b/keep.md\n@@ -1 +1 @@\n-stale\n+new\n"
    await _put(composite, "work/pr.diff", diff.encode())
    refused = await _call(composite, "sandbox_apply_patch", patch="work/pr.diff", checkout="repo")
    assert refused.is_error
    assert "1 of 2 file patch(es)" in refused.content and "nothing was written" in refused.content
    assert (
        "keep.md: hunk @@ -1 +1 @@ does not apply: line 1: the patch expects 'stale', the file has 'keep'"
        in refused.content
    )
    assert await _workspace_bytes(store, conversation, "repo/src/a.txt") == BEFORE.encode()
    not_a_diff = await _call(composite, "sandbox_apply_patch", patch="repo/keep.md", checkout="repo")
    assert not_a_diff.is_error and "not a unified diff" in not_a_diff.content


def test_apply_patch_is_offered_with_the_workspace():
    from app.services.builtin_tools.media import build_code_interpreter_tool

    definitions, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    tool = next(d for d in definitions if d.name == "sandbox_apply_patch")
    assert tool.input_schema["required"] == ["patch", "checkout"]
