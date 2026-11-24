"""sandbox_run_command(affected=true): the tests a checkout's changes affect."""

from __future__ import annotations

import uuid

import pytest

from app.services.sandbox_workspace import affected, commands
from app.services.sandbox_workspace.checkout import build_record, changes_since
from app.services.sandbox_workspace.checks import ReportSummary
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope

BEFORE = b'''"""A tool."""


def helper(value):
    return value + 1


def build_schema():
    return {"properties": {"prompt": {"type": "string"}}}
'''

AFTER = b'''"""A tool."""


def helper(value):
    return value + 1


def build_schema():
    return {"properties": {"prompt": {"type": "string"}, "start_frame": {"type": "string"}}}
'''


def test_a_test_file_is_known_by_its_name():
    for name in ("tests/test_x.py", "x_test.py", "src/a.test.ts", "src/b.spec.tsx", "c.test.mjs"):
        assert affected.is_test_file(name), name
    for name in ("app/x.py", "src/a.ts", "testing.py", "conftest.py"):
        assert not affected.is_test_file(name), name


def test_an_edit_names_the_definition_it_touches_and_the_literal_it_adds_not_the_untouched_ones():
    names = affected.changed_identifiers("backend/app/tool.py", BEFORE, AFTER, base="backend")
    assert "build_schema" in names and "start_frame" in names
    assert "helper" not in names
    # The module path of a Python file under the check's directory.
    assert "app.tool" in names


def test_an_added_or_unreadable_base_counts_every_definition():
    names = affected.changed_identifiers("backend/app/tool.py", None, AFTER, base="backend")
    assert "build_schema" in names
    # ``helper`` is a plain six-letter word: set aside as too generic.
    assert "helper" not in names


def test_short_and_common_names_are_not_searched():
    names = affected.changed_identifiers("a.py", b"x = 1\n", b'x = 2\nkey = "self"\nid = "abc"\n')
    assert not {"self", "abc", "x", "id"} & names


def test_plain_short_words_are_set_aside_and_named_but_identifiers_stay():
    generic: set[str] = set()
    after = b'state = "done"\nmode = "take"\nkey = "start_frame"\nk2 = "buildSchema"\nk3 = "finished_at"\nk4 = "incremental"\n'
    names = affected.changed_identifiers("a.py", b"", after, generic=generic)
    assert {"start_frame", "buildSchema", "finished_at", "incremental"} <= names
    assert not {"done", "take"} & names
    assert {"done", "take"} <= generic


def test_a_helper_nested_in_a_function_is_covered_by_its_enclosing_definition():
    before = b"def run_selection():\n    def nearest(x):\n        return x\n    return nearest(1)\n\n\nclass Box:\n    def open_lid(self):\n        return 1\n"
    after = b"def run_selection():\n    def nearest(x):\n        return x + 1\n    return nearest(1)\n\n\nclass Box:\n    def open_lid(self):\n        return 2\n"
    names = affected.changed_identifiers("m.py", before, after)
    assert "run_selection" in names and "nearest" not in names
    # A method stays: a class does not stand for its methods.
    assert "open_lid" in names


def test_selection_is_ranked_so_the_bound_keeps_the_best():
    tests = {
        "tests/test_a.py": "start_frame",
        "tests/test_b.py": "start_frame build_schema",
        "tests/test_c.py": "from app.tool import x",
        "tests/test_z.py": "",
    }
    got = affected.select_tests(tests, {"start_frame", "build_schema", "app.tool"}, changed=["tests/test_z.py"])
    assert list(got) == ["tests/test_z.py", "tests/test_c.py", "tests/test_b.py", "tests/test_a.py"]


def test_selection_is_word_for_word_and_keeps_changed_tests():
    tests = {
        "tests/test_schema.py": "assert 'start_frame' in props",
        "tests/test_other.py": "start_frames_count = 1",
        "tests/test_module.py": "from app.tool import helper",
        "tests/test_new.py": "",
    }
    got = affected.select_tests(tests, {"start_frame", "app.tool"}, changed=["tests/test_new.py"])
    assert got == {
        "tests/test_module.py": ["app.tool"],
        "tests/test_new.py": ["changed"],
        "tests/test_schema.py": ["start_frame"],
    }


def test_no_identifier_selects_only_the_changed_tests():
    assert affected.select_tests({"tests/test_a.py": "anything"}, set()) == {}
    assert affected.select_tests({}, set(), changed=["tests/test_b.py"]) == {"tests/test_b.py": ["changed"]}


def test_a_module_is_referenced_by_its_path_or_a_from_import():
    names = affected.module_reference("app.services.box.reading")
    assert names("from app.services.box.reading import symbols")
    assert names("import app.services.box.reading as r")
    assert names("from app.services.box import paths, reading")
    assert names("from app.services.box import (\n    paths,\n    reading,\n)")
    assert not names("from app.services.box import reading_list")
    assert not names("from app.services.other import reading")
    assert not names("reading = open(path).read()")


