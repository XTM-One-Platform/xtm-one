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

"""The checkout record carries no formatter-parsable name, and a legacy-named record still works (#1804)."""

from __future__ import annotations

import pytest

from app.services.frictions import checkout_source_writes
from app.services.sandbox_workspace.checkout import (
    LEGACY_RECORD_NAME,
    RECORD_NAME,
    build_record,
    held_record_path,
    is_record_name,
    read_record_bytes,
    record_root,
    write_record,
)
from app.services.sandbox_workspace.commands import keep_vanished_checkouts
from app.services.sandbox_workspace.eviction import checkout_roots


def test_record_name_has_no_extension_a_formatter_infers_a_parser_from():
    assert RECORD_NAME == ".xtm-checkout"
    assert "." not in RECORD_NAME.lstrip(".")
    assert LEGACY_RECORD_NAME == ".xtm-checkout.json"


def test_both_names_are_records():
    assert is_record_name("co/.xtm-checkout") and is_record_name("co/.xtm-checkout.json")
    assert is_record_name(".xtm-checkout")
    assert not is_record_name("co/xtm-checkout.json") and not is_record_name("co/.xtm-checkout.jsonl")
    assert record_root("a/b/.xtm-checkout") == "a/b"
    assert record_root("a/.xtm-checkout.json") == "a"
    assert record_root(".xtm-checkout") == ""
    assert record_root("a/b.py") is None


def test_checkout_roots_reads_both_names_once():
    entries = ["new/.xtm-checkout", "old/.xtm-checkout.json", "both/.xtm-checkout", "both/.xtm-checkout.json", "x/a.py"]
    assert checkout_roots(entries) == ["both", "new", "old"]


def test_held_record_path_prefers_the_current_name():
    assert held_record_path({"co/.xtm-checkout": 1, "co/.xtm-checkout.json": 1}, "co") == "co/.xtm-checkout"
    assert held_record_path({"co/.xtm-checkout.json": 1}, "co") == "co/.xtm-checkout.json"
    assert held_record_path({"co/a.py": 1}, "co") is None


def test_vanished_legacy_checkout_is_still_kept():
    removals = ["old/.xtm-checkout.json", "old/a.py", "other/b.py"]
    kept, vanished = keep_vanished_checkouts(removals)
    assert vanished == ["old"]
    assert kept == ["other/b.py"]


def test_friction_source_writes_skip_both_record_names():
    found = checkout_source_writes(
        ["new/a.py", "new/.xtm-checkout", "old/b.py", "old/.xtm-checkout.json"],
        ["new/.xtm-checkout", "old/.xtm-checkout.json", "new/a.py", "old/b.py"],
    )
    assert found == {"new": ["new/a.py"], "old": ["old/b.py"]}


class _Session:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = dict(files)
        self.calls: list[tuple[list[str], list[str]]] = []

    async def get_bytes(self, path: str):
        if path not in self.files:
            raise FileNotFoundError(path)
        return object(), self.files[path]

    async def put_tree(self, files, *, under: str = "", replace=(), remove=(), operation: str = "put"):
        written = [path for path, _data in files]
        dropped = [path for path in remove if path in self.files]
        for path, data in files:
            self.files[path] = data
        for path in dropped:
            self.files.pop(path, None)
        self.calls.append((written, dropped))
        return None


def _record(path: str = "co"):
    return build_record(
        provider="github", repository="o/r", ref="main", commit="a" * 40, path=path, sub_paths=(), files=[]
    )


@pytest.mark.asyncio
async def test_read_falls_back_to_the_legacy_name():
    data = _record().to_json()
    _entry, got = await read_record_bytes(_Session({"co/.xtm-checkout.json": data}), "co")
    assert got == data
    with pytest.raises(FileNotFoundError):
        await read_record_bytes(_Session({}), "co")


@pytest.mark.asyncio
async def test_write_moves_a_legacy_record_to_the_new_name():
    record = _record()
    session = _Session({"co/.xtm-checkout.json": record.to_json()})
    await write_record(session, record, operation="checkout_advance")
    assert "co/.xtm-checkout" in session.files
    assert "co/.xtm-checkout.json" not in session.files
    assert session.calls == [(["co/.xtm-checkout"], ["co/.xtm-checkout.json"])]
