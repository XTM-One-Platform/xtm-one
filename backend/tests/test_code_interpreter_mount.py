"""Conversation files are mounted in the code-interpreter sandbox.

Asked to compute over an attached 558k HTML model, the model tried
``open('Filigran_Americas_Coverage_Model TD.html')`` in a sandbox that held
nothing and retried with other paths - every retry an iteration. The files of
the turn now ride into the working directory under ``attachments/`` (plus
``attachments/index.json``), for the local subprocess and OpenSandbox alike:
names unique BY CONSTRUCTION (a file-id tag), bounded in bytes, NFC-normalised,
Windows device names defused; downloaded only when the code names the
directory; read through the turn's ``FileContext`` (no access it did not
grant); and the result announces only what the sandbox actually wrote.
"""

from __future__ import annotations

import json
import sys
import uuid

import pytest

from app.llm.base import FileRef
from app.services import code_interpreter
from app.services.builtin_tools import media
from app.tools.base import FileContext, ToolCall

_ID_A = "0f3c9a1b-2222-4333-8444-555566667777"
_ID_B = "7d0e11aa-2222-4333-8444-555566667777"


# -- Names --------------------------------------------------------------------


def test_a_mount_name_is_flat_safe_and_tagged_with_its_file_id():
    name = code_interpreter.safe_mount_name
    assert (
        name("Filigran_Americas_Coverage_Model TD.html", _ID_A) == "Filigran_Americas_Coverage_Model TD-0f3c9a1b222243338444555566667777.html"
    )
    assert name("../../etc/passwd", _ID_A) == "passwd-0f3c9a1b222243338444555566667777"
    assert name("a/b\\c:d*e?.csv", _ID_A) == "c_d_e_-0f3c9a1b222243338444555566667777.csv"
    assert name("", _ID_A) == "file-0f3c9a1b222243338444555566667777"
    assert name("...", _ID_A) == "file-0f3c9a1b222243338444555566667777"
    assert name(None, _ID_A) == "file-0f3c9a1b222243338444555566667777"
    assert name("DATA.CSV", _ID_A) == "DATA-0f3c9a1b222243338444555566667777.csv"
    assert name("archive.tar.gz", _ID_A) == "archive.tar-0f3c9a1b222243338444555566667777.gz"
    assert name("line\nbreak\t.txt", _ID_A) == "line_break_-0f3c9a1b222243338444555566667777.txt"


def test_two_uploads_with_the_same_name_never_collide():
    """The first-come-first-served name could shadow
    another file. The file-id tag makes the name unique whatever else is
    mounted - so a footer or a refusal naming ONE file's path is exact."""
    a = code_interpreter.safe_mount_name("data.csv", _ID_A)
    b = code_interpreter.safe_mount_name("data.csv", _ID_B)
    assert a != b and a.casefold() != b.casefold()
    assert code_interpreter.mount_path_hint("data.csv", _ID_A) == f"attachments/{a}"


def test_a_mount_name_is_capped_in_bytes_not_characters():
    """The 200-CHARACTER cap let a CJK name reach 600
    bytes, past the 255-byte filesystem limit."""
    cjk = code_interpreter.safe_mount_name("資料" * 200 + ".csv", _ID_A)
    assert cjk.endswith("-0f3c9a1b222243338444555566667777.csv")
    assert len(cjk.encode("utf-8")) <= 255
    assert "\ufffd" not in cjk, "never cut inside a character"
    latin = code_interpreter.safe_mount_name("x" * 400 + ".xlsx", _ID_A)
    assert latin.endswith("-0f3c9a1b222243338444555566667777.xlsx") and len(latin.encode("utf-8")) == 255


