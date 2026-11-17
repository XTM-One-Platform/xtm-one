"""A resumed turn never applies an interrupted sandbox edit a second time, reworded or not (#12097).

Run 94d4ecca (15 Nov 2026) resumed twice across a ``sandbox_edit_file``
insertion (the find kept inside the replace) and reworded it on each attempt:
the reworded call missed the in-flight digest, the find text was still there,
and the file ended with three copies of one bullet. ``sandbox_edit_file`` and
``sandbox_replace_in_files`` now declare a ``landed_probe`` read from the
workspace files and a ``landed_key`` (the path and each edit's anchor; the
scope and pattern of a replace): a repeat at the same anchor is answered
"already applied" when the interrupted call landed; an edit elsewhere runs.
"""

from __future__ import annotations

import re
import uuid

from app.llm.base import ChatMessage
from app.services.sandbox_workspace import landed
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope
from app.services.turn_checkpoint import ResumedWork, in_flight_calls, resume_note
from app.tools.agent_loop import run_agent_loop
from app.tools.base import ToolDefinition, ToolResult
from tests.test_turn_resume_in_flight_repeat import _round, _Script

INSERTED = "- one\n- two\n- three\n"
FIRST = {"path": "co/notes.md", "edits": [{"find": "- one\n", "replace": "- one\n- two\n"}]}
REWORDED = {"path": "co/notes.md", "edits": [{"find": "- one\n", "replace": "- one\n- two, reworded\n"}]}
ELSEWHERE = {"path": "co/notes.md", "edits": [{"find": "- three\n", "replace": "- three\n- four\n"}]}


def _session() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=1 << 20, max_files=100))


def test_an_insert_after_line_and_a_range_edit_are_told_from_their_lines():
    insert = [{"insert_after_line": 1, "expect_first": "- one", "replace": "- two\n"}]
    assert landed.edit_verdict(insert, INSERTED) is True
    assert landed.edit_verdict(insert, "- one\n- three\n") is False
    # The anchor line moved and the block is not there: cannot tell.
    assert landed.edit_verdict(insert, "- zero\n- three\n") is None
    rng = [{"start_line": 2, "end_line": 2, "expect_first": "- old", "replace": "- two\n"}]
    assert landed.edit_verdict(rng, INSERTED) is True
    assert landed.edit_verdict(rng, "- one\n- old\n") is False
    # A deletion by range, and line edits combined, are not told.
    assert landed.edit_verdict([{"start_line": 1, "expect_first": "- one", "replace": ""}], INSERTED) is None
    assert landed.edit_verdict([*insert, *rng], INSERTED) is None


def test_the_edit_key_is_the_path_and_the_anchors_whatever_the_inserted_text():
    assert landed.edit_key(FIRST) == landed.edit_key(REWORDED)
    assert landed.edit_key(FIRST) != landed.edit_key(ELSEWHERE)
    assert landed.edit_key({**FIRST, "path": "co/other.md"}) != landed.edit_key(FIRST)
    insert = {"path": "co/n.md", "edits": [{"insert_after_line": 3, "expect_first": "x", "replace": "a"}]}
    assert landed.edit_key(insert) == landed.edit_key({**insert, "edits": [{**insert["edits"][0], "replace": "b"}]})
    assert landed.edit_key({"path": "co/n.md", "edits": []}) is None
    assert landed.edit_key({"path": "../x", "edits": FIRST["edits"]}) is None


async def test_the_edit_probe_reads_the_file():
    session = _session()
    await session.put_bytes("co/notes.md", INSERTED.encode())
    found = await landed.edit_probe(session, FIRST)
    assert isinstance(found, str) and found.startswith("already applied: workspace/co/notes.md")
    assert await landed.edit_probe(session, ELSEWHERE) is False
    assert await landed.edit_probe(session, {**FIRST, "path": "co/absent.md"}) is None


