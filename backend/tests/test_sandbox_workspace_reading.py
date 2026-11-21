"""Workspace reads without echoing a file, and in-place edits.

The pure rules behind ``sandbox_read_file``, ``sandbox_grep``,
``sandbox_outline`` and ``sandbox_edit_file`` (``reading.py``, ``editing.py``),
and the CI log summary behind ``get_github_job_logs(failures_only=true)``
(``app.integrations.ci_logs``). No I/O, no store.
"""

from __future__ import annotations

import difflib

import pytest

from app.integrations import ci_logs
from app.services.sandbox_workspace import editing, reading

SOURCE = "import os\n\nCONST = 1\n\n\nclass Thing:\n    def run(self):\n        return 1\n\n\nasync def main():\n    pass\n"


# -- window --------------------------------------------------------------------


def test_a_window_is_numbered_and_says_what_remains():
    text = "\n".join(f"line {i}" for i in range(1, 11))
    out = reading.format_window("workspace/a.txt", reading.window(text, 3, 4), size=len(text))
    assert out.startswith("workspace/a.txt: lines 3-6 of 10")
    assert "3 | line 3" in out and "6 | line 6" in out and "7 | line 7" not in out
    assert "[4 more line(s): continue with offset_line=7]" in out


def test_a_window_past_the_end_and_an_empty_file_are_named():
    assert "offset_line 50 is past its end" in reading.format_window("w/a", reading.window("a\nb", 50, 10), size=3)
    assert reading.format_window("w/e", reading.window("", 1, 10), size=0) == "w/e is empty (0 lines, 0 bytes)."


def test_window_arguments_are_clamped():
    assert reading.clamp_window("x", None) == (1, reading.DEFAULT_WINDOW_LINES)
    assert reading.clamp_window(-4, 10**9) == (1, reading.MAX_WINDOW_LINES)


def test_a_long_line_is_cut_with_the_rest_counted():
    out = reading.format_window("w/a", reading.window("x" * 1000, 1, 1), size=1000)
    assert "x" * reading.DEFAULT_LINE_CHARS + " ... [+600 chars: raise max_line_chars (at most 20,000) to read the line whole]" in out
    assert "x" * (reading.DEFAULT_LINE_CHARS + 1) not in out


def test_a_long_line_reads_whole_with_max_line_chars():
    line = "cell | " * 700 + "anchor"
    win = reading.window(f"short\n{line}\nend", 1, 3)
    out = reading.format_window("w/t.md", win, size=len(line), line_chars=reading.clamp_line_chars(6000))
    assert f"2 | {line}" in out and "chars:" not in out
    assert "1 | short" in out and "3 | end" in out


def test_a_line_past_the_line_cap_says_the_cap_is_reached():
    line = "y" * (reading.MAX_LINE_CHARS + 5)
    out = reading.format_window("w/a", reading.window(line, 1, 1), size=len(line), line_chars=reading.MAX_LINE_CHARS)
    assert out.endswith("... [+5 chars past the 20,000-character line cap]")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, reading.DEFAULT_LINE_CHARS),
        ("x", reading.DEFAULT_LINE_CHARS),
        (0, reading.DEFAULT_LINE_CHARS),
        (-3, reading.DEFAULT_LINE_CHARS),
        (True, reading.DEFAULT_LINE_CHARS),
        ("900", 900),
        (6000, 6000),
        (10**9, reading.MAX_LINE_CHARS),
    ],
)
def test_max_line_chars_is_clamped(raw, expected):
    assert reading.clamp_line_chars(raw) == expected


def test_a_window_over_the_result_cap_drops_lines_and_says_so():
    text = "\n".join("y" * 300 for _ in range(400))
    out = reading.format_window("w/a", reading.window(text, 1, 400), size=len(text))
    assert len(out) < reading.MAX_RESULT_CHARS + 1000
    assert "line(s) of the window dropped" in out and "continue with offset_line=" in out


# -- grep ----------------------------------------------------------------------


def test_grep_reports_path_line_hits_and_counts_the_rest():
    result = reading.GrepResult()
    text = "\n".join(["a = 1", "b = 2", "c = 3", "d = 4"])
    assert reading.grep_text("w/x.py", text, reading.compile_pattern(r"= \d"), context=0, budget=2, out=result)
    result.files_scanned = result.files_matched = 1
    out = reading.format_grep(r"= \d", result, prefix="")
    assert "2 match(es)" in out and "w/x.py:1: a = 1" in out and "w/x.py:2: b = 2" in out
    assert "[2 more match(es) not shown" in out


def test_grep_context_lines_are_marked():
    result = reading.GrepResult()
    reading.grep_text("w/x", "one\ntwo\nthree", reading.compile_pattern("two"), context=1, budget=10, out=result)
    assert result.matches[0].context == ["w/x:1- one", "w/x:2: two", "w/x:3- three"]


def test_grep_overlapping_context_is_printed_once():
    result = reading.GrepResult()
    text = "alpha\nbeta\ngamma\nalpha_two\nx\ny\nz\nw\nv\nalpha_far"
    reading.grep_text("w/r", text, reading.compile_pattern("alpha|beta"), context=2, budget=10, out=result)
    result.files_scanned = result.files_matched = 1
    out = reading.format_grep("alpha|beta", result, prefix="")
    body = out.split("\n")[1:]
    assert body == [
        "w/r:1: alpha",
        "w/r:2: beta",
        "w/r:3- gamma",
        "w/r:4: alpha_two",
        "w/r:5- x",
        "w/r:6- y",
        "--",
        "w/r:8- w",
        "w/r:9- v",
        "w/r:10: alpha_far",
    ]
    assert "3 match(es)" not in out and "4 match(es)" in out


def test_grep_shows_the_window_around_a_match_past_the_line_limit():
    line = "a" * 500 + "NEEDLE" + "b" * 300
    result = reading.GrepResult()
    reading.grep_text("w/l", line, reading.compile_pattern("NEEDLE"), context=0, budget=1, out=result, line_chars=80)
    shown = result.matches[0].text
    assert "NEEDLE" in shown and shown.startswith("[463 chars] ... ")
    assert "[+263 chars]" in shown and "raise max_line_chars" in shown
    head = reading.GrepResult()
    reading.grep_text(
        "w/l", "NEEDLE" + "c" * 300, reading.compile_pattern("NEEDLE"), context=0, budget=1, out=head, line_chars=80
    )
    assert head.matches[0].text.startswith("NEEDLE")  # a match inside the head keeps the head


