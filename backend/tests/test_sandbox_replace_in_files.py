"""sandbox_replace_in_files: a regular-expression replace across workspace files, all or nothing."""

from __future__ import annotations

import asyncio
import re
import uuid

import pytest

from app.services.sandbox_workspace import replacing
from tests.test_sandbox_workspace_tools import _call, _composite, _put, _workspace_bytes, store  # noqa: F401


def test_plan_counts_per_file_and_skips_unchanged():
    planned = replacing.plan(
        [("a.py", "old_name()\nold_name()\n"), ("b.py", "other\n"), ("c.py", "x = old_name\n")],
        re.compile(r"\bold_name\b"),
        "new_name",
        cap=10,
    )
    assert [(c.path, c.replacements) for c in planned.changes] == [("a.py", 2), ("c.py", 1)]
    assert planned.files_scanned == 3 and planned.replacements == 3
    assert planned.changes[0].after == "new_name()\nnew_name()\n"


def test_plan_expands_groups_and_refuses_a_missing_group():
    planned = replacing.plan(
        [("a.py", "from x import y\n")], re.compile(r"from (\w+) import"), r"from pkg.\1 import", cap=5
    )
    assert planned.changes[0].after == "from pkg.x import y\n"
    with pytest.raises(replacing.ReplaceError, match="cannot expand"):
        replacing.plan([("a.py", "abc\n")], re.compile("b"), r"\2", cap=5)


def test_plan_refuses_past_the_cap():
    with pytest.raises(replacing.ReplaceError, match="more than 2 replacement"):
        replacing.plan([("a.py", "x x x\n")], re.compile("x"), "y", cap=2)


def test_clamp_and_template():
    assert replacing.clamp_max(None) == replacing.DEFAULT_MAX_REPLACEMENTS
    assert replacing.clamp_max(10**9) == replacing.MAX_REPLACEMENTS
    assert replacing.clamp_max(0) == 1
    assert replacing.check_template("") == ""
    with pytest.raises(replacing.ReplaceError):
        replacing.check_template(3)


async def test_replace_in_files_writes_every_file_in_one_write(store):  # noqa: F811
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "repo/src/a.py", b"def old_name():\n    return 1\n")
    await _put(composite, "repo/src/b.py", b"from a import old_name\nold_name()\n")
    await _put(composite, "repo/docs/c.md", b"old_name is documented\n")
    preview = await _call(
        composite,
        "sandbox_replace_in_files",
        pattern=r"\bold_name\b",
        replacement="new_name",
        path_prefix="repo/src",
        dry_run=True,
    )
    assert not preview.is_error, preview.content
    assert preview.content.startswith("Dry run, nothing written: 3 replacement(s) in 2 file(s)")
    assert "-def old_name():\n+def new_name():\n" in preview.content
    assert await _workspace_bytes(store, conversation, "repo/src/b.py") == b"from a import old_name\nold_name()\n"
    done = await _call(
        composite, "sandbox_replace_in_files", pattern=r"\bold_name\b", replacement="new_name", path_prefix="repo/src"
    )
    assert not done.is_error, done.content
    assert (
        "3 replacement(s) in 2 file(s)" in done.content
        and "workspace/repo/src/b.py: 2 replacement(s), +2 -2" in done.content
    )
    # A write answers each distinct line change once, never the per-site diff.
    # b.py's two adjacent changed lines are one change; a.py's def is the other.
    assert "Distinct line changes: 2, most frequent first" in done.content
    assert "1x in 1 file(s):\n  - def old_name():\n  + def new_name():" in done.content
    assert "+++ b/" not in done.content
    assert await _workspace_bytes(store, conversation, "repo/src/b.py") == b"from a import new_name\nnew_name()\n"
    assert await _workspace_bytes(store, conversation, "repo/docs/c.md") == b"old_name is documented\n"


