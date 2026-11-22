# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""A checkout rebase never answers "merged cleanly" over a block it wrote twice (#1171)."""

from __future__ import annotations

from app.services.sandbox_workspace.rebase import duplicated_block, merge_file, merge_text

BASE = """PROVIDERS = {
    "github": "GitHub",
    "jira": "Jira",
}


def other():
    return 1
"""

BLOCK = """    "netsuite": {
        "label": "NetSuite",
        "kind": "erp",
    },
"""

# The branch added the block at the end of the dict; the ref (main) squashed the same change next to "github".
OURS = BASE.replace('    "jira": "Jira",\n', '    "jira": "Jira",\n' + BLOCK)
THEIRS = BASE.replace('    "github": "GitHub",\n', '    "github": "GitHub",\n' + BLOCK).replace("return 1", "return 2")


def test_the_line_merge_writes_the_block_twice_without_an_overlap() -> None:
    merged = merge_text(BASE, OURS, THEIRS)
    assert merged.conflicts == 0
    assert merged.text.count('"netsuite"') == 2


def test_duplicated_block_names_the_line_of_the_copy() -> None:
    merged = merge_text(BASE, OURS, THEIRS).text
    line = duplicated_block(OURS, THEIRS, merged)
    assert line > 0
    assert merged.splitlines()[line - 1].strip().startswith('"netsuite"') or '"label"' in merged.splitlines()[line - 1]


def test_merge_file_reports_the_copy_and_keeps_the_workspace() -> None:
    result = merge_file(BASE.encode(), OURS.encode(), THEIRS.encode(), path="backend/app/providers.py")
    assert result.conflicts == 1
    assert result.data == OURS.encode()
    assert "writes a block twice" in result.reason


def test_a_clean_merge_of_distinct_changes_stays_clean() -> None:
    ours = BASE.replace('    "jira": "Jira",\n', '    "jira": "Jira",\n    "notion": "Notion",\n')
    result = merge_file(BASE.encode(), ours.encode(), THEIRS.replace(BLOCK, "").encode(), path="a.py")
    assert result.conflicts == 0
    assert b"return 2" in result.data and b'"notion"' in result.data


def test_a_block_one_side_already_holds_twice_is_not_a_copy_of_the_merge() -> None:
    ours = BASE + "\n" + BLOCK + "\n" + BLOCK
    # The ref edits a line far from the workspace's end-of-file blocks, so the merge is clean.
    theirs = BASE.replace('"github": "GitHub"', '"github": "GitHub.com"')
    merge = merge_text(BASE, ours, theirs)
    assert merge.conflicts == 0
    assert duplicated_block(ours, theirs, merge.text) == 0


def test_a_run_where_two_hunks_meet_is_not_a_copy() -> None:
    # Each side changes one line; the merged run 'A b C' is held by neither side, and is no copy.
    merge = merge_text("a\nb\nc\n", "A\nb\nc\n", "a\nb\nC\n")
    assert merge.conflicts == 0 and merge.text == "A\nb\nC\n"
    assert duplicated_block("A\nb\nc\n", "a\nb\nC\n", merge.text) == 0


def test_braces_and_blank_lines_alone_are_not_a_block() -> None:
    assert duplicated_block("a\n", "b\n", "}\n)\n\n}\n)\n\n") == 0