def test_grep_shows_a_long_matched_line_whole_with_max_line_chars():
    line = "a" * 4500 + " needle " + "b" * 500
    regex = reading.compile_pattern("needle")
    cut = reading.GrepResult()
    reading.grep_text("w/x", line, regex, context=0, budget=10, out=cut)
    # Past max_line_chars the match is shown in a window centred on it, the chars around it counted.
    assert " needle " in cut.matches[0].text and "raise max_line_chars" in cut.matches[0].text
    assert cut.matches[0].text.startswith("[") and "window around the match" in cut.matches[0].text
    whole = reading.GrepResult()
    reading.grep_text("w/x", f"{line}\n{'c' * 900}", regex, context=1, budget=10, out=whole, line_chars=6000)
    assert whole.matches[0].text == line
    assert whole.matches[0].context == [f"w/x:1: {line}", f"w/x:2- {'c' * 900}"]


LOG_LINE = (
    '{"timestamp": "2026-11-05T10:00:00.000Z", "level": "info", "name": "opencti", "message": "playbook fired", '
    '"version": "6.9.0", "pid": 42, "stack": "' + "s" * 600 + '"}'
)


def test_grep_with_json_fields_shows_only_those_keys_of_a_json_line():
    regex = reading.compile_pattern("playbook")
    result = reading.GrepResult()
    text = "\n".join([LOG_LINE, "plain playbook line", f"2026-11-05T10:00:01.1Z {LOG_LINE}"])
    fields = reading.clamp_json_fields(["timestamp", "level", "message"])
    reading.grep_text("w/e2e.log", text, regex, context=0, budget=10, out=result, fields=fields)
    first, plain, prefixed = (m.text for m in result.matches)
    assert first == "2026-11-05T10:00:00.000Z | info | playbook fired"
    assert len(first) < 150
    assert plain == "plain playbook line"
    assert prefixed == "2026-11-05T10:00:01.1Z 2026-11-05T10:00:00.000Z | info | playbook fired"


def test_grep_json_fields_write_an_absent_key_as_a_dash_and_skip_non_objects():
    fields = ("timestamp", "missing", "pid")
    assert reading.compact_json_line(LOG_LINE, fields) == "2026-11-05T10:00:00.000Z | - | 42"
    assert reading.compact_json_line("[1, 2]", fields) is None
    assert reading.compact_json_line("{not json}", fields) is None
    assert reading.compact_json_line("x" * 100 + LOG_LINE, fields) is None


def test_grep_without_json_fields_shows_a_json_line_as_before():
    result = reading.GrepResult()
    reading.grep_text("w/e2e.log", LOG_LINE, reading.compile_pattern("playbook"), context=0, budget=10, out=result)
    assert result.matches[0].text == reading._cut(LOG_LINE, reading.DEFAULT_LINE_CHARS)
    assert result.matches[0].text.startswith('{"timestamp"') and "raise max_line_chars" in result.matches[0].text


@pytest.mark.parametrize(
    ("json_fields", "compact_json", "expected"),
    [
        (None, False, ()),
        (None, True, reading.DEFAULT_JSON_FIELDS),
        ([], True, reading.DEFAULT_JSON_FIELDS),
        ("message", False, ("message",)),
        (["level", " ", "level", "message"], False, ("level", "message")),
    ],
)
def test_json_fields_are_clamped(json_fields, compact_json, expected):
    assert reading.clamp_json_fields(json_fields, compact_json) == expected


def _queue_line(ts: str, queue: str, msg: str = "published") -> str:
    return f'{{"timestamp": "{ts}", "message": "{msg}", "attributes": {{"queue": "{queue}", "size": 3}}}}'


RED_LOG = "\n".join(
    [
        _queue_line("10:00", "push_sync"),
        "plain published line",
        _queue_line("10:01", "listen_x"),
        _queue_line("10:05", "push_sync"),
        '{"timestamp": "10:06", "message": "published"}',
    ]
)
GREEN_LOG = "\n".join([_queue_line("09:00", "push_sync"), _queue_line("09:02", "playbook")])
QUEUE = reading.Grouping("attributes.queue")


def _grouped() -> reading.GrepResult:
    result = reading.GrepResult()
    regex = reading.compile_pattern("published")
    for path, text in (("w/green.log", GREEN_LOG), ("w/red.log", RED_LOG)):
        result.files_scanned += 1
        reading.grep_text(path, text, regex, context=0, budget=1, out=result, grouping=QUEUE)
    return result


def test_group_by_counts_each_value_per_file_with_first_and_last_times():
    result = _grouped()
    assert result.matches == [] and result.more == 0  # the budget of lines does not apply to a grouped search
    assert result.group_files == ["w/green.log", "w/red.log"]
    red = result.groups["push_sync"]["w/red.log"]
    assert (red.count, red.first_line, red.first_time, red.last_line, red.last_time) == (2, 1, "10:00", 4, "10:05")
    assert "w/green.log" not in result.groups["listen_x"]
    assert result.ungrouped == {"w/red.log": 2}


def test_format_groups_puts_both_logs_side_by_side():
    lines = reading.format_groups("published", QUEUE, _grouped(), prefix="w").splitlines()
    assert lines[0].startswith("5 matched line(s) for 'published' under w/ grouped by 'attributes.queue' into 3 group(s)")
    assert lines[1] == "Columns: [1] w/green.log; [2] w/red.log"
    assert "listen_x: [1] - | [2] 1 (L3 10:01)" in lines
    assert "playbook: [1] 1 (L2 09:02) | [2] -" in lines
    assert "push_sync: [1] 1 (L1 09:00) | [2] 2 (L1 10:00 .. L4 10:05)" in lines
    assert lines[-1] == "[matched lines with no JSON object or no 'attributes.queue': [2] 2]"


def test_format_groups_bounds_the_rows_and_counts_the_cut():
    result = reading.GrepResult()
    text = "\n".join(_queue_line("t", f"q{i:04d}") for i in range(400))
    reading.grep_text("w/a.log", text, reading.compile_pattern("published"), context=0, budget=1, out=result, grouping=QUEUE)
    out = reading.format_groups("published", QUEUE, result, prefix="", max_chars=1000)
    assert len(out) < 1800
    assert "group(s) not shown: the rows are capped at 1,000 characters" in out


