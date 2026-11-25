"""The code interpreter's sandbox workspace: what persists between calls, and its limits.

Synthetic fixtures only: an in-memory store (the production store's contract,
``store.MemoryWorkspaceStore``) and the real local subprocess sandbox, so a
file written by one ``code_interpreter`` call is read by the next exactly as
in production. The PostgreSQL store has its own suite
(``test_sandbox_workspace_postgres.py``).
"""

from __future__ import annotations

import os
import sys
import uuid

import pytest

from app.services import code_interpreter
from app.services.sandbox_workspace import local
from app.services.sandbox_workspace.local import BlobCache
from app.services.sandbox_workspace.manifest import (
    Limits,
    ManifestEntry,
    QuotaExceededError,
    format_diff,
    format_manifest,
    plan_commit,
)
from app.services.sandbox_workspace.paths import WorkspacePathError, normalize
from app.services.sandbox_workspace.run import (
    WorkspaceCall,
    code_names_workspace,
    format_call,
    mount_entries,
    named_workspace_paths,
    plan_mount,
)
from app.services.sandbox_workspace.session import WorkspaceSession, WorkspaceUnavailableError
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

MB = 1024 * 1024


@pytest.fixture(autouse=True)
def _local_sandbox(monkeypatch, tmp_path):
    from app.core.config import settings

    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    monkeypatch.setattr(settings, "code_interpreter_workspace_enabled", True)
    monkeypatch.setattr(local, "_cache", BlobCache(root=str(tmp_path / "cache"), max_bytes=64 * MB))


def _session(store: MemoryWorkspaceStore, *, user=None, conversation=None, quota=8 * MB, max_files=100) -> WorkspaceSession:
    scope = WorkspaceScope(user_id=user or uuid.uuid4(), conversation_id=conversation or uuid.uuid4())
    return WorkspaceSession(scope, store=store, limits=Limits(quota_bytes=quota, max_files=max_files), manifest_limit=5)


async def _run(code: str, workspace: WorkspaceSession, **kwargs) -> dict:
    return await code_interpreter.execute_python(code, timeout=60, workspace=workspace, **kwargs)


# -- Paths --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("workspace/src/a.py", "src/a.py"),
        ("/workspace/src/a.py", "src/a.py"),
        ("./workspace/a", "a"),
        ("src//a.py/", None),
        ("a.py", "a.py"),
        ("retest/xtm-one/.", "retest/xtm-one"),
        ("src/./a.py", "src/a.py"),
    ],
    ids=["mount-prefix", "absolute-mount", "dot-prefix", "empty-segment", "bare", "trailing-dot", "inner-dot"],
)
def test_a_path_is_read_the_way_the_model_writes_it(raw, expected):
    if expected is None:
        with pytest.raises(WorkspacePathError):
            normalize(raw)
    else:
        assert normalize(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["../etc/passwd", "a/../../b", "/etc/passwd", "a\\b", "a\x00b", "", "workspace"],
    ids=["parent", "nested-parent", "absolute", "backslash", "nul", "empty", "the-root"],
)
def test_a_path_never_leaves_the_workspace(raw):
    with pytest.raises(WorkspacePathError):
        normalize(raw)


def test_the_root_is_a_prefix_only_when_asked():
    assert normalize("workspace/", allow_root=True) == ""
    assert normalize(".", allow_root=True) == ""
    with pytest.raises(WorkspacePathError):
        normalize(".")


# -- The manifest -------------------------------------------------------------


def _entry(path: str, size: int, sha: str = "", mtime: float = 1.0) -> ManifestEntry:
    return ManifestEntry(path=path, size=size, sha256=sha or f"sha-{path}-{size}", mtime=mtime)


def test_a_commit_plan_reports_the_diff_and_the_contents_it_releases():
    current = {"a": _entry("a", 10, "s1"), "b": _entry("b", 20, "s2"), "c": _entry("c", 5, "s1")}
    plan = plan_commit(current, [_entry("b", 25, "s3"), _entry("d", 1, "s4")], ["a"], Limits(1000, 10))
    assert plan.diff.as_dict() == {"added": ["d"], "changed": ["b"], "removed": ["a"]}
    assert plan.size_bytes == 25 + 5 + 1 and plan.file_count == 3
    # s1 is still held by c: only s2 (b's old content) goes.
    assert plan.orphaned == frozenset({"s2"})


