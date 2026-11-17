"""A resumed turn's note says whether the sandbox writes the cut segment started landed (#8851, part B).

``sandbox_workspace.landed`` reads the workspace: a ``sandbox_write_file``
landed when the path holds the content's sha256, a ``sandbox_edit_file`` when
each replacement is in the file and its find text gone. The verdict is named
in the note, and a write that did not land runs on its first repeat.
"""

from __future__ import annotations

import uuid

from app.llm.base import ChatMessage
from app.services.sandbox_workspace import landed
from app.services.sandbox_workspace.manifest import Limits
from app.services.sandbox_workspace.session import WorkspaceSession
from app.services.sandbox_workspace.store import MemoryWorkspaceStore, WorkspaceScope
from app.services.turn_checkpoint import (
    IN_FLIGHT_ROLE,
    ResumedWork,
    _in_flight_of,
    in_flight_calls,
    in_flight_writes,
    resume_note,
)
from app.tools.base import ToolCall


def _in_flight(name: str, **arguments: object) -> list[dict]:
    return in_flight_calls(
        [ChatMessage(role="assistant", content="", tool_calls=[ToolCall(id="b", name=name, arguments=dict(arguments))])]
    )


def _session() -> WorkspaceSession:
    scope = WorkspaceScope(user_id=uuid.UUID(int=7), conversation_id=uuid.uuid4())
    return WorkspaceSession(scope, store=MemoryWorkspaceStore(), limits=Limits(quota_bytes=1 << 20, max_files=100))


def test_an_edit_verdict_reads_the_find_and_replace_texts():
    edits = [{"find": "old()", "replace": "new()"}]
    assert landed.edit_verdict(edits, "x = new()\n") is True
    assert landed.edit_verdict(edits, "x = old()\n") is False
    # Neither text: the file changed since, no verdict.
    assert landed.edit_verdict(edits, "x = other()\n") is None
    assert landed.edit_verdict(edits, None) is None
    # A deletion landed when the find text is gone.
    assert landed.edit_verdict([{"find": "debug()\n", "replace": ""}], "a\n") is True
    # An insertion keeps its find text: the replacement tells.
    insert = [{"find": "a\n", "replace": "a\nb\n"}]
    assert landed.edit_verdict(insert, "a\nb\n") is True
    assert landed.edit_verdict(insert, "a\nc\n") is False
    # A line-range edit and a cut argument cannot be told.
    assert landed.edit_verdict([{"start_line": 1, "replace": "x"}], "a") is None
    cut = [{"find": "a\n... [cut for the replay: 9 more characters]", "replace": "b"}]
    assert landed.edit_verdict(cut, "b") is None
    # Edits that disagree: no verdict.
    assert landed.edit_verdict([{"find": "a", "replace": "b"}, {"find": "c", "replace": "d"}], "b c") is None


def test_a_write_verdict_reads_the_sha256_of_the_path():
    import hashlib

    digest = hashlib.sha256(b"hello").hexdigest()
    assert landed.write_verdict("hello", digest) is True
    assert landed.write_verdict("hello", None) is False
    assert landed.write_verdict("hello", "0" * 64) is None


async def test_a_landed_edit_is_named_landed_and_stays_guarded():
    session = _session()
    await session.put_bytes("co/a.py", b"x = new()\n")
    calls = _in_flight("sandbox_edit_file", path="workspace/co/a.py", edits=[{"find": "old()", "replace": "new()"}])

    assert await landed.judge(calls, session) == 1
    assert calls[0]["landed"] is True
    note = resume_note(ResumedWork(found=True, in_flight=calls))
    assert note is not None
    assert "it landed (workspace/co/a.py holds its change): do not repeat it" in note.content
    # Still guarded: a repeat would fail on a find text that is gone.
    assert [c["name"] for c in in_flight_writes([note]).values()] == ["sandbox_edit_file"]


async def test_an_edit_that_did_not_land_runs_on_its_first_repeat():
    session = _session()
    await session.put_bytes("co/a.py", b"x = old()\n")
    calls = _in_flight("sandbox_edit_file", path="co/a.py", edits=[{"find": "old()", "replace": "new()"}])

    assert await landed.judge(calls, session) == 1
    note = resume_note(ResumedWork(found=True, in_flight=calls))
    assert note is not None
    assert "it did not land (workspace/co/a.py still holds every find text): repeat it as it was" in note.content
    assert in_flight_writes([note]) == {}


async def test_a_write_file_verdict_and_an_untold_call_are_kept_apart():
    session = _session()
    await session.put_bytes("co/new.py", b"print(1)\n")
    calls = _in_flight("sandbox_write_file", path="co/new.py", content="print(1)\n")
    absent = _in_flight("sandbox_write_file", path="co/other.py", content="x")
    other = _in_flight("create_github_commit", message="fix")

    assert await landed.judge([*calls, *absent, *other], session) == 2
    assert calls[0]["landed"] is True and absent[0]["landed"] is False
    assert "landed" not in other[0]


async def test_a_verdict_survives_the_stored_checkpoint():
    calls = _in_flight("sandbox_edit_file", path="co/a.py", edits=[{"find": "a", "replace": "b"}])
    calls[0].update(landed=False, outcome="it did not land")
    [read] = _in_flight_of([{"role": IN_FLIGHT_ROLE, "content": "", "calls": calls}])
    assert read["landed"] is False and read["outcome"] == "it did not land"


async def test_a_session_that_cannot_be_read_judges_nothing():
    class _Broken:
        async def entries(self, *, refresh: bool = False):
            raise RuntimeError("storage down")

    calls = _in_flight("sandbox_edit_file", path="co/a.py", edits=[{"find": "a", "replace": "b"}])
    assert await landed.judge(calls, _Broken()) == 0
    assert "landed" not in calls[0]


async def test_judge_for_resume_without_a_user_or_a_sandbox_write_reads_nothing():
    calls = _in_flight("sandbox_edit_file", path="co/a.py", edits=[{"find": "a", "replace": "b"}])
    assert await landed.judge_for_resume(calls, uuid.uuid4(), None) == 0
    assert await landed.judge_for_resume(_in_flight("create_github_pr", title="t"), uuid.uuid4(), uuid.uuid4()) == 0
