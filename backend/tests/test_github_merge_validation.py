"""Merge paths that report clean but would break the code (#11708).

Each case of the merge-and-update-branch family, replayed on the pure merge
functions and on the tools against a fixture repository:

- a line merge without an overlap that holds an addition both sides made twice;
- a JSON file (an i18n catalog) both sides hold valid that the merge would break;
- ``keep: both`` resurrecting a base line one side deleted, or writing a shared addition twice;
- a given resolution still holding conflict markers, or a ``.json`` content that does not parse;
- more than 100 resolutions in one call;
- the outcome line, read from the branch, on an error answer;
- a rebase whose ref widened the sub_paths, never reading the new files as deletions.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.integrations.providers.github.merge_hunks import merge_path
from app.integrations.providers.github.merge_resolution import parse_resolutions
from app.services.sandbox_workspace.checkout import CheckoutRecord, RecordedFile
from app.services.sandbox_workspace.rebase import Step, base_files_of, merge_text, plan
from tests.test_github_merge_hunks import _SAFE_REQUEST, FakeGitHub, _tool

# ---------------------------------------------------------------------------
# keep 'both' (the union merge)
# ---------------------------------------------------------------------------


def test_keep_both_writes_each_side_s_whole_hunk() -> None:
    # #12179: the branch's hunk then the ref's hunk, every line of each; nothing deduplicated or dropped.
    base = "a\nold\nc\n"
    ours = "a\nmine\nc\n"
    theirs = "a\nold\ntheirs\nc\n"
    merged = merge_text(base, ours, theirs, both=True)
    assert merged.conflicts == 1
    assert merged.text == "a\nmine\nold\ntheirs\nc\n"


def test_keep_both_keeps_a_line_both_hunks_hold_in_each() -> None:
    base = "a\nb\nc\n"
    ours = "a\nB\nshared()\nc\n"
    theirs = "a\nb2\nshared()\nc\n"
    merged = merge_text(base, ours, theirs, both=True)
    assert merged.text == "a\nB\nshared()\nb2\nshared()\nc\n"


_PY_BASE = "def f():\n    x = 1\n    return x\n\n\ndef g():\n    return 2\n"
_PY_OURS = "def f():\n    with open('a') as fh:\n        x = fh.read()\n\n    return x\n\n\ndef g():\n    return 2\n"
_PY_THEIRS = (
    "def f():\n    with lock:\n        x = compute()\n        y = 2\n\n    return x + y\n\n\ndef g():\n    return 2\n"
)


def test_keep_both_keeps_the_common_lines_of_an_overlapping_python_hunk() -> None:
    # The connectors #8393 case: blank lines and with-bodies both hunks hold were dropped from the ref's hunk.
    merged = merge_text(_PY_BASE, _PY_OURS, _PY_THEIRS, both=True)
    assert merged.conflicts == 1
    lines = merged.text.splitlines()
    assert lines.count("") == _PY_OURS.splitlines().count("") + _PY_THEIRS.splitlines().count("") - 2
    for line in ("    with open('a') as fh:", "        x = fh.read()", "    with lock:", "        x = compute()"):
        assert line in lines
    assert merged.text.endswith("def g():\n    return 2\n")


def test_a_keep_both_union_that_breaks_python_is_refused_with_its_line() -> None:
    from app.services.sandbox_workspace import structured

    why = structured.resolution_defect("src/a.py", "def f():\n    if x:\n", _PY_OURS, _PY_THEIRS)
    assert why.startswith("not valid Python at line 2")
    assert structured.resolution_defect("src/a.py", _PY_OURS, _PY_OURS, _PY_THEIRS) == ""
    # A side already broken is not the resolution's doing.
    assert structured.resolution_defect("src/a.py", "def (\n", "def (\n", _PY_THEIRS) == ""
    assert structured.resolution_defect("README.md", "def (\n", _PY_OURS) == ""


def test_keep_both_keeps_the_plain_union_when_nothing_was_deleted() -> None:
    assert merge_text("a\nb\nc\n", "a\nB\nc\n", "a\nb2\nc\n", both=True).text == "a\nB\nb2\nc\n"


# ---------------------------------------------------------------------------
# merge_path: a clean merge that breaks the file is a conflict
# ---------------------------------------------------------------------------


def test_an_addition_both_sides_made_in_different_places_is_refused_with_its_reason() -> None:
    base = "".join(f"line {n}\n" for n in range(1, 11))
    ours = base.replace("line 2\n", "line 2\nimport shared_helper\n")
    theirs = base.replace("line 8\n", "line 8\nimport shared_helper\n")
    merge = merge_path("src/app.py", base.encode(), ours.encode(), theirs.encode())
    assert not merge.clean
    assert "written twice" in merge.reason and "import shared_helper" in merge.reason


def test_a_line_one_side_already_held_twice_is_not_a_duplicate() -> None:
    base = "x\n"
    ours = "x\nreturn value\nreturn value\n"
    theirs = "x\nreturn value\nreturn value\n"
    merge = merge_path("a.py", base.encode(), ours.encode(), theirs.encode())
    assert merge.clean


def test_a_recurring_line_each_side_adds_in_its_own_block_is_not_a_duplicate() -> None:
    # xtm-one #1054 against main: two provider blocks of integration_catalog.py each opened with
    # '"available_events": [', a line the base already held in other blocks; the merge is clean.
    block = (
        '    {{\n        "name": "{0}",\n        "available_events": [\n            "{0}_event",\n        ],\n    }},\n'
    )
    base = "CATALOG = [\n" + block.format("github") + block.format("slack") + "]\n"
    ours = base.replace(block.format("github"), block.format("github") + block.format("netsuite"))
    theirs = base.replace(block.format("slack"), block.format("slack") + block.format("airwallex"))
    merge = merge_path("backend/app/api/v1/integration_catalog.py", base.encode(), ours.encode(), theirs.encode())
    assert merge.clean, merge.reason
    assert merge.merged is not None and merge.merged.count('"available_events": [') == 4


def test_a_json_catalog_the_line_merge_would_break_is_never_written_broken() -> None:
    base = '{\n  "a": "A",\n  "z": "Z"\n}\n'
    ours = '{\n  "a": "A",\n  "b": "B",\n  "z": "Z"\n}\n'
    theirs = '{\n  "a": "A",\n  "z": "Z",\n  "zz": "ZZ"\n}\n'
    merge = merge_path("frontend/src/i18n/lang/en.json", base.encode(), ours.encode(), theirs.encode())
    if merge.merged is not None:
        assert json.loads(merge.merged) == {"a": "A", "b": "B", "z": "Z", "zz": "ZZ"}
    else:
        assert "is not: " in merge.reason


def test_keep_both_on_json_that_would_not_parse_is_refused() -> None:
    merge = merge_path("x.json", b'{"a": 1}\n', b'{"a": 2}\n', b'{"a": 3}\n')
    assert not merge.clean and merge.union is not None
    assert merge.defect(merge.union).startswith("both sides are valid JSON but the merged text is not: not valid JSON")


# ---------------------------------------------------------------------------
# resolutions
# ---------------------------------------------------------------------------


def test_more_than_100_resolutions_are_accepted() -> None:
    raw = [{"path": f"src/f{n}.py", "keep": "ref"} for n in range(150)]
    parsed, err = parse_resolutions(raw)
    assert err is None and len(parsed) == 150


def test_resolutions_over_the_split_cap_are_refused() -> None:
    raw = [{"path": f"src/f{n}.py", "keep": "ref"} for n in range(801)]
    _parsed, err = parse_resolutions(raw)
    assert err is not None and "801 paths" in err


def _conflicting() -> FakeGitHub:
    base = {"src/a.py": "a\nb\nc\n", "lang/en.json": '{"k": "v"}\n'}
    ours = {"src/a.py": "a\nB\nc\n", "lang/en.json": '{"k": "ours"}\n'}
    theirs = {"src/a.py": "a\nb2\nc\n", "lang/en.json": '{"k": "theirs"}\n'}
    return FakeGitHub(base, ours, theirs)


@pytest.mark.asyncio
async def test_a_content_still_holding_markers_is_refused_before_any_write() -> None:
    fake = _conflicting()
    marked = "a\n<<<<<<< ours\nB\n=======\nb2\n>>>>>>> theirs\nc\n"
    with patch(_SAFE_REQUEST, new=fake):
        refused = await _tool("merge_github_branch")(
            repo="acme/app",
            branch="feat/x",
            resolutions=[{"path": "src/a.py", "content": marked}, {"path": "lang/en.json", "keep": "branch"}],
        )
    assert "would break" in refused and "src/a.py" in refused and "conflict hunk" in refused
    assert fake.commits == [] and not fake.merged
    assert "Outcome: landed false" in refused


@pytest.mark.asyncio
async def test_a_json_content_that_does_not_parse_is_refused_with_its_path() -> None:
    fake = _conflicting()
    with patch(_SAFE_REQUEST, new=fake):
        refused = await _tool("merge_github_branch")(
            repo="acme/app",
            branch="feat/x",
            resolutions=[
                {"path": "src/a.py", "keep": "branch"},
                {"path": "lang/en.json", "content": '{"k": "ours",}\n'},
            ],
        )
    assert "lang/en.json" in refused and "does not parse" in refused
    assert fake.commits == [] and not fake.merged


@pytest.mark.asyncio
async def test_a_python_content_that_does_not_parse_is_refused_with_its_line() -> None:
    # #12179: a resolved Python file both sides hold valid is parsed before any write.
    fake = _conflicting()
    with patch(_SAFE_REQUEST, new=fake):
        refused = await _tool("merge_github_branch")(
            repo="acme/app",
            branch="feat/x",
            resolutions=[
                {"path": "src/a.py", "content": "def f(:\n    pass\n"},
                {"path": "lang/en.json", "keep": "branch"},
            ],
        )
    assert "src/a.py" in refused and "not valid Python at line 1" in refused, refused
    assert fake.commits == [] and not fake.merged


@pytest.mark.asyncio
async def test_a_given_resolution_is_written_as_given() -> None:
    fake = _conflicting()
    with patch(_SAFE_REQUEST, new=fake):
        landed = await _tool("merge_github_branch")(
            repo="acme/app",
            branch="feat/x",
            resolutions=[
                {"path": "src/a.py", "content": "a\nmine and theirs\nc\n"},
                {"path": "lang/en.json", "content": '{"k": "both"}\n'},
            ],
        )
    assert landed.startswith("Merged 'main' into 'feat/x'"), landed
    assert fake.sides["ours"]["src/a.py"] == "a\nmine and theirs\nc\n"
    assert fake.sides["ours"]["lang/en.json"] == '{"k": "both"}\n'


@pytest.mark.asyncio
async def test_update_branch_error_names_the_outcome_read_from_the_branch() -> None:
    fake = _conflicting()
    with patch(_SAFE_REQUEST, new=fake):
        refused = await _tool("manage_github_pr")(repo="acme/app", number=7, action="update_branch")
    assert refused.startswith("Error: the branch of PR #7")
    assert "Outcome: landed false: 'feat/x' head is still" in refused


# ---------------------------------------------------------------------------
# sandbox_checkout_rebase: widened sub_paths are not deletions
# ---------------------------------------------------------------------------


def test_a_path_only_the_widened_sub_paths_cover_is_taken_from_the_ref() -> None:
    recorded = RecordedFile(sha256="0" * 64, size=1)
    checked_out = CheckoutRecord(
        "github", "acme/app", "feat/x", "c" * 40, "app", ("backend/src",), {"backend/src/a.py": recorded}
    )
    widened = CheckoutRecord(
        "github", "acme/app", "feat/x", "c" * 40, "app", ("backend",), {"backend/src/a.py": recorded}
    )
    base = {"backend/src/a.py": b"a\n", "backend/pyproject.toml": b"[tool]\n"}
    files = base_files_of(widened, base, checked_out=checked_out)
    assert "backend/pyproject.toml" not in files
    steps = plan(
        CheckoutRecord("github", "acme/app", "feat/x", "c" * 40, "app", ("backend",), files),
        {"backend/src/a.py": files["backend/src/a.py"].sha256},
        {"backend/src/a.py": b"a\n", "backend/pyproject.toml": b"[tool]\n"},
        in_scope=widened.covers,
    )
    assert [(step.path, step.step) for step in steps] == [("backend/pyproject.toml", Step.TAKE)]


def test_without_a_widening_the_base_covers_every_sub_path() -> None:
    record = CheckoutRecord("github", "acme/app", "feat/x", "c" * 40, "app", ("backend",), {})
    files = base_files_of(record, {"backend/a.py": b"a\n", "docs/b.md": b"b\n"})
    assert set(files) == {"backend/a.py", "docs/b.md"}