def test_a_change_past_the_quota_or_the_file_limit_is_refused_whole():
    with pytest.raises(QuotaExceededError, match=r"quota exceeded.*Nothing it wrote"):
        plan_commit({}, [_entry("big", 101)], [], Limits(100, 10))
    with pytest.raises(QuotaExceededError, match="file limit exceeded"):
        plan_commit({}, [_entry(f"f{i}", 1) for i in range(11)], [], Limits(1000, 10))


def test_a_rewrite_that_does_not_grow_the_count_passes_a_workspace_over_the_file_limit() -> None:
    """#2186: a rebase rewriting files of a workspace already past the limit is not refused."""
    current = {f"f{i}": _entry(f"f{i}", 1) for i in range(12)}
    plan = plan_commit(current, [_entry("f0", 2), _entry("g", 1)], ["f1"], Limits(1000, 10))
    assert plan.file_count == 12 and plan.diff.added == ["g"]
    with pytest.raises(QuotaExceededError, match="file limit exceeded"):
        plan_commit(current, [_entry("g", 1)], [], Limits(1000, 10))


def test_the_manifest_listing_is_bounded_and_counts_the_rest():
    entries = {f"f{i}.txt": _entry(f"f{i}.txt", 100, mtime=1000.0 + i) for i in range(12)}
    entries[".venv/site-packages/pkg/__init__.py"] = _entry(".venv/site-packages/pkg/__init__.py", 50)
    text = format_manifest(entries, limit=3, quota_bytes=MB, now=2000.0)
    lines = text.splitlines()
    assert lines[0].startswith("13 file(s)")
    # Most recent first, three shown, the rest counted, packages summed up.
    assert lines[1].startswith("- workspace/f11.txt") and lines[3].startswith("- workspace/f9.txt")
    assert "and 9 more file(s)" in lines[4]
    assert "installed packages, 1 file(s)" in lines[5]
    assert len(lines) == 6


def test_the_manifest_filter_reads_depth_and_glob_relative_to_the_prefix():
    from app.services.sandbox_workspace.manifest import matches_listing_filter

    assert matches_listing_filter("repo/src/a.test.ts", "repo/src", 1, "*.test.ts")
    assert not matches_listing_filter("repo/src/x/a.test.ts", "repo/src", 1, "")
    assert matches_listing_filter("repo/src/x/a.test.ts", "repo/src", 2, "*.test.ts")
    assert not matches_listing_filter("repo/src/main.ts", "repo/src", None, "*.test.ts")
    assert matches_listing_filter("repo/src/x/a.ts", "repo/src", None, "x/*.ts")
    assert matches_listing_filter("top.md", "", 1, "") and not matches_listing_filter("d/top.md", "", 1, "")
    assert matches_listing_filter("d/e/f.md", "", 0, "")  # 0: no bound


def test_the_manifest_filter_expands_brace_alternatives_in_the_glob():
    from app.services.sandbox_workspace.manifest import MAX_BRACE_EXPANSIONS, expand_braces, matches_listing_filter

    glob = "{architecture,overview}.md"
    assert matches_listing_filter("docs/architecture.md", "docs", None, glob)
    assert matches_listing_filter("docs/overview.md", "docs", None, glob)
    assert not matches_listing_filter("docs/readme.md", "docs", None, glob)
    assert matches_listing_filter("repo/src/x/a.tsx", "repo/src", None, "{x,y}/*.{ts,tsx}")
    assert sorted(expand_braces("a{b,c{d,e}}f")) == ["abf", "acdf", "acef"]
    assert expand_braces("{solo}.md") == ["{solo}.md"]  # no comma: literal
    assert expand_braces("{a,b.md") == ["{a,b.md"]  # unclosed: literal
    wide = "{a,b}" * 7  # 128 expansions
    assert expand_braces(wide) == [wide]
    assert MAX_BRACE_EXPANSIONS < 2**7