def test_a_name_a_changed_module_defines_selects_only_the_tests_importing_it():
    tests = {
        "tests/test_box.py": "from app.services.box import reading\nassert reading.MAX_RESULT_CHARS",
        "tests/test_direct.py": "from app.services.box.reading import symbols\nsymbols()",
        "tests/test_other.py": "from app.chat import MAX_RESULT_CHARS\nassert MAX_RESULT_CHARS",
        "tests/test_schema.py": "assert 'start_frame' in props",
    }
    scoped = {"app.services.box.reading": {"MAX_RESULT_CHARS", "symbols"}}
    got = affected.select_tests(tests, {"app.services.box.reading", "start_frame"}, scoped=scoped)
    assert got == {
        # ``reading.MAX_RESULT_CHARS`` is reached through the module, which selects the file.
        "tests/test_box.py": ["app.services.box.reading"],
        "tests/test_direct.py": ["app.services.box.reading", "symbols"],
        "tests/test_schema.py": ["start_frame"],
    }


async def test_a_changed_module_selects_its_importers_not_every_file_naming_its_constants():
    workspace = _workspace()
    before = b'MAX_RESULT_CHARS = 60_000\n\n\ndef window_lines(rows):\n    return rows\n'
    after = b'MAX_RESULT_CHARS = 50_000\n\n\ndef window_lines(rows):\n    return rows[:1]\n'
    tree = [
        ("backend/app/box/reading.py", before),
        ("backend/tests/test_reading.py", b"from app.box.reading import window_lines\n"),
        ("backend/tests/test_chat.py", b"from app.chat import MAX_RESULT_CHARS, window_lines\n"),
    ]
    record = build_record(provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=["backend"], files=tree)
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([("co/backend/app/box/reading.py", after)])
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree)[repo_path]

    selection = await affected.affected_tests(workspace, record, changes, cwd="co/backend", read_base=read_base)
    assert selection.tests == {"tests/test_reading.py": ["app.box.reading", "window_lines"]}
    # test_chat.py names window_lines too, but imports it from another module: not selected.
    assert selection.scoped == {"app.box.reading": {"window_lines"}}
    assert "select only the tests that import it: app.box.reading" in selection.describe()


def test_a_name_from_a_literal_alone_selects_only_the_tests_quoting_it():
    tests = {
        "tests/test_schema.py": "assert 'start_frame' in props",
        "tests/test_ts.test.ts": 'expect(body).toHaveProperty("start_frame")',
        "tests/test_tpl.test.ts": "expect(text).toContain(`start_frame`)",
        "tests/test_param.py": "def run(start_frame):\n    return start_frame",
    }
    got = affected.select_tests(tests, {"start_frame"}, quoted_only={"start_frame"})
    assert set(got) == {"tests/test_schema.py", "tests/test_ts.test.ts", "tests/test_tpl.test.ts"}
    # Without the literal-only mark, the parameter of the same name selects too.
    assert "tests/test_param.py" in affected.select_tests(tests, {"start_frame"})


def test_quotes_reads_whole_quoted_names_only():
    assert affected.quotes("x = d['start_frame'] + o.end_frame + `stop_at` + f\"{a}\" + 'two words'") == {"start_frame", "stop_at"}


def test_a_name_most_test_files_carry_is_set_aside_but_rare_names_and_modules_select():
    tests = {f"tests/test_{i:02d}.py": "client = make_client()\n" for i in range(40)}
    tests["tests/test_target.py"] = "from app.tool import x\nclient = make_client()\nbuild_schema()"
    tests["tests/test_rare.py"] = "build_schema()"
    tests["tests/test_changed.py"] = "make_client()"
    common: set[str] = set()
    got = affected.select_tests(
        tests, {"make_client", "build_schema", "app.tool"}, changed=["tests/test_changed.py"], common=common
    )
    assert common == {"make_client"}
    assert got == {
        "tests/test_changed.py": ["changed"],
        "tests/test_target.py": ["app.tool", "build_schema"],
        "tests/test_rare.py": ["build_schema"],
    }


def test_a_small_suite_keeps_every_hit_under_the_floor():
    tests = {f"tests/test_{i}.py": "make_client()" for i in range(affected.COMMON_FLOOR)}
    common: set[str] = set()
    assert len(affected.select_tests(tests, {"make_client"}, common=common)) == affected.COMMON_FLOOR
    assert common == set()


async def test_a_schema_key_selects_the_test_quoting_it_not_the_one_naming_a_variable_so():
    workspace = _workspace()
    tree = [
        ("backend/app/tool.py", BEFORE),
        ("backend/tests/test_schema.py", b"def test_it():\n    assert 'start_frame' in build()\n"),
        ("backend/tests/test_var.py", b"def test_it(start_frame=0):\n    assert start_frame == 0\n"),
    ]
    record = build_record(provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=[], files=tree)
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([("co/backend/app/tool.py", AFTER)])
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree)[repo_path]

    selection = await affected.affected_tests(workspace, record, changes, cwd="co/backend", read_base=read_base)
    # ``properties`` is on both sides of the change (and a schema keyword): only the added ``start_frame`` counts.
    assert selection.quoted_only == {"start_frame"}
    assert selection.tests == {"tests/test_schema.py": ["start_frame"]}
    assert "select only the tests quoting them: start_frame." in selection.describe()


TOOL_BEFORE = b'''"""A mail tool."""


def list_mail_schema():
    return {
        "type": "object",
        "properties": {
            "folder": {"type": "string"},
        },
    }
'''