def test_group_time_falls_back_to_the_runner_prefix_and_a_named_field():
    line = '2026-11-06T10:00:00.1Z {"queue": "a", "at": "T1"}'
    regex = reading.compile_pattern("queue")
    result = reading.GrepResult()
    reading.grep_text("w/a.log", line, regex, context=0, budget=1, out=result, grouping=reading.Grouping("queue"))
    assert result.groups["a"]["w/a.log"].first_time == "2026-11-06T10:00:00.1Z"
    named = reading.GrepResult()
    reading.grep_text("w/a.log", line, regex, context=0, budget=1, out=named, grouping=reading.clamp_grouping("queue", "at"))
    assert named.groups["a"]["w/a.log"].first_time == "T1"


def test_group_values_past_the_cap_are_counted_not_kept(monkeypatch):
    monkeypatch.setattr(reading, "MAX_GROUPS", 2)
    result = reading.GrepResult()
    text = "\n".join(_queue_line("t", q) for q in ("a", "b", "c", "c", "a"))
    reading.grep_text("w/a.log", text, reading.compile_pattern("published"), context=0, budget=1, out=result, grouping=QUEUE)
    assert sorted(result.groups) == ["a", "b"] and result.groups_cut == 2
    out = reading.format_groups("p", QUEUE, result, prefix="")
    assert "2 matched line(s) of groups past the first 2 not counted" in out


@pytest.mark.parametrize(
    ("group_by", "time_field", "expected"),
    [
        (None, None, None),
        ("  ", None, None),
        ("attributes.queue", None, reading.Grouping("attributes.queue", "timestamp")),
        ("queue", " ts ", reading.Grouping("queue", "ts")),
    ],
)
def test_grouping_is_clamped(group_by, time_field, expected):
    assert reading.clamp_grouping(group_by, time_field) == expected


def test_json_fields_read_a_dotted_key_nested():
    line = _queue_line("10:00", "push_sync")
    fields = ("timestamp", "attributes.queue", "attributes.missing")
    assert reading.compact_json_line(line, fields) == "10:00 | push_sync | -"
    assert reading.json_get({"a.b": 1, "a": {"b": 2}}, "a.b") == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, reading.DEFAULT_GREP_CHARS),
        ("x", reading.DEFAULT_GREP_CHARS),
        (True, reading.DEFAULT_GREP_CHARS),
        (10, 1000),
        (30_000, 30_000),
        (10**9, reading.MAX_RESULT_CHARS),
    ],
)
def test_grep_max_chars_is_clamped(raw, expected):
    assert reading.clamp_grep_chars(raw) == expected


def test_grep_output_is_cut_at_its_budget_with_the_count_and_how_to_lift_it():
    result = reading.GrepResult()
    text = "\n".join(f"hit {i} " + "z" * 380 for i in range(60))
    reading.grep_text("w/a.log", text, reading.compile_pattern("hit"), context=0, budget=60, out=result)
    result.files_scanned = result.files_matched = 1
    out = reading.format_grep("hit", result, prefix="")
    assert len(out) < reading.DEFAULT_GREP_CHARS + 1000
    assert "60 match(es)" in out and "w/a.log:1: hit 0" in out and "w/a.log:60:" not in out
    shown = out.count("w/a.log:")
    assert f"[{60 - shown} match(es) not shown: the matches are capped at 12,000 characters; raise max_chars (at most 60,000)" in out
    whole = reading.format_grep("hit", result, prefix="", max_chars=reading.clamp_grep_chars(60_000))
    assert "w/a.log:60:" in whole and "not shown" not in whole


def test_grep_budget_keeps_a_match_with_its_context_whole():
    result = reading.GrepResult()
    text = "\n".join("needle " + "q" * 500 if i % 3 == 1 else "ctx " + "r" * 500 for i in range(90))
    reading.grep_text("w/b", text, reading.compile_pattern("needle"), context=1, budget=30, out=result)
    out = reading.format_grep("needle", result, prefix="", max_chars=reading.clamp_grep_chars(5000))
    body = out.split("\n")[1:]
    lines = [line for line in body if line.startswith("w/b:")]
    assert len(lines) % 3 == 0 and not body[-2].startswith("--")
    assert "match(es) not shown" in out


def test_a_bad_pattern_is_refused_with_the_reason():
    with pytest.raises(reading.PatternError, match="does not compile"):
        reading.compile_pattern("(")
    with pytest.raises(reading.PatternError):
        reading.compile_pattern("")


def test_a_global_flag_written_mid_pattern_applies_to_the_whole_pattern():
    regex = reading.compile_pattern(r"error|(?i)warning")
    assert regex.search("WARNING: disk") and regex.search("ERROR")
    assert reading.compile_pattern(r"a(?i:b)c").search("aBc")
    assert not reading.compile_pattern(r"\(?i\)x").search("X")
    with pytest.raises(reading.PatternError, match="ignore_case=true"):
        reading.compile_pattern(r"x(?au)y")


def test_selection_skips_installed_packages_and_honours_globs():
    assert reading.selected("src/a.py", "", [])
    assert not reading.selected(".venv/site-packages/x.py", "", [])
    assert reading.selected(".venv/site-packages/x.py", ".venv", [])
    assert not reading.selected("other/a.py", "src", [])
    assert reading.selected("src/a.py", "src", ["*.py"])
    assert not reading.selected("src/a.md", "src", ["*.py"])
    assert reading.selected("src/deep/a.md", "", ["src/*/*.md"])


def test_include_globs_are_read_from_path_prefix_too():
    # Under path_prefix src a glob names the path from src (deep/*.md) or the workspace path (src/deep/*.md).
    assert reading.selected("src/deep/a.md", "src", ["deep/*.md"])
    assert reading.selected("src/deep/a.md", "src", ["src/deep/*.md"])
    assert reading.selected("src/root.py", "src", ["root.py"])
    assert not reading.selected("src/deep/a.md", "src", ["other/*.md"])