def test_the_diff_line_names_each_kind_of_change():
    from app.services.sandbox_workspace.manifest import ManifestDiff

    assert format_diff(ManifestDiff()) == "Workspace unchanged."
    line = format_diff(ManifestDiff(added=["a", "b"], removed=["c"]))
    assert line == "Workspace added 2: workspace/a, workspace/b; removed 1: workspace/c."


def test_the_code_names_the_workspace_as_a_word():
    assert code_names_workspace("open('workspace/a.txt')")
    assert code_names_workspace("os.environ['WORKSPACE_DIR']")
    assert not code_names_workspace("workspaces = []")
    assert not code_names_workspace("print(1)")


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("print(1)", []),
        ("open('workspace/a.txt').read()", ["a.txt"]),
        ('open("workspace/src/x.py"); open("workspace/src/y.py")', ["src/x.py", "src/y.py"]),
        ("glob.glob('workspace/data/*.csv')", ["data"]),
        ("pd.read_csv('workspace/./d/a.csv')", ["d/a.csv"]),
        ("os.listdir('workspace')", None),
        ("open(f'workspace/{name}')", None),
        ("open('workspace/' + name)", None),
        ("open('workspace/a' + suffix)", None),
        ("'workspace/%s' % name", None),
        ("'workspace/{}'.format(name)", None),
        ("os.environ['WORKSPACE_DIR']", None),
        ("open('workspace/../etc/passwd')", None),
        ("glob.glob('workspace/**/*.py')", None),
        ("open('workspace/a.txt'); os.walk('workspace')", None),
        ("open('workspace/my file.txt')", None),
    ],
)
def test_a_run_names_workspace_paths_only_through_literals(code, expected):
    assert named_workspace_paths(code) == expected


def _entries(*paths: str) -> dict:
    return {p: ManifestEntry(path=p, size=1, sha256="0" * 64, mtime=0.0) for p in paths}


def test_the_mount_keeps_the_named_paths_and_the_packages():
    entries = _entries("src/a.py", "src/b.py", "srcx/c.py", "docs/d.md", ".venv/lib/m.py")
    # A named file brings its directory (src/b.py), never a lookalike sibling directory (srcx/).
    assert sorted(mount_entries("open('workspace/src/a.py')", entries)) == [".venv/lib/m.py", "src/a.py", "src/b.py"]
    assert sorted(mount_entries("glob.glob('workspace/src/*.py')", entries)) == [
        ".venv/lib/m.py",
        "src/a.py",
        "src/b.py",
    ]
    assert mount_entries("os.listdir('workspace')", entries) is entries
    assert sorted(mount_entries("print(1)", entries)) == [".venv/lib/m.py"]
    assert mount_entries("print(1)", _entries("src/a.py")) is None


def test_a_named_file_brings_its_directory_tree_recursively():
    entries = _entries("work/a.csv", "work/b.csv", "work/sub/c.csv", "other/d.csv")
    mounted, left_out = plan_mount("open('workspace/work/a.csv')", entries)
    assert sorted(mounted) == ["work/a.csv", "work/b.csv", "work/sub/c.csv"]
    assert left_out == []
    # A file at the root brings the root: the whole workspace, within the budget.
    mounted, _ = plan_mount("open('workspace/notes.txt')", {**entries, **_entries("notes.txt")})
    assert len(mounted) == 5


def test_a_tree_over_the_widening_budget_is_left_out_and_named(monkeypatch):
    from app.services.sandbox_workspace import run

    monkeypatch.setattr(run, "WIDEN_BUDGET_FILES", 2)
    entries = _entries("small/a.py", "small/b.py", "big/x.py", "big/y.py", "big/z.py", "big/w.py")
    mounted, left_out = plan_mount("open('workspace/small/a.py'); open('workspace/big/x.py')", entries)
    # The smaller tree fits; the bigger one does not, but its named file is still mounted.
    assert sorted(mounted) == ["big/x.py", "small/a.py", "small/b.py"]
    assert [(tree.directory, tree.files) for tree in left_out] == [("big", 3)]
    call = WorkspaceCall(mounted=True, partial=(3, 6), left_out=left_out)
    line = format_call(call)
    assert "Left out, over the 32.0 MB / 2-file mount budget: workspace/big/ (3 files, 3 B)" in line
    assert "literal 'workspace/...' path" in line


