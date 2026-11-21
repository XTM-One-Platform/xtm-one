"""Tool results bounded to the fact asked for (#11707).

One test per member case of the harness gap family oversized-tool-results that
this change covers: list_frictions and the undisposed-frictions event filter by
kind, get_github_commit bounds a long message, sandbox_diff_files says a
line-ending-only change in one line, sandbox_edit_file echoes a bounded diff.
"""

from __future__ import annotations

import asyncio
import uuid

from app.integrations.providers.github import commits
from app.services import frictions, platform_events
from app.services.builtin_tools import build_friction_tools
from app.services.sandbox_workspace import checks, commands, editing, filediff
from app.tools.base import ToolCall

AGENT = uuid.uuid4()
USER = uuid.uuid4()
RUN = uuid.uuid4()


def _call(executor, name, **arguments):
    return asyncio.run(executor.execute_tool(ToolCall(id=name, name=name, arguments=arguments))).content


# -- list_frictions: a kind filter ------------------------------------------------


def test_list_frictions_passes_the_kinds_to_the_store(monkeypatch):
    seen: dict = {}

    async def fake_list(**kwargs):
        seen.update(kwargs)
        return [], 0

    monkeypatch.setattr(frictions, "list_frictions", fake_list)
    _, executor = build_friction_tools(str(USER), str(AGENT), run_id=RUN)
    text = _call(executor, "list_frictions", kind="turn_resumed, tool_error")
    assert seen["kinds"] == ["turn_resumed", "tool_error"]
    assert "of kind turn_resumed, tool_error" in text


def test_list_frictions_without_a_kind_filters_nothing(monkeypatch):
    seen: dict = {}

    async def fake_list(**kwargs):
        seen.update(kwargs)
        return [], 0

    monkeypatch.setattr(frictions, "list_frictions", fake_list)
    _, executor = build_friction_tools(str(USER), str(AGENT), run_id=RUN)
    _call(executor, "list_frictions")
    assert seen["kinds"] is None


def test_list_frictions_refuses_an_unknown_kind(monkeypatch):
    async def fake_list(**kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("listed")

    monkeypatch.setattr(frictions, "list_frictions", fake_list)
    _, executor = build_friction_tools(str(USER), str(AGENT), run_id=RUN)
    text = _call(executor, "list_frictions", kind="nope")
    assert text.startswith("Error: unknown kind nope") and "Nothing was listed" in text


def _friction_row(run_id: str | None) -> dict:
    return {"id": "f1", "kind": "turn_resumed", "run_id": run_id, "class": "unclassified", "evidence": "resumed"}


def test_list_frictions_by_agent_names_the_run_of_each_row(monkeypatch):
    run_id = str(uuid.uuid4())

    async def fake_list(**kwargs):
        return [_friction_row(run_id)], 1

    monkeypatch.setattr(frictions, "list_frictions", fake_list)
    _, executor = build_friction_tools(str(USER), str(AGENT), run_id=RUN)
    text = _call(executor, "list_frictions", agent_id=str(AGENT), kind="turn_resumed")
    assert f"- `f1` turn_resumed in run {run_id}" in text


def test_list_frictions_of_one_run_does_not_repeat_the_run(monkeypatch):
    async def fake_list(**kwargs):
        return [_friction_row(str(RUN))], 1

    monkeypatch.setattr(frictions, "list_frictions", fake_list)
    _, executor = build_friction_tools(str(USER), str(AGENT), run_id=RUN)
    text = _call(executor, "list_frictions")
    assert "in run" not in text and "- `f1` turn_resumed" in text


def test_the_kind_filter_is_a_clause_of_the_query(monkeypatch):
    statements: list = []

    class _Result:
        def scalar(self):
            return 0

        def scalars(self):
            return self

        def all(self):
            return []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt):
            statements.append(str(stmt))
            return _Result()

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    asyncio.run(frictions.list_frictions(user_id=USER, kinds=["turn_resumed"]))
    assert statements and all("agent_frictions.kind IN" in s for s in statements)


# -- run.completed_with_undisposed_frictions: kinds to filter on -------------------


