"""A resumed turn reads an interrupted write back by its natural key and is handed what it created.

14 Nov 2026: a non-graceful resume named ``create_github_issue`` as started,
outcome not recorded; the first attempt had created xtm-one #11832, and the
resumed turn, which reworded the title, created #11833. The guard matched a
repeat by its digest only, so a reworded repeat ran. Now (#11847) every
in-flight write whose tool declares a ``landed_probe`` is read back at the
start of the resumed turn and named on the resume note, and a reworded repeat
on the same natural key (``landed_key``) is answered with what landed once.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.integrations.providers.github.landed import (
    branch_key,
    comment_key,
    comment_match,
    issue_key,
    pull_key,
    pull_match,
)
from app.llm.base import ChatMessage
from app.services import turn_checkpoint
from app.services.turn_checkpoint import ResumedWork, in_flight_calls, landed_note_lines, resume_note
from app.tools.agent_loop import run_agent_loop
from app.tools.base import ToolDefinition, ToolResult, landed_key_of
from tests.test_turn_resume_in_flight_repeat import _round, _Script

NOW = datetime(2026, 11, 14, 15, 30, tzinfo=UTC)
RECENT = (NOW - timedelta(minutes=5)).isoformat()
OLD = (NOW - timedelta(days=1)).isoformat()
FIRST = {"repo": "o/r", "title": "fix(opencti): playbook graph tools hide the graph"}
REWORDED = {"repo": "o/r", "title": "fix(opencti): playbook graph tools hide the graph, misshape positions"}


async def _resumed_loop(
    tool: ToolDefinition, interrupted: dict, asked: dict
) -> tuple[list[ChatMessage], list[dict], ChatMessage]:
    """A resumed turn whose attempt was cut in *interrupted*; the model then asks *asked* twice."""
    ran: list[dict] = []

    class _Executor:
        async def execute_tool(self, call):
            ran.append(dict(call.arguments))
            return ToolResult(tool_call_id=call.id, content=f"Issue #{11832 + len(ran)} created")

    note = resume_note(ResumedWork(found=True, in_flight=in_flight_calls(_round("b", tool.name, None, **interrupted))))
    assert note is not None
    messages = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="go"), note]
    await run_agent_loop(
        llm=_Script(tool.name, asked), messages=messages, tools=[tool], executor=_Executor(), max_iterations=6
    )
    return [m for m in messages if m.role == "tool"], ran, note


def _issue_tool(probe) -> ToolDefinition:
    return ToolDefinition(name="create_github_issue", description="c", landed_probe=probe, landed_key=issue_key)


async def test_a_reworded_repeat_of_a_landed_issue_is_handed_the_issue_not_a_second_create():
    probed: list[dict] = []

    async def landed(arguments):
        probed.append(dict(arguments))
        # The first attempt's issue: found only under its own title.
        return (
            "issue #11832 (https://github.com/o/r/issues/11832)" if arguments.get("title") == FIRST["title"] else False
        )

    answers, ran, note = await _resumed_loop(_issue_tool(landed), FIRST, REWORDED)
    assert answers[0].not_run == "in_flight_landed"
    assert "issue #11832" in answers[0].content and "do not create it a second time" in answers[0].content
    # The reworded call was answered once; asked again, it runs (another object, the model's call).
    assert len(ran) == 1
    # Every probe read the interrupted call's own arguments, never the reworded title.
    assert probed and all(p["title"] == FIRST["title"] for p in probed)
    # The resume note the model read first already named the landed issue.
    assert "Read back since: create_github_issue landed: issue #11832" in note.content


async def test_an_identical_repeat_of_a_landed_issue_is_answered_from_the_read_back():
    calls = 0

    async def landed(_arguments):
        nonlocal calls
        calls += 1
        return "issue #11832 (https://github.com/o/r/issues/11832)"

    answers, ran, _note = await _resumed_loop(_issue_tool(landed), FIRST, FIRST)
    assert answers[0].not_run == "in_flight_landed"
    assert len(ran) == 1
    # Read back once at the start; the guard reused that verdict.
    assert calls == 1


async def test_a_reworded_repeat_runs_when_the_interrupted_write_did_not_land_or_cannot_be_told():
    for verdict in (False, None):

        async def probe(_arguments, verdict=verdict):
            return verdict

        answers, ran, note = await _resumed_loop(_issue_tool(probe), FIRST, REWORDED)
        assert answers[0].not_run is None
        assert ran[0] == REWORDED
        assert "Read back since" not in note.content


async def test_a_write_on_another_key_is_not_matched():
    async def landed(_arguments):
        return "issue #11832"

    other = {"repo": "o/other", "title": REWORDED["title"]}
    answers, ran, _note = await _resumed_loop(_issue_tool(landed), FIRST, other)
    assert answers[0].not_run is None
    assert ran[0] == other


def test_the_natural_keys():
    assert issue_key({"repo": "O/R", "title": "a"}) == issue_key({"repo": "o/r", "title": "b"}) == "o/r"
    assert pull_key({"repo": "o/r", "head": "fix/1", "title": "x"}) == pull_key({"repo": "o/r", "head": "fix/1"})
    assert pull_key({"repo": "o/r", "head": "fix/1"}) != pull_key({"repo": "o/r", "head": "fix/2"})
    assert branch_key({"repo": "o/r", "branch": "b"}) == "o/r:b"
    assert comment_key({"repo": "o/r", "issue_number": 7, "body": "x"}) == "o/r#7"
    assert pull_key({"repo": "o/r"}) is None and issue_key({"repo": "bad"}) is None

    def boom(_a):
        raise RuntimeError("x")

    assert landed_key_of(ToolDefinition(name="w", description="w", landed_key=boom), {}) is None
    assert landed_key_of(ToolDefinition(name="w", description="w"), {}) is None


def test_pull_and_comment_matches():
    pulls = [
        {"number": 5, "head": {"ref": "fix/2"}, "user": {"login": "sam"}, "created_at": RECENT, "state": "open"},
        {
            "number": 6,
            "head": {"ref": "fix/1"},
            "user": {"login": "Sam"},
            "created_at": RECENT,
            "state": "open",
            "html_url": "https://github.com/o/r/pull/6",
        },
    ]
    assert "pull request #6" in str(pull_match(pulls, "fix/1", "sam", NOW))
    assert pull_match(pulls, "fix/3", "sam", NOW) is False
    assert pull_match([{**pulls[1], "created_at": OLD}], "fix/1", "sam", NOW) is False
    assert pull_match(None, "fix/1", "sam", NOW) is None
    comments = [
        {"body": "LGTM", "user": {"login": "sam"}, "created_at": RECENT, "html_url": "u1"},
        {"body": "other", "user": {"login": "sam"}, "created_at": RECENT, "html_url": "u2"},
    ]
    assert "comment u1" in str(comment_match(comments, "LGTM", "sam", NOW))
    assert comment_match(comments, "LGTM", "bob", NOW) is False
    assert comment_match({"message": "x"}, "LGTM", "sam", NOW) is None


def test_the_note_names_the_read_back_before_its_clock_sentence():
    content = "[Resumed: ...\n1 call(s) were started ...\nIt is now 16:00 UTC.]"
    writes = {"d1": {"name": "create_github_pr"}}
    out = landed_note_lines(content, writes, {"d1": "pull request #6"})
    assert out.splitlines()[-2] == (
        "Read back since: create_github_pr landed: pull request #6. Use each as that call's outcome; do not create it again."
    )
    assert out.endswith("It is now 16:00 UTC.]")
    assert landed_note_lines(content, writes, {}) == content


def test_the_github_writes_declare_their_probe_and_key():
    from app.integrations.providers.github.branches import register as register_branches
    from app.integrations.providers.github.issues import register as register_issues
    from app.integrations.providers.github.pulls import register as register_pulls
    from app.tools.base import CallableToolExecutor

    tools = {
        t.name: t
        for register in (register_issues, register_pulls, register_branches)
        for t in register({}, CallableToolExecutor(), None)
    }
    for name in ("create_github_issue", "create_github_pr", "create_github_branch", "add_github_comment"):
        assert tools[name].landed_probe is not None and tools[name].landed_key is not None, name
    assert tools["update_github_pr"].landed_key is None


async def test_the_note_names_a_kept_file_only_when_the_resumed_attempt_can_open_it(monkeypatch):
    kept, purged = str(uuid.uuid4()), str(uuid.uuid4())

    async def rows(ids, *, user_id=None, **_kw):
        assert user_id == "u"
        return [SimpleNamespace(id=uuid.UUID(kept))] if kept in ids else []

    from app.services import sub_agent_files

    monkeypatch.setattr(sub_agent_files, "load_attachment_rows", rows)
    spilled = [
        {"call_id": "a", "name": "read", "file_id": kept, "chars": 30_000},
        {"call_id": "b", "name": "read", "file_id": purged, "chars": 30_000},
    ]
    checked = await turn_checkpoint.readable_spills(spilled, "u")
    assert [s["file_id"] for s in checked] == [kept, None]
    note = resume_note(
        ResumedWork(found=True, messages=[*_round("a", "read", "x"), *_round("b", "read", "y")], spilled=checked)
    )
    assert note is not None
    assert f"conversation file {kept}" in note.content and purged not in note.content
    # No user: nothing is checked, the entries are kept as they are.
    assert await turn_checkpoint.readable_spills(spilled, None) == spilled