def test_a_mount_name_is_nfc_normalised():
    decomposed = "Cafe\u0301 plan.csv"
    composed = "Caf\u00e9 plan.csv"
    assert code_interpreter.safe_mount_name(decomposed, _ID_A) == code_interpreter.safe_mount_name(composed, _ID_A)
    assert code_interpreter.safe_mount_name(decomposed, _ID_A) == "Caf\u00e9 plan-0f3c9a1b222243338444555566667777.csv"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("CON.txt", "_CON-0f3c9a1b222243338444555566667777.txt"),
        ("con", "_con-0f3c9a1b222243338444555566667777"),
        ("NUL.csv", "_NUL-0f3c9a1b222243338444555566667777.csv"),
        ("com1.json", "_com1-0f3c9a1b222243338444555566667777.json"),
        ("LPT9.tar.gz", "_LPT9.tar-0f3c9a1b222243338444555566667777.gz"),
        ("aux .csv", "_aux-0f3c9a1b222243338444555566667777.csv"),
        ("console.log", "console-0f3c9a1b222243338444555566667777.log"),
    ],
)
def test_a_windows_device_name_is_defused(filename, expected):
    assert code_interpreter.safe_mount_name(filename, _ID_A) == expected


def test_code_reads_attachments_is_what_triggers_a_download():
    assert code_interpreter.code_reads_attachments("import pandas as pd\npd.read_csv('attachments/x.csv')")
    assert code_interpreter.code_reads_attachments("json.load(open('attachments/index.json'))")
    assert code_interpreter.code_reads_attachments('os.listdir("attachments")')
    assert code_interpreter.code_reads_attachments("open(r'attachments\\x.csv')")
    assert not code_interpreter.code_reads_attachments("print(sum(range(10)))")
    assert not code_interpreter.code_reads_attachments("")
    # A comment or prose naming the word is not a path: no download for it.
    assert not code_interpreter.code_reads_attachments("# summarise the attachments\nprint(2 + 2)")


@pytest.mark.parametrize("filename", ["CONIN$.x.csv", "CONOUT$.a.b", "COM\u00b9.x.csv", "LPT\u00b3.log"])
def test_every_device_name_the_stdlib_knows_is_defused(filename):
    import ntpath

    name = code_interpreter.safe_mount_name(filename, _ID_A)
    assert not ntpath.isreserved(name), name
    assert name.endswith("-0f3c9a1b222243338444555566667777" + (filename[filename.rfind(".") :].lower()))


def test_invisible_and_line_breaking_characters_never_reach_a_name():
    name = code_interpreter.safe_mount_name("a\x85b\u2028c\u2029d\u202efdp.exe.csv", _ID_A)
    assert not any(ch in name for ch in "\x85\u2028\u2029\u202e")
    assert name == "abcdfdp.exe-0f3c9a1b222243338444555566667777.csv"


def test_a_directory_or_a_file_planted_before_the_mount_is_refused(tmp_path):
    """Local sandbox hardening: the mount never writes through an
    ``attachments`` directory (or link) it did not create, nor over a file
    already there under the name."""
    (tmp_path / code_interpreter.ATTACHMENTS_DIRNAME).mkdir()
    written, names = code_interpreter._write_input_files(str(tmp_path), [("a.csv", b"x")])
    assert (written, names) == (set(), [])
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    written, names = code_interpreter._write_input_files(str(fresh), [("a.csv", b"1"), ("a.csv", b"2")])
    assert names == ["a.csv"], "the second write under one name is refused, never an overwrite"
    assert (fresh / code_interpreter.ATTACHMENTS_DIRNAME / "a.csv").read_bytes() == b"1"


# -- The local sandbox ---------------------------------------------------------


def test_inputs_are_written_under_attachments_and_excluded_from_the_outputs(tmp_path):
    written, names = code_interpreter._write_input_files(
        str(tmp_path), [("data-0f3c9a1b222243338444555566667777.csv", b"a,b\n1,2\n"), ("index.json", b"[]")]
    )
    attach_dir = tmp_path / code_interpreter.ATTACHMENTS_DIRNAME
    assert (attach_dir / "data-0f3c9a1b222243338444555566667777.csv").read_bytes() == b"a,b\n1,2\n"
    assert written == {str(attach_dir / "data-0f3c9a1b222243338444555566667777.csv"), str(attach_dir / "index.json")}
    assert names == ["data-0f3c9a1b222243338444555566667777.csv", "index.json"]
    # What the code writes is an output; what was mounted is not.
    (tmp_path / "out.txt").write_text("written by the code")
    (attach_dir / "derived.txt").write_text("written next to the mounts")
    collected = code_interpreter._collect_output_files(str(tmp_path), exclude=written)
    assert sorted(f["filename"] for f in collected) == ["attachments/derived.txt", "out.txt"]