def test_the_undisposed_event_carries_its_kinds_and_declares_the_filter_key():
    data = platform_events.undisposed_frictions_data(
        run_id=RUN, agent_id=AGENT, friction_ids=["a", "b"], kinds=["turn_resumed", "tool_error", "turn_resumed"]
    )
    assert data["kinds"] == ["tool_error", "turn_resumed"] and data["count"] == 2
    assert "kinds" in platform_events.PLATFORM_EVENTS["run.completed_with_undisposed_frictions"]["filter_keys"]


def test_settling_a_run_publishes_the_undisposed_kinds(monkeypatch):
    published: list = []

    async def fake_publish(name, data, *, event_id=None):
        published.append((name, dict(data)))
        return event_id

    async def fake_ids(run_id):
        return ["f1"]

    async def fake_kinds(run_id):
        return ["turn_resumed"]

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt):
            return None

        async def commit(self):
            return None

    class _Run:
        id = RUN
        agent_id = AGENT
        assignment_id = None
        frictions_undisposed = None

    import app.core.database as database

    monkeypatch.setattr(platform_events, "publish", fake_publish)
    monkeypatch.setattr(frictions, "undisposed_of_run", fake_ids)
    monkeypatch.setattr(frictions, "undisposed_kinds_of_run", fake_kinds)
    monkeypatch.setattr(database, "async_session", lambda: _Session())
    assert asyncio.run(frictions.settle_run_frictions(_Run())) == 1
    assert published[0][1]["kinds"] == ["turn_resumed"]


# -- get_github_commit: a 63 KB message bounded ------------------------------------


def test_a_long_commit_message_is_cut_with_the_rest_counted():
    message = "feat(x): subject\n\n" + "body line\n" * 6_300
    text = commits.bound_commit_message(message)
    assert len(text) < 1_700
    assert text.startswith("feat(x): subject") and "more characters; full_message=true reads it" in text
    assert commits.bound_commit_message(message, full=True) == message.strip()


def test_a_short_commit_message_is_kept_whole():
    assert commits.bound_commit_message("  fix(y): one line  ") == "fix(y): one line"
    assert commits.bound_commit_message(None) == ""


# -- sandbox_diff_files: line endings only -----------------------------------------


def test_a_line_ending_only_change_is_one_line_not_a_whole_file_diff():
    lf = "".join(f"line {i}\n" for i in range(2_000))
    crlf = lf.replace("\n", "\r\n")
    text = filediff.two_way("a.bat", crlf, "b.bat", lf)
    assert text == "a.bat and b.bat differ only by their line endings: CRLF -> LF.\n"


def test_line_endings_mixed_are_named_and_a_real_change_is_still_a_diff():
    assert filediff.line_endings_only("a\r\nb\n", "a\nb\n") == "mixed (1 CRLF, 1 LF) -> LF"
    assert filediff.line_endings_only("a\n", "a\n") is None
    assert filediff.line_endings_only("a\r\n", "b\n") is None
    assert "-a\r\n" in filediff.two_way("x", "a\r\n", "y", "b\n")


# -- sandbox_edit_file: the echoed diff bounded -------------------------------------


def test_the_edit_echo_is_bounded_on_a_line_with_the_way_to_read_the_rest():
    before = "".join(f"old {i}\n" for i in range(3_000))
    after = "".join(f"new {i}\n" for i in range(3_000))
    hint = "sandbox_checkout_diff or sandbox_read_file reads it"
    text = editing.unified_diff("x.py", before, after, max_chars=editing.EDIT_ECHO_CHARS, rest_hint=hint)
    assert len(text) < editing.EDIT_ECHO_CHARS + 200
    assert text.endswith(f"more characters; {hint}]\n")
    body = text.rsplit("... [diff cut:", 1)[0]
    assert body.endswith("\n")


# -- sandbox_run_command: findings not repeated in stdout ---------------------------


_PYTEST_OUT = "\n".join(
    [
        "....F.",
        "=================================== FAILURES ===================================",
        "___ test_a ___",
        *[f"    traceback line {i}" for i in range(400)],
        "=========================== short test summary info ============================",
        "FAILED tests/test_x.py::test_a - AssertionError",
        "========================= 1 failed, 5 passed in 1.00s ==========================",
    ]
)