TOOL_AFTER = b'''"""A mail tool."""


def list_mail_schema():
    return {
        "type": "object",
        "properties": {
            "folder": {"type": "string", "description": "The folder read, inbox by default."},
            "page_token": {"type": "string", "description": "The token of the next page."},
        },
    }
'''


def test_schema_keywords_and_short_plain_literals_are_generic_distinctive_ones_stay():
    assert not affected.distinctive_literal("description")
    assert not affected.distinctive_literal("properties")
    assert not affected.distinctive_literal("sources")
    assert not affected.distinctive_literal("source")
    assert affected.distinctive_literal("page_token")
    assert affected.distinctive_literal("received_since")
    assert affected.distinctive_literal("incremental")


async def test_a_tool_schema_edit_selects_on_the_added_key_not_on_description():
    workspace = _workspace()
    filler = [
        (f"backend/tests/test_desc_{i:02d}.py", b"def test_it():\n    assert 'description' in tool()\n")
        for i in range(30)
    ]
    tree = [
        ("backend/app/mail.py", TOOL_BEFORE),
        ("backend/tests/test_mail.py", b"from app.mail import list_mail_schema\n"),
        ("backend/tests/test_paging.py", b"def test_it():\n    assert 'page_token' in body\n"),
        *filler,
    ]
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=[], files=tree
    )
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([("co/backend/app/mail.py", TOOL_AFTER)])
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree)[repo_path]

    selection = await affected.affected_tests(workspace, record, changes, cwd="co/backend", read_base=read_base)
    assert selection.tests == {
        "tests/test_mail.py": ["app.mail", "list_mail_schema"],
        "tests/test_paging.py": ["page_token"],
    }
    assert "description" not in selection.identifiers
    # The reason of each file stays in the answer, and the generic literal is named as set aside.
    text = selection.describe()
    assert "- tests/test_paging.py: page_token" in text
    assert any("set aside as too generic" in n and "description" in n for n in selection.notes)


def test_an_added_short_dict_key_selects_nothing_a_distinctive_one_does():
    before = b'def totals(row):\n    return {"amount": row.amount}\n'
    after = (
        b'def totals(row):\n    return {"amount": row.amount, "sources": row.sources, "received_since": row.since}\n'
    )
    generic: set[str] = set()
    literals: set[str] = set()
    names = affected.changed_identifiers("svc.py", before, after, generic=generic, literals=literals)
    assert "sources" not in names and "sources" in generic
    assert "received_since" in names and literals == {"received_since"}
    tests = {
        "tests/test_a.py": "assert d['sources'] == []",
        "tests/test_b.py": "assert d['received_since']",
    }
    assert affected.select_tests(tests, names - {"totals"}, quoted_only=literals) == {
        "tests/test_b.py": ["received_since"]
    }


def test_a_literal_on_both_sides_of_an_edited_description_selects_nothing():
    before = b'SCHEMA = {"received_since": {"type": "string", "description": "Old text."}}\n'
    after = b'SCHEMA = {"received_since": {"type": "string", "description": "New text."}}\n'
    literals: set[str] = set()
    names = affected.changed_identifiers("svc.py", before, after, literals=literals)
    assert "received_since" not in names and literals == set()


WORDING_BEFORE = b'''"""Budget guard of the Concierge."""


def check_budget(spent, cap):
    # The Concierge stops here.
    if spent > cap:
        raise ValueError(f"budget spent: {spent}")
    return cap - spent
'''

WORDING_AFTER = b'''"""Budget guard of Platform Health."""


def check_budget(spent, cap):
    # Platform Health stops here.
    if spent > cap:
        raise ValueError(f"budget spent: {spent}")
    return cap - spent
'''


def test_a_wording_only_change_names_the_replaced_words_not_its_definitions_or_module() -> None:
    wording: set[str] = set()
    names = affected.changed_identifiers(
        "backend/app/budget.py", WORDING_BEFORE, WORDING_AFTER, base="backend", wording=wording
    )
    assert names == {"Concierge", "Platform", "Health"} == wording
    assert affected.wording_words("budget.py", WORDING_BEFORE.decode(), WORDING_AFTER.decode()) == names
    # A code change, a non-Python file or an identical text is not wording-only.
    code = WORDING_AFTER.replace(b"spent > cap", b"spent >= cap")
    assert affected.wording_words("budget.py", WORDING_BEFORE.decode(), code.decode()) is None
    assert affected.wording_words("budget.ts", "a", "b") is None
    assert affected.wording_words("budget.py", "x = 1\n", "x = 1\n") is None
    assert "check_budget" in affected.changed_identifiers("backend/app/budget.py", WORDING_BEFORE, code, base="backend")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # A string value the code acts on is code, however short or plain.
        ('STATUS = "open"\n', 'STATUS = "closed"\n'),
        ('def f(x):\n    return x == "pending_review"\n', 'def f(x):\n    return x == "awaiting_review"\n'),
        ('import re\nPAT = re.compile(r"^[a-z]+$")\n', 'import re\nPAT = re.compile(r"^[a-z0-9]+$")\n'),
        ('def f():\n    raise ValueError("Concierge budget")\n', 'def f():\n    raise ValueError("Platform budget")\n'),
        ('def f(n):\n    return f"Concierge {n}"\n', 'def f(n):\n    return f"Platform {n}"\n'),
        ('def f(d):\n    return d.get("sources")\n', 'def f(d):\n    return d.get("sources", "none")\n'),
        # A docstring change whose replaced words are all too short to select by keeps the code path.
        ('def f():\n    """the open path."""\n    return 1\n', 'def f():\n    """the shut path."""\n    return 1\n'),
    ],
)
def test_a_string_value_or_an_unselectable_docstring_change_is_not_wording_only(old: str, new: str) -> None:
    assert affected.wording_words("svc.py", old, new) is None
    assert affected.changed_identifiers("backend/app/svc.py", old.encode(), new.encode(), base="backend")