def test_a_binary_head_is_not_text():
    assert not reading.is_text(b"\x00\x01\x02")
    assert reading.is_text("héllo".encode())
    assert reading.is_text(("é" * 5000).encode()[:8191])  # a multi-byte character cut at the boundary
    assert reading.is_text(("a" + "é" * 5000).encode())  # a 2-byte character cut at the head boundary


@pytest.mark.parametrize("cut", [1, 2])
def test_a_3_byte_character_cut_by_the_head_boundary_is_still_text(cut):
    # ja.json: CJK all the way to the head's end, the 8,192nd byte after the first or
    # second byte of a character, so dropping the last 3 bytes still ends inside one.
    lead = "a" * ((reading.TEXT_HEAD_BYTES - cut) % 3)
    data = (lead + "日本語" * 3000).encode()
    head = data[: reading.TEXT_HEAD_BYTES]
    # the last character starts `cut` bytes before the head's end: a lead byte, then continuation bytes
    assert head[-cut] >= 0xE0
    assert all(0x80 <= b < 0xC0 for b in head[len(head) - cut + 1 :])
    with pytest.raises(UnicodeDecodeError):
        head[:-3].decode("utf-8")  # the old retry fails here
    assert reading.is_text(data)


@pytest.mark.parametrize("cut", [1, 2, 3])
def test_a_4_byte_character_cut_by_the_head_boundary_is_still_text(cut):
    lead = "a" * ((reading.TEXT_HEAD_BYTES - cut) % 4)
    assert reading.is_text((lead + "\U0001f600" * 3000).encode())


def test_a_nul_byte_or_an_invalid_byte_in_the_head_stays_binary():
    assert not reading.is_text(b"a" * 100 + b"\x00" + b"b" * 10_000)
    assert not reading.is_text(b"a" * 4000 + b"\xff\xfe" + b"b" * 10_000)
    assert not reading.is_text(b"a" * 4000 + "日".encode()[:1] + b"b" * 10_000)


# -- outline -------------------------------------------------------------------


def test_a_python_outline_lists_classes_methods_functions_and_constants():
    out = reading.format_outline("w/m.py", reading.outline("w/m.py", SOURCE), total_lines=12)
    assert out.startswith("w/m.py (12 lines): 4 definition(s)")
    assert " 3  const CONST" in out and " 6  class Thing" in out and " 7    def run" in out and "11  async def main" in out


def test_a_python_file_with_a_syntax_error_still_gets_a_scan():
    defs = reading.outline("w/m.py", "def ok():\n    pass\nclass Broken(\n")
    assert [(d.kind, d.name) for d in defs] == [("def", "ok"), ("class", "Broken")]


def test_markdown_headings_and_typescript_declarations_are_outlined():
    md = reading.outline("w/g.md", "# Title\n\ntext\n## Part\n### Sub\n")
    assert [(d.depth, d.name) for d in md] == [(0, "Title"), (1, "Part"), (2, "Sub")]
    ts = reading.outline("w/c.tsx", "export const Foo = (a: number) => {\nexport function bar() {}\nexport interface Props {\n")
    assert [(d.kind, d.name) for d in ts] == [("const", "Foo"), ("function", "bar"), ("interface", "Props")]


GRAPHQL_SDL = '''directive @auth(for: [Capabilities] = []) on FIELD_DEFINITION
scalar DateTime
"""
A report.
"""
type Report implements StixDomainObject {
  id: ID!
  name: String!
}
input ReportAddInput {
  name: String! @constraint(minLength: 2)
  published: DateTime
}
enum ReportsOrdering {
  name
}
union Container = Report
interface StixDomainObject {
  id: ID!
}
extend type Query {
  report(id: String!): Report
}
'''


def test_a_graphql_schema_lists_its_definitions_and_reads_one_input_whole():
    defs = reading.outline("w/opencti.graphql", GRAPHQL_SDL)
    assert [(d.kind, d.name) for d in defs] == [
        ("directive", "auth"),
        ("scalar", "DateTime"),
        ("type", "Report"),
        ("input", "ReportAddInput"),
        ("enum", "ReportsOrdering"),
        ("union", "Container"),
        ("interface", "StixDomainObject"),
        ("extend type", "Query"),
    ]
    spans = {s.name: (s.first, s.last) for s in reading.symbols("w/opencti.graphql", GRAPHQL_SDL)}
    assert spans["ReportAddInput"] == (10, 13)
    assert reading.outline("w/s.gql", "input A {\n  b: Int\n}\n")[0].name == "A"
    assert "graphql" in reading.outline_kinds()


def test_an_extension_without_a_scan_is_named_not_guessed():
    assert reading.outline("w/a.bin", "x") is None
    out = reading.format_outline("w/a.bin", None, total_lines=1)
    assert "No outline for w/a.bin" in out and "'bin'" in out and "sandbox_read_file" in out


# -- edits ---------------------------------------------------------------------


def test_edits_apply_in_order_and_the_diff_reads_back():
    before = "a = 1\nb = 2\nc = a + b\n"
    edits = editing.parse_edits([{"find": "a = 1", "replace": "a = 10"}, {"find": "a + b", "replace": "a * b"}])
    applied = editing.apply_edits(before, edits)
    assert applied.text == "a = 10\nb = 2\nc = a * b\n" and applied.replacements == [1, 1]
    diff = editing.unified_diff("x.py", before, applied.text)
    assert diff.startswith("--- a/x.py\n+++ b/x.py\n") and "-a = 1\n+a = 10\n" in diff
    assert editing.summarize(before, applied.text) == (2, 2)


def test_a_missing_or_ambiguous_find_refuses_the_whole_call():
    text = "x = 1\ny = 1\n"
    with pytest.raises(editing.EditError, match="edit 2: 'find' text not found"):
        editing.apply_edits(text, editing.parse_edits([{"find": "x = 1", "replace": "x = 2"}, {"find": "zzz", "replace": ""}]))
    with pytest.raises(editing.EditError, match=r"occurs 2 times \(first at line 1\)"):
        editing.apply_edits(text, editing.parse_edits([{"find": "= 1", "replace": "= 2"}]))