def _report() -> checks.ReportSummary:
    return checks.ReportSummary(
        kind="junit",
        findings=[checks.Finding(path="tests.test_x::test_a", line=0, rule="", message="AssertionError")],
        tests=6,
        passed=5,
        failed=1,
        failures=[("test_a", "AssertionError\nbody")],
    )


def _result(report, failures_saved=True) -> str:
    return checks.format_result(
        check="backend-unit",
        argv=["pytest"],
        cwd="backend",
        exit_code=1,
        seconds=1.0,
        timed_out=False,
        stdout=_PYTEST_OUT,
        stderr="",
        run_dir=".xtm/runs/r",
        report=report,
        failures_saved=failures_saved,
    )


def test_the_failure_sections_are_left_out_of_stdout_when_the_findings_carry_them():
    text = _result(_report())
    assert "traceback line 7" not in text and "FAILED tests/test_x.py" not in text
    assert "pytest FAILURES, 402 line(s), left out" in text
    assert "1 failed, 5 passed in 1.00s" in text
    assert "tests.test_x::test_a" in text and len(text) < 2_000


def test_without_saved_failures_or_a_report_stdout_keeps_the_failures():
    assert "traceback line 7" in _result(_report(), failures_saved=False)
    assert "traceback line 7" in _result(None)


# -- sandbox_run_command without a check: a compact listing past 4k ----------------


def _specs(n: int) -> dict:
    return {
        f"check-{i}": checks.CheckSpec(
            name=f"check-{i}",
            argv=("python", "-m", "pytest", "-q", "--long-option", "x" * 40),
            cwd="backend",
            image="python-dev",
            description="A check described at length " * 6,
            default_args=("tests/" + "y" * 60,),
        )
        for i in range(n)
    }


def test_a_long_check_listing_is_compact_and_names_where_the_details_are():
    found = commands.CheckoutChecks(_specs(60), "declared", [])
    full = checks.describe_checks(found.specs)
    text = found.listing("repo")
    assert len(full) > 15_000 and len(text) < 8_000
    assert "- check-59: A check described at length" in text and "[image" not in text
    assert "60 checks, compact" in text and ".xtm-one/checks.toml" in text


def test_a_short_check_listing_keeps_every_detail():
    found = commands.CheckoutChecks(_specs(3), "declared", [])
    text = found.listing("repo")
    assert "[image python-dev]" in text and "compact" not in text


def test_the_default_diff_bound_is_unchanged_for_the_other_callers():
    before = "".join(f"old {i}\n" for i in range(3_000))
    after = "".join(f"new {i}\n" for i in range(3_000))
    assert len(editing.unified_diff("x.py", before, after)) > editing.EDIT_ECHO_CHARS * 5


# ── browser_control get_content (runner): the page's text, never its raw HTML ──


def test_a_runner_page_read_answers_its_text_not_its_markup():
    from app.services.builtin_tools.computer_use import _page_text_result

    script = "<script>" + "var x = 1;" * 5_000 + "</script><style>" + ".a{color:red}" * 2_000 + "</style>"
    html = f"<html><head><title>Sign-in</title>{script}</head><body><h1>Welcome</h1><p>Account: ops</p></body></html>"
    text = _page_text_result({"action": "get_content", "content": html})

    assert "Title: Sign-in" in text and "# Welcome" in text and "Account: ops" in text
    assert "var x" not in text and "color:red" not in text and "<p>" not in text
    assert len(text) < 4_000 < len(html)


def test_a_long_runner_page_text_is_cut_with_the_cut_said():
    from app.services.builtin_tools.computer_use import PAGE_TEXT_MAX_CHARS, _page_text_result

    html = "<body>" + "".join(f"<p>line {i}</p>" for i in range(20_000)) + "</body>"
    text = _page_text_result({"action": "get_content", "content": html, "frame": "#blade"})

    assert text.startswith("Frame: #blade")
    assert f"[Content truncated at {PAGE_TEXT_MAX_CHARS:,} characters]" in text
    assert len(text) < PAGE_TEXT_MAX_CHARS + 500
