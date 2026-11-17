"""sandbox_run_command(affected=true): the precision rules of gap family affected-selection-precision (#11491)."""

from __future__ import annotations

import uuid

from app.services.sandbox_workspace import affected
from app.services.sandbox_workspace.checkout import build_record, changes_since
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

TOOL = b'''"""A tool."""

AUDITED = ("workspace_move", "workspace_copy")


def build_schema():
    return {"properties": {"prompt": {"type": "string"}}}
'''

TRIPWIRE = b"""from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"


def test_no_published_page_says_canvas():
    assert not [p for p in (DOCS / "docs").rglob("*.md") if "canvas" in p.read_text()]
"""


def _workspace() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=11), conversation_id=uuid.uuid4())
    return WorkspaceSession(
        scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=8 * 1024 * 1024, max_files=200)
    )


async def _select(tree, edits, *, sub_paths=(), **kwargs):
    workspace = _workspace()
    record = build_record(
        provider="github",
        repository="o/r",
        ref="main",
        commit="c" * 40,
        path="co",
        sub_paths=list(sub_paths),
        files=tree,
    )
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([(f"co/{p}", d) for p, d in edits])
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree)[repo_path]

    return await affected.affected_tests(workspace, record, changes, cwd="co/backend", read_base=read_base, **kwargs)


def test_a_test_reads_a_prose_file_by_its_name_or_by_walking_its_directory():
    assert affected.reads_prose(
        'page = ROOT / "docs" / "docs" / "usage" / "presentations.md"', "docs/docs/usage/presentations.md"
    )
    assert affected.reads_prose(TRIPWIRE.decode(), "docs/docs/usage/flows.md")
    # The extension must match the walk, and the directory must be quoted as a path.
    assert not affected.reads_prose(TRIPWIRE.decode(), "docs/docs/usage/flows.rst")
    assert not affected.reads_prose('assert "docs" in nav\nfor p in ROOT.rglob("*.md"): pass', "dev-docs/roi.md")
    assert not affected.reads_prose("assert sentences('flows')", "docs/docs/usage/flows.md")


async def test_a_changed_docs_page_selects_the_tripwire_walking_the_docs_tree():
    """#11381: a docs page changed, the affected run was green, the tripwire banning a word went red in CI."""
    tree = [
        ("docs/docs/usage/flows.md", b"# Flows\n\nText.\n"),
        ("backend/tests/test_flow_builder_feature_flag.py", TRIPWIRE),
        ("backend/tests/test_flow_runs.py", b"def test_runs():\n    assert run_flow() == 1\n"),
    ]
    selection = await _select(tree, [("docs/docs/usage/flows.md", b"# Flows\n\nThe canvas.\n")])
    assert selection.tests == {"tests/test_flow_builder_feature_flag.py": ["reads docs/docs/usage/flows.md"]}
    assert any(
        "1 changed prose file(s)" in n
        and "docs/docs/usage/flows.md (read by tests/test_flow_builder_feature_flag.py)" in n
        for n in selection.notes
    )


async def test_a_removed_literal_selects_the_test_quoting_it_and_related_files_are_read_first():
    """#11328: the budget ended 1,700 files before the audit test quoting the removed literal."""
    audit = b'AUDITED = {\n    "workspace_move",\n}\n\ndef test_reads_are_audited():\n    assert AUDITED\n'
    tree = [
        ("backend/app/services/sandbox_workspace/tool.py", TOOL),
        *[(f"backend/tests/test_aaa_unrelated_{i}.py", b"def test_x():\n    assert True\n") for i in range(6)],
        ("backend/tests/test_sandbox_workspace_audit_reads.py", audit),
    ]
    after = TOOL.replace(b'("workspace_move", "workspace_copy")', b'("workspace_copy",)')
    edits = [("backend/app/services/sandbox_workspace/tool.py", after)]
    ticks = iter(range(100))
    # The clock passes the deadline after two reads: path order would read two unrelated files only.
    selection = await _select(tree, edits, deadline=4.0, clock=lambda: float(next(ticks)))
    assert "tests/test_sandbox_workspace_audit_reads.py" in selection.tests
    assert "workspace_move" in selection.tests["tests/test_sandbox_workspace_audit_reads.py"]
    assert selection.unsearched > 0
    note = next(n for n in selection.notes if "the first not searched" in n)
    assert "tests/test_aaa_unrelated_" in note
    assert "budget 4 s" in note