def test_a_name_that_is_not_a_plain_file_name_is_never_written(tmp_path):
    _written, names = code_interpreter._write_input_files(
        str(tmp_path), [("../escape.txt", b"x"), ("sub/dir.txt", b"x"), ("..", b"x"), ("", b"x"), ("ok.txt", b"y")]
    )
    assert names == ["ok.txt"]
    assert not (tmp_path / "escape.txt").exists()


def test_writing_no_inputs_creates_nothing(tmp_path):
    assert code_interpreter._write_input_files(str(tmp_path), None) == (set(), [])
    assert code_interpreter._write_input_files(str(tmp_path), []) == (set(), [])
    assert not (tmp_path / code_interpreter.ATTACHMENTS_DIRNAME).exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "the sandbox wrapper scrubs USERPROFILE and matplotlib cannot resolve a home "
        "directory on Windows (pre-existing, unrelated to the mount); CI runs on Linux"
    ),
)
async def test_execute_python_mounts_the_inputs_and_never_collects_them_as_outputs(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "custom_tool_sandbox", "subprocess")
    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    code = (
        "import os, json\n"
        "print(sorted(os.listdir('attachments')))\n"
        "print(open('attachments/data-0f3c9a1b222243338444555566667777.csv').read().strip())\n"
        "print(json.load(open('attachments/index.json'))[0]['path'])\n"
        "open('out.txt', 'w').write('written by the code')\n"
    )
    index = json.dumps([{"file_id": _ID_A, "filename": "data.csv", "path": "attachments/data-0f3c9a1b222243338444555566667777.csv"}])
    result = await code_interpreter.execute_python(
        code,
        timeout=120,
        input_files=[("data-0f3c9a1b222243338444555566667777.csv", b"a,b\n1,2\n"), ("index.json", index.encode())],
    )
    assert result.get("error") in (None, ""), result
    assert "['data-0f3c9a1b222243338444555566667777.csv', 'index.json']" in result["stdout"]
    assert "a,b\n1,2" in result["stdout"]
    assert "attachments/data-0f3c9a1b222243338444555566667777.csv" in result["stdout"]
    assert sorted(f["filename"] for f in result["output_files"]) == ["out.txt"]
    assert result["mounted"] == ["data-0f3c9a1b222243338444555566667777.csv", "index.json"]


# -- The tool builder ----------------------------------------------------------


def _ref(data: bytes, filename: str, content_type: str = "text/plain") -> FileRef:
    return FileRef.from_bytes(data, filename, content_type)


async def test_collect_sandbox_inputs_names_every_file_and_writes_the_index():
    ctx = FileContext()
    html = _ref(b"<html>model</html>", "Model TD.html", "text/html")
    csv_a = _ref(b"a\n1\n", "data.csv", "text/csv")
    csv_b = _ref(b"b\n2\n", "data.csv", "text/csv")
    for ref in (html, csv_a, csv_b):
        ctx.add(ref)

    inputs, planned, skipped = await media.collect_sandbox_inputs(ctx)
    names = [name for name, _ in inputs]
    expected = [code_interpreter.safe_mount_name(r.filename, r.file_id) for r in (html, csv_a, csv_b)]
    assert names == [*expected, "index.json"]
    assert len(set(names)) == 4 and skipped == []
    assert list(planned) == expected
    assert planned[expected[0]] == (f"attachments/{expected[0]} (18 B)", "Model TD.html")
    index = json.loads(dict(inputs)["index.json"])
    assert [row["file_id"] for row in index] == [html.file_id, csv_a.file_id, csv_b.file_id]
    assert [row["path"] for row in index] == [f"attachments/{n}" for n in expected]
    assert index[1]["filename"] == index[2]["filename"] == "data.csv"
    assert index[0]["content_type"] == "text/html"
    # The path every other surface names for a file is the path it lands at.
    assert index[2]["path"] == code_interpreter.mount_path_hint(csv_b.filename, csv_b.file_id)