def test_a_replace_verdict_reads_every_match():
    regex = re.compile(r"foo", re.MULTILINE)
    assert landed.replace_verdict([("a", "foobar x foobar")], regex, "foobar") is True
    assert landed.replace_verdict([("a", "foo x foo")], regex, "foobar") is False
    assert landed.replace_verdict([("a", "foobar x foo")], regex, "foobar") is None
    assert landed.replace_verdict([("a", "nothing")], regex, "foobar") is None
    key = {"pattern": "foo", "replacement": "foobar", "path_prefix": "co"}
    assert landed.replace_key(key) == landed.replace_key({**key, "replacement": "foobaz"})
    assert landed.replace_dry_run({**key, "dry_run": True}) and not landed.replace_dry_run(key)


async def test_the_replace_probe_reads_the_files_of_its_scope():
    session = _session()
    await session.put_bytes("co/a.py", b"foobar()\n")
    found = await landed.replace_probe(session, {"pattern": "foo", "replacement": "foobar", "path_prefix": "co"})
    assert isinstance(found, str) and found.startswith("already applied")
    await session.put_bytes("co/b.py", b"foo()\n")
    assert await landed.replace_probe(session, {"pattern": "foo", "replacement": "foobar", "path_prefix": "co"}) is None
    assert await landed.replace_probe(session, {"pattern": "foo", "replacement": "x", "dry_run": True}) is None


async def _loop(text: str, interrupted: dict, repeat: dict) -> tuple[list[ChatMessage], list[dict]]:
    """A resumed turn whose interrupted call was *interrupted*, the model asking *repeat*, over a file holding *text*."""
    session = _session()
    await session.put_bytes("co/notes.md", text.encode())
    ran: list[dict] = []

    async def probe(arguments):
        return await landed.edit_probe(session, arguments)

    tool = ToolDefinition(name="sandbox_edit_file", description="e", landed_probe=probe, landed_key=landed.edit_key)

    class _Executor:
        async def execute_tool(self, call):
            ran.append(dict(call.arguments))
            return ToolResult(tool_call_id=call.id, content="Edited")

    note = resume_note(ResumedWork(found=True, in_flight=in_flight_calls(_round("b", tool.name, None, **interrupted))))
    assert note is not None
    messages = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="go"), note]
    await run_agent_loop(
        llm=_Script(tool.name, repeat), messages=messages, tools=[tool], executor=_Executor(), max_iterations=6
    )
    return [m for m in messages if m.role == "tool"], ran


async def test_an_identical_insert_replayed_is_answered_already_applied():
    answers, ran = await _loop(INSERTED, FIRST, FIRST)
    assert answers[0].not_run == "in_flight_landed"
    assert "already applied" in answers[0].content
    # The model asked again after the answer: only that second call ran.
    assert len(ran) == 1


async def test_a_reworded_insert_at_the_same_anchor_is_answered_already_applied():
    answers, ran = await _loop(INSERTED, FIRST, REWORDED)
    assert answers[0].not_run == "in_flight_landed"
    assert "already applied" in answers[0].content and "reworded" in answers[0].content
    assert len(ran) == 1


async def test_a_range_edit_replayed_is_answered_already_applied():
    edit = {"path": "co/notes.md", "edits": [{"start_line": 2, "expect_first": "- old", "replace": "- two\n"}]}
    answers, ran = await _loop(INSERTED, edit, edit)
    assert answers[0].not_run == "in_flight_landed"
    assert len(ran) == 1


async def test_a_genuine_second_insert_elsewhere_runs():
    answers, ran = await _loop(INSERTED, FIRST, ELSEWHERE)
    assert answers[0].not_run is None
    assert ran[0] == ELSEWHERE


async def test_an_insert_that_did_not_land_runs_on_its_first_repeat():
    answers, ran = await _loop("- one\n- three\n", FIRST, FIRST)
    assert answers[0].not_run is None
    assert ran[0] == FIRST