def test_a_docstring_after_a_comment_or_in_a_class_is_wording() -> None:
    old = '# head\nclass Guard:\n    # note\n    """Concierge guard."""\n\n    LIMIT = "cap"\n'
    new = '# head\nclass Guard:\n    # note\n    """Platform guard."""\n\n    LIMIT = "cap"\n'
    assert affected.wording_words("svc.py", old, new) == {"Concierge", "Platform"}


async def test_a_wording_only_change_selects_the_test_pinning_the_text_not_the_tests_importing_the_module() -> None:
    workspace = _workspace()
    tree = [
        ("backend/app/budget.py", WORDING_BEFORE),
        (
            "backend/tests/test_budget.py",
            b"from app.budget import check_budget\n\ndef test_it():\n    assert check_budget(1, 2) == 1\n",
        ),
        ("backend/tests/test_budget_doc.py", b"def test_doc(doc):\n    assert 'Concierge' in doc\n"),
    ]
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=[], files=tree
    )
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([("co/backend/app/budget.py", WORDING_AFTER)])
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree).get(repo_path, "not checked out")

    selection = await affected.affected_tests(workspace, record, changes, cwd="co/backend", read_base=read_base)
    assert list(selection.tests) == ["tests/test_budget_doc.py"]
    assert not selection.scoped
    assert any("change wording only" in n and "backend/app/budget.py" in n for n in selection.notes)


async def test_a_new_test_file_runs_alone_its_literals_and_names_select_no_other_test():
    workspace = _workspace()
    tree = [
        ("backend/tests/test_header.py", b"def test_it():\n    assert 'Authorization' in headers('default')\n"),
        ("backend/tests/test_other.py", b"def test_other():\n    assert 'Authorization' in h and replace_default()\n"),
    ]
    record = build_record(provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=[], files=tree)
    new = b"def test_header_replace():\n    assert {'Authorization': 'x'}.get('Authorization') == replace_default('default')\n"
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([("co/backend/tests/test_new_header.py", new)])
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree).get(repo_path, "not checked out")

    selection = await affected.affected_tests(workspace, record, changes, cwd="co/backend", read_base=read_base)
    # test_other.py quotes 'Authorization' and calls replace_default, both named by the new test only: not selected.
    assert selection.tests == {"tests/test_new_header.py": ["changed"]}
    assert selection.quoted_only == set()
    assert "1 changed test file(s) run as changed; their own names select no other test file." in selection.describe()


def test_tests_failing_on_one_missing_file_fold_into_one_finding_per_path():
    from app.services.sandbox_workspace.checks import parse_junit

    cases = "".join(
        f'<testcase classname="tests.test_{i}" name="test_it" file="tests/test_{i}.py" line="3">'
        f"<failure message=\"FileNotFoundError: [Errno 2] No such file or directory: '/srv/ws/co/frontend/src/a.ts'\"/>"
        "</testcase>"
        for i in range(6)
    )
    real = '<testcase classname="tests.test_real" name="test_sum" file="tests/test_real.py" line="9"><failure message="assert 1 == 2"/></testcase>'
    summary = parse_junit(f"<testsuite>{cases}{real}</testsuite>")
    assert summary.failed == 7 and len(summary.failures) == 7
    assert len(summary.findings) == 2
    folded = [f for f in summary.findings if "a.ts" in f.message]
    assert len(folded) == 1 and "(and 5 more test(s) failing on the same missing file)" in folded[0].message
    assert any(f.message == "assert 1 == 2" for f in summary.findings)


def test_a_missing_file_line_names_the_workspace_path_it_looked_for():
    output = (
        "E   FileNotFoundError: [Errno 2] No such file or directory: '/srv/ws/main/frontend/tests_e2e/fixtures/a.json'\n"
        "Error: ENOENT: no such file or directory, open '/srv/ws/main/frontend/b.ts'\n"
        "read /srv/ws/main/backend/app/ok.py\n"
    )
    assert commands.missing_paths(output, "/srv/ws") == ["main/frontend/b.ts", "main/frontend/tests_e2e/fixtures/a.json"]
    assert commands.missing_paths("all good", "/srv/ws") == []