def test_a_crlf_file_matches_an_lf_snippet_and_keeps_its_endings():
    """A Windows-ended file: the find copied with LF matches, the file is written back with CRLF."""
    before = "def f():\r\n    return 1\r\n\r\nx = f()\r\n"
    edits = editing.parse_edits(
        [
            {"find": "def f():\n    return 1\n", "replace": "def f():\n    return 2\n"},
            {"find": "x = f()\r\n", "replace": "x = f()\r\ny = x\r\n"},
        ]
    )
    applied = editing.apply_edits(before, edits)
    assert applied.text == "def f():\r\n    return 2\r\n\r\nx = f()\r\ny = x\r\n"
    assert applied.replacements == [1, 1]
    assert editing.summarize(before, applied.text) == (2, 1)


def test_a_mixed_or_lf_file_is_matched_as_it_is():
    assert not editing.is_crlf("a\r\nb\n") and not editing.is_crlf("a") and editing.is_crlf("a\r\nb\r\n")
    with pytest.raises(editing.EditError, match="not found"):
        editing.apply_edits("a\r\nb\nc\n", editing.parse_edits([{"find": "a\nb", "replace": "x"}]))
    assert editing.apply_edits("a\nb\n", editing.parse_edits([{"find": "a\nb", "replace": "x"}])).text == "x\n"


def test_replace_all_counts_every_occurrence():
    applied = editing.apply_edits("= 1\n= 1\n", editing.parse_edits([{"find": "= 1", "replace": "= 2", "replace_all": True}]))
    assert applied.text == "= 2\n= 2\n" and applied.replacements == [2]


@pytest.mark.parametrize(
    "raw",
    [None, [], "x", [{"find": "", "replace": "a"}], [{"find": "a", "replace": 3}], ["a"], [{"find": "a", "replace": ""}] * (editing.MAX_EDITS + 1)],
    ids=["none", "empty", "string", "empty-find", "non-string-replace", "non-object", "too-many"],
)
def test_malformed_edits_are_refused_before_reading_the_file(raw):
    with pytest.raises(editing.EditError):
        editing.parse_edits(raw)


def _ranged(raw: object) -> list[editing.Edit]:
    return editing.parse_edits(raw, allow_ranges=True)


def test_a_line_range_edit_replaces_or_deletes_the_block_its_anchors_name():
    before = "a\nb\nc\nd\ne\n"
    replaced = editing.apply_edits(before, _ranged([{"start_line": 2, "end_line": 4, "expect_first": "b", "expect_last": "d", "replace": "X\nY"}]))
    assert replaced.text == "a\nX\nY\ne\n" and replaced.replacements == [1]
    deleted = editing.apply_edits(before, _ranged([{"start_line": 5, "end_line": 5, "expect_first": "e", "replace": ""}]))
    assert deleted.text == "a\nb\nc\nd\n"
    one = editing.apply_edits("a\nb", _ranged([{"start_line": 2, "expect_first": "b", "replace": "z"}]))
    assert one.text == "a\nz"


def test_a_line_range_counts_lines_after_the_previous_edits_of_the_call():
    edits = _ranged([{"find": "a\n", "replace": ""}, {"start_line": 1, "end_line": 1, "expect_first": "b", "replace": "B"}])
    assert editing.apply_edits("a\nb\nc\n", edits).text == "B\nc\n"


def test_a_line_range_keeps_crlf_endings():
    edits = _ranged([{"start_line": 2, "end_line": 3, "expect_first": "b", "expect_last": "c", "replace": "x\ny\n"}])
    assert editing.apply_edits("a\r\nb\r\nc\r\nd\r\n", edits).text == "a\r\nx\r\ny\r\nd\r\n"


def test_a_stale_or_past_the_end_range_refuses_the_whole_call():
    with pytest.raises(editing.EditError, match=r"line 2 is 'b'.*stale"):
        editing.apply_edits("a\nb\n", _ranged([{"start_line": 2, "expect_first": "zz", "replace": ""}]))
    with pytest.raises(editing.EditError, match="run past the end"):
        editing.apply_edits("a\n", _ranged([{"start_line": 1, "end_line": 3, "expect_first": "a", "expect_last": "q", "replace": ""}]))


@pytest.mark.parametrize(
    "item",
    [
        {"start_line": 0, "expect_first": "a", "replace": ""},
        {"start_line": 3, "end_line": 2, "expect_first": "a", "replace": ""},
        {"start_line": "1", "expect_first": "a", "replace": ""},
        {"start_line": 1, "replace": ""},
        {"start_line": 1, "end_line": 2, "expect_first": "a", "replace": ""},
        {"start_line": 1, "expect_first": "a", "find": "a", "replace": ""},
    ],
    ids=["zero", "inverted", "string", "no-expect-first", "no-expect-last", "find-and-range"],
)
def test_malformed_line_ranges_are_refused(item):
    with pytest.raises(editing.EditError):
        _ranged([item])


def test_line_ranges_are_refused_where_not_allowed():
    """create_github_commit's edits stay find/replace: a range there is a missing find."""
    with pytest.raises(editing.EditError, match="'find' must be a non-empty string"):
        editing.parse_edits([{"start_line": 1, "expect_first": "a", "replace": ""}])


def test_a_range_anchors_on_a_blank_line_by_the_explicit_flag():
    before = "a\n\nb\nc\n\nd\n"
    edits = _ranged([{"start_line": 2, "end_line": 5, "expect_first_blank": True, "expect_last_blank": True, "replace": "X"}])
    assert editing.apply_edits(before, edits).text == "a\nX\nd\n"
    with pytest.raises(editing.EditError, match="not blank as expect_first_blank"):
        editing.apply_edits(before, _ranged([{"start_line": 1, "expect_first_blank": True, "replace": ""}]))
    with pytest.raises(editing.EditError, match="expect_last_blank=true"):
        _ranged([{"start_line": 1, "end_line": 2, "expect_first": "a", "expect_last": "  ", "replace": ""}])
    with pytest.raises(editing.EditError, match="must be true or false"):
        _ranged([{"start_line": 2, "expect_first_blank": "yes", "replace": ""}])


