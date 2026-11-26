"""The platform insights suite without a database.

Registration (one built-in id, classified as a built-in when an agent lacks it,
every tool a read exempt from approval), the descriptions that carry the
dashboard's caveats, the General Assistant's administrator-only default, the
chat chart block (its contract, its place in an answer, its round trip through
the message schema and the conversation export), ``render_chart``'s pivot and
the release history helpers. The figures themselves are compared with the
dashboards in ``test_platform_insights_postgres.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

SUITE = "platform_insights"
TOOLS = {
    "query_platform_analytics",
    "list_platform_traces",
    "list_platform_sessions",
    "query_platform_objects",
    "get_platform_release_history",
    "query_ai_adoption",
    "query_audit_log",
    "get_platform_overview",
    "query_platform_metrics",
    "query_platform_frictions",
    # Each person's own default chat agent (#529).
    "get_user_default_agents",
    "render_chart",
    # The admin-only platform AI settings read.
    "get_platform_ai_settings",
    # The admin-only OAuth defaults read (booleans, never a value).
    "list_oauth_defaults",
}
#: The admin-only write served with the suite: gated like any write.
WRITE_TOOLS = {
    "update_platform_ai_settings",
    "purge_platform_storage",
    "upsert_oauth_default",
    "delete_oauth_default",
    "create_integration_app_secret",
    "apply_oauth_default_to_integration",
}


def _definitions():
    from app.services.builtin_tools import build_platform_insights_tools

    defs, _executor = build_platform_insights_tools("")
    return {definition.name: definition for definition in defs}


# ── Registration ────────────────────────────────────────────────────────────


def test_the_suite_is_one_builtin_id_of_the_catalog():
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    catalog = build_builtin_tool_catalog(include_disabled=True)
    assert {tool["name"] for tool in catalog[SUITE]} == TOOLS | WRITE_TOOLS


def test_a_call_on_an_agent_without_the_suite_is_a_real_builtin_not_a_typo():
    from app.tools.builtin_suites import builtin_tool_witnesses, resolve_builtin_tool

    for name in TOOLS:
        resolved = resolve_builtin_tool(name)
        assert resolved is not None and resolved.suite == SUITE and resolved.match == "exact", name
    # Any other tool of the suite proves it is wired: the suite is its only grantor.
    assert "render_chart" in builtin_tool_witnesses("query_platform_analytics")


def test_every_tool_is_a_read_exempt_from_approval():
    from app.services.hitl_whitelist_default import SHIPPED_DEFAULT_WHITELIST
    from app.services.tool_classification import classify_tool_name

    for name in TOOLS:
        assert classify_tool_name(name)[0] == "read", name
        assert name in SHIPPED_DEFAULT_WHITELIST, name
    # The override is the chart tool's alone: "render" stays a write elsewhere.
    assert classify_tool_name("render_video")[0] == "write"
    for name in WRITE_TOOLS:
        assert classify_tool_name(name)[0] != "read", name
        assert name not in SHIPPED_DEFAULT_WHITELIST, name


def test_the_agent_builder_offers_the_suite():
    from app.services.builtin_tools.agent_builder import _VALID_BUILTIN_TOOL_IDS

    assert SUITE in _VALID_BUILTIN_TOOL_IDS


def test_the_descriptions_carry_the_dashboard_caveats():
    defs = _definitions()
    analytics = defs["query_platform_analytics"].description
    for caveat in ("CURRENT membership", "aggregation_stale", "stats_coverage_start", "unattributed", "never zero"):
        assert caveat in analytics, caveat
    assert "before_release_history" in defs["query_platform_objects"].description
    assert "never backfilled" in defs["get_platform_release_history"].description
    assert "own activity" in defs["query_ai_adoption"].description
    assert "200" in defs["query_audit_log"].description
    assert "16 categories" in defs["render_chart"].description
    for name in ("query_platform_analytics", "query_platform_objects", "get_platform_overview"):
        assert "Admin-gated" in defs[name].description, name
    # A group manager's team reaches their LLM analytics, never the
    # trace and session lists.
    assert "group manager" in defs["query_platform_analytics"].description
    assert "administrators excepted" in defs["query_platform_analytics"].description
    for name in ("list_platform_traces", "list_platform_sessions"):
        assert "group manager" not in defs[name].description, name
    assert "never what it holds" in defs["list_platform_traces"].description


# ── General Assistant ───────────────────────────────────────────────────────


def test_the_general_assistant_carries_the_suite_for_administrators_only():
    from app.models.general_assistant_config import (
        DEFAULT_GA_BUILTIN_TOOL_IDS,
        GENERAL_ASSISTANT_BUILTIN_TOOL_IDS,
        general_assistant_turn_tools,
    )

    assert SUITE in DEFAULT_GA_BUILTIN_TOOL_IDS and SUITE in GENERAL_ASSISTANT_BUILTIN_TOOL_IDS
    tools = ["web_fetch", SUITE, "generate_file"]
    assert general_assistant_turn_tools(tools, is_admin=True) == tools
    assert general_assistant_turn_tools(tools, is_admin=False) == ["web_fetch", "generate_file"]


def test_the_migration_adds_the_suite_once_and_keeps_an_unreadable_list():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "alembic/versions/g6258gainsights_add_platform_insights_to_ga_tools.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert json.loads(module.with_added_tools('["web_fetch"]')) == ["web_fetch", SUITE]
    assert module.with_added_tools(json.dumps(["web_fetch", SUITE])) is None
    assert json.loads(module.with_added_tools(None)) == [SUITE]
    assert module.with_added_tools("not json") is None
    assert module.with_added_tools('{"a": 1}') is None


# ── The chat chart block ────────────────────────────────────────────────────


_CHART = {
    "kind": "line",
    "categories": ["2026-09-28", "2026-10-05"],
    "series": [{"name": "Cost", "values": [1.5, 2.25]}],
    "unit": "USD",
    "format": "currency",
    "title": "Customer Success cost",
    "caption": "Current group membership",
}


def test_the_chart_contract_is_the_presentation_chart_block():
    from app.services.chat_charts import ChartBlockError, validate_chart

    block = validate_chart(_CHART)
    assert block["type"] == "chart" and block["categories"] == _CHART["categories"]
    assert "size" not in block, "defaults are not stored"
    with pytest.raises(ChartBlockError, match="at most 16 categories"):
        validate_chart({"kind": "bar", "categories": [str(i) for i in range(17)], "series": [{"name": "x", "values": list(range(17))}]})
    with pytest.raises(ChartBlockError, match="kind must be one of"):
        validate_chart({**_CHART, "kind": "gauge"})
    with pytest.raises(ChartBlockError):
        validate_chart({**_CHART, "series": [{"name": "Cost", "values": [1.0]}]})


def test_the_chart_block_round_trips_through_the_message_schema_and_the_export():
    from app.api.v1.chat import MessageResponse
    from app.services.chat_charts import chart_fence, validate_chart
    from app.services.conversation_export import conversation_markdown

    block = validate_chart(_CHART)
    content = f"Here is the cost.\n\n{chart_fence(block)}\n\nCosts are in USD."
    message = MessageResponse(
        id="m1", role="assistant", content=content, channel="web", sequence=1, created_at=datetime.now(UTC).isoformat()
    )
    assert message.charts == [block]
    assert MessageResponse.model_validate(message.model_dump()).charts == [block]
    plain = MessageResponse(id="m2", role="user", content="no chart", channel="web", sequence=0, created_at="x")
    assert plain.charts is None

    exported = conversation_markdown(
        "Costs",
        [SimpleNamespace(role="user", content="graph it", created_at=datetime.now(UTC)),
         SimpleNamespace(role="assistant", content=content, created_at=datetime.now(UTC))],
    )
    assert "xtm-chart" not in exported
    assert "| | Cost |" in exported
    assert "| 2026-09-28 | $1.50 |" in exported
    assert "**Customer Success cost**" in exported and "_Current group membership_" in exported


def test_a_chart_is_placed_at_its_marker_or_after_the_answer_and_never_twice():
    from app.services.chat_charts import CHART_FENCE_LANGUAGE, inject_charts, new_chart, validate_chart

    first, second = new_chart(validate_chart(_CHART)), new_chart(validate_chart({**_CHART, "title": "Other"}))
    sink = [first, second]
    segment = inject_charts(f"Weekly cost:\n\n{first.marker}\n\nmore soon", sink)
    assert segment.index("Weekly cost") < segment.index(CHART_FENCE_LANGUAGE) < segment.index("more soon")
    assert segment.count(CHART_FENCE_LANGUAGE) == 2  # the second, left out, follows the answer
    assert sink == []
    # A marker already placed (or made up) is never shown as text.
    final = inject_charts(f"Done. {first.marker} [[CHART:0123456789ab]]", sink)
    assert final == "Done."


def test_render_chart_pivots_rows_into_categories_and_series():
    from app.services.platform_insights.charts import build_chart

    rows = [
        {"bucket": "2026-10-05", "dimension": "model-one", "value": 1},
        {"bucket": "2026-10-05", "dimension": "model-two", "value": 2},
        {"bucket": "2026-10-12", "dimension": "model-one", "value": 3},
    ]
    block = build_chart({"rows": rows, "kind": "stacked_bar", "unit": "USD", "format": "currency"})
    assert block["categories"] == ["2026-10-05", "2026-10-12"]
    assert block["series"] == [{"name": "model-one", "values": [1.0, 3.0]}, {"name": "model-two", "values": [2.0, 0.0]}]

    single = build_chart({"rows": [{"bucket": None, "dimension": "a-very-long-agent-name-beyond-24", "value": 4}, {"bucket": None, "dimension": "b", "value": 1}], "series_name": "Traces"})
    assert single["series"] == [{"name": "Traces", "values": [4.0, 1.0]}]
    assert len(single["categories"][0]) <= 24 and single["categories"][0].endswith("…")


def test_render_chart_refuses_what_it_would_have_to_invent():
    from app.services.platform_insights.charts import build_chart
    from app.services.platform_insights.scope import InsightRefusedError

    with pytest.raises(InsightRefusedError, match="masked cost"):
        build_chart({"rows": [{"bucket": "w1", "dimension": None, "value": None}]})
    with pytest.raises(InsightRefusedError, match="missing_values zero only"):
        build_chart(
            {
                "rows": [{"bucket": "w1", "dimension": "a", "value": 1}, {"bucket": "w2", "dimension": "b", "value": 2}],
                "missing_values": "error",
            }
        )
    many = [{"bucket": None, "dimension": f"user-{i}", "value": i} for i in range(30)]
    with pytest.raises(InsightRefusedError, match="at most 16"):
        build_chart({"rows": many})
    folded = build_chart({"rows": many, "fold_others": True})
    assert len(folded["categories"]) == 16 and folded["categories"][-1] == "Other (15)"
    assert sum(folded["series"][0]["values"]) == pytest.approx(sum(range(30)))
    with pytest.raises(InsightRefusedError, match="one total"):
        build_chart({"rows": [{"bucket": None, "dimension": None, "value": 3}]})


def test_render_chart_hands_the_block_to_the_turn_or_to_the_reply():
    from app.services.builtin_tools import build_platform_insights_tools

    rows = [{"bucket": "w1", "dimension": None, "value": 1}, {"bucket": "w2", "dimension": None, "value": 2}]
    sink: list = []
    _defs, executor = build_platform_insights_tools("", chart_sink=sink)
    answer = json.loads(asyncio.run(executor.handler("render_chart")(rows=rows, kind="line", title="Cost")))
    assert answer["marker"] == sink[0].marker and sink[0].block["title"] == "Cost"

    _defs, executor = build_platform_insights_tools("")
    answer = json.loads(asyncio.run(executor.handler("render_chart")(rows=rows, kind="line")))
    assert answer["chart_block"].startswith("```xtm-chart\n")


def test_a_channel_and_an_export_get_the_table():
    from app.services.chat_charts import chart_fence, charts_as_tables, validate_chart

    text = charts_as_tables("Before\n\n" + chart_fence(validate_chart(_CHART)) + "\n\nAfter")
    assert text.startswith("Before") and text.endswith("After") and "| 2026-10-05 | $2.25 |" in text
    assert charts_as_tables("```xtm-chart\nnot json\n```") == "```xtm-chart\nnot json\n```"


# ── Releases, refusals ──────────────────────────────────────────────────────


def _windows():
    from app.services.platform_builds import ReleaseWindow

    old = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
    new = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    return [
        ReleaseWindow("1.0.0 (aaaaaaa)", "1.0.0", "aaaaaaa1", old, new, new),
        ReleaseWindow("1.1.0 (bbbbbbb)", "1.1.0", "bbbbbbb2", new, new + timedelta(days=5), None),
    ]


def test_a_day_counts_for_the_build_first_seen_by_its_end():
    from app.services.platform_builds import BEFORE_HISTORY_BUCKET, history_note, release_of_day, release_order

    windows = _windows()
    assert release_of_day(date(2026, 8, 31), windows) == BEFORE_HISTORY_BUCKET
    assert release_of_day(date(2026, 9, 1), windows) == "1.0.0 (aaaaaaa)"
    assert release_of_day(date(2026, 10, 1), windows) == "1.1.0 (bbbbbbb)"
    assert release_order(BEFORE_HISTORY_BUCKET, windows) < release_order("1.0.0 (aaaaaaa)", windows)
    assert "before it" in history_note(windows, datetime(2026, 8, 1, tzinfo=UTC))
    assert "No build is recorded" in history_note([])


def test_a_newer_build_booted_while_an_older_one_answers_is_a_rollout_in_progress():
    """The Deploy workflow is green, the new pod booted and recorded itself, the old pod still answers: the history
    says so instead of the previous build alone."""
    from app.services.platform_builds import ROLLOUT_IN_PROGRESS, rollout

    windows = _windows()
    new = windows[1].first_seen_at
    found = rollout(windows, ("1.0.0", "aaaaaaa1"), now=new + timedelta(minutes=3))
    assert found.state == ROLLOUT_IN_PROGRESS
    assert found.words() == (
        "rollout in progress: 1.1.0 (bbbbbbb), started 2026-10-01T09:00:00+00:00 (its first pod booted); this answer "
        "comes from 1.0.0 (aaaaaaa), still serving"
    )
    payload = found.payload()
    assert payload["state"] == "in_progress" and payload["git_sha"] == "bbbbbbb2"
    assert payload["started_at"] == new.isoformat() and payload["answered_by"] == "1.0.0 (aaaaaaa)"
    assert payload["previous_release"] == "1.0.0 (aaaaaaa)"


def test_an_answering_build_no_row_names_is_the_one_rolling_out_not_an_old_one_still_serving() -> None:
    """#2219: a row is written at a build's first boot, so an answering build that no row names is newer than every
    recorded build: the newest row is never told 'in progress' with the new build as the old one still serving."""
    from app.services.platform_builds import ROLLOUT_IN_PROGRESS, ROLLOUT_UNRECORDED, rollout

    windows = _windows()  # type: ignore[no-untyped-call]
    new = windows[1].first_seen_at
    found = rollout(windows, ("1.2.0", "ccccccc3"), now=new + timedelta(minutes=3))
    assert found.state == ROLLOUT_UNRECORDED and found.state != ROLLOUT_IN_PROGRESS
    words = found.words()
    assert "still serving" not in words
    assert words.startswith("rollout in progress: 1.2.0 (ccccccc) answers and is newer than every recorded build")
    assert found.payload()["answered_by"] == "1.2.0 (ccccccc)" and found.payload()["release"] == "1.1.0 (bbbbbbb)"
    # A recorded older build answering is still a rollout in progress of the newest.
    assert rollout(windows, ("1.0.0", "aaaaaaa1"), now=new + timedelta(minutes=3)).state == ROLLOUT_IN_PROGRESS


def test_a_rollout_is_complete_once_the_newest_build_answers_and_the_previous_one_is_gone():
    from app.services.platform_builds import (
        DRAIN_GRACE,
        ROLLOUT_COMPLETE,
        ROLLOUT_DRAINING,
        ROLLOUT_ROLLED_BACK,
        ROLLOUT_UNKNOWN,
        ReleaseWindow,
        rollout,
    )

    windows = _windows()
    new = windows[1].first_seen_at
    done = rollout(windows, ("1.1.0", "bbbbbbb2"), now=new + timedelta(days=6))
    assert done.state == ROLLOUT_COMPLETE
    assert done.words() == "rollout complete: 1.1.0 (bbbbbbb) is the deployed build since 2026-10-01T09:00:00+00:00"
    # The previous build was still seen after the newest booted, minutes ago: its pods may still be draining.
    old = windows[0]
    draining = [
        ReleaseWindow(old.label, old.version, old.git_sha, old.first_seen_at, new + timedelta(minutes=2), new),
        windows[1],
    ]
    found = rollout(draining, ("1.1.0", "bbbbbbb2"), now=new + timedelta(minutes=3))
    assert found.state == ROLLOUT_DRAINING and "may still be draining" in found.words()
    # Past the grace, the same rows read as complete.
    assert (
        rollout(draining, ("1.1.0", "bbbbbbb2"), now=new + timedelta(minutes=2) + DRAIN_GRACE).state == ROLLOUT_COMPLETE
    )
    # The grace is a couple of ticks, not a quarter of an hour: an old pod that stopped refreshing is gone soon,
    # and one that still serves refreshes well within it, so "complete" is never read while it answers.
    assert DRAIN_GRACE.total_seconds() <= 180
    # The newest build not seen for longer than the grace while an older one answers: a rollback, not a rollout.
    back = rollout(windows, ("1.0.0", "aaaaaaa1"), now=windows[1].last_seen_at + DRAIN_GRACE)
    assert back.state == ROLLOUT_ROLLED_BACK and back.words().startswith("rolled back: 1.1.0 (bbbbbbb)")
    # Nothing recorded: nothing known.
    nothing = rollout([], ("1.1.0", "bbbbbbb2"))
    assert nothing.state == ROLLOUT_UNKNOWN and nothing.payload()["release"] is None


def test_a_process_without_its_commit_reads_its_own_build_as_complete():
    """A worker image built without GIT_SHA (#10718) answers (version, ""): the row its platform sibling recorded
    with the commit is the same build, not a rollout in progress for the life of the build."""
    from app.services.platform_builds import ROLLOUT_COMPLETE, ROLLOUT_IN_PROGRESS, is_build, rollout

    windows = _windows()
    new = windows[1].first_seen_at
    assert is_build(windows[1], "1.1.0", "") and is_build(windows[1], "1.1.0", "bbbbbbb2")
    assert not is_build(windows[1], "1.1.0", "ccccccc3") and not is_build(windows[1], "1.0.0", "")
    assert rollout(windows, ("1.1.0", ""), now=new + timedelta(minutes=3)).state == ROLLOUT_COMPLETE
    # An older build without its commit still sees the newer one rolling out.
    assert rollout(windows, ("1.0.0", ""), now=new + timedelta(minutes=3)).state == ROLLOUT_IN_PROGRESS


def test_the_release_history_tool_says_where_the_rollout_stands():
    """The windows read from the rows (newest first, as the query orders them) feed the rollout, and the tool's
    description says it answers 'is this fix deployed'."""
    from app.services.platform_builds import ROLLOUT_IN_PROGRESS, release_windows, rollout

    assert "rollout" in _definitions()["get_platform_release_history"].description
    windows = _windows()

    class _Db:
        async def execute(self, _statement):
            rows = [
                SimpleNamespace(
                    version=w.version, git_sha=w.git_sha, first_seen_at=w.first_seen_at, last_seen_at=w.last_seen_at
                )
                for w in reversed(windows)
            ]
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    found = asyncio.run(release_windows(_Db()))
    assert [w.label for w in found] == ["1.0.0 (aaaaaaa)", "1.1.0 (bbbbbbb)"]
    assert rollout(found, ("1.0.0", "aaaaaaa1"), now=windows[1].first_seen_at).state == ROLLOUT_IN_PROGRESS


def test_an_ancestor_newest_by_first_boot_reads_superseded_not_in_progress(monkeypatch):
    """The rows rank builds by first boot: when the answering build's commit carries the newest row's, the newest
    is an ancestor that booted first, and the rollout is complete on the answering build (#1987)."""
    from app.services import platform_builds
    from app.services.platform_builds import (
        DRAIN_GRACE,
        ROLLOUT_IN_PROGRESS,
        ROLLOUT_ROLLED_BACK,
        ROLLOUT_SUPERSEDED,
        read_rollout,
        rollout,
    )

    windows = _windows()
    new = windows[1].first_seen_at
    running = ("1.0.0", "aaaaaaa1")
    found = rollout(windows, running, now=new + timedelta(minutes=3), running_carries_newest=True)
    assert found.state == ROLLOUT_SUPERSEDED
    assert "ancestor, not a rollout in progress" in found.words() and found.payload()["state"] == "superseded"

    asked: list[tuple[str, str]] = []

    async def _carries(_db, _user, newest, running_sha):
        asked.append((newest.git_sha, running_sha))
        return carries

    monkeypatch.setattr(platform_builds, "running_carries", _carries)
    carries = True
    read = asyncio.run(read_rollout(None, windows, running, user_id="u", now=new + timedelta(minutes=3)))
    assert read.state == ROLLOUT_SUPERSEDED and asked == [("bbbbbbb2", "aaaaaaa1")]
    carries = None
    late = new + timedelta(days=5) + DRAIN_GRACE
    assert asyncio.run(read_rollout(None, windows, running, user_id="u", now=late)).state == ROLLOUT_ROLLED_BACK
    carries = False
    assert (
        asyncio.run(read_rollout(None, windows, running, user_id="u", now=new + timedelta(minutes=3))).state
        == ROLLOUT_IN_PROGRESS
    )
    # The newest build answering: nothing to read.
    asked.clear()
    asyncio.run(read_rollout(None, windows, ("1.1.0", "bbbbbbb2"), user_id="u", now=new + timedelta(days=6)))
    assert asked == []


def test_running_carries_reads_the_containment_reader_on_the_observed_repository(monkeypatch):
    from app.integrations import reference_state
    from app.services import work_item_blocked_refs
    from app.services.platform_builds import running_carries

    windows = _windows()
    calls: list[tuple] = []

    async def _reader(repo, commit, build_commit, token):
        calls.append((repo, commit, build_commit, token))
        return True

    async def _token(_db, provider, _user):
        return "tok" if provider == "github" else None

    monkeypatch.setattr(reference_state, "_CONTAINMENT_READERS", {"github": _reader})
    monkeypatch.setattr(work_item_blocked_refs, "reference_token", _token)
    monkeypatch.setattr(
        work_item_blocked_refs, "observed_deployment_repositories", lambda: frozenset({"xtm-one-platform/xtm-one"})
    )
    assert asyncio.run(running_carries(None, "u", windows[1], "AAAAAAA1")) is True
    assert calls == [("xtm-one-platform/xtm-one", "bbbbbbb2", "aaaaaaa1", "tok")]
    # A commit too short to name a build: nothing read.
    assert asyncio.run(running_carries(None, "u", windows[1], "")) is None and len(calls) == 1


def _history_tool(monkeypatch, windows, running=("1.1.0", "bbbbbbb2")):
    """``get_platform_release_history`` over *windows*, the process answering as *running*, no database."""
    from contextlib import asynccontextmanager

    from app.core import database
    from app.services import platform_builds
    from app.services.builtin_tools import build_platform_insights_tools
    from app.services.platform_insights import scope as scope_module

    @asynccontextmanager
    async def _session():
        yield None

    async def _windows_of(_db):
        return list(windows)

    async def _scope(_db, _user_id):
        return SimpleNamespace(header=lambda: {"scope": "platform"}, user=SimpleNamespace(id="u"))

    async def _carries_unread(_db, _user, _newest, _running_sha):
        return None

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(platform_builds, "running_carries", _carries_unread)
    monkeypatch.setattr(scope_module, "load_scope", _scope)
    monkeypatch.setattr(platform_builds, "release_windows", _windows_of)
    monkeypatch.setattr(platform_builds, "running_build", lambda: running)
    _defs, executor = build_platform_insights_tools("")

    def call(**args):
        return asyncio.run(executor.handler("get_platform_release_history")(**args))

    return call


def _many_windows(count: int):
    from app.services.platform_builds import ReleaseWindow

    start = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    windows = []
    for index in range(count):
        seen = start + timedelta(hours=index)
        sha = f"{index:07x}0"
        until = seen + timedelta(hours=1) if index + 1 < count else None
        windows.append(
            ReleaseWindow(f"1.{index}.0 ({sha[:7]})", f"1.{index}.0", sha, seen, seen + timedelta(minutes=50), until)
        )
    return windows


def test_the_release_window_slice_serves_the_newest_first():
    from app.services.platform_builds import release_window_slice

    windows = _many_windows(10)
    assert release_window_slice(windows) == windows
    newest = release_window_slice(windows, last_n=3)
    assert [w.version for w in newest] == ["1.9.0", "1.8.0", "1.7.0"]
    since = windows[6].first_seen_at
    assert [w.version for w in release_window_slice(windows, since=since)] == ["1.9.0", "1.8.0", "1.7.0", "1.6.0"]
    assert [w.version for w in release_window_slice(windows, since=since, last_n=2)] == ["1.9.0", "1.8.0"]
    assert release_window_slice(windows, since=windows[-1].first_seen_at + timedelta(seconds=1)) == []


def test_the_release_history_tool_without_an_argument_answers_every_build_oldest_first(monkeypatch):
    windows = _many_windows(200)
    call = _history_tool(monkeypatch, windows, running=("1.199.0", windows[-1].git_sha))
    answer = json.loads(call())
    assert [r["version"] for r in answer["releases"]] == [w.version for w in windows]
    assert "window" not in answer and answer["history_starts_at"] == windows[0].first_seen_at.isoformat()
    assert answer["rollout"]["state"] in {"complete", "draining"}


def test_last_n_answers_the_newest_releases_under_five_kb(monkeypatch):
    windows = _many_windows(200)
    call = _history_tool(monkeypatch, windows, running=("1.199.0", windows[-1].git_sha))
    raw = call(last_n=5)
    answer = json.loads(raw)
    assert [r["version"] for r in answer["releases"]] == ["1.199.0", "1.198.0", "1.197.0", "1.196.0", "1.195.0"]
    assert answer["window"] == {
        "last_n": 5,
        "since": None,
        "order": "newest_first",
        "served": 5,
        "recorded_releases": 200,
    }
    assert len(raw.encode()) < 5_000
    assert "positive integer" in call(last_n=0)


def test_since_answers_the_releases_first_seen_at_or_after_it(monkeypatch):
    windows = _many_windows(20)
    call = _history_tool(monkeypatch, windows, running=("1.19.0", windows[-1].git_sha))
    cut = windows[17].first_seen_at
    answer = json.loads(call(since=cut.isoformat().replace("+00:00", "Z")))
    assert [r["version"] for r in answer["releases"]] == ["1.19.0", "1.18.0", "1.17.0"]
    assert answer["window"]["since"] == cut.isoformat() and answer["window"]["served"] == 3
    assert "ISO-8601" in call(since="yesterday")


def test_running_only_answers_the_running_build_and_the_rollout_alone(monkeypatch):
    windows = _many_windows(20)
    running = windows[-1]
    call = _history_tool(monkeypatch, windows, running=(running.version, running.git_sha))
    raw = call(running_only=True)
    answer = json.loads(raw)
    assert "releases" not in answer
    assert answer["running"] == {
        "version": "1.19.0",
        "git_sha": running.git_sha,
        "release": running.label,
        "first_seen_at": running.first_seen_at.isoformat(),
        "previous_release": windows[-2].label,
    }
    assert answer["rollout"]["release"] == running.label and answer["window"]["recorded_releases"] == 20
    assert len(raw.encode()) < 2_500


def test_the_release_history_tool_documents_its_window():
    definition = _definitions()["get_platform_release_history"]
    assert set(definition.input_schema["properties"]) == {"last_n", "since", "running_only"}
    for word in ("running_only", "last_n", "since", "NEWEST first", "oldest first"):
        assert word in definition.description, word


def test_the_release_bucket_is_one_case_over_the_first_boots():
    from sqlalchemy import column
    from sqlalchemy.dialects import postgresql

    from app.services.platform_builds import release_case

    sql = str(
        release_case(column("created_at"), _windows()).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert sql.index("1.1.0 (bbbbbbb)") < sql.index("1.0.0 (aaaaaaa)")
    assert sql.endswith("ELSE 'before_release_history' END")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"metric": "latency", "group_by": "week"}, "Use metric duration"),
        ({"metric": "latency", "group_by": "model", "split_by": "source"}, "Use metric duration"),
        ({"metric": "spend"}, "metric must be one of"),
        ({"metric": "cost", "split_by": "model"}, "use group_by model"),
        ({"metric": "cost", "group_by": "agent", "split_by": "agent"}, "use another dimension"),
        ({"metric": "cost", "group_by": "agent", "projection": {"method": "linear"}}, "use group_by month"),
        ({"metric": "cost", "group_by": "month", "projection": {"method": "median"}}, "projection.method"),
        ({"metric": "cost", "group_by": "week", "compare_to": "previous_period"}, "widen the period"),
        ({"metric": "cost", "attribution": "guess"}, "attribution must be one of"),
        ({"metric": "cost", "group_kinds": ["team"]}, "group_kinds must be among"),
        ({"metric": "cost", "period": "custom"}, "date_from and date_to are required"),
    ],
    ids=[
        "latency-by-week",
        "latency-split",
        "unknown-metric",
        "split-without-group-by",
        "split-repeats-group-by",
        "projection-off-month",
        "unknown-projection",
        "compare-a-series",
        "unknown-attribution",
        "unknown-group-kind",
        "custom-without-dates",
    ],
)
def test_analytics_refuses_what_it_does_not_compute(args, message):
    from app.services.platform_insights.analytics import query_platform_analytics
    from app.services.platform_insights.scope import InsightRefusedError

    scope = SimpleNamespace(platform=True, user=SimpleNamespace(id="u"), group_ids=(), header=lambda: {})
    with pytest.raises(InsightRefusedError, match=message):
        asyncio.run(query_platform_analytics(None, scope, args))


def test_a_read_of_the_audit_log_has_its_own_audit_window():
    from app.services.builtin_tools.traces import trace_read_audit_key

    details = {"filters": {"action": ["delete"]}, "window_seconds": 900}
    assert trace_read_audit_key("u", None, details) != trace_read_audit_key("u", None, details, action="inspect_audit_log")
    assert trace_read_audit_key("u", None, details, action="inspect_audit_log") == trace_read_audit_key(
        "u", None, dict(reversed(details.items())), action="inspect_audit_log"
    )


def test_the_conversation_export_route_is_mounted():
    from app.main import application

    paths = {getattr(route, "path", "") for route in application.routes}
    assert "/api/v1/chat/conversations/{conversation_id}/export" in paths