def test_tsc_missing_files_of_a_narrow_checkout_are_resolved_from_the_check_directory() -> None:
    """tsc prints TS2307 relative to the check's directory and TS6053 with the absolute path: both name workspace paths."""
    output = (
        "error TS6053: File '/srv/ws/co/graphql/src/generated/graphql.ts' not found.\n"
        "builder/builder.ts(8,30): error TS2307: Cannot find module './plugin/native.node.plugin.ts' or its corresponding type declarations.\n"
        "vitest.config.ci-unit.ts(1,33): error TS2307: Cannot find module './vitest.config' or its corresponding type declarations.\n"
        "src/a.ts(3,1): error TS2307: Cannot find module 'lodash' or its corresponding type declarations.\n"
        "src/b.ts(2,1): error TS2307: Cannot find module '../../../../outside.ts'.\n"
    )
    assert commands.missing_paths(output, "/srv/ws", "co/graphql") == [
        "co/graphql/builder/plugin/native.node.plugin.ts",
        "co/graphql/src/generated/graphql.ts",
        "co/graphql/vitest.config",
    ]


def test_a_narrow_checkout_names_the_paths_outside_its_sub_paths():
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=["backend"], files=[("backend/a.py", b"")]
    )
    paths = ["co/frontend/x.json", "co/backend/b.py", "elsewhere/y.txt", "co/.xtm-checkout.json", "co/frontend/x.json"]
    assert record.outside(paths) == ["frontend/x.json"]
    whole = build_record(provider="github", repository="o/r", ref="main", commit="", path="co", sub_paths=[], files=[])
    assert whole.outside(paths) == []


def test_an_affected_pytest_run_keeps_going_past_a_file_it_cannot_collect():
    flag = "--continue-on-collection-errors"
    assert commands.continue_on_collection_errors(("python", "-m", "pytest", "-q")) == (flag,)
    assert commands.continue_on_collection_errors([".venv/bin/pytest", "-x"]) == (flag,)
    assert commands.continue_on_collection_errors(("python", "-m", "pytest", flag)) == ()
    assert commands.continue_on_collection_errors(("yarn", "vitest", "run")) == ()
    assert commands.continue_on_collection_errors(("python", "scripts/pytest_wrapper.py")) == ()


# What pytest prints when a selected file reads a fixture outside a narrow checkout at import.
COLLECTION_ERROR = """\
==================================== ERRORS ====================================
_______________ ERROR collecting tests/test_agent_generated_files.py ____________
tests/test_agent_generated_files.py:525: in <module>
E   FileNotFoundError: [Errno 2] No such file or directory: '/srv/ws/co/frontend/tests_e2e/fixtures/attachment-type-cases.json'
=========================== short test summary info ============================
ERROR tests/test_agent_generated_files.py - FileNotFoundError: [Errno 2] No such file or directory
ERROR tests/test_message_pagination.py::test_page_two - AssertionError
"""


def test_a_collection_error_names_the_test_file_not_a_failing_test():
    assert commands.uncollected_files(COLLECTION_ERROR) == ["tests/test_agent_generated_files.py"]
    assert commands.uncollected_files("ERROR tests/test_a.py\n") == ["tests/test_a.py"]
    assert commands.uncollected_files("4 passed in 1.2s") == []
    assert commands.missing_paths(COLLECTION_ERROR, "/srv/ws") == ["co/frontend/tests_e2e/fixtures/attachment-type-cases.json"]


def test_the_note_gives_the_verdict_of_the_files_that_ran_without_the_uncollected_one():
    outcome = commands.CommandOutcome(
        text="",
        exit_code=2,
        uncollected=["tests/test_agent_generated_files.py"],
        report=ReportSummary(kind="junit", tests=41, passed=40, errors=1),
    )
    note = commands.uncollected_note(outcome)
    assert "Not runnable in this checkout: 1 test file(s) not collected, the rest ran: tests/test_agent_generated_files.py." in note
    assert "40 passed, 0 failed, 0 error(s): the collected tests pass" in note
    outcome.report = ReportSummary(kind="junit", tests=41, passed=39, failed=1, errors=1)
    assert "the collected tests do not pass" in commands.uncollected_note(outcome)


def test_a_failure_on_a_file_outside_the_checkout_is_left_out_of_the_verdict():
    """One test failing on a frontend file a backend checkout lacks: the verdict of the rest is green (#1111)."""
    absent = "frontend/src/components/blocks/architecture-bare.test.tsx"
    report = ReportSummary(
        kind="junit",
        tests=81,
        passed=80,
        failed=1,
        failures=[("tests.test_x::test_fixture", f"FileNotFoundError: '/tmp/workspace/co/{absent}'")],
    )
    outcome = commands.CommandOutcome(text="", exit_code=1, report=report)
    verdict = commands.missing_file_verdict(outcome, [absent])
    assert "Without the 1 test(s) failing on those files (tests.test_x::test_fixture): 80 passed, 0 failed" in verdict
    assert "the tests this checkout can run pass" in verdict
    report.failures.append(("tests.test_x::test_real", "AssertionError: 1 != 2"))
    report.failed = 2
    assert "1 other test(s) fail on the change" in commands.missing_file_verdict(outcome, [absent])
    assert commands.missing_file_verdict(outcome, []) == ""
    outcome.report = ReportSummary(kind="junit", failures=[("t", "AssertionError")])
    assert commands.missing_file_verdict(outcome, [absent]) == ""


def test_a_session_pytest_stopped_at_collection_says_nothing_ran():
    outcome = commands.CommandOutcome(
        text="", exit_code=2, uncollected=["tests/test_agent_generated_files.py"], interrupted=True
    )
    note = commands.uncollected_note(outcome)
    assert "no test ran" in note
    assert "--continue-on-collection-errors" in note