def test_an_insert_puts_lines_after_its_anchor_and_replaces_nothing():
    before = "a\nb\n"
    top = editing.apply_edits(before, _ranged([{"insert_after_line": 0, "replace": "# head"}]))
    assert top.text == "# head\na\nb\n" and top.replacements == [1]
    mid = editing.apply_edits(before, _ranged([{"insert_after_line": 1, "expect_first": "a", "replace": "x\ny\n"}]))
    assert mid.text == "a\nx\ny\nb\n"
    end = editing.apply_edits("a\nb", _ranged([{"insert_after_line": 2, "expect_first": "b", "replace": "c"}]))
    assert end.text == "a\nb\nc\n"
    blank = editing.apply_edits("a\n\nb\n", _ranged([{"insert_after_line": 2, "expect_first_blank": True, "replace": "z"}]))
    assert blank.text == "a\n\nz\nb\n"
    crlf = editing.apply_edits("a\r\nb\r\n", _ranged([{"insert_after_line": 1, "expect_first": "a", "replace": "x"}]))
    assert crlf.text == "a\r\nx\r\nb\r\n"


def test_an_insert_counts_lines_after_the_previous_edits_of_the_call():
    edits = _ranged(
        [
            {"insert_after_line": 1, "expect_first": "a", "replace": "1\n2\n"},
            {"insert_after_line": 3, "expect_first": "2", "replace": "3"},
        ]
    )
    assert editing.apply_edits("a\nb\n", edits).text == "a\n1\n2\n3\nb\n"


def test_a_stale_or_past_the_end_insert_refuses_the_whole_call():
    with pytest.raises(editing.EditError, match=r"line 1 is 'a'.*stale"):
        editing.apply_edits("a\nb\n", _ranged([{"insert_after_line": 1, "expect_first": "zz", "replace": "x"}]))
    with pytest.raises(editing.EditError, match="past the end"):
        editing.apply_edits("a\n", _ranged([{"insert_after_line": 3, "expect_first": "a", "replace": "x"}]))


@pytest.mark.parametrize(
    "item",
    [
        {"insert_after_line": -1, "replace": "x"},
        {"insert_after_line": True, "replace": "x"},
        {"insert_after_line": 0, "replace": ""},
        {"insert_after_line": 2, "replace": "x"},
        {"insert_after_line": 1, "expect_first": "a", "find": "a", "replace": "x"},
        {"insert_after_line": 1, "start_line": 1, "expect_first": "a", "replace": "x"},
    ],
    ids=["negative", "bool", "empty-replace", "no-expect-first", "with-find", "with-range"],
)
def test_malformed_inserts_are_refused(item):
    with pytest.raises(editing.EditError):
        _ranged([item])


def test_an_occurrence_replaces_only_that_occurrence_of_a_repeated_find() -> None:
    text = "block\nx = 1\nblock\nx = 1\nblock\nx = 1\n"
    second = editing.apply_edits(text, _ranged([{"find": "x = 1", "replace": "x = 2", "occurrence": 2}]))
    assert second.text == "block\nx = 1\nblock\nx = 2\nblock\nx = 1\n" and second.replacements == [1]
    # Counted in the text as the previous edits of the call left it: an insert after each block.
    both = editing.apply_edits(
        "a\nend\nb\nend\n",
        _ranged(
            [
                {"find": "end\n", "replace": "end\nnew\n", "occurrence": 2},
                {"find": "end\n", "replace": "end\nnew\n", "occurrence": 1},
            ]
        ),
    )
    assert both.text == "a\nend\nnew\nb\nend\nnew\n"
    crlf = editing.apply_edits("k\r\nk\r\n", _ranged([{"find": "k", "replace": "z", "occurrence": 2}]))
    assert crlf.text == "k\r\nz\r\n"


def test_an_occurrence_past_the_count_or_malformed_is_refused() -> None:
    with pytest.raises(editing.EditError, match=r"occurrence 3 asked but 'find' text occurs 2 time\(s\)"):
        editing.apply_edits("a\na\n", _ranged([{"find": "a", "replace": "b", "occurrence": 3}]))
    for bad in (0, -1, True, "2"):
        with pytest.raises(editing.EditError, match="'occurrence' must be an integer >= 1"):
            _ranged([{"find": "a", "replace": "b", "occurrence": bad}])
    with pytest.raises(editing.EditError, match="either 'occurrence' or 'replace_all'"):
        _ranged([{"find": "a", "replace": "b", "occurrence": 1, "replace_all": True}])
    # Outside sandbox_edit_file (create_github_commit, comment bodies) the key is not part of the contract.
    assert editing.parse_edits([{"find": "a", "replace": "b", "occurrence": 2}])[0].occurrence == 0


def test_a_move_carries_a_block_down_up_and_to_the_top() -> None:
    text = "a\nb\nc\nd\ne\n"
    down = editing.apply_edits(
        text,
        _ranged(
            [
                {
                    "start_line": 1,
                    "end_line": 2,
                    "expect_first": "a",
                    "expect_last": "b",
                    "move_to_after_line": 4,
                    "expect_target": "d",
                    "replace": "",
                }
            ]
        ),
    )
    assert down.text == "c\nd\na\nb\ne\n" and down.replacements == [1]
    up = editing.apply_edits(
        text,
        _ranged(
            [
                {
                    "start_line": 4,
                    "end_line": 5,
                    "expect_first": "d",
                    "expect_last": "e",
                    "move_to_after_line": 1,
                    "expect_target": "a",
                }
            ]
        ),
    )
    assert up.text == "a\nd\ne\nb\nc\n"
    top = editing.apply_edits(text, _ranged([{"start_line": 3, "expect_first": "c", "move_to_after_line": 0}]))
    assert top.text == "c\na\nb\nd\ne\n"
    # Numbered as the previous edits of the call left the text.
    chained = editing.apply_edits(
        text,
        _ranged(
            [
                {"insert_after_line": 0, "replace": "z"},
                {"start_line": 1, "expect_first": "z", "move_to_after_line": 6, "expect_target": "e"},
            ]
        ),
    )
    assert chained.text == text + "z\n"


def test_a_move_keeps_crlf_and_gives_an_ending_to_a_last_line_without_one() -> None:
    crlf = editing.apply_edits(
        "a\r\nb\r\nc\r\n",
        _ranged([{"start_line": 1, "expect_first": "a", "move_to_after_line": 3, "expect_target": "c"}]),
    )
    assert crlf.text == "b\r\nc\r\na\r\n"
    last = editing.apply_edits(
        "a\nb\nc", _ranged([{"start_line": 3, "expect_first": "c", "move_to_after_line": 1, "expect_target": "a"}])
    )
    assert last.text == "a\nc\nb\n"
    onto_last = editing.apply_edits(
        "a\nb\nc", _ranged([{"start_line": 1, "expect_first": "a", "move_to_after_line": 3, "expect_target": "c"}])
    )
    assert onto_last.text == "b\nc\na\n"
    blank = editing.apply_edits(
        "a\n\nb\n",
        _ranged([{"start_line": 1, "expect_first": "a", "move_to_after_line": 2, "expect_target_blank": True}]),
    )
    assert blank.text == "\na\nb\n"


