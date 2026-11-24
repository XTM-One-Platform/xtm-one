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

"""A checkout's gitignored build output stays out of a check's read-back (#1639)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from app.services.sandbox_workspace import commands
from app.services.sandbox_workspace.checkout import RECORD_VERSION
from app.services.sandbox_workspace.gitignore import build_output_patterns
from app.services.sandbox_workspace.remote import TOOL_SRC

OPENCTI_ROOT = b"node_modules/\n.yarn/\n.nx/cache\n# Build\nbuild\ndist\n__generated__\n/licenses/\n*.log\n"


def test_plain_rules_become_directory_patterns() -> None:
    patterns = build_output_patterns("oc", [(".gitignore", OPENCTI_ROOT)], tracked=["opencti-front/src/a.ts"])
    assert "oc/build/" in patterns and "oc/*/build/" in patterns
    assert "oc/*/__generated__/" in patterns
    assert "oc/licenses/" in patterns and "oc/*/licenses/" not in patterns
    assert "oc/.nx/cache/" in patterns
    assert not any("*.log" in p for p in patterns)


def test_nested_gitignore_is_anchored_to_its_directory() -> None:
    patterns = build_output_patterns("oc", [("front/.gitignore", b"/playwright-report/\ncoverage\n")], tracked=[])
    assert patterns == ["oc/front/*/coverage/", "oc/front/coverage/", "oc/front/playwright-report/"]


def test_a_rule_covering_a_tracked_file_is_dropped() -> None:
    patterns = build_output_patterns("", [(".gitignore", b"build\nlogs\n")], tracked=["tools/build/run.sh"])
    assert "*/build/" not in patterns and "build/" in patterns
    assert "logs/" in patterns


def test_a_file_with_a_reinclude_gives_nothing() -> None:
    assert build_output_patterns("oc", [(".gitignore", b"build\n!build/keep\n")], tracked=[]) == []


def test_parent_segments_are_never_patterns() -> None:
    assert build_output_patterns("oc", [(".gitignore", b"../x\n./y\n..\n")], tracked=[]) == []


class _Entry:
    def __init__(self, path: str, data: bytes) -> None:
        self.path = path
        self.data = data


class _Workspace:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    async def read_entry(self, entry: _Entry) -> tuple[_Entry, bytes]:
        return entry, self.files[entry.path]


@pytest.mark.asyncio
async def test_build_output_ignores_reads_the_staged_checkout() -> None:
    record = {
        "version": RECORD_VERSION,
        "provider": "github",
        "repository": "o/r",
        "ref": "main",
        "commit": "",
        "path": "oc",
        "files": {".gitignore": {"sha256": "x", "size": 1}, "src/a.ts": {"sha256": "y", "size": 1}},
    }
    files = {"oc/.xtm-checkout.json": json.dumps(record).encode(), "oc/.gitignore": b"build\n", "oc/src/a.ts": b"x"}
    entries = {path: _Entry(path, data) for path, data in files.items()}
    ignore = await commands.build_output_ignores(_Workspace(files), entries, ["oc"])
    assert ignore[: len(commands.IGNORED_PATTERNS)] == list(commands.IGNORED_PATTERNS)
    assert "oc/build/" in ignore and "oc/*/build/" in ignore
    assert await commands.build_output_ignores(_Workspace(files), entries, []) == list(commands.IGNORED_PATTERNS)


def test_listing_skips_build_output_past_the_file_limit(tmp_path) -> None:
    root = tmp_path / "ws"
    build = root / "oc" / "front" / "build"
    build.mkdir(parents=True)
    for i in range(30):
        (build / f"f{i}.js").write_text("x")
    (root / "oc" / "front" / "a.ts").write_text("edited")
    tool = tmp_path / "tool.py"
    tool.write_text(TOOL_SRC)
    snapshot = tmp_path / "snap.json"
    snapshot.write_text("{}")
    out = tmp_path / "out.json"
    ignore_file = tmp_path / "ignore.json"
    patterns = list(commands.IGNORED_PATTERNS) + build_output_patterns("oc", [(".gitignore", b"build\n")], tracked=[])

    def listing(pats: list[str]) -> dict:
        ignore_file.write_text(json.dumps(pats))
        subprocess.run(  # noqa: S603 - the test's own helper copy and temp paths
            [sys.executable, str(tool), "list", str(root), str(snapshot), str(out), "10", "0", str(ignore_file)],
            check=True,
        )
        return json.loads(out.read_text())

    assert listing(list(commands.IGNORED_PATTERNS))["overflow"] is True
    kept = listing(patterns)
    assert kept["overflow"] is False
    assert [row[0] for row in kept["changed"]] == ["oc/front/a.ts"]
    assert os.path.isdir(build)