def _workspace() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=8 * 1024 * 1024, max_files=200))


async def test_affected_tests_reads_the_base_and_selects_the_tests_naming_the_change():
    workspace = _workspace()
    tree = [
        ("backend/app/tool.py", BEFORE),
        ("backend/tests/test_schema.py", b"def test_it():\n    assert 'start_frame' in build()\n"),
        ("backend/tests/test_helper.py", b"def test_it():\n    assert helper(1) == 2\n"),
        ("frontend/src/a.test.ts", b"start_frame"),
    ]
    record = build_record(provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=[], files=tree)
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([("co/backend/app/tool.py", AFTER), ("co/backend/tests/test_new.py", b"def test_new():\n    pass\n")])
    changes = changes_since(record, await workspace.entries(refresh=True))
    asked: list[str] = []

    async def read_base(repo_path: str) -> bytes | str:
        asked.append(repo_path)
        return dict(tree)[repo_path]

    selection = await affected.affected_tests(workspace, record, changes, cwd="co/backend", read_base=read_base)
    assert asked == ["backend/app/tool.py"]
    # Relative to the check's directory; the frontend test is outside it; the helper test names nothing changed.
    assert selection.tests == {"tests/test_new.py": ["changed"], "tests/test_schema.py": ["start_frame"]}
    text = selection.describe()
    assert text.startswith("Affected tests: 2 test file(s) of 3 read") and "start_frame" in text


async def test_a_changed_doc_selects_no_test_by_its_headings_only_the_code_does():
    """A heading ("API", "Decisions") selected 25 unrelated test files beside one change of code."""
    assert affected.is_prose_file("dev-docs/roi.md") and affected.is_prose_file("README.rst")
    assert not affected.is_prose_file("backend/app/tool.py") and not affected.is_prose_file("Makefile")
    workspace = _workspace()
    doc = b"# ROI outcomes\n\n## API\n\nText.\n"
    tree = [
        ("backend/app/tool.py", BEFORE),
        ("dev-docs/roi.md", doc),
        ("backend/tests/test_schema.py", b"def test_it():\n    assert 'start_frame' in build()\n"),
        ("backend/tests/test_api.py", b"def test_api():\n    API = 1\n    ROI = 2\n"),
    ]
    record = build_record(provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=[], files=tree)
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([("co/backend/app/tool.py", AFTER), ("co/dev-docs/roi.md", doc + b"\n## Decisions\n\nMore.\n")])
    changes = changes_since(record, await workspace.entries(refresh=True))
    asked: list[str] = []

    async def read_base(repo_path: str) -> bytes | str:
        asked.append(repo_path)
        return dict(tree)[repo_path]

    selection = await affected.affected_tests(workspace, record, changes, cwd="co/backend", read_base=read_base)
    assert asked == ["backend/app/tool.py"]
    assert selection.tests == {"tests/test_schema.py": ["start_frame"]}
    assert any("1 changed prose file(s) not read for identifiers" in n and "dev-docs/roi.md" in n for n in selection.notes)


async def test_an_unreadable_base_is_said_and_every_definition_counts():
    workspace = _workspace()
    tree = [("backend/app/tool.py", BEFORE), ("backend/tests/test_helper.py", b"from app.tool import build_schema\nhelper(build_schema())\n")]
    record = build_record(provider="github", repository="o/r", ref="main", commit="", path="co", sub_paths=[], files=tree)
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([("co/backend/app/tool.py", AFTER)])
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(_repo_path: str) -> bytes | str:
        return "no integration is connected to this run"

    selection = await affected.affected_tests(workspace, record, changes, cwd="co/backend", read_base=read_base)
    # Every definition counts, but the plain word ``helper`` is set aside and named.
    assert selection.tests == {"tests/test_helper.py": ["app.tool", "build_schema"]}
    assert any("set aside as too generic" in n and "helper" in n for n in selection.notes)
    assert any("Not read as checked out (1): backend/app/tool.py" in n for n in selection.notes)


async def test_the_selection_stops_reading_test_files_at_its_deadline_and_says_so():
    workspace = _workspace()
    tree = [
        ("backend/app/tool.py", BEFORE),
        ("backend/tests/test_schema.py", b"def test_it():\n    assert 'start_frame' in build()\n"),
        ("backend/tests/test_helper.py", b"def test_it():\n    assert helper(1) == 2\n"),
    ]
    record = build_record(provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=[], files=tree)
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([("co/backend/app/tool.py", AFTER)])
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree)[repo_path]

    # The deadline has passed before the first test file: none is read, the answer says how many were left.
    selection = await affected.affected_tests(
        workspace, record, changes, cwd="co/backend", read_base=read_base, deadline=10.0, clock=lambda: 11.0
    )
    assert selection.scanned == 0 and selection.unsearched == 2 and selection.tests == {}
    assert any("2 test file(s) not searched: the selection's time budget ended" in n for n in selection.notes)

    # A deadline still ahead reads them all, as without one.
    selection = await affected.affected_tests(
        workspace, record, changes, cwd="co/backend", read_base=read_base, deadline=10.0, clock=lambda: 1.0
    )
    assert selection.scanned == 2 and selection.unsearched == 0 and "tests/test_schema.py" in selection.tests