@pytest.mark.parametrize(
    ("item", "message"),
    [
        (
            {
                "start_line": 2,
                "end_line": 3,
                "expect_first": "b",
                "expect_last": "c",
                "move_to_after_line": 2,
                "expect_target": "b",
            },
            "would not move",
        ),
        ({"start_line": 2, "expect_first": "b", "move_to_after_line": 1, "expect_target": "a"}, "would not move"),
        ({"start_line": 1, "expect_first": "a", "move_to_after_line": 3}, "needs 'expect_target'"),
        ({"start_line": 1, "expect_first": "a", "move_to_after_line": -1}, "integer >= 0"),
        ({"start_line": 1, "expect_first": "a", "move_to_after_line": True}, "integer >= 0"),
        (
            {"start_line": 1, "expect_first": "a", "move_to_after_line": 3, "expect_target": "c", "replace": "x"},
            "leave 'replace' empty",
        ),
        ({"find": "a", "move_to_after_line": 3, "expect_target": "c"}, "not 'find'"),
        ({"move_to_after_line": 3, "expect_target": "c"}, "needs 'start_line'"),
        ({"start_line": 1, "move_to_after_line": 3, "expect_target": "c"}, "needs 'expect_first'"),
    ],
)
def test_malformed_moves_are_refused(item: dict[str, object], message: str) -> None:
    with pytest.raises(editing.EditError, match=message):
        _ranged([item])


def test_a_stale_move_is_refused_with_nothing_written() -> None:
    for item, message in (
        (
            {"start_line": 1, "expect_first": "x", "move_to_after_line": 3, "expect_target": "c"},
            "does not contain expect_first",
        ),
        (
            {"start_line": 1, "expect_first": "a", "move_to_after_line": 3, "expect_target": "x"},
            "does not contain expect_target",
        ),
        ({"start_line": 1, "expect_first": "a", "move_to_after_line": 9, "expect_target": "c"}, "past the end"),
    ):
        with pytest.raises(editing.EditError, match=message):
            editing.apply_edits("a\nb\nc\n", _ranged([item]))
    # Outside sandbox_edit_file the key is not part of the contract: a find edit stays a find edit.
    with pytest.raises(editing.EditError, match="'find' must be a non-empty string"):
        editing.parse_edits([{"start_line": 1, "move_to_after_line": 3, "replace": ""}])


def test_inserts_are_refused_where_not_allowed():
    with pytest.raises(editing.EditError, match="'find' must be a non-empty string"):
        editing.parse_edits([{"insert_after_line": 0, "replace": "x"}])


def test_a_file_without_a_final_newline_diffs_cleanly():
    diff = editing.unified_diff("x", "a", "b")
    assert "\\ No newline at end of file" in diff


ROW = "| " + " | ".join(f"cell {i:04d} text" for i in range(1000)) + " |"


def test_a_one_word_edit_of_a_long_line_reads_back_only_what_changed():
    before = f"# Table\n\n{ROW}\n{ROW.replace('cell', 'item')}\ntail\n"
    applied = editing.apply_edits(before, editing.parse_edits([{"find": "cell 0500 text", "replace": "cell 0500 TEXT"}]))
    diff = editing.unified_diff("t.md", before, applied.text)
    assert len(ROW) > 17_000 and len(diff) < 1_500
    removed = next(line for line in diff.splitlines() if line.startswith("-|") or line.startswith("-["))
    added = next(line for line in diff.splitlines() if line.startswith("+|") or line.startswith("+["))
    assert "cell 0500 text" in removed and "cell 0500 TEXT" in added
    assert removed.startswith("-[") and "unchanged chars] ..." in removed and removed.endswith("unchanged chars]")
    # The long context row is cut and counted; the short ones stay whole.
    assert " item 0000 text" in diff and "chars]" in diff and " tail\n" in diff and " # Table\n" in diff
    # The read-back only: the file written is the edit, whole.
    assert applied.text == before.replace("cell 0500 text", "cell 0500 TEXT")
    assert editing.summarize(before, applied.text) == (1, 1)


def test_a_diff_of_short_lines_is_unchanged():
    before, after = "a\nb\nc\nd\n", "a\nB\nc\nd\ne\n"
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    expected = "".join(difflib.unified_diff(old, new, fromfile="a/x", tofile="b/x", n=3))
    assert editing.unified_diff("x", before, after) == expected


def test_unpaired_long_lines_show_their_head_and_a_changed_tail_is_shown():
    long_added = "x" * 5_000
    diff = editing.unified_diff("x", "a\n", f"a\n{long_added}\n")
    assert f"+{'x' * editing.LONG_LINE_CHARS} ... [+{5_000 - editing.LONG_LINE_CHARS:,} chars]\n" in diff
    # Two removed lines replaced by one: the first pair is compacted, the second removed line cut.
    before = f"{'y' * 3_000}END1\n{'z' * 3_000}\n"
    after = f"{'y' * 3_000}END2\n"
    diff = editing.unified_diff("x", before, after)
    assert "END1" in diff and "END2" in diff and "[2,923 unchanged chars] ..." in diff
    assert f"-{'z' * editing.LONG_LINE_CHARS} ... [+{3_000 - editing.LONG_LINE_CHARS:,} chars]" in diff
    assert len(diff) < 2_000


def test_a_compacted_crlf_line_keeps_its_crlf_ending():
    before = f"# Table\r\n{ROW}\r\n{ROW.replace('cell', 'item')}\r\n"
    after = before.replace("cell 0500 text", "cell 0500 TEXT")
    diff = editing.unified_diff("t.md", before, after)
    body = diff.splitlines(keepends=True)[2:]
    changed = [line for line in body if line[:1] in "-+ "]
    assert changed and all(line.endswith("\r\n") for line in changed)
    assert any("unchanged chars]" in line for line in changed)
    assert not any(line.endswith("\r") or "\r " in line or "\r..." in line for line in body)