async def test_a_plural_page_reads_its_singular_tripwire_first_on_the_retest_shape():
    """Retest 95c8924b: sub_paths backend + docs/docs, a module and flows.md changed, the budget ending early.

    ``flows`` shared no token with ``test_flow_builder_feature_flag.py``: the tripwire stayed among the
    unread files. The selection was listed by path, tests reading frontend/ mixed in.
    """
    module = b'STATUS = "agentic_action"\n'
    tree = [
        ("backend/app/services/chat_turn_stream.py", module),
        ("docs/docs/usage/flows.md", b"# Flows\n\nText.\n"),
        *[(f"backend/tests/test_aaa_unrelated_{i}.py", b"def test_x():\n    assert True\n") for i in range(8)],
        ("backend/tests/test_flow_builder_feature_flag.py", TRIPWIRE),
        (
            "backend/tests/test_chat_turn_counter.py",
            b'from app.services import chat_turn_stream\nFRONT = "frontend"\n\ndef test_it():\n    assert chat_turn_stream\n',
        ),
        (
            "backend/tests/test_zz_chat_turn_stream_web.py",
            b"from app.services import chat_turn_stream\n\ndef test_it():\n    assert chat_turn_stream\n",
        ),
        ("frontend/package.json", b"{}"),
    ]
    edits = [
        ("backend/app/services/chat_turn_stream.py", b'STATUS = "agentic_action"  # edited\n'),
        ("docs/docs/usage/flows.md", b"# Flows\n\nThe canvas.\n"),
    ]
    ticks = iter(range(100))
    selection = await _select(
        tree, edits, sub_paths=["backend", "docs/docs"], deadline=5.0, clock=lambda: float(next(ticks))
    )
    # (b) the tripwire is read before the unrelated files and selected by the page.
    assert selection.tests["tests/test_flow_builder_feature_flag.py"] == ["reads docs/docs/usage/flows.md"]
    assert any("(read by tests/test_flow_builder_feature_flag.py)" in n for n in selection.notes)
    # (a) ranked, not alphabetical: the closest name first, the test reading frontend/ last and marked.
    assert list(selection.tests)[-1] == "tests/test_chat_turn_counter.py"
    assert list(selection.tests).index("tests/test_zz_chat_turn_stream_web.py") == 0
    assert "[needs frontend/]" in selection.describe()
    # (c) one note: the budget value and the first files not searched.
    note = next(n for n in selection.notes if "the first not searched" in n)
    assert "budget 5 s" in note and "tests/test_aaa_unrelated_" in note


def test_affinity_counts_the_name_tokens_a_test_shares_with_the_change():
    tokens = affected.change_tokens(
        ["backend/app/services/sandbox_workspace/affected.py", "frontend/src/DeckStage.tsx"]
    )
    assert {"sandbox", "workspace", "affected", "deck", "stage"} <= tokens
    assert affected.affinity("tests/test_sandbox_affected_tests.py", tokens) == 2
    assert affected.affinity("tests/test_kb_sync.py", tokens) == 0
    # A plural page relates to its singular test, and a word ending in ss stays whole.
    assert (
        affected.affinity("tests/test_flow_builder_feature_flag.py", affected.change_tokens(["docs/usage/flows.md"]))
        == 1
    )
    assert "access" in affected.name_tokens("tests/test_access.py")


async def test_a_top_level_directory_name_selects_no_test_and_tests_reading_an_absent_tree_run_last():
    """A checks list quoting "frontend" selected 53 test files of a backend-only checkout (FileNotFoundError)."""
    before = b'NEEDS = ["backend"]\n\n\ndef needs():\n    return NEEDS\n'
    after = b'NEEDS = ["backend", "frontend"]\n\n\ndef needs():\n    return NEEDS\n'
    reads_front = (
        b'from app.checks_list import needs\n\nROOT = "frontend/src"\n\ndef test_front():\n    assert needs()\n'
    )
    tree = [
        ("backend/app/checks_list.py", before),
        (
            "backend/tests/test_checks_list.py",
            b"from app.checks_list import needs\n\ndef test_it():\n    assert needs()\n",
        ),
        ("backend/tests/test_frontend_pins.py", reads_front),
        ("backend/tests/test_any_frontend.py", b'def test_it():\n    assert "frontend" in layout()\n'),
        ("frontend/package.json", b"{}"),
    ]
    selection = await _select(tree, [("backend/app/checks_list.py", after)], sub_paths=["backend"])
    assert "tests/test_any_frontend.py" not in selection.tests
    assert list(selection.tests) == ["tests/test_checks_list.py", "tests/test_frontend_pins.py"]
    assert any("top-level directory name(s) of the repository select no test: frontend" in n for n in selection.notes)
    assert selection.outside == {"tests/test_frontend_pins.py": ["frontend"]}
    assert any(
        "read a directory this checkout does not hold" in n and 'add_paths=["frontend"]' in n for n in selection.notes
    )
    assert "- tests/test_frontend_pins.py: " in selection.describe()
    assert "[needs frontend/] (not run)" in selection.describe()
    # Gap affected-runs-needs-files-on-narrow-checkout: the marked file is counted, never run.
    assert selection.runnable() == ["tests/test_checks_list.py"]
    assert any(n.startswith("Not runnable here: 1 selected test file(s)") for n in selection.notes)


async def test_a_whole_tree_checkout_runs_every_selected_test_file():
    """Full-tree behaviour unchanged: a test reading frontend/ runs when the checkout holds frontend/."""
    tree = [
        ("backend/app/checks_list.py", b"def needs():\n    return 1\n"),
        (
            "backend/tests/test_frontend_pins.py",
            b'from app.checks_list import needs\n\nROOT = "frontend/src"\n\ndef test_front():\n    assert needs()\n',
        ),
        ("frontend/package.json", b"{}"),
    ]
    selection = await _select(tree, [("backend/app/checks_list.py", b"def needs():\n    return 2\n")], sub_paths=[])
    assert selection.outside == {}
    assert selection.runnable() == ["tests/test_frontend_pins.py"]