GATE = b'import { readdirSync } from "node:fs";\ntest("no accent focus ring", () => walk(readdirSync("src")));\n'
MARKED = b"// @source-scan-gate: reads every form through a helper\ntest('forms', () => scanForms());\n"
PY_WALK = b"def test_no_raw_sql():\n    for path in APP.rglob('*.py'):\n        check(path)\n"


def test_a_source_scan_gate_is_known_by_its_tree_walk_or_its_marker():
    assert affected.is_source_scan(GATE.decode()) and affected.is_source_scan(MARKED.decode())
    # A Python tree walk alone is too common in large modules: such a gate declares the marker.
    assert not affected.is_source_scan(PY_WALK.decode())
    assert affected.is_source_scan("# @source-scan-gate\n" + PY_WALK.decode())
    assert not affected.is_source_scan("test('x', () => expect(readFileSync('a.json')).toBeTruthy());")


async def _frontend_selection(changed: list[tuple[str, bytes]], always: tuple[str, ...] = ()) -> affected.Selection:
    workspace = _workspace()
    tree = [
        ("frontend/src/components/Card.tsx", b"export function Card() {\n  return <div className='p-2' />;\n}\n"),
        (
            "frontend/src/components/Card.test.tsx",
            b"import { Card } from './Card';\ntest('card', () => render(<Card />));\n",
        ),
        ("frontend/src/focusable-badge-trigger.test.ts", GATE),
        ("frontend/tests_unit/form-controls-gate.test.ts", MARKED),
        ("frontend/src/lib/other.test.ts", b"test('other', () => expect(otherThing()).toBe(1));\n"),
        ("backend/app/tool.py", BEFORE),
    ]
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=[], files=tree
    )
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([(f"co/{p}", d) for p, d in changed])
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree)[repo_path]

    return await affected.affected_tests(
        workspace, record, changes, cwd="co/frontend", read_base=read_base, always=always
    )


async def test_a_changed_tsx_selects_every_source_scan_gate_after_the_tests_naming_it():
    """A focus-visible:ring-accent added to one .tsx was caught by CI one cycle later: no name selected the gate."""
    selection = await _frontend_selection(
        [
            (
                "frontend/src/components/Card.tsx",
                b"export function Card() {\n  return <div className='focus-visible:ring-accent' />;\n}\n",
            )
        ]
    )
    assert selection.tests == {
        "src/components/Card.test.tsx": ["Card"],
        "src/focusable-badge-trigger.test.ts": [affected.SOURCE_SCAN_HIT],
        "tests_unit/form-controls-gate.test.ts": [affected.SOURCE_SCAN_HIT],
    }
    assert "2 of them source-scan gate(s)" in selection.describe()


async def test_a_backend_only_or_test_only_change_selects_no_frontend_gate():
    backend = await _frontend_selection([("backend/app/tool.py", AFTER)])
    assert backend.tests == {}
    test_only = await _frontend_selection(
        [("frontend/src/lib/other.test.ts", b"test('other', () => expect(otherThing()).toBe(2));\n")]
    )
    assert test_only.tests == {"src/lib/other.test.ts": ["changed"]}


SELECT_H8 = b"export function Card() {\n  return <SelectTrigger className='h-8' />;\n}\n"


async def test_a_ratchet_gate_the_check_names_runs_first_whatever_the_argv_bound_keeps():
    """An h-8 SelectTrigger passed the sandbox green: 65 tree-walking tests ranked the form gate past the argv bound."""
    selection = await _frontend_selection(
        [("frontend/src/components/Card.tsx", SELECT_H8)], always=("tests_unit/form-controls-gate.test.ts",)
    )
    assert next(iter(selection.tests)) == "tests_unit/form-controls-gate.test.ts"
    assert selection.tests["tests_unit/form-controls-gate.test.ts"] == [affected.ALWAYS_HIT]
    assert selection.tests["src/focusable-badge-trigger.test.ts"] == [affected.SOURCE_SCAN_HIT]
    assert "1 of them ratchet gate(s) (always)" in selection.describe()


async def test_a_ratchet_gate_needs_a_source_change_and_never_displaces_a_changed_test():
    always = ("tests_unit/*.test.ts", "src/lib/other.test.ts")
    test_only = await _frontend_selection(
        [("frontend/src/lib/other.test.ts", b"test('other', () => expect(otherThing()).toBe(2));\n")], always=always
    )
    assert test_only.tests == {"src/lib/other.test.ts": ["changed"]}
    backend = await _frontend_selection([("backend/app/tool.py", AFTER)], always=always)
    assert backend.tests == {}


def test_select_tests_ranks_the_always_gates_after_the_changed_tests_and_before_the_named_hits():
    tests = {
        "a/named.test.ts": "Card Card",
        "a/changed.test.ts": "",
        "z/gate.test.ts": "no name here",
        "b/walk.test.ts": "readdirSync(src)",
    }
    picked = affected.select_tests(
        tests, {"Card"}, changed=["a/changed.test.ts"], source_changed=True, always=("z/*.test.ts",)
    )
    assert list(picked) == ["a/changed.test.ts", "z/gate.test.ts", "a/named.test.ts", "b/walk.test.ts"]
    assert affected.select_tests(tests, {"Card"}, always=("z/*.test.ts",)) == {"a/named.test.ts": ["Card"]}