# -- CI log summary ------------------------------------------------------------


PYTEST_LOG = (
    "2026-10-30T16:00:00.1234567Z ##[group]Run uv sync\n"
    "2026-10-30T16:00:00.1234567Z uv sync --locked\n"
    "2026-10-30T16:00:00.1234567Z ##[endgroup]\n"
    "2026-10-30T16:00:01.1234567Z ##[group]Run pytest\n"
    "2026-10-30T16:00:01.1234567Z ##[endgroup]\n"
    + "".join(f"2026-10-30T16:00:02.0Z tests/test_x.py::test_{i} PASSED\n" for i in range(300))
    + "2026-10-30T16:00:03.0Z =================================== FAILURES ===================================\n"
    "2026-10-30T16:00:03.0Z _______________________ test_modules _______________________\n"
    "2026-10-30T16:00:03.0Z     assert findings == []\n"
    "2026-10-30T16:00:03.0Z E   AssertionError: app/x.py:12\n"
    "2026-10-30T16:00:03.0Z =========================== short test summary info ============================\n"
    "2026-10-30T16:00:03.0Z FAILED tests/test_p.py::test_modules - AssertionError\n"
    "2026-10-30T16:00:03.0Z ================== 1 failed, 15520 passed in 120.11s ==================\n"
    "2026-10-30T16:00:04.0Z ##[error]Process completed with exit code 1.\n"
    "2026-10-30T16:00:05.0Z ##[group]Run upload\n"
    "2026-10-30T16:00:05.0Z ok\n"
    "2026-10-30T16:00:05.0Z ##[endgroup]\n"
)


def test_a_pytest_log_is_reduced_to_its_failure_sections_under_their_step():
    out = ci_logs.format_summary(ci_logs.summarize(PYTEST_LOG), title="Logs for job 1")
    assert out.startswith("Logs for job 1: 3 failure block(s) in step(s) 'Run pytest'")
    assert "--- step: Run pytest ---" in out and "--- step: Run upload ---" not in out
    assert "FAILED tests/test_p.py::test_modules - AssertionError" in out
    assert "1 failed, 15520 passed" in out and "##[error]Process completed with exit code 1." in out
    assert "test_7 PASSED" not in out and "2026-10-30T" not in out
    assert len(out) < 1500


def test_a_linter_finding_is_kept_with_its_step():
    log = "##[group]Run ruff\nruff check .\n##[endgroup]\napp/x.py:120:1: I001 [*] Import block is un-sorted\nFound 1 error.\n##[error]Process completed with exit code 1.\n"
    out = ci_logs.format_summary(ci_logs.summarize(log), title="t")
    assert "step(s) 'Run ruff'" in out and "app/x.py:120:1: I001" in out


def test_a_green_log_says_no_cue_was_found():
    out = ci_logs.format_summary(ci_logs.summarize("2026-10-30T16:00:02.0Z all good\n"), title="t")
    assert out.startswith("t: no failure cue found in 1 log lines")


def test_a_capped_summary_keeps_the_test_report_and_drops_progress_lines():
    """Many cue lines before the report must not push the FAILURES and short summary out of the cap."""
    noisy = "".join(
        f"app/x.py:{i}: error: seen in a log line\n"
        + "".join(f"tests/test_x.py::test_{i}_{k} PASSED\n" for k in range(12))
        for i in range(400)
    )
    log = (
        "##[group]Run vite build\n##[endgroup]\n"
        + "".join(f"dist/assets/chunk-{i}.js  12.3 kB\n" for i in range(2000))
        + "##[group]Run pytest\n##[endgroup]\n"
        + noisy
        + "=================================== FAILURES ===================================\n"
        "_______________________ test_dedup _______________________\n"
        "E   AttributeError: 'types.SimpleNamespace' object has no attribute 'agent_id'\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_d.py::test_dedup - AttributeError\n"
        "================== 1 failed, 900 passed in 60.00s ==================\n"
        "##[error]Process completed with exit code 1.\n"
    )
    out = ci_logs.format_summary(ci_logs.summarize(log), title="t", log_chars=len(log))
    assert "FAILED tests/test_d.py::test_dedup - AttributeError" in out
    assert "no attribute 'agent_id'" in out
    assert out.index("short test summary info") < out.index("FAILURES") < out.index("seen in a log line")
    assert " PASSED" not in out and "chunk-" not in out
    assert f"The whole log ({len(log):,} characters)" in out
    assert len(out) <= ci_logs.MAX_SUMMARY_CHARS + 600


def test_xdist_progress_lines_whose_test_ids_mention_errors_are_no_cue():
    log = (
        "##[group]Run pytest\n##[endgroup]\n"
        "tests/test_e.py::test_auth_failures_are_still_caught[Error: Tanium GraphQL error: Unauthorized] \n"
        "[gw1] [ 31%] PASSED tests/test_e.py::test_auth_failures_are_still_caught[Error: Tanium GraphQL error: Unauthorized] \n"
        "tests/test_e.py::test_x[GitHub API error: 401] PASSED [ 32%]\n"
        "[gw0] [ 52%] FAILED tests/test_d.py::test_dedup\n"
        "=========================== short test summary info ============================\n"
        "FAILED tests/test_d.py::test_dedup - AttributeError\n"
        "================== 1 failed, 2 passed in 1.00s ==================\n"
    )
    out = ci_logs.format_summary(ci_logs.summarize(log), title="t")
    assert "Tanium" not in out and "API error: 401" not in out
    assert "FAILED tests/test_d.py::test_dedup - AttributeError" in out


def test_a_typical_pytest_failure_summary_stays_under_eight_thousand_characters():
    out = ci_logs.format_summary(ci_logs.summarize(PYTEST_LOG), title="t", log_chars=len(PYTEST_LOG))
    assert len(out) < 8000 and "step(s) 'Run pytest'" in out


def test_the_summary_is_bounded_and_counts_the_blocks_dropped():
    # Distinct by name (failure_<i>), so the repeat folding of #815 leaves each block apart.
    log = "".join(f"##[error]failure_{i} " + "x" * 400 + "\n" for i in range(200))
    out = ci_logs.format_summary(ci_logs.summarize(log), title="t")
    assert len(out) <= ci_logs.MAX_SUMMARY_CHARS + 300
    assert "more failure block(s) not shown" in out