async def test_two_file_ids_sharing_their_first_digits_land_where_every_surface_says():
    """The tag was the first 8 hex digits, two ids sharing
    them collided, the collect step renamed the second mount - and every
    footer and refusal, which name a path from ONE file on its own, still
    advertised the unrenamed name, i.e. the OTHER file. The tag is the whole
    id: the names differ by construction and nothing is ever renamed."""
    ctx = FileContext()
    a = FileRef.from_bytes(b"a", "data.csv", "text/csv", file_id=_ID_A)
    b = FileRef.from_bytes(b"b", "data.csv", "text/csv", file_id=_ID_A[:8] + "-ffff-4fff-8fff-ffffffffffff")
    ctx.add(a)
    ctx.add(b)
    inputs, _planned, skipped = await media.collect_sandbox_inputs(ctx)
    mounted = dict(inputs)
    assert skipped == []
    for ref in (a, b):
        hint = code_interpreter.mount_path_hint(ref.filename, ref.file_id)
        assert mounted[hint.removeprefix("attachments/")] == ref.inline_data
    index = json.loads(mounted["index.json"])
    assert [row["path"] for row in index] == [code_interpreter.mount_path_hint(r.filename, r.file_id) for r in (a, b)]


def test_the_mount_tag_is_the_whole_id_whatever_its_spelling():
    tag = code_interpreter.mount_tag
    assert tag(_ID_A) == "0f3c9a1b222243338444555566667777"
    assert tag("file-" + _ID_A) == tag(_ID_A) == tag(_ID_A.upper()), "one file, one name"
    assert tag(_ID_A) != tag(_ID_A[:8] + "-ffff-4fff-8fff-ffffffffffff")
    # An id that is not a UUID is digested, never filtered down to a
    # prefix that another id could share.
    assert tag("a-b") != tag("ab") and tag("ABC") != tag("abc")
    assert tag("") == tag(None) == "file"


async def test_a_second_ref_of_the_same_file_is_mounted_once():
    ctx = FileContext()
    ctx.add(FileRef.from_bytes(b"a", "data.csv", "text/csv", file_id=_ID_A))
    ctx.add(FileRef.from_bytes(b"a", "data.csv", "text/csv", file_id="file-" + _ID_A))
    inputs, _planned, skipped = await media.collect_sandbox_inputs(ctx)
    assert [name for name, _ in inputs] == [code_interpreter.safe_mount_name("data.csv", _ID_A), "index.json"]
    assert skipped == ["data.csv"]


async def test_collect_sandbox_inputs_is_bounded_and_survives_a_load_failure(monkeypatch):
    monkeypatch.setattr(code_interpreter, "MAX_INPUT_TOTAL_BYTES", 30)
    ctx = FileContext()
    small = _ref(b"x" * 10, "small.txt")
    big = _ref(b"y" * 25, "big.txt")
    broken = _ref(b"z" * 5, "broken.txt")
    for ref in (small, big, broken):
        ctx.add(ref)
    original = FileContext.read_bytes

    async def _read(self, file_id):
        if file_id == broken.file_id:
            raise RuntimeError("storage down")
        return await original(self, file_id)

    monkeypatch.setattr(FileContext, "read_bytes", _read)
    inputs, planned, skipped = await media.collect_sandbox_inputs(ctx)
    assert [name for name, _ in inputs] == [code_interpreter.safe_mount_name("small.txt", small.file_id), "index.json"]
    assert skipped == ["big.txt", "broken.txt"]
    assert len(planned) == 1


async def test_the_file_count_is_bounded(monkeypatch):
    monkeypatch.setattr(code_interpreter, "MAX_INPUT_FILES", 2)
    ctx = FileContext()
    for i in range(4):
        ctx.add(_ref(f"row {i}".encode(), f"f{i}.txt"))
    inputs, _planned, skipped = await media.collect_sandbox_inputs(ctx)
    assert len(inputs) == 3 and skipped == ["f2.txt", "f3.txt"]


async def test_only_the_turns_own_files_are_read_through_its_context(monkeypatch):
    """Authorization stays the FileContext's: the mount reads exactly the
    files the context carries, through its own ``read_bytes``."""
    ctx = FileContext()
    mine = _ref(b"mine", "mine.txt")
    ctx.add(mine)
    reads: list[str] = []
    original = FileContext.read_bytes

    async def _read(self, file_id):
        reads.append(file_id)
        return await original(self, file_id)

    monkeypatch.setattr(FileContext, "read_bytes", _read)
    await media.collect_sandbox_inputs(ctx)
    assert reads == [mine.file_id]