def test_a_check_scan_pattern_selects_a_python_tripwire_on_a_source_change_only() -> None:
    """A guard reading every module through a helper names no changed identifier: #659 went red in the queue."""
    tests = {
        "tests/test_guard.py": "from tests import app_sources\n\ndef test_x():\n    app_sources.modules()\n",
        "tests/test_walk.py": "from tests.app_sources import modules, walk\n\ndef test_z():\n    walk(modules())\n",
        "tests/test_other.py": "def test_y():\n    assert 1\n",
    }
    scans = (r"\bfrom tests(?:\.app_sources\s+import\b|\s+import\s+[^\n]*\bapp_sources\b)",)
    picked = affected.select_tests(tests, set(), source_changed=True, scans=scans)
    assert picked == {
        "tests/test_guard.py": [affected.SOURCE_SCAN_HIT],
        "tests/test_walk.py": [affected.SOURCE_SCAN_HIT],
    }
    assert affected.select_tests(tests, set(), source_changed=False, scans=scans) == {}
    assert affected.select_tests(tests, set(), source_changed=True) == {}


def test_the_markers_a_check_deselects_are_read_from_its_argv() -> None:
    unit = ("python", "-m", "pytest", "-m", "not integration", "-q")
    assert affected.excluded_markers(unit) == ("integration",)
    assert affected.excluded_markers(("pytest", "-m", "not slow and not integration")) == ("slow", "integration")
    assert affected.excluded_markers(("pytest", "-mnot slow")) == ("slow",)
    # A selecting or mixed expression deselects nothing this reader can name.
    assert affected.excluded_markers(("pytest", "-m", "integration")) == ()
    assert affected.excluded_markers(("pytest", "-m", "not slow or fast")) == ()
    assert affected.excluded_markers(("python", "-m", "scripts.queue_tests")) == ()


async def test_a_selected_file_marked_with_a_deselected_marker_is_named_not_silently_skipped() -> None:
    """#778 left the queue red: the postgres test on the changed key was selected and ran nothing."""
    workspace = _workspace()
    tree = [
        ("backend/app/tool.py", BEFORE),
        (
            "backend/tests/test_tool_postgres.py",
            b"import pytest\n\npytestmark = pytest.mark.integration\n\ndef test_x():\n    assert 'start_frame'\n",
        ),
        ("backend/tests/test_tool.py", b"def test_y():\n    assert 'start_frame'\n"),
    ]
    record = build_record(
        provider="github", repository="o/r", ref="main", commit="c" * 40, path="co", sub_paths=["backend"], files=tree
    )
    await workspace.put_tree([(f"co/{p}", d) for p, d in tree] + [(record.record_path, record.to_json())])
    await workspace.put_tree([("co/backend/app/tool.py", AFTER)])
    changes = changes_since(record, await workspace.entries(refresh=True))

    async def read_base(repo_path: str) -> bytes | str:
        return dict(tree)[repo_path]

    selection = await affected.affected_tests(
        workspace, record, changes, cwd="co/backend", read_base=read_base, excluded=("integration",)
    )
    assert set(selection.tests) == {"tests/test_tool_postgres.py", "tests/test_tool.py"}
    assert selection.deselected == {"tests/test_tool_postgres.py": ["integration"]}
    text = selection.describe()
    assert "Deselected by this check's -m: 1 selected test file(s) are marked integration" in text
    assert "tests/test_tool_postgres.py" in text
    plain = await affected.affected_tests(workspace, record, changes, cwd="co/backend", read_base=read_base)
    assert plain.deselected == {}
    # Without a check of the directory selecting the marker, the advice stays generic.
    assert "the check that selects that marker" in text
    selection.runners = {"integration": ["backend-integration"]}
    named = selection.describe()
    assert "Run them with backend-integration (integration) (args: these files) before the push." in named
    assert "the check that selects that marker" not in named


def test_the_markers_a_check_selects_are_read_from_its_argv() -> None:
    integration = ("python", "../.xtm-one/stores.py", "-m", "integration", "-q")
    assert affected.selected_markers(integration) == ("integration",)
    assert affected.selected_markers(("python", "-m", "pytest", "-m", "slow or integration")) == ("slow", "integration")
    # `python -m pytest` names a module, never a marker; a `not` expression selects nothing.
    assert affected.selected_markers(("python", "-m", "pytest", "-q")) == ()
    assert affected.selected_markers(("python", "-m", "pytest", "-m", "not integration")) == ()
    assert affected.selected_markers(("pytest", "-m", "integration and not slow")) == ()


def test_the_check_running_a_deselected_marker_is_found_in_the_same_directory() -> None:
    checks = [
        ("backend-unit", ("python", "-m", "pytest", "-m", "not integration"), "co/backend"),
        ("backend-integration", ("python", "../.xtm-one/stores.py", "-m", "integration"), "co/backend"),
        ("other-integration", ("pytest", "-m", "integration"), "co/worker"),
    ]
    assert affected.marker_runners(checks, "co/backend", ["integration"]) == {"integration": ["backend-integration"]}
    assert affected.marker_runners(checks, "co/backend", ["slow"]) == {}
