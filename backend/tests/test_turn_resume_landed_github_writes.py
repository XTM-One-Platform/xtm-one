"""A resumed turn's repeat of a GitHub write that landed is answered with what landed.

Desk run 699b914e repeated a ``create_github_issue`` its interrupted attempt
had landed and created a duplicate (#8834 of #8833); run 3f46bdcc found its
``create_github_commit`` had landed only by re-reading the branch. The tool now
declares a ``landed_probe``: the in-flight repeat guard asks it, answers a
landed write with the issue or commit, runs a write that did not land, and
holds back once a write it cannot tell (#8851, slice 3).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.integrations.providers.github.landed import commit_match, issue_match, subject_of
from app.llm.base import ChatMessage
from app.services.turn_checkpoint import ResumedWork, in_flight_calls, resume_note
from app.tools.agent_loop import run_agent_loop
from app.tools.base import ToolDefinition, ToolResult, probe_landed
from tests.test_turn_resume_in_flight_repeat import _round, _Script

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
RECENT = (NOW - timedelta(minutes=5)).isoformat()
OLD = (NOW - timedelta(days=2)).isoformat()
ISSUE_ARGS = {"repo": "o/r", "title": "fix(x): a bug"}


def _issue(number: int, title: str, login: str, created: str, **extra: object) -> dict:
    return {
        "number": number,
        "title": title,
        "user": {"login": login},
        "created_at": created,
        "html_url": f"https://github.com/o/r/issues/{number}",
        **extra,
    }


def test_an_issue_with_the_same_title_by_the_account_lately_landed():
    issues = [_issue(7, "other", "sam", RECENT), _issue(8, "fix(x): a bug", "Sam", RECENT)]
    assert "issue #8" in str(issue_match(issues, "fix(x): a bug", "sam", NOW))


def test_an_old_issue_another_author_or_a_pull_request_is_not_the_landed_one():
    assert issue_match([_issue(8, "fix(x): a bug", "sam", OLD)], "fix(x): a bug", "sam", NOW) is False
    assert issue_match([_issue(8, "fix(x): a bug", "bob", RECENT)], "fix(x): a bug", "sam", NOW) is False
    pr = _issue(8, "fix(x): a bug", "sam", RECENT, pull_request={"url": "u"})
    assert issue_match([pr], "fix(x): a bug", "sam", NOW) is False
    assert issue_match({"message": "Not Found"}, "fix(x): a bug", "sam", NOW) is None


def test_a_commit_with_the_same_subject_lately_landed():
    commits = [
        {
            "sha": "3ee5a114aaaabbbbcccc",
            "html_url": "https://github.com/o/r/commit/3ee5a114",
            "commit": {"message": "fix(a): b\n\nbody", "author": {"date": RECENT}},
        }
    ]
    assert subject_of("fix(a): b\n\nbody") == "fix(a): b"
    assert "commit 3ee5a114aaaa" in str(commit_match(commits, "fix(a): b", NOW))
    # The tool wraps a non-conventional subject: the wrapped form is the same commit.
    wrapped = [{**commits[0], "commit": {"message": "chore(github): wip", "author": {"date": RECENT}}}]
    assert commit_match(wrapped, "wip", NOW)
    old = [{**commits[0], "commit": {"message": "fix(a): b", "author": {"date": OLD}}}]
    assert commit_match(old, "fix(a): b", NOW) is False
    assert commit_match(None, "fix(a): b", NOW) is None


async def test_probe_landed_fails_safe():
    async def boom(_a):
        raise RuntimeError("down")

    async def empty(_a):
        return "  "

    async def no(_a):
        return False

    assert await probe_landed(None, {}) is None
    assert await probe_landed(ToolDefinition(name="w", description="w"), {}) is None
    assert await probe_landed(ToolDefinition(name="w", description="w", landed_probe=boom), {}) is None
    assert await probe_landed(ToolDefinition(name="w", description="w", landed_probe=empty), {}) is None
    assert await probe_landed(ToolDefinition(name="w", description="w", landed_probe=no), {}) is False


async def _loop(tool: ToolDefinition) -> tuple[list[ChatMessage], list[dict]]:
    ran: list[dict] = []

    class _Executor:
        async def execute_tool(self, call):
            ran.append(dict(call.arguments))
            return ToolResult(tool_call_id=call.id, content=f"Issue #{8 + len(ran)} created")

    note = resume_note(ResumedWork(found=True, in_flight=in_flight_calls(_round("b", tool.name, None, **ISSUE_ARGS))))
    assert note is not None
    messages = [ChatMessage(role="system", content="s"), ChatMessage(role="user", content="go"), note]
    await run_agent_loop(
        llm=_Script(tool.name, ISSUE_ARGS), messages=messages, tools=[tool], executor=_Executor(), max_iterations=6
    )
    return [m for m in messages if m.role == "tool"], ran


async def test_the_loop_answers_a_landed_issue_with_it_instead_of_a_duplicate():
    async def landed(_a):
        return "issue #8 (https://github.com/o/r/issues/8)"

    answers, ran = await _loop(ToolDefinition(name="create_github_issue", description="c", landed_probe=landed))
    assert answers[0].not_run == "in_flight_landed"
    # The model asked again after the landed answer: only that second call ran.
    assert len(ran) == 1
    assert "issue #8" in answers[0].content and "do not create it a second time" in answers[0].content


async def test_the_loop_runs_an_issue_that_did_not_land_on_the_first_repeat():
    async def absent(_a):
        return False

    answers, ran = await _loop(ToolDefinition(name="create_github_issue", description="c", landed_probe=absent))
    assert answers and all(m.not_run is None for m in answers[:1])
    assert ran and ran[0] == ISSUE_ARGS


async def test_the_loop_holds_back_once_what_the_probe_cannot_tell():
    async def unknown(_a):
        return None

    answers, ran = await _loop(ToolDefinition(name="create_github_issue", description="c", landed_probe=unknown))
    assert answers[0].not_run == "in_flight_repeat"
    assert len(ran) == 1


def test_the_two_github_writes_declare_their_probe():
    from app.integrations.providers.github.contents import register as register_contents
    from app.integrations.providers.github.issues import register as register_issues
    from app.tools.base import CallableToolExecutor

    issues = {t.name: t for t in register_issues({}, CallableToolExecutor(), None)}
    contents = {t.name: t for t in register_contents({}, CallableToolExecutor(), None)}
    assert issues["create_github_issue"].landed_probe is not None
    assert contents["create_github_commit"].landed_probe is not None
    assert issues["update_github_issue"].landed_probe is None