async def test_a_run_naming_one_file_moves_that_file_and_keeps_the_rest():
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    await workspace.put_tree([("big/blob.bin", b"0" * 200_000), ("notes/a.txt", b"hello")])
    result = await _run(
        "print(open('workspace/notes/a.txt').read())\n_ = open('workspace/notes/b.txt', 'w').write('new')", workspace
    )
    assert result["error"] is None, result
    assert result["stdout"].strip() == "hello"
    call = result["workspace"]
    assert call.mounted and call.partial == (1, 2)
    assert call.bytes_read == len(b"hello")
    assert call.diff.as_dict() == {"added": ["notes/b.txt"], "changed": [], "removed": []}
    assert sorted(await workspace.entries(refresh=True)) == ["big/blob.bin", "notes/a.txt", "notes/b.txt"]
    assert "only the paths the code names and their directories, 1 of 2 files" in format_call(call)


async def test_an_exec_d_file_imports_its_sibling_module_and_joins_paths():
    """A file reached through exec, an import of its sibling and an os.path.join are all mounted."""
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    main = (
        "import os, sys\n"
        "base = os.path.join('workspace', 'src')\n"
        "sys.path.insert(0, base)\n"
        "import helper\n"
        "print(helper.VALUE, open(os.path.join(base, 'data.txt')).read())\n"
    )
    await workspace.put_tree(
        [
            ("src/main.py", main.encode()),
            ("src/helper.py", b"VALUE = 'sibling'\n"),
            ("src/data.txt", b"joined"),
            ("big/blob.bin", b"0" * 200_000),
        ]
    )
    result = await _run("exec(open('workspace/src/main.py').read())", workspace)
    assert result["error"] is None, result
    assert result["stdout"].strip() == "sibling joined"
    assert result["workspace"].partial == (3, 4)


async def test_a_scoped_run_never_overwrites_a_file_it_was_not_given():
    """A path derived at run time reaches a file left out: the stored file keeps its content."""
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    await workspace.put_tree([("src/a.txt", b"a"), ("other/log.txt", b"history\n")])
    code = (
        "import os\n"
        "root = 'workspace/src'\n"
        "target = root.replace('src', 'other') + '/log.txt'\n"
        "os.makedirs(os.path.dirname(target), exist_ok=True)\n"
        "_ = open(target, 'a').write('appended\\n')\n"
    )
    result = await _run(code, workspace)
    assert result["error"] is None, result
    call = result["workspace"]
    assert call.partial == (1, 2)
    assert any(s.startswith("other/log.txt (exists but was not mounted") for s in call.skipped)
    _entry, data = await workspace.get_bytes("other/log.txt")
    assert data == b"history\n"


# -- What persists between calls ---------------------------------------------


async def test_a_file_written_in_one_call_is_read_in_the_next():
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    first = await _run(
        "import os\nos.makedirs('workspace/out', exist_ok=True)\n"
        "open('workspace/out/data.txt', 'w').write('hello from call 1')\nprint('written')",
        workspace,
    )
    assert first["error"] is None, first
    call = first["workspace"]
    assert call.mounted and call.diff.added == ["out/data.txt"]
    assert call.bytes_written == len("hello from call 1")
    # The workspace is not an output: no download card for it.
    assert first["output_files"] == []

    second = await _run("print(open('workspace/out/data.txt').read())", workspace)
    assert second["stdout"].strip() == "hello from call 1"
    assert second["workspace"].diff.empty
    assert second["workspace"].bytes_read == len("hello from call 1")
    text = code_interpreter.format_text_result(second)
    assert text.startswith("[Fresh process (session=false)") and "Workspace unchanged." in text