async def test_replace_in_files_refusals_write_nothing(store):  # noqa: F811
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "r/a.py", b"x x x\n")
    unscoped = await _call(composite, "sandbox_replace_in_files", pattern="x", replacement="y")
    assert unscoped.is_error and "path_prefix or include_globs" in unscoped.content
    capped = await _call(
        composite, "sandbox_replace_in_files", pattern="x", replacement="y", include_globs=["*.py"], max_replacements=2
    )
    assert capped.is_error and "Nothing was written" in capped.content
    bad = await _call(composite, "sandbox_replace_in_files", pattern="(", replacement="y", path_prefix="r")
    assert bad.is_error and "does not compile" in bad.content
    none = await _call(composite, "sandbox_replace_in_files", pattern="zzz", replacement="y", path_prefix="r")
    assert not none.is_error and none.content.startswith("No match")
    assert await _workspace_bytes(store, conversation, "r/a.py") == b"x x x\n"


async def test_two_writes_of_one_round_on_one_file_both_land(store):  # noqa: F811
    """The agent loop gathers the calls of one response: no write may drop another's change."""
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "r/a.py", b"alpha = 1\nbeta = 2\n")
    first, second, third = await asyncio.gather(
        _call(composite, "sandbox_replace_in_files", pattern="alpha", replacement="ALPHA", path_prefix="r"),
        _call(composite, "sandbox_replace_in_files", pattern="beta", replacement="BETA", path_prefix="r"),
        _call(composite, "sandbox_edit_file", path="r/a.py", edits=[{"find": "= 2", "replace": "= 3"}]),
    )
    for result in (first, second, third):
        assert not result.is_error, result.content
    assert await _workspace_bytes(store, conversation, "r/a.py") == b"ALPHA = 1\nBETA = 3\n"


def test_template_expands_character_escapes():
    assert replacing.check_template(r"\1\u00a0:") == "\\1\u00a0:"
    assert replacing.check_template(r"\x41\U0001F600") == "A\U0001f600"
    # An escaped backslash stays a literal backslash followed by the letters.
    assert replacing.check_template(r"\\u00a0") == r"\\u00a0"
    # A backslash written as an escape stays one backslash in the expanded text.
    planned = replacing.plan([("a.txt", "x\n")], re.compile("x"), replacing.check_template(r"\u005c"), cap=5)
    assert planned.changes[0].after == "\\\n"
    with pytest.raises(replacing.ReplaceError, match="code point"):
        replacing.check_template(r"\UFFFFFFFF")


def test_plan_counts_matches_the_replacement_leaves_unchanged():
    planned = replacing.plan([("a.txt", "same same\n"), ("b.txt", "other\n")], re.compile("same"), "same", cap=5)
    assert planned.changes == [] and planned.unchanged == [("a.txt", 2)]


async def test_replace_in_files_nbsp_in_a_large_json_file(store):  # noqa: F811
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    filler = "".join(f'  "key {i}": "valeur {i}",\n' for i in range(80_000))
    body = "{\n" + filler + '  "Empty": "Vide :"\n}\n'
    assert len(body.encode()) > 2 * 1024 * 1024
    await _put(composite, "i18n/fr.json", body.encode())
    done = await _call(
        composite, "sandbox_replace_in_files", pattern="Vide :", replacement=r"Vide\u00a0:", path_prefix="i18n"
    )
    assert not done.is_error, done.content
    assert "1 replacement(s) in 1 file(s)" in done.content
    after = (await _workspace_bytes(store, conversation, "i18n/fr.json")).decode()
    assert after.endswith('"Empty": "Vide\u00a0:"\n}\n')
    same = await _call(
        composite, "sandbox_replace_in_files", pattern="valeur 7\\b", replacement="valeur 7", path_prefix="i18n"
    )
    assert (
        not same.is_error and same.content.startswith("Nothing to change") and "fr.json (1 match(es))" in same.content
    )


async def test_replace_in_files_names_a_file_over_the_budget(store, monkeypatch):  # noqa: F811
    from app.services.sandbox_workspace import reading

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "r/small.txt", b"abc\n")
    await _put(composite, "r/big.txt", b"abc\n" * 64)
    monkeypatch.setattr(reading, "MAX_GREP_FILE_BYTES", 100)
    none = await _call(composite, "sandbox_replace_in_files", pattern="zzz", replacement="y", path_prefix="r")
    assert not none.is_error and none.content.startswith("No match")
    assert "Not searched: workspace/r/big.txt" in none.content and "over the" in none.content