def test_a_whole_tree_checkout_misses_no_directory():
    record = build_record(
        provider="github",
        repository="o/r",
        ref="main",
        commit="c" * 40,
        path="co",
        sub_paths=[],
        files=[("frontend/a.ts", b"")],
    )
    assert affected.missing_directories(record) == set()
    assert affected.top_directories(record) == {"frontend"}


# -- gap family affected-selection (item 9c3f3318) ------------------------------


async def test_a_test_reading_two_absent_trees_names_both_in_the_mark_and_the_add_paths_hint():
    """Member affected-needs-dirs-path-joins: the mark said [needs .xtm-one/], the test failed on frontend/."""
    reads_two = (
        b"from app.checks_list import needs\n\n"
        b'LAB = ".xtm-one/lab"\nFRONT = "frontend/tests_unit"\n\ndef test_it():\n    assert needs()\n'
    )
    tree = [
        ("backend/app/checks_list.py", b"def needs():\n    return 1\n"),
        ("backend/tests/test_checks_two_trees.py", reads_two),
        ("frontend/package.json", b"{}"),
        (".xtm-one/checks.toml", b""),
    ]
    selection = await _select(
        tree, [("backend/app/checks_list.py", b"def needs():\n    return 2\n")], sub_paths=["backend"]
    )
    assert selection.outside == {"tests/test_checks_two_trees.py": [".xtm-one", "frontend"]}
    assert "[needs .xtm-one/, frontend/]" in selection.describe()
    assert any('add_paths=[".xtm-one", "frontend"]' in n for n in selection.notes)


def test_reads_directories_answers_every_absent_tree_a_test_quotes():
    text = 'A = ".github/workflows"\nB = "dev-docs"\n'
    assert affected.reads_directories(text, {"dev-docs", ".github", "frontend"}) == [".github", "dev-docs"]
    assert affected.reads_directories("x = 1\n", {"frontend"}) == []


def test_a_test_names_a_changed_file_by_its_file_name():
    """Member affected-misses-frontend-guard-tests: a source pin reads the file it guards by name."""
    pin = 'flows = _read("src", "pages", "Flows.tsx")\n'
    assert affected.names_file(pin, "frontend/src/pages/Flows.tsx")
    # A longer name holding it, or a different extension, is not the file.
    assert not affected.names_file('x = "MyFlows.tsx"', "frontend/src/pages/Flows.tsx")
    assert not affected.names_file('x = "Flows.tsx.snap"', "frontend/src/pages/Flows.tsx")
    # A stem that says nothing never selects.
    assert not affected.names_file('x = "index.ts"', "frontend/src/index.ts")
    assert not affected.names_file('x = "__init__.py"', "backend/app/__init__.py")


async def test_a_changed_frontend_page_selects_the_backend_pin_naming_it():
    """The pin reading ``Flows.tsx`` names no identifier the change touches: it was not selected."""
    page = b"export function Flows() {\n  return null;\n}\n"
    pin = b'def test_pin():\n    assert "useFeatureFlagResolved" in _read("src", "pages", "Flows.tsx")\n'
    tree = [
        ("frontend/src/pages/Flows.tsx", page),
        ("backend/tests/test_flow_builder_feature_flag.py", pin),
        ("backend/tests/test_unrelated.py", b"def test_x():\n    assert True\n"),
    ]
    edits = [("frontend/src/pages/Flows.tsx", page.replace(b"null", b"<div />"))]
    selection = await _select(tree, edits)
    assert "names Flows.tsx" in selection.tests["tests/test_flow_builder_feature_flag.py"]
    assert "tests/test_unrelated.py" not in selection.tests


async def test_the_test_files_are_read_through_one_manifest_not_one_per_file(monkeypatch):
    """Members affected-selection-time-budget / affected-tests-time-budget: 840 of 3,084 files read in 90 s.

    Each file was read with get_bytes, which reads the whole manifest again; the selection now reads
    the manifest once and each blob through it.
    """
    tree = [
        ("backend/app/checks_list.py", b"def needs():\n    return 1\n"),
        *[(f"backend/tests/test_n{i}.py", b"def test_x():\n    assert True\n") for i in range(30)],
    ]
    workspace = _workspace()
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=[], files=tree
    )
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([("co/backend/app/checks_list.py", b"def needs():\n    return 2\n")])
    changes = changes_since(record, await workspace.entries(refresh=True))
    refreshes = 0
    real = workspace.entries

    async def counting(*, refresh: bool = False):
        nonlocal refreshes
        refreshes += refresh
        return await real(refresh=refresh)

    monkeypatch.setattr(workspace, "entries", counting)

    async def read_base(repo_path: str) -> bytes:
        return dict(tree)[repo_path]

    selection = await affected.affected_tests(workspace, record, changes, cwd="co/backend", read_base=read_base)
    assert selection.scanned == 30
    # One for the changed source, one for the candidate list: never one per test file.
    assert refreshes <= 2