async def test_an_empty_context_mounts_nothing():
    assert await media.collect_sandbox_inputs(None) == ([], {}, [])
    assert await media.collect_sandbox_inputs(FileContext()) == ([], {}, [])


def test_the_mount_report_announces_only_what_the_sandbox_wrote():
    planned = {
        "a-1.csv": ("attachments/a-1.csv (1 B)", "a.csv"),
        "b-2.csv": ("attachments/b-2.csv (1 B)", "b.csv"),
    }
    mounted, skipped = media._mount_report(planned, ["huge.bin"], {"mounted": ["a-1.csv", "index.json"]})
    assert mounted == ["attachments/a-1.csv (1 B)"]
    assert skipped == ["huge.bin", "b.csv"], "a failed write is named like a skipped file: by its filename"
    # No word from the sandbox confirms nothing: the OpenSandbox early returns
    # (unreachable, quota spent, not created) announced every planned file as
    # an available path before they carried the key (independent review).
    mounted, skipped = media._mount_report(planned, [], {"stdout": "", "error": "OpenSandbox server is not reachable"})
    assert mounted == [] and skipped == ["a.csv", "b.csv"]
    # Nothing landed at all (the sandbox never started).
    mounted, skipped = media._mount_report(planned, [], {"mounted": []})
    assert mounted == [] and skipped == ["a.csv", "b.csv"]


def test_the_mount_line_is_bounded():
    mounted = [f"attachments/f{i}.csv (1 B)" for i in range(20)]
    line = media._format_mount_line(mounted, [f"huge{i}.bin" for i in range(9)])
    assert "+8 more; exact paths in attachments/index.json" in line
    assert "not mounted (too many, too large or failed to load): huge0.bin" in line and ", +3 more]" in line
    assert media._format_mount_line([], []) == ""
    assert media._format_mount_line([], ["x.bin"]).startswith("[Conversation files mounted for this run: none;")


def _capture_execute(monkeypatch, result: dict | None = None) -> dict:
    """``execute_python`` as the real one behaves: it awaits the loader (past
    its gates) and reports the names it wrote, unless *result* says otherwise."""
    captured: dict = {}

    async def _fake_execute_python(code, **kwargs):
        captured["code"] = code
        loader = kwargs.get("input_files")
        inputs = (await loader()) if callable(loader) else loader
        captured["input_files"] = inputs
        default = {"stdout": "42", "stderr": "", "error": None, "images": [], "output_files": []}
        default["mounted"] = [name for name, _ in inputs or []]
        return result or default

    monkeypatch.setattr("app.services.code_interpreter.execute_python", _fake_execute_python)
    return captured


def _executor_with(*refs: FileRef):
    defs, executor = media.build_code_interpreter_tool(user_id=str(uuid.uuid4()))
    ctx = FileContext()
    for ref in refs:
        ctx.add(ref)
    executor.file_context = ctx
    return defs, executor


async def test_the_files_are_mounted_when_the_code_opens_them_and_the_result_says_so(monkeypatch):
    captured = _capture_execute(monkeypatch)
    ref = _ref(b"<html>model</html>", "Model TD.html", "text/html")
    defs, executor = _executor_with(ref)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="code_interpreter", arguments={"code": "print(open('attachments/index.json').read())"})
    )
    mount = code_interpreter.safe_mount_name(ref.filename, ref.file_id)
    assert [name for name, _ in captured["input_files"]] == [mount, "index.json"]
    assert result.content.startswith(f"[Conversation files mounted for this run: attachments/{mount} (18 B)")
    assert "42" in result.content
    description = next(d for d in defs if d.name == "code_interpreter").description
    assert "attachments/ (attachments/index.json maps file ids to paths)" in description
    assert "instead of reading it window by window" in description