async def test_importing_a_workspace_module_writes_no_bytecode_cache():
    """An import of a checkout module leaves no __pycache__ behind (#9518)."""
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    await workspace.put_bytes("repo/pkg/helper.py", b"VALUE = 42\n")
    result = await _run(
        "import sys\nsys.path.insert(0, 'workspace/repo/pkg')\nimport helper\nprint(helper.VALUE)",
        workspace,
    )
    assert result["error"] is None, result
    assert result["stdout"].strip() == "42"
    assert result["workspace"].diff.empty, result["workspace"].diff
    assert sorted((await workspace.entries()).keys()) == ["repo/pkg/helper.py"]


async def test_a_call_that_does_not_name_the_workspace_does_not_mount_it():
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    await workspace.put_bytes("kept.txt", b"x")
    result = await _run("import os\nprint(sorted(os.listdir('.')))", workspace)
    assert "workspace" not in result["stdout"]
    assert result["workspace"].mounted is False and result["workspace"].bytes_read == 0


async def test_a_removed_and_a_changed_file_are_saved_as_such():
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    await workspace.put_tree([("a.txt", b"a"), ("b.txt", b"b")])
    result = await _run("import os\nos.remove('workspace/a.txt')\nopen('workspace/b.txt','w').write('B2')", workspace)
    assert result["workspace"].diff.as_dict() == {"added": [], "changed": ["b.txt"], "removed": ["a.txt"]}
    entries = await workspace.entries(refresh=True)
    assert sorted(entries) == ["b.txt"]
    _entry_b, data = await workspace.get_bytes("b.txt")
    assert data == b"B2"


async def test_a_change_past_the_quota_saves_nothing_and_says_so():
    store = MemoryWorkspaceStore()
    workspace = _session(store, quota=1024)
    result = await _run("open('workspace/big.bin','wb').write(b'0' * 4096)", workspace)
    call = result["workspace"]
    assert "Workspace quota exceeded" in call.refused and "Nothing it wrote under workspace/ was saved" in call.refused
    assert await workspace.entries(refresh=True) == {}
    assert "quota exceeded" in code_interpreter.format_text_result(result)
    # Nothing was left in the store either.
    assert store.blobs.objects == {}


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
async def test_a_link_is_never_followed_nor_saved(tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("not yours")
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    result = await _run(f"import os\nos.symlink({str(secret)!r}, 'workspace/link.txt')", workspace)
    call = result["workspace"]
    assert "link.txt (a link)" in call.skipped
    assert await workspace.entries(refresh=True) == {}


# -- Isolation ----------------------------------------------------------------


async def test_two_conversations_never_share_a_workspace():
    store = MemoryWorkspaceStore()
    user = uuid.uuid4()
    one = _session(store, user=user)
    two = _session(store, user=user)
    await one.put_bytes("only-one.txt", b"1")
    assert await two.entries() == {}
    result = await _run("import os\nprint(os.path.exists('workspace/only-one.txt'))", two)
    assert result["stdout"].strip() == "False"


async def test_another_person_never_opens_the_workspace():
    store = MemoryWorkspaceStore()
    conversation = uuid.uuid4()
    owner = _session(store, user=uuid.uuid4(), conversation=conversation)
    await owner.put_bytes("private.txt", b"mine")
    intruder = _session(store, user=uuid.uuid4(), conversation=conversation)
    with pytest.raises(WorkspaceUnavailableError, match="belongs to another person"):
        await intruder.entries()
    result = await _run("print(open('workspace/private.txt').read())", intruder)
    assert result["workspace"].unavailable and "belongs to another person" in result["workspace"].unavailable
    assert "mine" not in result["stdout"]


# -- The report ---------------------------------------------------------------


def test_the_call_line_says_the_mode_and_what_was_saved():
    call = WorkspaceCall(mode="session", kernel="restarted", mounted=True, bytes_read=10, bytes_written=0)
    line = format_call(call)
    assert line.startswith("[Kernel (session=true): a NEW kernel")
    assert "variables and imports from earlier calls are NOT defined" in line
    assert line.isascii()


async def test_the_workspace_session_creates_nothing_before_a_write():
    store = MemoryWorkspaceStore()
    workspace = _session(store)
    assert await workspace.entries() == {}
    assert store.records == {}
    await workspace.prepare()
    assert workspace.described.startswith("The workspace is empty")
    assert os.path.basename("x") == "x"