async def test_replace_in_files_stops_at_the_time_budget(store, monkeypatch):  # noqa: F811
    """#460: past the call's time budget a write refuses, nothing written; a dry run answers partial."""
    from app.services.sandbox_workspace import commands

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "r/a.py", b"old\n")
    await _put(composite, "r/b.py", b"old\n")
    monkeypatch.setattr(commands, "wait_budget", lambda *a, **k: 0.0)
    refused = await _call(composite, "sandbox_replace_in_files", pattern="old", replacement="new", path_prefix="r")
    assert refused.is_error and "time budget ran out after reading 0 of 2" in refused.content
    assert "workspace/r/a.py" in refused.content and "Nothing was written" in refused.content
    assert await _workspace_bytes(store, conversation, "r/a.py") == b"old\n"
    preview = await _call(
        composite, "sandbox_replace_in_files", pattern="old", replacement="new", path_prefix="r", dry_run=True
    )
    assert not preview.is_error, preview.content
    assert "2 file(s) the time budget did not reach" in preview.content


async def test_replace_in_files_refuses_files_past_the_byte_cap(store, monkeypatch):  # noqa: F811
    """#485: a file of the scope past MAX_GREP_BYTES is never read, so a write refuses whole."""
    from app.services.sandbox_workspace import reading

    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    await _put(composite, "r/a.py", b"old\n")
    await _put(composite, "r/b.py", b"old\n" * 4)
    monkeypatch.setattr(reading, "MAX_GREP_BYTES", 10)
    refused = await _call(composite, "sandbox_replace_in_files", pattern="old", replacement="new", path_prefix="r")
    assert refused.is_error and "1 file(s) past the" in refused.content
    assert "workspace/r/b.py" in refused.content and "Nothing was written" in refused.content
    assert await _workspace_bytes(store, conversation, "r/a.py") == b"old\n"
    preview = await _call(
        composite, "sandbox_replace_in_files", pattern="old", replacement="new", path_prefix="r", dry_run=True
    )
    assert not preview.is_error, preview.content
    assert "1 replacement(s) in 1 file(s)" in preview.content and "workspace/r/b.py" in preview.content


def test_report_caps_the_diffstat_lines():
    changes = [
        replacing.FileChange(path=f"f{i}.py", before="a\n", after="b\n", replacements=1)
        for i in range(replacing.MAX_STAT_LINES + 5)
    ]
    stats, _diffs = replacing.report(changes, lambda p: p)
    lines = stats.splitlines()
    assert len(lines) == replacing.MAX_STAT_LINES + 1
    assert lines[-1].startswith("- ... and 5 more file(s) changed")


def test_distinct_changes_counts_repeats_once():
    changes = [
        replacing.FileChange(
            path=f"f{i}.py",
            before="x\nimport old\ny\nimport old\n",
            after="x\nimport new\ny\nimport new\n",
            replacements=2,
        )
        for i in range(30)
    ]
    text = replacing.distinct_changes(changes)
    assert text.startswith("Distinct line changes: 1, most frequent first")
    assert "60x in 30 file(s):\n  - import old\n  + import new" in text
    assert len(text) < 200


def test_distinct_changes_bounded_and_lines_clipped():
    changes = [
        replacing.FileChange(path="a.py", before=f"v{i}\n", after=f"w{i}\n", replacements=1) for i in range(2000)
    ]
    text = replacing.distinct_changes(changes, max_chars=500)
    assert len(text) < 700
    assert "more distinct change(s); sandbox_checkout_diff reads the whole diff" in text
    long = [replacing.FileChange(path="a.py", before="a" * 1000 + "\n", after="b" * 1000 + "\n", replacements=1)]
    clipped = replacing.distinct_changes(long)
    assert f"[+{1000 - replacing.MAX_CHANGE_LINE_CHARS:,} chars]" in clipped


def test_replace_in_files_is_offered_with_the_workspace():
    from app.services.builtin_tools.media import build_code_interpreter_tool

    definitions, _ = build_code_interpreter_tool(user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    tool = next(d for d in definitions if d.name == "sandbox_replace_in_files")
    assert tool.input_schema["required"] == ["pattern", "replacement"]