async def test_a_run_that_does_not_open_the_files_downloads_nothing(monkeypatch):
    """Every run eagerly downloaded up to 64 MB of
    conversation files, even ``print(2 + 2)``."""
    captured = _capture_execute(
        monkeypatch, {"stdout": "4", "stderr": "", "error": None, "images": [], "output_files": []}
    )
    ref = _ref(b"x" * 1000, "big.csv", "text/csv")
    _defs, executor = _executor_with(ref)
    reads: list[str] = []
    original = FileContext.read_bytes

    async def _read(self, file_id):
        reads.append(file_id)
        return await original(self, file_id)

    monkeypatch.setattr(FileContext, "read_bytes", _read)
    result = await executor.execute_tool(ToolCall(id="c1", name="code_interpreter", arguments={"code": "print(2 + 2)"}))
    assert captured["input_files"] is None and reads == []
    assert "mounted" not in result.content


async def test_a_file_not_found_without_the_mount_points_at_the_exact_paths(monkeypatch):
    _capture_execute(
        monkeypatch,
        {
            "stdout": "",
            "stderr": "",
            "error": "FileNotFoundError: [Errno 2] No such file or directory: 'Model TD.html'",
            "images": [],
            "output_files": [],
        },
    )
    ref = _ref(b"<html>model</html>", "Model TD.html", "text/html")
    _defs, executor = _executor_with(ref)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="code_interpreter", arguments={"code": "open('Model TD.html').read()"})
    )
    assert "[The conversation files are mounted when the code opens them under attachments/: " in result.content
    assert code_interpreter.mount_path_hint(ref.filename, ref.file_id) in result.content


_INPUT_NOT_FOUND = {
    "stdout": "",
    "stderr": "",
    "error": "FileNotFoundError: [Errno 2] No such file or directory: 'attachments/input.json'",
    "images": [],
    "output_files": [],
}


async def test_the_input_document_path_in_a_run_without_one_says_none_is_mounted(monkeypatch):
    _capture_execute(monkeypatch, _INPUT_NOT_FOUND)
    _defs, executor = _executor_with()
    code = "import json\njson.load(open('attachments/input.json'))"
    result = await executor.execute_tool(ToolCall(id="c1", name="code_interpreter", arguments={"code": code}))
    assert media.NO_INPUT_DOCUMENT_HINT in result.content


async def test_the_input_document_hint_comes_with_the_mount_line_of_the_conversation_files(monkeypatch):
    _capture_execute(monkeypatch, {**_INPUT_NOT_FOUND, "mounted": ["report.txt", "index.json"]})
    _defs, executor = _executor_with(_ref(b"x", "report.txt"))
    code = "import json\njson.load(open('attachments/input.json'))"
    result = await executor.execute_tool(ToolCall(id="c1", name="code_interpreter", arguments={"code": code}))
    assert media.NO_INPUT_DOCUMENT_HINT in result.content


async def test_a_run_that_holds_the_input_document_gets_no_absent_hint(monkeypatch):
    _capture_execute(monkeypatch, _INPUT_NOT_FOUND)
    _defs, executor = _executor_with()
    executor.file_context.add_sandbox_input("input.json", b"{}")
    code = "import json\njson.load(open('attachments/input.json'))"
    result = await executor.execute_tool(ToolCall(id="c1", name="code_interpreter", arguments={"code": code}))
    assert media.NO_INPUT_DOCUMENT_HINT not in result.content


async def test_a_mount_the_sandbox_could_not_write_is_never_announced(monkeypatch):
    _capture_execute(
        monkeypatch,
        {"stdout": "x", "stderr": "", "error": None, "images": [], "output_files": [], "mounted": ["index.json"]},
    )
    ref = _ref(b"<html>model</html>", "Model TD.html", "text/html")
    _defs, executor = _executor_with(ref)
    result = await executor.execute_tool(
        ToolCall(id="c1", name="code_interpreter", arguments={"code": "open('attachments/index.json')"})
    )
    assert result.content.startswith("[Conversation files mounted for this run: none; not mounted")
    assert "Model TD.html" in result.content.split("not mounted", 1)[1]
    assert f"attachments/{code_interpreter.safe_mount_name(ref.filename, ref.file_id)} (" not in result.content


async def test_without_files_the_result_has_no_mount_line(monkeypatch):
    _capture_execute(monkeypatch, {"stdout": "ok", "stderr": "", "error": None, "images": [], "output_files": []})
    _defs, executor = media.build_code_interpreter_tool()
    result = await executor.execute_tool(
        ToolCall(id="c1", name="code_interpreter", arguments={"code": "print(open('attachments/x').read())"})
    )
    assert "mounted" not in result.content
